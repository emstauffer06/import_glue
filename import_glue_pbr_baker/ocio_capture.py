"""Resolve custom OCIO through OCIO itself, including nested LUT dependencies.

The configuration and LUTs remain external under content hashes. No environment
dictionary or unrelated environment values are written to the capture manifest.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import time


def _process_started():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        values = [wintypes.FILETIME() for _ in range(4)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not kernel.GetProcessTimes(wintypes.HANDLE(-1), *(ctypes.byref(item) for item in values)):
            raise ValueError("Cannot establish process start for custom OCIO cache admission")
        return ((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime) / 10_000_000 - 11644473600
    if Path("/proc/self/stat").is_file():
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK")
    raise ValueError("Custom OCIO startup cache verification is unsupported on this platform")


def _unchanged_since_start(path, started):
    stat = Path(path).stat()
    # /proc uptime is quantized to centiseconds. This allowance is smaller than
    # Blender's OCIO initialization interval and avoids false timestamp jitter.
    if max(stat.st_mtime, stat.st_ctime) > started + .05:
        raise ValueError("Custom OCIO resource changed after this Blender process started; restart Blender to load a coherent configuration: " + str(path))


def _runtime_config(path=None, *, check_startup=True):
    import bpy
    import numpy as np
    import PyOpenColorIO as ocio
    from mathutils import Color
    bundled = Path(bpy.utils.system_resource("DATAFILES", path="colormanagement")) / "config.ocio"
    selected = Path(path or os.environ.get("OCIO", "") or bundled).resolve()
    config = ocio.Config.CreateFromFile(str(selected))
    current = ocio.GetCurrentConfig()
    comparable = copy.deepcopy(current)
    # Blender 5.2 adds these two deterministic display encodings internally.
    # Ignore only these known extra entries, not arbitrary runtime edits.
    for name in ("blender:pq_rec2020_display_203nits", "blender:hlg_rec2020_display_203nits"):
        if config.getColorSpace(name) is None and comparable.getColorSpace(name) is not None:
            comparable.removeColorSpace(name)
    if comparable.serialize() != config.serialize():
        raise ValueError("Runtime OCIO configuration structure differs from the file on disk; restart Blender")
    if check_startup and selected != bundled.resolve():
        _unchanged_since_start(selected, _process_started())
    state = bpy.data.colorspace
    if state.is_missing_opencolorio_config:
        raise ValueError("The blend file's required OCIO configuration is missing")
    working = state.working_space
    if config.getColorSpace(working) is None:
        raise ValueError("Actual Blender working space is absent from the captured OCIO config: " + working)
    # Blender can override scene_linear per .blend. Use its actual working
    # space, and compare the real Blender basis with the independent OCIO one.
    processor = ocio.Config.GetProcessorToBuiltinColorSpace(config, working, "lin_rec709_scene")
    proof = _matrix_contract(processor)
    if not proof.get("affine"):
        raise ValueError("Blender working-space basis is not a provable affine OCIO transform")
    actual = np.column_stack([tuple(Color(axis).from_scene_linear_to_rec709_linear())
                              for axis in ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))])
    expected = np.asarray(proof["matrix"])[:3, :3]
    if not np.allclose(actual, expected, rtol=0, atol=2e-5) or max(map(abs, proof["offset"])) > 1e-7:
        raise ValueError("Actual Blender working-space matrix differs from the OCIO file/runtime context")
    return config, current, {"working_space": working, "working_space_interop_id": state.working_space_interop_id,
                             "matrix_max_absolute_difference": float(np.max(np.abs(actual - expected)))}


def _matrix_contract(processor, *, srgb=False):
    """Prove a matrix-only transform (plus the canonical sRGB transfer).

    Finite test colors alone could miss a nonlinear LUT between those colors.
    Unknown transform types therefore refuse instead of passing sampled checks.
    """
    import numpy as np
    import PyOpenColorIO as ocio
    matrix, offset = np.eye(4), np.zeros(4)
    transforms = list(processor.createGroupTransform())
    if srgb:
        if not transforms or not isinstance(transforms[-1], ocio.ExponentWithLinearTransform):
            return {"compatible": False, "reason": "sRGB transfer is not an explicit canonical exponent-with-linear transform"}
        transfer = transforms.pop()
        if (transfer.getDirection() != ocio.TRANSFORM_DIR_INVERSE or
                not np.allclose(transfer.getGamma(), [2.4, 2.4, 2.4, 1], rtol=0, atol=1e-12) or
                not np.allclose(transfer.getOffset(), [.055, .055, .055, 0], rtol=0, atol=1e-12) or
                transfer.getNegativeStyle() != ocio.NEGATIVE_LINEAR):
            return {"compatible": False, "reason": "sRGB transfer parameters differ from the delivery model"}
    for transform in transforms:
        if not isinstance(transform, ocio.MatrixTransform):
            return {"compatible": False, "reason": "Color transform contains an unproven nonlinear operation: " + type(transform).__name__}
        item = np.asarray(transform.getMatrix()).reshape(4, 4)
        bias = np.asarray(transform.getOffset())
        if transform.getDirection() == ocio.TRANSFORM_DIR_INVERSE:
            item = np.linalg.inv(item)
            bias = -item @ bias
        offset = item @ offset + bias
        matrix = item @ matrix
    error = float(np.max(np.sum(np.abs(matrix - np.eye(4)), axis=1)))
    bias_error = float(np.max(np.abs(offset)))
    compatible = bool(np.isfinite(matrix).all() and np.isfinite(offset).all() and error <= 1e-5 and bias_error <= 1e-7)
    return {"compatible": compatible, "affine": True, "matrix": matrix.tolist(), "offset": offset.tolist(),
            "matrix_max_row_absolute_error": error, "offset_max_absolute_error": bias_error,
            "reason": "Matrix transform agrees with the delivery color basis" if compatible else "Color basis differs from linear Rec.709"}


def target_contract():
    """Runtime role names and structural color proof for native/Roblox routing."""
    import PyOpenColorIO as ocio
    result = {"native_supported": False, "roblox_supported": False,
              "native_reasons": [], "roblox_reasons": [], "roles": {}}
    try:
        config, _current, runtime = _runtime_config()
        result["runtime"] = runtime
        result["config"] = os.environ.get("OCIO", "")
        linear = config.getColorSpace(runtime["working_space"])
        data = config.getColorSpace(ocio.ROLE_DATA)
        if data is None or not data.isData():
            data = next((space for space in config.getColorSpaces(ocio.SEARCH_REFERENCE_SPACE_ALL, ocio.COLORSPACE_ALL)
                         if space.isData()), None)
        result["roles"] = {"scene_linear": linear.getName() if linear else None,
                           "data": data.getName() if data else None}
        result["views"] = {display: list(config.getViews(display)) for display in config.getDisplays()}
        if linear is None:
            result["native_reasons"].append("OCIO scene_linear role is unavailable")
        if data is None:
            result["native_reasons"].append("OCIO has no verified data color space for native field textures")
        result["native_supported"] = not result["native_reasons"]
        if not result["native_supported"]:
            result["roblox_reasons"].extend(result["native_reasons"])
        else:
            proof = _matrix_contract(ocio.Config.GetProcessorToBuiltinColorSpace(config, linear.getName(), "lin_rec709_scene"))
            result["scene_linear_to_rec709"] = proof
            if not proof["compatible"]:
                result["roblox_reasons"].append(proof["reason"])
            if config.getColorSpace("sRGB") is None:
                result["roblox_reasons"].append("Roblox delivery requires a configured sRGB color space")
            else:
                proof = _matrix_contract(config.getProcessor(linear.getName(), "sRGB"), srgb=True)
                result["scene_linear_to_srgb"] = proof
                if not proof["compatible"]:
                    result["roblox_reasons"].append(proof["reason"])
            # The legacy Roblox baking implementation uses these fixed names.
            noncolor = config.getColorSpace("Non-Color")
            if noncolor is None or not noncolor.isData():
                result["roblox_reasons"].append("Roblox delivery requires the verified Non-Color data space")
            if not any("Standard" in views for views in result["views"].values()):
                result["roblox_reasons"].append("Roblox delivery requires the Standard view supported by its current bake implementation")
        result["roblox_supported"] = not result["roblox_reasons"]
    except Exception as exc:
        reason = "OCIO target color contract could not be proved: " + str(exc)
        if not result["native_supported"]:
            result["native_reasons"].append(reason)
        result["roblox_reasons"].append(reason)
        result["roblox_supported"] = False
    return result


def inspect_config(path, register, check=lambda: None):
    import PyOpenColorIO as ocio
    path = Path(path).resolve()
    started = _process_started()
    _unchanged_since_start(path, started)
    register(path, "OCIO_CONFIG")
    config, runtime_config, runtime = _runtime_config(path)
    config.validate()
    context = config.getCurrentContext()
    transforms = []
    for space in config.getColorSpaces(ocio.SEARCH_REFERENCE_SPACE_ALL, ocio.COLORSPACE_ALL):
        for direction in (ocio.COLORSPACE_DIR_TO_REFERENCE, ocio.COLORSPACE_DIR_FROM_REFERENCE):
            transforms.append(("space/" + space.getName() + "/" + str(direction), space.getTransform(direction)))
    for view in config.getViewTransforms():
        for direction in (ocio.VIEWTRANSFORM_DIR_TO_REFERENCE, ocio.VIEWTRANSFORM_DIR_FROM_REFERENCE):
            transforms.append(("view/" + view.getName() + "/" + str(direction), view.getTransform(direction)))
    for look in config.getLooks():
        transforms.extend((("look/" + look.getName() + "/forward", look.getTransform()),
                           ("look/" + look.getName() + "/inverse", look.getInverseTransform())))
    for named in config.getNamedTransforms(ocio.NAMEDTRANSFORM_ALL):
        for direction in (ocio.TRANSFORM_DIR_FORWARD, ocio.TRANSFORM_DIR_INVERSE):
            transforms.append(("named/" + named.getName() + "/" + str(direction), named.getTransform(direction)))
    if len(transforms) > 4096:
        raise ValueError("Custom OCIO transform inventory exceeds capture budget")
    processors = []
    for identity, transform in transforms:
        check()
        if transform is None:
            continue
        processor = config.getProcessor(context, transform, ocio.TRANSFORM_DIR_FORWARD)
        runtime_processor = runtime_config.getProcessor(runtime_config.getCurrentContext(), transform, ocio.TRANSFORM_DIR_FORWARD)
        if runtime_processor.getCacheID() != processor.getCacheID():
            raise ValueError("Runtime OCIO processor differs from the on-disk/context closure: " + identity)
        paths = []
        for filename in processor.getProcessorMetadata().getFiles():
            resolved = Path(context.resolveFileLocation(filename)).resolve()
            _unchanged_since_start(resolved, started)
            register(resolved, "OCIO_LUT")
            paths.append(str(resolved))
        processors.append({"identity": identity, "cache_id": processor.getCacheID(), "files": sorted(set(paths))})
    blob = json.dumps(processors, sort_keys=True, separators=(",", ":")).encode()
    return {"kind": "CUSTOM_OCIO", "config": str(path),
            "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "processor_sha256": hashlib.sha256(blob).hexdigest(),
            "processor_count": len(processors), "ocio_version": ocio.__version__,
            "runtime": runtime,
            "closure_policy": "all declared transforms resolved by OCIO; external content checksums"}
