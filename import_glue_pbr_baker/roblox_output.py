"""Offline Roblox SurfaceAppearance conversion; no bpy, upload, or network calls.

The linear reference model is E = decoded_ColorMap * mask * tint * strength.
It is a declared algebraic model, not a claim of Roblox renderer equivalence.
The caller must supply the FINAL delivered albedo, including its quantization.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import struct
import tempfile
import zlib

SCHEMA_VERSION = 1
REFERENCE_DATE = "2026-10-06"
ALGORITHM = "nonnegative_weighted_rank_one_als_v1"
MODEL = "E_linear[u,c] = ColorMap_linear[u,c] * (mask_uint8[u]/255) * tint_linear[c] * strength"
SOURCES = {
    "surface_api": "https://create.roblox.com/docs/reference/engine/classes/SurfaceAppearance",
    "surface_guide": "https://create.roblox.com/docs/art/modeling/surface-appearance",
    "texture_specs": "https://create.roblox.com/docs/art/modeling/texture-specifications",
    "content": "https://create.roblox.com/docs/reference/engine/datatypes/Content",
}
_PROPERTIES = {"color": "ColorMap", "metal": "MetalnessMap", "rough": "RoughnessMap",
               "normal": "NormalMap", "emissive": "EmissiveMaskContent"}


def _nonnegative(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(name + " must be " + ("positive" if positive else "nonnegative"))
    return float(value)


def _target(target, strength_limit):
    if target not in ("EXPERIENCE", "MARKETPLACE"):
        raise ValueError("target must be EXPERIENCE or MARKETPLACE")
    if strength_limit is not None:
        strength_limit = _nonnegative(strength_limit, "strength_limit", positive=True)
    if target == "MARKETPLACE":
        if strength_limit is not None and strength_limit > 40:
            raise ValueError("MARKETPLACE EmissiveStrength must not exceed documented 40")
        return 40.0 if strength_limit is None else strength_limit
    return strength_limit


def delivered_albedo_linear(encoded_rgb_uint8):
    """Decode final RGB8 sRGB values to linear RGB; excludes alpha deliberately."""
    import numpy as np
    values = np.asarray(encoded_rgb_uint8)
    if values.dtype != np.uint8 or values.ndim != 3 or values.shape[-1] != 3 or not values.size:
        raise ValueError("albedo must be a nonempty HxWx3 uint8 final ColorMap RGB")
    encoded = values.astype(np.float64) / 255.0
    return np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)


def factor_emission(albedo_linear, emission_linear, *, target="EXPERIENCE", strength_limit=None,
                    max_relative_error=0.01, mean_relative_error=0.002, absolute_error=1e-5,
                    allow_approximation=False, iterations=32):
    """Fit one shared tint/strength and a per-pixel mask, then audit RGB8 delivery.

    Both inputs are HxWx3 scene-linear arrays with matching lower-left origin.
    Emission includes evaluated source strength and can exceed one. Albedo MUST
    be decoded from the final delivered ColorMap, never an unquantized bake.
    ALS minimizes absolute squared RGB error, not the relative audit metric;
    this is bounded deterministic fitting, not proof of a global optimum.
    Default refusal is an explicit accepted=False result, not an exception.
    Invalid data raises ValueError. Return mask_uint8 is the only non-JSON field.
    """
    import numpy as np
    limit = _target(target, strength_limit)
    max_rel = _nonnegative(max_relative_error, "max_relative_error")
    mean_rel = _nonnegative(mean_relative_error, "mean_relative_error")
    abs_tol = _nonnegative(absolute_error, "absolute_error", positive=True)
    if type(allow_approximation) is not bool:
        raise ValueError("allow_approximation must be an explicit boolean")
    if type(iterations) is not int or not 1 <= iterations <= 128:
        raise ValueError("iterations must be an integer in [1,128]")
    a, e = np.asarray(albedo_linear), np.asarray(emission_linear)
    if a.shape != e.shape or a.ndim != 3 or a.shape[-1] != 3 or not a.size:
        raise ValueError("matching nonempty HxWx3 arrays required")
    if a.dtype.kind not in "fiu" or e.dtype.kind not in "fiu":
        raise ValueError("numeric RGB arrays required")
    count = a.shape[0] * a.shape[1]
    if count > 4096 * 4096:
        raise ValueError("conversion safety limit is 4096x4096 pixels")
    shape = a.shape[:2]
    a, e = a.reshape(-1, 3), e.reshape(-1, 3)
    block_size = 65536

    def blocks():
        for begin in range(0, count, block_size):
            end = min(begin + block_size, count)
            aa = a[begin:end].astype(np.float64, copy=False)
            ee = e[begin:end].astype(np.float64, copy=False)
            yield begin, end, aa, ee

    numer, denom = np.zeros(3), np.zeros(3)
    unmet, source_peak = 0, 0.0
    for _, _, aa, ee in blocks():
        if not np.all(np.isfinite(aa)) or not np.all(np.isfinite(ee)):
            raise ValueError("albedo and emission must be finite")
        if np.any(aa < 0) or np.any(aa > 1) or np.any(ee < 0):
            raise ValueError("albedo must lie in [0,1], emission must be nonnegative")
        # Avoid overflow in squared-error reductions; this is a numerical input
        # ceiling, not a claimed engine HDR limit. Ordinary baked radiance is far below it.
        if np.any(ee > 1e100):
            raise ValueError("emission exceeds numerical safety range")
        numer += np.sum(aa * ee, axis=0)
        denom += np.sum(aa * aa, axis=0)
        unmet += int(np.count_nonzero((aa == 0) & (ee > abs_tol)))
        source_peak = max(source_peak, float(np.max(ee)))
    tint = np.divide(numer, denom, out=np.zeros(3), where=denom > 0)
    peak = float(np.max(tint))
    tint = tint / peak if peak else np.ones(3)
    amplitudes = np.zeros(count, dtype=np.float64)
    completed = 0
    for step in range(iterations):
        numer, denom = np.zeros(3), np.zeros(3)
        for begin, end, aa, ee in blocks():
            basis = aa * tint
            divisor = np.sum(basis * basis, axis=1)
            # Values below this floor cannot be represented reliably by the
            # arithmetic contract. Their error remains visible in the audit.
            uu = np.divide(np.sum(basis * ee, axis=1), divisor,
                           out=np.zeros(end - begin), where=divisor > 1e-250)
            amplitudes[begin:end] = uu
            weighted = aa * uu[:, None]
            numer += np.sum(weighted * ee, axis=0)
            denom += np.sum(weighted * weighted, axis=0)
        updated = np.divide(numer, denom, out=np.zeros(3), where=denom > 1e-250)
        peak = float(np.max(updated))
        if peak:
            updated /= peak
        else:
            updated = np.ones(3)
        completed = step + 1
        difference = float(np.max(np.abs(updated - tint)))
        tint = updated
        if difference < 1e-12:
            break
    # Recompute amplitudes against the final tint, then enforce the target cap.
    for begin, end, aa, ee in blocks():
        basis = aa * tint
        divisor = np.sum(basis * basis, axis=1)
        amplitudes[begin:end] = np.divide(np.sum(basis * ee, axis=1), divisor,
                                         out=np.zeros(end - begin), where=divisor > 1e-250)
    unconstrained_strength = float(np.max(amplitudes))
    strength = min(unconstrained_strength, limit) if limit is not None else unconstrained_strength
    if not math.isfinite(strength) or not np.all(np.isfinite(amplitudes)):
        raise ValueError("factorization exceeded finite numerical range")
    if strength > float(np.finfo(np.float32).max):
        raise ValueError("emission strength exceeds finite Roblox float property range")
    mask = np.rint(np.clip(amplitudes / strength, 0, 1) * 255).astype(np.uint8) if strength else np.zeros(count, dtype=np.uint8)
    abs_max = abs_sum = square_sum = rel_max = rel_sum = 0.0
    failing_pixels = 0
    worst = []
    for begin, end, aa, ee in blocks():
        reconstructed = aa * tint * strength * (mask[begin:end, None] / 255.0)
        delta = np.abs(reconstructed - ee)
        pixel_error = np.max(delta, axis=1)
        scale = np.maximum(np.max(ee, axis=1), abs_tol)
        relative = pixel_error / scale
        abs_max = max(abs_max, float(np.max(delta)))
        abs_sum += float(np.sum(delta))
        square_sum += float(np.sum(delta * delta))
        rel_max = max(rel_max, float(np.max(relative)))
        rel_sum += float(np.sum(relative))
        failing_pixels += int(np.count_nonzero((pixel_error > abs_tol) & (relative > max_rel)))
        indices = np.argsort(-relative, kind="stable")[:8]
        worst.extend((float(relative[k]), begin + int(k), float(pixel_error[k])) for k in indices)
    rel_mean = rel_sum / count
    reasons = []
    if unmet:
        reasons.append("EMISSION_ON_ZERO_ALBEDO")
    if failing_pixels or (rel_mean > mean_rel and abs_sum / (count * 3) > abs_tol):
        reasons.append("QUANTIZED_FIT_EXCEEDS_TOLERANCE")
    if limit is not None and unconstrained_strength > limit:
        reasons.append("TARGET_STRENGTH_LIMIT_REQUIRED_CLIPPING")
    accepted = not reasons or allow_approximation
    worst = sorted(worst, key=lambda entry: (-entry[0], entry[1]))[:8]
    report = {
        "algorithm": ALGORITHM, "iterations_completed": completed,
        "reference_date": REFERENCE_DATE, "model": MODEL, "target": target,
        "target_strength_limit": limit, "unconstrained_strength": unconstrained_strength,
        "pixel_count": count, "source_peak_linear": source_peak,
        "zero_albedo_emitting_components": unmet, "pixels_exceeding_tolerance": failing_pixels,
        "errors": {"max_absolute_rgb": abs_max, "mean_absolute_rgb": abs_sum / (count * 3),
                   "rms_rgb": math.sqrt(square_sum / (count * 3)),
                   "max_relative_pixel": rel_max, "mean_relative_pixel": rel_mean},
        "relative_error_definition": "pixel max(abs(reconstructed-source)) / max(pixel max(source), absolute_error)",
        "thresholds": {"max_relative_error": max_rel, "mean_relative_error": mean_rel, "absolute_error": abs_tol},
        "worst_pixels": [{"x": index % shape[1], "y": index // shape[1], "relative": relative,
                           "absolute": absolute} for relative, index, absolute in worst],
        "reasons": reasons, "approximation_explicitly_allowed": allow_approximation,
        "studio_verified": False,
        "assumptions": ["Albedo is decoded from final RGB8 sRGB delivery.",
                        "Mask and numeric tint are linear multipliers in this reference calculation.",
                        "Roblox upload processing, tint interpretation, filtering, lighting and HDR clamp are unverified.",
                        "Texel-center agreement does not establish agreement under bilinear filtering or mipmapping."],
    }
    return {"mask_uint8": mask.reshape(shape), "tint_linear": [float(v) for v in tint],
            "strength": strength, "accepted": accepted,
            "status": "APPROXIMATED" if reasons and accepted else "REFUSED" if reasons else "WITHIN_TOLERANCE",
            "report": report}


def _atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = file.name
            file.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def write_emissive_mask(path, mask_uint8):
    """Write exact grayscale PNG8. Input scanlines are bottom-to-top (Blender)."""
    import numpy as np
    mask = np.asarray(mask_uint8)
    if mask.dtype != np.uint8 or mask.ndim != 2 or not mask.size:
        raise ValueError("mask must be a nonempty HxW uint8 array")
    if max(mask.shape) > 4096:
        raise ValueError("writer safety limit is 4096 per dimension")
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = b"".join(b"\0" + row.tobytes() for row in mask[::-1])
    encoded = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", mask.shape[1], mask.shape[0], 8, 0, 0, 0, 0))
    encoded += chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
    _atomic_bytes(path, encoded)
    return {"path": str(path), "width": mask.shape[1], "height": mask.shape[0], "bits": 8, "components": "R"}


class EmissionConversionError(ValueError):
    """Refusal retains an inspectable, JSON-safe numerical conversion report."""
    def __init__(self, result):
        self.result = {key: value for key, value in result.items() if key != "mask_uint8"}
        super().__init__("Roblox emission conversion refused: " + ", ".join(result["report"]["reasons"]))


def _write_rgb8(path, rgb):
    import numpy as np
    rgb = np.asarray(rgb)
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[-1] != 3:
        raise ValueError("RGB8 array required")
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = b"".join(b"\0" + row.tobytes() for row in rgb[::-1])
    encoded = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", rgb.shape[1], rgb.shape[0], 8, 2, 0, 0, 0))
    _atomic_bytes(path, encoded + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def finalize_roblox_object(folder, color_path, metal_path, rough_path, normal_path,
                           emission_pixels_linear, dimensions, binding_id, mesh_name,
                           allow_approximation=False, *, target="EXPERIENCE", strength_limit=None,
                           max_relative_error=0.01, mean_relative_error=0.002, absolute_error=1e-5):
    """Blender-only finalization of already-exported files, never source images.

    dimensions=(width,height); emission is HxWx3 or HxWx4 scene-linear float,
    in Blender's lower-left scanline order and with source strength evaluated.
    folder is the future bundle root; paths can be absolute inside it or relative.
    Temporary image datablocks are removed even on refusal. The returned binding
    contains JSON-safe fit evidence. EmissionConversionError.result retains it on
    refusal. No material/source image/mesh datablock is changed.
    """
    import bpy
    import numpy as np
    root = Path(folder).resolve()
    if not isinstance(dimensions, (tuple, list)) or len(dimensions) != 2 or any(type(v) is not int or v < 1 or v > 4096 for v in dimensions):
        raise ValueError("dimensions must be (width,height), each an integer in [1,4096]")
    width, height = dimensions
    local_paths, relative = {}, {}
    for key, value in (("color", color_path), ("metal", metal_path), ("rough", rough_path), ("normal", normal_path)):
        path = Path(value)
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("output files must remain inside bundle root")
        local_paths[key] = path
        relative[key] = path.relative_to(root).as_posix()
        info = _png_metadata(path)
        if (info["width"], info["height"]) != (width, height):
            raise ValueError("delivered map dimensions disagree with emission")
        if key in ("color", "normal") and info["png_color_type"] not in (2, 6):
            raise ValueError("color and normal require RGB/RGBA PNG8")

    def read_raw(path):
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            image.colorspace_settings.name = "Non-Color"
            if tuple(image.size) != (width, height):
                raise ValueError("Blender decoder dimensions disagree with PNG header")
            pixels = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            if not np.all(np.isfinite(pixels)) or np.any(pixels < 0) or np.any(pixels > 1):
                raise ValueError("decoded PNG8 samples outside normalized finite range")
            return np.rint(pixels.reshape(height, width, 4) * 255).astype(np.uint8)
        finally:
            bpy.data.images.remove(image)

    albedo = delivered_albedo_linear(read_raw(local_paths["color"])[..., :3])
    emission = np.asarray(emission_pixels_linear)
    if emission.shape not in ((height, width, 3), (height, width, 4)):
        raise ValueError("emission_pixels_linear must be HxWx3 or HxWx4")
    result = factor_emission(albedo, emission[..., :3], target=target, strength_limit=strength_limit,
                             max_relative_error=max_relative_error, mean_relative_error=mean_relative_error,
                             absolute_error=absolute_error, allow_approximation=allow_approximation)
    if not result["accepted"]:
        raise EmissionConversionError(result)
    # Validate both scalar fields before writing either of them. Taking red is
    # valid only after checking actual replicated RGB, never arbitrary color.
    scalars = {}
    for key in ("metal", "rough"):
        raw = read_raw(local_paths[key])
        if not np.array_equal(raw[..., 0], raw[..., 1]) or not np.array_equal(raw[..., 0], raw[..., 2]):
            raise ValueError(key + " output is not a scalar/replicated grayscale field")
        scalars[key] = raw[..., 0]
    normal = read_raw(local_paths["normal"])[..., :3]
    stem = local_paths["color"].stem
    stem = stem[:-4] if stem.endswith("_ALB") else stem
    emissive_path = local_paths["color"].with_name(stem + "_EMI.png")
    if emissive_path in local_paths.values():
        raise ValueError("emissive output would overwrite an existing channel")
    for key in ("metal", "rough"):
        write_emissive_mask(local_paths[key], scalars[key])
    _write_rgb8(local_paths["normal"], normal)
    write_emissive_mask(emissive_path, result["mask_uint8"])
    relative["emissive"] = emissive_path.relative_to(root).as_posix()
    result["report"]["delivered_files"] = {key: _relative_file(root, value)[1]["sha256"] for key, value in relative.items()}
    result["report"]["raw_png_read"] = "Blender load check_existing=False, Non-Color, RGB8 rounding, explicit sRGB decode only for albedo"
    return {"id": binding_id, "mesh_name": mesh_name, "maps": relative,
            "emission": {key: value for key, value in result.items() if key != "mask_uint8"}}


def _relative_file(root, value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("bundle file paths must be forward-slash relative paths")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("..", ".") for part in value.split("/")):
        raise ValueError("bundle path escapes or ambiguously addresses root")
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("bundle file missing or outside root: " + value)
    digest = hashlib.sha256()
    with resolved.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return resolved, {"path": relative.as_posix(), "sha256": digest.hexdigest(), "bytes": resolved.stat().st_size}


def _png_metadata(path):
    with path.open("rb") as file:
        header = file.read(33)
    if len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[8:16] != b"\0\0\0\rIHDR":
        raise ValueError("bundle maps must be PNG files")
    if (zlib.crc32(header[12:29]) & 0xffffffff) != struct.unpack(">I", header[29:33])[0]:
        raise ValueError("invalid PNG IHDR checksum")
    width, height, bits, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", header[16:29])
    if not width or not height or bits != 8 or color_type not in (0, 2, 6) or compression or filtering or interlace:
        raise ValueError("bundle requires non-interlaced PNG8 grayscale/RGB/RGBA")
    return {"width": width, "height": height, "bits": bits, "png_color_type": color_type}


def build_surface_bundle(root, bindings, *, target="EXPERIENCE", texture_limit=1024,
                         strength_limit=None, alpha_mode="Transparency", mesh_transparency=0.0,
                         output_name="roblox_surface_bundle.json"):
    """Validate and write an offline bundle for explicitly named imported meshes.

    Binding: id, mesh_name, optional mesh_path, maps{color,metal,rough,normal,
    emissive}, emission{accepted,tint_linear,strength,status,report}. Full factor
    results are accepted; their ndarray mask is excluded from the JSON.
    texture_limit is a chosen project constraint, not an asserted engine limit.
    Marketplace's documented strength ceiling remains enforced independently.
    """
    root = Path(root).resolve()
    limit = _target(target, strength_limit)
    if type(texture_limit) is not int or not 1 <= texture_limit <= 4096:
        raise ValueError("texture_limit must be an explicit integer in [1,4096]")
    if alpha_mode not in ("Overlay", "Transparency", "Opaque", "TintMask"):
        raise ValueError("unknown AlphaMode")
    transparency = _nonnegative(mesh_transparency, "mesh_transparency")
    if transparency > 1:
        raise ValueError("mesh_transparency must be in [0,1]")
    if not isinstance(output_name, str) or not output_name.endswith(".json") or Path(output_name).name != output_name or "\\" in output_name or ":" in output_name:
        raise ValueError("output_name must be a plain JSON filename")
    if not isinstance(bindings, (list, tuple)) or not bindings:
        raise ValueError("at least one explicit mesh binding is required")
    prepared, ids, names = [], set(), set()
    for binding in bindings:
        identifier, mesh_name = binding.get("id"), binding.get("mesh_name")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("binding id must be unique and nonempty")
        if not isinstance(mesh_name, str) or not mesh_name or mesh_name in names:
            raise ValueError("mesh_name must be unique and nonempty within supplied model")
        ids.add(identifier)
        names.add(mesh_name)
        maps = binding.get("maps", {})
        if set(maps) != set(_PROPERTIES):
            raise ValueError("exactly color, metal, rough, normal and emissive maps required")
        files, dimensions = {}, set()
        for semantic, value in maps.items():
            path, metadata = _relative_file(root, value)
            info = _png_metadata(path)
            if max(info["width"], info["height"]) > texture_limit:
                raise ValueError("map exceeds the configured texture_limit: " + value)
            allowed = (2, 6) if semantic == "color" else (2,) if semantic == "normal" else (0,)
            if info["png_color_type"] not in allowed:
                raise ValueError("wrong PNG components for " + semantic)
            metadata.update(info)
            metadata.update({"property": _PROPERTIES[semantic],
                             "colorspace": "sRGB RGB; linear straight alpha" if semantic == "color" else "Non-Color",
                             "uploaded_asset_id": None})
            files[semantic] = metadata
            dimensions.add((info["width"], info["height"]))
        if len(dimensions) != 1:
            raise ValueError("all five maps in a binding must have matching dimensions")
        emission = binding.get("emission", {})
        if emission.get("accepted") is not True or emission.get("status") not in ("WITHIN_TOLERANCE", "APPROXIMATED"):
            raise ValueError("refused or missing emission conversion cannot be bundled")
        tint = emission.get("tint_linear")
        if not isinstance(tint, (list, tuple)) or len(tint) != 3:
            raise ValueError("emission tint requires three numeric components")
        tint = [_nonnegative(value, "tint") for value in tint]
        if max(tint) > 1:
            raise ValueError("emission tint must lie in [0,1]")
        strength = _nonnegative(emission.get("strength"), "strength")
        if limit is not None and strength > limit:
            raise ValueError("emission strength exceeds target limit")
        report = emission.get("report")
        if not isinstance(report, dict) or report.get("model") != MODEL:
            raise ValueError("emission requires a report for the declared reference model")
        if emission["status"] == "APPROXIMATED" and report.get("approximation_explicitly_allowed") is not True:
            raise ValueError("approximation requires explicit recorded policy")
        delivered_hashes = report.get("delivered_files")
        if delivered_hashes is not None:
            if not isinstance(delivered_hashes, dict) or set(delivered_hashes) != set(files):
                raise ValueError("conversion report has incomplete delivered file hashes")
            if any(delivered_hashes[key] != files[key]["sha256"] for key in files):
                raise ValueError("delivered files changed after the emission audit")
        record = {"id": identifier, "mesh_name": mesh_name, "maps": files,
                  "surface_appearance": {"ClassName": "SurfaceAppearance", "Name": "ImportGlue_" + identifier,
                                         "AlphaMode": alpha_mode, "Color": [1, 1, 1],
                                         "ResampleMode": "Default", "EmissiveTint": tint, "EmissiveStrength": strength},
                  "mesh_settings": {"Transparency": transparency},
                  "emission_conversion": {key: emission[key] for key in ("accepted", "status", "report")}}
        if binding.get("mesh_path"):
            _, record["mesh_file"] = _relative_file(root, binding["mesh_path"])
        prepared.append(record)
    manifest = {"schema_version": SCHEMA_VERSION, "profile": "ROBLOX_SURFACEAPPEARANCE",
                "reference_date": REFERENCE_DATE, "target": target,
                "texture_limit": texture_limit, "texture_limit_basis": "explicit project policy; official generic texture guide contains both 4096 and 1024 statements",
                "strength_limit": limit, "studio_verified": False, "uploaded": False,
                "emission_model": MODEL, "color_tint_policy": "white; source color already baked",
                "normal_contract": "RGB8 tangent OpenGL (+X,+Y,+Z); mesh tangents required; target decoding and importer tangent retention require Studio verification",
                "mesh_transform_policy": "bind to user-imported MeshParts; importer does not change transforms, units, geometry or UVs",
                "alpha_mode_beta": alpha_mode in ("Opaque", "TintMask"),
                "bindings": prepared, "sources": SOURCES,
                "limitations": ["Numerical emission agreement is measured at texel centers before upload, not rendered Roblox agreement.",
                                "Texture upload/recompression, filtering, HDR clamp, tint interpretation and engine graphics quality need Studio acceptance.",
                                "This bundle cannot represent arbitrary shader closures, coat, transmission, displacement or light transport.",
                                "Hash metadata identifies local source files; the importer cannot verify uploaded asset pixels or ownership.",
                                "Marketplace settings are a bounded profile, not full item eligibility or moderation validation."]}
    payload = json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
    _atomic_bytes(root / "roblox_surface_importer.luau", luau_importer_source().encode("utf-8"))
    _atomic_bytes(root / output_name, payload)
    return manifest


def luau_importer_source():
    """Studio plugin/command context only. No uploads, HTTP, asset IDs, or workspace search."""
    return '''-- Import Glue offline bundle importer, schema 1; Studio verification pending.
-- ModuleScript: require(module).apply(decodedJson, uploadedIdsByRelativePath, model)
-- IDs MUST be user-supplied decimal strings (avoids large-integer precision loss).
-- Import meshes yourself first. Exact unique MeshPart names are resolved only inside model.
local Importer = {}

local function assetUri(ids, file)
    local value = ids[file.path]
    assert(type(value) == "string" and string.match(value, "^[1-9]%d*$"),
        "Missing/invalid user-uploaded ID for " .. tostring(file.path))
    return "rbxassetid://" .. value
end

function Importer.apply(bundle, uploadedIds, model)
    assert(type(bundle) == "table" and bundle.schema_version == 1
        and bundle.profile == "ROBLOX_SURFACEAPPEARANCE", "Unsupported bundle")
    assert(typeof(model) == "Instance" and (model:IsA("Model") or model:IsA("Folder")),
        "Provide the explicit imported Model or Folder")
    assert(type(uploadedIds) == "table", "Provide uploaded IDs keyed by exact relative file path")
    local names = {}
    for _, item in ipairs(model:GetDescendants()) do
        if item:IsA("MeshPart") then
            if names[item.Name] ~= nil then names[item.Name] = false else names[item.Name] = item end
        end
    end
    -- Preflight all bindings before constructing or modifying any instance.
    local plan, selected = {}, {}
    for _, binding in ipairs(bundle.bindings) do
        local mesh = names[binding.mesh_name]
        assert(mesh and not selected[mesh], "Missing, ambiguous, or repeated MeshPart: " .. binding.mesh_name)
        assert(not mesh:FindFirstChildOfClass("SurfaceAppearance"), "Existing SurfaceAppearance: " .. binding.mesh_name)
        selected[mesh] = true
        local settings = binding.surface_appearance
        assert(Enum.AlphaMode[settings.AlphaMode], "AlphaMode unavailable in this Studio version")
        local uris = {}
        for _, key in ipairs({"color", "metal", "rough", "normal", "emissive"}) do
            uris[key] = assetUri(uploadedIds, binding.maps[key])
        end
        table.insert(plan, {mesh = mesh, binding = binding, uris = uris,
            oldTransparency = mesh.Transparency})
    end
    local created = {}
    local ok, problem = pcall(function()
        for _, item in ipairs(plan) do
            local sa = Instance.new("SurfaceAppearance")
            table.insert(created, sa)
            local settings = item.binding.surface_appearance
            sa.Name = settings.Name
            sa.AlphaMode = Enum.AlphaMode[settings.AlphaMode]
            sa.ResampleMode = Enum.ResamplerMode[settings.ResampleMode]
            sa.Color = Color3.new(1, 1, 1)
            sa.ColorMap = item.uris.color
            sa.MetalnessMap = item.uris.metal
            sa.RoughnessMap = item.uris.rough
            sa.NormalMap = item.uris.normal
            sa.EmissiveMaskContent = Content.fromUri(item.uris.emissive)
            sa.EmissiveTint = Color3.new(table.unpack(settings.EmissiveTint))
            sa.EmissiveStrength = settings.EmissiveStrength
            sa:SetAttribute("ImportGlueBinding", item.binding.id)
        end
        for index, item in ipairs(plan) do
            item.mesh.Transparency = item.binding.mesh_settings.Transparency
            created[index].Parent = item.mesh
        end
    end)
    if not ok then
        for _, sa in ipairs(created) do sa:Destroy() end
        for _, item in ipairs(plan) do item.mesh.Transparency = item.oldTransparency end
        error("Import Glue rolled back: " .. tostring(problem))
    end
    return created
end

return Importer
'''
