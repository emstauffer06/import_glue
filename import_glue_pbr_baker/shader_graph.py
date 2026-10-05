"""Cycles material dependencies, preserving each nested group instance.

The walker follows socket dependencies, rather than scanning node trees. It is
iterative, has no depth limit, and never changes the material or its images.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import bpy


# Named incompatibilities only: registered custom groups and other shader
# nodes retain their existing traversal instead of facing a broad whitelist.
_CYCLES_UNSUPPORTED_NODES = {
    "ShaderNodeShaderToRGB": "Shader to RGB is only supported by Eevee and cannot be evaluated by Cycles.",
}


def _pointer(value: Any) -> int:
    return int(value.as_pointer())


def material_usage(obj: Any) -> dict:
    """Material slots the faces of ``obj`` use, on the base AND the evaluated mesh.

    A modifier can put faces on a slot no base-mesh face uses (Solidify's
    material offset) or add a material of its own (Geometry Nodes Set
    Material); Cycles renders and bakes the evaluated mesh.  Returns:

    * ``base``: sorted slot indices the base mesh's polygons use, unclamped;
    * ``evaluated``: {raw slot index: material or None} for the evaluated mesh
      (empty unless ``state`` is EVALUATED).  Slot i resolves as Cycles
      resolves it: the object's slot material, or the evaluated mesh's own
      material for slots the evaluation added, with the index clamped to the
      slot count as Blender clamps it;
    * ``state``: BASE (no modifiers: the base mesh is what renders), EVALUATED,
      NOT_EVALUATED (the object is not in the current view layer's depsgraph,
      so only the base mesh can be read) or ERROR (evaluation raised);
    * ``detail``: why, for NOT_EVALUATED and ERROR.

    Read-only: the temporary evaluated mesh is always cleared.
    """
    base = sorted({int(polygon.material_index) for polygon in obj.data.polygons})
    usage = {"base": base, "evaluated": {}, "state": "BASE", "detail": ""}
    if not len(getattr(obj, "modifiers", ())):
        return usage
    evaluated_obj, mesh = None, None
    try:
        evaluated_obj = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        if not evaluated_obj.is_evaluated:
            usage.update(state="NOT_EVALUATED",
                         detail="not evaluated in the current view layer")
            return usage
        mesh = evaluated_obj.to_mesh()
        slots = [slot.material for slot in obj.material_slots]
        # The evaluated mesh holds evaluated (copy-on-evaluation) materials;
        # callers compare, analyse and report the ORIGINAL datablocks.
        own = [getattr(m, "original", m) if m is not None else None
               for m in mesh.materials] if mesh is not None else []
        count = max(len(slots), len(own))
        raw = sorted({int(polygon.material_index) for polygon in mesh.polygons}) \
            if mesh is not None else []
        for index in raw:
            if count == 0:
                usage["evaluated"][index] = None
                continue
            clamped = min(max(index, 0), count - 1)
            usage["evaluated"][index] = (slots[clamped] if clamped < len(slots)
                                         else own[clamped])
        usage["state"] = "EVALUATED"
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never hidden
        usage.update(state="ERROR", detail="%s: %s" % (type(exc).__name__, exc), evaluated={})
    finally:
        if mesh is not None:
            try:
                evaluated_obj.to_mesh_clear()
            except Exception:
                pass
    return usage


def usage_materials(obj: Any, usage: dict) -> list:
    """Distinct materials of one ``material_usage`` result: base slots in slot
    order, then the evaluated mesh's other materials in slot order."""
    result, seen = [], set()
    indices = set(usage["base"])
    candidates = [slot.material for index, slot in enumerate(obj.material_slots)
                  if index in indices]
    candidates += [usage["evaluated"][index] for index in sorted(usage["evaluated"])]
    for material in candidates:
        if material is not None and _pointer(material) not in seen:
            seen.add(_pointer(material))
            result.append(material)
    return result


def used_materials(objects: Iterable[Any]) -> list:
    """Distinct materials actually assigned to mesh polygons, in encounter order.

    Base-mesh slots first, then every material only the EVALUATED mesh uses
    (``material_usage``): dependency gates must see what Cycles will shade.
    """
    result, seen = [], set()
    for obj in objects:
        if obj is None or obj.type != "MESH":
            continue
        for material in usage_materials(obj, material_usage(obj)):
            if _pointer(material) not in seen:
                seen.add(_pointer(material))
                result.append(material)
    return result


def _matching(sockets: Any, reference: Any) -> Any:
    # Interface identifiers remain stable across renames and duplicate names.
    identifier = reference.identifier
    return next((s for s in sockets if s.identifier == identifier), None)


def _links(socket: Any) -> list:
    return [link for link in socket.links if not getattr(link, "is_muted", False)]


def _static_scalar(socket: Any, context: tuple) -> Any:
    """Prove a scalar constant through reroutes/group inputs; unknown means None.

    Animation anywhere in a participating tree disables folding conservatively.
    Linked math/textures and registered custom nodes are never guessed constant.
    """
    seen = set()
    while socket is not None:
        key = (_pointer(socket), tuple(_pointer(n) for n in context))
        if key in seen or getattr(socket.id_data, "animation_data", None) is not None:
            return None
        seen.add(key)
        if not socket.is_output:
            links = _links(socket)
            if links:
                if len(links) != 1:
                    return None
                socket = links[0].from_socket
                continue
            value = getattr(socket, "default_value", None)
            return float(value) if isinstance(value, (float, int)) else None
        node = socket.node
        if node.bl_idname == "ShaderNodeValue":
            return float(socket.default_value)
        if node.type == "REROUTE":
            socket = node.inputs[0]
        elif node.type == "GROUP_INPUT" and context:
            socket = _matching(context[-1].inputs, socket)
            context = context[:-1]
        else:
            return None
    return None


def static_scalar(socket: Any, context: Iterable[Any] = ()) -> Optional[float]:
    """Read-only public form of the constant folding the walker itself uses."""
    return _static_scalar(socket, tuple(context))


def active_links(socket: Any) -> list:
    """Links the walker follows: muted links never carry a dependency."""
    return _links(socket)


def _inputs(node: Any, output: Any, context: tuple) -> list:
    if node.mute:
        return [link.from_socket for link in node.internal_links
                if link.to_socket == output]
    inputs = [socket for socket in node.inputs if socket.enabled]
    if node.bl_idname in ("ShaderNodeMixShader", "ShaderNodeMixRGB"):
        factor = _static_scalar(node.inputs[0], context)
        if factor == 0.0:
            return [node.inputs[0], node.inputs[1]]
        if factor == 1.0 and (node.bl_idname == "ShaderNodeMixShader"
                             or node.blend_type == "MIX"):
            return [node.inputs[0], node.inputs[2]]
    elif node.bl_idname == "ShaderNodeMix":
        # Blender's Mix has several typed sockets sharing names; use identifiers
        # via the enabled sockets, and only fold its scalar-factor variant.
        factors = [s for s in inputs if s.name == "Factor"]
        a = [s for s in inputs if s.name == "A"]
        b = [s for s in inputs if s.name == "B"]
        if len(factors) == len(a) == len(b) == 1:
            factor = _static_scalar(factors[0], context)
            if factor == 0.0:
                return factors + a
            if factor == 1.0 and (node.data_type != "RGBA" or node.blend_type == "MIX"):
                return factors + b
    return inputs


def material_dependencies(
    material: Any, visit: Optional[Callable[[Any, Any, tuple], None]] = None,
) -> dict:
    """Return images:set[int], diagnostics:list[dict], and node_count:int.

    Diagnostics contain severity (ERROR/WARNING/INFO), code, node, node_instance, and
    message. node_count counts distinct reachable node instances, including
    group boundary nodes. Consumers may reject ERROR diagnostics before baking.

    ``visit(node, output_socket, context)`` is an optional read-only observer
    called once for every required output socket of every non-muted node
    instance the walk reaches (groups and group inputs included), where
    ``context`` is the tuple of enclosing group nodes.  It sees exactly the
    walker's notion of "required"; it must not modify the graph.
    """
    result = {"images": set(), "diagnostics": [], "node_count": 0}
    if material is None or not material.use_nodes or material.node_tree is None:
        return result
    tree = material.node_tree
    output = tree.get_output_node("CYCLES")
    if output is None:
        return result
    visited, active, nodes, emitted = set(), set(), set(), set()

    def diagnostic(node: Any, context: tuple, severity: str, code: str, message: str):
        identity = (_pointer(node), tuple(_pointer(n) for n in context), code)
        if identity not in emitted:
            emitted.add(identity)
            result["diagnostics"].append({
                "severity": severity, "code": code, "node": node.name,
                "node_instance": "/".join([material.name] + [n.name for n in context] + [node.name]),
                "message": message,
            })

    nodes.add((_pointer(output), ()))
    # Exit events distinguish actual back edges from shared DAG dependencies.
    stack = [(False, socket, ()) for socket in output.inputs
             if socket.name in ("Surface", "Volume", "Displacement")]
    while stack:
        exiting, socket, context = stack.pop()
        path = tuple(_pointer(n) for n in context)
        key = (_pointer(socket), path)
        if exiting:
            active.discard(key)
            visited.add(key)
            continue
        if key in active:
            diagnostic(socket.node, context, "ERROR", "GRAPH_CYCLE", "Required shader dependency contains a cycle.")
            continue
        if key in visited:
            continue
        active.add(key)
        stack.append((True, socket, context))
        if not socket.is_output:
            stack.extend((False, link.from_socket, context) for link in _links(socket))
            continue
        node = socket.node
        nodes.add((_pointer(node), path))
        # Blender handles muted groups through their bypass links too.
        if node.mute:
            stack.extend((False, s, context) for s in _inputs(node, socket, context))
            continue
        if visit is not None:
            visit(node, socket, context)
        if node.type == "GROUP" or hasattr(node, "node_tree"):
            group = node.node_tree
            if group is None:
                diagnostic(node, context, "ERROR", "GROUP_MISSING", "Required node group has no node tree.")
                continue
            ancestor_trees = {_pointer(tree)} | {_pointer(n.node_tree) for n in context}
            if _pointer(group) in ancestor_trees:
                diagnostic(node, context, "ERROR", "GRAPH_CYCLE", "Required node group recursively references an ancestor.")
                continue
            group_outputs = [n for n in group.nodes if n.type == "GROUP_OUTPUT" and n.is_active_output]
            if not group_outputs:
                diagnostic(node, context, "ERROR", "GROUP_OUTPUT_MISSING", "Required node group has no active output.")
            for group_output in group_outputs:
                inner = _matching(group_output.inputs, socket)
                if inner is not None:
                    nodes.add((_pointer(group_output), path + (_pointer(node),)))
                    stack.append((False, inner, context + (node,)))
            continue
        if node.type == "GROUP_INPUT":
            if context:
                parent = _matching(context[-1].inputs, socket)
                if parent is not None:
                    stack.append((False, parent, context[:-1]))
            continue
        if node.bl_idname == "NodeUndefined" or node.bl_rna.identifier == "NodeUndefined":
            diagnostic(node, context, "ERROR", "NODE_UNDEFINED", "Required shader node registration is missing.")
            continue
        if node.bl_idname in _CYCLES_UNSUPPORTED_NODES:
            diagnostic(node, context, "ERROR", "CYCLES_UNSUPPORTED_NODE",
                       _CYCLES_UNSUPPORTED_NODES[node.bl_idname])
        if node.bl_idname == "ShaderNodeObjectInfo" and (
            socket.identifier == "Random" or socket.name == "Random"
        ):
            diagnostic(node, context, "WARNING", "OBJECT_RANDOM_CONTEXT",
                       "Identity-dependent Object Info Random values can differ for copied-object baking.")
        if node.bl_idname in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment"):
            if node.image is None:
                diagnostic(node, context, "ERROR", "IMAGE_UNASSIGNED", "Required image texture has no image assigned.")
            else:
                result["images"].add(_pointer(node.image))
        elif node.bl_idname.startswith("ShaderNodeTex") or node.bl_idname in (
            "ShaderNodeMath", "ShaderNodeVectorMath", "ShaderNodeMapRange",
        ):
            diagnostic(node, context, "INFO", "PROCEDURAL_NODE", "Required procedural node will be evaluated by Cycles.")
        stack.extend((False, s, context) for s in _inputs(node, socket, context))
    result["node_count"] = len(nodes)
    return result
