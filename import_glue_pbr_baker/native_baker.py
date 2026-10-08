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

from . import engine, native_graph, native_quality, native_optimization, source_capture, uv_validation, shader_resources, ocio_capture
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


def _capture_images(materials, plans, owned, shared=None):
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
            key = None
            if shared is not None and original.source in {"FILE","GENERATED"}:
                key = (pointer,json.dumps(source_capture._image_state(original,pixels=True),sort_keys=True))
                previous = shared.get(key)
                if previous:
                    try:
                        if previous["image"].as_pointer():
                            by_pointer[pointer] = previous["image"]
                            records.append({**previous["record"],"shared_capture":True})
                            continue
                    except ReferenceError:
                        shared.pop(key,None)
            capture = source_capture.capture_image_copy(
                original, [candidate for candidate in required_nodes if candidate.image == original])
            if not capture["owned"]:
                raise NativeRefusal("Linked image capture is external, not self-contained: " + original.name)
            owned["images"].append(capture["image"])
            by_pointer[pointer] = capture["image"]
            records.append(capture["record"])
            if key is not None:
                shared[key] = capture
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
    # Never install depsgraph-owned IDs in persistent datablocks: the next
    # graph evaluation can free them while the copied mesh still references it.
    effective = [getattr(slot.material, "original", slot.material) for slot in evaluated.material_slots]
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


def _preserve_texture_space(source, obj, owned):
    """Capture a POINT attribute for verified deformation topology.

    This is deliberately limited to deformation modifiers and shape keys.
    Topology-changing modifiers need evaluated ORCO provenance, not a guessed
    coordinate transfer or a new bounding-box normalization.
    """
    deformation_types = {"ARMATURE", "CAST", "CORRECTIVE_SMOOTH", "DISPLACE", "HOOK",
                         "LAPLACIANDEFORM", "LAPLACIANSMOOTH", "LATTICE", "MESH_DEFORM",
                         "SHRINKWRAP", "SIMPLE_DEFORM", "SMOOTH", "SURFACE_DEFORM", "WARP", "WAVE"}
    if any(mod.type not in deformation_types for mod in source.modifiers):
        raise NativeRefusal("Generated coordinates with topology-changing modifiers require evaluated ORCO capture")
    original, evaluated = source.data, obj.data
    if (len(original.vertices) != len(evaluated.vertices) or len(original.edges) != len(evaluated.edges)
            or len(original.polygons) != len(evaluated.polygons) or len(original.loops) != len(evaluated.loops)):
        raise NativeRefusal("Generated-coordinate deformation changed mesh topology")
    for name, prop, width in (("edges", "vertices", 2), ("loops", "vertex_index", 1)):
        first, second = getattr(original,name), getattr(evaluated,name)
        a, b = np.empty(len(first)*width,dtype=np.int32), np.empty(len(second)*width,dtype=np.int32)
        first.foreach_get(prop,a)
        second.foreach_get(prop,b)
        if not np.array_equal(a,b):
            raise NativeRefusal("Generated-coordinate deformation changed vertex/edge correspondence")
    texture_source = original.texco_mesh or original
    if len(texture_source.vertices) != len(evaluated.vertices):
        raise NativeRefusal("Explicit ORCO texture mesh has a different vertex count")
    coordinates = np.empty(len(texture_source.vertices)*3,dtype=np.float32)
    if texture_source.shape_keys:
        texture_source.shape_keys.reference_key.data.foreach_get("co",coordinates)
    else:
        texture_source.vertices.foreach_get("co",coordinates)
    location = np.asarray(original.texspace_location,dtype=np.float32)
    size = np.asarray(original.texspace_size,dtype=np.float32)
    if not np.isfinite(coordinates).all() or not np.isfinite(location).all() or not np.isfinite(size).all() or np.any(np.abs(size)<1e-8):
        raise NativeRefusal("Generated-coordinate texture space is nonfinite or degenerate")
    coordinates = (coordinates.reshape(-1,3)-location)/(2*size)+.5
    attribute = evaluated.attributes.new("__IG_ORCO_"+uuid.uuid4().hex[:12],"FLOAT_VECTOR","POINT")
    attribute.data.foreach_set("vector",coordinates.ravel())
    return {"policy": "private POINT attribute; original texture-space normalization and basis coordinates",
            "topology_correspondence_checked": True, "modifiers": [mod.type for mod in source.modifiers],
            "attribute": attribute.name}


def _replace_generated(material, attribute_name):
    for tree in _trees(material):
        attribute = None
        def socket():
            nonlocal attribute
            if attribute is None:
                attribute = tree.nodes.new("ShaderNodeAttribute")
                attribute.attribute_name = attribute_name
                attribute.attribute_type = "GEOMETRY"
                attribute.label = "Preserved original Generated coordinates"
            return attribute.outputs["Vector"]
        for node in list(tree.nodes):
            if node.bl_idname == "ShaderNodeTexCoord":
                for link in list(node.outputs["Generated"].links):
                    tree.links.new(socket(),link.to_socket)
            if node.bl_idname.startswith("ShaderNodeTex") and node.bl_idname not in {"ShaderNodeTexImage","ShaderNodeTexEnvironment"}:
                vector = node.inputs.get("Vector")
                if vector is not None and not vector.is_linked:
                    tree.links.new(socket(),vector)


def _target_image(name, resolution, owned):
    image = bpy.data.images.new(name, width=resolution, height=resolution, alpha=True, float_buffer=True)
    owned["images"].append(image)
    image.colorspace_settings.name = ocio_capture.target_contract()["roles"]["data"]
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
    loaded.colorspace_settings.name = ocio_capture.target_contract()["roles"]["data"]
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


def _plan_object(source, obj, owned, shared_images=None):
    used = {face.material_index for face in obj.data.polygons}
    sources = list(obj.data.materials)
    if not sources or any(index >= len(sources) or sources[index] is None for index in used):
        raise NativeRefusal("Every evaluated face must have a material")
    plans, materials = [], []
    for index, material in enumerate(sources):
        if index not in used:
            # Unused empty slots receive a private no-op material so bake targets
            # can be assigned uniformly without altering polygon slot indices.
            material = bpy.data.materials.new("__IG_NATIVE_EMPTY")
            owned["materials"].append(material)
            material.use_nodes = True
            sources[index] = material
        if not material.use_nodes:
            raise NativeRefusal("Legacy non-node material has no native Cycles closure graph")
        plan = native_graph.inspect_material(material)
        if plan["blockers"]:
            raise NativeRefusal(json.dumps(plan["blockers"], ensure_ascii=False))
        if (source.modifiers or source.data.shape_keys) and plan["uses_generated_coordinates"]:
            if plan["retained_domains"]:
                raise NativeRefusal("Generated-coordinate preservation for deformed volume/displacement domains requires domain-aware ORCO evaluation")
            if not obj.get("import_glue_orco_preserved"):
                obj["import_glue_orco_preserved"] = json.dumps(_preserve_texture_space(source, obj, owned))
        plans.append(plan)
        materials.append(native_graph.clone_material(material, "IG Native " + material.name,
                                                     owned["materials"], owned["node_groups"], owned["texts"]))
        if plan["uses_generated_coordinates"] and obj.get("import_glue_orco_preserved"):
            _replace_generated(materials[-1],json.loads(obj["import_glue_orco_preserved"])["attribute"])
        for resource in plan["external_resources"]:
            tree = materials[-1].node_tree
            for name in resource["group_path"]:
                tree = tree.nodes[name].node_tree
            resource["capture"] = shader_resources.capture_node(tree.nodes[resource["node"]],owned["texts"])
    indices = [face.material_index for face in obj.data.polygons]
    _assign_materials(obj, materials, indices)
    captures = _capture_images(materials, plans, owned, shared_images)
    return sources, materials, plans, captures


def _write_manifest(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def run(objects, output_dir, *, resolution, device, gpu_device_id, run_control=None,
        quality_settings=None, checkpoint_context=None, image_capture_cache=None):
    """Return clean/failed/cancelled census; only completed objects are published.

    Cancellation is cooperative between native calls. A running Cycles call is
    never claimed to be interruptible. Existing output folders are never reused.
    """
    if isinstance(resolution, bool) or not isinstance(resolution, int) or not 16 <= resolution <= 8192:
        raise ValueError("Native field resolution must be an integer from 16 to 8192")
    if device not in {"CPU", "GPU"}:
        raise ValueError("Native device must be CPU or GPU")
    sources = list(objects)
    quality = native_quality.settings(quality_settings)
    color_contract = ocio_capture.target_contract()
    if not color_contract["native_supported"]:
        raise NativeRefusal("Native color contract is unsupported: " + "; ".join(color_contract["native_reasons"]))
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
              "quality_settings": quality,
              "color_contract": color_contract,
              "sampling": {"filter": "LINEAR", "margin_px": quality["margin_px"], "format": "EXR", "bits": 32,
                           "signed": True, "hdr": True, "color_transform": "none; exact float32 round-trip checked"},
              "warnings": ["Finite independent UV samples and box-mip comparisons cannot prove arbitrary-view equivalence."]}
    started = time.monotonic()
    bake_scene = bpy.data.scenes.new("__IG_NATIVE_BAKE")
    bake_scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
    bake_scene.render.engine = "CYCLES"
    bake_scene.cycles.samples = 1
    bake_scene.cycles.use_denoising = False
    bake_scene.cycles.use_adaptive_sampling = False
    bake_scene.cycles.shading_system = scene.cycles.shading_system
    bake_scene.render.use_persistent_data = False
    bake_scene.display_settings.display_device = scene.display_settings.display_device
    bake_scene.view_settings.view_transform = scene.view_settings.view_transform
    bake_scene.view_settings.exposure = 0
    bake_scene.view_settings.gamma = 1
    collection = bpy.data.collections.new("Import Glue Native " + folder.name)
    scene.collection.children.link(collection)
    prior = engine.DEVICE, engine.GPU_DEVICE_ID, engine._RUN_CONTROL
    engine.DEVICE, engine.GPU_DEVICE_ID, engine._RUN_CONTROL = device, str(gpu_device_id or ""), run_control
    shared_images = image_capture_cache if image_capture_cache is not None else {}
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
                        source_materials, output_materials, plans, captures = _plan_object(source, obj, owned, shared_images)
                        item["captured_images"] = captures
                        item["captured_shader_resources"] = [resource for plan in plans for resource in plan["external_resources"]]
                        item["requires_osl"] = any(resource["kind"]=="OSL" for resource in item["captured_shader_resources"])
                        if obj.get("import_glue_orco_preserved"):
                            item["generated_coordinates"] = json.loads(obj["import_glue_orco_preserved"])
                        item["source_graph_statistics"] = native_optimization.statistics(source_materials)
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
                            shared_fields = {}
                            for slot, plan in enumerate(plans):
                                triangles, island_ids = native_optimization.uv_triangles(obj.data, uv_name, slot) if gate["ok"] else (None, None)
                                # Identities are computed before replacing any receiver so
                                # repeated consumers of one DAG expression share a sample.
                                keys = {field["input_identifier"] + "/" + "/".join(field["group_path"]) + "/" + field["node"]:
                                        native_optimization.field_key(output_materials[slot], field,
                                            source_fingerprint=item["source_fingerprint"], slot=slot,
                                            uv_name=uv_name, resolution=resolution, quality=quality)
                                        for field in plan["fields"] if field["eligible"] and gate["ok"]}
                                for field in plan["fields"]:
                                    record = {**field, "material_slot": slot}
                                    item["fields"].append(record)
                                    if not field["eligible"] or not gate["ok"]:
                                        record["action"] = "RETAIN_NATIVE"
                                        if field["eligible"]:
                                            record["reason"] = "Existing UV layout cannot hold unique static samples; native graph retained"
                                        continue
                                    _boundary(run_control, "native_field", object=source.name, field=field["input"])
                                    key = keys[field["input_identifier"] + "/" + "/".join(field["group_path"]) + "/" + field["node"]]
                                    if key in shared_fields:
                                        image, saved = shared_fields[key]
                                        native_graph.replace_field(output_materials[slot], field, image, uv_name)
                                        record.update(saved, shared_field=True, sampling_key=key)
                                        continue
                                    label = re.sub(r"[^A-Za-z0-9_-]+", "_", field["input"])[:48]
                                    path = folder / ("%03d_%03d_%s.exr" % (index, len(item["fields"]), label))
                                    cached = checkpoint_context.load_field(item["source_fingerprint"], key) if checkpoint_context else None
                                    if cached:
                                        saved = dict(cached["metadata"])
                                        width = int(saved["sample_resolution"])
                                        cached_image = bpy.data.images.load(str(cached["path"]), check_existing=False)
                                        try:
                                            cached_image.colorspace_settings.name = color_contract["roles"]["data"]
                                            if tuple(cached_image.size) != (width, width) or not cached_image.is_float:
                                                raise NativeRefusal("Checkpoint field dimensions/precision do not match metadata")
                                            pixels = np.empty(width*width*4, dtype=np.float32)
                                            cached_image.pixels.foreach_get(pixels)
                                            values = pixels.reshape(width,width,4)[...,:3].copy()
                                            if not saved.get("quality",{}).get("accepted") or not np.isfinite(values).all():
                                                raise NativeRefusal("Checkpoint field lacks a successful finite quality result")
                                        finally:
                                            bpy.data.images.remove(cached_image)
                                        record.update(saved, checkpoint_reused=True)
                                    else:
                                        attempts, values = [], None
                                        for size in native_quality.resolutions(resolution, quality):
                                            _boundary(run_control, "native_quality_attempt", object=source.name, field=field["input"], resolution=size)
                                            owner, coverage = native_quality.rasterize(triangles, island_ids, size)
                                            if not coverage["covered_pixels"] or coverage["conflicting_pixels"] or coverage["unsampled_triangles"]:
                                                attempts.append({"resolution": size, "accepted": False, "coverage": coverage,
                                                                 "reason": "UV coverage is incomplete at this resolution"})
                                                continue
                                            positive = _probe(obj, output_materials[slot], field, slot, output_materials, size, False, run_control)
                                            negative = _probe(obj, output_materials[slot], field, slot, output_materials, size, True, run_control)
                                            candidate, _padded_owners, padding = native_quality.pad_islands(positive-negative, owner, quality["margin_px"])
                                            reference_size = size*2
                                            reference_owner, reference_coverage = native_quality.rasterize(triangles, island_ids, reference_size)
                                            ref_positive = _probe(obj, output_materials[slot], field, slot, output_materials, reference_size, False, run_control)
                                            ref_negative = _probe(obj, output_materials[slot], field, slot, output_materials, reference_size, True, run_control)
                                            reference, _labels, _info = native_quality.pad_islands(ref_positive-ref_negative, reference_owner, quality["margin_px"]*2)
                                            measured = native_quality.measure(candidate, reference, reference_owner, quality)
                                            measured.update(resolution=size, reference_resolution=reference_size, coverage=coverage,
                                                            reference_coverage=reference_coverage, padding=padding)
                                            measured["accepted"] &= not reference_coverage["conflicting_pixels"] and not reference_coverage["unsampled_triangles"]
                                            attempts.append(measured)
                                            if measured["accepted"]:
                                                values = candidate
                                                break
                                        record["quality_attempts"] = attempts
                                        if values is None:
                                            record.update(action="RETAIN_NATIVE", reason="Finite field/boundary/mip error or UV coverage exceeded the requested quality budget")
                                            continue
                                        record.update(sample_resolution=int(values.shape[0]), quality=attempts[-1])
                                    paths.append(path)
                                    image = _write_float_field(values, path, bake_scene, owned)
                                    native_graph.replace_field(output_materials[slot], field, image, uv_name)
                                    record.update(action="SAMPLED_STATIC_FIELD", image=str(path),
                                                  minimum=float(values.min()), maximum=float(values.max()),
                                                  packed=True, float_bits=32, uv_layer=uv_name)
                                    record["sampling_key"] = key
                                    saved = {name: value for name, value in record.items() if name in {
                                        "action", "image", "minimum", "maximum", "packed", "float_bits", "uv_layer",
                                        "sampling_key", "sample_resolution", "quality", "quality_attempts", "checkpoint_reused"}}
                                    shared_fields[key] = (image, saved)
                                    if checkpoint_context and not cached:
                                        checkpoint_context.save_field(item["source_fingerprint"], key, str(path), saved)
                                    _boundary(run_control, "native_field_complete", object=source.name, field=field["input"], image=str(path), sampling_key=key)
                        item["optimization"] = native_optimization.prune_private(output_materials)
                        item["optimization"]["deduplicated_fields"] = sum(bool(f.get("shared_field")) for f in item["fields"])
                        # Release private resources made unreachable by replacement.
                        for kind in ("node_groups", "images", "texts", "materials"):
                            for resource in list(owned[kind]):
                                if resource.users == 0:
                                    getattr(bpy.data, kind).remove(resource)
                                    owned[kind].remove(resource)
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
