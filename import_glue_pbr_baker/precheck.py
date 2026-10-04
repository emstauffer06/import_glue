"""Texture precheck shared by the Blender UI add-on and headless runner.

The original ``bake_precheck.py`` was a process-oriented script: it repaired
paths immediately and raised ``SystemExit`` on failure.  This version keeps
that behavior for a no-argument legacy call while also exposing a structured,
non-exiting API suitable for a persistent Blender operator.
"""
from __future__ import annotations

import json
import ntpath
import os
import shutil
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import bpy

from . import image_validation, shader_graph


DEFAULT_POOL = "/root/work/_shared/downloads_sol_textures"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _is_foreign_absolute(path: str) -> bool:
    """Return True for Windows absolute paths while running on another OS."""
    return (
        not path.startswith("//")
        and ntpath.isabs(path)
        and not os.path.isabs(path)
    )


def _basename(path: str) -> str:
    """Extract a basename from either native or Windows-authored paths."""
    return ntpath.basename(path)


def _is_within(path: str, root: str) -> bool:
    if _is_foreign_absolute(path) != _is_foreign_absolute(root):
        return False
    try:
        return os.path.commonpath((_norm(path), _norm(root))) == _norm(root)
    except (OSError, ValueError):
        return False


def _index(root: str, index: Dict[str, List[str]], followlinks: bool) -> None:
    """Index files deterministically, pruning symlink cycles and file links."""
    if not root or not os.path.isdir(root):
        return
    visited: Set[str] = set()
    for directory, child_dirs, files in os.walk(root, followlinks=followlinks):
        real_directory = _norm(os.path.realpath(directory))
        if real_directory in visited:
            child_dirs[:] = []
            continue
        visited.add(real_directory)
        child_dirs[:] = sorted(child_dirs)
        if not followlinks:
            child_dirs[:] = [
                name for name in child_dirs
                if not os.path.islink(os.path.join(directory, name))
            ]
        for filename in sorted(files):
            path = os.path.join(directory, filename)
            if os.path.islink(path):
                continue
            index.setdefault(filename.lower(), []).append(path)


def build_index(scene_dir: str, pool: str = "") -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    _index(scene_dir, index, False)  # never write/read through scene directory links
    if pool and _norm(pool) != _norm(scene_dir):
        _index(pool, index, True)  # configured pools commonly contain linked folders
    return index


def material_images(objects: Optional[Iterable[Any]] = None) -> Set[int]:
    """Images required by polygon-used materials' active Cycles outputs."""
    result: Set[int] = set()
    source = bpy.data.objects if objects is None else objects
    for material in shader_graph.used_materials(source):
        result.update(shader_graph.material_dependencies(material)["images"])
    return result


def file_looks_valid(path: str) -> Tuple[bool, str]:
    """Validate a texture file's container structurally.

    v1.1.1: this was a header/trailer sniff -- a 64-byte size floor, an "IEND appears
    somewhere in the last 4 KiB" test for PNG, an EOI test for JPEG, and an
    unconditional pass for every other container.  That accepts three files Blender
    cannot decode: a signature+IHDR stub with no image data at all, a stream truncated
    mid-IDAT whose remaining tail happens to contain the bytes "IEND", and any
    corrupt .tga/.bmp/.tif.  Each of those reaches the bake as a missing texture and
    surfaces as a magenta or black map instead of an honest precheck refusal.

    inspect_raster() walks the real chunk/marker stream, verifies CRCs and declared
    dimensions, and falls back to Blender's own decoder for containers it does not
    parse itself.  Signature kept as (ok, reason) so the three call sites are unchanged.
    """
    try:
        valid, reason, _info = image_validation.inspect_raster(path)
    except Exception as exc:  # a validator must never itself break the precheck
        return False, "unreadable: %s" % exc
    return bool(valid), ("" if valid else str(reason))


def _image_paths(image: Any) -> List[Tuple[str, str, Optional[int]]]:
    """Return true-absolute paths, expanding every declared UDIM tile."""
    raw = image.filepath
    if _is_foreign_absolute(raw):
        # Keep a dead Windows drive/UNC reference absolute on Linux.  Joining
        # it below the blend directory would create paths such as scene/E:/...
        # instead of allowing basename lookup and an in-memory repoint.
        absolute = ntpath.normpath(raw)
    else:
        absolute = bpy.path.abspath(raw)
        if not os.path.isabs(absolute):
            absolute = os.path.join(
                os.path.dirname(os.path.abspath(bpy.data.filepath)), absolute
            )
        absolute = os.path.abspath(absolute)
    if "<UDIM>" not in absolute:
        return [(raw, os.path.normpath(absolute), None)]
    numbers = [tile.number for tile in image.tiles] or [1001]
    return [
        (raw.replace("<UDIM>", str(number)),
         os.path.normpath(absolute.replace("<UDIM>", str(number))), number)
        for number in numbers
    ]


def _path_parts(path: str) -> Tuple[str, ...]:
    normalized = os.path.normcase(os.path.normpath(path)).replace("\\", "/")
    return tuple(part for part in normalized.split("/") if part)


def _udim_template(candidate: str, tile_number: Optional[int]) -> str:
    if tile_number is None:
        return candidate
    before, marker, after = candidate.rpartition(str(tile_number))
    if not marker:
        raise ValueError("UDIM candidate does not contain tile number %s" % tile_number)
    return before + "<UDIM>" + after


def _pick_candidate(
    candidates: Sequence[str], expected: str, raw: str, scene_dir: str
) -> Tuple[Optional[str], List[str]]:
    existing = sorted({_norm(path): path for path in candidates if os.path.isfile(path)}.values())
    if not existing:
        return None, []
    relative_suffix = raw[2:] if raw.startswith("//") else _basename(expected)
    suffix_parts = _path_parts(relative_suffix)
    suffix_hits = [
        path for path in existing
        if len(_path_parts(path)) >= len(suffix_parts)
        and _path_parts(path)[-len(suffix_parts):] == suffix_parts
    ]
    if len(suffix_hits) == 1:
        return suffix_hits[0], []
    local_hits = [path for path in existing if _is_within(path, scene_dir)]
    if len(local_hits) == 1:
        return local_hits[0], []
    if len(existing) == 1:
        return existing[0], []
    return None, existing


def _place_file(source: str, destination: str, mode: str) -> str:
    """Materialize a missing relative reference and verify the result."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.lexists(destination):
        if os.path.islink(destination) and not os.path.exists(destination):
            os.unlink(destination)
        elif os.path.isfile(destination):
            return "EXISTS"
        else:
            raise OSError("destination exists and is not a regular file: %s" % destination)

    attempts = {
        "AUTO": ("HARDLINK", "SYMLINK", "COPY"),
        "HARDLINK": ("HARDLINK",),
        "SYMLINK": ("SYMLINK",),
        "COPY": ("COPY",),
    }.get(mode, ("HARDLINK", "SYMLINK", "COPY"))
    errors = []
    for method in attempts:
        try:
            if method == "HARDLINK":
                os.link(source, destination)
            elif method == "SYMLINK":
                os.symlink(source, destination)
            else:
                shutil.copy2(source, destination)
            if not os.path.isfile(destination):
                raise OSError("created destination did not verify")
            return method
        except OSError as exc:
            errors.append("%s: %s" % (method, exc))
            if os.path.lexists(destination):
                try:
                    os.unlink(destination)
                except OSError:
                    pass
    raise OSError("; ".join(errors))


def _write_marker(directory: str, name: str, entries: Sequence[str]) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    if entries:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(entries) + "\n")
    elif os.path.exists(path):
        os.remove(path)
    return path


def _default_marker_dir(scene_dir: str) -> str:
    external = os.environ.get("BAKE_OUT")
    if external:
        return os.path.join(external, os.path.basename(scene_dir))
    return scene_dir


def run(
    *,
    objects: Optional[Iterable[Any]] = None,
    pool: Optional[str] = None,
    marker_dir: Optional[str] = None,
    repair: bool = False,
    repair_unused: bool = False,
    repair_mode: str = "AUTO",
    repair_into_staging: bool = True,
    allow_missing: Optional[bool] = None,
    allow_corrupt: Optional[bool] = None,
    strict_absolute: bool = True,
    raise_on_failure: bool = True,
) -> Dict[str, Any]:
    """Scan material textures, optionally repair paths, and return a report.

    v1.2.1 -- ``repair`` now defaults to FALSE and ``repair_into_staging`` to
    TRUE, and ``strict_absolute`` defaults to TRUE. ``repair`` is the only mode
    that WRITES INTO THE SOURCE SCENE DIRECTORY; it must be asked for.
    ``repair_into_staging`` resolves a missing reference by REPOINTING the
    image datablock at the candidate it found, which touches no file on disk
    and is not persisted unless the caller saves the .blend. Previously the GUI
    defaulted to source-writing repair and forwarded it from the plain "Check
    Textures" button, so merely checking a scene created files beside the
    user's .blend (measured 2026-09-17).

    ``raise_on_failure=True`` preserves the original headless exit codes (11
    for missing references, 12 for corrupt images).  UI callers pass False and
    translate ``report['code']`` into an operator result instead.
    """
    blend_path = bpy.data.filepath
    if not blend_path:
        raise RuntimeError("Save the .blend before running the texture precheck.")
    scene_dir = os.path.dirname(os.path.abspath(blend_path))
    if pool is None:
        pool_value = os.environ.get("BAKE_POOL", DEFAULT_POOL)
    else:
        pool_value = pool
    pool = os.path.abspath(pool_value) if pool_value else ""
    marker_dir = os.path.abspath(marker_dir or _default_marker_dir(scene_dir))
    legacy_allow = os.environ.get("BAKE_ALLOW_MISSING_TEX") == "1"
    if allow_missing is None:
        allow_missing = legacy_allow
    if allow_corrupt is None:
        allow_corrupt = legacy_allow or os.environ.get("BAKE_ALLOW_CORRUPT_TEX") == "1"

    scoped_objects = None if objects is None else list(objects)
    needed = material_images(scoped_objects)
    index = build_index(scene_dir, pool)
    report: Dict[str, Any] = {
        "ok": False,
        "code": 0,
        "scene": blend_path,
        "scene_dir": scene_dir,
        "pool": pool if os.path.isdir(pool) else "",
        "marker_dir": marker_dir,
        "repair": bool(repair),
        "repair_into_staging": bool(repair_into_staging),
        "repair_unused": bool(repair_unused),
        "repair_mode": repair_mode,
        "material_used": len(needed),
        "image_files": 0,
        "repaired": 0,
        "repointed_originals": [],
        "repaired_by": {},
        "repointed": 0,
        "missing_relative": [],
        "missing_absolute": [],
        "corrupt": [],
        "ambiguous": [],
        "ignored_non_material": [],
        "unchecked_library": [],
        "warnings": [],
    }

    for image in bpy.data.images:
        pointer = int(image.as_pointer())
        is_needed = pointer in needed
        if image.library:
            if is_needed:
                report["unchecked_library"].append(image.name)
            continue
        if image.packed_file:
            if is_needed:
                try:
                    if image.size[0] == 0 or image.size[1] == 0:
                        report["corrupt"].append(
                            "%s\t<packed %d bytes>\tpacked image does not decode"
                            % (image.name, image.packed_file.size)
                        )
                except Exception as exc:
                    report["corrupt"].append("%s\t<packed>\t%s" % (image.name, exc))
            continue
        if image.source in ("GENERATED", "VIEWER") or not image.filepath:
            continue

        image_paths = _image_paths(image)
        pending_repoint: Optional[str] = None
        pending_entries: List[Tuple[bool, str]] = []
        repoint_blocked = False

        def record_unresolved(relative: bool, entry: str) -> None:
            if is_needed:
                target = (
                    report["missing_relative"]
                    if relative else report["missing_absolute"]
                )
                if entry not in target:
                    target.append(entry)
            elif entry not in report["ignored_non_material"]:
                report["ignored_non_material"].append(entry)

        for raw, expected, tile_number in image_paths:
            report["image_files"] += 1
            if os.path.isfile(expected):
                if is_needed:
                    valid, why = file_looks_valid(expected)
                    if not valid:
                        report["corrupt"].append("%s\t%s\t%s" % (image.name, expected, why))
                        repoint_blocked = True
                continue
            if not is_needed and not repair_unused:
                report["ignored_non_material"].append(
                    "%s\t%s\tusers=%d" % (image.name, expected, image.users)
                )
                continue

            relative = raw.startswith("//") or _is_within(expected, scene_dir)
            entry = "%s\t%s\tusers=%d" % (image.name, expected, image.users)
            candidate, ambiguous = _pick_candidate(
                index.get(_basename(expected).lower(), []), expected, raw, scene_dir
            )
            if ambiguous:
                entry = "%s\t%s\tcandidates=%s" % (
                    image.name, expected, " | ".join(ambiguous[:8])
                )
                report["ambiguous"].append(entry)
                record_unresolved(relative, entry)
                repoint_blocked = True
                continue

            if candidate:
                valid, why = file_looks_valid(candidate)
                if not valid:
                    missing = entry + "\tcandidate corrupt"
                    if is_needed:
                        report["corrupt"].append(
                            "%s\t%s\trepair candidate: %s"
                            % (image.name, candidate, why)
                        )
                    record_unresolved(relative, missing)
                    repoint_blocked = True
                    continue

            if candidate and (repair or repair_into_staging):
                try:
                    # Only source-writing repair copies the candidate into the
                    # scene folder. Staging mode takes the repoint path below,
                    # which rewrites image.filepath in this session only.
                    if relative and repair:
                        if not _is_within(expected, scene_dir):
                            raise OSError("relative destination escapes the blend directory")
                        real_parent = os.path.realpath(os.path.dirname(expected))
                        if not _is_within(real_parent, os.path.realpath(scene_dir)):
                            # Do not write through a directory symlink into a shared pool.
                            template = _udim_template(candidate, tile_number)
                            if pending_repoint is not None and _norm(template) != _norm(pending_repoint):
                                raise ValueError("UDIM candidates do not share one path template")
                            pending_repoint = template
                            pending_entries.append((relative, entry))
                            continue
                        method = _place_file(candidate, expected, repair_mode)
                        report["repaired"] += 1
                        by_method = report["repaired_by"]
                        by_method[method] = by_method.get(method, 0) + 1
                    else:
                        template = _udim_template(candidate, tile_number)
                        if pending_repoint is not None and _norm(template) != _norm(pending_repoint):
                            raise ValueError("UDIM candidates do not share one path template")
                        pending_repoint = template
                        pending_entries.append((relative, entry))
                    continue
                except Exception as exc:
                    repoint_blocked = True
                    report["warnings"].append(
                        "%s\t%s\trepair failed: %s" % (image.name, expected, exc)
                    )

            record_unresolved(relative, entry)
            repoint_blocked = True

        if pending_repoint is not None:
            # A UDIM image must be repointed as one tokenized path, and every
            # declared tile must exist under the same candidate template.
            candidate_paths = [
                pending_repoint.replace("<UDIM>", str(tile_number))
                if tile_number is not None else pending_repoint
                for _, _, tile_number in image_paths
            ]
            for candidate_path in candidate_paths:
                if not os.path.isfile(candidate_path):
                    report["warnings"].append(
                        "%s\tUDIM repoint missing candidate tile %s" % (image.name, candidate_path)
                    )
                    repoint_blocked = True
                    continue
                valid, why = file_looks_valid(candidate_path)
                if not valid:
                    if is_needed:
                        report["corrupt"].append(
                            "%s\t%s\trepoint candidate: %s"
                            % (image.name, candidate_path, why)
                        )
                    else:
                        report["warnings"].append(
                            "%s\t%s\tunused repoint candidate: %s"
                            % (image.name, candidate_path, why)
                        )
                    repoint_blocked = True
            if repoint_blocked:
                for relative, entry in pending_entries:
                    record_unresolved(relative, entry)
            else:
                # v1.2.1: record the original so the caller can put it back
                # before anything saves the .blend. A repoint is deliberately a
                # session-only resolution; persisting it would rewrite the
                # user's own texture paths as a side effect of baking.
                report["repointed_originals"].append(
                    [image.name, image.filepath])
                image.filepath = pending_repoint
                report["repointed"] += 1

    blocking_missing = list(report["missing_relative"])
    if strict_absolute:
        blocking_missing.extend(report["missing_absolute"])
    if report["corrupt"] and not allow_corrupt:
        report["code"] = 12
    elif blocking_missing and not allow_missing:
        report["code"] = 11
    report["ok"] = report["code"] == 0

    _write_marker(marker_dir, "MISSING_TEX", blocking_missing)
    _write_marker(marker_dir, "CORRUPT_TEX", report["corrupt"])
    report_path = os.path.join(marker_dir, "precheck_report.json")
    report["report_path"] = report_path
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    print(
        "PRECHECK images=%d material_used=%d repaired=%d repointed=%d "
        "missing_rel=%d missing_abs=%d ignored_non_material=%d corrupt=%d ambiguous=%d"
        % (
            report["image_files"], report["material_used"], report["repaired"],
            report["repointed"], len(report["missing_relative"]),
            len(report["missing_absolute"]), len(report["ignored_non_material"]),
            len(report["corrupt"]), len(report["ambiguous"]),
        ),
        flush=True,
    )
    if report["code"] == 12:
        print("PRECHECK REFUSE: corrupt material texture(s); see CORRUPT_TEX", flush=True)
    elif report["code"] == 11:
        print("PRECHECK REFUSE: unresolved material texture(s); see MISSING_TEX", flush=True)
    if raise_on_failure and report["code"]:
        raise SystemExit(int(report["code"]))
    return report
