"""Preserve native closures and sample only proven static fields on the same mesh.

This is a finite-resolution, frame-specific optimization, not a universal shader
flattening or an exact procedural replacement. Signed fields are measured with
two nonnegative emission probes, then stored as lossless full-float EXR data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

import bpy
import numpy as np

from . import engine, native_graph, source_capture, uv_validation
from .run_control import RunCancelled


class NativeRefusal(RuntimeError):
    pass


def _boundary(control, kind, **fields):
    if control is not None:
        control.emit({"kind": kind, **fields})
        control.check_cancel()


def _owned():
    return {"objects": [], "meshes": [], "materials": [], "node_groups": [], "images": [], "texts": []}


def _remove(owned):
    for kind in ("objects", "materials", "node_groups", "images", "texts", "meshes"):
        store = getattr(bpy.data, kind)
        for item in reversed(owned[kind]):
            try:
                store.remove(item, do_unlink=True)
            except ReferenceError:
                pass
        owned[kind].clear()


def _trees(material):
    if not material.use_nodes or material.node_tree is None:
        return
    stack = [material.node_tree]
    while stack:
        tree = stack.pop()
        yield tree
        stack.extend(node.node_tree for node in tree.nodes if node.type == "GROUP" and node.node_tree)


def _capture_images(materials, plans, owned):
    """Capture required images once per object, never remap original users."""
    by_pointer, records, required_nodes = {}, [], []
    for material, plan in zip(materials, plans):
        if material is None:
            continue
        required = set(plan["dependency"]["images"])
        nodes = [node for tree in _trees(material) for node in tree.nodes
                 if getattr(node, "image", None) is not None]
        for node in nodes:
            original = node.image
            pointer = original.as_pointer()
            if pointer not in required:
                # Inactive references must not pull an unused movie or missing
                # external file into the appendable output library.
                node.image = None
                continue
            required_nodes.append(node)
    for node in required_nodes:
        original = node.image
        pointer = original.as_pointer()
        if pointer not in by_pointer:
            capture = source_capture.capture_image_copy(
                original, [candidate for candidate in required_nodes if candidate.image == original])
            if not capture["owned"]:
                raise NativeRefusal("Linked image capture is external, not self-contained: " + original.name)
            owned["images"].append(capture["image"])
            by_pointer[pointer] = capture["image"]
            records.append(capture["record"])
    for node in required_nodes:
        node.image = by_pointer[node.image.as_pointer()]
    return records


def _duplicate(source, owned, depsgraph):
    if source.type != "MESH" or source.mode != "OBJECT":
        raise NativeRefusal("Native baking requires a mesh in Object Mode")
    evaluated = source.evaluated_get(depsgraph)
    if source.instance_type != "NONE" or any(
            instance.is_instance and instance.parent is not None
            and instance.parent.original == source for instance in depsgraph.object_instances):
        raise NativeRefusal("Unrealized object/geometry instances need an explicit instance realization policy")
    if source.modifiers or source.data.shape_keys:
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    else:
        mesh = source.data.copy()
    owned["meshes"].append(mesh)
    if not mesh.polygons:
        raise NativeRefusal("Evaluated object has no mesh faces; unrealized instances are not supported")
    copy = source.copy()
    owned["objects"].append(copy)
    copy.data = mesh
    copy.name = "RBX_NATIVE_" + source.name
    copy.parent = None
    copy.matrix_world = evaluated.matrix_world.copy()
    copy.animation_data_clear()
    copy.color = evaluated.color
    copy.pass_index = evaluated.pass_index
    copy.modifiers.clear()
    copy.constraints.clear()
    copy.hide_render = False
    copy.hide_viewport = False
    # Object-linked overrides otherwise bypass the private material probe even
    # after Mesh.materials is reassigned. Flatten effective evaluated slots onto
    # this private mesh, then make every copied slot use that private DATA slot.
    effective = [slot.material for slot in evaluated.material_slots]
    for index, material in enumerate(effective):
        if index < len(mesh.materials):
            mesh.materials[index] = material
    for slot in copy.material_slots:
        slot.link = "DATA"
    copy["import_glue_native"] = True
    copy["import_glue_source"] = source.name
    return copy


def _assign_materials(obj, materials, indices):
    obj.data.materials.clear()
    for material in materials:
        obj.data.materials.append(material)
    for face, index in zip(obj.data.polygons, indices):
        face.material_index = index


def _target_image(name, resolution, owned):
    image = bpy.data.images.new(name, width=resolution, height=resolution, alpha=True, float_buffer=True)
    owned["images"].append(image)
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"
    return image


def _emission_material(material, image, source=None, negative=False):
    tree = material.node_tree
    # A probe is evaluated on the original evaluated mesh, not a shader-displaced
    # surface. All original closures remain intact in the separate output copy.
    for output in [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"]:
        for socket in output.inputs:
            for link in list(socket.links):
                tree.links.remove(link)
        output.is_active_output = False
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.is_active_output = True
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0, 0, 0, 1)
    emission.inputs["Strength"].default_value = 1
    if source is not None:
        if negative:
            negate = tree.nodes.new("ShaderNodeVectorMath")
            negate.operation = "MULTIPLY"
            negate.inputs[1].default_value = (-1, -1, -1)
            tree.links.new(source, negate.inputs[0])
            source = negate.outputs["Vector"]
        positive = tree.nodes.new("ShaderNodeVectorMath")
        positive.operation = "MAXIMUM"
        positive.inputs[1].default_value = (0, 0, 0)
        tree.links.new(source, positive.inputs[0])
        tree.links.new(positive.outputs["Vector"], emission.inputs["Color"])
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    target = tree.nodes.new("ShaderNodeTexImage")
    target.image = image
    for node in tree.nodes:
        node.select = False
    target.select = True
    tree.nodes.active = target


def _probe(obj, source_material, field, slot_index, output_materials, resolution, negative, control):
    owned = _owned()
    indices = [face.material_index for face in obj.data.polygons]
    try:
        image = _target_image("__IG_NATIVE_PROBE", resolution, owned)
        probe = native_graph.clone_material(source_material, "__IG_NATIVE_PROBE", owned["materials"], owned["node_groups"], owned["texts"])
        vector = native_graph.expose_field(probe, field)
        _emission_material(probe, image, vector, negative)
        neutral = bpy.data.materials.new("__IG_NATIVE_NEUTRAL")
        owned["materials"].append(neutral)
        neutral.use_nodes = True
        _emission_material(neutral, image)
        _assign_materials(obj, [probe if index == slot_index else neutral
                                for index in range(len(output_materials))], indices)
        _boundary(control, "native_field_pass", field=field["input"], polarity="negative" if negative else "positive")
        engine.cycles_bake("EMIT", resolution)
        values = np.empty(resolution * resolution * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        values = values.reshape(resolution, resolution, 4)
        if not np.isfinite(values).all():
            raise NativeRefusal("Native emission probe produced nonfinite samples")
        return values[..., :3].copy()
    finally:
        _assign_materials(obj, output_materials, indices)
        _remove(owned)


def _write_float_field(values, path, scene, owned):
    """Round-trip validation prevents a color transform or half-float publication."""
    height, width = values.shape[:2]
    if width != height or not np.isfinite(values).all():
        raise NativeRefusal("Invalid finite square field image")
    image = _target_image("IG Field " + path.stem, width, owned)
    rgba = np.ones((height, width, 4), dtype=np.float32)
    rgba[..., :3] = values
    image.pixels.foreach_set(rgba.ravel())
    image.file_format = "OPEN_EXR"
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "32"
    scene.render.image_settings.exr_codec = "ZIP"
    image.save_render(str(path), scene=scene)
    loaded = bpy.data.images.load(str(path), check_existing=False)
    owned["images"].append(loaded)
    loaded.colorspace_settings.name = "Non-Color"
    loaded.alpha_mode = "CHANNEL_PACKED"
    actual = np.empty(rgba.size, dtype=np.float32)
    loaded.pixels.foreach_get(actual)
    if not np.array_equal(actual.reshape(rgba.shape), rgba):
        difference = float(np.max(np.abs(actual.reshape(rgba.shape) - rgba)))
        raise NativeRefusal("EXR32 field round trip altered raw samples (max error %g)" % difference)
    loaded.pack()
    # The reloaded, verified image is the sole image used by the native graph.
    owned["images"].remove(image)
    bpy.data.images.remove(image)
    return loaded


def _plan_object(source, obj, owned):
    used = {face.material_index for face in obj.data.polygons}
    sources = list(obj.data.materials)
    if not sources or any(index >= len(sources) or sources[index] is None for index in used):
        raise NativeRefusal("Every evaluated face must have a material")
    plans, materials = [], []
    for index, material in enumerate(sources):
        if material is None:
            # Unused empty slots receive a private no-op material so bake targets
            # can be assigned uniformly without altering polygon slot indices.
            material = bpy.data.materials.new("__IG_NATIVE_EMPTY")
            owned["materials"].append(material)
            material.use_nodes = True
            sources[index] = material
        if not material.use_nodes:
            raise NativeRefusal("Legacy non-node material has no native Cycles closure graph")
        plan = native_graph.inspect_material(material)
        if index not in used:
            plan["fields"] = []
        if plan["blockers"]:
            raise NativeRefusal(json.dumps(plan["blockers"], ensure_ascii=False))
        if (source.modifiers or source.data.shape_keys) and plan["uses_generated_coordinates"]:
            raise NativeRefusal("Evaluated modifier mesh plus Generated coordinates needs proven original texture-space preservation")
        plans.append(plan)
        materials.append(native_graph.clone_material(material, "IG Native " + material.name,
                                                     owned["materials"], owned["node_groups"], owned["texts"]))
    indices = [face.material_index for face in obj.data.polygons]
    _assign_materials(obj, materials, indices)
    captures = _capture_images(materials, plans, owned)
    return sources, materials, plans, captures


def _write_manifest(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def run(objects, output_dir, *, resolution, device, gpu_device_id, run_control=None):
    """Return clean/failed/cancelled census; only completed objects are published.

    Cancellation is cooperative between native calls. A running Cycles call is
    never claimed to be interruptible. Existing output folders are never reused.
    """
    if isinstance(resolution, bool) or not isinstance(resolution, int) or not 16 <= resolution <= 8192:
        raise ValueError("Native field resolution must be an integer from 16 to 8192")
    if device not in {"CPU", "GPU"}:
        raise ValueError("Native device must be CPU or GPU")
    sources = list(objects)
    scene = bpy.context.scene
    source_layer = bpy.context.view_layer
    folder = Path(output_dir).resolve() / ("native_" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    manifest = folder / "native_manifest.json"
    report = {"schema": 1, "target_profile": "BLENDER_NATIVE", "status": "RUNNING", "exit_code": 1,
              "manifest": str(manifest), "output_objects": [], "objects": [], "total": len(sources),
              "ok": 0, "failed": 0, "skipped": 0, "converted_fields": 0, "retained_fields": 0,
              "baked": False, "blender_version": list(bpy.app.version), "frame": scene.frame_current,
              "source_blend": bpy.data.filepath, "source_scene": scene.name,
              "source_render_engine": scene.render.engine, "field_evaluation_engine": "CYCLES",
              "subframe": scene.frame_subframe, "resolution": resolution,
              "geometry_policy": "evaluated frame mesh; original UV sets, attributes and normals; no repack or decimation",
              "fidelity": "Static fields are finitely sampled, not mathematically exact procedural equivalents.",
              "sampling": {"filter": "LINEAR", "margin_px": 0, "format": "EXR", "bits": 32,
                           "signed": True, "hdr": True, "color_transform": "none; exact float32 round-trip checked"},
              "warnings": ["No seam padding or adaptive frequency/error analysis in this native slice."]}
    started = time.monotonic()
    bake_scene = bpy.data.scenes.new("__IG_NATIVE_BAKE")
    bake_scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
    bake_scene.render.engine = "CYCLES"
    bake_scene.cycles.samples = 1
    bake_scene.cycles.use_denoising = False
    bake_scene.cycles.use_adaptive_sampling = False
    bake_scene.render.use_persistent_data = False
    bake_scene.view_settings.view_transform = "Standard"
    bake_scene.view_settings.exposure = 0
    bake_scene.view_settings.gamma = 1
    collection = bpy.data.collections.new("Import Glue Native " + folder.name)
    scene.collection.children.link(collection)
    prior = engine.DEVICE, engine.GPU_DEVICE_ID, engine._RUN_CONTROL
    engine.DEVICE, engine.GPU_DEVICE_ID, engine._RUN_CONTROL = device, str(gpu_device_id or ""), run_control
    try:
        _boundary(run_control, "native_preflight", total=len(sources))
        depsgraph = bpy.context.evaluated_depsgraph_get()
        layer = bake_scene.view_layers[0]
        with bpy.context.temp_override(scene=bake_scene, view_layer=layer):
            with engine.SceneSettingsGuard(snapshot_cycles_preferences=device == "GPU"):
                engine.ensure_cycles_device()
                report["device_selection"] = dict(engine._LAST_DEVICE_SELECTION)
                engine.configure_bake(resolution)
                bake_scene.render.bake.margin = 0
                bake_scene.render.bake.target = "IMAGE_TEXTURES"
                for index, source in enumerate(sources):
                    owned, paths, committed = _owned(), [], False
                    item = {"source": source.name, "status": "RUNNING", "fields": []}
                    try:
                        _boundary(run_control, "native_object", object=source.name, current=index, total=len(sources))
                        with bpy.context.temp_override(scene=scene, view_layer=source_layer):
                            item["source_fingerprint"] = engine.source_fingerprint(source, {})
                        obj = _duplicate(source, owned, depsgraph)
                        source_materials, output_materials, plans, captures = _plan_object(source, obj, owned)
                        item["captured_images"] = captures
                        item["mesh"] = {"vertices": len(obj.data.vertices), "polygons": len(obj.data.polygons),
                                        "loops": len(obj.data.loops), "uv_layers": [uv.name for uv in obj.data.uv_layers],
                                        "attributes": [attribute.name for attribute in obj.data.attributes],
                                        "evaluated_modifiers": bool(source.modifiers),
                                        "evaluated_shape_keys": source.data.shape_keys is not None}
                        uv_name = engine.choose_source_uv(obj.data)
                        gate = uv_validation.validate_uv(obj, uv_name, require_unique=True)
                        item["uv_validation"] = gate
                        bake_scene.collection.objects.link(obj)
                        obj.select_set(True, view_layer=layer)
                        layer.objects.active = obj
                        if uv_name:
                            obj.data.uv_layers.active = obj.data.uv_layers[uv_name]
                        layer.update()
                        with bpy.context.temp_override(scene=bake_scene, view_layer=layer, active_object=obj,
                                object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
                            for slot, plan in enumerate(plans):
                                for field in plan["fields"]:
                                    record = {**field, "material_slot": slot}
                                    item["fields"].append(record)
                                    if not field["eligible"] or not gate["ok"]:
                                        record["action"] = "RETAIN_NATIVE"
                                        if field["eligible"]:
                                            record["reason"] = "Existing UV layout cannot hold unique static samples; native graph retained"
                                        continue
                                    _boundary(run_control, "native_field", object=source.name, field=field["input"])
                                    positive = _probe(obj, output_materials[slot], field, slot, output_materials,
                                                      resolution, False, run_control)
                                    negative = _probe(obj, output_materials[slot], field, slot, output_materials,
                                                      resolution, True, run_control)
                                    values = positive - negative
                                    label = re.sub(r"[^A-Za-z0-9_-]+", "_", field["input"])[:48]
                                    path = folder / ("%03d_%03d_%s.exr" % (index, len(item["fields"]), label))
                                    paths.append(path)
                                    image = _write_float_field(values, path, bake_scene, owned)
                                    native_graph.replace_field(output_materials[slot], field, image, uv_name)
                                    record.update(action="SAMPLED_STATIC_FIELD", image=str(path),
                                                  minimum=float(values.min()), maximum=float(values.max()),
                                                  packed=True, float_bits=32, uv_layer=uv_name)
                        _boundary(run_control, "native_object_commit", object=source.name)
                        with bpy.context.temp_override(scene=scene, view_layer=source_layer):
                            if engine.source_fingerprint(source, {}) != item["source_fingerprint"]:
                                raise NativeRefusal("Source state changed during native processing; output not published")
                        item["source_unchanged"] = True
                        converted = sum(field["action"] == "SAMPLED_STATIC_FIELD" for field in item["fields"])
                        retained = len(item["fields"]) - converted
                        item.update(status="BAKED" if converted else "RETAINED_ONLY", output_object=obj.name,
                                    converted_fields=converted, retained_fields=retained,
                                    warning="" if converted else "No static fields converted; native material retained without optimization claim")
                        collection.objects.link(obj)
                        bake_scene.collection.objects.unlink(obj)
                        committed = True
                        report["output_objects"].append(obj.name)
                        report["converted_fields"] += converted
                        report["retained_fields"] += retained
                        report["ok"] += 1
                    except (RunCancelled, KeyboardInterrupt):
                        item["status"] = "CANCELLED"
                        raise
                    except Exception as exc:
                        item.update(status="REFUSED" if isinstance(exc, NativeRefusal) else "FAILED", error=str(exc))
                        report["failed"] += 1
                    finally:
                        report["objects"].append(item)
                        if not committed:
                            _remove(owned)
                            for path in paths:
                                path.unlink(missing_ok=True)
                        _write_manifest(manifest, report)
        report["status"] = "COMPLETED" if not report["failed"] else "COMPLETED_WITH_FAILURES"
        report["exit_code"] = 0 if report["ok"] and not report["failed"] else 1
    except (RunCancelled, KeyboardInterrupt) as exc:
        report.update(status="CANCELLED", exit_code=130, error=str(exc))
    except Exception as exc:
        report.update(status="FAILED", exit_code=1, error=str(exc))
    finally:
        engine.DEVICE, engine.GPU_DEVICE_ID, engine._RUN_CONTROL = prior
        bpy.data.scenes.remove(bake_scene)
        if not collection.objects:
            bpy.data.collections.remove(collection)
        report["baked"] = report["converted_fields"] > 0
        report["elapsed_seconds"] = time.monotonic() - started
        _write_manifest(manifest, report)
    return report
