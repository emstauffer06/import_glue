"""Bounded, opt-in capture of static scalar shader displacement as geometry.

This is a vertex-sampled approximation at a declared tessellation level, not
Cycles adaptive subdivision. Unsupported fields and discontinuities are refused
instead of welding incompatible displaced corners together.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import bpy
import bmesh
import numpy as np


SCHEMA_VERSION = 1
DEFAULTS = {"enabled": False, "subdivision_levels": 2, "max_vertices": 250000,
            "max_triangles": 500000, "max_working_bytes": 536870912,
            "max_image_pixels": 16777216, "seam_tolerance": 1e-6}


class GeometryRefusal(RuntimeError):
    pass


def settings(value=None):
    value = {} if value is None else dict(value)
    if "subdivisions" in value:
        if "subdivision_levels" in value and value["subdivisions"] != value["subdivision_levels"]:
            raise ValueError("Conflicting subdivisions and subdivision_levels")
        value["subdivision_levels"] = value.pop("subdivisions")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown geometry settings: " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **value}
    if not isinstance(result["enabled"], bool):
        raise ValueError("geometry enabled must be a boolean")
    for key, lo, hi in (("subdivision_levels", 0, 6), ("max_vertices", 3, 10000000),
                        ("max_triangles", 1, 20000000), ("max_working_bytes", 1024, 17179869184),
                        ("max_image_pixels", 1, 268435456)):
        val = result[key]
        if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
            raise ValueError("geometry %s must be an integer in [%d, %d]" % (key, lo, hi))
    tol = result["seam_tolerance"]
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or not 0 < tol <= 1e-3:
        raise ValueError("geometry seam_tolerance must be finite in (0, 0.001]")
    return result


validate_settings = settings


def _boundary(control, phase, **fields):
    if control:
        control.emit({"kind": "geometry_capture", "phase": phase, **fields})
        control.check_cancel()


def _input(socket):
    links = [link for link in socket.links if getattr(link, "is_valid", True) and not getattr(link, "is_muted", False)]
    if len(links) != 1:
        raise GeometryRefusal("Expected one active link into " + socket.name)
    link = links[0]
    if link.from_node.mute:
        raise GeometryRefusal("Muted displacement nodes require explicit graph evaluation")
    return link.from_node, link.from_socket


def _constant(socket, label):
    if socket.is_linked:
        raise GeometryRefusal(label + " must be an unlinked scalar constant")
    number = float(socket.default_value)
    if not math.isfinite(number):
        raise GeometryRefusal(label + " must be finite")
    return number


def _render_uv(mesh):
    layer = next((layer for layer in mesh.uv_layers if layer.active_render), mesh.uv_layers.active)
    return layer.name if layer else ""


def _field(material, mesh):
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    output = material.node_tree.get_output_node("CYCLES")
    if output is None or not output.inputs["Displacement"].is_linked:
        return None
    if getattr(material, "displacement_method", "UNKNOWN") != "DISPLACEMENT":
        raise GeometryRefusal("%s: choose Displacement Only; Bump and Displacement+Bump require separate residual-normal capture" % material.name)
    node, socket = _input(output.inputs["Displacement"])
    if node.bl_idname != "ShaderNodeDisplacement" or socket.name != "Displacement":
        raise GeometryRefusal("%s: only a direct scalar Displacement node is supported" % material.name)
    if node.space != "OBJECT" or node.inputs["Normal"].is_linked:
        raise GeometryRefusal("%s: displacement requires Object space and its default Normal" % material.name)
    result = {"material": material.name, "midlevel": _constant(node.inputs["Midlevel"], "Midlevel"),
              "scale": _constant(node.inputs["Scale"], "Scale"), "output": output.name,
              "displacement_node": node.name}
    height = node.inputs["Height"]
    if not height.is_linked:
        return {**result, "kind": "CONSTANT", "value": _constant(height, "Height")}
    image_node, output_socket = _input(height)
    component = None
    if image_node.bl_idname == "ShaderNodeSeparateColor" and image_node.mode == "RGB":
        component = {"Red": 0, "Green": 1, "Blue": 2}.get(output_socket.name)
        image_node, output_socket = _input(image_node.inputs["Color"])
        if output_socket.name != "Color":
            raise GeometryRefusal("Separate Color must read the image Color output")
    if image_node.bl_idname != "ShaderNodeTexImage":
        raise GeometryRefusal("Height must be a static UV Image Texture, optionally through Separate Color RGB")
    if output_socket.name == "Alpha":
        component = 3
    elif output_socket.name != "Color":
        raise GeometryRefusal("Unknown image height output")
    image = image_node.image
    if image is None or image.source not in {"FILE", "GENERATED"} or image.type not in {"IMAGE", "UV_TEST"}:
        raise GeometryRefusal("Height requires a static single-buffer FILE or GENERATED image")
    if image_node.projection != "FLAT" or image_node.interpolation not in {"Linear", "Closest"}:
        raise GeometryRefusal("Height image requires Flat projection and Linear or Closest interpolation")
    if image_node.extension not in {"REPEAT", "EXTEND"}:
        raise GeometryRefusal("Height image requires Repeat or Extend addressing")
    if component != 3 and not image.colorspace_settings.is_data:
        raise GeometryRefusal("Color height channels must use a data color space, without a color transform")
    vector = image_node.inputs["Vector"]
    if not vector.is_linked:
        raise GeometryRefusal("Height image needs an explicit UV Map or Texture Coordinate UV input")
    coordinates, vector_socket = _input(vector)
    if coordinates.bl_idname == "ShaderNodeUVMap" and not coordinates.from_instancer:
        uv_name = coordinates.uv_map or _render_uv(mesh)
    elif coordinates.bl_idname == "ShaderNodeTexCoord" and vector_socket.name == "UV" and not coordinates.from_instancer:
        uv_name = _render_uv(mesh)
    else:
        raise GeometryRefusal("Height coordinates must be the direct mesh UV output")
    if not uv_name or mesh.uv_layers.get(uv_name) is None:
        raise GeometryRefusal("Height UV layer is missing: " + uv_name)
    result.update(kind="IMAGE", image=image.name, image_pointer=image.as_pointer(), component=component,
                  uv_layer=uv_name, interpolation=image_node.interpolation, extension=image_node.extension)
    return result


def _plan(source, config):
    if source.type != "MESH" or source.mode != "OBJECT":
        raise GeometryRefusal("Geometry capture requires a mesh in Object Mode")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(depsgraph)
    mesh = evaluated.data
    materials = [getattr(slot.material, "original", slot.material) for slot in evaluated.material_slots]
    fields = {int(index): _field(materials[index] if index < len(materials) else None, mesh)
              for index in sorted({face.material_index for face in mesh.polygons})}
    if not any(fields.values()):
        return evaluated, materials, fields, {"status": "NO_DISPLACEMENT", "source": source.name, "schema": SCHEMA_VERSION}
    if not config["enabled"]:
        raise GeometryRefusal("Static displacement geometry is opt-in; enable geometry capture")
    from . import native_graph
    for slot in fields:
        material = materials[slot] if slot < len(materials) else None
        if material and material.use_nodes and material.node_tree and native_graph.inspect_material(material)["uses_generated_coordinates"]:
            raise GeometryRefusal("Surface Generated coordinates require undeformed ORCO transfer before geometry capture")
    transform = np.asarray(evaluated.matrix_world, dtype=np.float64)
    if not np.isfinite(transform).all() or abs(evaluated.matrix_world.to_3x3().determinant()) < 1e-12:
        raise GeometryRefusal("Displacement requires a finite, nonsingular object transform")
    if source.instance_type != "NONE" or any(inst.is_instance and inst.parent is not None and inst.parent.original == source for inst in depsgraph.object_instances):
        raise GeometryRefusal("Unrealized instances require an explicit realization policy")
    if getattr(getattr(source, "cycles", None), "use_adaptive_subdivision", False):
        raise GeometryRefusal("Cycles adaptive subdivision is not captured by this finite evaluated mesh")
    if bpy.context.scene.render.use_simplify:
        raise GeometryRefusal("Render Simplify can change displacement tessellation; disable it for capture")
    for modifier in source.modifiers:
        if modifier.type == "NODES":
            raise GeometryRefusal("Geometry Nodes needs a separate render-context and instance-realization proof")
        if modifier.show_viewport != modifier.show_render:
            raise GeometryRefusal("Modifier %s has different viewport/render visibility" % modifier.name)
        if modifier.show_render and modifier.type in {"SUBSURF", "MULTIRES"} and modifier.levels != modifier.render_levels:
            raise GeometryRefusal("Modifier %s has different viewport/render subdivision levels" % modifier.name)
    if not mesh.polygons or any(len(face.vertices) not in {3, 4} for face in mesh.polygons):
        raise GeometryRefusal("Bounded subdivision requires a nonempty triangle/quad mesh")
    # Admission uses the already evaluated mesh. No private mesh, image pixel
    # buffer or BMesh is allocated until these conservative bounds pass.
    n = 2 ** config["subdivision_levels"]
    tris = sum(len(face.vertices) == 3 for face in mesh.polygons)
    quads = len(mesh.polygons) - tris
    vertices = len(mesh.vertices) + len(mesh.edges) * (n - 1) + tris * ((n - 1) * (n - 2) // 2) + quads * (n - 1) ** 2
    triangles = (tris + 2 * quads) * n * n
    loops = (tris * 3 + quads * 4) * n * n
    image_pixels, image_channels = {}, {}
    for field in fields.values():
        if field and field["kind"] == "IMAGE":
            image = bpy.data.images.get(field["image"])
            pixels = int(image.size[0]) * int(image.size[1])
            if pixels <= 0 or pixels > config["max_image_pixels"]:
                raise GeometryRefusal("Height image %s exceeds the decoded-pixel budget or is empty" % image.name)
            image_pixels[field["image_pointer"]] = pixels
            image_channels.setdefault(field["image_pointer"], set()).add(field["component"])
    attribute_bytes = 64 * len(mesh.attributes)
    working_bytes = (vertices * (512 + attribute_bytes) + loops * (512 + attribute_bytes)
                     + triangles * 128 + sum(pixels * (24 + 4 * len(image_channels[pointer]))
                                               for pointer, pixels in image_pixels.items()))
    for actual, limit, label in ((vertices, config["max_vertices"], "vertices"),
                                 (triangles, config["max_triangles"], "triangles"),
                                 (working_bytes, config["max_working_bytes"], "working bytes")):
        if actual > limit:
            raise GeometryRefusal("Geometry admission estimates %d %s, exceeding budget %d" % (actual, label, limit))
    report = {"schema": SCHEMA_VERSION, "status": "READY", "source": source.name,
              "source_frame": bpy.context.scene.frame_current, "source_subframe": bpy.context.scene.frame_subframe,
              "subdivision_levels": config["subdivision_levels"], "estimated_vertices": vertices,
              "estimated_triangles": triangles, "estimated_working_bytes": working_bytes,
              "fields": [{"slot": slot, **{k: v for k, v in field.items() if k != "image_pointer"}}
                         for slot, field in fields.items() if field],
              "method": "Object-space scalar height sampled at simple-subdivision vertices",
              "accuracy": "Finite tessellation approximation; no adaptive subdivision, residual bump or renderer-equivalence claim"}
    return evaluated, materials, fields, report


def inspect(source, *, settings=None):
    config = globals()["settings"](settings)
    try:
        return _plan(source, config)[3]
    except GeometryRefusal as exc:
        return {"schema": SCHEMA_VERSION, "source": source.name, "status": "REFUSED", "reason": str(exc)}


def _pixels(field):
    image = bpy.data.images.get(field["image"])
    if image is None or image.as_pointer() != field["image_pointer"]:
        raise GeometryRefusal("Height image identity changed during capture")
    w, h = map(int, image.size)
    buffer = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    buffer = buffer.reshape(h, w, 4)
    if not np.isfinite(buffer).all():
        raise GeometryRefusal("Height image contains nonfinite values")
    channel = field["component"]
    if channel != 3 and image.alpha_mode != "CHANNEL_PACKED" and not np.all(buffer[..., 3] == 1):
        raise GeometryRefusal("Color height with nonopaque alpha requires Channel Packed alpha mode")
    if channel is None:
        if not (np.array_equal(buffer[..., 0], buffer[..., 1]) and np.array_equal(buffer[..., 1], buffer[..., 2])):
            raise GeometryRefusal("Direct image Color height requires grayscale RGB; select an explicit RGB channel for colored images")
        channel = 0
    return buffer[..., channel].copy()


def _sample(pixels, uv, field):
    if not np.isfinite(uv).all():
        raise GeometryRefusal("Height UVs contain nonfinite coordinates")
    h, w = pixels.shape
    # Bound before integer conversion, including maliciously huge finite UVs.
    uv = np.mod(uv, 1.0) if field["extension"] == "REPEAT" else np.clip(uv, 0.0, 1.0)
    x, y = uv[:, 0] * w, uv[:, 1] * h
    def lookup(xi, yi):
        if field["extension"] == "REPEAT":
            xi, yi = np.mod(xi, w), np.mod(yi, h)
        else:
            xi, yi = np.clip(xi, 0, w - 1), np.clip(yi, 0, h - 1)
        return pixels[yi, xi]
    if field["interpolation"] == "Closest":
        return lookup(np.floor(x).astype(np.int64), np.floor(y).astype(np.int64))
    x, y = x - .5, y - .5
    x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x - x0, y - y0
    return ((1 - fx) * (1 - fy) * lookup(x0, y0) + fx * (1 - fy) * lookup(x0 + 1, y0)
            + (1 - fx) * fy * lookup(x0, y0 + 1) + fx * fy * lookup(x0 + 1, y0 + 1))


@dataclass
class PreparedGeometry:
    source: object
    object: object
    metadata: dict
    owned: dict

    def close(self):
        for kind in ("objects", "materials", "meshes"):
            for item in reversed(self.owned.get(kind, [])):
                try:
                    getattr(bpy.data, kind).remove(item, do_unlink=True)
                except ReferenceError:
                    pass
            self.owned[kind] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def prepare(source, *, settings=None, run_control=None):
    config = globals()["settings"](settings)
    _boundary(run_control, "admission", source=source.name)
    evaluated, materials, fields, report = _plan(source, config)
    result = PreparedGeometry(source, source, report, {"objects": [], "meshes": [], "materials": []})
    if report["status"] == "NO_DISPLACEMENT":
        return result
    # Persistent datablock creation can trigger depsgraph reevaluation. Retain
    # ordinary values now, never a late read through an evaluated ID pointer.
    evaluated_matrix = evaluated.matrix_world.copy()
    evaluated_color = tuple(evaluated.color)
    evaluated_pass_index = evaluated.pass_index
    try:
        pixel_cache = {}
        for field in fields.values():
            if field and field["kind"] == "IMAGE":
                _boundary(run_control, "height_image", source=source.name, image=field["image"])
                key = (field["image_pointer"], field["component"])
                if key not in pixel_cache:
                    pixel_cache[key] = _pixels(field)
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
        result.owned["meshes"].append(mesh)
        # Explicitly convert evaluated material IDs to persistent originals.
        indices = [face.material_index for face in mesh.polygons]
        mesh.materials.clear()
        for material in materials:
            mesh.materials.append(material)
        for face, index in zip(mesh.polygons, indices):
            face.material_index = index
        _boundary(run_control, "subdivision", source=source.name)
        if config["subdivision_levels"]:
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=2 ** config["subdivision_levels"] - 1, use_grid_fill=True)
                bm.to_mesh(mesh)
            finally:
                bm.free()
        mesh.update()
        mesh.calc_loop_triangles()
        if len(mesh.vertices) > report["estimated_vertices"] or len(mesh.loop_triangles) > report["estimated_triangles"]:
            raise GeometryRefusal("Subdivision exceeded its admitted topology bound")
        count = len(mesh.loops)
        corner_displacements = np.zeros((count, 3), dtype=np.float64)
        normal = np.empty(count * 3, dtype=np.float32)
        mesh.corner_normals.foreach_get("vector", normal)
        normal = normal.reshape(-1, 3).astype(np.float64)
        if not np.isfinite(normal).all():
            raise GeometryRefusal("Mesh corner normals are nonfinite")
        for slot, field in fields.items():
            _boundary(run_control, "sample_slot", source=source.name, slot=slot)
            if field is None:
                continue
            indices = np.array([index for face in mesh.polygons if face.material_index == slot for index in face.loop_indices], dtype=np.int64)
            if field["kind"] == "CONSTANT":
                height = field["value"]
            else:
                uv = np.empty(count * 2, dtype=np.float32)
                mesh.uv_layers[field["uv_layer"]].data.foreach_get("uv", uv)
                height = _sample(pixel_cache[(field["image_pointer"], field["component"])], uv.reshape(-1, 2)[indices], field)
            distances = (height - field["midlevel"]) * field["scale"]
            corner_displacements[indices] = normal[indices] * np.asarray(distances)[..., None]
        vertex_indices = np.empty(count, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", vertex_indices)
        displacement = np.zeros((len(mesh.vertices), 3), dtype=np.float64)
        seen = np.zeros(len(mesh.vertices), dtype=bool)
        # Keep the first exact corner value. No averaging across UV, material or
        # sharp-normal discontinuities; that would silently change the surface.
        for index, vertex in enumerate(vertex_indices):
            if index % 8192 == 0:
                _boundary(run_control, "verify_seams", source=source.name, corners=index)
            if seen[vertex] and np.max(np.abs(displacement[vertex] - corner_displacements[index])) > config["seam_tolerance"]:
                raise GeometryRefusal("Shared vertex %d has conflicting displaced corners (UV/material seam or sharp normal); split the seam explicitly" % vertex)
            displacement[vertex] = corner_displacements[index] if not seen[vertex] else displacement[vertex]
            seen[vertex] = True
        coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coordinates)
        output = coordinates.reshape(-1, 3).astype(np.float64) + displacement
        if not np.isfinite(output).all() or np.any(np.abs(output) > np.finfo(np.float32).max):
            raise GeometryRefusal("Displaced coordinates are not finite float32 geometry")
        mesh.vertices.foreach_set("co", output.astype(np.float32).ravel())
        mesh.update()
        # Strip the captured root only on private materials, after all numerical
        # checks have passed. Surface resources and UV layers remain unchanged.
        for slot, material in enumerate(materials):
            if material is None:
                continue
            private = material.copy()
            result.owned["materials"].append(private)
            mesh.materials[slot] = private
            field = fields.get(slot)
            if field:
                output_node = private.node_tree.nodes[field["output"]]
                for link in list(output_node.inputs["Displacement"].links):
                    private.node_tree.links.remove(link)
        obj = source.copy()
        result.owned["objects"].append(obj)
        obj.data = mesh
        obj.name = "IG_GEOMETRY_" + source.name
        obj.parent = None
        obj.animation_data_clear()
        obj.constraints.clear()
        obj.modifiers.clear()
        obj.matrix_world = evaluated_matrix
        obj.color = evaluated_color
        obj.pass_index = evaluated_pass_index
        for slot in obj.material_slots:
            slot.link = "DATA"
        obj.hide_render = False
        obj.hide_viewport = False
        obj["import_glue_geometry"] = True
        obj["import_glue_source"] = source.name
        bpy.context.collection.objects.link(obj)
        result.object = obj
        result.metadata.update(status="APPLIED", output=obj.name, vertices=len(mesh.vertices),
                               triangles=len(mesh.loop_triangles), uv_layers=[uv.name for uv in mesh.uv_layers],
                               maximum_local_displacement=float(np.max(np.linalg.norm(displacement, axis=1))),
                               seam_policy="Refuse conflicting corner displacement vectors; no averaging",
                               source_matrix_world=[list(row) for row in evaluated_matrix])
        _boundary(run_control, "complete", source=source.name, vertices=len(mesh.vertices))
        return result
    except BaseException:
        result.close()
        raise
