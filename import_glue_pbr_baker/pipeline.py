"""Explicit Blender-native and Roblox deliveries; legacy engine remains available."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import uuid

import bpy

from . import engine, background_jobs
from .run_control import RunCancelled

TARGETS = ("LEGACY", "BLENDER_NATIVE", "ROBLOX", "BOTH")


@contextmanager
def _settings(**values):
    original = {name: getattr(engine, name) for name in values}
    try:
        for name, value in values.items():
            setattr(engine, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(engine, name, value)


def _select(objects):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def _library(objects, path):
    """Save an appendable output library without switching/saving the source file."""
    if not objects:
        raise RuntimeError("Target produced no output objects")
    bpy.data.libraries.write(str(path), set(objects), path_remap="ABSOLUTE",
                             fake_user=True, compress=True)
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError("Output .blend library was not saved")
    return {"path": str(path), "sha256": background_jobs.sha256_file(path),
            "kind": "APPENDABLE_OBJECT_LIBRARY", "objects": [o.name for o in objects]}


def _summary(result):
    """Detailed field records live in their target manifest, not worker status.

    Repeating thousands of native input records in every status/result envelope
    would exceed the supervisor's bounded JSON protocol on ordinary batches.
    """
    summary = {key: value for key, value in result.items() if key != "objects"}
    if "objects" in result:
        summary["object_details"] = result.get("manifest", "")
        summary["failed_objects"] = [{"source": item.get("source"), "status": item.get("status"),
                                      "error": str(item.get("error", ""))[:500]}
                                     for item in result["objects"]
                                     if item.get("status") not in {"BAKED", "RETAINED_ONLY"}]
    return summary


def run(args=None, *, run_control=None, before_finalize=None):
    args = dict(engine.cli_args() if args is None else args)
    target = engine.TARGET_PROFILE
    if target not in TARGETS or target == "LEGACY":
        raise ValueError("Invalid target pipeline: " + str(target))
    scope = engine.only_names(args.get("only"))
    objects = [obj for obj in bpy.context.selected_objects
               if obj.type == "MESH" and not engine.is_previous_output(obj)
               and (scope is None or obj.name in scope)]
    if not objects:
        return {"exit_code": 3, "status": "EMPTY", "total": 0, "ok": 0,
                "failed": 0, "skipped": 0, "manifest": ""}
    base = Path(engine.OUTPUT_ROOT or Path(bpy.data.filepath).parent or bpy.app.tempdir)
    root = base / ("import_glue_" + target.lower() + "_" + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    manifest_path = root / "delivery.json"
    manifest = {"schema": 1, "target": target, "source_blend": bpy.data.filepath,
                "source_objects": [o.name for o in objects], "targets": {},
                "notes": ["Native fields are sampled on the preserved UV layout; closure structure is retained.",
                          "New target deliveries rerun from source; legacy four-map checkpoints are not reused.",
                          "Roblox renderer parity requires verification in Studio."]}
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    visibility = [(obj, obj.hide_get()) for obj in objects]
    cancelled = False
    restore_errors = []
    try:
        for kind in (("BLENDER_NATIVE", "ROBLOX") if target == "BOTH" else (target,)):
            if run_control is not None:
                run_control.check_cancel()
                run_control.emit({"kind": "stage", "stage": "target", "target": kind})
            _select(objects)
            target_root = root / kind.lower()
            target_root.mkdir()
            try:
                if kind == "BLENDER_NATIVE":
                    from . import native_baker
                    result = native_baker.run(objects, str(target_root), resolution=engine.RES,
                                              device=engine.DEVICE, gpu_device_id=engine.GPU_DEVICE_ID,
                                              run_control=run_control)
                    if result.get("cancelled") or result.get("exit_code") == 130:
                        raise RunCancelled("Native target cancelled")
                    output_objects = [bpy.data.objects[name] for name in result.get("output_objects", [])]
                    if run_control is not None:
                        run_control.check_cancel()
                    if output_objects:
                        result["blend_library"] = _library(output_objects, target_root / "native_outputs.blend")
                    if result.get("exit_code") != 0:
                        manifest["targets"][kind] = _summary(result)
                        continue
                    if not output_objects:
                        raise RuntimeError("Native target reported success with no output objects")
                else:
                    from . import roblox_output
                    limit = int(engine.ROBLOX_TEXTURE_LIMIT)
                    if not 4 <= limit <= 4096:
                        raise ValueError("Roblox delivery preset must be between 4 and 4096 pixels")
                    # The authored graph drives all five passes. Channel tracing,
                    # high-precision portable maps and old checkpoints cannot
                    # silently replace this target's explicit contract.
                    options = {**args, "no-resume": True, "no-finalize": True}
                    with _settings(TARGET_PROFILE="ROBLOX", CAPABILITY_PROFILE="ROBLOX",
                                   OUTPUT_ROOT=str(target_root), ROUTE_MODE="GRAPH_ONLY",
                                   OUTPUT_PROFILE="PBR_BASE", CROP_SPLIT_PREPASS=False,
                                   DONE_LIST=False, DURABLE_CHECKPOINTS=False,
                                   FINALIZE_IN_SESSION=False, EXPORT_FBX=True, EXPORT_GLTF=False,
                                   MIN_RES=min(engine.MIN_RES, limit),
                                   RES=min(engine.RES, limit), MAX_RES=min(engine.MAX_RES, limit)):
                        result = engine.main(args_override=options, run_control=run_control)
                    if result.get("cancelled") or result.get("exit_code") == 130:
                        raise RunCancelled("Roblox target cancelled")
                    if result.get("exit_code") != 0:
                        manifest["targets"][kind] = {**result, "status": "FAILED"}
                        continue
                    report_path = Path(result["manifest"])
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    bindings = [item["roblox_binding"] for item in report["objects"]
                                if item.get("status") == "OK" and item.get("roblox_binding")]
                    if len(bindings) != len(objects):
                        raise RuntimeError("Roblox output does not have one verified binding per source")
                    mesh_path = Path(report["exports"]["fbx"])
                    for binding in bindings:
                        binding["mesh_path"] = mesh_path.relative_to(report_path.parent).as_posix()
                    if run_control is not None:
                        run_control.check_cancel()
                    bundle = roblox_output.build_surface_bundle(report_path.parent, bindings,
                                                                texture_limit=limit)
                    result.update(status="COMPLETE", surface_bundle=str(report_path.parent / "roblox_surface_bundle.json"),
                                  binding_count=len(bundle["bindings"]), studio_verified=False)
                manifest["targets"][kind] = _summary(result)
            except (RunCancelled, KeyboardInterrupt):
                raise
            except Exception as exc:
                manifest["targets"][kind] = {"exit_code": 1, "status": "FAILED",
                                             "reason": str(exc), "error_type": type(exc).__name__}
            finally:
                background_jobs.atomic_json(manifest_path, manifest)
        if run_control is not None:
            run_control.check_cancel()
    except (RunCancelled, KeyboardInterrupt):
        cancelled = True
    finally:
        try:
            _select([obj for obj in selected if obj.name in bpy.context.view_layer.objects])
            bpy.context.view_layer.objects.active = active
        except Exception as exc:
            restore_errors.append("selection: " + str(exc))
        for obj, hidden in visibility:
            try:
                obj.hide_set(hidden)
            except Exception as exc:
                restore_errors.append("visibility: " + str(exc))
        if callable(before_finalize):
            try:
                before_finalize()
            except Exception as exc:
                restore_errors.append("callback: " + str(exc))
    expected = 2 if target == "BOTH" else 1
    successful = sum(item.get("exit_code") == 0 for item in manifest["targets"].values())
    approximated = sum(int(item.get("approximated", 0)) for item in manifest["targets"].values())
    status = "CANCELLED" if cancelled else "COMPLETE" if successful == expected else "PARTIAL" if successful else "FAILED"
    if status == "COMPLETE" and approximated:
        status = "COMPLETE_WITH_APPROXIMATION"
    if restore_errors and not cancelled:
        status = "FAILED"
    census = {"exit_code": 130 if cancelled else 0 if successful == expected and not restore_errors else 1,
              "status": status, "cancelled": cancelled, "total": expected, "ok": successful,
              "failed": expected - successful, "skipped": 0, "manifest": str(manifest_path),
              "approximated": approximated,
              "target_results": manifest["targets"], "state_restore_error": restore_errors}
    manifest.update(status=status, census={key: value for key, value in census.items() if key != "target_results"})
    background_jobs.atomic_json(manifest_path, manifest)
    return census
