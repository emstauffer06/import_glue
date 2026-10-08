"""Explicit Blender-native and Roblox deliveries; legacy engine remains available."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import shutil
import traceback
import uuid

import bpy

from . import engine, background_jobs
from .run_control import RunCancelled
from . import target_checkpoints

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
    details = {"objects", "object_manifests", "visual_reports", "visual_galleries", "device_selections"}
    summary = {key: value for key, value in result.items() if key not in details}
    if result.get("visual_galleries"):
        summary["visual_galleries_preview"] = result["visual_galleries"][:5]
        summary["visual_gallery_count"] = len(result["visual_galleries"])
    if "objects" in result:
        summary["object_details"] = result.get("manifest", "")
        summary["failed_objects"] = [{"source": item.get("source"), "status": item.get("status"),
                                      "error": str(item.get("error", ""))[:500]}
                                     for item in result["objects"]
                                     if item.get("status") not in {"BAKED", "RETAINED_ONLY"}][:20]
    return summary


def _direct_source_signature(obj):
    """Add evaluated-only materials and Generated-coordinate provenance.

    The legacy digest hashes evaluated topology but not material datablocks
    introduced by Geometry Nodes, nor mesh texture-space settings. Those can
    change shading while all source material slots and vertex positions stay put.
    """
    from . import shader_graph
    usage = shader_graph.material_usage(obj)
    if usage["state"] in {"ERROR", "NOT_EVALUATED"}:
        raise ValueError("Evaluated material provenance unavailable: " + usage["detail"])
    hasher = hashlib.sha256()
    for material in shader_graph.usage_materials(obj, usage):
        material = getattr(material, "original", material)
        engine._digest_token(hasher, material.name,
                             bpy.path.abspath(material.library.filepath) if material.library else "")
        for name in ("use_nodes", "diffuse_color", "metallic", "roughness", "surface_render_method",
                     "blend_method", "alpha_threshold", "pass_index", "use_backface_culling",
                     "displacement_method"):
            engine._digest_token(hasher, name, engine._simple_rna_value(getattr(material, name, None)))
        engine._hash_node_tree(hasher, material.node_tree, set())
        pending, visited = [material.node_tree], set()
        while pending:
            tree = pending.pop()
            if tree is None or tree.as_pointer() in visited:
                continue
            visited.add(tree.as_pointer())
            for node in tree.nodes:
                for name in ("script", "ies"):
                    text = getattr(node, name, None)
                    if isinstance(text, bpy.types.Text):
                        engine._digest_token(hasher, "embedded-shader-text", name, text.as_string())
                if getattr(node, "node_tree", None) is not None:
                    pending.append(node.node_tree)
    for mesh in (obj.data, obj.data.texco_mesh):
        if mesh is None:
            continue
        engine._digest_token(hasher, "texture-space", mesh.name, mesh.use_auto_texspace,
                             list(mesh.texspace_location), list(mesh.texspace_size))
        if mesh != obj.data:
            engine._hash_mesh_geometry(hasher, mesh, "texture-coordinate-mesh")
        if mesh.shape_keys and mesh.shape_keys.reference_key:
            points = mesh.shape_keys.reference_key.data
            values = engine.np.empty(len(points) * 3, dtype=engine.np.float32)
            points.foreach_get("co", values)
            hasher.update(memoryview(values).cast("B"))
    return {"name": obj.name, "source": engine.source_fingerprint(obj, {}),
            "shading_provenance": hasher.hexdigest()}


def _checkpoint_cache(base, objects, args):
    if not engine.DONE_LIST or not engine.DURABLE_CHECKPOINTS:
        return None, "Durable checkpoints disabled by settings"
    try:
        inputs = args.get("_checkpoint_source")
        # A supervised immutable snapshot also seals otherwise difficult external
        # scene context. Direct callers still require a content fingerprint.
        if inputs is None:
            from . import source_capture, shader_resources
            # A long-lived add-on must not carry decoded/file digest memoization
            # across separate runs, including edits that preserve file mtimes.
            engine._SOURCE_FILE_DIGEST_CACHE.clear()
            engine._LIVE_IMAGE_DIGEST_CACHE.clear()
            source_capture._geometry_guard(objects)
            dependencies, shader_nodes = {}, []
            environment, register = source_capture._environment(source_capture._Budget(None), dependencies)
            _images, _nodes, diagnostics = source_capture._dependencies(objects, shader_nodes)
            if diagnostics:
                raise ValueError("; ".join(diagnostics))
            for node in shader_nodes:
                for resource in shader_resources.prepare_node(node)["dependencies"]:
                    register(resource["path"], "SHADER_RESOURCE")
            inputs = {"objects": [_direct_source_signature(obj) for obj in objects],
                      "environment": environment, "dependencies": sorted(dependencies.values(), key=lambda row: row["path"]),
                      "frame": [bpy.context.scene.frame_current, bpy.context.scene.frame_subframe]}
        identity = {"source": inputs, "engine_config": engine.config_fingerprint(),
                    "package": background_jobs.package_fingerprint(),
                    "blender": list(bpy.app.version), "build": bpy.app.build_hash.decode("ascii", errors="replace"),
                    "device": engine.DEVICE, "gpu": engine.GPU_DEVICE_ID,
                    "approximation": engine.ALLOW_APPROXIMATION,
                    "native_quality": getattr(engine, "NATIVE_QUALITY", None),
                    "visual": [engine.VISUAL_VALIDATION, engine.VISUAL_VALIDATION_GATE,
                               engine.VISUAL_VALIDATION_SETTINGS]}
        return target_checkpoints.Cache(base, identity,
                                       read=not args.get("no-resume") and not args.get("rerun")), ""
    except Exception as exc:
        return None, "Checkpoint input could not be proven; baking from source: " + str(exc)


def _restore(result):
    library = result.get("blend_library") or {}
    names = list(library.get("objects") or [])
    if not names or any(name in bpy.data.objects for name in names):
        return None
    path = Path(library["path"])
    if background_jobs.sha256_file(path) != library.get("sha256"):
        return None
    with bpy.data.libraries.load(str(path), link=False) as (available, wanted):
        if set(names) - set(available.objects):
            return None
        wanted.objects = list(names)
    if any(obj is None for obj in wanted.objects):
        raise RuntimeError("Verified checkpoint library did not restore all objects")
    collection = bpy.data.collections.new("Import Glue Resumed Results")
    bpy.context.scene.collection.children.link(collection)
    for obj in wanted.objects:
        collection.objects.link(obj)
    return {**result, "checkpoint_reused": True, "output_objects": [obj.name for obj in wanted.objects]}


def _native_object(obj, folder, cache, run_control, image_capture_cache=None):
    from . import native_baker
    result = native_baker.run([obj], str(folder), resolution=engine.RES, device=engine.DEVICE,
                              gpu_device_id=engine.GPU_DEVICE_ID, run_control=run_control,
                              checkpoint_context=cache,
                              quality_settings=getattr(engine, "NATIVE_QUALITY", None),
                              image_capture_cache=image_capture_cache)
    if result.get("cancelled") or result.get("exit_code") == 130:
        raise RunCancelled("Native target cancelled")
    if engine.VISUAL_VALIDATION and result.get("output_objects"):
        from . import target_visual
        target_visual.compare_native([obj], result, folder, run_control=run_control)
    return result


def _roblox_bake_object(obj, folder, args, run_control):
    from . import roblox_output
    limit = int(engine.ROBLOX_TEXTURE_LIMIT)
    if not 4 <= limit <= 4096:
        raise ValueError("Roblox delivery preset must be between 4 and 4096 pixels")
    _select([obj])
    before = {item.as_pointer() for item in bpy.data.objects}
    options = {**args, "no-resume": True, "no-finalize": True}
    # This function already has exactly one selected source, which may be a
    # private geometry capture with a different name from the original scope.
    options.pop("only", None)
    with _settings(TARGET_PROFILE="ROBLOX", CAPABILITY_PROFILE="ROBLOX", OUTPUT_ROOT=str(folder),
                   ROUTE_MODE="GRAPH_ONLY", OUTPUT_PROFILE="PBR_BASE", CROP_SPLIT_PREPASS=False,
                   DONE_LIST=False, DURABLE_CHECKPOINTS=False, FINALIZE_IN_SESSION=False,
                   EXPORT_FBX=True, EXPORT_GLTF=False, MIN_RES=min(engine.MIN_RES, limit),
                   RES=min(engine.RES, limit), MAX_RES=min(engine.MAX_RES, limit)):
        result = engine.main(args_override=options, run_control=run_control)
    if result.get("cancelled") or result.get("exit_code") == 130:
        raise RunCancelled("Roblox target cancelled")
    if result.get("exit_code") != 0:
        return {**result, "status": "FAILED"}
    report_path = Path(result["manifest"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    bindings = [item["roblox_binding"] for item in report["objects"]
                if item.get("status") == "OK" and item.get("roblox_binding")]
    if len(bindings) != 1:
        raise RuntimeError("Roblox object does not have one verified binding")
    mesh_path = Path(report["exports"]["fbx"])
    for binding in bindings:
        binding["mesh_path"] = mesh_path.relative_to(report_path.parent).as_posix()
    if run_control is not None:
        run_control.check_cancel()
    target_checkpoints.atomic_json(report_path, report)
    outputs = [item.name for item in bpy.data.objects if item.as_pointer() not in before
               and item.type == "MESH" and engine.is_previous_output(item)]
    result.update(status="COMPLETE", surface_bundle=str(report_path.parent / "roblox_surface_bundle.json"),
                  binding_count=len(bindings), studio_verified=False, output_objects=outputs)
    if report.get("device_selection"):
        result["device_selection"] = report["device_selection"]
    return result


def _roblox_object(obj, folder, args, run_control):
    """Publish only after geometry capture, scalar fitting and the final gate."""
    from . import roblox_output, geometry_delivery, material_fit
    fit_enabled = bool(engine.ROBLOX_MATERIAL_FIT)
    geometry_enabled = bool(engine.ROBLOX_GEOMETRY)
    if (fit_enabled or geometry_enabled) and not engine.ALLOW_APPROXIMATION:
        raise ValueError("Roblox fitting/geometry conversion requires Allow Approximation")
    fit_config = material_fit.settings(engine.ROBLOX_FIT_SETTINGS) if fit_enabled else None
    geometry_config = geometry_delivery.settings({**engine.ROBLOX_GEOMETRY_SETTINGS, "enabled": True}) if geometry_enabled else None
    if geometry_config is not None:
        # Never erase captured detail with the legacy automatic decimator.
        geometry_config["max_triangles"] = min(geometry_config["max_triangles"], engine.TRI_BUDGET)
    changed_delivery = fit_enabled or geometry_enabled
    original_signature = _direct_source_signature(obj) if changed_delivery else None
    prepared = None
    report_path = report = None
    try:
        source = obj
        if geometry_enabled:
            prepared = geometry_delivery.prepare(obj, settings=geometry_config, run_control=run_control)
            source = prepared.object
        overrides = {}
        if changed_delivery:
            # A baseline comparison would validate stale maps/geometry. Check
            # the delivered object against the original after the fit instead.
            overrides.update(VISUAL_VALIDATION=False, VISUAL_VALIDATION_GATE=False)
        if geometry_enabled:
            overrides.update(ENFORCE_TRI_BUDGET=False, DECIMATE_RATIO=1.0)
        with _settings(**overrides):
            result = _roblox_bake_object(source, folder, args, run_control)
        if result.get("exit_code") != 0:
            return result
        report_path = Path(result["manifest"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["baseline_census"] = report.get("census")
        report["delivery_status"] = "IN_PROGRESS"
        report["census"] = {"exit_code": 1, "status": "IN_PROGRESS", "manifest": str(report_path)}
        target_checkpoints.atomic_json(report_path, report)
        rows = [row for row in report["objects"] if row.get("status") == "OK" and row.get("roblox_binding")]
        if len(rows) != 1 or len(result["output_objects"]) != 1:
            raise RuntimeError("Expected one Roblox delivery object and binding")
        row = rows[0]
        output = bpy.data.objects[result["output_objects"][0]]
        row["source_object"] = obj.name
        row["object"] = obj.name
        if prepared is not None:
            row["geometry_delivery"] = prepared.metadata
            result["geometry_delivery"] = prepared.metadata
        if fit_enabled:
            fitted = material_fit.fit_roblox_object(obj, output, report_path.parent, row["roblox_binding"],
                                                    fit_settings=fit_config, run_control=run_control)
            row["roblox_binding"] = fitted.pop("binding")
            row["material_fit"] = fitted
            result["material_fit"] = fitted
        if changed_delivery:
            row["delivery_fidelity"] = "APPROXIMATION"
            result["approximated"] = max(1, result.get("approximated", 0))
            report["config"].update(visual_validation=bool(engine.VISUAL_VALIDATION),
                                   visual_validation_gate=bool(engine.VISUAL_VALIDATION_GATE))
            if engine.VISUAL_VALIDATION:
                if run_control is not None:
                    run_control.check_cancel()
                row["visual_validation"] = engine.run_visual_validation(obj, output, str(report_path.parent),
                                                                        "final_delivery")
            if original_signature != _direct_source_signature(obj):
                raise RuntimeError("Source content changed during Roblox delivery")
            report["source_unchanged"] = True
        gate_failed = bool(engine.VISUAL_VALIDATION and engine.VISUAL_VALIDATION_GATE and
                           row.get("visual_validation", {}).get("status") != "PASS")
        if gate_failed:
            result.update(exit_code=1, status="FAILED", reason="Final delivered material failed visual gate")
            report["delivery_status"] = "FAILED_VISUAL_GATE"
        else:
            if run_control is not None:
                run_control.check_cancel()
            roblox_output.build_surface_bundle(report_path.parent, [row["roblox_binding"]],
                                               texture_limit=int(engine.ROBLOX_TEXTURE_LIMIT))
            report["delivery_status"] = "COMPLETE_WITH_APPROXIMATION" if result.get("approximated") else "COMPLETE"
        # The baseline census predates fitted maps; preserve it explicitly and
        # publish a fresh terminal census for this final delivery.
        report["census"] = {key: value for key, value in result.items() if key != "target_results"}
        target_checkpoints.atomic_json(report_path, report)
        return result
    except BaseException as exc:
        if report is not None:
            cancelled = isinstance(exc, (RunCancelled, KeyboardInterrupt))
            report["delivery_status"] = "CANCELLED" if cancelled else "FAILED_DELIVERY"
            report["delivery_error"] = str(exc)
            report["census"] = {"exit_code": 130 if cancelled else 1,
                                "status": report["delivery_status"], "cancelled": cancelled}
            # These are generated files in this run's unique object folder.
            # A failed final write must not leave a usable-looking importer.
            for filename in ("roblox_surface_bundle.json", "roblox_surface_importer.luau"):
                (report_path.parent / filename).unlink(missing_ok=True)
            target_checkpoints.atomic_json(report_path, report)
        raise
    finally:
        if prepared is not None:
            prepared.close()


def _merge_native(results, folder):
    merged = {"schema": 2, "target_profile": "BLENDER_NATIVE", "objects": [], "output_objects": [],
              "total": len(results), "ok": 0, "failed": 0, "skipped": 0,
              "converted_fields": 0, "retained_fields": 0, "object_manifests": []}
    for result in results:
        merged["objects"].extend(result.get("objects", []))
        merged["output_objects"].extend(result.get("output_objects", []))
        for field in ("ok", "failed", "skipped", "converted_fields", "retained_fields"):
            merged[field] += result.get(field, 0)
        merged["object_manifests"].append(result.get("manifest", ""))
    for key in ("device_selection", "sampling", "quality_settings", "geometry_policy", "fidelity",
                "blender_version", "frame", "subframe", "source_scene", "source_render_engine",
                "field_evaluation_engine", "resolution"):
        present = [item[key] for item in results if key in item]
        if present and all(value == present[0] for value in present):
            merged[key] = present[0]
    merged["device_selections"] = [item["device_selection"] for item in results if item.get("device_selection")]
    merged["visual_reports"] = [value for item in results for value in item.get("visual_reports", [])]
    merged["visual_galleries"] = [item["visual_gallery"] for item in results if item.get("visual_gallery")]
    merged["visual"] = {}
    for item in results:
        for status, count in item.get("visual", {}).items():
            merged["visual"][status] = merged["visual"].get(status, 0) + count
    merged["elapsed_seconds"] = sum(item.get("elapsed_seconds", 0) for item in results)
    merged["warnings"] = sorted({warning for item in results for warning in item.get("warnings", [])})
    merged.update(exit_code=0 if all(item.get("exit_code") == 0 for item in results) else 1,
                  status="COMPLETE" if all(item.get("exit_code") == 0 for item in results) else "PARTIAL",
                  baked=merged["converted_fields"] > 0, manifest=str(folder / "native_manifest.json"))
    target_checkpoints.atomic_json(merged["manifest"], merged)
    return merged


def _merge_roblox(results, folder, run_control):
    if len(results) == 1:
        return results[0]
    if any(item.get("exit_code") != 0 for item in results):
        result = {"exit_code": 1, "status": "PARTIAL", "object_results": [_summary(item) for item in results],
                "output_objects": [name for item in results for name in item.get("output_objects", [])],
                "manifest": str(folder / "roblox_objects.json")}
        target_checkpoints.atomic_json(result["manifest"], result)
        return result
    from . import roblox_output
    bindings, rows, output_names = [], [], []
    for index, result in enumerate(results):
        report_path = Path(result["manifest"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        output_names.extend(result["output_objects"])
        for row in report["objects"]:
            if row.get("status") != "OK" or not row.get("roblox_binding"):
                continue
            binding = json.loads(json.dumps(row["roblox_binding"]))
            binding["id"] = "%04d_%s" % (index, binding["id"])
            for semantic, filename in binding["maps"].items():
                source = report_path.parent / filename
                destination = folder / ("%04d_%s" % (index, Path(filename).name))
                # Source names can sanitize to the same string. A stable object
                # index keeps every map distinct without renaming source IDs.
                with source.open("rb") as source_file, destination.open("xb") as output_file:
                    shutil.copyfileobj(source_file, output_file)
                binding["maps"][semantic] = destination.name
            binding["mesh_path"] = "rbx_pbr_smart.fbx"
            bindings.append(binding)
            rows.append({**row, "roblox_binding": binding, "source_object_manifest": str(report_path)})
    report = {"schema": 1, "objects": rows, "exports": {},
              "device_selections": [item["device_selection"] for item in results if item.get("device_selection")]}
    if report["device_selections"] and all(item == report["device_selections"][0] for item in report["device_selections"]):
        report["device_selection"] = report["device_selections"][0]
    with _settings(TARGET_PROFILE="ROBLOX", EXPORT_FBX=True, EXPORT_GLTF=False):
        engine.export_outputs([bpy.data.objects[name] for name in output_names], str(folder), report)
    if not report["exports"].get("fbx") or report["exports"].get("state_error"):
        raise RuntimeError("Combined Roblox mesh export failed: " + str(report["exports"]))
    if run_control is not None:
        run_control.check_cancel()
    bundle = roblox_output.build_surface_bundle(folder, bindings, texture_limit=int(engine.ROBLOX_TEXTURE_LIMIT))
    report_path = folder / "roblox_manifest.json"
    target_checkpoints.atomic_json(report_path, report)
    return {"exit_code": 0, "status": "COMPLETE", "manifest": str(report_path),
            "output_objects": output_names, "surface_bundle": str(folder / "roblox_surface_bundle.json"),
            "binding_count": len(bundle["bindings"]), "studio_verified": False,
            "approximated": sum(item.get("approximated", 0) for item in results)}


def _run_target(kind, objects, folder, args, cache, run_control):
    results = []
    image_capture_cache = {}
    for index, obj in enumerate(objects):
        if run_control is not None:
            run_control.check_cancel()
        key = [kind, obj.name, bpy.path.abspath(obj.library.filepath) if obj.library else ""]
        saved = cache.load("object", key) if cache is not None else None
        result = _restore(saved) if saved else None
        if result is None:
            object_folder = folder / ("object_%04d" % index)
            object_folder.mkdir()
            _select([obj])
            result = (_native_object(obj, object_folder, cache, run_control, image_capture_cache) if kind == "BLENDER_NATIVE" else
                      _roblox_object(obj, object_folder, args, run_control))
            output_objects = [bpy.data.objects[name] for name in result.get("output_objects", [])]
            if output_objects:
                result["blend_library"] = _library(output_objects, object_folder / "object_outputs.blend")
            if cache is not None and result.get("exit_code") == 0 and output_objects:
                cache.save("object", key, result, target_checkpoints.inventory(object_folder))
        elif engine.VISUAL_VALIDATION and kind == "BLENDER_NATIVE":
            from . import target_visual
            target_visual.compare_native([obj], result, folder, run_control=run_control)
        results.append(result)
    result = _merge_native(results, folder) if kind == "BLENDER_NATIVE" else _merge_roblox(results, folder, run_control)
    outputs = [bpy.data.objects[name] for name in result.get("output_objects", [])]
    if outputs:
        result["blend_library"] = _library(outputs, folder / ("native_outputs.blend" if kind == "BLENDER_NATIVE" else "target_outputs.blend"))
    if kind == "BLENDER_NATIVE":
        target_checkpoints.atomic_json(result["manifest"], result)
    if cache is not None and result.get("exit_code") == 0 and outputs:
        # Include files referenced by reused objects as well as this new target's files.
        paths = target_checkpoints.inventory(folder)
        for item in results:
            path = item.get("manifest")
            if path:
                paths.extend(target_checkpoints.inventory(Path(path).parent))
        cache.save("target", kind, result, paths)
    return result


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
    cache, cache_warning = _checkpoint_cache(base, objects, args)
    manifest = {"schema": 2, "target": target, "source_blend": bpy.data.filepath,
                "source_objects": [o.name for o in objects], "targets": {},
                "notes": ["Native fields are sampled on the preserved UV layout; closure structure is retained.",
                          "Completed native fields, objects and targets require content-verified target checkpoints.",
                          "Roblox renderer parity requires verification in Studio."]}
    manifest["checkpoint_warning"] = cache_warning
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    visibility = [(obj, obj.hide_get()) for obj in objects]
    cancelled = False
    restore_errors = []
    from . import ocio_capture
    color_contract = ocio_capture.target_contract()
    manifest["color_management"] = color_contract
    try:
        for kind in (("BLENDER_NATIVE", "ROBLOX") if target == "BOTH" else (target,)):
            if run_control is not None:
                run_control.check_cancel()
                run_control.emit({"kind": "stage", "stage": "target", "target": kind})
            _select(objects)
            target_root = root / kind.lower()
            target_root.mkdir()
            try:
                prefix = "native" if kind == "BLENDER_NATIVE" else "roblox"
                if not color_contract[prefix + "_supported"]:
                    raise ValueError("Target color contract refused: " + "; ".join(color_contract[prefix + "_reasons"]))
                saved = cache.load("target", kind) if cache is not None else None
                # Native visual validation runs again when requested; object caches
                # preserve completed bake work while fresh comparisons verify it.
                if kind == "BLENDER_NATIVE" and engine.VISUAL_VALIDATION:
                    saved = None
                result = _restore(saved) if saved else None
                if result is None:
                    result = _run_target(kind, objects, target_root, args, cache, run_control)
                manifest["targets"][kind] = _summary(result)
            except (RunCancelled, KeyboardInterrupt):
                raise
            except Exception as exc:
                traceback.print_exc()
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
    manifest["checkpoint_events"] = cache.events if cache is not None else []
    manifest.update(status=status, census={key: value for key, value in census.items() if key != "target_results"})
    background_jobs.atomic_json(manifest_path, manifest)
    return census
