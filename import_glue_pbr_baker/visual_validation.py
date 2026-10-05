"""Bounded visual comparison of a source object and its converted output.

``compare_pair()`` renders/bakes both objects with Cycles, saves reference,
output, difference and coverage-mask images plus a JSON report, and returns a
verdict.  It never edits the user's objects, meshes, materials, images, scene,
selection, render settings or preferences: every bake and render happens on
evaluated-mesh PROXY copies inside a private temporary scene that is removed
again, including on errors and cancellation.

Two independent layers
----------------------
surface  Channel data compared in a SHARED SURFACE CORRESPONDENCE.  When the
         source and output meshes have loop-identical topology -- or the same
         polygons with the same corners, reordered or with split vertices
         (the CROP_SPLIT join) -- both proxies get the same per-loop probe UV
         layout (built once from the source as connected charts), and that
         layout is used only as the bake TARGET.  The UV layer an
         unconnected Image Texture samples with (``active_render``) is left
         alone, so texel (x, y) of both bakes is the same surface point
         however the source and output UVs differ.  A polygon-ID bake measures
         exactly which polygons the probe sampled.  Channels: base colour
         (linear), metallic, roughness, alpha, and the object-space shading
         normal.  Colour/metal/rough/alpha need an independent reference:
         PRINCIPLED_DIRECT (a Principled BSDF provably IS the Cycles surface,
         with every input outside the channel contract neutral) or
         REFERENCE_MATERIALS (caller-supplied emission materials, e.g. the
         resilient-engine oracle).  Anything else is UNSUPPORTED_REFERENCE; a
         first top-level Principled is never treated as an oracle.
render   Fixed-framing orthographic renders from six axis-aligned directions
         under ``diffuse`` (uniform white world), ``grazing`` (sun 75 degrees
         off the view axis: normal relief) and ``highlight`` (sun 20 degrees off
         the view axis: roughness) lighting.  Premultiplied linear RGB and alpha
         are compared separately, read from 32-bit EXR files.  A material
         override render gives each object's geometric coverage, so a thin or
         missing silhouette is reported instead of hidden in an average.  This
         is the only layer available when topology changed; its scope is then
         stated as render-only.

Metrics per comparison: mean error, per-pixel outlier fraction, a local-region
(k x k window) maximum with the fraction of compared texels its windows cover,
and coverage counts, with an explicit mask.  Every threshold and setting is
persisted in the report.

Invariance: each object's fingerprint is read until two consecutive reads
agree (a cold-opened MOVIE image settles on its first read), before and after
the comparison.  A baseline that never settles is INVARIANCE_UNVERIFIED, never
blamed on the comparison as *_MODIFIED.

Verdicts: PASS, FAIL, INSUFFICIENT_COVERAGE, UNSUPPORTED_REFERENCE.  Any error,
missing artifact, unsupported reference or inadequate coverage prevents PASS
(see ``derive_status``).  A cancelled comparison publishes nothing.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import sys
import tempfile
import traceback
import uuid
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree

from .shader_graph import material_dependencies

SCHEMA_VERSION = 1
MODULE_VERSION = "1.1.0"
REPORT_NAME = "report.json"
REPORT_HASH_NAME = "report.json.sha256"
PROBE_UV = "__ig_visual_probe__"
POLYGON_ID_ATTRIBUTE = "__ig_visual_polygon_id__"
MAX_PROBE_RESOLUTION = 8192
# probe_resolution="auto" picks the smallest of these the layout predicts is enough.
AUTO_PROBE_RESOLUTIONS = (256, 512, 1024, 2048)
# Polygons join a probe chart while their normal stays within this angle of
# the chart's seed normal, so the orthographic projection never folds a
# polygon and texel density varies by at most 1/cos(45 deg) = 1.41.
CHART_ANGLE_DEG = 45.0
# Fingerprint reads per baseline: two consecutive reads must agree.
FINGERPRINT_READS = 3

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
UNSUPPORTED_REFERENCE = "UNSUPPORTED_REFERENCE"
NOTE = "NOTE"
_STATUS_PRECEDENCE = (FAIL, UNSUPPORTED_REFERENCE, INSUFFICIENT_COVERAGE)

VIEWS = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")
LIGHTING_PRESETS = ("diffuse", "grazing", "highlight")
LAYERS = ("surface", "render")
SURFACE_CHANNELS = ("color", "metal", "rough", "normal", "alpha")
RENDER_CHANNELS = ("color", "alpha")
REQUIRED_SETTINGS = ("resolution", "samples", "device", "views", "lighting_presets", "tolerances")
REQUIRED_ROLES = ("reference", "output", "difference", "mask")
_METRIC_KEYS = ("mean", "outlier_threshold", "outlier_fraction", "local_max")
_COVERAGE_KEYS = ("min_surface_texels", "min_surface_area_fraction", "min_view_interior_pixels",
                  "min_covered_views", "interior_alpha", "silhouette_alpha")
_MAX_UV_LAYERS = 8
_SENTINEL = -1.0

# View name -> (unit vector from the object centre to the camera, camera up).
_VIEW_AXES = {
    "+X": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "-X": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "+Y": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "-Y": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "+Z": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "-Z": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
}
_VIEW_FILE = {"+X": "pos_x", "-X": "neg_x", "+Y": "pos_y", "-Y": "neg_y", "+Z": "pos_z", "-Z": "neg_z"}
# Sun strength is irradiance (W/m^2); the angle is measured from the view axis.
LIGHTING = {
    "diffuse": {"world": 1.0, "sun_strength": 0.0, "sun_angle_from_view_deg": None,
                "description": "uniform white world, strength 1, no lamps"},
    "grazing": {"world": 0.0, "sun_strength": 8.0, "sun_angle_from_view_deg": 75.0,
                "description": "black world; hard sun 75 deg from the view axis toward camera-right"},
    "highlight": {"world": 0.0, "sun_strength": 3.0, "sun_angle_from_view_deg": 20.0,
                  "description": "black world; hard sun 20 deg from the view axis toward camera-up"},
}
DISPLAY_TRANSFORM = (
    "Display PNGs only: render RGB is un-premultiplied, clipped to [0,1] and encoded with the "
    "sRGB transfer function (no exposure, look, AgX or Filmic); alpha is written as straight "
    "alpha. Surface colour uses the same encoding; metal/rough/alpha are written as linear "
    "grey and the object-space normal as its 0.5*n+0.5 encoding. Uncovered texels are "
    "transparent. Metrics never read the PNGs: they use the linear float data (render EXRs "
    "are saved under render/linear/)."
)
ARTIFACT_LEGEND = {
    "difference": "grey = per-pixel max abs error scaled so 2x the local_max threshold is white; "
                  "red = above outlier_threshold; dark blue = uncovered by both; magenta = covered "
                  "by only one side (silhouette mismatch); yellow = non-finite value",
    "mask": "white = interior (fully covered by both); grey = partially covered edge; black = "
            "uncovered; magenta = coverage mismatch; yellow = non-finite value",
}

# Inputs outside the colour/metal/rough/normal/alpha channel contract, with the
# neutral value a channel reference may assume.  Linked means not neutral.
_PRINCIPLED_NEUTRAL = {
    "Diffuse Roughness": 0.0, "Subsurface Weight": 0.0, "Transmission Weight": 0.0,
    "Coat Weight": 0.0, "Sheen Weight": 0.0, "Anisotropic": 0.0,
    "Thin Film Thickness": 0.0, "Specular IOR Level": 0.5, "IOR": 1.5,
    "Specular Tint": (1.0, 1.0, 1.0), "Thin Wall": False,
}
# Shading that depends on the viewing ray, so no fixed map can hold it.
_VIEW_DEPENDENT_NODES = {
    "ShaderNodeFresnel": None, "ShaderNodeLayerWeight": None, "ShaderNodeLightPath": None,
    "ShaderNodeCameraData": None,
    "ShaderNodeNewGeometry": {"Incoming", "Backfacing"},
    "ShaderNodeTexCoord": {"Camera", "Window", "Reflection"},
}
# Cycles evaluates at most 64 closures per shading point and silently drops the
# rest, so a deep mix chain renders darker than the material it describes.
# Measured on 5.2.0 with chains of IDENTICAL BSDFs (which equal one BSDF): 64
# Diffuse fit and 65 lose energy; 12 metallic Principled fit and 13 lose
# energy (so up to 5 closures each).  Unmeasured closure nodes count 3.
CLOSURE_BUDGET = 64
_CLOSURE_COST = {
    "ShaderNodeBsdfPrincipled": 5, "ShaderNodeBsdfDiffuse": 1, "ShaderNodeEmission": 1,
    "ShaderNodeBsdfTransparent": 1, "ShaderNodeHoldout": 1, "ShaderNodeBsdfTranslucent": 1,
}
_CLOSURE_COST_DEFAULT = 3
_NOT_CLOSURES = {"ShaderNodeMixShader", "ShaderNodeAddShader"}
# Shading that depends on scene/object context a proxy copy does not carry.
_CONTEXT_DEPENDENT_NODES = {
    "ShaderNodeAmbientOcclusion": "depends on surrounding scene geometry",
    "ShaderNodeLightFalloff": "depends on the light being sampled",
    "ShaderNodeParticleInfo": "depends on a particle instancer",
    "ShaderNodeHairInfo": "depends on hair/curve context",
    "ShaderNodePointInfo": "depends on point-cloud context",
    "ShaderNodeVolumeInfo": "depends on volume context",
    "ShaderNodeUVAlongStroke": "depends on line-art stroke context",
}


# ------------------------------------------------------------------ settings

def default_tolerances() -> Dict[str, Any]:
    """Starting test parameters, not a universal perceptual guarantee.

    Surface means come from tests/blender/source_vs_output.py (colour .025,
    roughness .03, metalness .03, normal .05) and its outlier rule (no more
    than 2% of texels off by > .25); alpha .03 is new.

    Render thresholds were calibrated on Blender 5.2.0, CPU, 48 px renders
    (evidence/C_visual/windows/calibration): identical inputs rendered twice
    differ by exactly 0; faithful GRAPH_BAKE conversions reach colour mean
    <= .010 and local max <= .027 at 64 samples (the uniform-world preset is
    Monte Carlo noise that falls as 1/sqrt(samples): .056 local at 16
    samples); 6x6-texel corruptions reach local max .42 (colour), .45
    (normal, grazing) and .70 (alpha).  A 6x6-texel roughness change reaches
    only .047 in the render layer, so render-only scope can miss small
    roughness changes; the surface layer measures them directly.  These are
    synthetic-fixture calibrations: recalibrate before using the render layer
    as a release gate for another asset class.
    """
    return {
        "local_window": 4,
        "local_min_valid_fraction": 0.5,
        "surface": {
            "color": {"mean": 0.025, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.10},
            "metal": {"mean": 0.03, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.12},
            "rough": {"mean": 0.03, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.12},
            "normal": {"mean": 0.05, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.15},
            "alpha": {"mean": 0.03, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.12},
        },
        "render": {
            "color": {"mean": 0.02, "outlier_threshold": 0.15, "outlier_fraction": 0.02, "local_max": 0.06},
            "alpha": {"mean": 0.02, "outlier_threshold": 0.25, "outlier_fraction": 0.02, "local_max": 0.10},
        },
        "coverage": {
            "min_surface_texels": 64,
            "min_surface_area_fraction": 0.95,
            "min_view_interior_pixels": 16,
            "min_covered_views": 1,
            "interior_alpha": 0.99,
            "silhouette_alpha": 0.001,
        },
    }


def default_settings() -> Dict[str, Any]:
    """A complete settings dict; callers override fields explicitly."""
    return {
        "resolution": 128,
        "probe_resolution": 256,
        "samples": 64,
        "device": "CPU",
        "views": list(VIEWS),
        "lighting_presets": list(LIGHTING_PRESETS),
        "layers": list(LAYERS),
        "keep_linear": True,
        "channel_references": {},
        "tolerances": default_tolerances(),
    }


_OPTIONAL_DEFAULTS = {
    "probe_resolution": None, "layers": list(LAYERS), "keep_linear": True, "channel_references": {},
}


def _number(value: Any, label: str, low: float = 0.0, high: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number, got %r" % (label, value))
    if not low <= value <= high:
        raise ValueError("%s must be in [%s, %s], got %r" % (label, low, high, value))
    return value


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer, got %r" % (label, value))
    if not low <= value <= high:
        raise ValueError("%s must be in [%d, %d], got %r" % (label, low, high, value))
    return value


def _choices(value: Any, label: str, allowed: Sequence[str], allow_empty: bool) -> List[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise ValueError("%s must be a list of names" % label)
    bad = [v for v in value if v not in allowed]
    if bad:
        raise ValueError("%s has unknown entries %s; allowed: %s" % (label, bad, list(allowed)))
    if len(set(value)) != len(value):
        raise ValueError("%s has duplicate entries" % label)
    if not value and not allow_empty:
        raise ValueError("%s must not be empty" % label)
    return list(value)


def _exact_keys(value: Any, label: str, keys: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("%s must be a dict" % label)
    missing = [k for k in keys if k not in value]
    if missing:
        raise ValueError("%s is missing %s" % (label, ", ".join(label + "." + k for k in missing)))
    unknown = [k for k in value if k not in keys]
    if unknown:
        raise ValueError("%s has unknown keys %s" % (label, unknown))
    return value


def _validate_tolerances(tolerances: Any) -> None:
    _exact_keys(tolerances, "tolerances", ("local_window", "local_min_valid_fraction",
                                           "surface", "render", "coverage"))
    _integer(tolerances["local_window"], "tolerances.local_window", 1, 64)
    _number(tolerances["local_min_valid_fraction"], "tolerances.local_min_valid_fraction", 0.0, 1.0)
    for layer, channels in (("surface", SURFACE_CHANNELS), ("render", RENDER_CHANNELS)):
        rows = _exact_keys(tolerances[layer], "tolerances." + layer, channels)
        for channel in channels:
            label = "tolerances.%s.%s" % (layer, channel)
            row = _exact_keys(rows[channel], label, _METRIC_KEYS)
            for key in _METRIC_KEYS:
                _number(row[key], label + "." + key, 0.0, 1e6)
    coverage = _exact_keys(tolerances["coverage"], "tolerances.coverage", _COVERAGE_KEYS)
    _integer(coverage["min_surface_texels"], "tolerances.coverage.min_surface_texels", 1, 1 << 30)
    _number(coverage["min_surface_area_fraction"], "tolerances.coverage.min_surface_area_fraction", 0.0, 1.0)
    _integer(coverage["min_view_interior_pixels"], "tolerances.coverage.min_view_interior_pixels", 1, 1 << 30)
    _integer(coverage["min_covered_views"], "tolerances.coverage.min_covered_views", 1, len(VIEWS))
    _number(coverage["interior_alpha"], "tolerances.coverage.interior_alpha", 0.0, 1.0)
    _number(coverage["silhouette_alpha"], "tolerances.coverage.silhouette_alpha", 0.0, 1.0)


def validate_settings(settings: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Return (resolved JSON-safe copy, optional keys that took defaults).

    Required keys are never defaulted; unknown keys are rejected so a typo
    cannot silently fall back to a different threshold.
    """
    if not isinstance(settings, dict):
        raise ValueError("visual comparison settings must be a dict")
    missing = [key for key in REQUIRED_SETTINGS if key not in settings]
    if missing:
        raise ValueError("missing required visual comparison settings: " + ", ".join(missing))
    unknown = [key for key in settings if key not in REQUIRED_SETTINGS and key not in _OPTIONAL_DEFAULTS]
    if unknown:
        raise ValueError("unknown visual comparison settings: %s" % unknown)
    try:
        resolved = json.loads(json.dumps(settings, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("visual comparison settings must be JSON-safe: %s" % exc) from exc
    defaulted = []
    for key, value in _OPTIONAL_DEFAULTS.items():
        if key not in resolved:
            resolved[key] = json.loads(json.dumps(value))
            defaulted.append(key)
    _integer(resolved["resolution"], "resolution", 8, 4096)
    if resolved["probe_resolution"] is None:
        resolved["probe_resolution"] = resolved["resolution"]
    if resolved["probe_resolution"] != "auto":
        _integer(resolved["probe_resolution"], "probe_resolution", 8, MAX_PROBE_RESOLUTION)
    _integer(resolved["samples"], "samples", 1, 1 << 16)
    if resolved["device"] not in ("CPU", "GPU"):
        raise ValueError("device must be 'CPU' or 'GPU', got %r" % (resolved["device"],))
    layers = _choices(resolved["layers"], "layers", LAYERS, False)
    needs_render = "render" in layers
    _choices(resolved["views"], "views", VIEWS, not needs_render)
    _choices(resolved["lighting_presets"], "lighting_presets", LIGHTING_PRESETS, not needs_render)
    if not isinstance(resolved["keep_linear"], bool):
        raise ValueError("keep_linear must be a bool")
    references = resolved["channel_references"]
    if not isinstance(references, dict):
        raise ValueError("channel_references must map material names to {'color','mra'} names")
    for name, row in references.items():
        _exact_keys(row, "channel_references[%r]" % name, ("color", "mra"))
        for key in ("color", "mra"):
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError("channel_references[%r].%s must be a material name" % (name, key))
    _validate_tolerances(resolved["tolerances"])
    return resolved, defaulted


# ------------------------------------------------------------------- helpers

def _text(value: Any, limit: int = 2000) -> str:
    try:
        text = str(value)
    except Exception:
        text = repr(value)
    return text[:limit]


def _finding(code: str, klass: str, layer: str, subject: str, message: str) -> Dict[str, Any]:
    return {"code": code, "class": klass, "layer": layer, "subject": subject, "message": message}


def _pointer(block: Any) -> int:
    return int(block.as_pointer())


def _render_uv_name(mesh: Any) -> Optional[str]:
    for layer in mesh.uv_layers:
        if layer.active_render:
            return layer.name
    return None


def _active_uv_name(mesh: Any) -> Optional[str]:
    layer = mesh.uv_layers.active
    return layer.name if layer is not None else None


def _sha256(path: str) -> Tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            size += len(block)
            hasher.update(block)
    return hasher.hexdigest(), size


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _png_bytes(pixels: np.ndarray) -> bytes:
    """8-bit PNG from (H, W, C) uint8 rows ordered TOP-DOWN, C in {3, 4}."""
    height, width, channels = pixels.shape
    color_type = {3: 2, 4: 6}[channels]
    raw = np.zeros((height, 1 + width * channels), np.uint8)
    raw[:, 1:] = pixels.reshape(height, width * channels)
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def _to_u8(values: np.ndarray) -> np.ndarray:
    return np.clip(np.round(np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=0.0) * 255.0),
                   0, 255).astype(np.uint8)


def _srgb_encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(np.nan_to_num(linear, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


# --------------------------------------------------------------- the run state

_TRACKED_COLLECTIONS = ("objects", "meshes", "materials", "images", "cameras", "lights",
                        "worlds", "scenes", "collections", "node_groups")


class _Run:
    """Everything this comparison creates, so it can all be removed again."""

    def __init__(self, settings: Dict[str, Any], staging: str):
        self.settings = settings
        self.staging = staging
        self.stage = "setup"
        self.created: List[Tuple[str, Any]] = []
        self.scene = None
        self.view_layer = None
        self.temp_dir = tempfile.mkdtemp(prefix="ig_visual_")
        self.token = uuid.uuid4().hex[:10]
        self.render_result_existed = any(im.type == "RENDER_RESULT" for im in bpy.data.images)
        self.before = {name: {_pointer(x) for x in getattr(bpy.data, name)} for name in _TRACKED_COLLECTIONS}
        self.artifacts: Dict[str, Dict[str, Any]] = {}
        self.render_count = 0
        self.bake_count = 0
        # The probe size the surface layer actually uses ("auto" resolves per mesh).
        self.probe_size = settings["probe_resolution"] if isinstance(settings["probe_resolution"], int) else None

    def name(self, label: str) -> str:
        return "__ig_visual_%s_%s" % (self.token, label)

    def track(self, kind: str, block: Any) -> Any:
        self.created.append((kind, block))
        return block

    def artifact(self, rel: str, layer: str, role: str, subject: str) -> str:
        self.artifacts[rel] = {"layer": layer, "role": role, "subject": subject}
        path = os.path.join(self.staging, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def cleanup(self) -> List[str]:
        """Remove every datablock and file this run created; report leftovers."""
        errors: List[str] = []
        if self.view_layer is not None:
            try:
                self.view_layer.material_override = None
            except Exception:
                pass
        if self.scene is not None:
            try:
                bpy.data.scenes.remove(self.scene)
            except Exception as exc:
                errors.append("remove temporary scene: %s" % _text(exc))
            self.scene = None
            self.view_layer = None
        order = {"objects": 0, "cameras": 1, "lights": 1, "meshes": 1, "worlds": 2,
                 "materials": 3, "images": 4, "node_groups": 5, "collections": 6}
        for kind, block in sorted(reversed(self.created), key=lambda row: order.get(row[0], 9)):
            collection = getattr(bpy.data, kind)
            try:
                pointer = _pointer(block)
            except ReferenceError:
                continue                      # already removed during the run
            if pointer in self.before[kind]:
                continue                      # never touch a pre-existing datablock
            match = next((x for x in collection if _pointer(x) == pointer), None)
            if match is None:
                continue
            try:
                collection.remove(match, do_unlink=True)
            except TypeError:
                collection.remove(match)
            except Exception as exc:
                errors.append("remove %s: %s" % (kind, _text(exc)))
        self.created.clear()
        if not self.render_result_existed:
            for image in list(bpy.data.images):
                if image.type == "RENDER_RESULT":
                    try:
                        bpy.data.images.remove(image)
                    except Exception as exc:
                        errors.append("remove Render Result: %s" % _text(exc))
        for name in _TRACKED_COLLECTIONS:
            extra = [x for x in getattr(bpy.data, name)
                     if _pointer(x) not in self.before[name]
                     and not (name == "images" and x.type == "RENDER_RESULT")]
            if extra:
                errors.append("cleanup left %d new %s: %s"
                              % (len(extra), name, ", ".join(_text(x.name, 60) for x in extra[:5])))
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        return errors


# ---------------------------------------------------------- material analysis

def _links(socket: Any) -> List[Any]:
    return [link for link in socket.links
            if not getattr(link, "is_muted", False) and getattr(link, "is_valid", True)]


def _matching(sockets: Any, reference: Any) -> Any:
    identifier = reference.identifier
    return next((s for s in sockets if s.identifier == identifier), None)


def _reachable(material: Any) -> List[Tuple[Any, Any, Tuple[Any, ...]]]:
    """(node, output socket, group context) for every node the Cycles output
    depends on, through groups, reroutes and muted-node bypasses.

    Mix factors are not folded: considering more nodes reachable only makes a
    reference less likely to be accepted, which is the safe direction.
    """
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return []
    output = tree.get_output_node("CYCLES")
    if output is None:
        return []
    found: List[Tuple[Any, Any, Tuple[Any, ...]]] = []
    seen = set()
    stack = [(socket, ()) for socket in output.inputs if socket.name in ("Surface", "Volume", "Displacement")]
    while stack:
        socket, context = stack.pop()
        for link in _links(socket):
            source = link.from_socket
            node = source.node
            key = (_pointer(node), source.identifier, tuple(_pointer(n) for n in context))
            if key in seen:
                continue
            seen.add(key)
            found.append((node, source, context))
            if node.mute:
                stack.extend((l.from_socket, context) for l in node.internal_links
                             if l.to_socket == source)
                continue
            group = getattr(node, "node_tree", None) if node.type == "GROUP" or hasattr(node, "node_tree") else None
            if group is not None:
                if any(_pointer(group) == _pointer(n.node_tree) for n in context):
                    continue
                for inner in group.nodes:
                    if inner.type == "GROUP_OUTPUT" and inner.is_active_output:
                        target = _matching(inner.inputs, source)
                        if target is not None:
                            stack.append((target, context + (node,)))
                continue
            if node.type == "GROUP_INPUT":
                if context:
                    parent = _matching(context[-1].inputs, source)
                    if parent is not None:
                        stack.append((parent, context[:-1]))
                continue
            stack.extend((s, context) for s in node.inputs if s.enabled)
    return found


def _instance(material: Any, node: Any, context: Tuple[Any, ...]) -> str:
    return "/".join([material.name] + [n.name for n in context] + [node.name])


def _direct_principled(material: Any) -> Tuple[Optional[Any], str]:
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None, "material has no node tree"
    output = tree.get_output_node("CYCLES")
    if output is None:
        return None, "no active Cycles material output"
    links = _links(output.inputs["Surface"])
    if not links:
        return None, "Cycles Surface output is not connected"
    socket = links[0].from_socket
    node = socket.node
    while node.type == "REROUTE" and not node.mute:
        incoming = _links(node.inputs[0])
        if not incoming:
            return None, "Surface reroute is not connected"
        socket = incoming[0].from_socket
        node = socket.node
    if node.type != "BSDF_PRINCIPLED" or node.mute:
        return None, "Cycles surface is %s (%s), not a Principled BSDF" % (node.bl_idname, node.name)
    return node, ""


def _not_neutral(socket: Any, neutral: Any) -> bool:
    if _links(socket):
        return True
    value = getattr(socket, "default_value", None)
    if isinstance(neutral, bool):
        return bool(value) != neutral
    if isinstance(neutral, tuple):
        return any(abs(float(a) - b) > 1e-6 for a, b in zip(value, neutral))
    return abs(float(value) - neutral) > 1e-6


def analyze_material(material: Any, channel_references: Optional[Dict[str, Any]] = None,
                     role: str = "source") -> Dict[str, Any]:
    """Read-only reference analysis for one used material.

    ``blocking_issues``: the material cannot be shown as authored at all
    (missing nodes/images, identity- or scene-context-dependent inputs); both
    layers are unsupported.  ``render_issues``: blocking issues plus anything
    that makes a Cycles render of it unfaithful (the closure budget).
    ``surface_issues``: blocking issues plus anything that keeps fixed
    colour/metal/rough/alpha/normal channels from describing the surface.
    A time-dependent image (movie/sequence) is both a render and a surface
    issue: a fixed map holds one frame, and a single-frame render is not a
    reference for a surface whose appearance changes over time.
    """
    references = channel_references or {}
    result: Dict[str, Any] = {"material": None, "role": role, "kind": None, "principled": None,
                              "blocking_issues": [], "render_issues": [], "surface_issues": [],
                              "closure_estimate": 0, "native_normal_reliable": True}

    def add(row: Dict[str, Any], scope: str) -> None:
        targets = {"blocking": ("blocking_issues", "render_issues", "surface_issues"),
                   "render": ("render_issues",), "surface": ("surface_issues",),
                   "time": ("render_issues", "surface_issues")}[scope]
        for key in targets:
            if row not in result[key]:
                result[key].append(row)

    if material is None:
        add({"code": "MATERIAL_MISSING", "node": "", "node_instance": "",
             "message": "a used material slot is empty"}, "blocking")
        return result
    result["material"] = material.name

    def issue(code: str, node: Any, context: Tuple[Any, ...], message: str, scope: str) -> None:
        add({"code": code, "node": node.name if node is not None else "",
             "node_instance": _instance(material, node, context) if node is not None else material.name,
             "message": message}, scope)

    for diagnostic in material_dependencies(material)["diagnostics"]:
        if diagnostic["severity"] == "ERROR" or diagnostic["code"] == "OBJECT_RANDOM_CONTEXT":
            add({k: diagnostic[k] for k in ("code", "node", "node_instance", "message")}, "blocking")
    closures = 0
    for node, socket, context in _reachable(material):
        idname = node.bl_idname
        if (socket.type == "SHADER" and not node.mute and idname not in _NOT_CLOSURES
                and node.type not in ("GROUP", "REROUTE", "GROUP_INPUT")
                and getattr(node, "node_tree", None) is None):
            closures += _CLOSURE_COST.get(idname, _CLOSURE_COST_DEFAULT)
        if idname in _VIEW_DEPENDENT_NODES:
            outputs = _VIEW_DEPENDENT_NODES[idname]
            if outputs is None or socket.name in outputs:
                issue("VIEW_DEPENDENT_INPUT", node, context,
                      "%s output %r depends on the viewing ray; fixed maps cannot hold it"
                      % (node.bl_label or idname, socket.name), "surface")
        if idname in _CONTEXT_DEPENDENT_NODES:
            issue("CONTEXT_DEPENDENT_INPUT", node, context, _CONTEXT_DEPENDENT_NODES[idname], "blocking")
        if idname == "ShaderNodeAttribute" and getattr(node, "attribute_type", "GEOMETRY") != "GEOMETRY":
            issue("CONTEXT_DEPENDENT_INPUT", node, context,
                  "attribute of type %s is read from object/scene context" % node.attribute_type, "blocking")
        if idname in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment") and node.image is not None:
            image = node.image
            if image.source == "FILE" and image.packed_file is None:
                # Lexical normalisation, as the texture precheck resolves it: an
                # unnormalised '..' path can exceed Windows MAX_PATH, or pass
                # through a component that does not exist on POSIX (M1 Task 4).
                path = os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))
                if not os.path.isfile(path):
                    issue("IMAGE_MISSING", node, context,
                          "image %r file not found: %s" % (image.name, path), "blocking")
            if image.source in ("MOVIE", "SEQUENCE"):
                issue("TIME_DEPENDENT_IMAGE", node, context,
                      "%s image %r changes over time; a fixed map holds one frame, and one rendered "
                      "frame is not a reference for it" % (image.source.lower(), image.name), "time")
    result["closure_estimate"] = closures
    if closures > CLOSURE_BUDGET:
        result["native_normal_reliable"] = False
        issue("CLOSURE_LIMIT", None, (),
              "about %d closures reachable; Cycles evaluates at most %d per shading point and drops "
              "the rest, so its render of this material is not the authored mix" % (closures, CLOSURE_BUDGET),
              "render")

    if material.name in references:
        result["kind"] = "REFERENCE_MATERIALS"
        for key in ("color", "mra"):
            name = references[material.name][key]
            ref = bpy.data.materials.get(name)
            if ref is None:
                issue("REFERENCE_MATERIAL_MISSING", None, (), "channel reference %r for %s not found"
                      % (name, key), "surface")
                continue
            for diagnostic in material_dependencies(ref)["diagnostics"]:
                if diagnostic["severity"] == "ERROR":
                    issue("REFERENCE_MATERIAL_INVALID", None, (),
                          "channel reference %r: %s at %s" % (name, diagnostic["code"],
                                                             diagnostic["node_instance"]), "surface")
        return result
    principled, why = _direct_principled(material)
    if principled is None:
        issue("SURFACE_NOT_DIRECT_PRINCIPLED", None, (),
              "%s; no independent channel reference was supplied (settings.channel_references)" % why,
              "surface")
        return result
    tree = material.node_tree
    output = tree.get_output_node("CYCLES")
    for name in ("Volume", "Displacement"):
        if _links(output.inputs[name]):
            issue("%s_CONNECTED" % name.upper(), output, (),
                  "%s output is connected; outside the channel contract" % name, "surface")
    for name, neutral in _PRINCIPLED_NEUTRAL.items():
        socket = principled.inputs.get(name)
        if socket is not None and socket.enabled and _not_neutral(socket, neutral):
            issue("PRINCIPLED_INPUT_OUTSIDE_CONTRACT", principled, (),
                  "%r is not neutral (%r); fixed colour/metal/rough/normal/alpha maps cannot hold it"
                  % (name, neutral), "surface")
    strength = principled.inputs.get("Emission Strength")
    color = principled.inputs.get("Emission Color")
    if strength is not None and color is not None and _not_neutral(strength, 0.0) \
            and _not_neutral(color, (0.0, 0.0, 0.0)):
        issue("PRINCIPLED_INPUT_OUTSIDE_CONTRACT", principled, (),
              "emission is active; outside the channel contract", "surface")
    result["kind"] = "PRINCIPLED_DIRECT"
    result["principled"] = principled.name
    return result


def surface_reference_support(objects: Iterable[Any],
                              channel_references: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Preflight helper: which used materials have an independent channel reference.

    "Used" means used by the EVALUATED mesh, which is what the comparison
    renders and bakes: a modifier (e.g. Solidify's material offset) can put
    faces on a slot the base mesh never uses."""
    rows = {}
    for obj in objects:
        for material in _material_usage(obj)[0]:
            key = material.name if material is not None else "<empty>"
            info = analyze_material(material, channel_references)
            rows[key] = {"kind": info["kind"], "supported": not info["surface_issues"],
                         "issues": info["surface_issues"]}
    return rows


def _slot_indices(mesh: Any, count: int) -> List[int]:
    """Slot indices ``mesh``'s polygons use, clamped as Blender clamps them."""
    if count == 0:
        return []
    polys = mesh.polygons
    indices = np.zeros(len(polys), np.int32)
    if len(polys):
        polys.foreach_get("material_index", indices)
    return sorted({min(max(int(i), 0), count - 1) for i in np.unique(indices)}) if len(polys) else []


def _used_slot_indices(obj: Any) -> List[int]:
    """Slot indices used by ``obj.data`` as it is (proxies: already evaluated)."""
    return _slot_indices(obj.data, len(obj.material_slots))


def _unique_materials(materials: Iterable[Any]) -> List[Any]:
    result, seen = [], set()
    for material in materials:
        key = _pointer(material) if material is not None else None
        if key not in seen:
            seen.add(key)
            result.append(material)
    return result


def _used_materials(obj: Any) -> List[Any]:
    """Materials used by ``obj.data`` as it is -- for the comparison's own
    proxies, whose meshes ARE the evaluated meshes.  For user objects use
    ``_material_usage``."""
    slots = obj.material_slots
    return _unique_materials(slots[index].material for index in _used_slot_indices(obj))


def _material_usage(obj: Any) -> Tuple[List[Any], str]:
    """(materials the EVALUATED mesh uses, "evaluated" | "base: <why>").

    Slot i resolves exactly as ``_make_proxy`` assigns it: the object's slot
    material, or the evaluated mesh's own material for slots a modifier added.
    """
    evaluated = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
    except Exception as exc:
        return _used_materials(obj), "base: modifier evaluation unavailable (%s)" % _text(exc, 200)
    try:
        slots = [slot.material for slot in obj.material_slots]
        own = list(mesh.materials)
        count = max(len(slots), len(own))

        def at(index: int) -> Any:
            return slots[index] if index < len(slots) else own[index]
        return _unique_materials(at(i) for i in _slot_indices(mesh, count)), "evaluated"
    finally:
        try:
            evaluated.to_mesh_clear()
        except Exception:
            pass


# ----------------------------------------------------------- scene and proxies

def _device(requested: str) -> Tuple[Dict[str, Any], Optional[str]]:
    info: Dict[str, Any] = {"requested": requested, "actual": None, "backend": None, "devices": []}
    if requested == "CPU":
        info.update(actual="CPU", backend="CPU")
        return info, None
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        kind = prefs.compute_device_type
        devices = [d for d in prefs.devices if d.use and d.type == kind and d.type != "CPU"]
    except Exception as exc:
        return info, "could not read Cycles device preferences: %s" % _text(exc)
    info["backend"] = kind
    info["devices"] = [d.name for d in devices]
    if kind in ("NONE", "", None) or not devices:
        return info, ("GPU requested but Cycles has no enabled GPU device (compute device type %r); "
                      "enable one in Preferences > System, or run inside the engine after "
                      "ensure_cycles_device(). A CPU render is never reported as GPU." % kind)
    info["actual"] = kind
    return info, None


def _setup_scene(run: _Run, user_scene: Any) -> None:
    scene = bpy.data.scenes.new(run.name("scene"))
    run.scene = scene
    run.view_layer = scene.view_layers[0]
    scene.frame_current = user_scene.frame_current
    scene.render.engine = "CYCLES"
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.film_transparent = True
    scene.render.resolution_x = scene.render.resolution_y = run.settings["resolution"]
    scene.render.resolution_percentage = 100
    scene.render.use_persistent_data = False
    settings = scene.render.image_settings
    settings.file_format = "OPEN_EXR"
    settings.color_mode = "RGBA"
    settings.color_depth = "32"
    try:
        settings.exr_codec = "ZIP"
    except Exception:
        pass
    cycles = scene.cycles
    cycles.device = run.settings["device"]
    cycles.samples = run.settings["samples"]
    cycles.use_adaptive_sampling = False
    cycles.use_denoising = False
    cycles.seed = 0
    cycles.use_animated_seed = False
    try:
        scene.view_settings.view_transform = "Standard"
    except Exception:
        pass
    bake = scene.render.bake
    bake.margin = 0
    bake.use_clear = False
    bake.use_selected_to_active = False
    bake.normal_space = "OBJECT"
    bake.target = "IMAGE_TEXTURES"


def _make_proxy(run: _Run, obj: Any, label: str) -> Tuple[Any, Dict[str, Any]]:
    """An evaluated, independent copy of ``obj`` placed in the private scene."""
    info: Dict[str, Any] = {"evaluated": True, "notes": []}
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True,
                                               depsgraph=depsgraph)
        source_mesh = evaluated.data
    except Exception as exc:
        mesh = obj.data.copy()
        source_mesh = obj.data
        info["evaluated"] = False
        info["notes"].append("modifier evaluation unavailable (%s); base mesh used" % _text(exc, 200))
    run.track("meshes", mesh)
    mesh.name = run.name(label + "_mesh")
    effective = [slot.material for slot in obj.material_slots]
    for index, material in enumerate(effective):
        if index < len(mesh.materials):
            mesh.materials[index] = material
        else:
            mesh.materials.append(material)
    render_uv = _render_uv_name(source_mesh)
    if render_uv is not None and render_uv in mesh.uv_layers:
        mesh.uv_layers[render_uv].active_render = True
    proxy = run.track("objects", bpy.data.objects.new(run.name(label), mesh))
    proxy.matrix_world = obj.matrix_world.copy()
    proxy.color = obj.color
    proxy.pass_index = obj.pass_index
    run.scene.collection.objects.link(proxy)
    proxy.hide_render = False
    info["texture_uv"] = _render_uv_name(mesh)
    info["uv_layers"] = [layer.name for layer in mesh.uv_layers]
    info["modifiers"] = [m.type for m in obj.modifiers]
    if not info["evaluated"] and obj.modifiers:
        info["unevaluated_modifiers"] = True
    return proxy, info


def _world_points(obj: Any) -> np.ndarray:
    mesh = obj.data
    count = len(mesh.vertices)
    co = np.empty(count * 3, np.float64)
    if count:
        mesh.vertices.foreach_get("co", co)
    co = co.reshape(count, 3)
    matrix = np.array(obj.matrix_world, np.float64)
    return co @ matrix[:3, :3].T + matrix[:3, 3]


def _topology_mismatch(a: Any, b: Any) -> Optional[str]:
    ma, mb = a.data, b.data
    for label, x, y in (("vertex", len(ma.vertices), len(mb.vertices)),
                        ("polygon", len(ma.polygons), len(mb.polygons)),
                        ("loop", len(ma.loops), len(mb.loops))):
        if x != y:
            return "%s count differs (%d source vs %d output)" % (label, x, y)
    for collection, attr in (("polygons", "loop_start"), ("polygons", "loop_total"), ("loops", "vertex_index")):
        ca, cb = getattr(ma, collection), getattr(mb, collection)
        xa = np.empty(len(ca), np.int64)
        xb = np.empty(len(cb), np.int64)
        if len(ca):
            ca.foreach_get(attr, xa)
            cb.foreach_get(attr, xb)
        if not np.array_equal(xa, xb):
            return "%s.%s differs; there is no loop-for-loop correspondence" % (collection, attr)
    pa, pb = _world_points(a), _world_points(b)
    if len(pa):
        extent = float(np.linalg.norm(pa.max(0) - pa.min(0)))
        delta = float(np.abs(pa - pb).max())
        if delta > 1e-5 * extent + 1e-6:
            return "world-space vertex positions differ by up to %.6g" % delta
    return None


def _loop_tables(mesh: Any) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(loop vertex, loop edge, polygon start, polygon size, polygon of loop)."""
    loops, polys = len(mesh.loops), len(mesh.polygons)
    vertex = np.empty(loops, np.int32)
    edge = np.empty(loops, np.int32)
    if loops:
        mesh.loops.foreach_get("vertex_index", vertex)
        mesh.loops.foreach_get("edge_index", edge)
    starts = np.empty(polys, np.int32)
    totals = np.empty(polys, np.int32)
    if polys:
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", totals)
    if polys and starts[0] == 0 and np.array_equal(starts[1:], np.cumsum(totals)[:-1]):
        owner = np.repeat(np.arange(polys, dtype=np.int64), totals)    # Blender's usual layout
    else:
        owner = np.full(loops, -1, np.int64)
        for index, (start, total) in enumerate(zip(starts.tolist(), totals.tolist())):
            owner[start:start + total] = index
    return (vertex.astype(np.int64), edge.astype(np.int64), starts.astype(np.int64),
            totals.astype(np.int64), owner)


def _next_loop(starts: np.ndarray, totals: np.ndarray, owner: np.ndarray) -> np.ndarray:
    """Index of the next loop around each loop's polygon."""
    following = np.arange(len(owner), dtype=np.int64) + 1
    if len(owner):
        offset = following - starts[owner]
        following = starts[owner] + offset % totals[owner]
    return following


def _probe_charts(obj: Any, angle_deg: float = CHART_ANGLE_DEG) -> Dict[str, Any]:
    """Group polygons into connected, orientation-consistent charts whose
    normals stay within ``angle_deg`` of the chart's seed normal, and give
    every loop its orthographic 2D position in its chart (world units).

    Neighbouring polygons stay neighbours in the probe, so a k x k window
    sees a contiguous patch of surface instead of isolated padded cells.
    """
    mesh = obj.data
    points = _world_points(obj)
    vertex, edge, starts, totals, owner = _loop_tables(mesh)
    count = len(starts)
    following = _next_loop(starts, totals, owner)
    charts: Dict[str, Any] = {"polygons": count, "areas": np.zeros(count), "projected": np.zeros(count),
                              "chart": np.full(count, -1, np.int64), "count": 0,
                              "sizes": np.zeros((0, 2)), "fill": np.zeros(0), "owner": owner,
                              "local": np.zeros((len(owner), 2)), "total_area": 0.0}
    if not count:
        return charts
    # Newell normals, relative to each polygon's first corner for precision.
    corner = points[vertex] - points[vertex[starts]][owner]
    ahead = points[vertex[following]] - points[vertex[starts]][owner]
    terms = np.stack([(corner[:, 1] - ahead[:, 1]) * (corner[:, 2] + ahead[:, 2]),
                      (corner[:, 2] - ahead[:, 2]) * (corner[:, 0] + ahead[:, 0]),
                      (corner[:, 0] - ahead[:, 0]) * (corner[:, 1] + ahead[:, 1])], -1)
    newell = np.stack([np.bincount(owner, terms[:, i], count) for i in range(3)], -1)
    length = np.linalg.norm(newell, axis=1)
    live = np.isfinite(length) & (length > 1e-18)
    areas = np.where(live, 0.5 * length, 0.0)
    unit = np.zeros((count, 3))
    unit[live] = newell[live] / length[live, None]
    charts["areas"] = areas
    charts["total_area"] = float(areas.sum())
    # Manifold, consistently wound edges join neighbouring polygons.
    by_edge = np.argsort(edge, kind="stable")
    sorted_edges = edge[by_edge]
    first = np.flatnonzero(np.concatenate([[True], sorted_edges[1:] != sorted_edges[:-1]]))
    sizes = np.diff(np.append(first, len(sorted_edges)))
    pair = first[sizes == 2]
    a, b = by_edge[pair], by_edge[pair + 1]
    pa, pb = owner[a], owner[b]
    keep = ((vertex[a] == vertex[following[b]]) & (vertex[following[a]] == vertex[b])
            & live[pa] & live[pb] & (pa != pb))
    src = np.concatenate([pa[keep], pb[keep]])
    dst = np.concatenate([pb[keep], pa[keep]])
    by_src = np.argsort(src, kind="stable")
    dst = dst[by_src].tolist()
    pointer = np.searchsorted(src[by_src], np.arange(count + 1)).tolist()
    ux, uy, uz = (unit[:, i].tolist() for i in range(3))
    limit = math.cos(math.radians(angle_deg))
    chart = [-1] * count
    axes: List[Tuple[float, float, float]] = []
    for seed in np.argsort(-areas, kind="stable").tolist():
        if chart[seed] >= 0 or not live[seed]:
            continue
        cid = len(axes)
        ax, ay, az = ux[seed], uy[seed], uz[seed]
        axes.append((ax, ay, az))
        chart[seed] = cid
        stack = [seed]
        while stack:
            polygon = stack.pop()
            for k in range(pointer[polygon], pointer[polygon + 1]):
                other = dst[k]
                if chart[other] < 0 and ux[other] * ax + uy[other] * ay + uz[other] * az >= limit:
                    chart[other] = cid
                    stack.append(other)
    chart_of = np.array(chart, np.int64)
    charts["chart"] = chart_of
    charts["count"] = len(axes)
    if not axes:
        return charts
    axis = np.array(axes)
    helper = np.where(np.abs(axis[:, 2:3]) < 0.9, [[0.0, 0.0, 1.0]], [[1.0, 0.0, 0.0]])
    e1 = np.cross(helper, axis)
    e1 /= np.linalg.norm(e1, axis=1, keepdims=True)
    e2 = np.cross(axis, e1)
    charts["projected"] = np.where(chart_of >= 0, areas * np.abs(
        np.sum(unit * axis[np.maximum(chart_of, 0)], axis=1)), 0.0)
    loop_chart = chart_of[owner]
    inside = loop_chart >= 0
    lc = loop_chart[inside]
    position = points[vertex[inside]]
    u = np.sum(position * e1[lc], axis=1)
    v = np.sum(position * e2[lc], axis=1)
    # Rotate each chart to its principal axes for a tight bounding box.
    n = np.bincount(lc, minlength=len(axes)).astype(np.float64)
    mu = np.bincount(lc, u, len(axes)) / np.maximum(n, 1)
    mv = np.bincount(lc, v, len(axes)) / np.maximum(n, 1)
    du, dv = u - mu[lc], v - mv[lc]
    cuu = np.bincount(lc, du * du, len(axes))
    cvv = np.bincount(lc, dv * dv, len(axes))
    cuv = np.bincount(lc, du * dv, len(axes))
    theta = 0.5 * np.arctan2(2.0 * cuv, cuu - cvv)
    cos_t, sin_t = np.cos(theta)[lc], np.sin(theta)[lc]
    ru, rv = cos_t * du + sin_t * dv, -sin_t * du + cos_t * dv
    low = np.full((len(axes), 2), np.inf)
    high = np.full((len(axes), 2), -np.inf)
    np.minimum.at(low, (lc, 0), ru)
    np.minimum.at(low, (lc, 1), rv)
    np.maximum.at(high, (lc, 0), ru)
    np.maximum.at(high, (lc, 1), rv)
    local = np.zeros((len(owner), 2))
    local[inside, 0] = ru - low[lc, 0]
    local[inside, 1] = rv - low[lc, 1]
    charts["local"] = local
    charts["sizes"] = high - low
    box = np.maximum(charts["sizes"][:, 0] * charts["sizes"][:, 1], 1e-30)
    charts["fill"] = np.minimum(np.bincount(chart_of[chart_of >= 0], charts["projected"][chart_of >= 0],
                                            len(axes)) / box, 1.0)
    return charts


def _shelf(widths: np.ndarray, heights: np.ndarray) -> Optional[np.ndarray]:
    """Shelf-pack rectangles (tallest first) into the unit square; (n, 2) origins or None."""
    order = np.argsort(-heights, kind="stable")
    w, h = widths[order], heights[order]
    if not len(w) or (w > 1.0).any():
        return None if len(w) else np.zeros((0, 2))
    cumulative = np.concatenate([[0.0], np.cumsum(w)])
    placed = np.zeros((len(w), 2))
    start, y = 0, 0.0
    while start < len(w):
        end = int(np.searchsorted(cumulative, cumulative[start] + 1.0 + 1e-12, side="right")) - 1
        end = max(end, start + 1)
        placed[start:end, 0] = cumulative[start:end] - cumulative[start]
        placed[start:end, 1] = y
        y += h[start]
        if y > 1.0 + 1e-12:
            return None
        start = end
    origins = np.zeros_like(placed)
    origins[order] = placed
    return origins


def _pack_charts(charts: Dict[str, Any], resolution: int, pad_texels: float,
                 min_extent_texels: float) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Scale every chart by one common factor (bisected to the largest that
    fits), stretch charts thinner than ``min_extent_texels`` up to it, pad
    each by ``pad_texels`` and shelf-pack.  Both proxies share the result
    loop for loop, so stretching never breaks the correspondence."""
    sizes = charts["sizes"]
    count = charts["count"]
    stats: Dict[str, Any] = {
        "layout": "connected charts (normals within %g deg of the seed, orthographic)" % CHART_ANGLE_DEG,
        "polygons": int(charts["polygons"]), "live_polygons": int((charts["chart"] >= 0).sum()),
        "charts": int(count), "surface_area": float(charts["total_area"]), "probe_resolution": resolution,
        "pad_texels": pad_texels, "scale": 0.0, "texels_per_unit": 0.0, "min_extent_texels": 0.0,
        "largest_chart_texels": [0, 0], "polygons_sampled_estimate": 0,
        "area_fraction_sampled_estimate": 0.0, "texel_area": None, "chart_texels": None,
    }
    if not count or charts["total_area"] <= 0.0:
        return None, stats
    pad = pad_texels / float(resolution)

    def attempt(scale: float, minimum: float) -> Optional[np.ndarray]:
        dims = np.maximum(sizes * scale, minimum)
        return _shelf(dims[:, 0] + 2 * pad, dims[:, 1] + 2 * pad)

    best = scale = minimum = None
    for minimum_texels in (min_extent_texels, 0.0):
        minimum = minimum_texels / float(resolution)
        low, high = 0.0, 1.0 / max(float(sizes.max()), 1e-12)
        best = attempt(low, minimum)
        if best is None:
            continue
        for _ in range(40):
            middle = 0.5 * (low + high)
            placed = attempt(middle, minimum)
            if placed is None:
                high = middle
            else:
                low, best = middle, placed
        scale = low
        break
    if best is None:
        return None, stats
    dims = np.maximum(sizes * scale, minimum)
    stretch = np.where(sizes > 0, dims / np.maximum(sizes, 1e-30), 0.0)
    loop_chart = charts["chart"][charts["owner"]]
    inside = loop_chart >= 0
    uv = np.zeros((len(loop_chart), 2), np.float32)
    lc = loop_chart[inside]
    uv[inside] = charts["local"][inside] * stretch[lc] + best[lc] + pad
    chart_of = charts["chart"]
    texel_area = np.where(chart_of >= 0, charts["projected"] * np.prod(stretch, axis=1)[
        np.maximum(chart_of, 0)] * resolution ** 2, 0.0)
    sampled = texel_area >= 1.0
    largest = int(np.argmax(dims[:, 0] * dims[:, 1]))
    stats.update(scale=float(scale), texels_per_unit=float(scale * resolution),
                 min_extent_texels=float(minimum * resolution),
                 largest_chart_texels=[round(float(dims[largest, 0] * resolution), 2),
                                       round(float(dims[largest, 1] * resolution), 2)],
                 polygons_sampled_estimate=int(sampled.sum()),
                 area_fraction_sampled_estimate=float(charts["areas"][sampled].sum() / charts["total_area"]),
                 texel_area=texel_area, chart_texels=dims * resolution)
    return uv, stats


def _layout_sufficient(charts: Dict[str, Any], stats: Dict[str, Any], tolerances: Dict[str, Any]) -> bool:
    """Prediction (geometry only, no bake): enough area lies in polygons of
    at least two texels inside charts large enough to hold an evaluable
    local window, and enough texels are covered."""
    rule = tolerances["coverage"]
    window = tolerances["local_window"]
    fraction_needed = tolerances["local_min_valid_fraction"]
    need = max(1.0, math.ceil(fraction_needed * window * window))
    texels = stats["chart_texels"]
    # A chart holds an evaluable window when it has room for 1.5x the valid
    # texels a window needs and is at least that window's valid width wide.
    roomy = ((texels[:, 0] * texels[:, 1] * charts["fill"] >= 1.5 * need)
             & (texels.min(axis=1) >= math.ceil(fraction_needed * window) + 1))
    chart_of = charts["chart"]
    good = (stats["texel_area"] >= 2.0) & (chart_of >= 0) & roomy[np.maximum(chart_of, 0)]
    fraction = float(charts["areas"][good].sum() / max(charts["total_area"], 1e-30))
    return (fraction >= rule["min_surface_area_fraction"]
            and float(stats["texel_area"].sum()) >= 2.0 * rule["min_surface_texels"])


def _needed_probe_resolution(charts: Dict[str, Any], resolution: int, pad_texels: float,
                             tolerances: Dict[str, Any]) -> Optional[int]:
    """Smallest resolution * 2**k (k >= 1, up to MAX_PROBE_RESOLUTION) whose
    layout ``_layout_sufficient`` predicts to be enough; None if none is."""
    candidate = resolution * 2
    while candidate <= MAX_PROBE_RESOLUTION:
        uv, stats = _pack_charts(charts, candidate, pad_texels, 2.0)
        if uv is not None and _layout_sufficient(charts, stats, tolerances):
            return candidate
        candidate *= 2
    return None


_GEOMETRIC_MATCH_LIMIT = 1_000_000


def _polygon_centres(points: np.ndarray, vertex: np.ndarray, totals: np.ndarray,
                     owner: np.ndarray) -> np.ndarray:
    corners = points[vertex]
    return np.stack([np.bincount(owner, corners[:, i], len(totals)) for i in range(3)], -1) / totals[:, None]


def _geometric_correspondence(source: Any, output: Any) -> Tuple[Optional[np.ndarray], str]:
    """Output loop -> source loop, by matching polygons corner for corner.

    For an output with the same polygons at the same world positions but a
    different polygon order and/or split vertices (the CROP_SPLIT join).  An
    output polygon matches a source polygon only when every corner lies within
    the topology tolerance in the same cyclic order (same winding); the match
    must be unique and one-to-one, otherwise there is no correspondence.
    """
    ms, mo = source.data, output.data
    if len(ms.polygons) != len(mo.polygons) or len(ms.loops) != len(mo.loops):
        return None, ("polygon/loop counts differ (%d/%d source vs %d/%d output)"
                      % (len(ms.polygons), len(ms.loops), len(mo.polygons), len(mo.loops)))
    if len(ms.polygons) > _GEOMETRIC_MATCH_LIMIT:
        return None, "more than %d polygons; geometric matching not attempted" % _GEOMETRIC_MATCH_LIMIT
    ps, po = _world_points(source), _world_points(output)
    if not len(ps) or not len(po) or not len(ms.polygons):
        return None, "no geometry to match"
    if not (np.isfinite(ps).all() and np.isfinite(po).all()):
        return None, "non-finite vertex positions"
    tolerance = 1e-5 * float(np.linalg.norm(ps.max(0) - ps.min(0))) + 1e-6
    vs, _, ss, ts, owner_s = _loop_tables(ms)
    vo, _, so, to, owner_o = _loop_tables(mo)
    tree = KDTree(len(ss))
    for index, centre in enumerate(_polygon_centres(ps, vs, ts, owner_s).tolist()):
        tree.insert(centre, index)
    tree.balance()
    corners_s, corners_o = ps[vs], po[vo]
    mapping = np.full(len(vo), -1, np.int64)
    used = np.zeros(len(ss), bool)
    for q, centre in enumerate(_polygon_centres(po, vo, to, owner_o).tolist()):
        n = int(to[q])
        mine = corners_o[so[q]:so[q] + n]
        matches = []
        for _co, p, _distance in tree.find_range(centre, 2.0 * tolerance):
            if ts[p] != n:
                continue
            theirs = corners_s[ss[p]:ss[p] + n]
            for r in np.flatnonzero(np.abs(theirs - mine[0]).max(axis=1) <= tolerance).tolist():
                if np.abs(np.roll(theirs, -r, axis=0) - mine).max() <= tolerance:
                    matches.append((p, r))
                    break
        if len(matches) != 1:
            return None, (("output polygon %d has no source polygon with the same corners and winding" % q)
                          if not matches else
                          ("output polygon %d matches %d coincident source polygons (ambiguous)"
                           % (q, len(matches))))
        p, r = matches[0]
        if used[p]:
            return None, "two output polygons match source polygon %d" % p
        used[p] = True
        mapping[so[q]:so[q] + n] = ss[p] + (np.arange(n) + r) % n
    return mapping, ""


def _loop_correspondence(source: Any, output: Any) -> Tuple[Optional[np.ndarray], str, str]:
    """(output loop -> source loop map or None, method, description/reason)."""
    mismatch = _topology_mismatch(source, output)
    if mismatch is None:
        return (np.arange(len(source.data.loops), dtype=np.int64), "loop-identical",
                "loop-identical topology; one probe UV per loop shared by both proxies")
    mapping, why = _geometric_correspondence(source, output)
    if mapping is not None:
        return (mapping, "geometric",
                "geometric: every output polygon has exactly one source polygon with the same corners "
                "in the same winding (topology differs: %s); each source loop's probe UV is copied to "
                "its output loop" % mismatch)
    return None, "unavailable", "%s; geometric matching failed: %s" % (mismatch, why)


def _apply_probe(mesh: Any, uv: np.ndarray) -> None:
    layer = mesh.uv_layers.new(name=PROBE_UV, do_init=False)
    if layer is None:
        raise RuntimeError("could not add a probe UV layer")
    layer.data.foreach_set("uv", uv.ravel())
    # Bake TARGET only: active_render keeps naming the authored texture UVs.
    mesh.uv_layers.active = mesh.uv_layers[PROBE_UV]
    mesh.update()


# ---------------------------------------------------------------- bake/render

def _bake(run: _Run, obj: Any, bake_type: str) -> None:
    scene = run.scene
    scene.cycles.bake_type = bake_type
    for other in scene.objects:
        other.hide_render = other != obj
        other.select_set(other == obj, view_layer=run.view_layer)
    run.view_layer.objects.active = obj
    with bpy.context.temp_override(scene=scene, view_layer=run.view_layer, active_object=obj,
                                   object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
        status = bpy.ops.object.bake(type=bake_type)
    run.bake_count += 1
    if "FINISHED" not in status:
        raise RuntimeError("Cycles %s bake returned %r" % (bake_type, status))


def _target_image(run: _Run, label: str) -> Any:
    size = run.probe_size
    image = run.track("images", bpy.data.images.new(run.name(label), size, size, alpha=True,
                                                     float_buffer=True))
    image.colorspace_settings.name = "Non-Color"
    image.pixels.foreach_set(np.full(size * size * 4, _SENTINEL, np.float32))
    return image


def _add_target(tree: Any, image: Any) -> None:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    for other in tree.nodes:
        other.select = False
    node.select = True
    tree.nodes.active = node


def _route(tree: Any, socket: Any, destination: Any) -> None:
    links = _links(socket)
    if links:
        tree.links.new(links[0].from_socket, destination)
        return
    value = socket.default_value
    if hasattr(value, "__len__"):
        rgb = tuple(float(v) for v in value)[:3]
        destination.default_value = (*rgb, 1.0) if len(destination.default_value) == 4 else rgb
    else:
        scalar = float(value)
        if hasattr(destination.default_value, "__len__"):
            destination.default_value = (scalar, scalar, scalar, 1.0)
        else:
            destination.default_value = scalar


# Principled inputs that stay EVALUATED in every instrumented copy.  Cycles
# returns an Image Texture's Color output premultiplied unless its Alpha output
# is used (measured: linear(sRGB(c) * a)); disconnecting the Principled would
# therefore change what Base Color reads whenever an image's alpha feeds Alpha,
# or a packed roughness/metal channel.
_LIVE_INPUTS = ("Base Color", "Metallic", "Roughness", "Alpha", "Normal")


def _keep_live(tree: Any, principled: Any, routed: Sequence[str], shader: Any) -> Any:
    """Return a shader equal to ``shader`` that still depends on every linked
    contract input not in ``routed``.

    Mix Shader(factor = sum(inputs) > 1e30, shader, black emission): the
    factor is 0 for every finite input, and a comparison against a runtime
    value cannot be constant-folded away, so the dependency survives Cycles'
    graph optimisation while the emitted value stays exactly ``shader``.
    """
    sources = [_links(principled.inputs[name])[0].from_socket for name in _LIVE_INPUTS
               if name not in routed and _links(principled.inputs[name])]
    if not sources:
        return shader
    total = None
    for source in sources:
        add = tree.nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.inputs[1].default_value = 0.0
        tree.links.new(source, add.inputs[0])
        if total is not None:
            tree.links.new(total, add.inputs[1])
        total = add.outputs[0]
    compare = tree.nodes.new("ShaderNodeMath")
    compare.operation = "GREATER_THAN"
    compare.inputs[1].default_value = 1e30
    tree.links.new(total, compare.inputs[0])
    black = tree.nodes.new("ShaderNodeEmission")
    black.inputs["Strength"].default_value = 0.0
    mix = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(compare.outputs[0], mix.inputs[0])
    tree.links.new(shader, mix.inputs[1])
    tree.links.new(black.outputs[0], mix.inputs[2])
    return mix.outputs[0]


def _normal_emission(tree: Any, principled: Any, emission: Any) -> None:
    """Emit the object-space shading normal the Principled receives, encoded
    0.5 * n + 0.5.  (A Cycles NORMAL bake returns a zero vector for a
    Principled with alpha < 1 and a Normal input, so it is not used here.)"""
    links = _links(principled.inputs["Normal"])
    if links:
        vector = links[0].from_socket
    else:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        vector = geometry.outputs["Normal"]
    transform = tree.nodes.new("ShaderNodeVectorTransform")
    transform.vector_type = "NORMAL"
    transform.convert_from = "WORLD"
    transform.convert_to = "OBJECT"
    tree.links.new(vector, transform.inputs[0])
    unit = tree.nodes.new("ShaderNodeVectorMath")
    unit.operation = "NORMALIZE"
    tree.links.new(transform.outputs[0], unit.inputs[0])
    encode = tree.nodes.new("ShaderNodeVectorMath")
    encode.operation = "MULTIPLY_ADD"
    encode.inputs[1].default_value = (0.5, 0.5, 0.5)
    encode.inputs[2].default_value = (0.5, 0.5, 0.5)
    tree.links.new(unit.outputs[0], encode.inputs[0])
    tree.links.new(encode.outputs[0], emission.inputs["Color"])


def _channel_material(run: _Run, material: Any, analysis: Dict[str, Any], channel: str,
                      target: Any) -> Any:
    """A private copy whose Cycles surface emits ``channel`` into ``target``."""
    if analysis["kind"] == "REFERENCE_MATERIALS":
        if channel == "normal":
            # Arbitrary graph: Cycles' own object-space NORMAL bake of the
            # actual material (checked for degenerate vectors afterwards).
            copy = run.track("materials", material.copy())
        else:
            name = run.settings["channel_references"][material.name][channel]
            copy = run.track("materials", bpy.data.materials[name].copy())
        _add_target(copy.node_tree, target)
        return copy
    copy = run.track("materials", material.copy())
    tree = copy.node_tree
    principled = tree.nodes[analysis["principled"]]
    output = tree.get_output_node("CYCLES")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    if channel == "color":
        routed: Tuple[str, ...] = ("Base Color",)
        _route(tree, principled.inputs["Base Color"], emission.inputs["Color"])
    elif channel == "normal":
        routed = ("Normal",)
        _normal_emission(tree, principled, emission)
    else:
        routed = ("Metallic", "Roughness", "Alpha")
        combine = tree.nodes.new("ShaderNodeCombineColor")
        combine.mode = "RGB"
        # Combine inputs are Float sockets, so a colour link receives Blender's
        # own implicit colour->float conversion, exactly as the Principled does.
        for index, name in enumerate(routed):
            _route(tree, principled.inputs[name], combine.inputs[index])
        tree.links.new(combine.outputs[0], emission.inputs["Color"])
    surface = _keep_live(tree, principled, routed, emission.outputs[0])
    for link in list(output.inputs["Surface"].links):
        tree.links.remove(link)
    tree.links.new(surface, output.inputs["Surface"])
    _add_target(tree, target)
    return copy


_MARKER = 1.0e4


def _emitter(run: _Run, target: Any, value: float, label: str) -> Any:
    material = run.track("materials", bpy.data.materials.new(run.name(label)))
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (value, value, value, 1.0)
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    _add_target(tree, target)
    return material


def _bake_side(run: _Run, proxy: Any, analyses: Dict[Any, Dict[str, Any]], side: str) -> Dict[str, np.ndarray]:
    """Bake colour, packed metal/rough/alpha and normal through the probe UVs.

    Principled-direct materials emit their own inputs; reference-material
    graphs bake their supplied emission references, and their normal comes
    from Cycles' native object-space NORMAL bake.  An object mixing both kinds
    gets an EMIT normal bake with marker texels for the reference slots and a
    NORMAL bake that fills exactly those texels.
    """
    size = run.probe_size
    originals = list(proxy.data.materials)
    used = set(_used_slot_indices(proxy))

    def analysis(material: Any) -> Dict[str, Any]:
        row = analyses.get(_pointer(material))
        if row is None:          # never let a pointer value reach the report text
            raise RuntimeError("no reference analysis for material %r used by the %s proxy"
                               % (material.name, side))
        return row

    def bake_once(label: str, bake_type: str, make: Any) -> np.ndarray:
        target = _target_image(run, "%s_%s" % (side, label))
        copies = []
        try:
            for index, material in enumerate(originals):
                if material is None or index not in used:
                    replacement = _emitter(run, target, 0.0, "unused_slot")
                else:
                    replacement = make(material, target)
                copies.append(replacement)
                proxy.data.materials[index] = replacement
            _bake(run, proxy, bake_type)
            buffer = np.empty(size * size * 4, np.float32)
            target.pixels.foreach_get(buffer)
            return buffer.reshape(size, size, 4).copy()
        finally:
            for index, material in enumerate(originals):
                proxy.data.materials[index] = material
            for copy in copies:
                if copy.users == 0:
                    bpy.data.materials.remove(copy)
            bpy.data.images.remove(target)

    result = {}
    for channel in ("color", "mra"):
        result[channel] = bake_once(channel, "EMIT", lambda m, t, c=channel: _channel_material(
            run, m, analysis(m), c, t))
    kinds = {analysis(originals[i])["kind"] for i in used if originals[i] is not None}
    if "REFERENCE_MATERIALS" not in kinds:
        result["normal"] = bake_once("normal", "EMIT", lambda m, t: _channel_material(
            run, m, analysis(m), "normal", t))
        result["normal_native"] = np.zeros((size, size), bool)
    elif kinds == {"REFERENCE_MATERIALS"}:
        result["normal"] = bake_once("normal", "NORMAL", lambda m, t: _channel_material(
            run, m, analysis(m), "normal", t))
        result["normal_native"] = result["normal"][..., 3] > _SENTINEL / 2
    else:
        emitted = bake_once("normal_emit", "EMIT", lambda m, t: (
            _emitter(run, t, _MARKER, "normal_marker") if analysis(m)["kind"] == "REFERENCE_MATERIALS"
            else _channel_material(run, m, analysis(m), "normal", t)))
        native = bake_once("normal_native", "NORMAL", lambda m, t: _channel_material(
            run, m, analysis(m), "normal", t))
        marker = emitted[..., 0] > _MARKER / 2
        result["normal"] = np.where(marker[..., None], native, emitted)
        result["normal_native"] = marker
    return result


def _bake_polygon_ids(run: _Run, proxy: Any) -> Optional[np.ndarray]:
    """Which polygon each probe texel samples, from Cycles' own rasterisation.

    A FACE float attribute holding the polygon index is emitted through every
    slot and baked once (1 sample, so no texel can blend two ids).  Returns
    (size, size) float ids with -1 for uncovered texels, or None when the
    polygon count exceeds float32's exact integer range.
    """
    mesh = proxy.data
    count = len(mesh.polygons)
    if count >= (1 << 24):
        return None
    size = run.probe_size
    attribute = mesh.attributes.new(POLYGON_ID_ATTRIBUTE, "FLOAT", "FACE")
    attribute.data.foreach_set("value", np.arange(count, dtype=np.float32))
    target = _target_image(run, "polygon_ids")
    material = run.track("materials", bpy.data.materials.new(run.name("polygon_ids")))
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    reader = tree.nodes.new("ShaderNodeAttribute")
    reader.attribute_type = "GEOMETRY"
    reader.attribute_name = POLYGON_ID_ATTRIBUTE
    tree.links.new(reader.outputs["Fac"], emission.inputs["Color"])
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    _add_target(tree, target)
    originals = list(mesh.materials)
    samples = run.scene.cycles.samples
    try:
        if originals:
            for index in range(len(originals)):
                mesh.materials[index] = material
        else:
            mesh.materials.append(material)
        run.scene.cycles.samples = 1
        _bake(run, proxy, "EMIT")
        buffer = np.empty(size * size * 4, np.float32)
        target.pixels.foreach_get(buffer)
        buffer = buffer.reshape(size, size, 4)
        return np.where(buffer[..., 3] > _SENTINEL / 2, buffer[..., 0], -1.0)
    finally:
        run.scene.cycles.samples = samples
        if originals:
            for index, original in enumerate(originals):
                mesh.materials[index] = original
        else:
            mesh.materials.clear()
        attribute = mesh.attributes.get(POLYGON_ID_ATTRIBUTE)
        if attribute is not None:
            mesh.attributes.remove(attribute)
        bpy.data.images.remove(target)


def _render_exr(run: _Run, path: str) -> np.ndarray:
    """One Cycles render of the private scene; linear RGBA, rows bottom-up."""
    scene = run.scene
    scene.render.filepath = path
    status = bpy.ops.render.render(write_still=True, scene=scene.name)
    run.render_count += 1
    if "FINISHED" not in status:
        raise RuntimeError("Cycles render returned %r" % (status,))
    if not os.path.isfile(path):
        raise RuntimeError("Cycles render did not write %s" % path)
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        buffer = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(buffer)
        return buffer.reshape(height, width, 4).copy()
    finally:
        bpy.data.images.remove(image)


def _framing(points: np.ndarray, view: str) -> Optional[Dict[str, Any]]:
    back, up = (np.array(v, np.float64) for v in _VIEW_AXES[view])
    right = np.cross(up, back)
    lo, hi = points.min(0), points.max(0)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    center = 0.5 * (lo + hi)
    rel = corners - center
    width = float(np.ptp(rel @ right))
    height = float(np.ptp(rel @ up))
    depth = float(np.ptp(rel @ back))
    scale = max(width, height) * 1.1
    diagonal = float(np.linalg.norm(hi - lo))
    if diagonal <= 1e-9:
        return None
    if scale <= 1e-9:
        scale = diagonal * 1.1
    distance = 0.5 * depth + scale + 1.0
    rotation = Matrix((tuple(right), tuple(up), tuple(back))).transposed()
    return {"center": center, "back": back, "right": right, "up": up, "scale": scale,
            "location": center + back * distance, "rotation": rotation,
            "clip_end": distance + depth + scale + 1.0}


def _sun_rotation(direction: np.ndarray) -> Matrix:
    z = Vector(tuple(direction)).normalized()
    helper = Vector((0.0, 0.0, 1.0)) if abs(z.z) < 0.9 else Vector((1.0, 0.0, 0.0))
    x = helper.cross(z).normalized()
    y = z.cross(x)
    return Matrix((tuple(x), tuple(y), tuple(z))).transposed()


# --------------------------------------------------------------------- metrics

def _local_max(error: np.ndarray, valid: np.ndarray, window: int, min_fraction: float
               ) -> Tuple[Optional[float], int, float]:
    """(largest k x k window mean error, evaluable windows, fraction of the
    valid texels that lie inside at least one evaluable window)."""
    height, width = error.shape
    k = min(window, height, width)
    values = np.where(valid, error, 0.0).astype(np.float64)
    counts = valid.astype(np.float64)
    s = np.pad(values, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    c = np.pad(counts, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    sums = s[k:, k:] - s[:-k, k:] - s[k:, :-k] + s[:-k, :-k]
    cnts = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    ok = cnts >= max(1.0, math.ceil(min_fraction * k * k))
    if not ok.any():
        return None, 0, 0.0
    # Texel (y, x) is inside window (i, j) for i in [y-k+1, y], j in [x-k+1, x].
    w = np.pad(ok.astype(np.int64), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    rows, cols = np.arange(height), np.arange(width)
    r0, r1 = np.clip(rows - k + 1, 0, ok.shape[0] - 1), np.clip(rows, 0, ok.shape[0] - 1)
    c0, c1 = np.clip(cols - k + 1, 0, ok.shape[1] - 1), np.clip(cols, 0, ok.shape[1] - 1)
    inside = (w[np.ix_(r1 + 1, c1 + 1)] - w[np.ix_(r0, c1 + 1)] - w[np.ix_(r1 + 1, c0)]
              + w[np.ix_(r0, c0)]) > 0
    total = int(valid.sum())
    covered = float((inside & valid).sum()) / total if total else 0.0
    return float((sums[ok] / cnts[ok]).max()), int(ok.sum()), covered


def _metrics(reference: np.ndarray, output: np.ndarray, valid: np.ndarray,
             thresholds: Dict[str, float], tolerances: Dict[str, Any]) -> Tuple[Dict[str, Any], np.ndarray]:
    error = np.abs(reference.astype(np.float64) - output.astype(np.float64))
    if error.ndim == 3:
        error = error.max(axis=-1)
    error = np.where(valid, error, 0.0)
    row: Dict[str, Any] = {"pixels": int(valid.sum()), "thresholds": dict(thresholds)}
    if not row["pixels"]:
        row.update({"mean_error": None, "outlier_fraction": None, "local_max": None,
                    "local_region_evaluated": False, "local_window_coverage": 0.0,
                    "failed_checks": [], "pass": None, "note": "no valid pixels: nothing compared"})
        return row, error
    values = error[valid]
    local, windows, window_coverage = _local_max(error, valid, tolerances["local_window"],
                                                 tolerances["local_min_valid_fraction"])
    row.update({
        "mean_error": round(float(values.mean()), 7),
        "outlier_fraction": round(float((values > thresholds["outlier_threshold"]).mean()), 7),
        "local_max": None if local is None else round(local, 7),
        "local_windows": windows,
        "local_window_coverage": round(window_coverage, 7),
        "p95": round(float(np.percentile(values, 95)), 7),
        "p99": round(float(np.percentile(values, 99)), 7),
        "max_error": round(float(values.max()), 7),
        "reference_mean": round(float(reference[valid].mean()), 7),
        "output_mean": round(float(output[valid].mean()), 7),
    })
    failed = []
    if row["mean_error"] > thresholds["mean"]:
        failed.append("mean")
    if row["outlier_fraction"] > thresholds["outlier_fraction"]:
        failed.append("outlier_fraction")
    if local is not None and row["local_max"] > thresholds["local_max"]:
        failed.append("local_max")
    row["local_region_evaluated"] = local is not None
    row["failed_checks"] = failed
    row["pass"] = not failed
    return row, error


def _difference_png(error: np.ndarray, classes: np.ndarray, thresholds: Dict[str, float]) -> np.ndarray:
    """classes: 0 uncovered, 1 interior, 2 edge, 3 mismatch, 4 invalid."""
    scale = 2.0 * max(thresholds["local_max"], 1e-6)
    grey = np.clip(error / scale, 0.0, 1.0)
    rgb = np.stack([grey, grey, grey], -1)
    rgb[error > thresholds["outlier_threshold"]] = (1.0, 0.15, 0.15)
    rgb[classes == 0] = (0.08, 0.08, 0.25)
    rgb[classes == 3] = (1.0, 0.0, 1.0)
    rgb[classes == 4] = (1.0, 1.0, 0.0)
    return _to_u8(rgb)[::-1]


def _mask_png(classes: np.ndarray) -> np.ndarray:
    palette = np.array([(0, 0, 0), (255, 255, 255), (128, 128, 128), (255, 0, 255), (255, 255, 0)], np.uint8)
    return palette[classes][::-1]


def _display_rgba(rgba: np.ndarray, encode: bool) -> np.ndarray:
    alpha = np.clip(np.nan_to_num(rgba[..., 3]), 0.0, 1.0)
    rgb = rgba[..., :3].astype(np.float64)
    safe = np.where(alpha > 1e-6, alpha, 1.0)[..., None]
    rgb = np.where(alpha[..., None] > 1e-6, rgb / safe, 0.0)
    rgb = _srgb_encode(rgb) if encode else np.clip(rgb, 0.0, 1.0)
    out = np.concatenate([rgb, alpha[..., None]], -1)
    return _to_u8(out)[::-1]


# -------------------------------------------------------------------- layers

def _surface_layer(run: _Run, report: Dict[str, Any], proxies: Dict[str, Any],
                   analyses: Dict[str, Dict[int, Dict[str, Any]]], infos: Dict[str, Any]) -> None:
    coverage = report["coverage"]["surface"]
    findings = report["findings"]
    source, output = proxies["source"], proxies["output"]
    unsupported = {}
    kinds = {"source": {}, "output": {}}
    for side in ("source", "output"):
        for analysis in analyses[side].values():
            name = analysis["material"] or "<empty>"
            kinds[side][name] = analysis["kind"]
            if analysis["surface_issues"]:
                unsupported[name] = analysis["surface_issues"]
    coverage["reference_kinds"] = kinds
    coverage["unsupported_materials"] = unsupported
    for side in ("source", "output"):
        if infos[side].get("unevaluated_modifiers"):
            unsupported.setdefault("<%s modifiers>" % side, []).append({
                "code": "MODIFIERS_NOT_EVALUATED", "node": "", "node_instance": "",
                "message": "the %s object's modifiers could not be evaluated" % side})
    correspondence, method, description = _loop_correspondence(source, output)
    coverage["correspondence_method"] = method
    if correspondence is None:
        coverage.update(status="NOT_AVAILABLE", reason=description)
        findings.append(_finding("SURFACE_CORRESPONDENCE_UNAVAILABLE", INSUFFICIENT_COVERAGE, "surface", "mesh",
                                 "no shared surface correspondence: %s. Only fixed-camera renders were "
                                 "compared; the skipped loop correspondence proves nothing." % description))
        return
    if unsupported:
        coverage.update(status=UNSUPPORTED_REFERENCE,
                        reason="no independent channel reference for: " + ", ".join(sorted(unsupported)))
        findings.append(_finding(
            "UNSUPPORTED_REFERENCE", UNSUPPORTED_REFERENCE, "surface", ", ".join(sorted(unsupported)),
            "surface channels not compared: " + "; ".join(
                "%s: %s" % (name, ", ".join(sorted({i["code"] for i in issues})))
                for name, issues in sorted(unsupported.items()))))
        return
    for side, proxy in (("source", source), ("output", output)):
        if len(proxy.data.uv_layers) == 0:
            reason = ("%s mesh has no authored UV layer; a probe layer would become the texture "
                      "sampling layer and change what is compared" % side)
        elif len(proxy.data.uv_layers) >= _MAX_UV_LAYERS:
            reason = "%s mesh already has %d UV layers; no room for a probe layout" % (side, _MAX_UV_LAYERS)
        else:
            continue
        coverage.update(status="NOT_AVAILABLE", reason=reason)
        findings.append(_finding("SURFACE_CORRESPONDENCE_UNAVAILABLE", INSUFFICIENT_COVERAGE,
                                 "surface", side, reason))
        return
    tolerances = run.settings["tolerances"]
    rule = tolerances["coverage"]
    # Gaps between charts are at least one local window wide, so no window
    # mixes two unrelated pieces of surface.
    pad = float(max(2, math.ceil(tolerances["local_window"] / 2.0)))
    charts = _probe_charts(source)
    if run.settings["probe_resolution"] == "auto":
        size = _auto_probe_resolution(charts, pad, tolerances)
        coverage["probe_resolution_rule"] = (
            "auto: the smallest of %s that the chart layout predicts to be enough (else the largest)"
            % ", ".join(str(r) for r in AUTO_PROBE_RESOLUTIONS))
    else:
        size = run.settings["probe_resolution"]
        coverage["probe_resolution_rule"] = "fixed by settings.probe_resolution"
    run.probe_size = size
    uv, layout = _pack_charts(charts, size, pad, 2.0)
    coverage.update({key: value for key, value in layout.items() if key not in ("texel_area", "chart_texels")})
    coverage["probe_texels_total"] = size * size
    coverage["probe_resolution_needed"] = None
    coverage["correspondence"] = description
    coverage["uv_roles"] = {side: {"texture_uv": infos[side]["texture_uv"], "bake_target_uv": PROBE_UV}
                            for side in ("source", "output")}
    if charts["total_area"] <= 0.0 or not charts["count"]:
        reason = ("the surface has no measurable area: all %d polygons are degenerate (zero area or "
                  "non-finite corners)" % charts["polygons"])
        coverage.update(status="NOT_AVAILABLE", reason=reason, probe_texels_covered=0)
        findings.append(_finding("SURFACE_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "surface", "mesh", reason))
        return
    if uv is None:
        needed = _needed_probe_resolution(charts, size, pad, tolerances)
        coverage["probe_resolution_needed"] = needed
        reason = ("the probe layout cannot place %d charts (%d polygons) at probe_resolution %d even at its "
                  "minimum cell size of %g padded texels; %s"
                  % (charts["count"], charts["polygons"], size, 2 * pad, _raise_hint(needed)))
        coverage.update(status="NOT_AVAILABLE", reason=reason, probe_texels_covered=0)
        findings.append(_finding("SURFACE_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "surface", "mesh", reason))
        return
    for side, proxy in (("source", source), ("output", output)):
        # The output's loops take the probe UV of their corresponding source loop.
        _apply_probe(proxy.data, uv if side == "source" else uv[correspondence])
    for side in ("source", "output"):
        texture_uv = _render_uv_name(proxies[side].data)
        if texture_uv != infos[side]["texture_uv"] or texture_uv == PROBE_UV:
            raise RuntimeError("probe layout changed the %s texture UV layer" % side)
    ids = _bake_polygon_ids(run, source)
    bakes = {side: _bake_side(run, proxies[side], analyses[side], side) for side in ("source", "output")}
    images = ("color", "mra", "normal")
    covered = {side: np.all([bakes[side][k][..., 3] > _SENTINEL / 2 for k in images], axis=0)
               for side in bakes}
    both = covered["source"] & covered["output"]
    either = covered["source"] | covered["output"]
    finite = np.ones_like(both)
    for side in bakes:
        for key in images:
            finite &= np.isfinite(bakes[side][key]).all(-1)
    valid = both & finite
    # A native NORMAL bake can return a zero vector (e.g. transparency plus a
    # custom normal); such texels are not a normal reference.
    degenerate = np.zeros_like(both)
    for side in bakes:
        length = np.linalg.norm(bakes[side]["normal"][..., :3] * 2.0 - 1.0, axis=-1)
        degenerate |= bakes[side]["normal_native"] & both & (length < 0.5)
    coverage["normal_reference_degenerate_texels"] = int(degenerate.sum())
    if degenerate.any():
        findings.append(_finding(
            "UNSUPPORTED_REFERENCE", UNSUPPORTED_REFERENCE, "surface", "normal",
            "Cycles' NORMAL bake returned %d degenerate (near-zero) normals for reference-material "
            "graphs; the normal channel is not compared there" % int(degenerate.sum())))
    classes = np.zeros(both.shape, np.int8)
    classes[both] = 1
    classes[either & ~both] = 3
    classes[either & ~finite] = 4
    coverage["probe_texels_covered"] = int(both.sum())
    coverage["coverage_mismatch_texels"] = int((either & ~both).sum())
    coverage["invalid_texels"] = int((either & ~finite).sum())
    _measure_sampling(coverage, findings, charts, layout, ids, valid)
    rel = "surface/mask.png"
    _write_bytes(run.artifact(rel, "surface", "mask", "coverage"), _png_bytes(_mask_png(classes)))
    if coverage["coverage_mismatch_texels"]:
        findings.append(_finding("SURFACE_COVERAGE_MISMATCH", FAIL, "surface", "mesh",
                                 "%d probe texels were baked on only one side"
                                 % coverage["coverage_mismatch_texels"]))
    if coverage["invalid_texels"]:
        findings.append(_finding("INVALID_PIXELS", FAIL, "surface", "mesh",
                                 "%d probe texels hold non-finite values" % coverage["invalid_texels"]))
    short = []
    if coverage["probe_texels_covered"] < rule["min_surface_texels"]:
        short.append("%d covered texels < %d" % (coverage["probe_texels_covered"], rule["min_surface_texels"]))
    if coverage["area_fraction_sampled"] < rule["min_surface_area_fraction"]:
        short.append("%d of %d polygons (%.2f%% of the surface area) received no compared probe texel at "
                     "probe_resolution %d (sampled area fraction %.4f < %.4f)"
                     % (charts["polygons"] - coverage["polygons_sampled"], charts["polygons"],
                        100.0 * (1.0 - coverage["area_fraction_sampled"]), size,
                        coverage["area_fraction_sampled"], rule["min_surface_area_fraction"]))
    channels = {
        "color": lambda b: b["color"][..., :3], "metal": lambda b: b["mra"][..., 0:1],
        "rough": lambda b: b["mra"][..., 1:2], "alpha": lambda b: b["mra"][..., 2:3],
        "normal": lambda b: b["normal"][..., :3],
    }
    metrics = report["metrics"].setdefault("surface", {})
    unreliable_normal = sorted({a["material"] for side in analyses for a in analyses[side].values()
                                if a["kind"] == "REFERENCE_MATERIALS" and not a["native_normal_reliable"]})
    coverage["closure_estimates"] = {side: {(a["material"] or "<empty>"): a["closure_estimate"]
                                            for a in analyses[side].values()} for side in analyses}
    window = tolerances["local_window"]
    for channel in SURFACE_CHANNELS:
        ref, out = channels[channel](bakes["source"]), channels[channel](bakes["output"])
        thresholds = tolerances["surface"][channel]
        row, error = _metrics(ref, out, valid & ~degenerate if channel == "normal" else valid,
                              thresholds, tolerances)
        row["method"] = {side: sorted({a["kind"] for a in analyses[side].values()})
                         for side in ("source", "output")}
        if channel == "normal":
            row["method"] = {}
            for side in ("source", "output"):
                native = int((bakes[side]["normal_native"] & both).sum())
                row["method"][side] = ("Cycles NORMAL bake" if native == int(both.sum()) else
                                       "emitted Principled Normal input" if native == 0 else
                                       "mixed: %d of %d texels from a NORMAL bake" % (native, int(both.sum())))
        metrics[channel] = row
        encode = channel == "color"
        for role, data in (("reference", ref), ("output", out)):
            rgba = np.concatenate([np.repeat(data, 3, -1) if data.shape[-1] == 1 else data,
                                   valid[..., None].astype(np.float32)], -1)
            path = run.artifact("surface/%s_%s.png" % (channel, role), "surface", role, channel)
            _write_bytes(path, _png_bytes(_display_rgba(rgba, encode)))
        path = run.artifact("surface/%s_difference.png" % channel, "surface", "difference", channel)
        _write_bytes(path, _png_bytes(_difference_png(error, classes, thresholds)))
        if channel == "normal" and unreliable_normal:
            row["unsupported"] = ("native NORMAL bake of %s is not a reliable reference above the "
                                  "%d-closure budget" % (", ".join(unreliable_normal), CLOSURE_BUDGET))
            row["pass"] = None
            findings.append(_finding("UNSUPPORTED_REFERENCE", UNSUPPORTED_REFERENCE, "surface", "normal",
                                     row["unsupported"]))
        if row["pass"] is False:
            findings.append(_finding("METRIC_EXCEEDED", FAIL, "surface", channel,
                                     "%s failed %s (mean %.5f, outliers %.5f, local max %s)"
                                     % (channel, ", ".join(row["failed_checks"]), row["mean_error"],
                                        row["outlier_fraction"], row["local_max"])))
        if row["pass"] is not None and row["local_window_coverage"] < rule["min_surface_area_fraction"]:
            # The local-region maximum is the check that sees small defects;
            # texels outside every evaluable window were only averaged.
            short.append("%s: %.1f%% of the compared texels lie in an evaluable %dx%d local window "
                         "(need %.1f%%)" % (channel, 100.0 * row["local_window_coverage"], window, window,
                                            100.0 * rule["min_surface_area_fraction"]))
    if short:
        needed = _needed_probe_resolution(charts, size, pad, tolerances)
        coverage["probe_resolution_needed"] = needed
        findings.append(_finding("SURFACE_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "surface", "mesh",
                                 "%s; %s" % ("; ".join(short), _raise_hint(needed))))
        coverage["status"] = "INSUFFICIENT"
    else:
        coverage["status"] = "COMPARED"
    if coverage["probe_texels_covered"]:
        report["coverage"]["layers_compared"].append("surface")


def _raise_hint(needed: Optional[int]) -> str:
    if needed is None:
        return ("no probe_resolution up to %d is predicted to be enough for this many charts; this mesh "
                "cannot be surface-compared at the current tolerances" % MAX_PROBE_RESOLUTION)
    return ("raise probe_resolution: %d is predicted to be enough (a geometry-only estimate that may be "
            "conservative by one doubling)" % needed)


def _auto_probe_resolution(charts: Dict[str, Any], pad_texels: float, tolerances: Dict[str, Any]) -> int:
    for candidate in AUTO_PROBE_RESOLUTIONS:
        uv, stats = _pack_charts(charts, candidate, pad_texels, 2.0)
        if uv is not None and _layout_sufficient(charts, stats, tolerances):
            return candidate
    return AUTO_PROBE_RESOLUTIONS[-1]


def _measure_sampling(coverage: Dict[str, Any], findings: List[Dict[str, Any]], charts: Dict[str, Any],
                      layout: Dict[str, Any], ids: Optional[np.ndarray], valid: np.ndarray) -> None:
    """Record which polygons received a compared texel, from the polygon-ID bake."""
    if ids is None:
        coverage.update(polygons_sampled=layout["polygons_sampled_estimate"],
                        area_fraction_sampled=layout["area_fraction_sampled_estimate"],
                        sampling_measured_by="layout estimate")
        findings.append(_finding("SAMPLING_ESTIMATED", NOTE, "surface", "mesh",
                                 "%d polygons exceed float32's exact integer range; sampled polygons "
                                 "are a layout estimate, not a polygon-ID bake" % charts["polygons"]))
        return
    seen = ids[valid]
    exact = (seen >= 0) & (seen < charts["polygons"]) & (np.abs(seen - np.round(seen)) < 1e-3)
    sampled = np.unique(np.round(seen[exact]).astype(np.int64))
    coverage.update(polygons_sampled=int(len(sampled)),
                    area_fraction_sampled=float(charts["areas"][sampled].sum() / charts["total_area"]),
                    sampling_measured_by="polygon-ID bake", polygon_id_invalid_texels=int((~exact).sum()))
    if not exact.all():
        findings.append(_finding("POLYGON_ID_INEXACT", NOTE, "surface", "mesh",
                                 "%d compared texels held no exact polygon id; they are not counted as "
                                 "sampling any polygon" % int((~exact).sum())))


def _render_layer(run: _Run, report: Dict[str, Any], proxies: Dict[str, Any]) -> None:
    settings = run.settings
    tolerances = settings["tolerances"]
    rule = tolerances["coverage"]
    coverage = report["coverage"]["render"]
    findings = report["findings"]
    scene = run.scene
    points = np.concatenate([_world_points(proxies["source"]), _world_points(proxies["output"])])
    coverage.update(covered_views=[], views_without_coverage=[], interior_pixels={},
                    union_pixels={}, silhouette_mismatch_pixels={})
    coverage["lighting"] = {name: LIGHTING[name] for name in settings["lighting_presets"]}
    if not len(points):
        findings.append(_finding("RENDER_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "render", "mesh",
                                 "no geometry to render"))
        coverage["status"] = "INSUFFICIENT"
        return
    camera_data = run.track("cameras", bpy.data.cameras.new(run.name("camera")))
    camera_data.type = "ORTHO"
    camera = run.track("objects", bpy.data.objects.new(run.name("camera"), camera_data))
    scene.collection.objects.link(camera)
    scene.camera = camera
    sun_data = run.track("lights", bpy.data.lights.new(run.name("sun"), "SUN"))
    sun_data.angle = 0.0
    sun = run.track("objects", bpy.data.objects.new(run.name("sun"), sun_data))
    scene.collection.objects.link(sun)
    worlds = {}
    for label, strength in (("white", 1.0), ("black", 0.0)):
        world = run.track("worlds", bpy.data.worlds.new(run.name("world_" + label)))
        background = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
        background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        background.inputs["Strength"].default_value = strength
        worlds[label] = world
    cover = run.track("materials", bpy.data.materials.new(run.name("coverage")))
    tree = cover.node_tree
    tree.nodes.clear()
    cover_out = tree.nodes.new("ShaderNodeOutputMaterial")
    cover_emit = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(cover_emit.outputs[0], cover_out.inputs["Surface"])

    def solo(side: Optional[str]) -> None:
        for key, proxy in proxies.items():
            proxy.hide_render = key != side
        camera.hide_render = False

    def render(label: str) -> Tuple[np.ndarray, str]:
        path = os.path.join(run.temp_dir, label + ".exr")
        return _render_exr(run, path), path

    metrics = report["metrics"].setdefault("render", {})
    keep = settings["keep_linear"]
    for view in settings["views"]:
        frame = _framing(points, view)
        if frame is None:
            coverage["views_without_coverage"].append(view)
            continue
        camera.matrix_world = Matrix.Translation(Vector(tuple(frame["location"]))) @ frame["rotation"].to_4x4()
        camera_data.ortho_scale = frame["scale"]
        camera_data.clip_start = 1e-4 * frame["scale"]
        camera_data.clip_end = frame["clip_end"]
        tag = _VIEW_FILE[view]
        geo = {}
        scene.world = worlds["black"]
        sun.hide_render = True
        run.view_layer.material_override = cover
        try:
            for side in ("source", "output"):
                solo(side)
                geo[side] = render("%s_coverage_%s" % (tag, side))[0][..., 3]
        finally:
            run.view_layer.material_override = None
        silhouette = rule["silhouette_alpha"]
        union = (geo["source"] > silhouette) | (geo["output"] > silhouette)
        interior = (geo["source"] >= rule["interior_alpha"]) & (geo["output"] >= rule["interior_alpha"])
        mismatch = np.abs(geo["source"] - geo["output"]) > 0.5
        coverage["union_pixels"][view] = int(union.sum())
        coverage["interior_pixels"][view] = int(interior.sum())
        coverage["silhouette_mismatch_pixels"][view] = int(mismatch.sum())
        if not union.any():
            coverage["views_without_coverage"].append(view)
            continue
        if coverage["interior_pixels"][view] >= rule["min_view_interior_pixels"]:
            coverage["covered_views"].append(view)
        for preset in settings["lighting_presets"]:
            light = LIGHTING[preset]
            scene.world = worlds["white" if light["world"] > 0 else "black"]
            if light["sun_strength"] > 0:
                angle = math.radians(light["sun_angle_from_view_deg"])
                side_axis = frame["right"] if preset == "grazing" else frame["up"]
                sun.matrix_world = _sun_rotation(math.cos(angle) * frame["back"]
                                                 + math.sin(angle) * side_axis).to_4x4()
                sun_data.energy = light["sun_strength"]
                sun.hide_render = False
            else:
                sun.hide_render = True
            images, paths = {}, {}
            for side in ("source", "output"):
                solo(side)
                images[side], paths[side] = render("%s_%s_%s" % (tag, preset, side))
            ref, out = images["source"], images["output"]
            finite = np.isfinite(ref).all(-1) & np.isfinite(out).all(-1)
            valid = union & finite
            classes = np.zeros(union.shape, np.int8)
            classes[union] = 2
            classes[interior] = 1
            classes[union & mismatch] = 3
            classes[union & ~finite] = 4
            key = "%s/%s" % (view, preset)
            entry = {"coverage": {"union_pixels": int(union.sum()), "interior_pixels": int(interior.sum()),
                                  "silhouette_mismatch_pixels": int((union & mismatch).sum()),
                                  "invalid_pixels": int((union & ~finite).sum()),
                                  "covered": view in coverage["covered_views"]}}
            base = "render/%s_%s" % (tag, preset)
            for channel, ref_data, out_data in (("color", ref[..., :3], out[..., :3]),
                                                ("alpha", ref[..., 3:4], out[..., 3:4])):
                thresholds = tolerances["render"][channel]
                row, error = _metrics(ref_data, out_data, valid, thresholds, tolerances)
                entry[channel] = row
                path = run.artifact("%s_%s_difference.png" % (base, channel), "render", "difference",
                                    key + "/" + channel)
                _write_bytes(path, _png_bytes(_difference_png(error, classes, thresholds)))
                if row["pass"] is False:
                    findings.append(_finding("METRIC_EXCEEDED", FAIL, "render", key + "/" + channel,
                                             "%s %s failed %s (mean %.5f, outliers %.5f, local max %s)"
                                             % (key, channel, ", ".join(row["failed_checks"]),
                                                row["mean_error"], row["outlier_fraction"], row["local_max"])))
            metrics[key] = entry
            if entry["coverage"]["invalid_pixels"]:
                findings.append(_finding("INVALID_PIXELS", FAIL, "render", key,
                                         "%d covered pixels are non-finite" % entry["coverage"]["invalid_pixels"]))
            for role, side in (("reference", "source"), ("output", "output")):
                path = run.artifact("%s_%s.png" % (base, role), "render", role, key)
                _write_bytes(path, _png_bytes(_display_rgba(images[side], True)))
                if keep:
                    rel = "render/linear/%s_%s_%s.exr" % (tag, preset, role)
                    shutil.copyfile(paths[side], run.artifact(rel, "render", "linear", key))
            _write_bytes(run.artifact("%s_mask.png" % base, "render", "mask", key), _png_bytes(_mask_png(classes)))
    insufficient = False
    if len(coverage["covered_views"]) < rule["min_covered_views"]:
        findings.append(_finding(
            "RENDER_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "render", "views",
            "%d of %d views have at least %d interior pixels (need %d views); views without any "
            "coverage: %s" % (len(coverage["covered_views"]), len(settings["views"]),
                              rule["min_view_interior_pixels"], rule["min_covered_views"],
                              ", ".join(coverage["views_without_coverage"]) or "none")))
        insufficient = True
    gaps = _render_local_gaps(metrics)
    coverage["local_region_unevaluated"] = gaps
    if gaps:
        # Mirrors the surface rule: a covered view judged on mean and outliers
        # alone (a sparse silhouette) is not a sufficient comparison.
        findings.append(_finding(
            "RENDER_COVERAGE_INSUFFICIENT", INSUFFICIENT_COVERAGE, "render", ", ".join(gaps),
            "the local-region maximum could not be evaluated in covered view(s) %s; raise resolution"
            % ", ".join(gaps)))
        insufficient = True
    coverage["status"] = "INSUFFICIENT" if insufficient else "COMPARED"
    if metrics:
        report["coverage"]["layers_compared"].append("render")


def _render_local_gaps(metrics: Dict[str, Any]) -> List[str]:
    """``view/preset/channel`` keys of COVERED views whose compared rows have
    no evaluable local window."""
    gaps = []
    for key in sorted(metrics):
        entry = metrics[key]
        if not (entry.get("coverage") or {}).get("covered"):
            continue
        for channel in RENDER_CHANNELS:
            row = entry.get(channel) or {}
            if row.get("pass") is not None and not row.get("local_region_evaluated"):
                gaps.append("%s/%s" % (key, channel))
    return gaps


# ------------------------------------------------------------------- verdicts

def derive_status(report: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """Recompute the verdict from a report's own content.

    PASS requires: a well-formed report, no errors, no non-NOTE finding (a
    finding whose class is not a known verdict class counts as FAIL), no
    failing metric, at least one compared layer, every requested layer either
    compared or explained by a finding, and reference/output/difference/mask
    artifacts for every compared layer.  Never raises: a malformed report is
    REPORT_MALFORMED (FAIL).
    """
    reasons: List[Dict[str, Any]] = []

    def malformed(where: str, what: str) -> None:
        reasons.append(_finding("REPORT_MALFORMED", FAIL, "report", where, what))

    def typed(container: Any, key: str, kind: type, default: Any) -> Any:
        value = container.get(key, default) if isinstance(container, dict) else default
        if value is None:
            value = default
        if not isinstance(value, kind):
            malformed(key, "%s is a %s, not a %s" % (key, type(value).__name__, kind.__name__))
            return default
        return value

    if not isinstance(report, dict):
        malformed("report", "the report is a %s, not an object" % type(report).__name__)
        return FAIL, reasons
    for error in typed(report, "errors", list, []):
        if not isinstance(error, dict):
            malformed("errors", "an errors entry is not an object: %s" % _text(error, 120))
            continue
        stage = _text(error.get("stage", "?"), 80)
        reasons.append(_finding("ERROR", FAIL, stage, stage, _text(error.get("message", ""), 500)))
    for row in typed(report, "findings", list, []):
        if not isinstance(row, dict):
            malformed("findings", "a findings entry is not an object: %s" % _text(row, 120))
            continue
        klass = row.get("class")
        if klass == NOTE:
            continue
        reasons.append(row)
        if klass not in _STATUS_PRECEDENCE:
            reasons.append(_finding("FINDING_CLASS_UNKNOWN", FAIL, _text(row.get("layer", "report"), 80),
                                    _text(row.get("code", "?"), 120),
                                    "finding %s has class %r, which is not a verdict class; it counts as FAIL"
                                    % (_text(row.get("code", "?"), 120), klass)))
    metric_subjects = {(r.get("layer"), r.get("subject")) for r in reasons if r.get("code") == "METRIC_EXCEEDED"}
    metrics = typed(report, "metrics", dict, {})
    for channel, row in typed(metrics, "surface", dict, {}).items():
        if not isinstance(row, dict):
            malformed("metrics.surface", "metrics.surface.%s is not an object" % _text(channel, 60))
        elif row.get("pass") is False and ("surface", channel) not in metric_subjects:
            reasons.append(_finding("METRIC_EXCEEDED", FAIL, "surface", channel, "metric marked failed"))
    for key, entry in typed(metrics, "render", dict, {}).items():
        if not isinstance(entry, dict):
            malformed("metrics.render", "metrics.render.%s is not an object" % _text(key, 60))
            continue
        for channel in RENDER_CHANNELS:
            row = entry.get(channel)
            subject = "%s/%s" % (key, channel)
            if row is not None and not isinstance(row, dict):
                malformed("metrics.render", "metrics.render.%s is not an object" % _text(subject, 80))
            elif isinstance(row, dict) and row.get("pass") is False and ("render", subject) not in metric_subjects:
                reasons.append(_finding("METRIC_EXCEEDED", FAIL, "render", subject, "metric marked failed"))
    coverage = typed(report, "coverage", dict, {})
    compared = typed(coverage, "layers_compared", list, [])
    if "layers_requested" not in coverage:
        malformed("coverage", "coverage.layers_requested is missing")
    requested = typed(coverage, "layers_requested", list, [])
    for label, names in (("layers_compared", compared), ("layers_requested", requested)):
        if any(name not in LAYERS for name in names):
            malformed("coverage", "coverage.%s has unknown layers %s" % (label, _text(names, 120)))
    compared = [name for name in compared if name in LAYERS]
    if not compared:
        reasons.append(_finding("NOTHING_COMPARED", INSUFFICIENT_COVERAGE, "report", "layers",
                                "no layer was compared, so nothing can be called a visual match"))
    explained = {row.get("layer") for row in reasons}
    for layer in requested:
        if layer in LAYERS and layer not in compared and layer not in explained:
            reasons.append(_finding("LAYER_NOT_COMPARED", INSUFFICIENT_COVERAGE, layer, layer,
                                    "the %s layer was requested but was neither compared nor explained by a "
                                    "finding" % layer))
    artifacts = typed(report, "artifacts", dict, {})
    rows = [row for row in artifacts.values() if isinstance(row, dict)]
    if len(rows) != len(artifacts):
        malformed("artifacts", "%d artifact entries are not objects" % (len(artifacts) - len(rows)))
    for layer in compared:
        roles = {row.get("role") for row in rows if row.get("layer") == layer}
        missing = [role for role in REQUIRED_ROLES if role not in roles]
        if missing:
            reasons.append(_finding("ARTIFACT_MISSING", FAIL, layer, layer,
                                    "no %s artifact for the compared %s layer" % ("/".join(missing), layer)))
    for problem in typed(report, "artifact_problems", list, []):
        problem = _text(problem, 300)
        reasons.append(_finding(problem.split(":", 1)[0], FAIL, "artifacts", problem, problem))
    classes = {row.get("class") for row in reasons}
    status = next((klass for klass in _STATUS_PRECEDENCE if klass in classes), PASS)
    unique, seen = [], set()
    for row in reasons:
        marker = json.dumps(row, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(row)
    return status, unique


def _confined(root: str, rel: Any) -> Optional[str]:
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or ":" in rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    path = os.path.join(root, *parts)
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if os.path.commonpath([real_root, real]) != real_root:
        return None
    return path


def _verify_artifacts(root: str, artifacts: Dict[str, Any]) -> List[str]:
    problems = []
    for rel, row in sorted(artifacts.items(), key=lambda item: _text(item[0])):
        if not isinstance(row, dict):
            problems.append("REPORT_MALFORMED: artifact entry %s is not an object" % _text(rel, 200))
            continue
        path = _confined(root, rel)
        if path is None:
            problems.append("ARTIFACT_PATH_OUTSIDE_REPORT: %s" % _text(rel, 200))
            continue
        if not os.path.isfile(path):
            problems.append("ARTIFACT_MISSING: %s" % rel)
            continue
        digest, size = _sha256(path)
        if digest != row.get("sha256") or size != row.get("bytes"):
            problems.append("ARTIFACT_HASH_MISMATCH: %s" % rel)
    return problems


def inspect_report(destination: str) -> Dict[str, Any]:
    """Re-verify a published report from its bytes on disk.  Never raises for
    report content: a malformed report is a REPORT_MALFORMED FAIL."""
    root = os.path.abspath(os.fspath(destination))
    problems: List[str] = []
    path = os.path.join(root, REPORT_NAME)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        report = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "status": FAIL, "recorded_status": None,
                "problems": ["REPORT_UNREADABLE: %s" % _text(exc, 300)], "reasons": []}
    if not isinstance(report, dict):
        problems.append("REPORT_MALFORMED: the report is a %s, not an object" % type(report).__name__)
        report = {}
    elif report.get("schema_version") != SCHEMA_VERSION:
        problems.append("SCHEMA_UNSUPPORTED: %r" % (report.get("schema_version"),))
    hash_path = os.path.join(root, REPORT_HASH_NAME)
    if os.path.isfile(hash_path):
        with open(hash_path, "r", encoding="ascii", errors="replace") as handle:
            words = handle.read().split()
        if not words or hashlib.sha256(raw).hexdigest() != words[0]:
            problems.append("REPORT_HASH_MISMATCH: %s" % REPORT_NAME)
    else:
        problems.append("REPORT_HASH_MISSING: %s" % REPORT_HASH_NAME)
    artifacts = report.get("artifacts")
    problems.extend(_verify_artifacts(root, artifacts if isinstance(artifacts, dict) else {}))
    checked = dict(report)
    checked["artifact_problems"] = [p for p in problems if p.startswith("ARTIFACT_")]
    try:
        status, reasons = derive_status(checked)
    except Exception as exc:                       # belt and braces: never raise
        status, reasons = FAIL, [_finding("REPORT_MALFORMED", FAIL, "report", "report", _text(exc, 300))]
    problems.extend("REPORT_MALFORMED: %s" % row.get("message", "") for row in reasons
                    if row.get("code") == "REPORT_MALFORMED")
    if problems:
        status = FAIL
    recorded = report.get("status")
    if recorded != status:
        problems.append("STATUS_INCONSISTENT: recorded %s, derived %s" % (_text(recorded, 60), status))
    return {"ok": not problems, "status": status, "recorded_status": recorded,
            "problems": problems, "reasons": reasons}


# ------------------------------------------------------------ fingerprinting

_ATTRIBUTE_FIELDS = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32), "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, bool), "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT2": ("vector", 2, np.float32), "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32), "INT32_2D": ("value", 2, np.int32),
    "INT16_2D": ("value", 2, np.int32), "QUATERNION": ("value", 4, np.float32),
    "FLOAT4X4": ("value", 16, np.float32),
}
_TREE_EXCLUDED = {"rna_type", "name", "label", "location", "location_absolute", "width", "width_hidden",
                  "height", "dimensions", "select", "show_options", "show_preview", "show_texture",
                  "parent", "inputs", "outputs", "internal_links", "node_tree", "image", "color",
                  "use_custom_color", "hide", "warning_propagation", "bl_idname", "bl_label",
                  "bl_description", "bl_icon", "bl_static_type", "bl_width_default", "bl_width_min",
                  "bl_width_max", "bl_height_default", "bl_height_min", "bl_height_max", "type"}


def _token(hasher: Any, *values: Any) -> None:
    for value in values:
        data = _text(value, 1 << 20).encode("utf-8", "surrogatepass")
        hasher.update(struct.pack(">Q", len(data)))
        hasher.update(data)


def _simple(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "bl_rna") and hasattr(value, "name"):
        return [type(value).__name__, value.name]
    try:
        items = list(value)
    except (TypeError, ValueError, ReferenceError):
        return None
    if len(items) <= 32 and all(isinstance(v, (bool, int, float, str)) for v in items):
        return items
    return None


def _image_digest(hasher: Any, image: Any) -> None:
    _token(hasher, "image", image.name, image.source, image.filepath,
           image.colorspace_settings.name, getattr(image, "alpha_mode", ""), tuple(image.size))
    if image.packed_file is not None:
        _token(hasher, "packed", hashlib.sha256(image.packed_file.data).hexdigest())
        return
    path = (os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))
            if image.filepath else "")
    if image.source == "FILE" and path and os.path.isfile(path) and not image.is_dirty:
        _token(hasher, "file", _sha256(path)[0])
        return
    if image.source in ("GENERATED", "FILE") and image.size[0] * image.size[1] <= 16384 * 16384:
        pixels = np.empty(image.size[0] * image.size[1] * 4, np.float32)
        if len(pixels):
            image.pixels.foreach_get(pixels)
        _token(hasher, "pixels", hashlib.sha256(pixels.tobytes()).hexdigest())
        return
    _token(hasher, "unhashed-source", image.source)


def _tree_digest(hasher: Any, tree: Any, seen: set) -> None:
    if tree is None:
        _token(hasher, "no-tree")
        return
    if _pointer(tree) in seen:
        _token(hasher, "tree-ref", tree.name)
        return
    seen.add(_pointer(tree))
    _token(hasher, "tree", tree.name)
    for node in sorted(tree.nodes, key=lambda n: n.name):
        _token(hasher, "node", node.bl_idname, node.name, node.mute)
        rows = []
        for prop in node.bl_rna.properties:
            if prop.identifier in _TREE_EXCLUDED:
                continue
            try:
                value = _simple(getattr(node, prop.identifier))
            except Exception:
                continue
            if value is not None:
                rows.append((prop.identifier, value))
        _token(hasher, json.dumps(rows, sort_keys=True, default=str))
        for socket in node.inputs:
            _token(hasher, "in", socket.identifier, socket.enabled, _simple(getattr(socket, "default_value", None)))
        image = getattr(node, "image", None)
        if image is not None:
            _image_digest(hasher, image)
        if hasattr(node, "node_tree"):
            _tree_digest(hasher, node.node_tree, seen)
    for link in sorted(tree.links, key=lambda l: (l.from_node.name, l.from_socket.identifier,
                                                   l.to_node.name, l.to_socket.identifier)):
        _token(hasher, "link", link.from_node.name, link.from_socket.identifier,
               link.to_node.name, link.to_socket.identifier, link.is_muted)


def content_fingerprint(obj: Any) -> str:
    """``visual_validation.content-v1``: a digest of everything this module
    compares -- transform, evaluated mesh with every attribute (including UVs
    and custom normals), UV roles, slot materials with their node trees, and
    image identity/bytes.  Used when the engine's own fingerprint cannot be
    computed for an object, and recorded as such in the report."""
    hasher = hashlib.sha256()
    _token(hasher, "content-v1", obj.name, obj.type, [list(row) for row in obj.matrix_world])
    evaluated, mesh = None, obj.data
    try:
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
    except Exception as exc:
        _token(hasher, "base-mesh", _text(exc, 200))
        evaluated = None
    try:
        for collection, attr, dtype in (("vertices", "co", np.float32), ("loops", "vertex_index", np.int32),
                                        ("polygons", "loop_start", np.int32),
                                        ("polygons", "loop_total", np.int32)):
            data = getattr(mesh, collection)
            width = 3 if attr == "co" else 1
            array = np.empty(len(data) * width, dtype)
            if len(data):
                data.foreach_get(attr, array)
            _token(hasher, collection, attr, hashlib.sha256(array.tobytes()).hexdigest())
        for attribute in sorted(mesh.attributes, key=lambda a: a.name):
            _token(hasher, "attribute", attribute.name, attribute.domain, attribute.data_type)
            field = _ATTRIBUTE_FIELDS.get(attribute.data_type)
            if field is None:
                _token(hasher, "attribute-type-not-hashed")
                continue
            name, width, dtype = field
            array = np.empty(len(attribute.data) * width, dtype)
            try:
                if len(array):
                    attribute.data.foreach_get(name, array)
                _token(hasher, hashlib.sha256(array.tobytes()).hexdigest())
            except Exception as exc:
                _token(hasher, "attribute-unreadable", _text(exc, 200))
        _token(hasher, "uv-roles", _render_uv_name(mesh), _active_uv_name(mesh))
    finally:
        if evaluated is not None:
            try:
                evaluated.to_mesh_clear()
            except Exception:
                pass
    seen: set = set()
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        _token(hasher, "slot", index, slot.link, material.name if material else "<empty>")
        if material is not None:
            _tree_digest(hasher, material.node_tree, seen)
    return hasher.hexdigest()


def _fingerprint(obj: Any, is_output: bool) -> Dict[str, Any]:
    """One read: the engine's fingerprint when it can be computed; otherwise content-v1."""
    engine_error = None
    try:
        from . import engine
        if is_output:
            value, algorithm = engine.output_object_fingerprint(obj), "engine.output_object_fingerprint"
        else:
            value, algorithm = engine.source_fingerprint(obj), "engine.source_fingerprint"
        return {"value": value, "algorithm": algorithm, "error": None, "engine_error": None}
    except Exception as exc:
        engine_error = _text(exc, 500)
    try:
        return {"value": content_fingerprint(obj), "algorithm": "visual_validation.content-v1",
                "error": None, "engine_error": engine_error}
    except Exception as exc:
        return {"value": None, "algorithm": None, "error": _text(exc, 500), "engine_error": engine_error}


def _settled_fingerprint(obj: Any, is_output: bool) -> Dict[str, Any]:
    """Evaluate the depsgraph, then read until two consecutive reads agree.

    A cold-opened MOVIE image reports an empty colour space until something
    first loads it -- the first fingerprint read does -- so a single read is
    not a baseline.  ``stable`` is False when FINGERPRINT_READS reads never
    produced two equal consecutive values; invariance is then unverifiable,
    which is never evidence that anything was modified.
    """
    try:
        bpy.context.evaluated_depsgraph_get()
    except Exception:
        pass
    reads: List[Tuple[Any, Any]] = []
    row: Dict[str, Any] = {}
    for _ in range(FINGERPRINT_READS):
        row = _fingerprint(obj, is_output)
        reads.append((row["algorithm"], row["value"]))
        if len(reads) >= 2 and reads[-1] == reads[-2]:
            break
    stable = len(reads) >= 2 and reads[-1] == reads[-2] and row["value"] is not None
    return dict(row, stable=stable, reads=len(reads), distinct=len(set(reads)))


def _environment() -> Dict[str, Any]:
    build_hash = getattr(bpy.app, "build_hash", b"")
    env = {
        "blender_version": bpy.app.version_string,
        "blender_build_hash": build_hash.decode() if isinstance(build_hash, bytes) else str(build_hash),
        "platform": platform.platform(), "system": platform.system(),
        "python": platform.python_version(), "module": __name__, "module_version": MODULE_VERSION,
        "engine_tool_version": None, "addon_version": None,
    }
    try:
        from . import engine
        env["engine_tool_version"] = engine.TOOL_VERSION
        env["engine_md5"] = engine.tool_file_md5()
    except Exception as exc:
        env["engine_error"] = _text(exc, 300)
    package = sys.modules.get(__package__ or "")
    info = getattr(package, "bl_info", None)
    if isinstance(info, dict) and "version" in info:
        env["addon_version"] = ".".join(str(v) for v in info["version"])
    return env


def _object_summary(obj: Any) -> Dict[str, Any]:
    mesh = obj.data
    materials, origin = _material_usage(obj)
    library = getattr(obj, "library", None)
    return {"object": obj.name, "library": library.filepath if library is not None else None,
            "mesh": mesh.name, "vertices": len(mesh.vertices),
            "polygons": len(mesh.polygons), "loops": len(mesh.loops),
            "uv_layers": [layer.name for layer in mesh.uv_layers],
            "texture_uv": _render_uv_name(mesh), "edit_uv": _active_uv_name(mesh),
            "materials": [m.name if m is not None else None for m in materials],
            "materials_from": origin}


def _without_non_finite(value: Any, path: str, found: List[str]) -> Any:
    """A JSON-safe copy of ``value``: every NaN/inf float becomes None and its
    path is recorded, so a report can always be published."""
    if isinstance(value, float) and not math.isfinite(value):
        found.append(path)
        return None
    if isinstance(value, dict):
        return {key: _without_non_finite(item, "%s.%s" % (path, key), found) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_non_finite(item, "%s[%d]" % (path, index), found) for index, item in enumerate(value)]
    return value


def _invariance(report: Dict[str, Any], side: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """Record whether ``side`` is unchanged, blaming the comparison only for a
    change between two SETTLED fingerprints of the same algorithm."""
    row = report[side]
    row["fingerprint_after"] = after["value"]
    row["fingerprint_after_reads"] = after["reads"]
    unchanged = None
    if before["value"] is None:
        pass                                  # FINGERPRINT_UNAVAILABLE is already a FAIL finding
    elif after["value"] is None or after["algorithm"] != before["algorithm"]:
        report["findings"].append(_finding(
            "INVARIANCE_CHECK_FAILED", FAIL, "report", side,
            "the %s object could not be re-fingerprinted with the same algorithm after the comparison "
            "(%s -> %s: %s)" % (side, before["algorithm"], after["algorithm"],
                                after["error"] or after["engine_error"] or "different algorithm")))
    elif not (before["stable"] and after["stable"]):
        report["findings"].append(_finding(
            "INVARIANCE_UNVERIFIED", INSUFFICIENT_COVERAGE, "report", side,
            "the %s fingerprint never gave two equal consecutive reads (before: %d reads, %d distinct; "
            "after: %d reads, %d distinct), so whether the comparison left it unchanged is not verified; "
            "nothing is blamed on the comparison" % (side, before["reads"], before["distinct"],
                                                     after["reads"], after["distinct"])))
    else:
        unchanged = after["value"] == before["value"]
        if not unchanged:
            report["findings"].append(_finding(
                "%s_MODIFIED" % side.upper(), FAIL, "report", side,
                "the %s object's settled fingerprint changed during the comparison" % side))
    row["unchanged"] = unchanged


# ------------------------------------------------------------- destination

def _prepare_destination(destination: Any) -> Tuple[str, str]:
    dest = os.path.abspath(os.fspath(destination))
    if os.path.lexists(dest):
        junction = getattr(os.path, "isjunction", lambda _p: False)(dest)
        if os.path.islink(dest) or junction or not os.path.isdir(dest):
            raise FileExistsError("refusing to write a visual report over existing path: %s" % dest)
        if os.listdir(dest):
            raise FileExistsError("visual report destination is not empty: %s" % dest)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".ig_visual_stage_", dir=parent)
    return dest, staging


def _publish(staging: str, dest: str) -> None:
    if os.path.lexists(dest):
        junction = getattr(os.path, "isjunction", lambda _p: False)(dest)
        if os.path.islink(dest) or junction or not os.path.isdir(dest) or os.listdir(dest):
            raise FileExistsError("visual report destination changed during the comparison: %s" % dest)
        os.rmdir(dest)
    os.replace(staging, dest)


# ------------------------------------------------------------------- driver

def compare_pair(source_object: Any, output_object: Any, destination: Any,
                 settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save reference, output and difference artifacts and JSON verdict.

    Returns the report that was published at ``destination/report.json``.
    Raises ValueError for invalid settings/objects and OSError (including
    FileExistsError) when the destination cannot receive a new report; both
    happen before any rendering.  Other failures during comparison are
    recorded in the report (status FAIL).  KeyboardInterrupt/SystemExit
    restore the scene, remove the staging folder and propagate.
    """
    resolved, defaulted = validate_settings(settings)
    for label, obj in (("source", source_object), ("output", output_object)):
        if obj is None or getattr(obj, "type", None) != "MESH" or obj.data is None:
            raise ValueError("%s must be a mesh object" % label)
    user_scene = bpy.context.scene
    fingerprints = {"source": _settled_fingerprint(source_object, False),
                    "output": _settled_fingerprint(output_object, True)}
    summaries = {"source": _object_summary(source_object), "output": _object_summary(output_object)}
    dest, staging = _prepare_destination(destination)
    published = False
    try:
        run = _Run(resolved, staging)
        device, device_error = _device(resolved["device"])
        report: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "import_glue.visual_comparison",
            "status": None,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "destination": dest,
            "destination_resolved": os.path.realpath(dest),
            "source": dict(summaries["source"], fingerprint=fingerprints["source"]["value"],
                           fingerprint_algorithm=fingerprints["source"]["algorithm"],
                           fingerprint_reads=fingerprints["source"]["reads"],
                           fingerprint_baseline="stable" if fingerprints["source"]["stable"] else "unstable"),
            "output": dict(summaries["output"], fingerprint=fingerprints["output"]["value"],
                           fingerprint_algorithm=fingerprints["output"]["algorithm"],
                           fingerprint_reads=fingerprints["output"]["reads"],
                           fingerprint_baseline="stable" if fingerprints["output"]["stable"] else "unstable"),
            "frame": user_scene.frame_current,
            "environment": _environment(),
            "device": device,
            "settings": resolved,
            "defaulted_optional_settings": defaulted,
            "lighting": {name: LIGHTING[name] for name in resolved["lighting_presets"]},
            "display_transform": DISPLAY_TRANSFORM,
            "artifact_legend": ARTIFACT_LEGEND,
            "coverage": {"layers_requested": list(resolved["layers"]), "layers_compared": [],
                         "surface": {"status": "NOT_REQUESTED"}, "render": {"status": "NOT_REQUESTED"}},
            "metrics": {},
            "findings": [],
            "errors": [],
            "artifacts": {},
            "reasons": [],
        }
        for side in ("source", "output"):
            if fingerprints[side]["engine_error"]:
                report[side]["engine_fingerprint_error"] = fingerprints[side]["engine_error"]
                report["findings"].append(_finding(
                    "ENGINE_FINGERPRINT_UNAVAILABLE", NOTE, "report", side,
                    "engine fingerprint failed (%s); the verdict is tied to %s instead"
                    % (fingerprints[side]["engine_error"], fingerprints[side]["algorithm"])))
            if fingerprints[side]["error"]:
                report["findings"].append(_finding(
                    "FINGERPRINT_UNAVAILABLE", FAIL, "report", side,
                    "the %s fingerprint could not be computed, so this verdict cannot be tied to "
                    "its content: %s" % (side, fingerprints[side]["error"])))
        if device_error:
            report["findings"].append(_finding("DEVICE_UNAVAILABLE", FAIL, "report", "device", device_error))
        try:
            if not device_error:
                _compare(run, report, source_object, output_object, user_scene)
        except Exception as exc:
            report["errors"].append({"stage": run.stage, "message": _text(exc),
                                     "traceback": traceback.format_exc()[-3000:]})
        finally:
            cleanup_errors = run.cleanup()
        for message in cleanup_errors:
            report["errors"].append({"stage": "cleanup", "message": message})
        report["counts"] = {"renders": run.render_count, "bakes": run.bake_count}
        for side, obj in (("source", source_object), ("output", output_object)):
            _invariance(report, side, fingerprints[side], _settled_fingerprint(obj, side == "output"))
        artifacts = {}
        for rel, row in sorted(run.artifacts.items()):
            path = os.path.join(staging, *rel.split("/"))
            if os.path.isfile(path):
                digest, size = _sha256(path)
                artifacts[rel] = dict(row, sha256=digest, bytes=size)
            else:
                report["findings"].append(_finding("ARTIFACT_MISSING", FAIL, row["layer"], rel,
                                                   "artifact was not written: %s" % rel))
        report["artifacts"] = artifacts
        _scope(report)
        non_finite: List[str] = []
        report = _without_non_finite(report, "report", non_finite)
        if non_finite:
            report["findings"].append(_finding(
                "NON_FINITE_VALUE", FAIL, "report", ", ".join(non_finite[:10]),
                "%d report values were NaN/inf and are recorded as null: %s"
                % (len(non_finite), ", ".join(non_finite[:10]))))
        report["status"], report["reasons"] = derive_status(report)
        payload = json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False)
        data = payload.encode("utf-8")
        _write_bytes(os.path.join(staging, REPORT_NAME), data)
        _write_bytes(os.path.join(staging, REPORT_HASH_NAME),
                     ("%s  %s\n" % (hashlib.sha256(data).hexdigest(), REPORT_NAME)).encode("ascii"))
        _publish(staging, dest)
        published = True
        return json.loads(payload)
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _scope(report: Dict[str, Any]) -> None:
    compared = report["coverage"]["layers_compared"]
    requested = report["coverage"]["layers_requested"]
    if compared == ["render"] or (compared and "surface" not in compared):
        scope = ("render-only: fixed-camera paired renders from %s; surface correspondence NOT "
                 "tested (%s)" % (", ".join(report["settings"]["views"]),
                                  report["coverage"]["surface"].get("reason", "not requested")
                                  if "surface" in requested else "not requested"))
    elif compared == ["surface"]:
        scope = "surface-only: shared probe-UV channel comparison; fixed-camera renders NOT tested"
    elif compared:
        scope = "surface channels in a shared correspondence + fixed-camera paired renders"
    else:
        scope = "nothing compared"
    time_dependent = sorted({
        name for layer in LAYERS
        for name, rows in (report["coverage"][layer].get("unsupported_materials") or {}).items()
        if any(row.get("code") == "TIME_DEPENDENT_IMAGE" for row in rows)})
    if time_dependent:
        scope += ("; the source has time-dependent images (%s): only frame %s could be shown, which is "
                  "never a fidelity claim" % (", ".join(time_dependent), report.get("frame")))
    report["coverage"]["fidelity_scope"] = scope


def _non_finite_geometry(proxy: Any) -> Optional[str]:
    points = _world_points(proxy)
    bad = int((~np.isfinite(points).all(axis=1)).sum()) if len(points) else 0
    if bad:
        return "%d of %d evaluated vertex positions are non-finite (NaN/inf) in world space" % (bad, len(points))
    return None


def _compare(run: _Run, report: Dict[str, Any], source_object: Any, output_object: Any,
             user_scene: Any) -> None:
    settings = run.settings
    run.stage = "proxies"
    _setup_scene(run, user_scene)
    proxies, infos = {}, {}
    for side, obj in (("source", source_object), ("output", output_object)):
        proxies[side], infos[side] = _make_proxy(run, obj, side)
        if infos[side]["notes"]:
            report["findings"].extend(_finding("PROXY_NOTE", NOTE, "report", side, note)
                                      for note in infos[side]["notes"])
    run.stage = "geometry"
    invalid = {side: _non_finite_geometry(proxies[side]) for side in proxies}
    if any(invalid.values()):
        for layer in settings["layers"]:
            report["coverage"][layer].update(status="INVALID_GEOMETRY")
            for side, why in sorted(invalid.items()):
                if why:
                    report["findings"].append(_finding(
                        "INVALID_GEOMETRY", FAIL, layer, side,
                        "%s layer not compared: the %s %s" % (layer, side, why)))
        run.stage = "done"
        return
    # Analyse what the proxies actually render and bake: the EVALUATED mesh's
    # used slots (a modifier can add some).  Keys are the original material
    # pointers, which is what the proxy slots hold; they never reach the report.
    run.stage = "analyze"
    analyses = {"source": {}, "output": {}}
    for side in ("source", "output"):
        for material in _used_materials(proxies[side]):
            key = _pointer(material) if material is not None else 0
            analyses[side][key] = analyze_material(material, settings["channel_references"], side)

    def issues(key: str) -> Dict[str, Dict[str, Any]]:
        return {side: {(a["material"] or "<empty>"): a[key] for a in analyses[side].values() if a[key]}
                for side in analyses}
    blocking = issues("blocking_issues")
    render_blockers = issues("render_issues")
    if "surface" in settings["layers"]:
        run.stage = "surface"
        if blocking["source"]:
            _reference_blocked(report, "surface", blocking)
        else:
            _surface_layer(run, report, proxies, analyses, infos)
    if "render" in settings["layers"]:
        run.stage = "render"
        if render_blockers["source"]:
            _reference_blocked(report, "render", render_blockers)
        elif blocking["output"]:
            report["coverage"]["render"].update(status="OUTPUT_INVALID")
            report["findings"].append(_finding(
                "OUTPUT_MATERIAL_INVALID", FAIL, "render", ", ".join(sorted(blocking["output"])),
                "the output's materials cannot render as intended: " + _describe(blocking["output"])))
        elif render_blockers["output"]:
            report["coverage"]["render"].update(status=UNSUPPORTED_REFERENCE,
                                                reason="the output cannot be rendered faithfully")
            report["findings"].append(_finding(
                "UNSUPPORTED_REFERENCE", UNSUPPORTED_REFERENCE, "render",
                ", ".join(sorted(render_blockers["output"])),
                "render layer not compared; the output cannot be rendered faithfully: "
                + _describe(render_blockers["output"])))
        else:
            _render_layer(run, report, proxies)
    run.stage = "done"


def _describe(rows: Dict[str, List[Dict[str, Any]]]) -> str:
    return "; ".join("%s: %s" % (name, ", ".join("%s at %s" % (i["code"], i["node_instance"])
                                                for i in issues))
                     for name, issues in sorted(rows.items()))


def _reference_blocked(report: Dict[str, Any], layer: str, blockers: Dict[str, Dict[str, Any]]) -> None:
    rows = blockers["source"]
    coverage = report["coverage"][layer]
    coverage["status"] = UNSUPPORTED_REFERENCE
    coverage["reason"] = "the source cannot be %s as authored" % ("rendered" if layer == "render" else "shown")
    coverage.setdefault("unsupported_materials", {}).update(rows)
    report["findings"].append(_finding(
        "UNSUPPORTED_REFERENCE", UNSUPPORTED_REFERENCE, layer, ", ".join(sorted(rows)),
        "%s layer not compared; the source reference is not the authored appearance: %s"
        % (layer, _describe(rows))))
    if blockers["output"]:
        report["findings"].append(_finding(
            "OUTPUT_NOT_GRADED", NOTE, layer, ", ".join(sorted(blockers["output"])),
            "output material problems were not graded because the reference is unsupported"))
