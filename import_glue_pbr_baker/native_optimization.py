"""Conservative private-graph simplification and sampling identities."""
from __future__ import annotations

import hashlib
import json
import numpy as np

from . import native_graph


def field_key(material, field, *, source_fingerprint, slot, uv_name, resolution, quality):
    _tree, target, _parents = native_graph.locate(material, field)
    links = list(target.links)
    if len(links) != 1:
        raise ValueError("Field identity requires one original source link")
    link = links[0]
    identity = {"schema": 2, "source": source_fingerprint, "material_slot": slot,
                "group_path": field["group_path"], "source_node": link.from_node.name,
                "source_socket": link.from_socket.identifier, "receiver_type": target.type,
                "uv_layer": uv_name, "resolution": resolution, "quality": quality}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def uv_triangles(mesh, uv_name, slot):
    """Connected UV islands respecting mesh edges, material slots and seams."""
    mesh.calc_loop_triangles()
    uv = mesh.uv_layers[uv_name].data
    faces = [p for p in mesh.polygons if p.material_index == slot]
    parents = {p.index: p.index for p in faces}
    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    edges = {}
    for face in faces:
        loops = list(face.loop_indices)
        for first, second in zip(loops, loops[1:]+loops[:1]):
            a, b = mesh.loops[first].vertex_index, mesh.loops[second].vertex_index
            ua, ub = tuple(uv[first].uv), tuple(uv[second].uv)
            # Exact UV equality is deliberate: even a small author seam must
            # not silently share padding with its neighboring face.
            key = (a,b,ua,ub) if a < b else (b,a,ub,ua)
            if key in edges:
                parents[root(face.index)] = root(edges[key])
            else:
                edges[key] = face.index
    ids, triangles = [], []
    for triangle in mesh.loop_triangles:
        if triangle.polygon_index in parents:
            triangles.append([tuple(uv[loop].uv) for loop in triangle.loops])
            ids.append(root(triangle.polygon_index))
    return np.asarray(triangles, dtype=np.float64).reshape(-1,3,2), ids


def statistics(materials):
    nodes = groups = 0
    images = {}
    for material in materials:
        if material is None or not material.use_nodes:
            continue
        stack = [material.node_tree]
        while stack:
            tree = stack.pop()
            nodes += len(tree.nodes)
            for node in tree.nodes:
                image = getattr(node, "image", None)
                if image is not None:
                    images[image.as_pointer()] = image
                if node.type == "GROUP" and node.node_tree:
                    groups += 1
                    stack.append(node.node_tree)
    return {"node_instances": nodes, "group_instances": groups, "unique_images": len(images),
            "image_buffer_bytes": sum(int(image.size[0])*int(image.size[1])*4*(4 if image.is_float else 1)
                                      for image in images.values())}


def prune_private(materials):
    """Remove physically unreachable nodes from private graphs only.

    Traversing all connected inputs (including inactive Mix branches) is
    conservative. Group outputs are preserved, so no public interface changes.
    """
    before = statistics(materials)
    for material in materials:
        if material is None or not material.use_nodes:
            continue
        trees = [(material.node_tree, True)]
        while trees:
            tree, top = trees.pop()
            roots = ([node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"] if top else
                     [node for node in tree.nodes if node.type == "GROUP_OUTPUT"])
            keep, pending = set(), [node for node in roots if node is not None]
            while pending:
                node = pending.pop()
                pointer = node.as_pointer()
                if pointer in keep:
                    continue
                keep.add(pointer)
                pending.extend(link.from_node for socket in node.inputs for link in socket.links)
            for node in list(tree.nodes):
                if node.as_pointer() not in keep:
                    tree.nodes.remove(node)
                elif node.type == "GROUP" and node.node_tree:
                    trees.append((node.node_tree, False))
    after = statistics(materials)
    return {"before": before, "after": after,
            "removed_node_instances": before["node_instances"]-after["node_instances"],
            "render_speed_claim": False, "note": "Graph/image counts measured; render and device memory speedups require separate benchmarks."}
