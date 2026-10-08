"""Durable, one-worker background jobs. Pure stdlib; safe to import without bpy.

The immutable job input is sealed after a private snapshot has been saved. Worker
status is atomic JSON, never Python/pickle. Cancellation is cooperative; only a
Popen handle owned by this interpreter can be explicitly terminated. Reopening
a job never claims ownership of an arbitrary PID. No checkpoint is deleted.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

SCHEMA = 1
MAX_JSON_BYTES = 256 * 1024
MAX_TEXT = 8192
WORKER_THREADS = min(4, os.cpu_count() or 1)
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "CRASHED", "TERMINATED", "REFUSED"})
ENGINE_KEYS = frozenset({
    "OUTPUT_ROOT", "RES", "MAX_RES", "BAKE_DENSITY_SCALE", "DEVICE", "ALLOW_CPU_FALLBACK",
    "ALLOW_APPROXIMATION", "SOURCE_DIRS", "ROUTE_MODE", "SOURCE_NORMAL_IS_DIRECTX", "EXPORT_GLTF",
    "EXPORT_FBX", "AUTO_UNWRAP_NO_UV", "DONE_LIST", "DURABLE_CHECKPOINTS", "TRI_BUDGET",
    "ENFORCE_TRI_BUDGET", "CROP_SPLIT_PREPASS", "FINALIZE_IN_SESSION", "VISUAL_VALIDATION",
    "VISUAL_VALIDATION_GATE", "VISUAL_VALIDATION_SETTINGS",
    "GPU_DEVICE_ID", "OUTPUT_PROFILE", "TARGET_PROFILE", "ROBLOX_TEXTURE_LIMIT", "NATIVE_QUALITY",
    "ROBLOX_MATERIAL_FIT", "ROBLOX_FIT_SETTINGS", "ROBLOX_GEOMETRY", "ROBLOX_GEOMETRY_SETTINGS",
})
PRECHECK_KEYS = frozenset({"enabled", "pool", "repair", "repair_into_staging", "repair_mode",
                          "allow_missing", "allow_corrupt", "strict_absolute"})
ARG_KEYS = frozenset({"tag", "mem-gate", "done", "rerun", "no-resume", "repair", "no-finalize"})
_OWNED = {}
_MUTEX = threading.RLock()


class JobError(RuntimeError):
    pass


def _json_copy(value):
    """Reject non-JSON, nonfinite, deep/large or custom Python values before writing."""
    count = [0]
    def walk(v, depth=0):
        count[0] += 1
        if depth > 12 or count[0] > 20000:
            raise JobError("JSON structure exceeds bounded protocol limits")
        if v is None or type(v) in (bool, int):
            return
        if type(v) is float:
            if not math.isfinite(v):
                raise JobError("JSON numbers must be finite")
            return
        if type(v) is str:
            if len(v) > MAX_TEXT or "\x00" in v:
                raise JobError("JSON string exceeds limit or contains NUL")
            return
        if type(v) is list:
            for x in v: walk(x, depth + 1)
            return
        if type(v) is dict:
            for k, x in v.items():
                if type(k) is not str: raise JobError("JSON keys must be strings")
                walk(k, depth + 1)
                walk(x, depth + 1)
            return
        raise JobError("Only plain JSON types are accepted")
    walk(value)
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(blob) > MAX_JSON_BYTES:
        raise JobError("JSON document exceeds protocol byte limit")
    return json.loads(blob)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out: raise JobError("Duplicate JSON key: " + key)
        out[key] = value
    return out


def read_json(path):
    with open(path, "rb") as handle:
        blob = handle.read(MAX_JSON_BYTES + 1)
    if len(blob) > MAX_JSON_BYTES: raise JobError("Oversized protocol file")
    try:
        value = json.loads(blob, object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise JobError("Invalid JSON protocol file: %s" % exc) from exc
    if type(value) is not dict: raise JobError("Protocol document must be a JSON object")
    return _json_copy(value)


def atomic_json(path, value):
    path = Path(path)
    data = _json_copy(value)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_fingerprint(directory=None):
    directory = Path(directory or Path(__file__).parent).resolve()
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _inside(job_dir, filename):
    base = Path(job_dir).resolve()
    path = base / filename
    if path.resolve().parent != base or path.is_symlink():
        raise JobError("Protocol path must stay inside the private job directory")
    return path


def _mapping(value, allowed, label):
    value = _json_copy(value or {})
    if not isinstance(value, dict): raise JobError(label + " must be an object")
    unknown = set(value) - allowed
    if unknown: raise JobError("Unknown %s keys: %s" % (label, ", ".join(sorted(unknown))))
    return value


def _settings(value, source):
    settings = _mapping(value, ENGINE_KEYS, "engine setting")
    for key in ENGINE_KEYS - {"OUTPUT_ROOT", "RES", "MAX_RES", "BAKE_DENSITY_SCALE", "DEVICE",
                              "SOURCE_DIRS", "ROUTE_MODE", "TRI_BUDGET", "VISUAL_VALIDATION_SETTINGS", "GPU_DEVICE_ID", "OUTPUT_PROFILE", "TARGET_PROFILE", "ROBLOX_TEXTURE_LIMIT", "NATIVE_QUALITY", "ROBLOX_FIT_SETTINGS", "ROBLOX_GEOMETRY_SETTINGS"}:
        if key in settings and type(settings[key]) is not bool:
            raise JobError(key + " must be a boolean")
    for key in ("RES", "MAX_RES"):
        if key in settings and (type(settings[key]) is not int or not 4 <= settings[key] <= 8192):
            raise JobError(key + " must be an integer in [4,8192]")
    if settings.get("MAX_RES", 8192) < settings.get("RES", 4):
        raise JobError("MAX_RES must be at least RES")
    if "BAKE_DENSITY_SCALE" in settings and (type(settings["BAKE_DENSITY_SCALE"]) not in (int, float)
                                              or not 0.05 <= settings["BAKE_DENSITY_SCALE"] <= 8):
        raise JobError("BAKE_DENSITY_SCALE must be in [0.05,8]")
    if "DEVICE" in settings and settings["DEVICE"] not in ("CPU", "GPU"):
        raise JobError("DEVICE must be CPU or GPU")
    if "GPU_DEVICE_ID" in settings and type(settings["GPU_DEVICE_ID"]) is not str:
        raise JobError("GPU_DEVICE_ID must be a string")
    if "OUTPUT_PROFILE" in settings and settings["OUTPUT_PROFILE"] not in ("PBR_BASE", "PBR_HIGH_PRECISION"):
        raise JobError("Unsupported OUTPUT_PROFILE")
    if "TARGET_PROFILE" in settings and settings["TARGET_PROFILE"] not in ("LEGACY", "BLENDER_NATIVE", "ROBLOX", "BOTH"):
        raise JobError("Unsupported TARGET_PROFILE")
    if "ROBLOX_TEXTURE_LIMIT" in settings and (type(settings["ROBLOX_TEXTURE_LIMIT"]) is not int or
                                               not 4 <= settings["ROBLOX_TEXTURE_LIMIT"] <= 8192):
        raise JobError("ROBLOX_TEXTURE_LIMIT must be an integer in [4,8192]")
    if "ROUTE_MODE" in settings and settings["ROUTE_MODE"] not in ("AUTO", "CROP_ONLY", "PROXY_ONLY", "GRAPH_ONLY", "BAKE_ONLY"):
        raise JobError("Unsupported route")
    if "TRI_BUDGET" in settings and (type(settings["TRI_BUDGET"]) is not int or settings["TRI_BUDGET"] < 1):
        raise JobError("TRI_BUDGET must be a positive integer")
    if "SOURCE_DIRS" in settings and (type(settings["SOURCE_DIRS"]) is not list
                                      or any(type(p) is not str or not os.path.isabs(p) for p in settings["SOURCE_DIRS"])):
        raise JobError("SOURCE_DIRS must contain absolute paths")
    if "VISUAL_VALIDATION_SETTINGS" in settings and settings["VISUAL_VALIDATION_SETTINGS"] is not None and type(settings["VISUAL_VALIDATION_SETTINGS"]) is not dict:
        raise JobError("VISUAL_VALIDATION_SETTINGS must be JSON object or null")
    if "NATIVE_QUALITY" in settings and settings["NATIVE_QUALITY"] is not None and type(settings["NATIVE_QUALITY"]) is not dict:
        raise JobError("NATIVE_QUALITY must be JSON object or null")
    for name in ("ROBLOX_FIT_SETTINGS", "ROBLOX_GEOMETRY_SETTINGS"):
        if name in settings and type(settings[name]) is not dict:
            raise JobError(name + " must be a JSON object")
    if (settings.get("ROBLOX_MATERIAL_FIT") or settings.get("ROBLOX_GEOMETRY")) and not settings.get("ALLOW_APPROXIMATION", False):
        raise JobError("Roblox fitting/geometry conversion requires explicit ALLOW_APPROXIMATION")
    output = settings.get("OUTPUT_ROOT") or str(Path(source).parent)
    if type(output) is not str or not os.path.isabs(output): raise JobError("OUTPUT_ROOT must be absolute")
    settings["OUTPUT_ROOT"] = str(Path(output).resolve())
    # Worker isolation is explicit in the sealed effective settings; never save over its immutable input.
    settings["FINALIZE_IN_SESSION"] = False
    return settings


def _precheck(value):
    result = {"enabled": True, "pool": "", "repair": False, "repair_into_staging": True,
              "repair_mode": "AUTO", "allow_missing": False, "allow_corrupt": False, "strict_absolute": True}
    result.update(_mapping(value, PRECHECK_KEYS, "precheck setting"))
    for key in PRECHECK_KEYS - {"pool", "repair_mode"}:
        if type(result[key]) is not bool: raise JobError(key + " must be boolean")
    if type(result["pool"]) is not str or (result["pool"] and not os.path.isabs(result["pool"])):
        raise JobError("Precheck pool must be empty or absolute")
    if result["repair_mode"] not in ("AUTO", "COPY", "HARDLINK", "SYMLINK"):
        raise JobError("Unsupported repair mode")
    if result["repair"] and not result["repair_into_staging"]:
        raise JobError("Background texture repair requires staging; source files are protected")
    return result


def _resource_policy(value):
    # Normal packaged import, with a pure-file path for CLI unit tools that deliberately avoid bpy/__init__.
    if __package__:
        from . import resources
    else:
        spec = importlib.util.spec_from_file_location("_import_glue_resources_protocol", Path(__file__).with_name("resources.py"))
        resources = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resources)
    try:
        return resources.validate_policy(value)
    except (ValueError, TypeError) as exc:
        raise JobError("Invalid resource policy: " + str(exc)) from exc


def create_job(job_root, source_blend, object_names, engine_settings, *, snapshot_writer=None,
               engine_args=None, resource_policy=None, precheck_settings=None, resume_from=None,
               dependency_files=None):
    source = Path(source_blend).resolve()
    if not source.is_file() or source.suffix.lower() != ".blend":
        raise JobError("A saved source .blend is required")
    objects = []
    identities = _json_copy(object_names)
    if type(identities) is not list: raise JobError("Source identities must be a list")
    for item in identities:
        item = {"name": item, "library": ""} if isinstance(item, str) else item
        if type(item) is not dict or set(item) != {"name", "library"} or not isinstance(item["name"], str) or not item["name"]:
            raise JobError("Source identities require name and library")
        if item["library"] is None: item["library"] = ""
        if type(item["library"]) is not str: raise JobError("Library must be a string")
        if item in objects: raise JobError("Duplicate source identity")
        objects.append(item)
    if not objects or len(objects) > 10000: raise JobError("Select between 1 and 10000 source objects")
    settings = _settings(engine_settings, source)
    args = _mapping(engine_args, ARG_KEYS, "engine argument")
    args["no-finalize"] = True
    for key in ("rerun", "no-resume", "repair", "no-finalize"):
        if key in args and type(args[key]) is not bool: raise JobError(key + " must be boolean")
    for key in ("tag", "done"):
        if key in args and type(args[key]) is not str: raise JobError(key + " must be a string")
    if "done" in args and not os.path.isabs(args["done"]): raise JobError("done-list path must be absolute")
    if "mem-gate" in args:
        try: gate = float(args["mem-gate"])
        except (TypeError, ValueError): raise JobError("mem-gate must be numeric")
        if not 0 < gate <= 1024: raise JobError("mem-gate must be in (0,1024]")
    policy = _resource_policy(_json_copy(resource_policy or {}))
    checked = _precheck(precheck_settings)
    dependencies = []
    for value in dependency_files or []:
        if not isinstance(value, (str, os.PathLike)): raise JobError("Dependency paths must be filesystem paths")
        path = Path(value).resolve()
        if not path.is_file(): raise JobError("Referenced dependency unavailable: " + str(path))
        if not any(item["path"] == str(path) for item in dependencies):
            dependencies.append({"path": str(path), "sha256": sha256_file(path)})
    root = Path(job_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    directory = root / job_id
    directory.mkdir(mode=0o700)
    snapshot = directory / source.name
    # Inputs exist only after the snapshot callback completed; failures preserve evidence in the reserved directory.
    capture = None
    if snapshot_writer is None: shutil.copyfile(source, snapshot)
    else: capture = snapshot_writer(str(snapshot))
    if not snapshot.is_file() or snapshot.stat().st_size == 0: raise JobError("Snapshot was not saved")
    spec = {"schema": SCHEMA, "job_id": job_id, "created_at": time.time(), "source_blend": str(source),
            "snapshot": snapshot.name, "snapshot_sha256": sha256_file(snapshot), "objects": objects,
            "engine_dir": str(Path(__file__).resolve().parent), "engine_sha256": package_fingerprint(),
            "engine_settings": settings, "engine_args": args, "precheck_settings": checked,
            "resource_policy": policy, "resume_from": str(resume_from or ""), "dependencies": dependencies,
            "dependency_policy": "Registered external files are hash-checked before and after work, not copied; resolver-discovered files remain engine-validated."}
    if isinstance(capture, dict) and capture.get("schema") == 1 and capture.get("status") == "CAPTURED":
        manifest_path = _inside(directory, "source_capture.json")
        recorded = _read_capture_manifest(manifest_path)
        if recorded != capture or recorded.get("snapshot_sha256") != spec["snapshot_sha256"] or Path(recorded.get("snapshot", "")).resolve() != snapshot:
            raise JobError("Source capture manifest does not describe the saved snapshot")
        spec["source_capture"] = {"manifest": manifest_path.name, "sha256": sha256_file(manifest_path)}
    elif isinstance(capture, dict) and "capture_id" in capture:
        raise JobError("Source capture callback did not produce a complete capture")
    atomic_json(directory / "job.json", spec)
    atomic_json(directory / "seal.json", {"schema": SCHEMA, "job_id": job_id,
                                         "job_sha256": sha256_file(directory / "job.json")})
    write_status(directory, "CREATED", reason="Private snapshot sealed")
    return str(directory)


def load_job(job_dir, *, verify_snapshot=False, verify_engine=False):
    spec_path = _inside(job_dir, "job.json")
    seal = read_json(_inside(job_dir, "seal.json"))
    if sha256_file(spec_path) != seal.get("job_sha256"): raise JobError("Immutable job input changed")
    spec = read_json(spec_path)
    keys = {"schema", "job_id", "created_at", "source_blend", "snapshot", "snapshot_sha256", "objects",
            "engine_dir", "engine_sha256", "engine_settings", "engine_args", "precheck_settings", "resource_policy", "resume_from",
            "dependencies", "dependency_policy"}
    if type(spec) is not dict or set(spec) not in (keys, keys | {"source_capture"}) or spec["schema"] != SCHEMA or spec["job_id"] != seal.get("job_id"):
        raise JobError("Invalid job schema or identity")
    if spec["job_id"] != Path(job_dir).resolve().name: raise JobError("Job directory identity mismatch")
    snapshot = _inside(job_dir, spec["snapshot"])
    if verify_snapshot:
        if sha256_file(snapshot) != spec["snapshot_sha256"]: raise JobError("Private snapshot changed")
        verify_dependencies(spec)
    if "source_capture" in spec:
        capture = spec["source_capture"]
        if not isinstance(capture, dict) or set(capture) != {"manifest", "sha256"}:
            raise JobError("Invalid source capture reference")
        capture_path = _inside(job_dir, capture["manifest"])
        if sha256_file(capture_path) != capture["sha256"]:
            raise JobError("Source capture manifest changed")
        captured = _read_capture_manifest(capture_path)
        if captured.get("snapshot_sha256") != spec["snapshot_sha256"] or Path(captured.get("snapshot", "")).resolve() != snapshot:
            raise JobError("Source capture snapshot identity mismatch")
        if verify_snapshot:
            verify_dependencies(captured)
    if verify_engine and (Path(spec["engine_dir"]).resolve() != Path(__file__).resolve().parent
                          or package_fingerprint() != spec["engine_sha256"]):
        raise JobError("Add-on code changed; create a fresh job rather than silently resuming with different code")
    _settings(spec["engine_settings"], spec["source_blend"])
    _precheck(spec["precheck_settings"])
    _resource_policy(spec["resource_policy"])
    return spec


def verify_dependencies(spec):
    for item in spec["dependencies"]:
        path = Path(item["path"])
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise JobError("Registered external dependency changed or disappeared: " + str(path))


def _read_capture_manifest(path):
    """Capture manifests have a separate bounded size; no bpy import in supervisor."""
    with open(path, "rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise JobError("Oversized source capture manifest")
    value = json.loads(data, object_pairs_hook=_no_duplicates)
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("status") != "CAPTURED" or not isinstance(value.get("dependencies"), list):
        raise JobError("Invalid source capture manifest")
    return value


def write_status(job_dir, state, **fields):
    job_dir = Path(job_dir).resolve()
    record = {"schema": SCHEMA, "job_id": job_dir.name, "state": state, "updated_at": time.time(), **fields}
    atomic_json(_inside(job_dir, "status.json"), record)
    return record


def _identity(pid):
    """Return a process birth token, None when gone, '?' when existence is uncertain."""
    if type(pid) is not int or pid <= 0: return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle: return None if ctypes.get_last_error() == 87 else "?"
        try:
            exit_code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(exit_code)): return "?"
            if exit_code.value != 259: return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(ctypes.byref(v) for v in times)): return "?"
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally: k.CloseHandle(handle)
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
        tail = stat[stat.rfind(")") + 2:].split()
        if tail[0] == "Z": return None
        return tail[19]
    except FileNotFoundError:
        try: os.kill(pid, 0)
        except ProcessLookupError: return None
        except PermissionError: return "?"
        return "?"
    except OSError: return "?"


def _running(process_info):
    now = _identity(process_info.get("pid"))
    if now is None: return False
    then = process_info.get("identity")
    return now == "?" or then in (None, "?") or now == then


def _lock_path(job_dir):
    # This serializes a job root, not all Blender installations or every GPU user on the host.
    return Path(job_dir).resolve().parent / ".import-glue-worker.lock"


def _acquire_lock(job_dir):
    path = _lock_path(job_dir)
    if path.exists():
        lock = read_json(path)
        process = lock.get("process", {})
        # A live launcher might still be between lock acquisition and spawning its child.
        if not process: process = lock.get("launcher", {})
        if not isinstance(process, dict) or type(process.get("pid")) is not int or not process.get("identity"):
            raise JobError("Cannot prove existing worker lock stale; inspect its metadata explicitly")
        if _running(process): raise JobError("Another worker or its launcher still owns this job-root lock")
        path.unlink()  # Proven stale identity only; no process is signalled.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"job_id": Path(job_dir).name, "launcher": {"pid": os.getpid(), "identity": _identity(os.getpid())}}, handle)


def _release_lock(job_dir):
    path = _lock_path(job_dir)
    if path.exists() and read_json(path).get("job_id") == Path(job_dir).name: path.unlink()


def worker_argv(job_dir, blender_binary):
    executable = Path(blender_binary).resolve()
    if not executable.is_file(): raise JobError("Blender executable does not exist")
    return [str(executable), "--background", "--factory-startup", "--disable-autoexec", "--threads", str(WORKER_THREADS),
            "--python-exit-code", "1", "--python", str(Path(__file__).with_name("background_worker.py").resolve()),
            "--", "--job", str(Path(job_dir).resolve())]


def launch(job_dir, blender_binary, *, admission=None):
    with _MUTEX:
        for owned in _OWNED.values():
            if owned["process"].poll() is None: raise JobError("Only one owned worker may run at a time")
        spec = load_job(job_dir, verify_snapshot=True, verify_engine=True)
        status = read_json(_inside(job_dir, "status.json"))
        if status.get("state") != "CREATED": raise JobError("Job already started; resume into a new private job")
        if admission is not None:
            decision = _json_copy(admission(spec))
            if type(decision) is not dict or type(decision.get("allowed")) is not bool:
                raise JobError("Admission callback must return an explicit allowed boolean")
            if not decision["allowed"]:
                return write_status(job_dir, "REFUSED", reason=str(decision.get("reason", "Resource admission refused")), admission=decision)
        argv = worker_argv(job_dir, blender_binary)
        _acquire_lock(job_dir)
        log = None
        process = None
        try:
            write_status(job_dir, "STARTING", reason="Starting isolated worker")
            log = open(_inside(job_dir, "worker.log"), "ab", buffering=0)
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                      "shell": False, "cwd": str(Path(job_dir).resolve())}
            if os.name == "nt": kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else: kwargs["start_new_session"] = True
            process = subprocess.Popen(argv, **kwargs)
            info = {"pid": process.pid, "identity": _identity(process.pid), "argv": argv,
                    "job_id": spec["job_id"], "started_at": time.time(), "worker_threads": WORKER_THREADS}
            atomic_json(_inside(job_dir, "process.json"), info)
            atomic_json(_lock_path(job_dir), {"job_id": spec["job_id"], "process": info})
            _OWNED[str(Path(job_dir).resolve())] = {"process": process, "log": log}
        except BaseException as exc:
            # Failure before durable PID metadata must not leave an unsupervised newly-created child.
            # This is launch rollback of this exact Popen object, never a process found by name/PID.
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            _OWNED.pop(str(Path(job_dir).resolve()), None)
            if log is not None: log.close()
            _release_lock(job_dir)
            write_status(job_dir, "FAILED", reason="Worker launch failed: " + str(exc)[:2000])
            raise
        return poll(job_dir)


def poll(job_dir):
    key = str(Path(job_dir).resolve())
    spec = load_job(key)
    status = read_json(_inside(key, "status.json"))
    if status.get("job_id") != spec["job_id"] or status.get("schema") != SCHEMA:
        raise JobError("Status identity/schema mismatch")
    owned = _OWNED.get(key)
    exit_code = owned["process"].poll() if owned else None
    process_path = _inside(key, "process.json")
    process_info = read_json(process_path) if process_path.exists() else {}
    live = (exit_code is None) if owned else (_running(process_info) if process_info else False)
    if owned and exit_code is not None:
        owned["log"].close()
        del _OWNED[key]
        if status["state"] not in TERMINAL:
            status = write_status(key, "CRASHED", reason="Worker exited without a terminal result", exit_code=exit_code)
        elif status["state"] == "SUCCEEDED" and exit_code != 0:
            status = write_status(key, "CRASHED", reason="Worker failed after reporting success", exit_code=exit_code)
        _release_lock(key)
    elif not owned and process_info and not live:
        if status["state"] not in TERMINAL:
            status = write_status(key, "CRASHED", reason="Recorded worker no longer exists; exit code unavailable")
        _release_lock(key)
    if _inside(key, "cancel.json").exists() and status["state"] not in TERMINAL:
        status = {**status, "state": "CANCEL_REQUESTED", "reason": "Cancellation requested; a native bake may finish before the next safe boundary"}
    return {**status, "process_alive": live, "owned_process": key in _OWNED,
            "exit_code": exit_code if exit_code is not None else status.get("exit_code"), "job_dir": key}


def cancel(job_dir):
    spec = load_job(job_dir)
    current = poll(job_dir)
    if current["state"] in TERMINAL: return current
    atomic_json(_inside(job_dir, "cancel.json"), {"schema": SCHEMA, "job_id": spec["job_id"], "requested_at": time.time()})
    if current["state"] == "CREATED": write_status(job_dir, "CANCELLED", reason="Cancelled before launch")
    return poll(job_dir)


def terminate_owned(job_dir, *, force=False):
    """Explicit escalation only. Never look up an arbitrary/recovered PID to kill it."""
    key = str(Path(job_dir).resolve())
    with _MUTEX:
        owned = _OWNED.get(key)
        if owned is None or owned["process"].poll() is not None:
            raise JobError("Escalation requires a live worker Popen handle owned by this session")
        cancel(key)
        if force: owned["process"].kill()
        else: owned["process"].terminate()
        write_status(key, "TERMINATED", reason="Explicit forced kill" if force else "Explicit process termination",
                     escalation="kill" if force else "terminate")
        return poll(key)


def read_log_tail(job_dir, max_bytes=16384):
    count = max(0, min(int(max_bytes), 65536))
    with open(_inside(job_dir, "worker.log"), "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - count))
        return handle.read(count).decode("utf-8", "replace")


def load_result(job_dir, *, allow_partial=False):
    status = poll(job_dir)
    if allow_partial and status["state"] in {"FAILED", "CANCELLED"} and not status["process_alive"]:
        result = read_json(_inside(job_dir, "partial_result.json"))
        spec = load_job(job_dir)
        if (result.get("schema") != SCHEMA or result.get("job_id") != Path(job_dir).name
                or result.get("state") != "PARTIAL" or result.get("target") != "BLENDER_NATIVE"
                or result.get("snapshot_sha256") != spec["snapshot_sha256"]
                or result.get("engine_sha256") != spec["engine_sha256"]):
            raise JobError("Partial result identity/status mismatch")
        names = result.get("output_objects")
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
            raise JobError("Partial result has no validated object list")
        path = Path(result["result_path"])
        if path.is_symlink() or not path.resolve().is_relative_to(Path(spec["engine_settings"]["OUTPUT_ROOT"]).resolve()):
            raise JobError("Partial result is outside its output root")
        if sha256_file(path) != result.get("result_sha256"):
            raise JobError("Partial native library checksum mismatch")
        if sha256_file(result["manifest"]) != result.get("manifest_sha256"):
            raise JobError("Partial result manifest checksum mismatch")
        verify_dependencies({"dependencies": result.get("output_dependencies", [])})
        return result
    if status["state"] != "SUCCEEDED" or status["process_alive"]:
        raise JobError("Only an exited, successful worker result can be loaded")
    result = read_json(_inside(job_dir, "result.json"))
    if result.get("schema") != SCHEMA or result.get("job_id") != Path(job_dir).name or result.get("state") != "SUCCEEDED":
        raise JobError("Result identity/status mismatch")
    if type(result.get("output_objects")) is not list or not result["output_objects"] or any(type(name) is not str for name in result["output_objects"]):
        raise JobError("Result output object list is missing or invalid")
    path = _inside(job_dir, result["result_blend"])
    if sha256_file(path) != result.get("result_sha256"): raise JobError("Result blend checksum mismatch")
    verify_dependencies({"dependencies": result.get("output_dependencies", [])})
    if result.get("manifest_sha256"):
        if sha256_file(result["manifest"]) != result["manifest_sha256"]:
            raise JobError("Result manifest checksum mismatch")
    return {**result, "result_path": str(path)}


def resume_job(previous_dir, job_root=None):
    """Resume the same snapshot/settings in a new job; only no-resume/rerun execution flags are removed.

    Engine validates durable per-object checkpoints. An explicitly disabled
    checkpoint policy is never silently enabled: the user must create a fresh run.
    """
    previous = load_job(previous_dir, verify_snapshot=True, verify_engine=True)
    status = poll(previous_dir)
    if status["process_alive"] or status["state"] not in TERMINAL:
        raise JobError("Cannot resume an active or unstarted job")
    if job_root is not None and Path(job_root).resolve() != Path(previous_dir).resolve().parent:
        raise JobError("Resume must use the original job root to preserve relative dependency paths")
    if previous["engine_settings"].get("DONE_LIST") is False or previous["engine_settings"].get("DURABLE_CHECKPOINTS") is False:
        raise JobError("Previous run explicitly disabled durable checkpoints or the done-list; create a fresh run")
    resume_args = dict(previous["engine_args"])
    resume_args.pop("no-resume", None)
    resume_args.pop("rerun", None)
    # The original source file can move after snapshotting. The immutable snapshot itself is the resume source.
    source = _inside(previous_dir, previous["snapshot"])
    snapshot_writer = None
    if "source_capture" in previous:
        old_capture = _read_capture_manifest(_inside(previous_dir, previous["source_capture"]["manifest"]))
        def snapshot_writer(path):
            shutil.copyfile(source, path)
            captured = {**old_capture, "snapshot": str(Path(path).resolve()), "fresh_open_verified": False,
                        "resumed_from": str(Path(previous_dir).resolve())}
            # Retain the exact packed snapshot bytes; no dependency path inside
            # the snapshot changes when resuming within this job root.
            manifest_path = Path(path).parent / "source_capture.json"
            with manifest_path.open("x", encoding="utf-8") as stream:
                json.dump(captured, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return captured
    new_dir = create_job(job_root or Path(previous_dir).parent, source, previous["objects"], previous["engine_settings"],
                         snapshot_writer=snapshot_writer,
                         engine_args=resume_args, resource_policy=previous["resource_policy"],
                         precheck_settings=previous["precheck_settings"], resume_from=str(Path(previous_dir).resolve()),
                         dependency_files=[item["path"] for item in previous["dependencies"]])
    return new_dir
