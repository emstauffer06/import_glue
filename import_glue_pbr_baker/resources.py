"""Measured resource availability and explicit, conservative job admission.

No allocation is reserved by an admission result. Free memory can change immediately.
Unavailable measurements remain None; they are never substituted with total capacity.
"""
from __future__ import annotations

import csv
import ctypes
import io
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time

SCHEMA = 1


def source_statistics(objects, images=(), source_path=""):
    """Cheap source counts, not evaluated modifier geometry or a memory guarantee."""
    vertices = triangles = pixels = max_pixels = 0
    unknown_tiles, tile_count, uncertainties = 0, 0, []
    objects = list(objects)
    # Source-invariance hashes visit every original material slot and node,
    # including unreachable resources, so their extraction cost also counts.
    def identity(value):
        return value.as_pointer() if hasattr(value, "as_pointer") else id(value)
    all_images = {identity(image): image for image in images}
    trees = [material.node_tree for obj in objects for slot in getattr(obj, "material_slots", ())
             if (material := getattr(slot, "material", None)) is not None and getattr(material, "node_tree", None) is not None]
    visited = set()
    while trees:
        tree = trees.pop()
        if identity(tree) in visited:
            continue
        visited.add(identity(tree))
        for node in tree.nodes:
            image = getattr(node, "image", None)
            if image is not None:
                all_images[identity(image)] = image
            if getattr(node, "node_tree", None) is not None:
                trees.append(node.node_tree)
    for obj in objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        vertices += len(obj.data.vertices)
        triangles += sum(max(0, len(face.vertices) - 2) for face in obj.data.polygons)
    for image in all_images.values():
        if getattr(image, "source", "") == "MOVIE":
            # Even querying movie size can start a decoder and alter Blender's
            # image state. Videos are outside the static capture contract.
            unknown_tiles += 1
            pixels += 8192 ** 2
            max_pixels = max(max_pixels, 8192 ** 2)
            uncertainties.append("Video image has no verified static dimensions: " + str(getattr(image, "name", "image")))
            continue
        exposed = list(getattr(image, "size", (0, 0)))
        exposed_pixels = max(0, int(exposed[0])) * max(0, int(exposed[1])) if len(exposed) >= 2 else 0
        if getattr(image, "source", "") == "TILED":
            sizes = []
            for tile in getattr(image, "tiles", ()):
                value = list(getattr(tile, "size", (0, 0)))
                sizes.append(max(0, int(value[0])) * max(0, int(value[1])) if len(value) >= 2 else 0)
            if not sizes:
                sizes = [0]
            tile_count += len(sizes)
            for count in sizes:
                if count:
                    pixels += count
                    max_pixels = max(max_pixels, count)
                else:
                    unknown_tiles += 1
                    # This is a planning fallback, explicitly not a proven
                    # upper bound. Strict admission refuses unknown dimensions.
                    fallback = max(8192 ** 2, exposed_pixels, max(sizes))
                    pixels += fallback
                    max_pixels = max(max_pixels, fallback)
            if any(not count for count in sizes):
                uncertainties.append("UDIM tile dimensions unavailable: " + str(getattr(image, "name", "image")))
        else:
            pixels += exposed_pixels
            max_pixels = max(max_pixels, exposed_pixels)
            if not exposed_pixels:
                unknown_tiles += 1
                pixels += 8192 ** 2
                max_pixels = max(max_pixels, 8192 ** 2)
                uncertainties.append("Image dimensions unavailable: " + str(getattr(image, "name", "image")))
    try:
        size = os.path.getsize(source_path)
    except OSError:
        size = 0
    return {"vertices": vertices, "triangles": triangles,
            "texture_pixels": pixels, "max_texture_pixels": max_pixels, "source_bytes": size, "udim_tiles": tile_count,
            "unknown_texture_tiles": unknown_tiles, "texture_size_uncertainties": uncertainties}
_MIB = 1024 ** 2
_POLICY_KEYS = frozenset({"enforce", "reserve_fraction", "estimated_ram_bytes",
                          "estimated_vram_bytes", "device_id"})


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("%s must be a nonnegative integer" % name)
    return value


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) - _POLICY_KEYS:
        raise ValueError("Invalid resource policy keys")
    result = {"enforce": False, "reserve_fraction": 0.2, **policy}
    if not isinstance(result["enforce"], bool):
        raise ValueError("enforce must be boolean")
    reserve = result["reserve_fraction"]
    if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or not math.isfinite(reserve) or not 0 <= reserve <= 0.8:
        raise ValueError("reserve_fraction must be finite and between 0 and 0.8")
    for key in ("estimated_ram_bytes", "estimated_vram_bytes"):
        if key in result:
            _positive_integer(result[key], key)
    if "device_id" in result and (not isinstance(result["device_id"], str) or len(result["device_id"]) > 512):
        raise ValueError("device_id must be a short string")
    return result


def _linux_memory(proc_root="/proc", cgroup_root="/sys/fs/cgroup"):
    fields = {}
    for line in (Path(proc_root) / "meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        columns = value.split()
        if columns:
            fields[name] = int(columns[0]) * (1024 if len(columns) > 1 and columns[1] == "kB" else 1)
    result = {"total_bytes": fields.get("MemTotal"), "available_bytes": fields.get("MemAvailable"),
              "source": "Linux /proc/meminfo MemAvailable", "cgroup": None}
    # Resolve this process's cgroup-v2 location, rather than assuming it is at the mount root.
    relative = ""
    try:
        for line in (Path(proc_root) / "self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                relative = line[3:].lstrip("/")
                break
    except OSError:
        pass
    root = Path(cgroup_root).resolve()
    current_dir = (root / relative).resolve()
    if current_dir != root and root not in current_dir.parents:
        current_dir = root
    caps = []
    # Parent limits also constrain nested jobs. An unreadable node is explicitly skipped.
    for directory in (current_dir, *current_dir.parents):
        if directory != root and root not in directory.parents:
            break
        try:
            maximum = (directory / "memory.max").read_text().strip()
            used = int((directory / "memory.current").read_text().strip())
            if maximum != "max":
                limit = int(maximum)
                caps.append({"path": str(directory), "limit_bytes": limit, "current_bytes": used,
                             "available_bytes": max(0, limit - used)})
        except (OSError, ValueError):
            continue
    if caps:
        cap = min(caps, key=lambda item: item["available_bytes"])
        result["cgroup"] = {"selected": cap, "observed": caps,
                            "basis": "conservative limit minus current; no speculative cache reclaim"}
        if result["available_bytes"] is not None:
            result["available_bytes"] = min(result["available_bytes"], cap["available_bytes"])
        result["total_bytes"] = min([v for v in [result["total_bytes"]] +
                                     [c["limit_bytes"] for c in caps] if v is not None])
    return result


def _windows_memory():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]
    state = MEMORYSTATUSEX()
    state.length = ctypes.sizeof(state)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        raise OSError("GlobalMemoryStatusEx failed")
    return {"total_bytes": int(state.total_phys), "available_bytes": int(state.avail_phys),
            "source": "Windows GlobalMemoryStatusEx", "cgroup": None}


def parse_nvidia_csv(text):
    devices = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 6:
            raise ValueError("Unexpected NVIDIA query columns")
        index, uuid, name, total, free, utilization = [part.strip() for part in row]
        # N/A is unavailable, not zero. Keep recognized identity even if its metrics are absent.
        def metric(raw, multiplier=1):
            try:
                value = float(raw)
                return int(value * multiplier) if math.isfinite(value) and value >= 0 else None
            except ValueError:
                return None
        devices.append({"index": index, "id": uuid, "name": name,
                        "total_bytes": metric(total, _MIB), "available_bytes": metric(free, _MIB),
                        "utilization_percent": metric(utilization), "source": "nvidia-smi memory.free"})
    return devices


def collect_snapshot(*, include_gpu=True):
    result = {"schema": SCHEMA, "sampled_unix": time.time(), "platform": platform.system(),
              "cpu_count": os.cpu_count(), "ram": {"total_bytes": None, "available_bytes": None,
              "source": "unavailable", "cgroup": None}, "gpus": [], "unavailable": []}
    try:
        if result["platform"] == "Linux":
            result["ram"] = _linux_memory()
        elif result["platform"] == "Windows":
            result["ram"] = _windows_memory()
        else:
            result["unavailable"].append("RAM adapter not implemented for " + result["platform"])
    except (OSError, ValueError, AttributeError) as exc:
        result["unavailable"].append("RAM: " + str(exc))
    try:
        directory = tempfile.gettempdir()
        result["temporary_disk"] = {"path": directory, "available_bytes": shutil.disk_usage(directory).free,
                                    "source": "temporary directory filesystem free space"}
    except (OSError, ValueError) as exc:
        result["temporary_disk"] = {"available_bytes": None}
        result["unavailable"].append("Temporary disk: " + str(exc))
    if include_gpu:
        try:
            process = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                                      check=True, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            result["gpus"] = parse_nvidia_csv(process.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result["unavailable"].append("GPU free-memory query unavailable: " + str(exc))
    return result


def estimate_job(source_stats=None, *, resolution=1024, target="LEGACY", quality_settings=None):
    """Planning estimate, not an allocation bound. No speed/fit guarantee is made."""
    stats = source_stats or {}
    if not isinstance(stats, dict):
        raise ValueError("source_stats must be a dictionary")
    counts = {key: _positive_integer(stats.get(key, 0), key)
              for key in ("vertices", "triangles", "texture_pixels", "source_bytes", "objects",
                          "eligible_fields", "material_nodes", "material_instances", "udim_tiles", "unknown_texture_tiles")}
    # Old capture manifests omit the largest image; conservatively use the
    # total decoded image count rather than silently estimating zero overhead.
    counts["max_texture_pixels"] = _positive_integer(stats.get("max_texture_pixels", counts["texture_pixels"]), "max_texture_pixels")
    _positive_integer(resolution, "resolution")
    if not 4 <= resolution <= 8192:
        raise ValueError("resolution outside supported limits")
    # Float RGBA input plus evaluated/copy geometry, output/temporary passes and a fixed runtime allowance.
    geometry = counts["vertices"] * 256 + counts["triangles"] * 384
    textures = counts["texture_pixels"] * 16
    fingerprint_bytes = counts["max_texture_pixels"] * 16
    fingerprint_disk = fingerprint_bytes if fingerprint_bytes > 256 * _MIB else 0
    passes = resolution * resolution * 16 * 8
    if target not in {"LEGACY", "BLENDER_NATIVE", "ROBLOX", "BOTH"}:
        raise ValueError("Unknown output target")
    native = target in {"BLENDER_NATIVE", "BOTH"}
    # Native outputs remain resident while later objects/targets bake. Include
    # cloned graph storage, retained float fields and positive/negative probes,
    # reference samples, island masks, comparisons and padded output buffers.
    retained_fields = counts["eligible_fields"] * resolution * resolution * 16 if native else 0
    graph_copies = counts["material_nodes"] * 4096 * 3 + counts["material_instances"] * 65536 if native else 0
    native_temps = resolution * resolution * 16 * 24 if native else 0
    retained_roblox = counts["objects"] * resolution * resolution * 16 * 5 if target in {"ROBLOX", "BOTH"} else 0
    retained = retained_fields + graph_copies + retained_roblox
    render_peak = max(passes, native_temps)
    # Verified still-image capture retains source/copy arrays, packed bytes
    # and independently decoded roundtrip buffers. The 8K float acceptance
    # reached 10.31 GiB RSS; allow eight extra decoded buffers in addition to
    # resident source/output images and the explicit fingerprint allocation.
    image_capture_temps = fingerprint_bytes * 8
    peak = max(render_peak, image_capture_temps)
    geometry_capture = _positive_integer(stats.get("geometry_working_bytes", 0), "geometry_working_bytes")
    fit_temps = _positive_integer(stats.get("fit_temporary_bytes", 0), "fit_temporary_bytes")
    return {"ram_bytes": 512 * _MIB + counts["source_bytes"] * 2 + geometry * 3 + textures * 2 + peak + retained + fingerprint_bytes + geometry_capture + fit_temps,
            "vram_bytes": 256 * _MIB + geometry * 2 + textures + render_peak + retained_fields + retained_roblox + geometry_capture + fit_temps,
            "source_stats": counts, "resolution": resolution,
            "target": target, "retained_output_bytes": retained, "peak_temporary_bytes": peak,
            "graph_copy_bytes": graph_copies, "native_field_bytes": retained_fields,
            "geometry_capture_bytes": geometry_capture, "material_fit_temporary_bytes": fit_temps,
            "fingerprint_temporary_bytes": fingerprint_bytes, "fingerprint_disk_bytes": fingerprint_disk,
            "image_capture_temporary_bytes": image_capture_temps,
            "fingerprint_disk_reserve_bytes": 64 * _MIB if fingerprint_disk else 0,
            "fingerprint_basis": "one contiguous float32 extraction; large file-backed mapping may be fully resident in RAM",
            "unknown_texture_tiles": counts["unknown_texture_tiles"],
            "uncertainties": list(stats.get("texture_size_uncertainties", [])),
            "basis": "conservative planning heuristic; Blender/shader/backend overhead is scene-dependent"}


def admission(estimate, snapshot, *, device="CPU", device_id="", reserve_fraction=0.2, enforce=False):
    policy = validate_policy({"enforce": enforce, "reserve_fraction": reserve_fraction, "device_id": device_id})
    if not isinstance(estimate, dict) or not isinstance(snapshot, dict):
        raise ValueError("estimate and snapshot must be dictionaries")
    for key in ("ram_bytes", "vram_bytes"):
        _positive_integer(estimate.get(key), key)
    if not isinstance(device, str) or device.upper() not in {"CPU", "GPU"}:
        raise ValueError("device must be CPU or GPU")
    def measured_bytes(value):
        # Invalid telemetry is unmeasured, never a successful comparison against NaN/inf or bool.
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    reasons = []
    if estimate.get("unknown_texture_tiles", 0):
        reasons.append("Texture/UDIM tile dimensions are unmeasured; fallback memory estimate is not an upper bound")
    ram = snapshot.get("ram")
    available_ram = measured_bytes(ram.get("available_bytes") if isinstance(ram, dict) else None)
    if available_ram is None:
        reasons.append("RAM available memory is unmeasured")
    elif estimate["ram_bytes"] > available_ram * (1 - policy["reserve_fraction"]):
        reasons.append("estimated RAM exceeds available memory after reserve")
    disk_bytes = estimate.get("fingerprint_disk_bytes", 0)
    if disk_bytes:
        temporary_disk = snapshot.get("temporary_disk")
        free_disk = measured_bytes(temporary_disk.get("available_bytes") if isinstance(temporary_disk, dict) else None)
        if free_disk is None:
            reasons.append("temporary disk free space is unmeasured for live-image fingerprint")
        elif disk_bytes + estimate.get("fingerprint_disk_reserve_bytes", 64 * _MIB) > free_disk:
            reasons.append("live-image fingerprint exceeds temporary disk free space after reserve")
    selected = None
    if device.upper() == "GPU":
        devices = snapshot.get("gpus", [])
        devices = [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
        matches = [d for d in devices if device_id in (d.get("id"), d.get("index"), d.get("name"))] if device_id else devices
        if len(matches) == 1:
            selected = matches[0]
            available = measured_bytes(selected.get("available_bytes"))
            if available is None:
                reasons.append("selected GPU available memory is unmeasured")
            elif estimate["vram_bytes"] > available * (1 - policy["reserve_fraction"]):
                reasons.append("estimated VRAM exceeds selected GPU available memory after reserve")
        else:
            reasons.append("GPU memory identity is unavailable or ambiguous; select a unique device")
    return {"allowed": not reasons or not enforce, "assessment": "unverified" if reasons else "within_estimate",
            "reason": "; ".join(reasons) if reasons else "planning estimate fits measured availability",
            "enforced": enforce, "selected_gpu": selected, "estimate": estimate, "snapshot": snapshot,
            "reservation": False}


def evaluate_policy(resource_policy, engine_settings, source_stats=None):
    policy = validate_policy(resource_policy or {})
    settings = engine_settings
    if not isinstance(settings, dict):
        raise ValueError("engine_settings must be a dictionary")
    missing = {"RES", "MAX_RES", "DEVICE"} - settings.keys()
    if missing:
        raise ValueError("Effective engine settings required for resource admission: " + ", ".join(sorted(missing)))
    resolutions = [settings["RES"], settings["MAX_RES"]]
    for value in resolutions:
        _positive_integer(value, "resolution")
        if not 4 <= value <= 8192:
            raise ValueError("resolution outside supported limits")
    estimate = estimate_job(source_stats, resolution=max(resolutions),
                            target=settings.get("TARGET_PROFILE", "LEGACY"),
                            quality_settings=settings.get("NATIVE_QUALITY"))
    for field, target in (("estimated_ram_bytes", "ram_bytes"), ("estimated_vram_bytes", "vram_bytes")):
        if field in policy:
            estimate[target] = max(estimate[target], policy[field])
    device = settings["DEVICE"]
    if not isinstance(device, str) or device.upper() not in {"CPU", "GPU"}:
        raise ValueError("device must be CPU or GPU")
    device = device.upper()
    engine_id = settings.get("GPU_DEVICE_ID", "")
    validate_policy({"device_id": engine_id})
    policy_id = policy.get("device_id", "")
    selected_id = policy_id or engine_id
    result = admission(estimate, collect_snapshot(include_gpu=device == "GPU"), device=device,
                       device_id=selected_id, reserve_fraction=policy["reserve_fraction"], enforce=policy["enforce"])
    result["engine_device_id"] = engine_id
    result["admission_device_id"] = selected_id
    if device == "GPU" and engine_id and policy_id and engine_id != policy_id:
        # Cycles IDs and telemetry UUIDs can differ even on one physical card.
        # Without an explicit mapping we cannot certify that the admitted card is the selected one.
        result["assessment"] = "unverified"
        result["allowed"] = not policy["enforce"]
        result["reason"] += "; engine and admission device selectors differ; physical identity mapping is unverified"
    return result
