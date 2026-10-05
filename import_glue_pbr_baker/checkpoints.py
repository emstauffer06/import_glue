"""Durable completed-part checkpoints.

A done-list entry says "this part's maps are on disk", but until now resuming it
also needed the generated output OBJECT to still be in the open blend.  Reopen
the original input, or lose the worker mid-run, and every finished part was
baked again.  A checkpoint makes a completed part restorable on its own:

    <store>/<object>-<hash>/g_<checkpoint id>/
        maps/<base>_ALB.png ...   byte-exact copies of the published maps
        payload.blend             the output object, its mesh, materials and
                                  map images, referencing only maps/ (relative)
        manifest.json             the commit marker -- written LAST

Write protocol (write_checkpoint):
  1. The entry must describe this live object and these maps (fingerprints are
     recomputed, never trusted), and the object must be capturable faithfully;
     anything we cannot capture is refused by name, not approximated.
  2. Artifacts go into a private ".g_<id>.tmp" directory; every file is
     written with exclusive create, fsynced, re-read and hashed, and each map is
     structurally decoded.
  3. The payload is loaded back into Blender, evaluated in a temporary
     collection, and its canonical content (evaluated mesh included) must equal
     the live object's before anything is published.
  4. The staging directory is renamed to "g_<id>" and only then is the
     manifest written beside it (as "manifest.json.partial"), fsynced and
     renamed into place.  No manifest, no generation: an interrupted write can
     never manufacture DONE status.  Earlier generations are never modified.

Restore protocol (restore_checkpoint):
  1. inspect_checkpoint() verifies schema, path confinement, sizes and SHA-256
     of every artifact before Blender reads a byte of the payload.
  2. The writer's Blender release and file format must be compatible; the
     CURRENT source, configuration and required dependencies must match.
  3. The payload is appended (scripts never run: no texts, no drivers, no
     animation data are accepted), every new datablock is tracked, and images
     are rebound to the verified checkpoint-owned maps.
  4. Every static content component must equal the recorded one BEFORE the
     payload is evaluated.  Only then is the object linked into a temporary
     validation collection and evaluated, and the evaluated mesh must equal the
     recorded one too.  Output dependency and UV gates run; only then are
     runtime names/paths/fingerprints regenerated, and the engine's own
     validate_done_entry() must accept them.
  5. The object is exposed (linked into the target collection) only after all of
     that.  Any failure removes exactly the datablocks this restore created.

Canonical content (checkpoint_content_fingerprint) retains transforms, topology,
positions, loops, UV values and UV/attribute names, material-slot assignment,
every modifier setting (nested ones such as Geometry Nodes inputs included),
the evaluated mesh, material settings, node graph structure and socket values,
image interpretation and verified image bytes.  It excludes only datablock
display names, UI/runtime state and the directory part of image paths, which a
restore may legitimately change.  Content it cannot represent is refused at
write time.  The rules are recorded in every manifest (NORMALIZATION).

References: a done entry's "checkpoint" records the absolute manifest path and
the path relative to the store root (<object>-<hash>/g_<id>/manifest.json), plus
the manifest's SHA-256.  restore_for_entry(..., store_root=) resolves the
relative path first, so a moved output root still finds its generations.

Durability: files and (on POSIX) directories are fsynced and every commit step
is a same-directory os.replace().  This has been exercised on local NTFS and
Linux overlay/ext4 filesystems only; no stronger guarantee is claimed for
network filesystems.

Engine binding: functions use the engine module this package imports.  When
engine.py runs as a standalone script it must call bind_engine(its module) so
config patches and caches are shared with the checkpoint code.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import time
import warnings
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import bpy
import numpy as np

# 2: compatibility records the writer's Blender release and file format, the
#    content normalization covers nested modifier settings and the evaluated
#    mesh, and references carry a store-relative manifest path.
CHECKPOINT_SCHEMA = 2
CONTENT_SCHEMA = 2
KIND = "import_glue_checkpoint"
MANIFEST_NAME = "manifest.json"
MANIFEST_PARTIAL = MANIFEST_NAME + ".partial"
PAYLOAD_NAME = "payload.blend"
MAPS_DIR = "maps"
# Short names on purpose: Windows without long-path support stops at 259
# characters, and every checkpoint path sits deeper than the run's own outputs.
GENERATION_PREFIX = "g_"
STAGING_PREFIX = ".g_"
STAGING_SUFFIX = ".tmp"
STORE_DIR_NAME = "_rbx_ckpt"
WINDOWS_MAX_PATH = 259
RESTORE_COLLECTION = "RBX_PBR_CHECKPOINT_RESTORED"
VALIDATION_COLLECTION = "RBX_PBR_CHECKPOINT_VALIDATING"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
PAYLOAD_SLACK_BYTES = 1024 * 1024
PAYLOAD_BYTES_PER_ELEMENT = 128

NORMALIZATION = {
    "version": 2,
    "excluded": [
        "datablock display names: object, mesh, material, image, node group, shape-key "
        "datablock, modifier",
        "directory part of image file paths (the file name is retained)",
        "mesh attributes whose names start with '.' (Blender selection/hide UI state)",
        "node layout and display properties (location, size, label, colour, selection, hide)",
        "modifier UI and runtime state: expanded/active/pinned/edit-mode/cage flags, panel open "
        "states, node warnings, execution time, persistent ids, Decimate face count, cache status",
        "selection flags of curve-mapping and profile points; derived custom-profile segments",
    ],
    "retained": [
        "object transform, delta transforms, colour, pass index, custom properties (full values)",
        "modifiers: type, order, viewport/render flags and every RNA setting, nested settings "
        "included (Geometry Nodes inputs, custom profiles, curve mappings, projector lists)",
        "evaluated mesh after modifiers and shape keys: topology, positions, UV values, "
        "attributes and corner normals",
        "vertex/edge/loop/polygon topology and positions, material indices, smooth/sharp/seam",
        "UV layer names, order, values, active and render-active flags",
        "named attributes and colour-attribute selection, corner normals",
        "vertex groups and weights, shape keys",
        "material-slot assignment and link mode, material and Cycles material settings",
        "node graph: node names and types, properties, socket values, links, group interfaces",
        "image source, colour space, alpha mode, precision flag, file name and file bytes",
    ],
    "refused": [
        "simulation and cache modifiers (cloth, soft body, collision, dynamic paint, fluid, "
        "particles, particle instance, explode, ocean, surface, mesh caches)",
        "Skin modifiers (skin radii live outside the modifier settings)",
        "bound Mesh Deform, Surface Deform, Laplacian Deform and Corrective Smooth modifiers "
        "(bind data)",
        "Multires modifiers with subdivision levels (displacement data)",
        "Geometry Nodes modifiers with bakes or simulation state",
        "parents, constraints, drivers, animation data, library-linked data, packed or generated "
        "images, references to other objects, and any setting the fingerprint cannot represent",
    ],
    "rename_policy": "restored datablocks take their recorded names when those names are free; "
                     "otherwise Blender's unique name is kept for the restored datablock only",
    "relocation_policy": "maps and payload are addressed relative to the generation directory; "
                         "a generation may move as a unit; done entries also record the manifest "
                         "path relative to the store root (<object>-<hash>/g_<id>/manifest.json)",
    "blender_policy": "restore refuses a checkpoint written by a different Blender major.minor "
                      "release or file-format version (BLENDER_CHANGED); a different patch "
                      "release is accepted and reported",
}

# Test-only instrumentation: called as hook(phase, detail) at each commit step.
_PHASE_HOOK: Optional[Callable[[str, Any], None]] = None
_ENGINE: Any = None
# Binds a restore's datablock handle to this Blender process: ID.session_uid
# values restart in every process, so a handle from another process must never
# match (and remove) unrelated datablocks.
_PROCESS_TOKEN = secrets.token_hex(8)

_ID_RE = re.compile(r"^\d{8}T\d{9}Z-[0-9a-f]{6}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    "%s%d" % (prefix, number) for prefix in ("COM", "LPT") for number in range(1, 10)}
_PAYLOAD_FROM_ALLOWED = {"objects", "meshes", "materials", "images", "node_groups"}
_NEW_ID_ALLOWED = {"OBJECT", "MESH", "MATERIAL", "IMAGE", "NODETREE", "KEY", "LIBRARY"}
_REQUIRED_ENTRY_KEYS = (
    "status", "route", "output_object", "folder", "source_fingerprint", "config_fingerprint",
    "output_fingerprints", "output_object_fingerprint", "uv_validation", "dependency_validation",
    "triangles", "resume_schema",
)
_DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_WINDOWS_DISK_FULL = {39, 112}  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL

# Properties that are datablock bookkeeping, never content.
_ID_META = {
    "rna_type", "name", "name_full", "id_type", "session_uid", "is_evaluated", "original",
    "users", "use_fake_user", "use_extra_user", "is_embedded_data", "is_linked_packed",
    "is_missing", "is_runtime_data", "is_editable", "tag", "is_library_indirect", "library",
    "library_weak_reference", "asset_data", "override_library", "preview", "animation_data",
}
_MATERIAL_EXCLUDED = _ID_META | {
    "node_tree", "texture_paint_images", "texture_paint_slots", "paint_active_slot",
    "paint_clone_slot", "preview_render_type", "use_preview_world", "grease_pencil",
    "is_grease_pencil", "lineart", "cycles", "line_color", "line_priority",
}
_NODE_EXCLUDED = {
    "rna_type", "name", "label", "location", "location_absolute", "width", "width_hidden",
    "height", "dimensions", "inputs", "outputs", "panel_states", "internal_links", "parent",
    "warning_propagation", "use_custom_color", "color", "color_tag", "select", "show_options",
    "show_preview", "hide", "show_texture", "bl_label", "bl_description", "bl_icon",
    "bl_static_type", "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default",
    "bl_height_min", "bl_height_max", "node_tree", "image", "image_user", "texture_mapping",
    "color_mapping", "color_ramp", "mapping",
}
_MODIFIER_EXCLUDED = {
    "rna_type", "name", "show_expanded", "show_on_cage", "show_in_editmode", "is_active",
    "is_override_data", "persistent_uid", "execution_time", "use_pin_to_last",
    # Geometry Nodes UI state and runtime reports (open_* panel flags are
    # excluded by prefix), Decimate's evaluated face count, Ocean cache status.
    "panels", "node_warnings", "show_group_selector", "show_manage_panel", "face_count",
    "is_cached",
}
# Inside nested modifier settings: UI selection flags everywhere, plus derived
# or UI-only members of specific structs.
_NESTED_EXCLUDED = {"rna_type", "select"}
_NESTED_EXCLUDED_BY_STRUCT = {
    "CurveProfile": {"segments"},                    # derived from points + resolution
    "GeometryNodesModifierInterface": {"panels"},   # panel open/closed state
}
_MAX_SETTINGS_DEPTH = 6
# Modifiers whose effect depends on state outside the RNA settings hashed here.
_REFUSED_MODIFIERS = {
    "CLOTH": "cloth simulation state", "SOFT_BODY": "soft-body simulation state",
    "COLLISION": "collision simulation state", "DYNAMIC_PAINT": "dynamic-paint simulation state",
    "FLUID": "fluid simulation state", "PARTICLE_SYSTEM": "particle simulation state",
    "PARTICLE_INSTANCE": "another object's particles", "EXPLODE": "particle simulation state",
    "OCEAN": "an ocean simulation and its cache", "SURFACE": "physics surface state",
    "MESH_CACHE": "an external mesh cache file", "MESH_SEQUENCE_CACHE": "an external cache file",
    "SKIN": "skin vertex radii stored outside the modifier settings",
}
_BIND_STATE = {"MESH_DEFORM": "is_bound", "SURFACE_DEFORM": "is_bound",
               "LAPLACIANDEFORM": "is_bind", "CORRECTIVE_SMOOTH": "is_bind"}
_INTERFACE_EXCLUDED = {"rna_type", "name", "description", "parent", "index", "position",
                       "is_panel_toggle", "select"}
_ATTRIBUTE_SPECS = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32),
    "FLOAT_VECTOR": ("vector", 3, np.float32), "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32), "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT2": ("vector", 2, np.float32), "INT8": ("value", 1, np.int32),
    "INT32_2D": ("value", 2, np.int32), "QUATERNION": ("value", 4, np.float32),
    "FLOAT4X4": ("value", 16, np.float32),
    # Blender 5.x: custom split normals are stored as INT16_2D "custom_normal".
    "INT16_2D": ("value", 2, np.int32), "FLOAT4": ("vector", 4, np.float32),
}
_SKIP = object()


# ------------------------------------------------------------------ errors

class CheckpointError(ValueError):
    """A checkpoint was refused or could not be written.

    ``code`` is a stable machine-readable reason; ``detail`` carries structured
    context (for example the differing content components).
    """

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


class CheckpointRejected(CheckpointError):
    """inspect/restore refused a generation; the scene was left unchanged."""


class CheckpointUnsupported(CheckpointError):
    """write refused: this output cannot be captured faithfully."""


class CheckpointWriteError(CheckpointError):
    """write failed; nothing was committed and earlier generations are intact."""


# ------------------------------------------------------------- utilities

def bind_engine(module: Any) -> None:
    """Use this engine module instead of importing the package's engine."""
    global _ENGINE
    _ENGINE = module


def _eng() -> Any:
    if _ENGINE is not None:
        return _ENGINE
    from . import engine
    return engine


def _text(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return repr(value)


def _phase(name: str, detail: Any = None) -> None:
    hook = _PHASE_HOOK
    if hook is not None:
        hook(name, detail)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _same_path(first: str, second: str) -> bool:
    return _norm_path(first) == _norm_path(second)


def _sha256_file(path: str) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _json_copy(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise CheckpointError("ENTRY_INVALID", "%s is not plain JSON data: %s" % (what, exc))


def _manifest_digest(body: Dict[str, Any]) -> str:
    """SHA-256 of the canonical manifest body, excluding the digest itself."""
    clean = {key: value for key, value in body.items() if key != "manifest_sha256"}
    data = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _new_checkpoint_id() -> str:
    """UTC time to the millisecond (sortable) plus 24 random bits."""
    seconds, millis = divmod(time.time_ns() // 1_000_000, 1000)
    return "%s%03dZ-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(seconds)), millis,
                           secrets.token_hex(3))


def _entry_bases(entry: Dict[str, Any]) -> List[str]:
    bases = entry.get("file_bases") or ([entry["file_base"]] if entry.get("file_base") else [])
    return [_text(base) for base in bases]


def _expected_maps(entry: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """{basename: (file_base, semantic)} for every map the entry published."""
    eng = _eng()
    result: Dict[str, Tuple[str, str]] = {}
    for base in _entry_bases(entry):
        for semantic in eng.CHANNELS:
            result[os.path.basename(eng.output_path("", base, semantic))] = (base, semantic)
    return result


def checkpoint_store_root(run_folder: str) -> str:
    """Store beside the done-list (one per scene), not inside a run folder."""
    return os.path.join(os.path.dirname(os.path.abspath(run_folder)), STORE_DIR_NAME)


def object_store_dir(store_root: str, source_name: str) -> str:
    """Portable, collision-free directory for one source object's generations."""
    name = _text(source_name)
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:20] or "object"
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return os.path.join(store_root, "%s-%s" % (readable, digest))


def _store_relative(manifest_path: str) -> str:
    """"<object store>/g_<id>/manifest.json": the manifest relative to its store root."""
    generation = os.path.dirname(os.path.abspath(manifest_path))
    return "/".join((os.path.basename(os.path.dirname(generation)),
                     os.path.basename(generation), MANIFEST_NAME))


# ------------------------------------------------------- datablock tracking

def _id_pointers() -> Set[int]:
    return {item.as_pointer() for item in bpy.data.all_ids}


def _new_ids(before: Set[int]) -> List[Any]:
    return [item for item in bpy.data.all_ids
            if item.as_pointer() not in before and not getattr(item, "is_embedded_data", False)]


def _remove_ids(items: Iterable[Any]) -> List[str]:
    """Remove exactly these datablocks; libraries last.  Returns failures."""
    alive, libraries, failures = [], [], []
    for item in items:
        try:
            kind = item.id_type
        except ReferenceError:
            continue
        (libraries if kind == "LIBRARY" else alive).append(item)
    for group in (alive, libraries):
        if not group:
            continue
        try:
            bpy.data.batch_remove(group)
        except Exception as exc:
            failures.append(_text(exc))
            for item in group:
                try:
                    collection = getattr(bpy.data, _ID_COLLECTIONS.get(item.id_type, ""), None)
                    if collection is not None:
                        collection.remove(item)
                except (ReferenceError, Exception) as inner:
                    failures.append(_text(inner))
    return failures


_ID_COLLECTIONS = {
    "OBJECT": "objects", "MESH": "meshes", "MATERIAL": "materials", "IMAGE": "images",
    "NODETREE": "node_groups", "KEY": "shape_keys", "LIBRARY": "libraries",
    "COLLECTION": "collections",
}


# ------------------------------------------------------- canonical content

def _feed(hasher: Any, *values: Any) -> None:
    """Length-prefixed, type-explicit hashing of JSON rows and numeric arrays."""
    for value in values:
        if isinstance(value, np.ndarray):
            header = json.dumps(["ndarray", value.dtype.str, list(value.shape)]).encode("utf-8")
            hasher.update(struct.pack(">Q", len(header)))
            hasher.update(header)
            data = np.ascontiguousarray(value).tobytes()
        else:
            data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8", "surrogatepass")
        hasher.update(struct.pack(">Q", len(data)))
        hasher.update(data)


def _simple(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (set, frozenset)):
        return sorted(_text(item) for item in value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            simple = _simple(item, depth + 1)
            if simple is _SKIP:
                return _SKIP
            out[_text(key)] = simple
        return out
    if isinstance(value, bpy.types.ID) or hasattr(value, "bl_rna") or isinstance(value, bytes):
        return _SKIP
    if depth > 3:
        return _SKIP
    try:
        items = list(value)
    except (TypeError, ValueError, ReferenceError):
        return _SKIP
    if len(items) > 64:
        return _SKIP
    out = []
    for item in items:
        simple = _simple(item, depth + 1)
        if simple is _SKIP:
            return _SKIP
        out.append(simple)
    return out


class _Refs:
    """Checkpoint-stable identities for the datablocks one output references.

    Materials, node groups and images are numbered in a deterministic traversal
    order (slot order, then node-name order) instead of by display name, and
    images are identified by the artifact they resolve to.
    """

    def __init__(self, artifact_ids: Dict[str, str]):
        self.artifacts = {_norm_path(path): _text(ident) for path, ident in artifact_ids.items()}
        self.materials: Dict[int, str] = {}
        self.material_list: List[Tuple[str, Any]] = []
        self.groups: Dict[int, str] = {}
        self.group_list: List[Tuple[str, Any]] = []
        self.images: Dict[int, str] = {}
        self.image_list: List[Tuple[str, Any, str]] = []

    def material(self, material: Any) -> str:
        key = material.as_pointer()
        if key not in self.materials:
            ident = "material%d" % len(self.material_list)
            self.materials[key] = ident
            self.material_list.append((ident, material))
        return self.materials[key]

    def group(self, tree: Any) -> str:
        key = tree.as_pointer()
        if key not in self.groups:
            ident = "group%d" % len(self.group_list)
            self.groups[key] = ident
            self.group_list.append((ident, tree))
        return self.groups[key]

    def image(self, image: Any) -> str:
        key = image.as_pointer()
        if key in self.images:
            return self.images[key]
        name = _text(image.name)
        if getattr(image, "source", "") != "FILE":
            raise CheckpointUnsupported(
                "UNSUPPORTED", "image %r is %s, not a published map file"
                % (name, getattr(image, "source", "?")))
        if image.packed_file is not None or len(getattr(image, "packed_files", ())):
            raise CheckpointUnsupported(
                "UNSUPPORTED", "image %r is packed; checkpoints capture published map files" % name)
        raw = _text(image.filepath)
        try:
            absolute = bpy.path.abspath(raw, library=image.library)
        except Exception:
            absolute = raw
        ident = self.artifacts.get(_norm_path(absolute)) if absolute else None
        if ident is None:
            raise CheckpointUnsupported(
                "UNSUPPORTED", "image %r (%s) is not one of this part's published maps"
                % (name, raw))
        self.images[key] = ident
        self.image_list.append((ident, image, absolute))
        return ident

    def ref(self, value: Any) -> str:
        kind = getattr(value, "id_type", "")
        if kind == "MATERIAL":
            return self.material(value)
        if kind == "NODETREE":
            return self.group(value)
        if kind == "IMAGE":
            return self.image(value)
        raise CheckpointUnsupported(
            "UNSUPPORTED", "output references external %s datablock %r"
            % (kind or type(value).__name__, _text(getattr(value, "name", value))))


def _rna_rows(value: Any, excluded: Set[str], refs: _Refs) -> List[Any]:
    rows: List[Any] = []
    try:
        properties = value.bl_rna.properties
    except Exception:
        return rows
    for prop in properties:
        name = prop.identifier
        if name in excluded or prop.type == "COLLECTION":
            continue
        try:
            current = getattr(value, name)
        except Exception:
            continue
        if prop.type == "POINTER":
            if current is None:
                rows.append([name, None])
            elif isinstance(current, bpy.types.ID):
                rows.append([name, "id:" + refs.ref(current)])
            continue  # nested structs are hashed explicitly where they matter
        simple = _simple(current)
        if simple is not _SKIP:
            rows.append([name, simple])
    return rows


def _plain_value(value: Any, refs: _Refs, where: str) -> Any:
    """A full, JSON-representable copy of a property value, or a refusal.

    Unlike _simple() nothing is capped or skipped: a value the fingerprint
    cannot represent refuses the capture (UNSUPPORTED) instead of being left
    out of the digest."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, bpy.types.ID):
        return "id:" + refs.ref(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_text(item) for item in value)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif hasattr(value, "to_list"):
        value = value.to_list()
    if isinstance(value, dict):
        return {_text(key): _plain_value(item, refs, where) for key, item in value.items()}
    if hasattr(value, "bl_rna"):
        raise CheckpointUnsupported(
            "UNSUPPORTED", "%s holds a Blender struct where a value was expected" % where)
    try:
        items = list(value)
    except (TypeError, ValueError, ReferenceError):
        raise CheckpointUnsupported(
            "UNSUPPORTED", "%s holds a %s value the content fingerprint cannot represent"
            % (where, type(value).__name__))
    return [_plain_value(item, refs, where) for item in items]


def _struct_rows(value: Any, excluded: Set[str], refs: _Refs, where: str,
                 depth: int = 0) -> List[Any]:
    """Every RNA property of a settings struct, nested structs and collections
    included (REVIEW_B finding 1: Geometry Nodes inputs, custom profiles and
    curve mappings live in nested structs).  Nothing is skipped silently:
    anything that cannot be represented refuses the capture."""
    if depth > _MAX_SETTINGS_DEPTH:
        raise CheckpointUnsupported(
            "UNSUPPORTED", "%s nests deeper than %d levels" % (where, _MAX_SETTINGS_DEPTH))
    if depth:
        excluded = _NESTED_EXCLUDED | _NESTED_EXCLUDED_BY_STRUCT.get(value.bl_rna.identifier, set())
    rows: List[Any] = []
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name in excluded or (depth == 0 and name.startswith("open_")):
            continue
        path = "%s.%s" % (where, name)
        try:
            current = getattr(value, name)
        except Exception as exc:
            raise CheckpointUnsupported("UNSUPPORTED", "%s cannot be read: %s" % (path, _text(exc)))
        if prop.type == "POINTER":
            if current is None:
                rows.append([name, None])
            elif isinstance(current, bpy.types.ID):
                rows.append([name, "id:" + refs.ref(current)])
            else:
                rows.append([name, _struct_rows(current, set(), refs, path, depth + 1)])
        elif prop.type == "COLLECTION":
            items = []
            for index, item in enumerate(current):
                if isinstance(item, bpy.types.ID):
                    items.append("id:" + refs.ref(item))
                else:
                    items.append(_struct_rows(item, set(), refs, "%s[%d]" % (path, index),
                                              depth + 1))
            rows.append([name, items])
        else:
            rows.append([name, _plain_value(current, refs, path)])
    return rows


def _modifier_rows(modifier: Any, refs: _Refs) -> List[Any]:
    return _struct_rows(modifier, _MODIFIER_EXCLUDED, refs,
                        "modifier %r" % _text(modifier.name))


def _array(collection: Any, attribute: str, width: int, dtype: Any) -> np.ndarray:
    values = np.empty(len(collection) * width, dtype=dtype)
    if len(values):
        collection.foreach_get(attribute, values)
    return values


def _custom_properties(owner: Any, refs: _Refs) -> List[Any]:
    rows = []
    try:
        keys = sorted(owner.keys())
    except Exception:
        return rows
    for key in keys:
        rows.append([_text(key), _plain_value(owner[key], refs,
                                              "custom property %r" % _text(key))])
    return rows


def _object_component(obj: Any, refs: _Refs) -> str:
    # matrix_world is runtime-evaluated (an appended, not-yet-evaluated copy
    # does not carry it).  Parents and constraints are refused, so the stored
    # basis transform plus deltas fully determines the world transform.
    hasher = hashlib.sha256()
    _feed(hasher, "object-v1", obj.type, [list(row) for row in obj.matrix_basis],
          list(obj.location), obj.rotation_mode, list(obj.rotation_euler),
          list(obj.rotation_quaternion), list(obj.rotation_axis_angle), list(obj.scale),
          list(obj.delta_location), list(obj.delta_rotation_euler),
          obj.delta_rotation_euler.order, list(obj.delta_rotation_quaternion),
          list(obj.delta_scale), obj.rotation_mode, list(obj.color), int(obj.pass_index))
    _feed(hasher, "parent", None if obj.parent is None else "id:" + refs.ref(obj.parent))
    _feed(hasher, "constraints", [constraint.type for constraint in obj.constraints])
    _feed(hasher, "custom", _custom_properties(obj, refs))
    for modifier in obj.modifiers:
        _feed(hasher, "modifier", modifier.type, bool(modifier.show_viewport),
              bool(modifier.show_render), _modifier_rows(modifier, refs))
    return hasher.hexdigest()


def _mesh_components(mesh: Any, refs: Optional[_Refs], custom: bool = True) -> Dict[str, str]:
    geometry = hashlib.sha256()
    _feed(geometry, "geometry-v1", len(mesh.vertices), len(mesh.edges), len(mesh.loops),
          len(mesh.polygons))
    _feed(geometry, _array(mesh.vertices, "co", 3, np.float32))
    _feed(geometry, _array(mesh.edges, "vertices", 2, np.int32))
    _feed(geometry, _array(mesh.loops, "vertex_index", 1, np.int32))
    for name in ("loop_start", "loop_total", "material_index"):
        _feed(geometry, name, _array(mesh.polygons, name, 1, np.int32))
    _feed(geometry, "use_smooth", _array(mesh.polygons, "use_smooth", 1, np.bool_))
    for name in ("use_edge_sharp", "use_seam"):
        if len(mesh.edges) and hasattr(mesh.edges[0], name):
            _feed(geometry, name, _array(mesh.edges, name, 1, np.bool_))
    _feed(geometry, "texspace", bool(getattr(mesh, "use_auto_texspace", True)),
          list(getattr(mesh, "texspace_location", ())), list(getattr(mesh, "texspace_size", ())))
    if custom:
        _feed(geometry, "mesh-custom", _custom_properties(mesh, refs))

    uv = hashlib.sha256()
    _feed(uv, "uv-v1", int(getattr(mesh.uv_layers, "active_index", -1)))
    for layer in mesh.uv_layers:
        _feed(uv, _text(layer.name), bool(getattr(layer, "active_render", False)),
              len(layer.data), _array(layer.data, "uv", 2, np.float32))

    attributes = hashlib.sha256()
    uv_names = {_text(layer.name) for layer in mesh.uv_layers}
    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is not None:
        _feed(attributes, "color-selection",
              _text(getattr(color_attributes, "active_color_name", "") or ""),
              _text(getattr(color_attributes, "default_color_name", "") or ""))
    rows = sorted(
        (attribute for attribute in mesh.attributes
         if not _text(attribute.name).startswith(".") and _text(attribute.name) not in uv_names),
        key=lambda item: (_text(item.name), item.domain, item.data_type))
    for attribute in rows:
        _feed(attributes, "attribute", _text(attribute.name), attribute.domain,
              attribute.data_type, len(attribute.data))
        if attribute.data_type == "STRING":
            _feed(attributes, [_text(getattr(item, "value", "")) for item in attribute.data])
            continue
        spec = _ATTRIBUTE_SPECS.get(attribute.data_type)
        if spec is None:
            raise CheckpointUnsupported(
                "UNSUPPORTED", "mesh attribute %r has unsupported type %s"
                % (_text(attribute.name), attribute.data_type))
        property_name, width, dtype = spec
        _feed(attributes, _array(attribute.data, property_name, width, dtype))

    normals = hashlib.sha256()
    corner = mesh.corner_normals
    _feed(normals, "corner-normals-v1", len(corner), _array(corner, "vector", 3, np.float32))
    return {"geometry": geometry.hexdigest(), "uv": uv.hexdigest(),
            "attributes": attributes.hexdigest(), "normals": normals.hexdigest()}


def _evaluated_component(obj: Any) -> str:
    """Digest of the evaluated (final) mesh: what Cycles baked and what an
    exporter consumes after modifiers and shape keys.  This binds a restored
    output to the geometry the maps were baked for even where a setting the
    static components hash could still miss (REVIEW_B finding 1, option b).

    The object must be in the active view layer: an object outside the
    depsgraph has no evaluated state, and hashing its base mesh instead would
    silently ignore every modifier."""
    obj.update_tag(refresh={"OBJECT", "DATA"})  # evaluate from current data, never a stale cache
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    if not getattr(evaluated, "is_evaluated", False):
        raise CheckpointUnsupported(
            "UNSUPPORTED", "output %r is not in the active view layer, so its evaluated geometry "
            "cannot be fingerprinted" % _text(obj.name))
    mesh = evaluated.to_mesh()
    try:
        parts = _mesh_components(mesh, None, custom=False)
    finally:
        evaluated.to_mesh_clear()
    hasher = hashlib.sha256()
    _feed(hasher, "evaluated-v1", sorted(parts.items()))
    return hasher.hexdigest()


def _deform_component(obj: Any) -> str:
    hasher = hashlib.sha256()
    groups = sorted(obj.vertex_groups, key=lambda group: int(group.index))
    _feed(hasher, "vertex-groups-v1",
          [[int(group.index), _text(group.name), bool(group.lock_weight)] for group in groups])
    rows = []
    for vertex in obj.data.vertices:
        rows.append([int(vertex.index)] + [
            [int(item.group), float(item.weight)]
            for item in sorted(vertex.groups, key=lambda item: int(item.group))])
    _feed(hasher, rows)
    keys = obj.data.shape_keys
    if keys is None:
        _feed(hasher, "no-shape-keys")
        return hasher.hexdigest()
    _feed(hasher, "shape-keys", bool(keys.use_relative), float(keys.eval_time),
          _text(keys.reference_key.name) if keys.reference_key else None)
    for block in keys.key_blocks:
        _feed(hasher, _text(block.name),
              _text(block.relative_key.name) if block.relative_key else None,
              block.interpolation, bool(block.mute), float(block.slider_min),
              float(block.slider_max), float(block.value), _text(block.vertex_group),
              _array(block.data, "co", 3, np.float32))
    return hasher.hexdigest()


def _tree_rows(hasher: Any, tree: Any, refs: _Refs) -> None:
    for node in sorted(tree.nodes, key=lambda item: (_text(item.name), item.bl_idname)):
        _feed(hasher, "node", node.bl_idname, _text(node.name), bool(node.mute),
              _rna_rows(node, _NODE_EXCLUDED, refs))
        if "image" in node.bl_rna.properties:
            image = node.image
            _feed(hasher, "image", None if image is None else "id:" + refs.image(image))
        for struct_name in ("image_user", "texture_mapping", "color_mapping"):
            if struct_name in node.bl_rna.properties:
                _feed(hasher, struct_name,
                      _rna_rows(getattr(node, struct_name), {"rna_type"}, refs))
        if "node_tree" in node.bl_rna.properties:
            sub = node.node_tree
            _feed(hasher, "group-ref", None if sub is None else "id:" + refs.group(sub))
        ramp = getattr(node, "color_ramp", None)
        if ramp is not None:
            _feed(hasher, "color-ramp", ramp.color_mode, ramp.interpolation,
                  getattr(ramp, "hue_interpolation", ""),
                  [[float(item.position), list(item.color)] for item in ramp.elements])
        mapping = getattr(node, "mapping", None)
        if mapping is not None and hasattr(mapping, "curves"):
            _feed(hasher, "curve-mapping", _rna_rows(mapping, {"rna_type"}, refs),
                  [[[list(point.location), getattr(point, "handle_type", "")]
                    for point in curve.points] for curve in mapping.curves])
        for direction, sockets in (("in", node.inputs), ("out", node.outputs)):
            for socket in sockets:
                row = [direction, _text(socket.identifier), socket.bl_idname,
                       bool(socket.is_linked)]
                if hasattr(socket, "default_value"):
                    value = socket.default_value
                    if isinstance(value, bpy.types.ID):
                        row.append("id:" + refs.ref(value))
                    else:
                        simple = _simple(value)
                        row.append(None if simple is _SKIP else simple)
                _feed(hasher, row)
    links = sorted(
        [_text(link.from_node.name), _text(link.from_socket.identifier),
         _text(link.to_node.name), _text(link.to_socket.identifier),
         bool(getattr(link, "is_muted", False))]
        for link in tree.links)
    _feed(hasher, "links", links)
    interface = getattr(tree, "interface", None)
    if interface is not None and getattr(tree, "is_embedded_data", False) is False:
        items = []
        for item in interface.items_tree:
            row = [_text(getattr(item, "identifier", "")), item.item_type,
                   _rna_rows(item, _INTERFACE_EXCLUDED, refs)]
            items.append(row)
        _feed(hasher, "interface", items)


def _materials_component(obj: Any, refs: _Refs) -> str:
    hasher = hashlib.sha256()
    slots = []
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        slots.append([index, slot.link, None if material is None else refs.material(material)])
    _feed(hasher, "slots-v1", slots)
    position = 0
    while position < len(refs.material_list):
        ident, material = refs.material_list[position]
        position += 1
        _feed(hasher, "material", ident, _rna_rows(material, _MATERIAL_EXCLUDED, refs))
        cycles = getattr(material, "cycles", None)
        if cycles is not None:
            _feed(hasher, "cycles", _rna_rows(cycles, {"rna_type"}, refs))
        tree = material.node_tree
        if tree is None:
            _feed(hasher, "no-node-tree")
        else:
            _tree_rows(hasher, tree, refs)
    position = 0
    while position < len(refs.group_list):
        ident, group = refs.group_list[position]
        position += 1
        _feed(hasher, "group", ident, group.bl_idname)
        _tree_rows(hasher, group, refs)
    return hasher.hexdigest()


def _images_component(refs: _Refs) -> str:
    hasher = hashlib.sha256()
    cache: Dict[str, Tuple[int, str]] = {}
    rows = []
    for ident, image, absolute in refs.image_list:
        key = _norm_path(absolute)
        if key not in cache:
            try:
                cache[key] = _sha256_file(absolute)
            except OSError as exc:
                raise CheckpointUnsupported(
                    "UNSUPPORTED", "map %s is unreadable: %s" % (absolute, exc))
        size, digest = cache[key]
        rows.append([ident, os.path.basename(absolute), image.source,
                     _text(image.colorspace_settings.name), image.alpha_mode,
                     bool(getattr(image, "use_half_precision", False)), size, digest])
    _feed(hasher, "images-v1", sorted(rows))
    return hasher.hexdigest()


def checkpoint_content_fingerprint(output_object: Any, artifact_ids: Dict[str, str], *,
                                   evaluated: bool = True) -> Dict[str, Any]:
    """Fingerprint actual restored content independent of allowed relocation.

    ``artifact_ids`` maps a map file path (any spelling) to its checkpoint-stable
    artifact id.  Every image the output references must resolve to one of those
    files; its bytes are hashed here, from disk, not taken from the mapping.
    With ``evaluated`` (the default, and what a manifest records) the evaluated
    mesh is a component too, which needs the object in the active view layer;
    ``evaluated=False`` gives the static components only, so a restore can
    compare them before it lets Blender evaluate a payload.
    Raises CheckpointUnsupported for content that cannot be identified stably.
    """
    obj = output_object
    if obj is None or getattr(obj, "type", None) != "MESH" or obj.data is None:
        raise CheckpointUnsupported("UNSUPPORTED", "output is not a mesh object")
    refs = _Refs(artifact_ids)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        components = {"object": _object_component(obj, refs)}
        components.update(_mesh_components(obj.data, refs))
        components["deform"] = _deform_component(obj)
        if evaluated:
            components["evaluated"] = _evaluated_component(obj)
        components["materials"] = _materials_component(obj, refs)
        components["images"] = _images_component(refs)
    total = hashlib.sha256()
    _feed(total, "content-v%d" % CONTENT_SCHEMA, sorted(components.items()))
    return {
        "schema_version": CONTENT_SCHEMA,
        "sha256": total.hexdigest(),
        "components": components,
        "evaluated": bool(evaluated),
        "artifacts": sorted({ident for ident, _image, _path in refs.image_list}),
        "normalization": NORMALIZATION["version"],
    }


# --------------------------------------------------------- capture limits

def _closure_ids(obj: Any) -> List[Any]:
    """Object, mesh, shape keys, materials, embedded trees, groups and images."""
    found: List[Any] = [obj, obj.data]
    if obj.data.shape_keys is not None:
        found.append(obj.data.shape_keys)
    seen: Set[int] = set()

    def walk(tree: Any) -> None:
        if tree is None or tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        found.append(tree)
        for node in tree.nodes:
            image = getattr(node, "image", None) if "image" in node.bl_rna.properties else None
            if image is not None:
                found.append(image)
            if "node_tree" in node.bl_rna.properties and node.node_tree is not None:
                walk(node.node_tree)
            for socket in list(node.inputs) + list(node.outputs):
                value = getattr(socket, "default_value", None)
                if isinstance(value, bpy.types.ID):
                    found.append(value)

    for slot in obj.material_slots:
        material = slot.material
        if material is not None:
            found.append(material)
            walk(material.node_tree)
    for modifier in obj.modifiers:
        # Geometry Nodes trees ride along with the output (it is a copy of the
        # source), so they must pass the same library/animation checks.
        walk(getattr(modifier, "node_group", None))
    return found


def _capture_problems(obj: Any) -> List[str]:
    """Reasons this output cannot be captured faithfully (empty = capturable)."""
    problems: List[str] = []
    if obj is None or getattr(obj, "type", None) != "MESH" or obj.data is None:
        return ["output is not a mesh object"]
    if obj.parent is not None:
        problems.append("output is parented to %r; parent relations are not captured"
                        % _text(obj.parent.name))
    if len(obj.constraints):
        problems.append("output has %d constraint(s); constraints are not captured"
                        % len(obj.constraints))
    for modifier in obj.modifiers:
        label = "modifier %r (%s)" % (_text(modifier.name), modifier.type)
        state = _REFUSED_MODIFIERS.get(modifier.type)
        if state:
            problems.append("%s depends on %s, which checkpoints do not capture" % (label, state))
        flag = _BIND_STATE.get(modifier.type)
        if flag and bool(getattr(modifier, flag, False)):
            problems.append("%s is bound; its bind data is not captured" % label)
        if modifier.type == "MULTIRES" and int(getattr(modifier, "total_levels", 0) or 0) > 0:
            problems.append("%s has subdivision levels; its displacement data is not captured"
                            % label)
        if modifier.type == "NODES" and len(getattr(modifier, "bakes", ())):
            problems.append("%s has Geometry Nodes bakes or simulation state, which checkpoints "
                            "do not capture" % label)
    for item in _closure_ids(obj):
        label = "%s %r" % (getattr(item, "id_type", type(item).__name__).lower(),
                           _text(getattr(item, "name", "")))
        if getattr(item, "library", None) is not None or getattr(item, "override_library", None):
            problems.append("%s is linked from a library" % label)
        animation = getattr(item, "animation_data", None)
        if animation is not None and (
                animation.action is not None or len(animation.drivers)
                or len(getattr(animation, "nla_tracks", ()))):
            problems.append("%s has animation data or drivers; scripts and drivers are never "
                            "captured or executed by checkpoints" % label)
    return problems


def _image_basenames(obj: Any, map_paths: Dict[str, str]) -> Dict[int, str]:
    """{image pointer: map basename} for every image the output references."""
    lookup = {_norm_path(path): basename for basename, path in map_paths.items()}
    result: Dict[int, str] = {}
    for item in _closure_ids(obj):
        if getattr(item, "id_type", "") != "IMAGE":
            continue
        try:
            absolute = bpy.path.abspath(_text(item.filepath), library=item.library)
        except Exception:
            absolute = _text(item.filepath)
        basename = lookup.get(_norm_path(absolute)) if absolute else None
        if basename is None:
            raise CheckpointUnsupported(
                "UNSUPPORTED", "image %r is not one of this part's published maps"
                % _text(item.name))
        result[item.as_pointer()] = basename
    return result


def _datablock_names(obj: Any, image_basenames: Dict[int, str]) -> Dict[str, Any]:
    images: Dict[str, str] = {}
    for item in _closure_ids(obj):
        if getattr(item, "id_type", "") == "IMAGE":
            basename = image_basenames.get(item.as_pointer())
            if basename:
                images.setdefault(basename, _text(item.name))
    return {
        "object": _text(obj.name),
        "mesh": _text(obj.data.name),
        "materials": [None if slot.material is None else _text(slot.material.name)
                      for slot in obj.material_slots],
        "images": images,
    }


# ------------------------------------------------------------ file writing

def _write_stream(path: str, chunks: Iterable[bytes]) -> None:
    """Exclusive-create, write, flush and fsync one file.  Never overwrites."""
    handle = open(path, "xb")
    try:
        with handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _fsync_file(path: str) -> None:
    # Windows: os.fsync() needs a writable handle (same as engine._flush_to_disk).
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: str) -> None:
    """Persist a directory entry change on POSIX; Windows has no directory fsync."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass  # some filesystems refuse directory fsync; the rename still happened
    finally:
        os.close(descriptor)


def _replace(source: str, destination: str) -> None:
    """os.replace with a short retry for transient Windows sharing violations
    (an antivirus scanner holding a just-written file open)."""
    delay = 0.05
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def _disk_free(path: str) -> Optional[int]:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _libraries_write(path: str, datablocks: Set[Any]) -> None:
    bpy.data.libraries.write(path, datablocks, path_remap="NONE", fake_user=False,
                             compress=False)


def _write_error(exc: BaseException, action: str) -> CheckpointWriteError:
    code = "WRITE_FAILED"
    if isinstance(exc, OSError):
        if exc.errno in _DISK_FULL_ERRNOS or getattr(exc, "winerror", None) in _WINDOWS_DISK_FULL:
            code = "DISK_FULL"
        elif exc.errno == errno.EROFS:
            code = "READ_ONLY"
        elif isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            code = "PERMISSION"
    else:
        text = _text(exc).lower()
        if "no space" in text or "disk full" in text or "not enough space" in text:
            code = "DISK_FULL"
        elif "read-only" in text or "read only" in text:
            code = "READ_ONLY"
        elif "permission denied" in text or "access is denied" in text:
            code = "PERMISSION"
    return CheckpointWriteError(code, "%s failed: %s" % (action, _text(exc)), {"action": action})


def _copy_verified(source: str, destination: str) -> Tuple[int, str]:
    """Copy with exclusive create + fsync, then re-read: returns (size, sha256)."""
    digest = hashlib.sha256()
    counter = [0]

    def chunks() -> Iterable[bytes]:
        with open(source, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
                counter[0] += len(block)
                yield block

    _write_stream(destination, chunks())
    size, written = _sha256_file(destination)
    if size != counter[0] or written != digest.hexdigest():
        raise CheckpointWriteError(
            "VERIFY_FAILED", "%s did not read back identically" % os.path.basename(destination))
    return size, written


def _discard_dir(path: str) -> None:
    def retry(function: Any, target: str, _info: Any) -> None:
        try:
            os.chmod(target, stat.S_IRWXU)
            function(target)
        except OSError:
            pass
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=retry)


def _write_payload(output_object: Any, payload_path: str,
                   image_basenames: Dict[int, str]) -> Dict[str, Any]:
    """Write a self-contained payload .blend for one output object.

    Private copies of the object, mesh, materials, image-bearing node groups and
    images are created, the image copies are pointed at "//maps/<file>" relative
    to the payload, the copies are written, and every copy is removed again.  The
    live output and its references are never modified.
    """
    before = _id_pointers()
    try:
        image_copies: Dict[int, Any] = {}
        group_copies: Dict[int, Any] = {}
        material_copies: Dict[int, Any] = {}
        image_trees: Dict[int, bool] = {}

        def image_copy(image: Any) -> Any:
            key = image.as_pointer()
            if key not in image_copies:
                basename = image_basenames.get(key)
                if basename is None:
                    raise CheckpointUnsupported(
                        "UNSUPPORTED", "image %r is not one of this part's published maps"
                        % _text(image.name))
                duplicate = image.copy()
                duplicate.filepath_raw = "//%s/%s" % (MAPS_DIR, basename)
                image_copies[key] = duplicate
            return image_copies[key]

        def has_images(tree: Any, stack: Tuple[int, ...] = ()) -> bool:
            key = tree.as_pointer()
            if key in image_trees:
                return image_trees[key]
            if key in stack:
                return False
            found = False
            for node in tree.nodes:
                if "image" in node.bl_rna.properties and node.image is not None:
                    found = True
                elif ("node_tree" in node.bl_rna.properties and node.node_tree is not None
                      and has_images(node.node_tree, stack + (key,))):
                    found = True
                elif any(isinstance(getattr(socket, "default_value", None), bpy.types.Image)
                         for socket in node.inputs):
                    found = True
                if found:
                    break
            image_trees[key] = found
            return found

        def remap(tree: Any) -> None:
            for node in tree.nodes:
                if "image" in node.bl_rna.properties and node.image is not None:
                    node.image = image_copy(node.image)
                if ("node_tree" in node.bl_rna.properties and node.node_tree is not None
                        and has_images(node.node_tree)):
                    node.node_tree = group_copy(node.node_tree)
                for socket in node.inputs:
                    value = getattr(socket, "default_value", None)
                    if isinstance(value, bpy.types.Image):
                        socket.default_value = image_copy(value)

        def group_copy(tree: Any) -> Any:
            key = tree.as_pointer()
            if key not in group_copies:
                duplicate = tree.copy()
                group_copies[key] = duplicate
                remap(duplicate)
            return group_copies[key]

        def material_copy(material: Any) -> Any:
            key = material.as_pointer()
            if key not in material_copies:
                duplicate = material.copy()
                material_copies[key] = duplicate
                if duplicate.node_tree is not None:
                    remap(duplicate.node_tree)
            return material_copies[key]

        mesh = output_object.data.copy()
        for index, material in enumerate(mesh.materials):
            if material is not None:
                mesh.materials[index] = material_copy(material)
        obj = output_object.copy()
        obj.data = mesh
        for slot in obj.material_slots:
            if slot.link == "OBJECT" and slot.material is not None:
                slot.material = material_copy(slot.material)
        _libraries_write(payload_path, {obj})
        return {"object": _text(obj.name)}
    finally:
        _remove_ids(_new_ids(before))


class _Appended:
    """A payload appended into the open blend, with every new datablock tracked."""

    def __init__(self, obj: Any, new_ids: List[Any]):
        self.obj = obj
        self.new_ids = new_ids

    def stage(self, name: str) -> Any:
        """Link the object into a new, tracked collection and evaluate it.

        The payload carries the select flag the output had when it was
        written; staging must not change the user's selection."""
        collection = bpy.data.collections.new(name)
        self.new_ids.append(collection)
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(self.obj)
        self.obj.select_set(False)
        bpy.context.view_layer.update()
        return collection

    def discard(self) -> List[str]:
        return _remove_ids(self.new_ids)


def _append_payload(payload_path: str, object_name: str, maps_dir: str,
                    allowed_basenames: Set[str]) -> _Appended:
    """Append the payload object; refuse anything that could run code or escape."""
    before = _id_pointers()
    forbidden: Dict[str, List[str]] = {}
    objects: List[str] = []
    try:
        with bpy.data.libraries.load(payload_path, link=False, relative=False) as (source, target):
            for attribute in dir(source):
                if attribute.startswith("_") or attribute in _PAYLOAD_FROM_ALLOWED:
                    continue
                value = getattr(source, attribute, None)
                if isinstance(value, list) and value:
                    forbidden[attribute] = [_text(item) for item in value[:8]]
            objects = [_text(item) for item in source.objects]
            if not forbidden and objects == [object_name]:
                target.objects = [object_name]
    except Exception as exc:
        _remove_ids(_new_ids(before))
        raise CheckpointRejected("PAYLOAD_UNREADABLE", "payload could not be read: %s" % _text(exc))
    new = _new_ids(before)
    try:
        if forbidden:
            raise CheckpointRejected(
                "PAYLOAD_UNSAFE", "payload carries datablocks a checkpoint never writes: %s"
                % ", ".join("%s=%s" % item for item in sorted(forbidden.items())))
        if objects != [object_name]:
            raise CheckpointRejected(
                "PAYLOAD_UNSAFE", "payload objects %r, expected exactly %r" % (objects, object_name))
        counts = Counter(item.id_type for item in new)
        unexpected = sorted("%s:%s" % (item.id_type, _text(item.name)) for item in new
                            if item.id_type not in _NEW_ID_ALLOWED)
        if (unexpected or counts["OBJECT"] != 1 or counts["MESH"] != 1
                or counts["KEY"] > 1 or counts["LIBRARY"] > 1):
            raise CheckpointRejected(
                "PAYLOAD_UNSAFE", "payload created unexpected datablocks: %s (counts %s)"
                % (", ".join(unexpected) or "-", dict(counts)))
        obj = next(item for item in new if item.id_type == "OBJECT")
        trees = [item.node_tree for item in new
                 if item.id_type == "MATERIAL" and item.node_tree is not None]
        for item in list(new) + trees:
            if getattr(item, "animation_data", None) is not None:
                raise CheckpointRejected(
                    "PAYLOAD_UNSAFE", "%s %r carries animation data or drivers; checkpoints "
                    "never evaluate them" % (item.id_type, _text(item.name)))
        if obj.parent is not None or len(obj.constraints):
            raise CheckpointRejected("PAYLOAD_UNSAFE", "payload object has a parent or constraints")
        for image in (item for item in new if item.id_type == "IMAGE"):
            if image.source != "FILE" or image.packed_file is not None or len(image.packed_files):
                raise CheckpointRejected(
                    "PAYLOAD_UNSAFE", "payload image %r is not an external map file" % image.name)
            raw = _text(image.filepath)
            basename = raw.replace("\\", "/").rsplit("/", 1)[-1]
            expected = os.path.join(maps_dir, basename)
            resolved = bpy.path.abspath(raw, library=image.library)
            if basename not in allowed_basenames or not _same_path(resolved, expected):
                raise CheckpointRejected(
                    "PAYLOAD_UNSAFE", "payload image %r points outside this generation's maps (%s)"
                    % (_text(image.name), raw))
            # Rebind to the verified, checkpoint-owned file (absolute, so the
            # binding never depends on where the open blend happens to live).
            image.filepath = expected
        return _Appended(obj, new)
    except BaseException:
        _remove_ids(new)
        raise


def write_checkpoint(folder: str, output_object: Any, resume_entry: Dict[str, Any], *,
                     source_name: Optional[str] = None,
                     verify_roundtrip: bool = True) -> Dict[str, Any]:
    """Create and verify an immutable generation; publish its manifest last.

    ``folder`` holds this source object's generations (see object_store_dir).
    ``resume_entry`` is the recorded done-list entry for ``output_object``.
    Returns a JSON-safe reference for the done entry's "checkpoint" field.
    Raises CheckpointError (ENTRY_INVALID / STALE_ENTRY), CheckpointUnsupported
    or CheckpointWriteError; on any failure nothing is committed, staging is
    removed, earlier generations are untouched, and the open blend is unchanged.
    """
    eng = _eng()
    folder = os.path.abspath(os.fspath(folder))
    entry = _json_copy(resume_entry, "done entry")
    if not isinstance(entry, dict) or entry.get("status") != "OK":
        raise CheckpointError("ENTRY_INVALID", "only an OK done entry can be checkpointed")
    missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in entry]
    if missing:
        raise CheckpointError("ENTRY_INVALID", "done entry lacks %s" % ", ".join(missing))
    if entry["resume_schema"] != eng.DONE_ENTRY_SCHEMA:
        raise CheckpointError(
            "ENTRY_INVALID", "done entry schema %r is not the current %r"
            % (entry["resume_schema"], eng.DONE_ENTRY_SCHEMA))
    if not (entry.get("uv_validation") or {}).get("ok") or \
            not (entry.get("dependency_validation") or {}).get("ok"):
        raise CheckpointError("ENTRY_INVALID", "done entry lacks passing UV/dependency validation")
    bases = _entry_bases(entry)
    if not bases:
        raise CheckpointError("ENTRY_INVALID", "done entry lacks file bases")
    if output_object is None or _text(getattr(output_object, "name", "")) != entry["output_object"]:
        raise CheckpointError(
            "ENTRY_INVALID", "output object %r is not the entry's %r"
            % (_text(getattr(output_object, "name", None)), entry["output_object"]))

    # The entry must describe this live object and these maps right now.
    run_folder = _text(entry["folder"])
    try:
        current_object = eng.output_object_fingerprint(output_object)
    except Exception as exc:
        raise CheckpointError("STALE_ENTRY", "could not fingerprint the output object: %s"
                              % _text(exc))
    if current_object != entry["output_object_fingerprint"]:
        raise CheckpointError("STALE_ENTRY", "output object %r changed after its done entry was "
                              "recorded" % entry["output_object"])
    try:
        current_maps = eng.output_fingerprints(run_folder, bases)
    except Exception as exc:
        raise CheckpointError("STALE_ENTRY", "published maps are unreadable: %s" % _text(exc))
    if current_maps != entry["output_fingerprints"]:
        raise CheckpointError("STALE_ENTRY", "published maps changed after the done entry was "
                              "recorded")

    problems = _capture_problems(output_object)
    if problems:
        raise CheckpointUnsupported("UNSUPPORTED", "; ".join(problems))
    expected = _expected_maps(entry)
    map_paths = {basename: eng.output_path(run_folder, base, semantic)
                 for basename, (base, semantic) in expected.items()}
    live_ids = {path: MAPS_DIR + "/" + basename for basename, path in map_paths.items()}
    live = checkpoint_content_fingerprint(output_object, live_ids)
    basenames = _image_basenames(output_object, map_paths)
    names = _datablock_names(output_object, basenames)

    checkpoint_id = _new_checkpoint_id()
    staging = os.path.join(folder, STAGING_PREFIX + checkpoint_id + STAGING_SUFFIX)
    generation = os.path.join(folder, GENERATION_PREFIX + checkpoint_id)
    manifest_path = os.path.join(generation, MANIFEST_NAME)
    if os.name == "nt":
        # Blender and Python both stop at MAX_PATH here unless long paths are
        # enabled system-wide; refuse up front instead of failing mid-write.
        longest = max([len(os.path.join(root, MAPS_DIR, basename))
                       for root in (staging, generation) for basename in expected]
                      + [len(os.path.join(staging, PAYLOAD_NAME)),
                         len(os.path.join(generation, MANIFEST_PARTIAL))])
        if longest > WINDOWS_MAX_PATH:
            raise CheckpointWriteError(
                "PATH_TOO_LONG", "checkpoint paths would reach %d characters (Windows limit %d "
                "without long-path support); choose a shorter Output Root or object name"
                % (longest, WINDOWS_MAX_PATH), {"longest": longest})

    action = "create checkpoint store"
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise _write_error(exc, action)
    mesh = output_object.data
    needed = sum(int(item["size"]) for item in entry["output_fingerprints"].values()) \
        + PAYLOAD_SLACK_BYTES + PAYLOAD_BYTES_PER_ELEMENT * (
            len(mesh.vertices) + len(mesh.edges) + len(mesh.loops) + len(mesh.polygons))
    free = _disk_free(folder)
    if free is not None and free < needed:
        raise CheckpointWriteError(
            "DISK_FULL", "checkpoint needs about %d bytes but only %d are free in %s"
            % (needed, free, folder), {"needed": needed, "free": free})
    _phase("precheck_ok")

    renamed = committed = False
    try:
        action = "create staging directory"
        os.makedirs(os.path.join(staging, MAPS_DIR))
        _phase("staging_created", staging)
        artifacts: List[Dict[str, Any]] = []
        for basename, (base, semantic) in expected.items():
            relative = MAPS_DIR + "/" + basename
            destination = os.path.join(staging, MAPS_DIR, basename)
            action = "copy map " + basename
            size, digest = _copy_verified(map_paths[basename], destination)
            recorded = entry["output_fingerprints"].get(basename) or {}
            if digest != recorded.get("sha256") or size != recorded.get("size"):
                raise CheckpointError("STALE_ENTRY", "map %s changed while it was being "
                                      "checkpointed" % basename)
            try:
                width, height = eng.verify_png_file(destination)
            except Exception as exc:
                raise CheckpointWriteError("VERIFY_FAILED", "copied map %s does not decode: %s"
                                           % (basename, _text(exc)))
            artifacts.append({"id": relative, "path": relative, "role": "map", "size": size,
                              "sha256": digest, "file_base": base, "semantic": semantic,
                              "width": int(width), "height": int(height)})
            _phase("artifact_written:" + relative, destination)

        action = "write payload library"
        payload = os.path.join(staging, PAYLOAD_NAME)
        written = _write_payload(output_object, payload, basenames)
        _fsync_file(payload)
        with open(payload, "rb") as handle:
            if handle.read(7) != b"BLENDER":
                raise CheckpointWriteError("VERIFY_FAILED", "payload is not an uncompressed .blend")
        size, digest = _sha256_file(payload)
        artifacts.append({"id": PAYLOAD_NAME, "path": PAYLOAD_NAME, "role": "payload",
                          "size": size, "sha256": digest})
        _phase("artifact_written:" + PAYLOAD_NAME, payload)

        if verify_roundtrip:
            action = "verify payload round trip"
            staged_maps = os.path.join(staging, MAPS_DIR)
            staged_ids = {os.path.join(staged_maps, basename): MAPS_DIR + "/" + basename
                          for basename in expected}
            try:
                appended = _append_payload(payload, written["object"], staged_maps,
                                           set(expected))
            except CheckpointRejected as exc:
                raise CheckpointWriteError(
                    "ROUNDTRIP_MISMATCH", "payload could not be reloaded for verification: %s"
                    % exc.message, {"components": [], "reload_code": exc.code}) from exc
            try:
                # Evaluated like a restore would evaluate it, in a temporary
                # collection that discard() removes again.
                appended.stage(VALIDATION_COLLECTION)
                roundtrip = checkpoint_content_fingerprint(appended.obj, staged_ids)
            finally:
                appended.discard()
            if roundtrip["sha256"] != live["sha256"]:
                differing = sorted(key for key in live["components"]
                                   if live["components"][key] != roundtrip["components"].get(key))
                raise CheckpointWriteError(
                    "ROUNDTRIP_MISMATCH", "payload does not restore to the live output's content "
                    "(%s)" % ", ".join(differing), {"components": differing})
            _phase("roundtrip_verified")

        action = "publish generation directory"
        _fsync_dir(os.path.join(staging, MAPS_DIR))
        _fsync_dir(staging)
        _replace(staging, generation)
        renamed = True
        _fsync_dir(folder)
        _phase("generation_renamed", generation)

        action = "write manifest"
        body = {
            "schema_version": CHECKPOINT_SCHEMA,
            "kind": KIND,
            "checkpoint_id": checkpoint_id,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "writer": {
                "tool_version": _text(getattr(eng, "TOOL_VERSION", "")),
                "engine_md5": _text(eng.tool_file_md5()),
                "blender": bpy.app.version_string,
                "platform": sys.platform,
            },
            "source_object": None if source_name is None else _text(source_name),
            "source_fingerprint": entry["source_fingerprint"],
            "config_fingerprint": entry["config_fingerprint"],
            "output_fingerprint": entry["output_object_fingerprint"],
            "output_fingerprints": entry["output_fingerprints"],
            "content_fingerprint": live,
            "payload": {"path": PAYLOAD_NAME, "object": written["object"], "names": names},
            "artifacts": artifacts,
            "resume_entry": entry,
            "normalization": NORMALIZATION,
            "compatibility": {
                "checkpoint_schema": CHECKPOINT_SCHEMA,
                "content_schema": CONTENT_SCHEMA,
                "done_entry_schema": eng.DONE_ENTRY_SCHEMA,
                "done_list_schema": eng.DONE_LIST_SCHEMA,
                "manifest_schema": eng.MANIFEST_SCHEMA,
                "blender_version": [int(part) for part in bpy.app.version],
                "blender_file_version": [int(part) for part in bpy.app.version_file],
            },
            "durability": {
                "files_fsynced": True, "directories_fsynced": os.name != "nt",
                "commit": "manifest written last, os.replace() within the generation directory",
                "network_filesystems": "untested",
            },
        }
        body["manifest_sha256"] = _manifest_digest(body)
        raw = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        partial = os.path.join(generation, MANIFEST_PARTIAL)
        _write_stream(partial, [raw])
        _phase("manifest_tmp_written", partial)
        action = "commit manifest"
        _replace(partial, manifest_path)
        committed = True
        _fsync_dir(generation)
        _phase("manifest_committed", manifest_path)
    except BaseException as exc:
        if not committed:
            _discard_dir(generation if renamed else staging)
        if isinstance(exc, CheckpointError) or not isinstance(exc, Exception):
            raise
        raise _write_error(exc, action) from exc

    try:
        inspect_checkpoint(manifest_path)
    except CheckpointError as exc:
        # Withdraw the commit marker: an unverifiable generation must not count.
        try:
            _replace(manifest_path, manifest_path + ".rejected")
        except OSError:
            pass
        raise CheckpointWriteError("VERIFY_FAILED", "committed generation failed verification: %s"
                                   % exc.message) from exc
    _phase("post_commit_verified", manifest_path)
    _size, manifest_sha = _sha256_file(manifest_path)
    return {
        "schema_version": CHECKPOINT_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "manifest": manifest_path,
        "store_relative": _store_relative(manifest_path),
        "manifest_sha256": manifest_sha,
        "generation": generation,
        "store": os.path.abspath(folder),
        "content_sha256": live["sha256"],
        "artifacts": len(artifacts),
    }


# --------------------------------------------------------------- inspect

def _reject(code: str, message: str, **detail: Any) -> CheckpointRejected:
    return CheckpointRejected(code, message, detail)


def _parse_manifest(raw: bytes) -> Any:
    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key %r" % key)
            result[key] = value
        return result

    def constant(name: str) -> Any:
        raise ValueError("non-finite number %s" % name)

    try:
        text = raw.decode("utf-8")
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _reject("MANIFEST_MALFORMED", "manifest is not valid JSON: %s" % _text(exc))


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64_RE.match(value) is not None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, field: str, what: str) -> None:
    if not condition:
        raise _reject("MANIFEST_MALFORMED", "%s must be %s" % (field, what), field=field)


def _validate_structure(body: Dict[str, Any]) -> None:
    _require(isinstance(body.get("checkpoint_id"), str)
             and _ID_RE.match(body["checkpoint_id"]) is not None, "checkpoint_id", "a checkpoint id")
    _require(isinstance(body.get("created_utc"), str), "created_utc", "a string")
    for key in ("source_fingerprint", "config_fingerprint", "output_fingerprint"):
        _require(_is_hex64(body.get(key)), key, "a SHA-256 hex digest")
    outputs = body.get("output_fingerprints")
    _require(isinstance(outputs, dict) and bool(outputs), "output_fingerprints", "a mapping")
    for name, value in outputs.items():
        _require(isinstance(value, dict) and _is_hex64(value.get("sha256"))
                 and _is_int(value.get("size")), "output_fingerprints[%s]" % name,
                 "a sha256/size record")
    content = body.get("content_fingerprint")
    _require(isinstance(content, dict) and _is_hex64(content.get("sha256"))
             and isinstance(content.get("components"), dict)
             and _is_int(content.get("schema_version")), "content_fingerprint",
             "a content fingerprint record")
    payload = body.get("payload")
    _require(isinstance(payload, dict) and isinstance(payload.get("path"), str)
             and isinstance(payload.get("object"), str) and bool(payload.get("object"))
             and isinstance(payload.get("names"), dict), "payload", "a payload record")
    artifacts = body.get("artifacts")
    _require(isinstance(artifacts, list) and bool(artifacts), "artifacts", "a non-empty list")
    ids: Set[str] = set()
    for index, artifact in enumerate(artifacts):
        field = "artifacts[%d]" % index
        _require(isinstance(artifact, dict), field, "an object")
        _require(isinstance(artifact.get("id"), str) and bool(artifact["id"]), field + ".id",
                 "a non-empty string")
        _require(isinstance(artifact.get("path"), str), field + ".path", "a string")
        _require(isinstance(artifact.get("role"), str), field + ".role", "a string")
        _require(_is_int(artifact.get("size")) and artifact["size"] >= 0, field + ".size",
                 "a non-negative integer")
        _require(_is_hex64(artifact.get("sha256")), field + ".sha256", "a SHA-256 hex digest")
        _require(artifact["id"] not in ids, field + ".id", "unique")
        ids.add(artifact["id"])
    entry = body.get("resume_entry")
    _require(isinstance(entry, dict), "resume_entry", "an object")
    _require(entry.get("status") == "OK", "resume_entry.status", '"OK"')
    _require(isinstance(entry.get("route"), str), "resume_entry.route", "a string")
    _require(isinstance(entry.get("output_object"), str), "resume_entry.output_object", "a string")
    bases = entry.get("file_bases") or ([entry.get("file_base")] if entry.get("file_base") else [])
    _require(isinstance(bases, list) and bool(bases)
             and all(isinstance(item, str) and item for item in bases),
             "resume_entry.file_bases", "a non-empty list of names")
    normalization = body.get("normalization")
    _require(isinstance(normalization, dict) and _is_int(normalization.get("version")),
             "normalization", "a normalization record")
    compatibility = body.get("compatibility")
    _require(isinstance(compatibility, dict) and _is_int(compatibility.get("done_entry_schema"))
             and _is_version(compatibility.get("blender_version"))
             and _is_version(compatibility.get("blender_file_version")),
             "compatibility", "a compatibility record with the writer's Blender version")


def _is_version(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 3
            and all(_is_int(part) and part >= 0 for part in value))


def _safe_parts(relative: Any) -> List[str]:
    """Split a manifest path; refuse anything that is not a plain relative path."""
    def escape(why: str) -> CheckpointRejected:
        return _reject("PATH_ESCAPE", "artifact path %r is not allowed: %s" % (relative, why),
                       path=_text(relative))
    if not isinstance(relative, str) or not relative:
        raise escape("empty")
    if "\x00" in relative or any(ord(char) < 32 for char in relative):
        raise escape("control character")
    if "\\" in relative:
        raise escape("backslash separator")
    if relative.startswith("/"):
        raise escape("absolute path")
    if ":" in relative:
        raise escape("drive letter or alternate data stream")
    parts = relative.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise escape("empty, current or parent directory segment")
        if part != part.strip() or part.endswith("."):
            raise escape("leading/trailing space or trailing dot")
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            raise escape("reserved Windows device name")
    return parts


def _confined_file(root: str, real_root: str, parts: List[str]) -> str:
    full = os.path.join(root, *parts)
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        raise _reject("ARTIFACT_MISSING", "artifact %s is missing" % "/".join(parts),
                      path="/".join(parts))
    except OSError as exc:
        raise _reject("ARTIFACT_MISSING", "artifact %s is unreadable: %s"
                      % ("/".join(parts), _text(exc)), path="/".join(parts))
    if stat.S_ISLNK(info.st_mode):
        raise _reject("PATH_ESCAPE", "artifact %s is a symbolic link" % "/".join(parts),
                      path="/".join(parts))
    real = os.path.realpath(full)
    prefix = os.path.normcase(real_root.rstrip(os.sep) + os.sep)
    if not os.path.normcase(real).startswith(prefix):
        raise _reject("PATH_ESCAPE", "artifact %s resolves outside the generation (%s)"
                      % ("/".join(parts), real), path="/".join(parts))
    if not stat.S_ISREG(os.stat(real).st_mode):
        raise _reject("PATH_ESCAPE", "artifact %s is not a regular file" % "/".join(parts),
                      path="/".join(parts))
    return full


def _read_sealed_manifest(manifest_path: str) -> Tuple[str, str, Dict[str, Any]]:
    """(manifest path, generation dir, body): the manifest file itself verified --
    regular file, size, sealed digest, schema and structure -- without hashing
    any artifact.  Raises CheckpointRejected."""
    path = os.path.abspath(os.fspath(manifest_path))
    if os.path.basename(path) != MANIFEST_NAME:
        raise _reject("INCOMPLETE_GENERATION", "%s is not a committed manifest (expected %s)"
                      % (os.path.basename(path), MANIFEST_NAME))
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _reject("MANIFEST_MISSING", "no committed manifest at %s (generation incomplete, "
                      "moved or deleted)" % path)
    except OSError as exc:
        raise _reject("MANIFEST_MISSING", "manifest is unreadable: %s" % _text(exc))
    if stat.S_ISLNK(info.st_mode):
        raise _reject("PATH_ESCAPE", "manifest is a symbolic link")
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
        raise _reject("MANIFEST_MALFORMED", "manifest is not a regular file under %d bytes"
                      % MAX_MANIFEST_BYTES)
    with open(path, "rb") as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    body = _parse_manifest(raw)
    if not isinstance(body, dict):
        raise _reject("MANIFEST_MALFORMED", "manifest is not a JSON object")
    version = body.get("schema_version")
    if not _is_int(version):
        raise _reject("MANIFEST_MALFORMED", "schema_version must be an integer, not %r" % (version,))
    if version != CHECKPOINT_SCHEMA:
        raise _reject("SCHEMA_INCOMPATIBLE", "checkpoint schema %d is not supported by this build "
                      "(supports %d)" % (version, CHECKPOINT_SCHEMA))
    if body.get("kind") != KIND:
        raise _reject("MANIFEST_MALFORMED", "manifest kind is %r" % (body.get("kind"),))
    if not _is_hex64(body.get("manifest_sha256")):
        raise _reject("MANIFEST_MALFORMED", "manifest_sha256 is missing")
    try:
        sealed = _manifest_digest(body)
    except ValueError as exc:
        raise _reject("MANIFEST_MALFORMED", "manifest cannot be canonicalized: %s" % _text(exc))
    if sealed != body["manifest_sha256"]:
        raise _reject("MANIFEST_INTEGRITY", "manifest content does not match its recorded digest")
    _validate_structure(body)
    root = os.path.dirname(path)
    if os.path.basename(root) != GENERATION_PREFIX + body["checkpoint_id"]:
        raise _reject("MANIFEST_MALFORMED", "manifest %s does not belong to directory %s"
                      % (body["checkpoint_id"], os.path.basename(root)))
    if body["normalization"]["version"] != NORMALIZATION["version"] or \
            body["content_fingerprint"]["schema_version"] != CONTENT_SCHEMA:
        raise _reject("SCHEMA_INCOMPATIBLE", "content normalization %r/%r is not this build's %r/%r"
                      % (body["normalization"]["version"],
                         body["content_fingerprint"]["schema_version"],
                         NORMALIZATION["version"], CONTENT_SCHEMA))
    return path, root, body


def inspect_checkpoint(manifest_path: str) -> Dict[str, Any]:
    """Read/verify bytes and schema without importing Blender datablocks.

    Raises CheckpointRejected (a ValueError) with a stable ``code``.
    """
    path, root, body = _read_sealed_manifest(manifest_path)
    real_root = os.path.realpath(root)
    resolved: Dict[str, Dict[str, Any]] = {}
    seen_paths: Set[str] = set()
    for artifact in body["artifacts"]:
        parts = _safe_parts(artifact["path"])
        folded = "/".join(parts).lower()
        if folded in seen_paths:
            raise _reject("MANIFEST_MALFORMED", "artifact path %s is listed twice" % artifact["path"])
        seen_paths.add(folded)
        full = _confined_file(root, real_root, parts)
        size, digest = _sha256_file(full)
        if size != artifact["size"]:
            raise _reject("ARTIFACT_SIZE", "artifact %s is %d bytes, manifest says %d"
                          % (artifact["path"], size, artifact["size"]), path=artifact["path"])
        if digest != artifact["sha256"]:
            raise _reject("ARTIFACT_HASH", "artifact %s content does not match its SHA-256"
                          % artifact["path"], path=artifact["path"])
        resolved[artifact["id"]] = dict(artifact, absolute=full)

    payloads = [item for item in resolved.values() if item["role"] == "payload"]
    maps = [item for item in resolved.values() if item["role"] == "map"]
    others = [item["id"] for item in resolved.values() if item["role"] not in ("payload", "map")]
    if others:
        raise _reject("PAYLOAD_UNSAFE", "unknown artifact roles: %s" % ", ".join(others))
    if len(payloads) != 1 or payloads[0]["path"] != PAYLOAD_NAME \
            or body["payload"]["path"] != PAYLOAD_NAME:
        raise _reject("PAYLOAD_INCOMPLETE", "generation must hold exactly one %s" % PAYLOAD_NAME)
    with open(payloads[0]["absolute"], "rb") as handle:
        if handle.read(7) != b"BLENDER":
            raise _reject("PAYLOAD_UNSAFE", "payload is not an uncompressed .blend file")
    expected = _expected_maps(body["resume_entry"])
    by_name: Dict[str, Dict[str, Any]] = {}
    for item in maps:
        parts = item["path"].split("/")
        if len(parts) != 2 or parts[0] != MAPS_DIR or item["id"] != item["path"]:
            raise _reject("PAYLOAD_UNSAFE", "map artifact %s is not under %s/" % (item["path"], MAPS_DIR))
        by_name[parts[1]] = item
    missing = sorted(set(expected) - set(by_name))
    extra = sorted(set(by_name) - set(expected))
    if missing:
        raise _reject("PAYLOAD_INCOMPLETE", "required maps are missing from the generation: %s"
                      % ", ".join(missing))
    if extra:
        raise _reject("PAYLOAD_UNSAFE", "unexpected maps in the generation: %s" % ", ".join(extra))
    for basename, item in sorted(by_name.items()):
        recorded = body["output_fingerprints"].get(basename)
        if not recorded or recorded.get("sha256") != item["sha256"] \
                or recorded.get("size") != item["size"]:
            raise _reject("CONTENT_MISMATCH", "map %s differs from the published output recorded "
                          "for this part" % basename, components=["maps"])
    if set(body["output_fingerprints"]) != set(expected):
        raise _reject("PAYLOAD_INCOMPLETE", "recorded output fingerprints do not cover exactly the "
                      "part's maps")
    _size, manifest_sha = _sha256_file(path)
    return {
        "ok": True,
        "manifest_path": path,
        "manifest_sha256": manifest_sha,
        "generation": root,
        "checkpoint_id": body["checkpoint_id"],
        "manifest": body,
        "payload": payloads[0]["absolute"],
        "maps_dir": os.path.join(root, MAPS_DIR),
        "maps": {basename: item["absolute"] for basename, item in by_name.items()},
        "artifacts": {ident: {key: item[key] for key in ("path", "role", "size", "sha256",
                                                          "absolute")}
                      for ident, item in resolved.items()},
    }


# --------------------------------------------------------------- restore

def _restore_names(appended: _Appended, names: Dict[str, Any]) -> List[Dict[str, str]]:
    """Give restored datablocks their recorded names where those are free."""
    renamed: List[Dict[str, str]] = []
    new = {item.as_pointer() for item in appended.new_ids}

    def rename(item: Any, collection: Any, desired: Any, kind: str) -> None:
        if item is None or item.as_pointer() not in new or not isinstance(desired, str) \
                or not desired or item.name == desired:
            return
        if collection.get(desired) is None:
            item.name = desired
        if item.name != desired:
            renamed.append({"type": kind, "desired": desired, "actual": _text(item.name)})

    obj = appended.obj
    rename(obj, bpy.data.objects, names.get("object"), "object")
    rename(obj.data, bpy.data.meshes, names.get("mesh"), "mesh")
    materials = names.get("materials") or []
    done: Set[int] = set()
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        if material is None or material.as_pointer() in done or index >= len(materials):
            continue
        done.add(material.as_pointer())
        rename(material, bpy.data.materials, materials[index], "material")
    images = names.get("images") or {}
    for item in appended.new_ids:
        if item.id_type == "IMAGE":
            basename = os.path.basename(_text(item.filepath))
            rename(item, bpy.data.images, images.get(basename), "image")
    return renamed


def restore_checkpoint(manifest_path: str, source: Any, source_index: Any = None, *,
                       collection: Any = None,
                       capability_cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Restore into a temporary collection; validate; then expose the result.

    ``source`` is the CURRENT source object; ``source_index`` the run's source
    index (built here when None, before anything is appended).  ``collection``
    receives the restored object; by default a new collection is created.
    Returns plain JSON data: the restored object name, a regenerated done entry
    that already passed the engine's validate_done_entry(), the writer/reader
    Blender versions ("blender") and a handle for discard_restored().  Raises
    CheckpointRejected (BLENDER_CHANGED for another Blender release or file
    format); on failure every datablock this call created is removed again.
    ``capability_cache`` is the run's texture-validity cache (see
    material_capabilities.analyze_objects), shared with the engine's checks.

    Cheap rejections come first (M1 final review finding 5): the sealed
    manifest alone decides schema, Blender, source, dependency, capability
    policy, configuration and source-fingerprint refusals.  Every artifact is
    then hashed (inspect_checkpoint) before anything is loaded, and the
    manifest must not have changed in between.
    """
    eng = _eng()
    _path, _root, manifest = _read_sealed_manifest(manifest_path)
    recorded = manifest["resume_entry"]
    if manifest["compatibility"]["done_entry_schema"] != eng.DONE_ENTRY_SCHEMA:
        raise _reject("SCHEMA_INCOMPATIBLE", "checkpoint was written for done-entry schema %r; "
                      "this engine uses %r" % (manifest["compatibility"]["done_entry_schema"],
                                                eng.DONE_ENTRY_SCHEMA))
    blender = _blender_compatibility(manifest["compatibility"])
    if source is None or getattr(source, "type", None) != "MESH":
        raise _reject("SOURCE_UNAVAILABLE", "the current source object is not a mesh")
    try:
        eng.require_material_dependencies(eng.used_materials([source]))
    except Exception as exc:
        raise _reject("DEPENDENCY_BLOCKED", _text(exc))
    # The policy validate_done_entry() applies at the end, decided up front.
    try:
        capability = eng.capability_decision(source, capability_cache)
    except Exception as exc:
        raise _reject("CAPABILITY_REFUSED", "could not analyze material capability: %s"
                      % _text(exc))
    if capability is None or not capability.get("allowed"):
        raise _reject("CAPABILITY_REFUSED", "material capability %s under the current policy"
                      % (capability or {}).get("outcome", "UNKNOWN"))
    try:
        current_config = eng.config_fingerprint()
    except Exception as exc:
        raise _reject("CONFIG_CHANGED", "could not fingerprint the run configuration: %s"
                      % _text(exc))
    if current_config != manifest["config_fingerprint"]:
        raise _reject("CONFIG_CHANGED", "run configuration (engine build, resolution, routing, "
                      "bake or discovery settings) differs from the checkpoint's")
    if source_index is None:
        try:
            source_index = eng.build_source_index()
        except Exception as exc:
            raise _reject("SOURCE_UNAVAILABLE", "could not build the source index: %s"
                          % _text(exc))
    try:
        current_source = eng.source_fingerprint(source, source_index)
    except Exception as exc:
        raise _reject("SOURCE_CHANGED", "could not fingerprint the current source: %s" % _text(exc))
    if current_source != manifest["source_fingerprint"]:
        raise _reject("SOURCE_CHANGED", "source geometry, UVs, materials, images or modifiers "
                      "changed since the checkpoint was written")

    # Only now: every artifact's size and hash, before any payload is loaded.
    info = inspect_checkpoint(manifest_path)
    if info["manifest"] != manifest:
        raise _reject("MANIFEST_INTEGRITY", "manifest changed while it was being restored")
    artifact_ids = {path: MAPS_DIR + "/" + basename for basename, path in info["maps"].items()}
    appended = _append_payload(info["payload"], manifest["payload"]["object"], info["maps_dir"],
                               set(info["maps"]))
    target_created = None
    try:
        obj = appended.obj
        expected = manifest["content_fingerprint"]
        # Stage 1, before Blender evaluates anything in the payload: every
        # static component (settings, data, materials, map bytes) must match.
        try:
            static = checkpoint_content_fingerprint(obj, artifact_ids, evaluated=False)
        except CheckpointUnsupported as exc:
            raise _reject("PAYLOAD_UNSAFE", exc.message)
        keys = (set(expected["components"]) | set(static["components"])) - {"evaluated"}
        differing = sorted(key for key in keys
                           if expected["components"].get(key) != static["components"].get(key))
        if differing:
            raise _reject("CONTENT_MISMATCH", "restored content differs from the checkpoint's "
                          "canonical content in: %s" % ", ".join(differing), components=differing)
        # Stage 2: evaluate in a temporary validation collection (this also
        # gives the object the matrix_world the engine fingerprint needs); the
        # evaluated mesh must equal the one the maps were baked for.
        validating = appended.stage(VALIDATION_COLLECTION)
        try:
            restored = checkpoint_content_fingerprint(obj, artifact_ids)
        except CheckpointUnsupported as exc:
            raise _reject("PAYLOAD_UNSAFE", exc.message)
        if restored["sha256"] != expected["sha256"]:
            keys = set(expected["components"]) | set(restored["components"])
            differing = sorted(key for key in keys
                               if expected["components"].get(key) != restored["components"].get(key))
            raise _reject("CONTENT_MISMATCH", "restored content differs from the checkpoint's "
                          "canonical content in: %s" % (", ".join(differing) or "digest"),
                          components=differing)
        renamed = _restore_names(appended, manifest["payload"]["names"])
        bpy.context.view_layer.update()
        try:
            eng.require_material_dependencies(eng.used_materials([obj]))
        except Exception as exc:
            raise _reject("DEPENDENCY_BLOCKED", "restored output: %s" % _text(exc))
        route = _text(recorded.get("route", ""))
        try:
            uv_validation = eng.require_output_uv(obj, route)
        except Exception as exc:
            raise _reject("UV_INVALID", _text(exc))
        bases = _entry_bases(recorded)
        maps_now = eng.output_fingerprints(info["maps_dir"], bases)
        if maps_now != manifest["output_fingerprints"]:
            raise _reject("CONTENT_MISMATCH", "checkpoint maps differ from the recorded outputs",
                          components=["maps"])

        # Only now: runtime metadata for the accepted names and paths.
        entry = copy.deepcopy(recorded)
        entry["output_object"] = _text(obj.name)
        entry["folder"] = info["maps_dir"]
        entry["output_fingerprints"] = maps_now
        entry["output_object_fingerprint"] = eng.output_object_fingerprint(obj)
        entry["uv_validation"] = uv_validation
        entry["dependency_validation"] = eng.require_material_dependencies(
            eng.used_materials([source]))
        entry["checkpoint"] = {
            "schema_version": CHECKPOINT_SCHEMA, "checkpoint_id": manifest["checkpoint_id"],
            "manifest": info["manifest_path"],
            "store_relative": _store_relative(info["manifest_path"]),
            "manifest_sha256": info["manifest_sha256"],
        }
        entry["restored_from_checkpoint"] = {
            "checkpoint_id": manifest["checkpoint_id"], "manifest": info["manifest_path"],
            "content_sha256": restored["sha256"],
            "restored_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "renamed": renamed,
        }
        ok, reason = eng.validate_done_entry(entry, source, source_index,
                                             capability_cache=capability_cache)
        if not ok:
            raise _reject("DONE_ENTRY_INVALID", "engine resume validation refused the restored "
                          "part: %s" % reason)

        target = collection
        if target is None:
            target = bpy.data.collections.new(RESTORE_COLLECTION)
            appended.new_ids.append(target)
            target_created = target
            bpy.context.scene.collection.children.link(target)
        target.objects.link(obj)
        validating.objects.unlink(obj)
        ok, reason = eng.validate_done_entry(entry, source, source_index,
                                             capability_cache=capability_cache)
        if not ok:
            raise _reject("DONE_ENTRY_INVALID", "engine resume validation refused the exposed "
                          "part: %s" % reason)
        appended.new_ids.remove(validating)
        bpy.data.collections.remove(validating)
        libraries = [item for item in appended.new_ids if item.id_type == "LIBRARY"]
        if libraries:
            users = bpy.data.user_map(subset=libraries)
            _remove_ids([item for item in libraries if not users.get(item)])
        created: Dict[str, List[str]] = {}
        handle: List[Dict[str, Any]] = []
        for item in appended.new_ids:
            try:
                if item.id_type != "LIBRARY":
                    created.setdefault(item.id_type.lower(), []).append(_text(item.name))
                    handle.append({"type": item.id_type, "name": _text(item.name),
                                   "session_uid": int(item.session_uid)})
            except ReferenceError:
                continue
    except BaseException as exc:
        appended.discard()
        if isinstance(exc, CheckpointError) or not isinstance(exc, Exception):
            raise
        raise _reject("RESTORE_FAILED", "%s: %s" % (type(exc).__name__, _text(exc))) from exc
    # Plain JSON data only (REVIEW_B finding 9): callers may log or serialize it.
    return {
        "ok": True,
        "checkpoint_id": manifest["checkpoint_id"],
        "manifest": info["manifest_path"],
        "object": _text(obj.name),
        "collection": _text(target.name),
        "entry": entry,
        "renamed": renamed,
        "created": created,
        "content_sha256": restored["sha256"],
        "blender": blender,
        # What discard_restored() may remove: these IDs in THIS process only.
        "datablocks": {"process": _PROCESS_TOKEN, "ids": handle},
    }


def _blender_compatibility(compatibility: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse a payload another Blender release or file format wrote.

    Loading a .blend from a different file-format version runs Blender's
    versioning code over it, which can rewrite state the content fingerprint
    does not see.  A different patch release of the same major.minor and file
    format is accepted, and reported."""
    writer = [int(part) for part in compatibility["blender_version"]]
    writer_file = [int(part) for part in compatibility["blender_file_version"]]
    reader = [int(part) for part in bpy.app.version]
    reader_file = [int(part) for part in bpy.app.version_file]
    described = {
        "writer": "%d.%d.%d" % tuple(writer), "reader": "%d.%d.%d" % tuple(reader),
        "writer_file_version": "%d.%d.%d" % tuple(writer_file),
        "reader_file_version": "%d.%d.%d" % tuple(reader_file),
    }
    if writer_file != reader_file or writer[:2] != reader[:2]:
        raise _reject("BLENDER_CHANGED", "checkpoint was written by Blender %s (file format %s); "
                      "this is Blender %s (file format %s), and loading its payload would run "
                      "Blender's file versioning" % (described["writer"],
                                                     described["writer_file_version"],
                                                     described["reader"],
                                                     described["reader_file_version"]),
                      **described)
    described["patch_differs"] = writer != reader
    return described


def discard_restored(result: Dict[str, Any]) -> List[str]:
    """Remove exactly the datablocks one successful restore created.

    For a caller that restored a part but then could not use it (for example
    the engine failed to materialize its maps into the run folder).  ``result``
    may be the restore result itself or its JSON round trip.  Returns removal
    failures; unrelated datablocks are never touched, and a handle from another
    Blender process removes nothing.  A second call is a no-op.
    """
    handle = result.pop("datablocks", None) if isinstance(result, dict) else None
    if not isinstance(handle, dict) or not isinstance(handle.get("ids"), list):
        return []
    if handle.get("process") != _PROCESS_TOKEN:
        return ["restore handle belongs to another Blender process; nothing was removed"]
    wanted = {(row.get("type"), row.get("session_uid")) for row in handle["ids"]
              if isinstance(row, dict) and isinstance(row.get("type"), str)
              and _is_int(row.get("session_uid"))}
    items = [item for item in bpy.data.all_ids
             if not getattr(item, "is_embedded_data", False)
             and (item.id_type, int(item.session_uid)) in wanted]
    return _remove_ids(items)


# ------------------------------------------------- generations and entries

def list_generations(folder: str) -> List[Dict[str, Any]]:
    """Every generation in a store, newest first.

    status COMMITTED means a manifest.json exists (not yet verified: use
    inspect_checkpoint); INCOMPLETE means a published directory without a
    manifest; STAGING means an unfinished private write.
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    rows = []
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if name.startswith(STAGING_PREFIX) and name.endswith(STAGING_SUFFIX):
            checkpoint_id = name[len(STAGING_PREFIX):-len(STAGING_SUFFIX)]
            status = "STAGING"
        elif name.startswith(GENERATION_PREFIX):
            checkpoint_id = name[len(GENERATION_PREFIX):]
            status = ("COMMITTED" if os.path.isfile(os.path.join(path, MANIFEST_NAME))
                      else "INCOMPLETE")
        else:
            continue
        if _ID_RE.match(checkpoint_id) is None:
            continue
        rows.append({"checkpoint_id": checkpoint_id, "path": path, "status": status,
                     "manifest": os.path.join(path, MANIFEST_NAME) if status == "COMMITTED"
                     else None})
    rows.sort(key=lambda row: row["checkpoint_id"], reverse=True)
    return rows


def latest_committed(folder: str, verify: bool = True) -> Optional[str]:
    """Manifest path of the newest committed generation that verifies."""
    for row in list_generations(folder):
        if row["status"] != "COMMITTED":
            continue
        if not verify:
            return row["manifest"]
        try:
            inspect_checkpoint(row["manifest"])
        except CheckpointError:
            continue
        return row["manifest"]
    return None


def discard_incomplete_generations(folder: str) -> List[str]:
    """Remove unfinished generations (never a committed one).  Returns paths."""
    removed = []
    for row in list_generations(folder):
        if row["status"] in ("STAGING", "INCOMPLETE"):
            _discard_dir(row["path"])
            if not os.path.exists(row["path"]):
                removed.append(row["path"])
    return removed


def _reference_parts(relative: Any) -> Optional[List[str]]:
    """["<object store>", "g_<id>", "manifest.json"], or None if malformed."""
    try:
        parts = _safe_parts(relative)
    except CheckpointRejected:
        return None
    if len(parts) != 3 or parts[2] != MANIFEST_NAME or not parts[1].startswith(GENERATION_PREFIX) \
            or _ID_RE.match(parts[1][len(GENERATION_PREFIX):]) is None:
        return None
    return parts


def checkpoint_reference(entry: Any, store_root: Optional[str] = None
                         ) -> Tuple[Optional[str], str, str]:
    """(manifest path, code, reason) for a done entry's checkpoint reference.

    With ``store_root`` (the current run's checkpoint_store_root()), the
    store-relative path is tried first, so a moved output root still resolves;
    the recorded absolute path is the fallback.  Either way the manifest must
    hash to the recorded manifest_sha256.
    """
    reference = entry.get("checkpoint") if isinstance(entry, dict) else None
    if reference is None:
        return None, "NO_CHECKPOINT", (
            "done entry predates durable checkpoints (no checkpoint reference); it can resume "
            "only while its output object is still in the open blend, otherwise it is rebaked")
    if not isinstance(reference, dict) or not isinstance(reference.get("manifest"), str) \
            or not isinstance(reference.get("manifest_sha256"), str):
        return None, "CHECKPOINT_MALFORMED", "done entry's checkpoint reference is malformed"
    candidates: List[str] = []
    if "store_relative" in reference:
        parts = _reference_parts(reference["store_relative"])
        if parts is None:
            return None, "CHECKPOINT_MALFORMED", (
                "done entry's store-relative checkpoint path %r is not "
                "<object>/g_<id>/manifest.json" % (reference["store_relative"],))
        if store_root:
            candidates.append(os.path.join(os.path.abspath(os.fspath(store_root)), *parts))
    candidates.append(reference["manifest"])
    changed: List[str] = []
    unreadable: List[str] = []
    seen: Set[str] = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isfile(path):
            continue
        seen.add(key)
        try:
            _size, digest = _sha256_file(path)
        except OSError as exc:
            unreadable.append("%s (%s)" % (path, _text(exc)))
            continue
        if digest == reference["manifest_sha256"]:
            return path, "", ""
        changed.append(path)
    if changed:
        return None, "CHECKPOINT_CHANGED", (
            "checkpoint manifest %s is not the one this done entry recorded" % changed[0])
    if unreadable:
        return None, "CHECKPOINT_MISSING", "checkpoint manifest is unreadable: %s" % unreadable[0]
    return None, "CHECKPOINT_MISSING", (
        "checkpoint manifest is missing at %s (moved or deleted); the part will be rebaked"
        % " and at ".join(candidates))


def restore_for_entry(entry: Any, source: Any, source_index: Any = None, *,
                      collection: Any = None, store_root: Optional[str] = None,
                      capability_cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Restore a done entry's checkpoint, or explain exactly why not.

    ``store_root`` is the current run's checkpoint_store_root(run folder); see
    checkpoint_reference().  Never raises CheckpointError: returns
    {"restored": False, "code", "reason"} so the caller can fall back to the
    live-object resume path or a rebake.  Every result is plain JSON data.
    """
    path, code, reason = checkpoint_reference(entry, store_root)
    if path is None:
        return {"restored": False, "code": code, "reason": reason}
    try:
        result = restore_checkpoint(path, source, source_index, collection=collection,
                                    capability_cache=capability_cache)
    except CheckpointError as exc:
        return {"restored": False, "code": exc.code, "reason": _text(exc), "manifest": path}
    result["restored"] = True
    return result


__all__ = [
    "CHECKPOINT_SCHEMA", "CONTENT_SCHEMA", "NORMALIZATION", "STORE_DIR_NAME",
    "CheckpointError", "CheckpointRejected", "CheckpointUnsupported", "CheckpointWriteError",
    "bind_engine", "checkpoint_store_root", "object_store_dir",
    "write_checkpoint", "inspect_checkpoint", "checkpoint_content_fingerprint",
    "restore_checkpoint", "restore_for_entry", "checkpoint_reference", "discard_restored",
    "list_generations", "latest_committed", "discard_incomplete_generations",
]
