"""Explicit delivered-map contracts and an opt-in, lossless PNG16 writer.

No Blender import and no change to engine defaults. Merely requesting an extra
channel does not implement a bake: assess_output_feasibility requires evidence
from the calling backend. Material capability analysis remains a separate gate.
"""
from __future__ import annotations

import math
import os
import struct
import tempfile
import zlib

SCHEMA_VERSION = 1
DEFAULT_PROFILE = "PBR_BASE"
PROFILES = ("PBR_BASE", "PBR_HIGH_PRECISION")
_ALIASES = {"LEGACY": "PBR_BASE", "HIGH_PRECISION": "PBR_HIGH_PRECISION"}
LEGACY_CHANNELS = ("color", "metal", "rough", "normal")
SUFFIXES = {"color": "_ALB", "metal": "_MET", "rough": "_RGH", "normal": "_NOR",
            "emission": "_EMI", "height": "_HGT"}


def build_output_contract(profile=DEFAULT_PROFILE, *, emission=False, height=False,
                          height_scale=None, height_zero=0.0,
                          height_units="BLENDER_WORLD_UNIT", normal_convention="OPENGL"):
    """Return a fresh, JSON-serializable contract; this performs no conversion.

    PNG16 still has bounded [0,1] storage and is NOT HDR. Optional emission is
    linear RGB float EXR. Optional height is an authored, single-valued scalar
    field: displacement = (sample - height_zero) * height_scale in declared units.
    Neither optional channel is advertised as an implemented engine feature.
    """
    profile = _ALIASES.get(profile, profile)
    if profile not in PROFILES:
        raise ValueError("unknown output profile: %r" % profile)
    if type(emission) is not bool or type(height) is not bool:
        raise ValueError("emission and height must be explicit booleans")
    if normal_convention != "OPENGL":
        raise ValueError("delivered normal convention must be OPENGL (+X,+Y,+Z)")
    if not isinstance(height_units, str) or not height_units.strip():
        raise ValueError("height_units must be a nonempty declared unit")
    if not isinstance(height_zero, (int, float)) or isinstance(height_zero, bool) or not math.isfinite(height_zero):
        raise ValueError("height_zero must be finite")
    if height and (not isinstance(height_scale, (int, float)) or isinstance(height_scale, bool)
                   or not math.isfinite(height_scale) or height_scale <= 0):
        raise ValueError("height requires an explicit finite positive height_scale")
    bits = 8 if profile == "PBR_BASE" else 16
    channels = []
    definitions = (
        ("color", "base_color", "sRGB", "RGB or RGBA", "dimensionless reflectance",
         "RGB sRGB transfer; optional straight alpha is linear coverage"),
        ("metal", "metallic", "Non-Color", "R or replicated RGB", "dimensionless fraction",
         "0 dielectric, 1 metal; no transfer function"),
        ("rough", "perceptual_roughness", "Non-Color", "R or replicated RGB", "dimensionless",
         "Principled perceptual roughness; not roughness squared"),
        ("normal", "tangent_space_normal", "Non-Color", "RGB", "dimensionless unit vector",
         "stored RGB=(xyz+1)/2; OpenGL +X,+Y,+Z; reconstruct using exported mesh tangent basis"),
    )
    for key, meaning, space, components, units, encoding in definitions:
        channels.append({"key": key, "semantic": meaning, "suffix": SUFFIXES[key],
                         "extension": ".png", "format": "PNG", "precision_bits": bits,
                         "sample_type": "UNORM", "range": [0, 1], "colorspace": space,
                         "components": components, "units": units, "encoding": encoding,
                         "alpha_mode": "STRAIGHT" if key == "color" else "NONE",
                         "implemented_legacy": True})
    if emission:
        channels.append({"key": "emission", "semantic": "emission_radiance", "suffix": "_EMI",
                         "extension": ".exr", "format": "OPEN_EXR", "precision_bits": 32,
                         "sample_type": "FLOAT", "range": "finite nonnegative HDR",
                         "colorspace": "Linear", "components": "RGB",
                         "units": "scene-linear emission RGB with evaluated strength",
                         "encoding": "No sRGB or view transform; bake the evaluated static emission field",
                         "implemented_legacy": False})
    if height:
        channels.append({"key": "height", "semantic": "authored_scalar_height", "suffix": "_HGT",
                         "extension": ".exr", "format": "OPEN_EXR", "precision_bits": 32,
                         "sample_type": "FLOAT", "range": "finite signed scalar",
                         "colorspace": "Non-Color", "components": "R", "units": height_units,
                         "scale": float(height_scale), "zero": float(height_zero),
                         "encoding": "displacement=(sample-zero)*scale; no vector displacement",
                         "implemented_legacy": False})
    return {"schema_version": SCHEMA_VERSION, "profile": profile,
            "default_compatible": profile == "PBR_BASE" and not emission and not height,
            "channels": channels, "normal_convention": normal_convention,
            "normal_axes": ["POS_X", "POS_Y", "POS_Z"],
            "uv_origin": "lower-left", "file_scanlines": "top-to-bottom",
            "precision_scope": "four loose PNG files; exporter texture re-encoding is not verified",
            "gltf_fbx_precision_verified": False,
            "requires_float_bake_buffer": profile != "PBR_BASE" or emission or height,
            "required_route": "GRAPH_BAKE" if profile != "PBR_BASE" or emission or height else "EXISTING",
            "view_transform": "NONE",
            "limitations": ["A map set does not represent arbitrary material closures.",
                           "A normal map does not carry geometric displacement.",
                           "PNG16 increases precision, not range or texture resolution.",
                           "glTF/FBX exporters may re-encode textures; their precision is not established by this contract.",
                           "Emission/height requests require implemented bake, writer, and target support."]}


def assess_output_feasibility(contract, facts=None):
    """Fail closed for optional paths lacking explicit backend evidence.

    facts keys: route, float_bake_buffer, png16_writer, unsupported_effects,
    optional_channels. Each optional channel needs implemented, roundtrip_tested,
    target_supports, and uv_static all True; height additionally needs
    scalar_authored and single_valued. These are caller assertions, not a shader
    graph analysis and not proof of visual equivalence.
    """
    facts = facts or {}
    if not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported output contract schema")
    if contract.get("profile") not in PROFILES or not isinstance(contract.get("channels"), list):
        raise ValueError("invalid output contract")
    keys = [channel.get("key") for channel in contract["channels"]]
    if keys[:4] != list(LEGACY_CHANNELS) or len(set(keys)) != len(keys):
        raise ValueError("contract must preserve the ordered four legacy channels")
    diagnostics = []

    def block(code, message, channel=None):
        diagnostics.append({"code": code, "severity": "ERROR", "channel": channel, "message": message})

    if contract.get("required_route") == "GRAPH_BAKE" and facts.get("route") != "GRAPH_BAKE":
        block("route_required", "This opt-in output contract requires GRAPH_BAKE.")
    if contract.get("requires_float_bake_buffer") and facts.get("float_bake_buffer") is not True:
        block("float_buffer_required", "A float bake buffer is required; up-converting an 8-bit bake is insufficient.")
    if contract["profile"] == "PBR_HIGH_PRECISION" and facts.get("png16_writer") is not True:
        block("png16_writer_required", "A tested true 16-bit PNG writer must be connected.")
    for effect in facts.get("unsupported_effects", []):
        block("unrepresentable_effect", "Requested material effect is not represented: " + str(effect))
    optional = facts.get("optional_channels", {})
    for key in keys[4:]:
        if key not in ("emission", "height"):
            block("unknown_channel", "Unknown optional channel.", key)
            continue
        evidence = optional.get(key, {})
        required = ["implemented", "roundtrip_tested", "target_supports", "uv_static"]
        if key == "height":
            required += ["scalar_authored", "single_valued"]
        missing = [name for name in required if evidence.get(name) is not True]
        if missing:
            block("optional_channel_unproven", "Missing explicit backend evidence: " + ", ".join(missing), key)
        if evidence.get("view_dependent") or evidence.get("time_dependent"):
            block("nonstatic_optional_channel", "A static UV map cannot preserve view/time-dependent behaviour.", key)
        if key == "height" and evidence.get("vector_displacement"):
            block("vector_displacement_unsupported", "A scalar height map cannot carry vector displacement.", key)
    return {"schema_version": SCHEMA_VERSION, "ok": not diagnostics,
            "status": "SUPPORTED_BY_DECLARED_BACKEND" if not diagnostics else "BLOCKED",
            "profile": contract["profile"], "diagnostics": diagnostics,
            "material_capability_gate_still_required": True}


def _chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def write_png16(path, array, *, compression=1):
    """Atomically write normalized, bottom-up Blender pixels as true PNG16.

    Accepts HxW, HxWx3, or HxWx4 float arrays; finite [0,1] input is mandatory.
    No gamma conversion, normal flip, view transform, alpha conversion, or
    silent clipping occurs here. Callers supply already encoded contract data.
    Uses row-sized quantization buffers, big-endian samples, and lossless zlib.
    Existing files survive validation/encoding failures.
    """
    import numpy as np

    if type(compression) is not int or not 0 <= compression <= 9:
        raise ValueError("compression must be an integer from 0 through 9")
    data = np.asarray(array)
    if data.dtype.kind != "f":
        raise ValueError("PNG16 expects normalized floating point input, not quantized integers")
    if data.ndim == 2:
        channels, color_type = 1, 0
    elif data.ndim == 3 and data.shape[2] in (3, 4):
        channels, color_type = data.shape[2], 2 if data.shape[2] == 3 else 6
    else:
        raise ValueError("PNG16 array must be HxW, HxWx3, or HxWx4")
    height, width = data.shape[:2]
    if not 0 < width <= 32768 or not 0 < height <= 32768 or width * height > 67_108_864:
        raise ValueError("PNG16 dimensions exceed the bounded writer contract")
    if not np.isfinite(data).all() or np.any(data < 0) or np.any(data > 1):
        raise ValueError("PNG16 requires finite [0,1] samples; HDR needs a float format")
    destination = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(destination)
    fd, temporary = tempfile.mkstemp(prefix=".import-glue-png16-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            stream.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, color_type, 0, 0, 0)))
            compressor = zlib.compressobj(compression)
            for row in data[::-1]:
                quantized = np.rint(row.astype(np.float64) * 65535).astype(">u2")
                payload = compressor.compress(b"\0" + quantized.tobytes(order="C"))
                if payload:
                    stream.write(_chunk(b"IDAT", payload))
            payload = compressor.flush()
            if payload:
                stream.write(_chunk(b"IDAT", payload))
            stream.write(_chunk(b"IEND", b""))
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {"path": destination, "format": "PNG", "precision_bits": 16,
            "width": width, "height": height, "channels": channels,
            "quantization_max_abs_error": 0.5 / 65535, "transfer_applied": False}
