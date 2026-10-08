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
import subprocess
import time

SCHEMA = 1


def source_statistics(objects, images=(), source_path=""):
    """Cheap source counts, not evaluated modifier geometry or a memory guarantee."""
    vertices = triangles = pixels = 0
    for obj in objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        vertices += len(obj.data.vertices)
        triangles += sum(max(0, len(face.vertices) - 2) for face in obj.data.polygons)
    for image in images:
        if len(image.size) >= 2:
            pixels += max(0, int(image.size[0])) * max(0, int(image.size[1]))
    try:
        size = os.path.getsize(source_path)
    except OSError:
        size = 0
    return {"vertices": vertices, "triangles": triangles,
            "texture_pixels": pixels, "source_bytes": size}
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
    if include_gpu:
        try:
            process = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                                      check=True, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            result["gpus"] = parse_nvidia_csv(process.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result["unavailable"].append("GPU free-memory query unavailable: " + str(exc))
    return result


def estimate_job(source_stats=None, *, resolution=1024):
    """Planning estimate, not an allocation bound. No speed/fit guarantee is made."""
    stats = source_stats or {}
    if not isinstance(stats, dict):
        raise ValueError("source_stats must be a dictionary")
    counts = {key: _positive_integer(stats.get(key, 0), key)
              for key in ("vertices", "triangles", "texture_pixels", "source_bytes")}
    _positive_integer(resolution, "resolution")
    if not 4 <= resolution <= 8192:
        raise ValueError("resolution outside supported limits")
    # Float RGBA input plus evaluated/copy geometry, output/temporary passes and a fixed runtime allowance.
    geometry = counts["vertices"] * 256 + counts["triangles"] * 384
    textures = counts["texture_pixels"] * 16
    passes = resolution * resolution * 16 * 8
    return {"ram_bytes": 512 * _MIB + counts["source_bytes"] * 2 + geometry * 3 + textures * 2 + passes,
            "vram_bytes": 256 * _MIB + geometry * 2 + textures + passes,
            "source_stats": counts, "resolution": resolution,
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
    ram = snapshot.get("ram")
    available_ram = measured_bytes(ram.get("available_bytes") if isinstance(ram, dict) else None)
    if available_ram is None:
        reasons.append("RAM available memory is unmeasured")
    elif estimate["ram_bytes"] > available_ram * (1 - policy["reserve_fraction"]):
        reasons.append("estimated RAM exceeds available memory after reserve")
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
    estimate = estimate_job(source_stats, resolution=max(resolutions))
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
