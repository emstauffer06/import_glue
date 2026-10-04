"""Verified external movie/sequence staging for finalized blend copies.

Staging NEVER changes live Image paths. A successful plan exposes ``path_updates``
as [{"image": Image, "filepath": "//media/..."}], and ``original_paths`` as the
same record shape with the original filepath. The caller applies updates, saves
with ``copy=True, relative_remap=False``, and restores in a finally block.
``destination`` MUST be a direct child of the TARGET blend's parent directory:
relative paths deliberately refer to that target, not the currently open blend.
Any errors invalidate the whole plan (path_updates is empty); verified copies
already staged may remain for a later run. No destination file is overwritten.

Movies are checked for Blender container readability, not decoded at every frame.
PNG/JPEG/TGA numbered sequences are checked frame by frame; unsupported sequence
formats fail explicitly because their decoded memory cannot be bounded here.
Finite configurable limits bound files, bytes, image pixels and streamed work.
Blender decoder calls themselves are synchronous and not interruptible by Python.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import struct
import time

import bpy

_CHUNK = 1024 * 1024
_SEQUENCE = re.compile(r"^(.*?)(\d+)(\.[^.]+)$")


class _Budget:
    def __init__(self, max_files, max_bytes, max_seconds, max_pixels):
        if any(not math.isfinite(limit) or limit <= 0
               for limit in (max_files, max_bytes, max_seconds, max_pixels)):
            raise ValueError("Media staging limits must be positive and finite")
        self.max_files, self.max_bytes, self.max_pixels = max_files, max_bytes, max_pixels
        self.max_seconds = max_seconds
        self.deadline = time.monotonic() + max_seconds
        self.sources = set()
        self.bytes = 0

    def check(self):
        if time.monotonic() > self.deadline:
            raise ValueError("Media staging time limit exceeded (%s seconds)" % self.max_seconds)

    def source(self, path):
        self.check()
        if path not in self.sources:
            size = path.stat().st_size
            if size <= 0:
                raise ValueError("Empty media file: %s" % path)
            self.sources.add(path)
            self.bytes += size
            if len(self.sources) > self.max_files:
                raise ValueError("Media staging file resource limit exceeded: %d > %s" %
                                 (len(self.sources), self.max_files))
            if self.bytes > self.max_bytes:
                raise ValueError("Media staging byte resource limit exceeded: %d > %s" %
                                 (self.bytes, self.max_bytes))


def _hash(path, budget):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK):
            budget.check()
            hasher.update(block)
    return hasher.hexdigest()


def _sequence_dimensions(path):
    """Read dimensions before Blender can allocate a full decoded frame."""
    with path.open("rb") as stream:
        if path.suffix.lower() == ".png":
            header = stream.read(24)
            if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                raise ValueError("Invalid PNG sequence frame: %s" % path)
            return struct.unpack(">II", header[16:24])
        if path.suffix.lower() in {".tga", ".targa"}:
            header = stream.read(18)
            if (len(header) != 18 or header[1] not in {0, 1} or
                    header[2] not in {1, 2, 3, 9, 10, 11} or
                    header[16] not in {8, 15, 16, 24, 32}):
                raise ValueError("Invalid TGA sequence frame header: %s" % path)
            return struct.unpack("<HH", header[12:16])
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            if stream.read(2) != b"\xff\xd8":
                raise ValueError("Invalid JPEG sequence frame: %s" % path)
            # JPEG headers have bounded 16-bit segment lengths; do not read pixels.
            for _ in range(4096):
                if stream.read(1) != b"\xff":
                    break
                marker = stream.read(1)
                for _padding in range(64):
                    if marker != b"\xff":
                        break
                    marker = stream.read(1)
                else:
                    raise ValueError("JPEG marker padding limit exceeded: %s" % path)
                if not marker or marker in {b"\xd9", b"\xda"}:
                    break
                length = stream.read(2)
                if len(length) != 2:
                    break
                length = struct.unpack(">H", length)[0]
                if length < 2:
                    break
                if marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    size = stream.read(5)
                    if len(size) == 5:
                        height, width = struct.unpack(">HH", size[1:])
                        return width, height
                    break
                stream.seek(length - 2, 1)
            raise ValueError("Unreadable JPEG dimensions: %s" % path)
    raise ValueError("Unsupported sequence frame format (supported: PNG/JPEG/TGA): %s" % path)


def _readable(path, source, budget):
    budget.check()
    if source == "SEQUENCE":
        width, height = _sequence_dimensions(path)
        if not width or not height or width * height > budget.max_pixels:
            raise ValueError("Sequence frame pixel resource limit exceeded: %dx%d > %s pixels: %s" %
                             (width, height, budget.max_pixels, path))
    # A new Image avoids stale cached source pixels and is removed even on failure.
    image = None
    try:
        image = bpy.data.images.load(str(path), check_existing=False)
        width, height = image.size[:]
        if not width or not height or not image.has_data:
            raise ValueError("Blender cannot decode media: %s" % path)
        if width * height > budget.max_pixels:
            raise ValueError("Media pixel resource limit exceeded: %dx%d > %s pixels: %s" %
                             (width, height, budget.max_pixels, path))
        if source == "MOVIE" and image.source != "MOVIE":
            raise ValueError("Source is not a readable movie container: %s" % path)
    finally:
        if image is not None:
            bpy.data.images.remove(image)
    budget.check()


def _image_users(image, budget):
    """Discover authored durations on direct and nested node/texture owners."""
    owners = bpy.data.user_map(subset={image}).get(image, set())
    seen = set()

    def visit(owner):
        budget.check()
        pointer = owner.as_pointer()
        if pointer in seen:
            return
        seen.add(pointer)
        if getattr(owner, "image", None) == image and hasattr(owner, "image_user"):
            yield owner.image_user
        tree = getattr(owner, "node_tree", None)
        if tree is not None:
            yield from visit(tree)
        for node in getattr(owner, "nodes", ()):
            budget.check()
            if getattr(node, "image", None) == image and hasattr(node, "image_user"):
                yield node.image_user
            tree = getattr(node, "node_tree", None)
            if tree is not None:
                yield from visit(tree)
        for background in getattr(owner, "background_images", ()):
            if background.image == image:
                yield background.image_user
        for area in getattr(owner, "areas", ()):
            for space in area.spaces:
                if getattr(space, "image", None) == image and hasattr(space, "image_user"):
                    yield space.image_user

    for owner in owners:
        yield from visit(owner)


def _sources(image, budget):
    if image.packed_file or image.packed_files:
        raise ValueError("Packed movie/sequence cannot be delivered as external media: %s" % image.name)
    if not image.filepath:
        raise ValueError("Missing media filepath: %s" % image.name)
    # Sequence aliases carry authored family names. Resolving them would rename
    # required frames and can move the first frame's discovery to another folder.
    path = Path(os.path.abspath(bpy.path.abspath(image.filepath, library=image.library)))
    if not path.is_file():
        raise ValueError("Missing media file: %s" % path)
    if image.source == "MOVIE":
        return [path.resolve()], path.resolve()
    match = _SEQUENCE.match(path.name)
    if not match:
        raise ValueError("Unsupported nonnumbered sequence filepath: %s" % path)
    prefix, digits, extension = match.groups()
    pattern = re.compile(r"^%s(\d{%d})%s$" % (re.escape(prefix), len(digits), re.escape(extension)))
    frames = {}
    # scandir streams a directory instead of allocating an unbounded listing.
    with os.scandir(path.parent) as entries:
        for entry in entries:
            budget.check()
            candidate = pattern.match(entry.name)
            if candidate:
                if len(frames) >= budget.max_files:
                    raise ValueError("Sequence frame resource limit exceeded (%s files): %s" %
                                     (budget.max_files, path))
                if not entry.is_file():
                    raise ValueError("Missing sequence frame file: %s" % entry.path)
                frames[int(candidate.group(1))] = Path(entry.path)
    start, end = min(frames), max(frames)
    if end - start + 1 != len(frames):
        raise ValueError("Missing frame inside numbered sequence: %s" % path)
    first = int(digits)
    ranges = [(first, max(1, image.frame_duration))]
    ranges.extend((first + user.frame_offset, max(1, user.frame_duration))
                  for user in _image_users(image, budget))
    for required_start, duration in ranges:
        if required_start < start or required_start + duration - 1 > end:
            raise ValueError("Missing declared sequence frame range %d..%d: %s" %
                             (required_start, required_start + duration - 1, path))
    return [frames[number] for number in sorted(frames)], path


def _verified_copy(source, target, digest, budget):
    """Exclusive create or verify existing content; never truncate existing paths."""
    if target.is_symlink():
        return None
    if target.exists():
        if target.is_file() and target.stat().st_size == source.stat().st_size and _hash(target, budget) == digest:
            return True
        return None
    try:
        output = target.open("xb")
    except FileExistsError:
        return _verified_copy(source, target, digest, budget)
    try:
        copied = hashlib.sha256()
        with output, source.open("rb") as incoming:
            while block := incoming.read(_CHUNK):
                budget.check()
                output.write(block)
                copied.update(block)
        if copied.hexdigest() != digest or _hash(target, budget) != digest:
            raise ValueError("Media changed during staging or copy verification failed: %s" % source)
        return False
    except Exception:
        target.unlink(missing_ok=True)  # Only our exclusively-created file.
        raise


def stage_external_media(images, destination, *, max_files=10000,
                         max_bytes=32 * 1024 ** 3, max_seconds=120,
                         max_pixels=64 * 1024 ** 2):
    """Return files, nonmutating path_updates, original_paths and blocking errors.

    ``files`` entries contain source/destination absolute paths, sha256, size and
    reused. Only MOVIE/SEQUENCE inputs are staged. FILE/TILED/GENERATED images are
    outside this API and skipped. Limits are configurable keyword arguments.
    """
    plan = {"files": [], "path_updates": [], "original_paths": [], "errors": []}
    destination = Path(destination).absolute()
    try:
        budget = _Budget(max_files, max_bytes, max_seconds, max_pixels)
        if destination.is_symlink() or destination.resolve() != destination:
            raise ValueError("Media destination must not traverse symlinks: %s" % destination)
        if not destination.name or destination.name in {".", ".."}:
            raise ValueError("Media destination must be a named directory")
        seen_images, recorded_files, pending = set(), set(), []
        assets = []
        for image in images:
            budget.check()
            if image.source not in {"MOVIE", "SEQUENCE"} or image.as_pointer() in seen_images:
                continue
            seen_images.add(image.as_pointer())
            plan["original_paths"].append({"image": image, "filepath": image.filepath})
            paths, first = _sources(image, budget)
            if any(path.parent.resolve() == destination.resolve() for path in paths):
                raise ValueError("Media destination is a source directory: %s" % destination)
            verified = []
            for path in paths:
                budget.source(path)
                _readable(path, image.source, budget)
                verified.append((path, _hash(path, budget)))
            assets.append((image, first, verified))
        destination.mkdir(parents=True, exist_ok=True)
        movie_targets = {}
        for image, first, verified in assets:
            if image.source == "MOVIE":
                path, identity = verified[0]
                if identity in movie_targets:
                    relative = movie_targets[identity].relative_to(destination.parent).as_posix()
                    pending.append({"image": image, "filepath": "//" + relative})
                    continue
                base = "movie_" + identity
            else:
                family = hashlib.sha256()
                for path, digest in verified:
                    family.update(path.name.encode("utf-8"))
                    family.update(b"\0" + bytes.fromhex(digest))
                base = "sequence_" + family.hexdigest()
            for collision in range(1000):
                budget.check()
                name = base + ("_%d" % collision if collision else "")
                folder = destination / name if image.source == "SEQUENCE" else destination
                if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                    continue
                targets = [folder / (path.name if image.source == "SEQUENCE" else name + path.suffix)
                           for path, _ in verified]
                if any(target.is_symlink() or (target.exists() and (not target.is_file() or
                       target.stat().st_size != path.stat().st_size or _hash(target, budget) != digest))
                       for target, (path, digest) in zip(targets, verified)):
                    continue
                folder.mkdir(exist_ok=True)
                records = []
                collision_found = False
                for target, (path, digest) in zip(targets, verified):
                    reused = _verified_copy(path, target, digest, budget)
                    if reused is None:
                        collision_found = True
                        break
                    records.append({"source": str(path), "destination": str(target),
                                    "sha256": digest, "size": path.stat().st_size, "reused": reused})
                if collision_found:
                    continue
                for record in records:
                    if record["destination"] not in recorded_files:
                        plan["files"].append(record)
                        recorded_files.add(record["destination"])
                first_target = targets[[path for path, _ in verified].index(first)]
                if image.source == "MOVIE":
                    movie_targets[identity] = first_target
                relative = first_target.relative_to(destination.parent).as_posix()
                pending.append({"image": image, "filepath": "//" + relative})
                break
            else:
                raise ValueError("Too many conflicting delivery filenames: %s" % base)
        plan["path_updates"] = pending
    except Exception as exc:
        plan["path_updates"] = []
        plan["errors"].append("%s: %s" % (type(exc).__name__, exc))
    return plan


def restore_media_paths(plan):
    """Idempotently restore all originals, including partially-applied updates."""
    failures = []
    for record in plan.get("original_paths", ()):
        try:
            record["image"].filepath = record["filepath"]
        except Exception as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError("Failed to restore media paths: " + "; ".join(failures))
