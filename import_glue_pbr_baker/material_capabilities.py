"""Read-only material capability report for one output profile.

``analyze_material`` and ``analyze_objects`` state, for every material the
given objects actually use, whether its required Cycles shading can be carried
by the delivered map set (SUPPORTED), can only be carried by dropping or
freezing an effect (APPROXIMATION_REQUIRED), or cannot be converted from the
inputs present (BLOCKED).  Every non-supported verdict names the offending
node instance, the reason and the next action.

Contract:

* Read-only.  Nothing here creates, renames or relinks nodes, changes image
  paths, writes beside the source, changes bake routing or installs anything.
  ``write_report`` writes only the path it is given.
* "Required" is exactly ``shader_graph.material_dependencies``: unused slots,
  disconnected nodes and statically folded Mix branches never count.
* Existing dependency diagnostics are kept verbatim (code, node,
  node_instance, message) and take precedence: any blocking one makes the
  material BLOCKED whatever else it contains.
* Unknown behaviour is not supported: a required node type without a rule
  below is BLOCKED.  SUPPORTED means every required effect maps onto a
  profile channel; it is not a measured visual match.
* Reports are plain JSON trees.  Blender pointers are used only inside one
  call to recognise shared images and are never written.
"""
from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import warnings
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import bpy

from . import shader_graph
from .precheck import _image_paths, file_looks_valid


SCHEMA_VERSION = 1
RULES_VERSION = 3   # 2: closure-path, topology and instancer rules; unknown Principled
                    #    inputs BLOCKED (REVIEW_A findings 1, 8, 9)
                    # 3: material usage read from the evaluated mesh, modifier-only
                    #    slots BLOCKED (FINAL_REVIEW finding 1); dielectric F0
                    #    tolerance recalibrated on render metrics (finding 2)

SUPPORTED = "SUPPORTED"
APPROXIMATION_REQUIRED = "APPROXIMATION_REQUIRED"
BLOCKED = "BLOCKED"
NONE = "NONE"
_RANK = {NONE: 0, SUPPORTED: 0, APPROXIMATION_REQUIRED: 1, BLOCKED: 2}
_SEVERITY = {NONE: "INFO", APPROXIMATION_REQUIRED: "WARNING", BLOCKED: "ERROR"}

DEFAULT_PROFILE = "PBR_BASE"
PROFILES = {
    "PBR_BASE": {
        "description": (
            "Import Glue's delivered map set: _ALB base colour with straight alpha, "
            "_MET metalness, _RGH roughness and _NOR tangent-space normal (OpenGL), "
            "8-bit PNG, consumed by one metallic-roughness surface."
        ),
        "channels": ["base_color", "alpha", "metallic", "roughness", "normal"],
        # One metallic-roughness surface has a fixed dielectric reflectance.
        "dielectric_f0": 0.04,
        # Float noise only: the maps cannot carry IOR or Specular IOR Level.
        # Calibrated on the M1 RENDER metrics (FINAL_REVIEW finding 2,
        # tests/blender/calibrate_f0_tolerance.py, evidence/final_fixes/
        # f0_calibration): the grazing/highlight local max grows by up to 579
        # per unit of F0 difference at roughness 0.05 (421 at 0.1, 231 at 0.2),
        # so an F0 difference of 0.0001 already reaches the 0.06 limit there,
        # and IOR 1.45 (0.0063) fails at every roughness up to 0.5.  No nonzero
        # tolerance holds for every roughness.  (Rules version 2 allowed 0.01,
        # calibrated on a uniform-world render against the 0.025 surface colour
        # mean instead.)
        "dielectric_f0_tolerance": 1e-6,
    },
}

# ---------------------------------------------------------------------------
# Node rules.  Keys are bl_idname values.  A required node whose type is not
# listed anywhere here is BLOCKED as UNRECOGNIZED_NODE.
# ---------------------------------------------------------------------------

_STRUCTURAL = frozenset({
    "NodeReroute", "NodeFrame", "NodeGroupInput", "NodeGroupOutput", "ShaderNodeGroup",
    "ShaderNodeOutputMaterial", "ShaderNodeOutputAOV", "ShaderNodeOutputLight",
    "ShaderNodeOutputWorld", "ShaderNodeOutputLineStyle",
})

# Evaluated per shading point from stored data (UVs, positions, attributes,
# constants); Cycles evaluates them identically during the bake.
_STATIC = frozenset({
    "ShaderNodeMath", "ShaderNodeVectorMath", "ShaderNodeMapRange", "ShaderNodeClamp",
    "ShaderNodeMix", "ShaderNodeMixRGB", "ShaderNodeValToRGB", "ShaderNodeRGBCurve",
    "ShaderNodeVectorCurve", "ShaderNodeFloatCurve", "ShaderNodeSeparateColor",
    "ShaderNodeCombineColor", "ShaderNodeSeparateRGB", "ShaderNodeCombineRGB",
    "ShaderNodeSeparateHSV", "ShaderNodeCombineHSV", "ShaderNodeSeparateXYZ",
    "ShaderNodeCombineXYZ", "ShaderNodeRGBToBW", "ShaderNodeInvert", "ShaderNodeGamma",
    "ShaderNodeBrightContrast", "ShaderNodeHueSaturation", "ShaderNodeWavelength",
    "ShaderNodeBlackbody", "ShaderNodeMapping", "ShaderNodeVectorRotate", "ShaderNodeNormal",
    "ShaderNodeValue", "ShaderNodeRGB", "ShaderNodeSqueeze", "ShaderNodeRadialTiling",
    "FunctionNodeInputBool", "FunctionNodeInputInt", "FunctionNodeInputVector",
    "FunctionNodeInputMenu", "GeometryNodeMenuSwitch", "NodeImplicitConversion",
    "ShaderNodeNormalMap", "ShaderNodeBump", "ShaderNodeTangent",
    "ShaderNodeVertexColor", "ShaderNodeDisplacement", "ShaderNodeVectorDisplacement",
    "ShaderNodeObjectInfo",   # Random is reported by shader_graph (OBJECT_RANDOM_CONTEXT)
    "ShaderNodeTexNoise", "ShaderNodeTexVoronoi", "ShaderNodeTexWave", "ShaderNodeTexMagic",
    "ShaderNodeTexBrick", "ShaderNodeTexChecker", "ShaderNodeTexGradient",
    "ShaderNodeTexWhiteNoise", "ShaderNodeTexGabor", "ShaderNodeTexMusgrave",
})

# Nodes whose required failure is already a shader_graph diagnostic.
_DEPENDENCY_OWNED = frozenset({"ShaderNodeShaderToRGB"})

_PRINCIPLED = "ShaderNodeBsdfPrincipled"
_TRANSPARENT = "ShaderNodeBsdfTransparent"
_MIX_SHADER = "ShaderNodeMixShader"
_ADD_SHADER = "ShaderNodeAddShader"
_IMAGE_NODES = frozenset({"ShaderNodeTexImage", "ShaderNodeTexEnvironment"})

# Terminal closures with no PBR_BASE equivalent, and what the bake does with them.
_CLOSURES = {
    "ShaderNodeBsdfDiffuse": "Diffuse BSDF has no specular lobe; the bake reads its Color as "
                             "base colour and its Oren-Nayar Roughness as PBR roughness",
    "ShaderNodeBsdfAnisotropic": "Glossy BSDF is baked as metalness 1.0 with its Color as base "
                                 "colour; its anisotropy and distribution are dropped",
    "ShaderNodeBsdfGlossy": "Glossy BSDF is baked as metalness 1.0 with its Color as base "
                            "colour; its distribution is dropped",
    "ShaderNodeBsdfMetallic": "Metallic BSDF (F82/physical conductor) is baked with the bake's "
                              "non-Principled default metalness 0.0",
    "ShaderNodeEmission": "Emission is baked as base colour; self-illumination is not "
                          "represented and the surface becomes lit",
    "ShaderNodeBackground": "Background is a world shader; on a surface it emits light, which "
                            "is not represented",
    "ShaderNodeBsdfGlass": "Glass refraction and transmission are not represented",
    "ShaderNodeBsdfRefraction": "Refraction is not represented",
    "ShaderNodeBsdfTranslucent": "Translucency is not represented",
    "ShaderNodeSubsurfaceScattering": "Subsurface scattering is not represented",
    "ShaderNodeBsdfSheen": "Sheen is not represented",
    "ShaderNodeBsdfVelvet": "Velvet/sheen is not represented",
    "ShaderNodeBsdfToon": "Toon shading is not represented",
    "ShaderNodeBsdfHair": "Hair shading is not represented",
    "ShaderNodeBsdfHairPrincipled": "Principled hair shading is not represented",
    "ShaderNodeBsdfRayPortal": "Ray portals are not represented",
    "ShaderNodeHoldout": "Holdout cuts the surface out of the render; it is baked as alpha 0",
}
_VOLUME_CLOSURES = frozenset({
    "ShaderNodeVolumeAbsorption", "ShaderNodeVolumeScatter", "ShaderNodeVolumePrincipled",
    "ShaderNodeVolumeCoefficients",
})

_VIEW_NODES = {
    "ShaderNodeLayerWeight": "Layer Weight",
    "ShaderNodeFresnel": "Fresnel",
    "ShaderNodeLightPath": "Light Path",
    "ShaderNodeCameraData": "Camera Data",
}
_VIEW_OUTPUTS = {
    "ShaderNodeTexCoord": frozenset({"Camera", "Window", "Reflection"}),
    "ShaderNodeNewGeometry": frozenset({"Incoming", "Backfacing"}),
}
# Outputs that follow mesh topology.  The bake evaluates a copy whose topology
# may differ (triangulation, tri-budget decimation), as for Wireframe below.
_TOPOLOGY_OUTPUTS = {
    "ShaderNodeNewGeometry": frozenset({"Pointiness", "Random Per Island", "Parametric"}),
}
# With From Instancer set these outputs come from the instancing object; the
# bake copy is not an instance.
_INSTANCER_OUTPUTS = {
    "ShaderNodeTexCoord": frozenset({"Generated", "UV"}),
    "ShaderNodeUVMap": frozenset({"UV"}),
}
_DIRECTIONAL_TEXTURES = frozenset({"ShaderNodeTexEnvironment", "ShaderNodeTexSky"})
_CONTEXT_NODES = {
    "ShaderNodeAmbientOcclusion": "it samples surrounding geometry",
    "ShaderNodeBevel": "it ray-traces neighbouring geometry",
    "ShaderNodeRaycast": "it ray-traces the scene",
    "ShaderNodeWireframe": "it follows mesh topology the bake may triangulate or decimate",
    "ShaderNodeLightFalloff": "it is defined for light emission",
    "ShaderNodeParticleInfo": "it reads particle instances",
    "ShaderNodeHairInfo": "it reads hair curves",
    "ShaderNodePointInfo": "it reads point clouds",
    "ShaderNodeVolumeInfo": "it reads volume grids",
    "ShaderNodeUVAlongStroke": "it is defined for Freestyle strokes",
    "ShaderNodeTexIES": "it is defined for lights",
    "ShaderNodeTexPointDensity": "it reads another object's points",
}
_TIME_NODES = {"GeometryNodeInputSceneTime": "Scene Time"}

_BLOCKED_NODES = {
    "ShaderNodeEeveeSpecular": (
        "CYCLES_UNSUPPORTED_NODE",
        "Specular BSDF is EEVEE-only: Cycles renders it black, so a Cycles bake cannot "
        "reproduce the authored surface.",
    ),
    "ShaderNodeScript": (
        "SCRIPT_NODE",
        "An OSL Script node runs arbitrary shader code whose behaviour this report cannot "
        "classify.",
    ),
}
# Present in Blender 5.x shader trees, but no regression evidence shows the bake
# instrumentation (closure probes) evaluates them faithfully.
_UNVERIFIED = frozenset({
    "GeometryNodeRepeatInput", "GeometryNodeRepeatOutput", "NodeClosureInput",
    "NodeClosureOutput", "NodeEvaluateClosure", "NodeCombineBundle", "NodeSeparateBundle",
    "NodeJoinBundle",
})

_SPECIAL = frozenset({
    _PRINCIPLED, _TRANSPARENT, _MIX_SHADER, _ADD_SHADER, "ShaderNodeTexImage",
    "ShaderNodeTexEnvironment", "ShaderNodeTexSky", "ShaderNodeTexCoord",
    "ShaderNodeNewGeometry", "ShaderNodeAttribute", "ShaderNodeVectorTransform",
    "ShaderNodeUVMap",
})

# Principled BSDF: (input, neutral value, effect).  The effect is absent only
# when the input provably equals its neutral value.
_PRINCIPLED_GATES = (
    ("Transmission Weight", 0.0, "transmission"),
    ("Subsurface Weight", 0.0, "subsurface"),
    ("Coat Weight", 0.0, "coat"),
    ("Sheen Weight", 0.0, "sheen"),
    ("Anisotropic", 0.0, "anisotropy"),
    ("Thin Film Thickness", 0.0, "thin_film"),
    ("Diffuse Roughness", 0.0, "diffuse_roughness"),
)
_PRINCIPLED_REPRESENTED = frozenset({"Base Color", "Metallic", "Roughness", "Alpha", "Normal",
                                     "Weight"})
# Only meaningful while their gate is active; the gate is what gets reported.
# Thin Wall: a Cycles render with it enabled and no transmission/subsurface is
# bit-identical to one without (calibration test), so it rides on those gates.
_PRINCIPLED_DEPENDENT = frozenset({
    "Subsurface Radius", "Subsurface Scale", "Subsurface IOR", "Subsurface Anisotropy",
    "Anisotropic Rotation", "Tangent", "Coat Roughness", "Coat IOR", "Coat Tint",
    "Coat Normal", "Sheen Roughness", "Sheen Tint", "Thin Film IOR", "Emission Color",
    "Thin Wall",
})
# Emission (strength x colour) and specular (IOR, level, tint) need combined rules.
_PRINCIPLED_SPECIAL = frozenset({"Emission Strength", "Specular Tint", "Specular IOR Level",
                                 "IOR"})

_ACTIONS = {
    "NODE_UNDEFINED": "Install and enable the add-on that registers this node (its original "
                      "implementation), or rebuild the effect from built-in nodes, then check "
                      "again. Import Glue does not substitute a guessed implementation.",
    "GROUP_MISSING": "Restore or re-link the node group this instance refers to.",
    "GROUP_OUTPUT_MISSING": "Add or activate a Group Output node inside the node group.",
    "GRAPH_CYCLE": "Break the dependency loop in the shader graph.",
    "CYCLES_UNSUPPORTED_NODE": "Replace the node with Cycles-evaluable nodes; the conversion "
                               "bakes with Cycles.",
    "IMAGE_UNASSIGNED": "Assign the intended image to the texture node, or disconnect it.",
    "OBJECT_RANDOM_CONTEXT": "Replace Object Info Random with a stored value or attribute; "
                             "baking on a copied object would change it.",
    "REQUIRED_IMAGE_INVALID": "Restore the original texture at the reported path, or point "
                              "Shared Texture Pool at the folder that holds it. Missing or "
                              "corrupt inputs are never substituted.",
    "UNRECOGNIZED_NODE": "Rebuild the effect from built-in Cycles nodes, or add a capability "
                         "rule backed by reference evidence before converting.",
    "UNVERIFIED_NODE": "Flatten the zone/closure/bundle into ordinary nodes, or add a "
                       "capability rule backed by reference evidence before converting.",
    "SCRIPT_NODE": "Replace the OSL Script node with built-in nodes.",
    "PBR_INPUT_UNREPRESENTED": "Set the input to its neutral value if the effect is not "
                               "wanted, or opt in to approximation knowing the effect will be "
                               "missing from the maps.",
    "CLOSURE_UNREPRESENTED": "Rebuild the surface with a Principled BSDF, or opt in to "
                             "approximation knowing how the bake maps this closure.",
    "CLOSURE_MIX_BLEND": "Drive the Mix Shader with a binary (0/1) mask, mix the Principled "
                         "inputs instead of the closures, or opt in to approximation.",
    "CLOSURE_ADD": "Combine the effects inside one Principled BSDF, or opt in to "
                   "approximation.",
    "VIEW_DEPENDENT_INPUT": "Remove the view-dependent input, or opt in to approximation to "
                            "store one fixed evaluation.",
    "CONTEXT_DEPENDENT_INPUT": "Bake the context-dependent result into an image texture first, "
                               "or opt in to approximation to store one fixed evaluation.",
    "TIME_DEPENDENT_INPUT": "Freeze the value, or opt in to approximation to store one frame.",
    "TIME_DEPENDENT_IMAGE": "Use a still image, or opt in to approximation to store the "
                            "current frame only.",
    "ANIMATED_MATERIAL": "Remove the material animation/drivers, or opt in to approximation "
                         "to store the current frame only.",
    "OUTPUT_UNREPRESENTED": "Disconnect the output if the effect is not wanted, or opt in to "
                            "approximation knowing PBR_BASE has no channel for it.",
    "SURFACE_FALLBACK": "Connect a surface shader to the active Cycles Material Output.",
    "NO_SURFACE_CLOSURE": "Connect a shader to every Mix/Add Shader input and group shader "
                          "output on the surface path, or disconnect the Surface input on "
                          "purpose.",
    "MUTED_CLOSURE": "Unmute the node if its effect is wanted, otherwise remove or disconnect "
                     "it; the bake cannot honour a muted closure.",
    "CLOSURE_PASSTHROUGH": "Connect the shader directly to the next shader socket (take the "
                           "Reroute off the closure path); reroutes on colour or value links "
                           "are fine.",
    "DATA_AS_SHADER": "Feed the value into a shader input (for example Principled Base Color "
                      "or Emission Color) instead of linking it to the shader socket.",
    "PRINCIPLED_INPUT_UNCLASSIFIED": "Add a capability rule for this Principled input (new "
                                     "Blender version?), backed by reference evidence, "
                                     "before converting.",
    "EMPTY_MATERIAL_SLOT": "Assign a material to every slot used by faces.",
    "MODIFIER_MATERIAL_SLOT": "Apply the modifier on a copy of the object (its faces then use "
                              "the slot in the base mesh) and convert the copy, or assign "
                              "the material to base-mesh faces.",
    "MODIFIER_MATERIALS_UNVERIFIED": "Fix or disable the modifier that fails to evaluate, "
                                     "then check again.",
    "MODIFIER_MATERIALS_NOT_EVALUATED": "None required; include the object in the view "
                                        "layer to analyse its evaluated mesh.",
    "MISSING_MATERIAL_SLOT": "Add the material slot the faces refer to, or reassign the faces.",
    "NOTHING_ANALYZED": "Select at least one mesh object with faces.",
    "CUSTOM_GROUP_EVALUATED": "None required; recorded for review.",
    "PROCEDURAL_NODE": "None required; Cycles evaluates the procedural node during the bake.",
}
_CATEGORY_ORDER = {"dependency": 0, "evaluability": 1, "object": 2, "representability": 3,
                   "information": 4}
_CAPABILITY_BLOCKING_DEPENDENCY_CODES = frozenset({"OBJECT_RANDOM_CONTEXT"})


def known_node_types() -> frozenset:
    """Every bl_idname with an explicit rule (the drift test compares Blender's)."""
    return frozenset(
        _STRUCTURAL | _STATIC | _DEPENDENCY_OWNED | _SPECIAL | set(_CLOSURES)
        | _VOLUME_CLOSURES | set(_VIEW_NODES) | set(_CONTEXT_NODES) | set(_TIME_NODES)
        | set(_BLOCKED_NODES) | _UNVERIFIED
    )


def principled_input_names() -> frozenset:
    return frozenset({name for name, _neutral, _effect in _PRINCIPLED_GATES}
                     | _PRINCIPLED_REPRESENTED | _PRINCIPLED_DEPENDENT | _PRINCIPLED_SPECIAL)


def node_capability(bl_idname: str) -> Dict[str, Any]:
    """Static rule for one node type; settings/socket-dependent kinds say so."""
    if bl_idname in _BLOCKED_NODES:
        code, message = _BLOCKED_NODES[bl_idname]
        return {"bl_idname": bl_idname, "kind": "blocked", "impact": BLOCKED, "code": code,
                "message": message}
    if bl_idname in _UNVERIFIED:
        return {"bl_idname": bl_idname, "kind": "unverified", "impact": BLOCKED,
                "code": "UNVERIFIED_NODE"}
    if bl_idname in _CLOSURES or bl_idname in _VOLUME_CLOSURES:
        return {"bl_idname": bl_idname, "kind": "closure", "impact": APPROXIMATION_REQUIRED,
                "code": "CLOSURE_UNREPRESENTED"}
    if bl_idname in _VIEW_NODES:
        return {"bl_idname": bl_idname, "kind": "view", "impact": APPROXIMATION_REQUIRED,
                "code": "VIEW_DEPENDENT_INPUT"}
    if bl_idname in _CONTEXT_NODES:
        return {"bl_idname": bl_idname, "kind": "context", "impact": APPROXIMATION_REQUIRED,
                "code": "CONTEXT_DEPENDENT_INPUT"}
    if bl_idname in _TIME_NODES:
        return {"bl_idname": bl_idname, "kind": "time", "impact": APPROXIMATION_REQUIRED,
                "code": "TIME_DEPENDENT_INPUT"}
    if bl_idname in _SPECIAL:
        return {"bl_idname": bl_idname, "kind": "conditional", "impact": None, "code": None}
    if bl_idname in _STRUCTURAL or bl_idname in _STATIC or bl_idname in _DEPENDENCY_OWNED:
        return {"bl_idname": bl_idname, "kind": "supported", "impact": NONE, "code": None}
    return {"bl_idname": bl_idname, "kind": "unrecognized", "impact": BLOCKED,
            "code": "UNRECOGNIZED_NODE"}


# ---------------------------------------------------------------------------
# Small read-only helpers
# ---------------------------------------------------------------------------

def _require_profile(output_profile: str) -> None:
    if output_profile not in PROFILES:
        raise ValueError("unknown output profile %r; known profiles: %s"
                         % (output_profile, ", ".join(sorted(PROFILES))))


def _library(datablock: Any) -> Optional[str]:
    library = getattr(datablock, "library", None)
    return library.filepath if library is not None else None


def _instance(material: Any, context: tuple, node: Any) -> str:
    return "/".join([material.name] + [n.name for n in context] + [node.name])


def _number(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, 6) if math.isfinite(value) else None


def _fmt(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        return "(%s)" % ", ".join("%g" % v for v in value)
    return "%g" % value


def _animated(datablock: Any) -> bool:
    """Effective animation: an action, drivers or NLA tracks on the datablock."""
    animation = getattr(datablock, "animation_data", None)
    if animation is None:
        return False
    try:
        return bool(animation.action is not None or len(animation.drivers)
                    or len(animation.nla_tracks))
    except Exception:
        return True


def _matching(sockets: Any, reference: Any) -> Any:
    identifier = reference.identifier
    return next((s for s in sockets if s.identifier == identifier), None)


def _animated_paths(datablock: Any) -> Optional[Set[str]]:
    """RNA paths animated by drivers or the active action; None means unknown."""
    animation = getattr(datablock, "animation_data", None)
    if animation is None:
        return set()
    try:
        paths = {curve.data_path for curve in animation.drivers}
        if len(animation.nla_tracks):
            return None
        action = animation.action
        if action is None:
            return paths
        curves = None
        try:
            from bpy_extras import anim_utils
            bag = anim_utils.action_get_channelbag_for_slot(action, animation.action_slot)
            curves = list(bag.fcurves) if bag is not None else []
        except Exception:
            legacy = getattr(action, "fcurves", None)
            curves = list(legacy) if legacy is not None else None
        if curves is None:
            return None
        paths.update(curve.data_path for curve in curves)
        return paths
    except Exception:
        return None


def _socket_animated(socket: Any) -> bool:
    paths = _animated_paths(socket.id_data)
    if paths is None:
        return True
    if not paths:
        return False
    try:
        prefix = socket.path_from_id()
    except Exception:
        return True
    return any(path == prefix or path.startswith(prefix + ".") for path in paths)


def _scalar_state(socket: Any, context: tuple) -> Tuple[Optional[float], str]:
    """(value, how): a provable constant, or None with "linked"/"animated"."""
    value = shader_graph.static_scalar(socket, context)
    if value is not None:
        return value, "constant"
    links = shader_graph.active_links(socket)
    if links:
        return None, "linked"
    # shader_graph refuses to fold anything in a tree carrying animation_data;
    # an unlinked default is still constant unless this socket is animated.
    if _socket_animated(socket):
        return None, "animated"
    try:
        return float(socket.default_value), "constant"
    except (TypeError, ValueError):
        return None, "unreadable"


def _color_state(socket: Any, context: tuple) -> Tuple[Optional[tuple], str]:
    """Colour analogue of _scalar_state through reroutes, group inputs and RGB."""
    seen: Set[Tuple[int, tuple]] = set()
    context = tuple(context)
    while socket is not None:
        key = (socket.as_pointer(), tuple(n.as_pointer() for n in context))
        if key in seen:
            return None, "linked"
        seen.add(key)
        if not socket.is_output:
            links = shader_graph.active_links(socket)
            if len(links) > 1:
                return None, "linked"
            if links:
                socket = links[0].from_socket
                continue
            if _socket_animated(socket):
                return None, "animated"
            try:
                return tuple(float(v) for v in socket.default_value)[:3], "constant"
            except (TypeError, ValueError):
                return None, "unreadable"   # not a colour value: cannot prove it neutral
        node = socket.node
        if node.mute:
            return None, "linked"
        if node.bl_idname == "ShaderNodeRGB":
            if _socket_animated(socket):
                return None, "animated"
            return tuple(float(v) for v in socket.default_value)[:3], "constant"
        if node.type == "REROUTE":
            socket = node.inputs[0]
            continue
        if node.type == "GROUP_INPUT" and context:
            socket = _matching(context[-1].inputs, socket)
            context = context[:-1]
            continue
        return None, "linked"
    return None, "linked"


class _Issues:
    """Collect issue dicts with one fixed key set; drop exact duplicates."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self._seen: Set[tuple] = set()

    def add(self, code: str, impact: str, category: str, node: Optional[str],
            node_instance: str, message: str, *, socket: Optional[str] = None,
            effect: Optional[str] = None, severity: Optional[str] = None,
            source: str = "material_capabilities", **extra: Any) -> None:
        key = (code, node_instance, socket, effect, extra.get("image"))
        if key in self._seen:
            return
        self._seen.add(key)
        item = {
            "code": code, "impact": impact, "blocking": impact == BLOCKED,
            "severity": severity or _SEVERITY[impact], "category": category,
            "source": source, "node": node, "node_instance": node_instance,
            "socket": socket, "effect": effect, "message": message,
            "action": _ACTIONS.get(code, "Resolve the reported dependency; conversion is "
                                         "refused until it is fixed."),
        }
        item.update(extra)
        self.items.append(item)

    def sorted(self) -> List[Dict[str, Any]]:
        return sorted(self.items, key=lambda i: (
            _CATEGORY_ORDER.get(i["category"], 9), -_RANK[i["impact"]], i["code"],
            i["node_instance"] or "", i["socket"] or "", i["effect"] or "",
            i.get("image") or ""))


def _status(issues: Iterable[Dict[str, Any]]) -> str:
    worst = max((_RANK[i["impact"]] for i in issues), default=0)
    return {0: SUPPORTED, 1: APPROXIMATION_REQUIRED, 2: BLOCKED}[worst]


# ---------------------------------------------------------------------------
# Classification of required nodes
# ---------------------------------------------------------------------------

def _gate_message(socket_name: str, effect: str, value: Any, how: str, neutral: Any) -> str:
    label = effect.replace("_", " ")
    if how == "constant":
        state = "is %s (neutral %s)" % (_fmt(value), _fmt(neutral))
    elif how == "animated":
        state = "is animated, so it cannot be proven to stay at its neutral value %s" \
                % _fmt(neutral)
    else:
        state = "is driven by the graph, so it cannot be proven to stay at its neutral " \
                "value %s" % _fmt(neutral)
    return ("Principled input '%s' %s: %s has no PBR_BASE channel (base colour, alpha, "
            "metallic, roughness, normal) and would be dropped." % (socket_name, state, label))


def _principled(node: Any, context: tuple, instance: str, issues: _Issues) -> None:
    def flag(socket_name: str, effect: str, message: str) -> None:
        issues.add("PBR_INPUT_UNREPRESENTED", APPROXIMATION_REQUIRED, "representability",
                   node.name, instance, message, socket=socket_name, effect=effect)

    for name, neutral, effect in _PRINCIPLED_GATES:
        socket = node.inputs.get(name)
        if socket is None or not socket.enabled:
            continue
        value, how = _scalar_state(socket, context)
        if value is None or not math.isclose(value, neutral, abs_tol=1e-6):
            flag(name, effect, _gate_message(name, effect, value, how, neutral))

    strength_socket = node.inputs.get("Emission Strength")
    if strength_socket is not None and strength_socket.enabled:
        strength, strength_how = _scalar_state(strength_socket, context)
        if strength is None or not math.isclose(strength, 0.0, abs_tol=1e-6):
            color_socket = node.inputs.get("Emission Color")
            color, color_how = (_color_state(color_socket, context) if color_socket is not None
                                else ((0.0, 0.0, 0.0), "constant"))
            if color is None or max(color) > 1e-6:
                flag("Emission Strength", "emission", (
                    "Principled emission is active (strength %s, colour %s): emission has no "
                    "PBR_BASE channel and would be dropped."
                    % (_fmt(strength) if strength is not None else strength_how,
                       _fmt(color) if color is not None else color_how)))

    _specular(node, context, instance, issues, flag)

    # Unknown behaviour is BLOCKED: an input this rules version has never seen
    # has no evidence that it is neutral, representable or merely droppable.
    known = principled_input_names()
    for socket in node.inputs:
        if socket.enabled and socket.name not in known:
            issues.add("PRINCIPLED_INPUT_UNCLASSIFIED", BLOCKED, "evaluability", node.name,
                       instance,
                       "Principled input '%s' has no capability rule in rules version %d; its "
                       "effect is unknown, so it is not treated as representable."
                       % (socket.name, RULES_VERSION), socket=socket.name)


def _specular(node: Any, context: tuple, instance: str, issues: _Issues, flag: Any) -> None:
    """Specular tint, and dielectric reflectance against the profile's fixed F0."""
    tint_socket = node.inputs.get("Specular Tint")
    if tint_socket is not None and tint_socket.enabled:
        tint, how = _color_state(tint_socket, context)
        if tint is None or any(abs(c - 1.0) > 1e-6 for c in tint):
            # Tints dielectric reflection and the metallic edge colour alike.
            flag("Specular Tint", "specular", (
                "Principled input 'Specular Tint' is %s (neutral white): tinted specular "
                "reflection has no PBR_BASE channel and would be dropped."
                % (_fmt(tint) if tint is not None else how)))

    metallic_socket = node.inputs.get("Metallic")
    metallic = _scalar_state(metallic_socket, context)[0] if metallic_socket is not None else None
    if metallic is not None and math.isclose(metallic, 1.0, abs_tol=1e-6):
        return  # a pure conductor has no dielectric reflectance to compare
    ior_socket, level_socket = node.inputs.get("IOR"), node.inputs.get("Specular IOR Level")
    if ior_socket is None or level_socket is None:
        return
    ior, ior_how = _scalar_state(ior_socket, context)
    level, level_how = _scalar_state(level_socket, context)
    reference = PROFILES[DEFAULT_PROFILE]["dielectric_f0"]
    tolerance = PROFILES[DEFAULT_PROFILE]["dielectric_f0_tolerance"]
    if ior is None or level is None:
        unknown = "IOR" if ior is None else "Specular IOR Level"
        flag(unknown, "specular", (
            "Principled input '%s' is %s, so the dielectric reflectance cannot be proven to "
            "match the fixed F0 %s of PBR_BASE." % (unknown, ior_how if ior is None else
                                                    level_how, _fmt(reference))))
        return
    # Principled v2: F0 = ((ior - 1) / (ior + 1))^2, scaled by 2 x Specular IOR Level.
    f0 = ((ior - 1.0) / (ior + 1.0)) ** 2 * 2.0 * level if ior > 0 else float("inf")
    delta = abs(f0 - reference)
    socket = "Specular IOR Level" if not math.isclose(level, 0.5, abs_tol=1e-6) else "IOR"
    if delta > tolerance:
        flag(socket, "specular", (
            "Principled dielectric reflectance F0 %.5f (IOR %s, Specular IOR Level %s) differs "
            "from the fixed F0 %s of PBR_BASE (IOR 1.5 with Specular IOR Level 0.5) by %.5f. "
            "The maps cannot carry IOR or Specular IOR Level, so the specular strength would "
            "change; under hard light even a 0.0001 difference reaches the render tolerance "
            "on glossy surfaces." % (f0, _fmt(ior), _fmt(level), _fmt(reference), delta)))


def _mix_shader(node: Any, context: tuple, instance: str, issues: _Issues) -> None:
    factor = shader_graph.static_scalar(node.inputs[0], context)
    if factor == 0.0 or factor == 1.0:
        return  # the walker folded it: only one branch is required
    first, second = (shader_graph.active_links(node.inputs[i]) for i in (1, 2))
    if not first and not second:
        return
    if len(first) == 1 and len(second) == 1 and first[0].from_socket == second[0].from_socket:
        return  # mix(a, a, f) == a
    described = ("constant factor %s" % _fmt(factor) if factor is not None
                 else "a factor driven by the graph or animation")
    partner = "two closures" if first and second else "a closure with an empty (black) input"
    issues.add("CLOSURE_MIX_BLEND", APPROXIMATION_REQUIRED, "representability", node.name,
               instance,
               "Mix Shader blends %s with %s. Cycles renders a blend of the shading lobes, "
               "while PBR_BASE stores one per-pixel blend of their parameters; they agree "
               "only where the factor is exactly 0 or 1 (for example a binary mask)."
               % (partner, described), socket=node.outputs[0].name, effect="closure_mix")


def _add_shader(node: Any, instance: str, issues: _Issues) -> None:
    if all(shader_graph.active_links(node.inputs[i]) for i in (0, 1)):
        issues.add("CLOSURE_ADD", APPROXIMATION_REQUIRED, "representability", node.name,
                   instance, "Add Shader sums two closures; one metallic-roughness surface "
                   "cannot store a sum of lobes.", socket=node.outputs[0].name,
                   effect="closure_add")


def _classify(node: Any, socket: Any, context: tuple, instance: str, first: bool,
              issues: _Issues, image_users: Dict[int, List[Tuple[str, str]]]) -> None:
    idname = node.bl_idname
    if idname == "NodeUndefined" or node.bl_rna.identifier == "NodeUndefined":
        return  # NODE_UNDEFINED comes from shader_graph
    if node.type == "GROUP" or hasattr(node, "node_tree"):
        if idname != "ShaderNodeGroup" and first:
            issues.add("CUSTOM_GROUP_EVALUATED", NONE, "information", node.name, instance,
                       "Registered custom group '%s' is evaluated through its node tree; its "
                       "nodes are classified individually." % idname)
        return
    if idname in _STRUCTURAL or idname in _DEPENDENCY_OWNED or idname in _STATIC:
        return
    if idname in _IMAGE_NODES:
        image = getattr(node, "image", None)
        if image is not None:
            users = image_users.setdefault(image.as_pointer(), [])
            if (instance, node.name) not in users:
                users.append((instance, node.name))
            if first and image.source in {"MOVIE", "SEQUENCE"}:
                issues.add("TIME_DEPENDENT_IMAGE", APPROXIMATION_REQUIRED, "representability",
                           node.name, instance,
                           "Image '%s' is a %s; PBR_BASE maps are static, so the bake stores "
                           "a single frame of it." % (image.name, image.source.lower()),
                           socket=socket.name, effect="time", image=image.name)
        if idname == "ShaderNodeTexImage":
            return
    if idname == _PRINCIPLED:
        if first:
            _principled(node, context, instance, issues)
    elif idname == _TRANSPARENT:
        if first:
            color, how = _color_state(node.inputs["Color"], context)
            if color is None or any(abs(c - 1.0) > 1e-6 for c in color):
                issues.add("CLOSURE_UNREPRESENTED", APPROXIMATION_REQUIRED, "representability",
                           node.name, instance,
                           "Transparent BSDF tints transmitted light (colour %s); PBR_BASE "
                           "alpha is untinted coverage." % (_fmt(color) if color else how),
                           socket=socket.name, effect="tinted_transparency")
    elif idname == _MIX_SHADER:
        if first:
            _mix_shader(node, context, instance, issues)
    elif idname == _ADD_SHADER:
        if first:
            _add_shader(node, instance, issues)
    elif idname in _CLOSURES or idname in _VOLUME_CLOSURES:
        if first:
            note = _CLOSURES.get(idname, "Volume shading is not represented")
            issues.add("CLOSURE_UNREPRESENTED", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance, "%s (%s): %s." % (node.bl_label, idname, note),
                       socket=socket.name,
                       effect="volume" if idname in _VOLUME_CLOSURES else "closure")
    elif idname in _BLOCKED_NODES:
        code, message = _BLOCKED_NODES[idname]
        issues.add(code, BLOCKED, "evaluability", node.name, instance, message)
    elif idname in _UNVERIFIED:
        issues.add("UNVERIFIED_NODE", BLOCKED, "evaluability", node.name, instance,
                   "%s (%s) has no regression evidence that the bake evaluates it correctly."
                   % (node.bl_label, idname))
    elif idname in _VIEW_NODES:
        issues.add("VIEW_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                   node.name, instance,
                   "%s output '%s' depends on the viewing direction or ray type; a bake stores "
                   "one fixed evaluation." % (_VIEW_NODES[idname], socket.name),
                   socket=socket.name, effect="view")
    elif idname in _VIEW_OUTPUTS or idname in _INSTANCER_OUTPUTS:
        if socket.name in _VIEW_OUTPUTS.get(idname, ()):
            issues.add("VIEW_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "%s output '%s' depends on the viewing direction; a bake stores one "
                       "fixed evaluation." % (node.bl_label, socket.name),
                       socket=socket.name, effect="view")
        elif socket.name in _TOPOLOGY_OUTPUTS.get(idname, ()):
            issues.add("CONTEXT_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "%s output '%s' follows mesh topology; the bake evaluates a copy whose "
                       "topology may differ (triangulation, tri-budget decimation) and stores "
                       "one fixed result." % (node.bl_label, socket.name),
                       socket=socket.name, effect="context")
        elif socket.name in _INSTANCER_OUTPUTS.get(idname, ()) \
                and getattr(node, "from_instancer", False):
            issues.add("CONTEXT_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "%s output '%s' is read From Instancer; the bake copy is not an "
                       "instance, so it does not reproduce the instancer's coordinates."
                       % (node.bl_label, socket.name), socket=socket.name, effect="context")
    elif idname in _DIRECTIONAL_TEXTURES:
        vector = node.inputs.get("Vector")
        if first and (vector is None or not vector.enabled
                      or not shader_graph.active_links(vector)):
            issues.add("VIEW_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "%s without a Vector input is sampled by direction; a bake stores one "
                       "fixed evaluation." % node.bl_label, socket=socket.name, effect="view")
    elif idname in _CONTEXT_NODES:
        issues.add("CONTEXT_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                   node.name, instance,
                   "%s output '%s' depends on render context (%s); the bake evaluates it on an "
                   "isolated copy and stores one fixed result."
                   % (node.bl_label, socket.name, _CONTEXT_NODES[idname]),
                   socket=socket.name, effect="context")
    elif idname in _TIME_NODES:
        issues.add("TIME_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                   node.name, instance,
                   "%s makes the material change over time; PBR_BASE maps store one frame."
                   % _TIME_NODES[idname], socket=socket.name, effect="time")
    elif idname == "ShaderNodeAttribute":
        kind = getattr(node, "attribute_type", "GEOMETRY")
        if first and kind in {"INSTANCER", "VIEW_LAYER"}:
            issues.add("CONTEXT_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "Attribute '%s' is read from the %s, which the bake copy does not "
                       "reproduce." % (node.attribute_name, kind.lower().replace("_", " ")),
                       socket=socket.name, effect="context")
    elif idname == "ShaderNodeVectorTransform":
        if first and "CAMERA" in (node.convert_from, node.convert_to):
            issues.add("VIEW_DEPENDENT_INPUT", APPROXIMATION_REQUIRED, "representability",
                       node.name, instance,
                       "Vector Transform converts through camera space, which depends on the "
                       "view; a bake stores one fixed evaluation.",
                       socket=socket.name, effect="view")
    else:
        if first:
            issues.add("UNRECOGNIZED_NODE", BLOCKED, "evaluability", node.name, instance,
                       "Required node type %s ('%s') has no capability rule; its behaviour is "
                       "unknown, so it is not treated as representable."
                       % (idname, node.bl_label))


# ---------------------------------------------------------------------------
# The closure path into Surface
# ---------------------------------------------------------------------------
#
# The 1.3.0 GRAPH_BAKE (engine.instrument_graph_semantic) replaces EVERY node
# that has a linked shader output, except Mix/Add Shader, groups and group
# boundaries, with an emission probe built from that node's own inputs.  It
# does not look at mute, and it has nothing to probe where no closure exists.
# So these shapes, which Cycles renders one way, bake another way (each is
# measured by test_closure_path_rules_match_the_engine_bake):
#
# * a muted closure: Cycles uses only its pass-through links (none for a
#   BSDF: no closure), the bake probes its inputs as if it were active;
# * any other node passing a closure through (a Reroute): replaced by a probe
#   of its own inputs, so the closure behind it is dropped (default grey);
# * a colour/value socket linked into a shader input: Cycles makes it an
#   emission, the bake writes its components into every pass;
# * a linked Surface reached by no live closure (empty Mix/Add Shader, empty
#   group output, a folded Mix choosing an empty or muted input): Cycles
#   renders no closure (black), the bake writes zeros (alpha 0) or probes.
#
# The walk follows exactly the walker's links and Mix Shader folding.

def _passes_closures(node: Any) -> bool:
    """Mix/Add Shader and groups: the bake keeps these and probes inside them."""
    return (node.bl_idname in (_MIX_SHADER, _ADD_SHADER) or node.type == "GROUP"
            or getattr(node, "node_tree", None) is not None)


def _closure_path(material: Any, output: Any, issues: _Issues) -> None:
    surface = output.inputs.get("Surface")
    if surface is None or not shader_graph.active_links(surface):
        return  # SURFACE_FALLBACK: the engine's documented substitution
    live = False
    dead_ends: List[str] = []
    seen: Set[tuple] = set()
    stack: List[Tuple[Any, tuple]] = [(surface, ())]
    while stack:
        socket, context = stack.pop()
        key = (socket.as_pointer(), tuple(n.as_pointer() for n in context))
        if key in seen:
            continue
        seen.add(key)
        if not socket.is_output:
            links = shader_graph.active_links(socket)
            if not links:
                dead_ends.append("%s:%s" % (_instance(material, context, socket.node),
                                            socket.name))
            stack.extend((link.from_socket, context) for link in links)
            continue
        node = socket.node
        instance = _instance(material, context, node)
        if node.bl_idname == "NodeUndefined" or node.bl_rna.identifier == "NodeUndefined":
            live = True  # NODE_UNDEFINED (dependency) already blocks it
            continue
        if socket.type != "SHADER":
            live = True
            issues.add("DATA_AS_SHADER", BLOCKED, "evaluability", node.name, instance,
                       "%s output '%s' (a %s value) is linked straight into a shader input on "
                       "the surface path. Cycles renders it as an emission closure, but the "
                       "GRAPH_BAKE has no closure to probe there and writes the value itself "
                       "into every pass: its components become metalness, roughness and "
                       "alpha." % (node.bl_label, socket.name, socket.type.lower()),
                       socket=socket.name, effect="closure")
            continue
        if node.mute:
            through = [link.from_socket for link in node.internal_links
                       if link.to_socket == socket]
            if not _passes_closures(node):
                issues.add("MUTED_CLOSURE", BLOCKED, "evaluability", node.name, instance,
                           "%s ('%s') is muted on the required surface path. Cycles passes "
                           "through %s, but the GRAPH_BAKE probes every closure node's own "
                           "inputs whether it is muted or not, so it would bake this disabled "
                           "node as if it were active." % (
                               node.bl_label, node.bl_idname,
                               ", ".join("input '%s'" % s.name for s in through)
                               or "nothing (no closure)"),
                           socket=socket.name, effect="closure")
            if not through:
                dead_ends.append(instance)
            stack.extend((s, context) for s in through)
            continue
        if node.type == "GROUP" or getattr(node, "node_tree", None) is not None:
            group = node.node_tree
            outputs = [n for n in group.nodes if n.type == "GROUP_OUTPUT"
                       and n.is_active_output] if group is not None else []
            if not outputs:
                live = True  # GROUP_MISSING / GROUP_OUTPUT_MISSING already block it
            for group_output in outputs:
                inner = _matching(group_output.inputs, socket)
                if inner is not None:
                    stack.append((inner, context + (node,)))
            continue
        if node.type == "GROUP_INPUT":
            parent = _matching(context[-1].inputs, socket) if context else None
            if parent is None:
                dead_ends.append(instance)
            else:
                stack.append((parent, context[:-1]))
            continue
        if node.bl_idname == _MIX_SHADER:
            factor = shader_graph.static_scalar(node.inputs[0], context)
            branches = ([node.inputs[1]] if factor == 0.0 else [node.inputs[2]]
                        if factor == 1.0 else [node.inputs[1], node.inputs[2]])
            stack.extend((s, context) for s in branches)
            continue
        if node.bl_idname == _ADD_SHADER:
            stack.extend((s, context) for s in node.inputs if s.enabled and s.type == "SHADER")
            continue
        carried = [s for s in node.inputs if s.enabled and s.type == "SHADER"]
        if carried:
            issues.add("CLOSURE_PASSTHROUGH", BLOCKED, "evaluability", node.name, instance,
                       "%s ('%s') carries a closure on the surface path. The GRAPH_BAKE "
                       "replaces every node with a shader output except Mix/Add Shader and "
                       "groups by a probe of that node's own inputs, so it writes the probe's "
                       "defaults (measured for a Reroute: colour 0.8, roughness 0.5) and drops "
                       "the closure behind it." % (node.bl_label, node.bl_idname),
                       socket=socket.name, effect="closure")
            stack.extend((s, context) for s in carried)
            continue
        live = True  # a terminal closure: the bake probes it like Cycles shades it
    if not live:
        shown = ", ".join(sorted(set(dead_ends))[:4])
        more = " (+%d more)" % (len(set(dead_ends)) - 4) if len(set(dead_ends)) > 4 else ""
        issues.add("NO_SURFACE_CLOSURE", BLOCKED, "evaluability", output.name,
                   _instance(material, (), output),
                   "The Surface input of the active Cycles material output is linked, but no "
                   "active closure reaches it (dead ends: %s%s). Cycles renders no closure "
                   "here (a black, opaque surface); the bake does not reproduce that: it writes "
                   "zero colour, metalness, roughness and alpha (a transparent mirror), or the "
                   "inputs of muted nodes." % (shown or "none recorded", more),
                   socket="Surface", effect="closure")


# ---------------------------------------------------------------------------
# Public analysis
# ---------------------------------------------------------------------------

def _describe_image(image: Any, users: List[Tuple[str, str]],
                    cache: Dict[str, Tuple[bool, str]]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "name": image.name, "library": _library(image), "source": image.source,
        "filepath": image.filepath, "packed": bool(image.packed_file),
        "colorspace": image.colorspace_settings.name, "paths": [], "valid": True,
        "problems": [], "nodes": [instance for instance, _node in users],
    }
    if entry["packed"] or image.source not in {"FILE", "TILED", "SEQUENCE", "MOVIE"}:
        return entry
    for _authored, path, _tile in _image_paths(image):
        entry["paths"].append(path)
        if path not in cache:
            cache[path] = file_looks_valid(path)
        valid, reason = cache[path]
        if not valid:
            entry["valid"] = False
            entry["problems"].append({"path": path, "reason": reason})
    return entry


def analyze_material(material: Any, output_profile: str = DEFAULT_PROFILE,
                     _cache: Optional[Dict[str, Tuple[bool, str]]] = None) -> Dict[str, Any]:
    """Return JSON-safe capability data without editing the material."""
    _require_profile(output_profile)
    if material is None:
        raise ValueError("analyze_material needs a material")
    cache = {} if _cache is None else _cache
    issues = _Issues()
    visits: List[Tuple[Any, Any, tuple]] = []
    dependency = shader_graph.material_dependencies(
        material, visit=lambda node, socket, context: visits.append((node, socket, context)))

    # 1. Existing dependency diagnostics, verbatim, with their precedence.
    for item in dependency["diagnostics"]:
        blocking = item["severity"] == "ERROR" \
            or item["code"] in _CAPABILITY_BLOCKING_DEPENDENCY_CODES
        impact = BLOCKED if blocking else NONE
        issues.add(item["code"], impact, "dependency" if blocking else "information",
                   item.get("node"), item.get("node_instance") or material.name,
                   item["message"], severity=item["severity"], source="shader_graph")

    # 2. Required nodes, exactly as the walker reached them.
    image_users: Dict[int, List[Tuple[str, str]]] = {}
    seen_instances: Set[tuple] = set()
    trees: Dict[int, Tuple[Any, str]] = {}
    for node, socket, context in visits:
        key = (node.as_pointer(), tuple(n.as_pointer() for n in context))
        first = key not in seen_instances
        seen_instances.add(key)
        instance = _instance(material, context, node)
        tree = node.id_data
        trees.setdefault(tree.as_pointer(), (tree, "/".join(
            [material.name] + [n.name for n in context])))
        _classify(node, socket, context, instance, first, issues, image_users)

    # 3. Required images: descriptive data only, the engine's validity check.
    images = []
    for image in bpy.data.images:
        pointer = image.as_pointer()
        if pointer not in dependency["images"]:
            continue
        users = image_users.get(pointer, [])
        entry = _describe_image(image, users, cache)
        images.append(entry)
        instance, node_name = users[0] if users else (material.name, None)
        for problem in entry["problems"]:
            # Same message shape as engine.require_material_dependencies().
            issues.add("REQUIRED_IMAGE_INVALID", BLOCKED, "dependency", node_name, instance,
                       "%s: %s (%s)" % (image.name, problem["reason"], problem["path"]),
                       image=image.name, path=problem["path"])

    # 4. Output-level effects and substituted surfaces.
    tree = getattr(material, "node_tree", None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        use_nodes = bool(getattr(material, "use_nodes", True))
    output = tree.get_output_node("CYCLES") if (tree is not None and use_nodes) else None
    if output is None:
        issues.add("SURFACE_FALLBACK", APPROXIMATION_REQUIRED, "representability", None,
                   material.name,
                   "The material has no active Cycles material output%s: Cycles renders no "
                   "authored surface here, and the bake substitutes the viewport display "
                   "colour/metallic/roughness instead." % (
                       "" if use_nodes else " (node tree disabled)"),
                   effect="surface_fallback")
    else:
        surface = output.inputs.get("Surface")
        if surface is None or not shader_graph.active_links(surface):
            issues.add("SURFACE_FALLBACK", APPROXIMATION_REQUIRED, "representability",
                       output.name, _instance(material, (), output),
                       "Nothing reaches the Surface input of the active Cycles material output: "
                       "Cycles renders no surface here, and the bake substitutes viewport "
                       "display values instead.", socket="Surface", effect="surface_fallback")
        _closure_path(material, output, issues)
        for socket_name, effect in (("Volume", "volume"), ("Displacement", "displacement")):
            socket = output.inputs.get(socket_name)
            if socket is not None and shader_graph.active_links(socket):
                detail = ""
                if effect == "displacement":
                    detail = " (displacement method %s)" % getattr(
                        material, "displacement_method", "UNKNOWN")
                issues.add("OUTPUT_UNREPRESENTED", APPROXIMATION_REQUIRED, "representability",
                           output.name, _instance(material, (), output),
                           "The Cycles material output's %s input is connected%s; PBR_BASE has "
                           "no %s channel, so the effect would be missing from the maps."
                           % (socket_name, detail, effect), socket=socket_name, effect=effect)

    # 5. Animation anywhere in a required tree freezes to one frame.
    for _pointer, (animated_tree, where) in sorted(trees.items(), key=lambda t: t[1][1]):
        if _animated(animated_tree):
            issues.add("ANIMATED_MATERIAL", APPROXIMATION_REQUIRED, "representability", None,
                       where, "Node tree '%s' carries an action, drivers or NLA tracks; "
                       "PBR_BASE maps store one frame." % animated_tree.name, effect="time")

    ordered = issues.sorted()
    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "material": material.name,
        "library": _library(material),
        "output_profile": output_profile,
        "status": _status(ordered),
        "issues": ordered,
        "required_images": images,
        "node_count": int(dependency["node_count"]),
    }


_USAGE_LABELS = {
    "BASE": "base mesh (no modifiers)%s",
    "EVALUATED": "evaluated%s",
    "NOT_EVALUATED": "base mesh only: %s",
    "ERROR": "base mesh only: modifier evaluation failed (%s)",
}


def _modifier_slots(obj: Any, usage: Dict[str, Any], issues: "_Issues",
                    slots: List[Dict[str, Any]]) -> None:
    """Slots only the evaluated mesh uses, and what that means for conversion.

    The engine routes plan from the BASE mesh's slots (plan_jobs, GRAPH_BAKE's
    per-slot bake materials, CROP atlases), so faces a modifier puts on another
    slot would be baked with an invented default surface (GRAPH_BAKE) or the
    wrong atlas: BLOCKED, whatever the material itself supports.  The hidden
    materials are still analysed by the caller, so their own issues show.
    """
    stack = ", ".join("%s '%s'" % (m.type, m.name) for m in list(obj.modifiers)[:6])
    if len(obj.modifiers) > 6:
        stack += " (+%d more)" % (len(obj.modifiers) - 6)
    if usage["state"] == "NOT_EVALUATED":
        issues.add("MODIFIER_MATERIALS_NOT_EVALUATED", NONE, "information", None, obj.name,
                   "The object is %s, so the materials its modifier stack (%s) puts on faces "
                   "were read from the base mesh only; it is analysed again when it is "
                   "converted from the view layer." % (usage["detail"], stack))
        return
    if usage["state"] == "ERROR":
        issues.add("MODIFIER_MATERIALS_UNVERIFIED", BLOCKED, "object", None, obj.name,
                   "Evaluating the modifier stack (%s) failed (%s), so the materials its "
                   "faces use are unknown." % (stack, usage["detail"]))
        return
    base = set(usage["base"])
    hidden = [(index, material) for index, material in sorted(usage["evaluated"].items())
              if index not in base]
    if not hidden:
        return
    for index, material in hidden:
        slots.append({"slot": index, "material": material.name if material else None,
                      "library": _library(material) if material else None,
                      "status": None if material is not None else BLOCKED,
                      "origin": "modifier", "_material": material})
    named = ", ".join("%d ('%s')" % (index, material.name if material else "<empty>")
                      for index, material in hidden)
    issues.add("MODIFIER_MATERIAL_SLOT", BLOCKED, "object", None, obj.name,
               "The modifier stack (%s) puts faces on material slot(s) %s, which no base-mesh "
               "face uses. The conversion builds its bake and crop materials from the base "
               "mesh's slots, so those faces would get an invented default surface instead "
               "of their material." % (stack, named),
               slots=[index for index, _material in hidden])


def analyze_objects(objects: Iterable[Any], output_profile: str = DEFAULT_PROFILE, *,
                    cache: Optional[Dict[str, Tuple[bool, str]]] = None) -> Dict[str, Any]:
    """Aggregate used material reports for these objects.

    ``cache`` (optional) maps an image file path to its validity verdict.  A
    caller that analyses many objects in one run (resume validation, then the
    preflight) may share one dict so each texture file is validated once; it
    must not be reused once files can have changed.
    """
    _require_profile(output_profile)
    cache = {} if cache is None else cache
    reports: Dict[int, Dict[str, Any]] = {}
    materials: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    for obj in objects:
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        if obj.type != "MESH":
            ignored.append({"object": obj.name, "reason": "not a mesh (%s)" % obj.type})
            continue
        if not len(obj.data.polygons):
            ignored.append({"object": obj.name, "reason": "mesh has no faces"})
            continue
        issues = _Issues()
        slots = []
        # The EVALUATED mesh decides which materials Cycles shades (FINAL_REVIEW
        # finding 1): a modifier can put faces on a slot no base face uses.
        usage = shader_graph.material_usage(obj)
        for index in usage["base"]:
            if index >= len(obj.material_slots):
                issues.add("MISSING_MATERIAL_SLOT", BLOCKED, "object", None, obj.name,
                           "Faces use material slot %d, which does not exist; the conversion "
                           "refuses to invent a default surface." % index, slot=index)
                slots.append({"slot": index, "material": None, "library": None,
                              "status": BLOCKED})
                continue
            material = obj.material_slots[index].material
            if material is None:
                issues.add("EMPTY_MATERIAL_SLOT", BLOCKED, "object", None, obj.name,
                           "Faces use material slot %d, which is empty; the conversion refuses "
                           "to invent a default surface." % index, slot=index)
                slots.append({"slot": index, "material": None, "library": None,
                              "status": BLOCKED})
                continue
            slots.append({"slot": index, "material": material.name,
                          "library": _library(material), "status": None,
                          "_material": material})
        _modifier_slots(obj, usage, issues, slots)
        used = shader_graph.usage_materials(obj, usage)
        for material in used:
            if material.as_pointer() not in reports:
                report = analyze_material(material, output_profile, _cache=cache)
                reports[material.as_pointer()] = report
                materials.append(report)
        for slot in slots:
            material = slot.pop("_material", None)
            if slot["status"] is None and material is not None:
                slot["status"] = reports[material.as_pointer()]["status"]
        object_issues = issues.sorted()
        statuses = [reports[m.as_pointer()]["status"] for m in used]
        rank = max([_RANK[s] for s in statuses] + [_RANK[i["impact"]] for i in object_issues]
                   + [0])
        entries.append({
            "object": obj.name, "library": _library(obj),
            "status": {0: SUPPORTED, 1: APPROXIMATION_REQUIRED, 2: BLOCKED}[rank],
            "materials": [m.name for m in used],
            "material_usage": _USAGE_LABELS[usage["state"]] % usage["detail"],
            "slots": slots, "issues": object_issues,
        })
    report_issues = _Issues()
    if not entries:
        report_issues.add("NOTHING_ANALYZED", BLOCKED, "object", None, "<selection>",
                          "No mesh object with faces was analysed; an empty analysis is not "
                          "a supported result.")
    top = report_issues.sorted()
    rank = max([_RANK[e["status"]] for e in entries] + [_RANK[i["impact"]] for i in top] + [0])
    counts = {SUPPORTED: 0, APPROXIMATION_REQUIRED: 0, BLOCKED: 0}
    for report in materials:
        counts[report["status"]] += 1
    object_counts = {SUPPORTED: 0, APPROXIMATION_REQUIRED: 0, BLOCKED: 0}
    for entry in entries:
        object_counts[entry["status"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "output_profile": output_profile,
        "profile": dict(PROFILES[output_profile], channels=list(
            PROFILES[output_profile]["channels"])),
        "blender": bpy.app.version_string,
        "status": {0: SUPPORTED, 1: APPROXIMATION_REQUIRED, 2: BLOCKED}[rank],
        "counts": counts,                 # materials, by status
        "object_counts": object_counts,   # analysed objects, by status
        "objects": entries,
        "materials": materials,
        "ignored": ignored,
        "issues": top,
    }


# ---------------------------------------------------------------------------
# Execution policy and presentation
# ---------------------------------------------------------------------------

def _reason(issue: Dict[str, Any], material: Optional[str] = None) -> Dict[str, Any]:
    return {"code": issue["code"], "impact": issue["impact"], "source": issue["source"],
            "material": material, "node": issue["node"],
            "node_instance": issue["node_instance"], "socket": issue["socket"],
            "effect": issue["effect"], "message": issue["message"],
            "action": issue["action"]}


# Codes the 1.3.0 engine refuses on every route, unconditionally:
# require_material_dependencies() runs in CROP, CROP_SPLIT, PROXY_BAKE and
# GRAPH_BAKE.  They keep their established failure message and precedence.
DEPENDENCY_GATE_CODES = frozenset({
    "NODE_UNDEFINED", "GROUP_MISSING", "GROUP_OUTPUT_MISSING", "GRAPH_CYCLE",
    "IMAGE_UNASSIGNED", "OBJECT_RANDOM_CONTEXT", "REQUIRED_IMAGE_INVALID",
})
# plan_jobs' empty/missing used-slot rule.  It is CONDITIONAL: it only exists
# while engine.CENSUS_FAIL_ON_NOMAT is True (the legacy configuration bakes a
# neutral grey instead), so the policy defers to it only when told it is on.
SLOT_GATE_CODES = frozenset({"EMPTY_MATERIAL_SLOT", "MISSING_MATERIAL_SLOT"})
ENGINE_ENFORCED_CODES = DEPENDENCY_GATE_CODES | SLOT_GATE_CODES   # informational


def _engine_enforced(reason: Dict[str, Any], slot_gate: bool) -> bool:
    if reason["code"] == "CYCLES_UNSUPPORTED_NODE":
        return reason.get("source") == "shader_graph"   # Shader to RGB, not EEVEE Specular
    return reason["code"] in DEPENDENCY_GATE_CODES or (
        slot_gate and reason["code"] in SLOT_GATE_CODES)


def policy_refusal(decision: Optional[Dict[str, Any]], slot_gate: bool = False
                   ) -> Optional[str]:
    """Failure reason the capability policy must ADD for this decision, or None.

    None when the conversion is allowed, and when an engine gate that is
    certainly active will refuse the object anyway (existing dependency errors
    keep precedence and wording).  ``slot_gate`` states whether the engine's
    empty/missing-slot gate is active in this run (``engine.CENSUS_FAIL_ON_NOMAT``);
    unless the caller says so, the policy refuses by itself.  A missing
    decision is refused (fail closed).  Otherwise a one-line reason naming
    every offending code and node instance (first eight, then a count).
    """
    if decision is None:
        return ("material capability decision missing for this object; conversion "
                "refused (fail closed)")
    if decision["allowed"]:
        return None
    reasons = decision["reasons"]
    if decision["outcome"] == "BLOCKED" and any(
            reason["impact"] == BLOCKED and _engine_enforced(reason, slot_gate)
            for reason in reasons):
        return None
    parts = ["%s%s at %s" % (r["code"], " (%s)" % r["effect"] if r["effect"] else "",
                             r["node_instance"]) for r in reasons]
    more = " (+%d more)" % (len(parts) - 8) if len(parts) > 8 else ""
    return "material capability %s under %s: %s%s" % (
        decision["outcome"], decision["output_profile"], "; ".join(parts[:8]), more)


def _issues_of(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every issue of a material report or an aggregate report, with its material."""
    if "materials" not in report:
        return [_reason(i, report.get("material")) for i in report["issues"]]
    result = [_reason(i) for i in report.get("issues", [])]
    for entry in report.get("objects", []):
        result.extend(_reason(i) for i in entry["issues"])
    for material in report["materials"]:
        result.extend(_reason(i, material["material"]) for i in material["issues"])
    return result


def _decide(status: str, reasons: List[Dict[str, Any]], allow_approximation: bool,
            output_profile: str) -> Dict[str, Any]:
    # Every non-informational reason is kept, BLOCKED first: a BLOCKED record
    # still says what would have been approximated (REVIEW_A finding 3).
    kept = sorted((r for r in reasons if r["impact"] != NONE),
                  key=lambda r: -_RANK[r["impact"]])
    if status == BLOCKED:
        allowed, outcome = False, "BLOCKED"
    elif status == APPROXIMATION_REQUIRED:
        allowed = bool(allow_approximation)
        outcome = "APPROXIMATED" if allowed else "REJECTED_APPROXIMATION"
    else:
        allowed, outcome, kept = True, SUPPORTED, []
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": "ALLOW_APPROXIMATION" if allow_approximation else "REJECT_APPROXIMATION",
        "output_profile": output_profile,
        "status": status,
        "allowed": allowed,
        "outcome": outcome,
        "approximated": outcome == "APPROXIMATED",
        "reasons": kept,
    }


def execution_policy(report: Dict[str, Any], allow_approximation: bool = False
                     ) -> Dict[str, Any]:
    """Decide whether a conversion may proceed.

    Default: only SUPPORTED proceeds.  ``allow_approximation=True`` lets an
    APPROXIMATION_REQUIRED conversion proceed as outcome APPROXIMATED, with every
    approximation reason in the returned record (persist it with the output).
    BLOCKED never proceeds, whatever the opt-in.
    """
    return _decide(report["status"], _issues_of(report), allow_approximation,
                   report["output_profile"])


def object_identity(item: Any) -> Tuple[str, Optional[str]]:
    """(object name, library filepath or None) of an object, report entry or decision.

    A bare name is not an identity: a linked object and a local one may share
    it (REVIEW_A finding 2).
    """
    if isinstance(item, dict):
        return item["object"], item.get("library")
    return item.name, _library(item)


def decision_index(decisions: Iterable[Dict[str, Any]]
                   ) -> Dict[Tuple[str, Optional[str]], Dict[str, Any]]:
    """Look-up table for ``object_decisions()`` keyed by ``object_identity``.

    An identity listed twice is left out, so a look-up fails closed instead of
    returning another object's decision.
    """
    index: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    repeated = set()
    for decision in decisions:
        key = object_identity(decision)
        if key in index:
            repeated.add(key)
        index[key] = decision
    for key in repeated:
        del index[key]
    return index


def decision_for(decisions: Iterable[Dict[str, Any]], obj: Any) -> Optional[Dict[str, Any]]:
    """This object's own decision, or None (callers refuse on None)."""
    return decision_index(decisions).get(object_identity(obj))


def object_decisions(report: Dict[str, Any], allow_approximation: bool = False
                     ) -> List[Dict[str, Any]]:
    """Per-object execution decisions, in ``report["objects"]`` order.

    Each decision names its ``object`` and ``library``; find one with
    ``decision_for`` / ``decision_index``, never by bare name.
    """
    by_key = {(m["material"], m["library"]): m for m in report["materials"]}
    result = []
    for entry in report["objects"]:
        reasons = [_reason(i) for i in entry["issues"]]
        for slot in entry["slots"]:
            material = by_key.get((slot["material"], slot["library"]))
            if material is not None:
                reasons.extend(_reason(i, material["material"]) for i in material["issues"])
        unique, seen = [], set()
        for reason in reasons:
            key = json.dumps(reason, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(reason)
        decision = _decide(entry["status"], unique, allow_approximation,
                           report["output_profile"])
        decision["object"] = entry["object"]
        decision["library"] = entry["library"]
        result.append(decision)
    return result


def preflight_report(objects: Iterable[Any], output_profile: str = DEFAULT_PROFILE,
                     allow_approximation: bool = False, *,
                     cache: Optional[Dict[str, Tuple[bool, str]]] = None) -> Dict[str, Any]:
    """analyze_objects plus the policy verdicts, ready to embed in a manifest."""
    report = analyze_objects(objects, output_profile, cache=cache)
    report["policy"] = execution_policy(report, allow_approximation)
    report["object_decisions"] = object_decisions(report, allow_approximation)
    return report


def summary_text(report: Dict[str, Any]) -> str:
    """One line for a status bar or panel label: overall, objects, materials."""
    profile = report["output_profile"]
    if "materials" not in report:
        return "Material capability (%s): %s is %s" % (
            profile, report["material"], report["status"].replace("_", " ").lower())

    def tally(counts: Dict[str, int]) -> str:
        return "%d supported, %d approximation required, %d blocked" % (
            counts[SUPPORTED], counts[APPROXIMATION_REQUIRED], counts[BLOCKED])

    objects = report.get("object_counts")
    if objects is None:
        objects = {SUPPORTED: 0, APPROXIMATION_REQUIRED: 0, BLOCKED: 0}
        for entry in report["objects"]:
            objects[entry["status"]] += 1
    return "Material capability (%s): %s; objects: %s; materials: %s" % (
        profile, report["status"].replace("_", " "), tally(objects), tally(report["counts"]))


def summary_lines(report: Dict[str, Any], limit: int = 6) -> List[str]:
    """Most severe findings first, one short line each, for a UI box."""
    rows = []
    materials = report["materials"] if "materials" in report else [report]
    for entry in report.get("objects", []):
        for issue in entry["issues"]:
            rows.append((_RANK[issue["impact"]], "%s %s: %s" % (
                issue["impact"].replace("_", " "), entry["object"], issue["code"])))
    for material in materials:
        notable = [i for i in material["issues"] if i["impact"] != NONE]
        if not notable:
            continue
        first = notable[0]
        what = first["code"] + (" (%s)" % first["effect"] if first["effect"] else "")
        more = " (+%d more)" % (len(notable) - 1) if len(notable) > 1 else ""
        rows.append((_RANK[material["status"]], "%s %s: %s at %s%s" % (
            material["status"].replace("_", " "), material["material"], what,
            first["node_instance"], more)))
    rows.sort(key=lambda row: -row[0])
    lines = [text for _rank, text in rows]
    if len(lines) > limit:
        hidden = len(lines) - (limit - 1)
        lines = lines[:limit - 1] + ["... and %d more; see the capability report" % hidden]
    return lines


def report_json(report: Dict[str, Any]) -> str:
    """Pretty JSON text that always encodes as UTF-8.

    Non-ASCII names stay readable.  A lone surrogate (Blender returns one for
    a file path whose bytes are not valid UTF-8) cannot be encoded, so it is
    written as its JSON escape (``\\udce9``), which parses back to the same
    string (REVIEW_A finding 4).
    """
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False) + "\n"
    # Outside string literals JSON text is ASCII, so every replaced character
    # sits inside a string, where \\uXXXX is a valid escape.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _umask() -> int:
    """The process umask, read without changing it where the OS allows."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    current = os.umask(0o022)   # no read-only query exists elsewhere
    os.umask(current)
    return current


def _report_mode(path: str) -> int:
    """Mode for the new report: the replaced report's own, else an ordinary file's."""
    try:
        existing = os.stat(path)
        if stat.S_ISREG(existing.st_mode):
            return stat.S_IMODE(existing.st_mode)
    except OSError:
        pass
    return 0o666 & ~_umask()


def write_report(report: Dict[str, Any], path: str) -> str:
    """Write the JSON report atomically to exactly ``path``; raise on failure.

    The text goes to a temporary file in the destination folder, is flushed and
    fsynced, and then replaces ``path``.  On POSIX the file gets the mode an
    ordinary new file would (umask), or the mode of the report it replaces,
    not mkstemp's private 0600.  Any failure (filesystem, encoding, a value
    JSON cannot hold) removes the temporary file, leaves any previous report
    untouched and raises an OSError naming ``path``.
    """
    path = os.path.abspath(path)
    temp: Optional[str] = None
    try:
        text = report_json(report)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=".capability_", suffix=".tmp",
                                        dir=os.path.dirname(path))
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temp, _report_mode(path))
        os.replace(temp, path)
        temp = None
    except (OSError, UnicodeError, ValueError) as exc:
        message = "could not write material capability report %s: %s" % (path, exc)
        error: OSError = OSError(message)
        if isinstance(exc, OSError):
            try:
                error = type(exc)(message)
            except Exception:
                pass
        raise error from exc
    finally:
        if temp is not None:
            try:
                os.remove(temp)
            except OSError:
                pass
    return path
