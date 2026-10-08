"""Content-validated checkpoints for native fields, objects and complete targets.

Only a completed record is reusable. Artifacts are never inferred from file names,
mtimes, or a previous success flag. Records are published by atomic rename after
all bytes have been hashed; interruption leaves only an ignored staging directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

SCHEMA = 1
MAX_RECORD_BYTES = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Checkpoint record exceeds 16 MiB")
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:12])
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Cache:
    def __init__(self, output_root, identity, *, read=True, write=True):
        self.output_root = Path(output_root).resolve()
        self.identity = digest(identity)
        # Full digests remain in every verified record; shorter locator names
        # leave room for ordinary Windows output roots without MAX_PATH overflow.
        self.root = self.output_root / ".ig-targets" / self.identity[:32]
        self.read_enabled, self.write_enabled = bool(read), bool(write)
        self.events = []

    def _path(self, path):
        path = Path(path)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.output_root) or not resolved.is_file():
            raise ValueError("Checkpoint artifact missing or outside output root")
        # Reject links anywhere between output root and payload, including parent links.
        current = path.absolute()
        while current != self.output_root and current.is_relative_to(self.output_root):
            if current.is_symlink():
                raise ValueError("Checkpoint artifact must not use symlinks")
            current = current.parent
        return resolved

    def _entry(self, kind, key):
        if kind not in {"target", "object", "field"}:
            raise ValueError("Unknown checkpoint granularity")
        return self.root / kind / digest(key)[:32]

    def save(self, kind, key, result, paths):
        if not self.write_enabled:
            return None
        directory = self._entry(kind, key)
        directory.mkdir(parents=True, exist_ok=True)
        inventory = []
        for path in sorted({str(self._path(p)) for p in paths}):
            item = Path(path)
            inventory.append({"path": str(item.relative_to(self.output_root)),
                              "bytes": item.stat().st_size, "sha256": sha256(item)})
        if not inventory:
            raise ValueError("Checkpoint requires at least one artifact")
        record = {"schema": SCHEMA, "state": "COMPLETE", "identity": self.identity,
                  "kind": kind, "key": digest(key), "result": result, "files": inventory}
        # Atomic replacement is limited to our own index. Existing output files are never replaced.
        atomic_json(directory / "record.json", record)
        self.events.append({"kind": kind, "event": "saved", "key": digest(key)})
        return record

    def load(self, kind, key):
        if not self.read_enabled:
            return None
        path = self._entry(kind, key) / "record.json"
        if not path.is_file():
            return None
        try:
            if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
                raise ValueError("Invalid checkpoint record")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("schema"), record.get("state"), record.get("identity"),
                    record.get("kind"), record.get("key")) != (SCHEMA, "COMPLETE", self.identity, kind, digest(key)):
                raise ValueError("Checkpoint identity changed")
            if not record.get("files"):
                raise ValueError("Empty artifact inventory")
            for item in record["files"]:
                value = Path(item["path"])
                if value.is_absolute() or ".." in value.parts:
                    raise ValueError("Invalid checkpoint artifact path")
                artifact = self._path(self.output_root / value)
                if artifact.stat().st_size != item["bytes"] or sha256(artifact) != item["sha256"]:
                    raise ValueError("Checkpoint artifact bytes changed: " + str(value))
            self.events.append({"kind": kind, "event": "verified", "key": digest(key)})
            return record["result"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.events.append({"kind": kind, "event": "invalidated", "reason": str(exc)[:500]})
            return None

    def save_field(self, object_key, field_key, path, metadata):
        if not self.write_enabled:
            return None
        directory = self._entry("field", [object_key, field_key])
        directory.mkdir(parents=True, exist_ok=True)
        # Copy before publication: native rollback is allowed to remove its incomplete object folder.
        payload = directory / ("field-" + uuid.uuid4().hex + Path(path).suffix)
        with Path(path).open("rb") as source, payload.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        return self.save("field", [object_key, field_key],
                         {"path": str(payload), "metadata": metadata}, [payload])

    def load_field(self, object_key, field_key):
        return self.load("field", [object_key, field_key])


def inventory(folder):
    """Only caller-owned delivery folders; do not scan the whole user's output root."""
    return [path for path in Path(folder).rglob("*") if path.is_file()
            and not path.name.endswith(".blend1") and ".tmp-" not in path.name]
