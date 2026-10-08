"""Conservative field planning for a preserved native Cycles material graph.

Plans identify group *instances*, not just shared group definitions. Unknown
data nodes stay native; identity/instancer contexts that a copy changes refuse.
"""
from __future__ import annotations

import bpy

from . import shader_graph, shader_resources

STATIC = frozenset({
    "ShaderNodeMath", "ShaderNodeVectorMath", "ShaderNodeMapRange", "ShaderNodeClamp",
    "ShaderNodeMix", "ShaderNodeMixRGB", "ShaderNodeValToRGB", "ShaderNodeRGBCurve",
    "ShaderNodeVectorCurve", "ShaderNodeFloatCurve", "ShaderNodeSeparateColor",
    "ShaderNodeCombineColor", "ShaderNodeSeparateRGB", "ShaderNodeCombineRGB",
    "ShaderNodeSeparateXYZ", "ShaderNodeCombineXYZ", "ShaderNodeRGBToBW",
    "ShaderNodeInvert", "ShaderNodeGamma", "ShaderNodeBrightContrast",
    "ShaderNodeHueSaturation", "ShaderNodeMapping", "ShaderNodeVectorRotate",
    "ShaderNodeValue", "ShaderNodeRGB", "ShaderNodeTexNoise", "ShaderNodeTexVoronoi",
    "ShaderNodeTexWave", "ShaderNodeTexMagic", "ShaderNodeTexBrick", "ShaderNodeTexChecker",
    "ShaderNodeTexGradient", "ShaderNodeTexWhiteNoise", "ShaderNodeTexGabor",
})
COORDINATES = frozenset({"ShaderNodeTexCoord", "ShaderNodeUVMap", "ShaderNodeAttribute",
                         "ShaderNodeVertexColor", "ShaderNodeNewGeometry"})


def _socket(sockets, reference):
    return next((item for item in sockets if item.identifier == reference.identifier), None)


def _active_output(tree):
    return next((node for node in tree.nodes if node.type == "GROUP_OUTPUT" and node.is_active_output), None)


def classify_input(socket, context=()):
    """Prove a spatial, nontrivial data expression; never infer static from unknown."""
    stack, seen, spatial, work = [(socket, tuple(context))], set(), False, False
    while stack:
        current, parents = stack.pop()
        if current is None:
            return False, "unresolved group interface"
        key = (current.as_pointer(), tuple(n.as_pointer() for n in parents))
        if key in seen:
            continue
        seen.add(key)
        if getattr(current.id_data, "animation_data", None) is not None:
            return False, "animated data expression retained"
        if not current.is_output:
            stack.extend((link.from_socket, parents) for link in shader_graph.active_links(current))
            continue
        node, kind = current.node, current.node.bl_idname
        if node.mute or node.type == "REROUTE":
            stack.extend((item, parents) for item in shader_graph._inputs(node, current, parents))
            continue
        if node.type == "GROUP":
            if node.node_tree is None:
                return False, "missing group"
            output = _active_output(node.node_tree)
            if output is None:
                return False, "missing active group output"
            stack.append((_socket(output.inputs, current), parents + (node,)))
            continue
        if node.type == "GROUP_INPUT":
            if not parents:
                return False, "unbound group input"
            stack.append((_socket(parents[-1].inputs, current), parents[:-1]))
            continue
        if kind == "ShaderNodeTexImage":
            if node.image is None or node.image.source not in {"FILE", "GENERATED", "TILED"}:
                return False, "image is not a captured static image"
            spatial = True
        elif kind in COORDINATES:
            if kind == "ShaderNodeTexCoord" and (current.name not in {"UV", "Generated", "Object"}
                                                   or node.object is not None):
                return False, "external/view coordinate context retained"
            if kind == "ShaderNodeNewGeometry" and current.name != "Position":
                return False, "geometry derivative/directional context retained"
            if kind == "ShaderNodeAttribute" and node.attribute_type != "GEOMETRY":
                return False, "non-geometry attribute context retained"
            spatial = True
        elif kind in STATIC:
            if kind not in {"ShaderNodeValue", "ShaderNodeRGB"}:
                work = True
            if kind.startswith("ShaderNodeTex"):
                spatial = True
        else:
            return False, "native data node retained: " + kind
        stack.extend((item, parents) for item in shader_graph._inputs(node, current, parents))
    if not spatial:
        return False, "constant expression retained without resampling"
    if not work:
        return False, "existing texture/attribute/coordinate retained without resampling"
    return True, "proven static spatial expression; finite UV sampling is approximate"


def inspect_material(material):
    fields, blockers, seen, generated = [], [], set(), False
    resources, resource_seen = [], set()

    def visit(node, output, context, include_fields=True):
        nonlocal generated
        kind = node.bl_idname
        path = [n.name for n in context]
        location = "/".join([material.name, *path, node.name])
        image = getattr(node, "image", None)
        if image is not None and image.source == "MOVIE":
            blockers.append({"code": "MOVIE_EXCLUDED", "path": location,
                             "reason": "Required movie textures are excluded by this output policy."})
        if kind in {"ShaderNodeScript", "ShaderNodeTexIES"} and getattr(node, "mode", "") == "EXTERNAL":
            proof = shader_resources.inspect_node(node)
            if not proof["supported"]:
                blockers.append({"code": "UNCAPTURED_EXTERNAL_SHADER_RESOURCE", "path": location,
                                 "reason": proof["reason"]})
            elif (tuple(path),node.name) not in resource_seen:
                resource_seen.add((tuple(path),node.name))
                resources.append({"group_path": path,"node": node.name,"kind": proof["kind"]})
        if kind == "ShaderNodeScript" and getattr(node,"mode","") == "INTERNAL":
            try:
                shader_resources.validate_internal_node(node)
            except shader_resources.ResourceError as exc:
                blockers.append({"code":"UNVERIFIED_INTERNAL_OSL", "path":location, "reason":str(exc)})
        if kind == "ShaderNodeObjectInfo" and output.name == "Random":
            blockers.append({"code": "OBJECT_RANDOM_CONTEXT", "path": location,
                             "reason": "A copied object changes Random; no equivalent captured field is available."})
        if getattr(node, "from_instancer", False) or (kind == "ShaderNodeAttribute"
                and getattr(node, "attribute_type", "GEOMETRY") in {"INSTANCER", "VIEW_LAYER"}):
            blockers.append({"code": "INSTANCE_CONTEXT", "path": location,
                             "reason": "The native copy cannot prove this instance/view-layer context unchanged."})
        if kind == "ShaderNodeTexCoord" and output.name == "Generated":
            generated = True
        if kind.startswith("ShaderNodeTex") and kind not in {"ShaderNodeTexImage", "ShaderNodeTexEnvironment"}:
            vector = node.inputs.get("Vector")
            if vector is not None and not shader_graph.active_links(vector):
                generated = True
        if not include_fields or output.type != "SHADER" or node.type in {"GROUP", "GROUP_INPUT", "REROUTE"}:
            return
        for index, target in enumerate(node.inputs):
            if not target.enabled or target.type not in {"VALUE", "RGBA", "VECTOR"}:
                continue
            identity = (tuple(path), node.name, target.identifier)
            if identity in seen:
                continue
            seen.add(identity)
            linked = bool(shader_graph.active_links(target))
            eligible, reason = classify_input(target, context) if linked else (False, "unlinked native constant")
            fields.append({"group_path": path, "node": node.name, "input": target.name,
                           "input_identifier": target.identifier, "input_index": index,
                           "socket_type": target.type, "eligible": eligible, "reason": reason,
                           "action": "BAKE_STATIC_FIELD" if eligible else "RETAIN_NATIVE"})

    dependency = shader_graph.material_dependencies(material, visit=visit)
    output = material.node_tree.get_output_node("CYCLES")
    # Native libraries preserve engine-specific output roots as well. Walk
    # their resources conservatively; only Cycles fields are candidates for
    # sampling. This avoids pruning an Eevee root while silently clearing its
    # images or leaving an excluded movie reachable through that root.
    pending = [(socket,()) for node in material.node_tree.nodes
               if node.type == "OUTPUT_MATERIAL" and node != output for socket in node.inputs]
    alternate_seen = set()
    while pending:
        current, parents = pending.pop()
        key = (current.as_pointer(),tuple(node.as_pointer() for node in parents))
        if key in alternate_seen:
            continue
        alternate_seen.add(key)
        if not current.is_output:
            pending.extend((link.from_socket,parents) for link in shader_graph.active_links(current))
            continue
        node = current.node
        visit(node,current,parents,False)
        image = getattr(node,"image",None)
        if image is not None:
            dependency["images"].add(image.as_pointer())
        if node.type == "GROUP" and node.node_tree:
            inner = _active_output(node.node_tree)
            match = _socket(inner.inputs,current) if inner else None
            if match is not None:
                pending.append((match,parents+(node,)))
        elif node.type == "GROUP_INPUT" and parents:
            match = _socket(parents[-1].inputs,current)
            if match is not None:
                pending.append((match,parents[:-1]))
        else:
            pending.extend((socket,parents) for socket in shader_graph._inputs(node,current,parents))
    domains = [name for name in ("Volume", "Displacement")
               if output is not None and output.inputs.get(name) is not None
               and shader_graph.active_links(output.inputs[name])]
    if domains:
        # A surface UV atlas cannot reproduce a 3D volume field. Displacement
        # also changes coordinate evaluation; keep the whole graph until a
        # domain-aware bake can prove which fields remain surface-invariant.
        for field in fields:
            if field["eligible"]:
                field.update(eligible=False, action="RETAIN_NATIVE",
                             reason="Native %s domain requires contextual evaluation; surface sampling disabled"
                                    % "/".join(domains))
    blockers.extend({"code": item["code"], "path": item.get("node_instance", material.name),
                     "reason": item["message"]} for item in dependency["diagnostics"]
                    if item["severity"] == "ERROR")
    return {"fields": fields, "blockers": blockers, "uses_generated_coordinates": generated,
            "external_resources": resources,
            "retained_domains": domains,
            "dependency": {**dependency, "images": sorted(dependency["images"])}}


def clone_material(material, name, materials, groups, texts=None):
    """Deep private material graph; shared group definitions become per-instance copies."""
    copy = material.copy()
    copy.name = name
    copy.use_fake_user = False
    materials.append(copy)
    if not copy.use_nodes or copy.node_tree is None:
        return copy
    stack, text_copies = [(copy.node_tree, frozenset())], {}
    while stack:
        tree, ancestors = stack.pop()
        for node in tree.nodes:
            for attribute in ("script", "ies"):
                text = getattr(node, attribute, None)
                if texts is not None and isinstance(text, bpy.types.Text):
                    pointer = text.as_pointer()
                    if pointer not in text_copies:
                        text_copies[pointer] = text.copy()
                        text_copies[pointer].use_fake_user = False
                        texts.append(text_copies[pointer])
                    setattr(node, attribute, text_copies[pointer])
            original = getattr(node, "node_tree", None)
            if original is None:
                continue
            pointer = original.as_pointer()
            if pointer in ancestors:
                raise ValueError("Recursive native group cannot be copied")
            child = original.copy()
            child.name = "__IG_NATIVE_" + original.name
            child.use_fake_user = False
            groups.append(child)
            node.node_tree = child
            stack.append((child, ancestors | {pointer}))
    return copy


def locate(material, field):
    tree, parents = material.node_tree, []
    for name in field["group_path"]:
        node = tree.nodes[name]
        parents.append((tree, node))
        tree = node.node_tree
    node = tree.nodes[field["node"]]
    target = next((socket for socket in node.inputs
                   if socket.identifier == field["input_identifier"]), None)
    if target is None:
        raise ValueError("Native input interface changed while planning")
    return tree, target, parents


def expose_field(material, field):
    """Expose receiver-typed RGB components at the root of a private probe graph."""
    tree, target, parents = locate(material, field)
    links = shader_graph.active_links(target)
    if len(links) != 1:
        raise ValueError("A field probe requires exactly one original input link")
    source = links[0].from_socket
    if target.type == "VALUE":
        scalar = tree.nodes.new("ShaderNodeMath")
        scalar.operation = "ADD"
        scalar.inputs[1].default_value = 0.0
        tree.links.new(source, scalar.inputs[0])
        source = scalar.outputs[0]
    vector = tree.nodes.new("ShaderNodeVectorMath")
    vector.operation = "ADD"
    vector.inputs[1].default_value = (0, 0, 0)
    tree.links.new(source, vector.inputs[0])
    source = vector.outputs["Vector"]
    for parent_tree, group_node in reversed(parents):
        interface = tree.interface.new_socket(name="__IG_FIELD", in_out="OUTPUT", socket_type="NodeSocketVector")
        output = _active_output(tree)
        inner = next(s for s in output.inputs if s.identifier == interface.identifier)
        tree.links.new(source, inner)
        source = next(s for s in group_node.outputs if s.identifier == interface.identifier)
        tree = parent_tree
    return source


def replace_field(material, field, image, uv_name):
    tree, target, _parents = locate(material, field)
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.name = "IG Baked " + field["input"]
    texture.label = "Sampled static field (float EXR)"
    texture.image = image
    texture.interpolation = "Linear"
    texture.extension = "EXTEND"
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = uv_name
    tree.links.new(uv.outputs["UV"], texture.inputs["Vector"])
    source = texture.outputs["Color"]
    if target.type == "VALUE":
        split = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(source, split.inputs[0])
        source = split.outputs["X"]
    tree.links.new(source, target)
