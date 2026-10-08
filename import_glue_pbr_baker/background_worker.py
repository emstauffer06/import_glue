"""Private Blender worker launched by background_jobs; never used as UI code."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import types


def _package_modules():
    # A script launched with --python has no package context. Register a private
    # package namespace without importing/registering the add-on UI __init__.
    name = "_import_glue_background_runtime"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[name] = package
    return name, importlib.import_module(name + ".background_jobs")


class WorkerControl:
    """Atomic latest-event status + heartbeat; zero bpy operations from the thread."""
    def __init__(self, job_dir, jobs, cancelled_type):
        self.job_dir = Path(job_dir).resolve()
        self.jobs = jobs
        self.cancelled_type = cancelled_type
        self.sequence = 0
        self.event = {"kind": "worker_start", "stage": "opening_snapshot"}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.started = time.time()
        self.thread = threading.Thread(target=self._heartbeat, name="ImportGlueJobHeartbeat", daemon=True)
        self.thread.start()

    def _write(self):
        self.jobs.write_status(self.job_dir, "RUNNING", pid=os.getpid(), seq=self.sequence,
                               event=self.event, heartbeat_at=time.time(), started_at=self.started)

    def _heartbeat(self):
        while not self.stop.wait(2.0):
            with self.lock:
                if not self.stop.is_set():
                    try: self._write()
                    except OSError: pass  # Main-thread emit/terminal write remains authoritative and fails closed.

    def emit(self, event):
        allowed = {"kind", "stage", "target", "object", "route", "semantic", "field", "resolution",
                   "current", "completed", "total", "checkpoint", "reason", "message"}
        safe = {}
        for key, value in event.items():
            if key not in allowed: continue
            if isinstance(value, str): value = value[:2000]
            safe[key] = value
        with self.lock:
            self.sequence += 1
            self.event = self.jobs._json_copy(safe)
            self._write()

    def check_cancel(self):
        path = self.job_dir / "cancel.json"
        if path.exists():
            request = self.jobs.read_json(path)
            if request.get("schema") != self.jobs.SCHEMA or request.get("job_id") != self.job_dir.name:
                raise self.jobs.JobError("Cancellation request identity/schema mismatch")
            raise self.cancelled_type("Cancellation requested; completed checkpoints retained")

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3.0)


def _sources(bpy, engine, identities):
    engine.unexclude_all_collections()
    if bpy.ops.object.mode_set.poll(): bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    targets = []
    for identity in identities:
        # Empty library in a string identity is intentionally fail-closed when
        # linked and local objects share a name; never pick bpy.data.objects.get().
        candidates = [obj for obj in bpy.context.scene.objects if obj.name == identity["name"]]
        if identity["library"]:
            candidates = [obj for obj in candidates if obj.library is not None
                          and os.path.normcase(os.path.abspath(bpy.path.abspath(obj.library.filepath)))
                          == os.path.normcase(os.path.abspath(identity["library"]))]
        if len(candidates) != 1:
            raise RuntimeError("Source identity missing or ambiguous: %r" % identity)
        obj = candidates[0]
        if obj.type != "MESH" or (not obj.data.polygons and engine.TARGET_PROFILE == "LEGACY") or engine.is_previous_output(obj):
            raise RuntimeError("Selected source is empty, non-mesh, or prior output: " + obj.name)
        try: obj.hide_viewport = False
        except (AttributeError, RuntimeError): pass
        obj.hide_set(False)
        obj.select_set(True)
        if not obj.select_get(): raise RuntimeError("Cannot select exact source: " + obj.name)
        targets.append(obj)
    if not targets: raise RuntimeError("Empty source scope")
    bpy.context.view_layer.objects.active = targets[0]
    return targets


def _source_stats(bpy, targets):
    triangles = 0
    vertices = 0
    for obj in targets:
        vertices += len(obj.data.vertices)
        triangles += sum(max(0, len(p.vertices) - 2) for p in obj.data.polygons)
    textures = []
    for image in bpy.data.images:
        if image.source not in {"VIEWER", "GENERATED"} and len(image.size) >= 2:
            width, height = int(image.size[0]), int(image.size[1])
            if width > 0 and height > 0: textures.append([width, height])
    return {"vertices": vertices, "triangles": triangles, "objects": len(targets), "texture_dimensions": textures}


def _output_dependencies(bpy, objects, jobs):
    """Protect externally stored result maps as well as the .blend containing their references."""
    files = {}
    visited = set()
    def nodes(tree):
        if tree is None or tree.as_pointer() in visited: return
        visited.add(tree.as_pointer())
        for node in tree.nodes:
            image = getattr(node, "image", None)
            if image is not None and not image.packed_file:
                if image.source != "FILE" or not image.filepath:
                    raise RuntimeError("Output image is not a packed or file-backed deliverable: " + image.name)
                path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
                if not path.is_file(): raise RuntimeError("Output map disappeared: " + str(path))
                files[str(path)] = jobs.sha256_file(path)
            child = getattr(node, "node_tree", None)
            if child is not None: nodes(child)
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material is not None: nodes(slot.material.node_tree)
    return [{"path": path, "sha256": digest} for path, digest in sorted(files.items())]


def _resource_decision(engine, resources, policy, stats):
    # A sealed spec may override only a subset of engine settings. Admission
    # must describe the effective workload, including untouched engine defaults.
    effective = {key: getattr(engine, key) for key in ("RES", "MAX_RES", "DEVICE", "GPU_DEVICE_ID")}
    effective["TARGET_PROFILE"] = getattr(engine, "TARGET_PROFILE", "LEGACY")
    effective["NATIVE_QUALITY"] = getattr(engine, "NATIVE_QUALITY", None)
    return resources.evaluate_policy(policy, effective, stats)


def run(job_dir):
    package_name, jobs = _package_modules()
    directory = Path(job_dir).resolve()
    control = None
    exit_code = 1
    terminal = {"state": "FAILED", "reason": "Worker did not complete"}
    census = None
    trusted = False
    try:
        # Verify snapshot and code BEFORE allowing Blender to open the private scene.
        spec = jobs.load_job(directory, verify_snapshot=True, verify_engine=True)
        trusted = True
        import bpy
        control_module = importlib.import_module(package_name + ".run_control")
        control = WorkerControl(directory, jobs, control_module.RunCancelled)
        control.check_cancel()
        control.emit({"kind": "worker_start", "stage": "opening_snapshot"})
        capture = None
        if spec.get("source_capture"):
            capture = importlib.import_module(package_name + ".source_capture")
            capture_path = directory / spec["source_capture"]["manifest"]
            capture.verify_capture_files(capture_path, snapshot_path=directory / spec["snapshot"])
        bpy.ops.wm.open_mainfile(filepath=str(directory / spec["snapshot"]), load_ui=False, use_scripts=False)
        if capture is not None:
            capture.verify_snapshot(capture_path, snapshot_path=directory / spec["snapshot"])
        engine = importlib.import_module(package_name + ".engine")
        for key, value in spec["engine_settings"].items(): setattr(engine, key, value)
        targets = _sources(bpy, engine, spec["objects"])
        before = {obj.as_pointer() for obj in bpy.data.objects}
        control.check_cancel()

        resources = importlib.import_module(package_name + ".resources")
        preflight = importlib.import_module(package_name + ".preflight")
        explanation = preflight.analyze(targets)
        jobs.atomic_json(directory / "preflight.json", explanation)
        stats = explanation["source_stats"]
        decision = _resource_decision(engine, resources, spec["resource_policy"], stats)
        decision = jobs._json_copy(decision)
        if type(decision) is not dict or type(decision.get("allowed")) is not bool:
            raise RuntimeError("Resource policy returned no explicit admission decision")
        jobs.atomic_json(directory / "admission.json", decision)
        jobs.atomic_json(directory / "resource.json", decision)
        if not decision["allowed"]:
            terminal = {"state": "REFUSED", "reason": str(decision.get("reason", "Resource admission refused")), "admission": decision}
            exit_code = 2
            return exit_code
        control.emit({"kind": "stage", "stage": "texture_precheck"})
        options = dict(spec["precheck_settings"])
        if options.pop("enabled"):
            precheck = importlib.import_module(package_name + ".precheck")
            checked = precheck.run(objects=targets, marker_dir=str(directory / "precheck"),
                                   repair_unused=False, raise_on_failure=False, **options)
            for name, original in checked.get("repointed_originals", []) or []:
                engine.note_repointed_filepath(name, original)
            if not checked.get("ok"):
                terminal = {"state": "REFUSED", "reason": "Texture precheck blocked", "precheck_report": checked.get("report_path", "")}
                exit_code = 2
                return exit_code
        control.check_cancel()
        # Include resolver-repointed file images and linked libraries observed after precheck.
        # These remain external files, so verify them again before a successful result is published.
        runtime_inputs = {}
        for image in bpy.data.images:
            if image.source == "FILE" and not image.packed_file and image.filepath:
                path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
                if path.is_file(): runtime_inputs[str(path)] = jobs.sha256_file(path)
        for library in bpy.data.libraries:
            path = Path(bpy.path.abspath(library.filepath)).resolve()
            if path.is_file(): runtime_inputs[str(path)] = jobs.sha256_file(path)
        runtime_dependencies = {"dependencies": [{"path": path, "sha256": digest}
                                                   for path, digest in sorted(runtime_inputs.items())]}
        jobs.atomic_json(directory / "runtime_dependencies.json", runtime_dependencies)
        control.emit({"kind": "stage", "stage": "engine"})
        args = dict(spec["engine_args"])
        # This private argument is made only after sealed snapshot, capture and
        # external dependency verification. Resume copies the snapshot unchanged.
        capture_identity = None
        if spec.get("source_capture"):
            captured = jobs._read_capture_manifest(capture_path)
            capture_identity = {name: captured.get(name) for name in
                                ("capture_id", "snapshot_sha256", "images", "environment", "dependencies")}
        args["_checkpoint_source"] = {
            "snapshot_sha256": spec["snapshot_sha256"], "source_capture": capture_identity,
            "objects": spec["objects"], "dependencies": spec["dependencies"],
            "runtime_dependencies": runtime_dependencies["dependencies"]}
        census = engine.main(args_override=args, run_control=control)
        if not isinstance(census, dict): raise RuntimeError("Engine returned no failure census")
        if census.get("cancelled") or census.get("exit_code") == 130:
            terminal = {"state": "CANCELLED", "reason": "Cooperative cancellation; completed checkpoints retained", "census": census}
            exit_code = 130
            return exit_code
        if type(census.get("exit_code")) is not int or census["exit_code"] != 0:
            # Successful native output has a separately saved library even when
            # the Roblox target fails. Seal it only after rechecking inputs.
            native = census.get("target_results", {}).get("BLENDER_NATIVE", {})
            library = native.get("blend_library") or {}
            if native.get("exit_code") == 0 and library.get("objects") and library.get("path"):
                jobs.verify_dependencies(spec)
                jobs.verify_dependencies(runtime_dependencies)
                if capture is not None:
                    capture.verify_capture_files(capture_path, snapshot_path=directory / spec["snapshot"])
                path = Path(library["path"]).resolve()
                if not path.is_relative_to(Path(engine.OUTPUT_ROOT).resolve()):
                    raise RuntimeError("Partial native library is outside the output root")
                if jobs.sha256_file(path) != library["sha256"]:
                    raise RuntimeError("Partial native library changed before sealing")
                outputs = [bpy.data.objects[name] for name in library["objects"]]
                partial = {"schema": jobs.SCHEMA, "job_id": spec["job_id"], "state": "PARTIAL",
                           "target": "BLENDER_NATIVE", "result_path": str(path),
                           "result_sha256": library["sha256"], "output_objects": library["objects"],
                           "snapshot_sha256": spec["snapshot_sha256"], "engine_sha256": spec["engine_sha256"],
                           "manifest": census["manifest"], "manifest_sha256": jobs.sha256_file(census["manifest"]),
                           "output_dependencies": _output_dependencies(bpy, outputs, jobs)}
                jobs.atomic_json(directory / "partial_result.json", partial)
            terminal = {"state": "FAILED", "reason": "Engine census reports incomplete or failed outputs", "census": census}
            exit_code = 1
            return exit_code
        control.check_cancel()
        outputs = [obj for obj in bpy.data.objects if obj.as_pointer() not in before
                   and obj.type == "MESH" and engine.is_previous_output(obj)]
        output_objects = [obj.name for obj in outputs]
        if not outputs: raise RuntimeError("Clean census produced no new output meshes to load")
        output_dependencies = _output_dependencies(bpy, outputs, jobs)
        control.emit({"kind": "stage", "stage": "saving_result"})
        result_path = directory / "result.blend"
        # Save the private worker result only. Main-session source scene remains untouched.
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(result_path), copy=True, relative_remap=True)
        control.check_cancel()
        jobs.verify_dependencies(spec)
        jobs.verify_dependencies(runtime_dependencies)
        if capture is not None:
            capture.verify_capture_files(capture_path, snapshot_path=directory / spec["snapshot"])
        result = {"schema": jobs.SCHEMA, "job_id": spec["job_id"], "state": "SUCCEEDED", "census": census,
                  "manifest": census.get("manifest", ""), "result_blend": result_path.name,
                  "result_sha256": jobs.sha256_file(result_path), "output_objects": sorted(output_objects),
                  "snapshot_sha256": spec["snapshot_sha256"], "engine_sha256": spec["engine_sha256"],
                  "output_dependencies": output_dependencies}
        if result["manifest"]:
            result["manifest_sha256"] = jobs.sha256_file(result["manifest"])
        jobs.atomic_json(directory / "result.json", result)
        terminal = {"state": "SUCCEEDED", "reason": "Clean census and separate result blend saved",
                    "manifest": result["manifest"], "census": census}
        exit_code = 0
        return exit_code
    except BaseException as exc:
        is_cancel = control is not None and isinstance(exc, control.cancelled_type)
        exit_code = 130 if is_cancel else 1
        terminal = {"state": "CANCELLED" if is_cancel else "FAILED",
                    "reason": str(exc)[:2000] or exc.__class__.__name__}
        if census is not None: terminal["census"] = census
        if not is_cancel: traceback.print_exc()
        return exit_code
    finally:
        if control is not None: control.close()
        terminal["exit_code"] = exit_code
        state = terminal.pop("state")
        if trusted: jobs.write_status(directory, state, **terminal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    tail = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    options = parser.parse_args(tail)
    code = run(options.job)
    # SystemExit produces a nonzero process status under --python-exit-code;
    # semantic cancellation is preserved in status.json rather than inferred from the OS code.
    raise SystemExit(code)


if __name__ == "__main__": main()
