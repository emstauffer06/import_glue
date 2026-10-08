"""Private, verified image-buffer capture for saved background snapshots.

Required still-image buffers are copied explicitly, packed on private IDs and
round-tripped before saving. Original IDs are never saved/reloaded/packed. A
capture is usable only after file verification and fresh-open verification.
Multiview and ambiguous sequence buffers fail closed; they are not
silently replaced with on-disk images. Linked resources remain checksum-guarded
external dependencies, which is stated in the manifest rather than called frozen.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import uuid

import bpy
import numpy as np

from . import background_jobs, shader_graph, shader_resources, ocio_capture

SCHEMA = 1
MANIFEST_NAME = "source_capture.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
DEFAULT_LIMITS = {"max_images": 256, "max_image_pixels": 67_108_864,
                  "max_total_pixels": 134_217_728, "max_external_files": 4096,
                  "max_external_bytes": 16 * 1024**3, "max_seconds": 120.0}
_BUSY = False


class CaptureError(RuntimeError):
    def __init__(self, message, manifest=None):
        super().__init__(message)
        self.manifest = manifest


class _Budget:
    def __init__(self, limits):
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        if set(self.limits) != set(DEFAULT_LIMITS):
            raise CaptureError("Unknown source capture limit")
        for key, value in self.limits.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise CaptureError("Capture limits must be finite positive numbers")
            if key != "max_seconds" and type(value) is not int:
                raise CaptureError("Capture counts must be integers")
        self.start = time.monotonic()
        self.pixels = self.files = self.bytes = 0
        self.seen_images = set()

    def check(self):
        if time.monotonic() - self.start > self.limits["max_seconds"]:
            raise CaptureError("Source capture time budget exceeded")

    def image(self, image):
        self.check()
        width, height = tuple(image.size)
        count = width * height
        if width < 1 or height < 1 or count > self.limits["max_image_pixels"]:
            raise CaptureError("Image dimensions unavailable or exceed capture budget: " + image.name)
        if image.as_pointer() in self.seen_images:
            return
        self.seen_images.add(image.as_pointer())
        self.pixels += count
        if self.pixels > self.limits["max_total_pixels"]:
            raise CaptureError("Source capture total pixel budget exceeded")

    def file(self, path):
        self.check()
        size = path.stat().st_size
        self.files += 1
        self.bytes += size
        if self.files > self.limits["max_external_files"] or self.bytes > self.limits["max_external_bytes"]:
            raise CaptureError("Source capture external dependency budget exceeded")


def _sha(path):
    return background_jobs.sha256_file(path)


def _atomic_manifest(path, manifest):
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise CaptureError("Source capture manifest exceeds bounded size")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_manifest(value):
    if isinstance(value, dict):
        manifest = value
    else:
        with Path(value).open("rb") as stream:
            blob = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(blob) > MAX_MANIFEST_BYTES:
            raise CaptureError("Oversized source capture manifest")
        manifest = json.loads(blob, object_pairs_hook=background_jobs._no_duplicates)
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or manifest.get("status") != "CAPTURED":
        raise CaptureError("Source capture is not complete")
    return manifest


def _identity(block):
    library = getattr(block, "library", None)
    return {"name": block.name, "library": str(Path(bpy.path.abspath(library.filepath)).resolve()) if library else ""}


def _pixels(image):
    if image.channels != 4 or len(image.pixels) != image.size[0] * image.size[1] * 4:
        raise CaptureError("Only verified RGBA image buffers can be captured: " + image.name)
    data = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(data)
    if not np.isfinite(data).all():
        raise CaptureError("Non-finite image pixels: " + image.name)
    return data


def _pixel_hash(data):
    return hashlib.sha256(data.astype("<f4", copy=False).tobytes()).hexdigest()


def _packed(image):
    return [{"filepath": entry.filepath,
             "sha256": hashlib.sha256(bytes(entry.packed_file.data)).hexdigest(),
             "bytes": entry.packed_file.size}
            for entry in image.packed_files]


def _image_state(image, *, pixels=False):
    row = {"identity": _identity(image), "source": image.source, "filepath": image.filepath,
           "colorspace": image.colorspace_settings.name, "alpha_mode": image.alpha_mode,
           "dirty": bool(image.is_dirty), "fake_user": bool(image.use_fake_user),
           "packed": _packed(image), "tiles": [tile.number for tile in image.tiles]}
    # A sequence without an evaluated ImageUser can attempt to load frame zero
    # from these properties. Inspect only the resolved private frame's buffer.
    if image.source != "SEQUENCE":
        row.update(is_float=bool(image.is_float), size=list(image.size))
    if pixels:
        row["pixel_sha256"] = _pixel_hash(_pixels(image))
    return row


def _basic_image_state(image):
    # Do not decode unrelated images (including unused movies) just to verify
    # save-copy restored their existing filepath and ID flags.
    return {"identity": _identity(image), "source": image.source, "filepath": image.filepath,
            "fake_user": bool(image.use_fake_user)}


def _check_drivers(block):
    animation = getattr(block, "animation_data", None)
    if animation is not None and len(animation.drivers):
        raise CaptureError("Unverified driver dependency on %s; custom driver namespaces are not captured" % block.name)


def _geometry_guard(objects):
    """Reject history-dependent geometry before requesting any evaluated mesh.

    This is a named exclusion check, not a general geometry equivalence proof.
    Node groups inside active GN modifiers are checked conservatively in full.
    """
    physics = {"CLOTH", "SOFT_BODY", "FLUID", "DYNAMIC_PAINT", "PARTICLE_SYSTEM", "EXPLODE",
               "MESH_CACHE", "MESH_SEQUENCE_CACHE"}
    cache_nodes = {"GeometryNodeSimulationInput", "GeometryNodeSimulationOutput", "GeometryNodeBake"}
    reverse = {}
    for dependency, users in bpy.data.user_map().items():
        for user in users:
            reverse.setdefault(user, []).append(dependency)
    pending, visited, checked_trees = list(objects), set(), set()
    while pending:
        block = pending.pop()
        if block in visited:
            continue
        visited.add(block)
        _check_drivers(block)
        if isinstance(block, bpy.types.Object):
            if block.rigid_body is not None or block.rigid_body_constraint is not None:
                raise CaptureError("Simulation-dependent geometry: rigid-body state is not captured for " + block.name)
            for modifier in block.modifiers:
                if not modifier.show_viewport and not modifier.show_render:
                    continue
                if modifier.type in physics or getattr(modifier, "point_cache", None) is not None:
                    raise CaptureError("Simulation-dependent geometry: %s / %s (%s) has an unverified cache" %
                                       (block.name, modifier.name, modifier.type))
                if modifier.type == "OCEAN" and modifier.is_cached:
                    raise CaptureError("Simulation-dependent geometry: cached ocean data is not captured for " + block.name)
                if modifier.type != "NODES" or modifier.node_group is None:
                    continue
                trees = [modifier.node_group]
                while trees:
                    tree = trees.pop()
                    if tree in checked_trees:
                        continue
                    checked_trees.add(tree)
                    _check_drivers(tree)
                    for node in tree.nodes:
                        if node.bl_idname in cache_nodes:
                            raise CaptureError("Simulation-dependent geometry: %s / %s contains %s; simulation/bake caches are not captured" %
                                               (block.name, tree.name, node.bl_idname))
                        nested = getattr(node, "node_tree", None)
                        if nested is not None:
                            trees.append(nested)
        pending.extend(dependency for dependency in reverse.get(block, [])
                       if not isinstance(dependency, (bpy.types.Material, bpy.types.ShaderNodeTree)))


def _all_shader_dependencies(owner, visit=None):
    """Traverse all saved output roots without editing active-output flags.

    Native libraries retain engine-specific roots. Their resources must be
    frozen before a worker opens the snapshot, even if Cycles never uses them.
    """
    tree = getattr(owner, "node_tree", None)
    if tree is None:
        return {"images": set(), "diagnostics": [], "node_count": 0}
    primary = tree.get_output_node("CYCLES")
    outputs = [node for node in tree.nodes if node.type in {"OUTPUT_MATERIAL", "OUTPUT_WORLD", "OUTPUT_LIGHT"}]
    result = {"images": set(), "diagnostics": [], "node_count": 0}
    class TreeProxy:
        def __init__(self, output):
            self.output = output
        def get_output_node(self, _target):
            return self.output
        def __getattr__(self, key):
            return getattr(tree, key)
    class OwnerProxy:
        def __init__(self, output):
            self.node_tree = TreeProxy(output)
        def __getattr__(self, key):
            return getattr(owner, key)
    for output in outputs:
        current = shader_graph.material_dependencies(OwnerProxy(output), visit=visit)
        result["images"].update(current["images"])
        result["node_count"] += current["node_count"]
        result["diagnostics"].extend(item for item in current["diagnostics"]
                                     if output == primary or item["code"] != "CYCLES_UNSUPPORTED_NODE")
    return result


def _dependencies(objects, resources=None):
    """Live shader socket closure plus non-shader ID dependencies of selected geometry."""
    roots = list(objects)
    owners = list(shader_graph.used_materials(objects))
    if bpy.context.scene.world:
        owners.append(bpy.context.scene.world)
    owners.extend(obj.data for obj in bpy.context.scene.objects if obj.type == "LIGHT")
    images, nodes, diagnostics = set(), {}, []
    for owner in owners:
        _check_drivers(owner)
        if getattr(owner, "node_tree", None) is not None:
            _check_drivers(owner.node_tree)
        def visit(node, _socket, _context):
            _check_drivers(node.id_data)
            image = getattr(node, "image", None)
            if isinstance(image, bpy.types.Image):
                nodes.setdefault(image.as_pointer(), []).append(node)
            if node.bl_idname in {"ShaderNodeScript", "ShaderNodeTexIES"} and getattr(node, "mode", "INTERNAL") == "EXTERNAL":
                if resources is not None and node not in resources:
                    resources.append(node)
            elif node.bl_idname == "ShaderNodeScript":
                try:
                    shader_resources.validate_internal_node(node)
                except shader_resources.ResourceError as exc:
                    diagnostics.append(str(exc))
        result = _all_shader_dependencies(owner, visit=visit)
        images.update(result["images"])
        diagnostics.extend(item["message"] for item in result["diagnostics"] if item["severity"] == "ERROR")
    # Follow geometry/modifier/constraint ID references, without reintroducing
    # disconnected material images already excluded by the socket traversal.
    reverse = {}
    for dependency, users in bpy.data.user_map().items():
        for user in users:
            reverse.setdefault(user, []).append(dependency)
    visited, pending = set(), roots[:]
    while pending:
        owner = pending.pop()
        if owner in visited:
            continue
        visited.add(owner)
        for dependency in reverse.get(owner, []):
            if isinstance(dependency, (bpy.types.Material, bpy.types.ShaderNodeTree)):
                continue
            if isinstance(dependency, bpy.types.Image):
                images.add(dependency.as_pointer())
                nodes.setdefault(dependency.as_pointer(), [])
            elif isinstance(dependency, (bpy.types.CacheFile, bpy.types.Volume, bpy.types.MovieClip, bpy.types.Sound)):
                diagnostics.append("Unverified geometry/scene resource: " + dependency.name + " (" + dependency.bl_rna.identifier + ")")
            else:
                pending.append(dependency)
    required = [image for image in bpy.data.images if image.as_pointer() in images]
    return required, nodes, diagnostics


def _environment(budget, dependencies):
    scene = bpy.context.scene
    def register(path, kind):
        path = Path(path).resolve()
        if not path.is_file():
            raise CaptureError("Required external resource unavailable: " + str(path))
        if str(path) not in dependencies:
            budget.file(path)
            dependencies[str(path)] = {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size, "kind": kind}
    config_dir = Path(bpy.utils.system_resource("DATAFILES", path="colormanagement"))
    config = os.environ.get("OCIO", "")
    if config and Path(config).resolve() != (config_dir / "config.ocio").resolve():
        try:
            ocio = ocio_capture.inspect_config(config, register, budget.check)
        except Exception as exc:
            raise CaptureError("Custom OCIO closure could not be verified: " + str(exc)) from exc
    else:
        if not config_dir.is_dir() or not (config_dir / "config.ocio").is_file():
            raise CaptureError("Bundled OCIO configuration could not be identified")
        config_hash = hashlib.sha256()
        for path in sorted(config_dir.rglob("*")):
            if path.is_file():
                register(path, "OCIO")
                config_hash.update(path.relative_to(config_dir).as_posix().encode() + b"\0" + bytes.fromhex(dependencies[str(path.resolve())]["sha256"]))
        ocio = {"kind": "BLENDER_BUNDLED", "config": str(config_dir / "config.ocio"), "sha256": config_hash.hexdigest()}
    for library in bpy.data.libraries:
        register(bpy.path.abspath(library.filepath), "LINKED_LIBRARY")
    return {"blender_version": list(bpy.app.version),
            "build_hash": bpy.app.build_hash.decode("utf-8", "replace"),
            "scene": scene.name, "frame": scene.frame_current, "subframe": scene.frame_subframe,
            "render_engine": scene.render.engine,
            "ocio": ocio,
            "cycles_shading_system": bool(getattr(scene.cycles, "shading_system", False)),
            "working_space": bpy.data.colorspace.working_space,
            "working_space_interop_id": bpy.data.colorspace.working_space_interop_id,
            "view": {key: getattr(scene.view_settings, key) for key in ("view_transform", "look", "exposure", "gamma")},
            "display_device": scene.display_settings.display_device,
            "sequencer_colorspace": scene.sequencer_colorspace_settings.name}, register


def _single_frame_path(image, nodes):
    if image.is_dirty:
        raise CaptureError("Dirty sequence buffer cannot be associated with an exact image-user frame: " + image.name)
    if not nodes:
        raise CaptureError("Sequence has no proven static image-user context: " + image.name)
    paths = set()
    # Original node users can have frame_current == 0 until render depsgraph
    # evaluation. filepath_from_user merely substitutes that cached value. Use
    # Blender's noncyclic one-frame calculation (BKE_image_user_frame_get) in a
    # private ImageUser, then let Blender resolve the sequence filename itself.
    tree = bpy.data.node_groups.new("__IG_SEQUENCE_PATH_" + uuid.uuid4().hex, "ShaderNodeTree")
    try:
        private_user = tree.nodes.new("ShaderNodeTexImage").image_user
        for node in nodes:
            user = getattr(node, "image_user", None)
            if user is None or user.frame_duration < 1:
                raise CaptureError("Sequence requires an explicit positive-duration image-user configuration: " + image.name)
            relative_frame = int(bpy.context.scene.frame_current_final) - user.frame_start + 1
            if user.use_cyclic:
                relative_frame = relative_frame % user.frame_duration or user.frame_duration
            private_user.frame_current = min(max(relative_frame, 0), user.frame_duration) + user.frame_offset
            paths.add(str(Path(image.filepath_from_user(image_user=private_user)).resolve()))
    finally:
        bpy.data.node_groups.remove(tree)
    if len(paths) != 1:
        raise CaptureError("Sequence users resolve to different frames: " + image.name)
    return Path(next(iter(paths)))


def _tiled_paths(image):
    template = bpy.path.abspath(image.filepath, library=image.library)
    if "<UDIM>" not in template:
        raise CaptureError("UDIM capture requires an explicit <UDIM> filepath template: " + image.name)
    return [(tile.number, Path(template.replace("<UDIM>", str(tile.number))).resolve()) for tile in image.tiles]


def _exr_has_float_channels(path):
    """Check the lossless-writer contract without depending on another decoder."""
    with Path(path).open("rb") as stream:
        if stream.read(4) != b"\x76\x2f\x31\x01":
            return False
        stream.read(4)
        def string():
            data = bytearray()
            while len(data) <= 255:
                value = stream.read(1)
                if value == b"\0":
                    return bytes(data)
                if not value:
                    raise CaptureError("Truncated EXR header")
                data.extend(value)
            raise CaptureError("Oversized EXR header string")
        while True:
            name = string()
            if not name:
                return False
            kind = string()
            size_bytes = stream.read(4)
            if len(size_bytes) != 4:
                raise CaptureError("Truncated EXR attribute")
            size = struct.unpack("<I", size_bytes)[0]
            if size > 1024 * 1024:
                raise CaptureError("EXR attribute exceeds capture budget")
            data = stream.read(size)
            if name == b"channels" and kind == b"chlist":
                position, count = 0, 0
                while position < len(data) and data[position] != 0:
                    position = data.index(b"\0", position) + 1
                    if position + 16 > len(data) or struct.unpack_from("<i", data, position)[0] != 2:
                        return False
                    count += 1
                    position += 16
                return count >= 3


def _decoded_tiles(image, tiles, temporary, budget=None):
    """Decode each encoded packed tile using separate single-image datablocks."""
    entries = list(image.packed_files)
    if len(entries) != len(tiles):
        raise CaptureError("Packed UDIM tile count differs")
    result = []
    for number in tiles:
        # Every capture sets this explicit naming convention before packing.
        matches = [entry for entry in entries if re.search(r"(?<!\d)%d(?!\d)" % number, Path(entry.filepath).name)]
        if len(matches) != 1:
            raise CaptureError("Packed UDIM tile cannot be mapped uniquely: " + str(number))
        entry = matches[0]
        path = Path(temporary) / ("decoded.%d" % number + Path(entry.filepath).suffix)
        path.write_bytes(bytes(entry.packed_file.data))
        decoded = bpy.data.images.load(str(path), check_existing=False)
        try:
            decoded.colorspace_settings.name = image.colorspace_settings.name
            decoded.alpha_mode = image.alpha_mode
            if budget is not None:
                budget.image(decoded)
            result.append({"tile": number, "size": list(decoded.size), "is_float": bool(decoded.is_float),
                           "pixel_sha256": _pixel_hash(_pixels(decoded))})
        finally:
            bpy.data.images.remove(decoded)
    return result


def _private_udim(image, row, budget, created):
    if "save_copy" not in bpy.types.Image.bl_rna.functions["save"].parameters:
        raise CaptureError("UDIM buffer capture requires Blender's non-destructive save_copy API")
    if image.file_format not in {"PNG", "OPEN_EXR"}:
        raise CaptureError("UDIM buffer capture requires lossless PNG byte tiles or float32 EXR tiles")
    if image.is_float != (image.file_format == "OPEN_EXR"):
        raise CaptureError("UDIM buffer precision does not match a proven lossless PNG/float32 EXR encoding")
    original = _image_state(image, pixels=True)
    numbers = [tile.number for tile in image.tiles]
    if len(numbers) > budget.limits["max_images"]:
        raise CaptureError("UDIM tile count exceeds capture budget")
    tile_pixels = 0
    for tile in image.tiles:
        width, height = tile.size
        count = width * height
        if width < 1 or height < 1 or count > budget.limits["max_image_pixels"]:
            raise CaptureError("UDIM tile dimensions unavailable or exceed capture budget: " + str(tile.number))
        tile_pixels += count
    if budget.pixels + tile_pixels > budget.limits["max_total_pixels"]:
        raise CaptureError("UDIM total pixel budget exceeded before native export")
    # Explicit native save-copy walks the current per-tile ImBuf cache. Image.copy
    # would instead discard dirty tiles. Only private paths receive encoded bytes.
    with tempfile.TemporaryDirectory(prefix="import-glue-udim-") as temporary:
        suffix = ".exr" if image.is_float else ".png"
        template = Path(temporary) / ("capture.<UDIM>" + suffix)
        budget.check()
        image.save(filepath=str(template), save_copy=True)
        if _image_state(image, pixels=True) != original:
            raise CaptureError("Native UDIM save-copy changed original image state")
        inventory = []
        for number in numbers:
            path = Path(str(template).replace("<UDIM>", str(number)))
            if not path.is_file():
                raise CaptureError("Native save-copy omitted UDIM tile " + str(number))
            budget.file(path)
            if image.is_float and not _exr_has_float_channels(path):
                raise CaptureError("UDIM EXR writer did not preserve float32 channels")
            if not image.is_float:
                with path.open("rb") as stream:
                    header = stream.read(26)
                if header[:8] != b"\x89PNG\r\n\x1a\n" or header[24] != 8:
                    raise CaptureError("UDIM PNG writer did not preserve byte channels")
            inventory.append({"tile": number, "sha256": _sha(path)})
        clone = bpy.data.images.load(str(template).replace("<UDIM>", str(numbers[0])), check_existing=False)
        created.append(clone)
        clone.name = "__IG_CAPTURE_" + uuid.uuid4().hex
        clone.source = "TILED"
        clone.filepath = str(template)
        for number in numbers:
            if number not in [tile.number for tile in clone.tiles]:
                clone.tiles.new(number)
        clone.colorspace_settings.name = image.colorspace_settings.name
        clone.alpha_mode = image.alpha_mode
        clone.use_half_precision = image.use_half_precision
        clone.reload()
        clone.pack()
        tile_pixels = _decoded_tiles(clone, numbers, temporary, budget)
        if tile_pixels[0]["pixel_sha256"] != original["pixel_sha256"]:
            raise CaptureError("UDIM exposed tile pixels changed during lossless save-copy")
        if sorted(entry["sha256"] for entry in _packed(clone)) != sorted(entry["sha256"] for entry in inventory):
            raise CaptureError("UDIM packed bytes differ from private buffer exports")
        if any(entry["is_float"] != image.is_float for entry in tile_pixels):
            raise CaptureError("Mixed-precision UDIM tiles are not supported by the lossless writer contract")
        row.update(mode="PACKED_UDIM_BUFFERS", captured_identity=_identity(clone),
                   captured_state=_image_state(clone), decoded_tiles=tile_pixels,
                   pixel_contract={"comparison": "fresh-decoded float32 hashes for every native save-copy tile",
                                   "source_buffer_access": "Blender native all-tile writer; exposed first tile checked bitwise",
                                   "encoding": "float32 EXR" if image.is_float else "PNG8"})
    return clone


def _private_image(image, nodes, row, budget, register, created):
    if image.source == "MOVIE":
        raise CaptureError("Video textures are outside this static-material capture: " + image.name)
    if image.source not in {"FILE", "GENERATED", "TILED", "SEQUENCE"} or image.type not in {"IMAGE", "UV_TEST"}:
        raise CaptureError("Unverified image source/type: %s (%s/%s)" % (image.name, image.source, image.type))
    if getattr(image, "is_multiview", False) or getattr(image, "is_stereo_3d", False):
        raise CaptureError("Multiview image capture is unverified: " + image.name)
    linked = image.library is not None or any(getattr(node.id_data, "library", None) for node in nodes)
    if linked:
        if image.is_dirty or image.source in {"GENERATED", "SEQUENCE"}:
            raise CaptureError("Unsaved or frame-dependent linked image cannot be serialized by remapping a local ID: " + image.name)
        if not image.packed_files:
            paths = _tiled_paths(image) if image.source == "TILED" else [(None, Path(bpy.path.abspath(image.filepath, library=image.library)).resolve())]
            for tile, path in paths:
                register(path, "LINKED_IMAGE")
                row["external_tiles"].append({"tile": tile, "path": str(path), "sha256": _sha(path)})
        row.update(mode="RETAINED_LINKED", captured_identity=_identity(image), captured_state=_image_state(image, pixels=image.source != "TILED"))
        return None
    if image.source == "TILED":
        if image.is_dirty:
            return _private_udim(image, row, budget, created)
        clone = image.copy()
        created.append(clone)
        clone.name = "__IG_CAPTURE_" + uuid.uuid4().hex
        if not clone.packed_files:
            for tile, path in _tiled_paths(image):
                budget.file(path)
                if not path.is_file():
                    raise CaptureError("Required UDIM tile missing: " + str(path))
                row["external_tiles"].append({"tile": tile, "path": str(path), "sha256": _sha(path)})
            clone.pack()
        if len(clone.packed_files) != len(image.tiles):
            raise CaptureError("Not every UDIM tile was packed: " + image.name)
        # Native packing from files must preserve the actual bytes for every tile.
        packed_hashes = sorted(entry["sha256"] for entry in _packed(clone))
        expected = sorted(entry["sha256"] for entry in (_packed(image) or row["external_tiles"]))
        if packed_hashes != expected:
            raise CaptureError("UDIM packed-file bytes do not match the captured tile inventory")
        row.update(mode="PACKED_UDIM", captured_identity=_identity(clone), captured_state=_image_state(clone))
        with tempfile.TemporaryDirectory(prefix="import-glue-udim-proof-") as temporary:
            row["decoded_tiles"] = _decoded_tiles(clone, [tile.number for tile in clone.tiles], temporary, budget)
        return clone
    source = image
    if image.source == "SEQUENCE":
        path = _single_frame_path(image, nodes)
        budget.file(path)
        row["sequence_frame"] = {"path": str(path), "sha256": _sha(path), "policy": "resolved static scene frame; all live users agree"}
        source = bpy.data.images.load(str(path), check_existing=False)
        created.append(source)
        source.colorspace_settings.name = image.colorspace_settings.name
        source.alpha_mode = image.alpha_mode
    budget.image(source)
    pixels = _pixels(source)
    row["source_pixel_sha256"] = _pixel_hash(pixels)
    clone = bpy.data.images.new("__IG_CAPTURE_" + uuid.uuid4().hex,
                               width=source.size[0], height=source.size[1], alpha=True,
                               float_buffer=source.is_float, is_data=source.colorspace_settings.is_data)
    created.append(clone)
    if source.is_float and not source.colorspace_settings.is_data and source.colorspace_settings.name != clone.colorspace_settings.name:
        raise CaptureError("Float-buffer colorspace interpretation is not proven: " + source.name + " / " + source.colorspace_settings.name)
    clone.colorspace_settings.name = source.colorspace_settings.name
    clone.alpha_mode = source.alpha_mode
    clone.use_half_precision = source.use_half_precision
    clone.pixels.foreach_set(pixels)
    if _pixel_hash(_pixels(clone)) != row["source_pixel_sha256"]:
        raise CaptureError("Private image buffer copy changed pixels: " + source.name)
    clone.pack()
    if len(clone.packed_files) != 1:
        raise CaptureError("Private image did not pack exactly one buffer: " + source.name)
    # Image.reload() may retain a cached buffer when a generated image's
    # original filepath is empty. Decode the packed bytes through a genuinely
    # new datablock, then pack that exact file before its temporary path goes.
    encoded = bytes(clone.packed_files[0].packed_file.data)
    with tempfile.TemporaryDirectory(prefix="import-glue-image-proof-") as temporary:
        encoded_path = Path(temporary) / ("pixels.exr" if source.is_float else "pixels.png")
        encoded_path.write_bytes(encoded)
        decoded = bpy.data.images.load(str(encoded_path), check_existing=False)
        created.append(decoded)
        decoded.name = "__IG_CAPTURE_" + uuid.uuid4().hex
        decoded.colorspace_settings.name = source.colorspace_settings.name
        decoded.alpha_mode = source.alpha_mode
        decoded.use_half_precision = source.use_half_precision
        reloaded = _pixels(decoded)
        decoded.pack()
    if _pixel_hash(reloaded) != row["source_pixel_sha256"]:
        error = float(np.max(np.abs(reloaded - pixels))) if reloaded.shape == pixels.shape else None
        raise CaptureError("Packed image roundtrip is not bit-exact: %s (max_abs_error=%s)" % (source.name, error))
    clone = decoded
    row.update(mode="PACKED_PIXEL_BUFFER", captured_identity=_identity(clone),
               captured_state=_image_state(clone, pixels=True),
               pixel_contract={"comparison": "bitwise float32 buffer equality", "max_abs_error": 0.0,
                               "encoding": "native EXR" if source.is_float else "native PNG"})
    return clone


def capture_image_copy(image, image_nodes=None, *, limits=None):
    """Return a verified private packed image without remapping any source users.

    Caller removes result['image'] when result['owned'] is True. A clean linked
    image can only be retained under external checksums; that result has owned
    False and must not be described as a self-contained portable image copy.
    """
    # Querying a movie's dimensions/type metadata may start decoding and alter
    # its color interpretation. Refuse before collecting any buffer state.
    if image.source == "MOVIE":
        raise CaptureError("Video textures are outside this static-material capture: " + image.name)
    budget = _Budget(limits)
    if image.source in {"FILE", "GENERATED"}:
        budget.image(image)
    created, dependencies = [], {}
    row = {"source_identity": _identity(image), "source_state": _image_state(image), "external_tiles": []}
    before = _image_state(image, pixels=image.source in {"FILE", "GENERATED"})
    def register(path, kind):
        path = Path(path).resolve()
        if not path.is_file():
            raise CaptureError("Required external resource unavailable: " + str(path))
        if str(path) not in dependencies:
            budget.file(path)
            dependencies[str(path)] = {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size, "kind": kind}
    result = None
    try:
        private = _private_image(image, image_nodes or [], row, budget, register, created)
        if image.library:
            register(bpy.path.abspath(image.library.filepath), "LINKED_LIBRARY")
        if _image_state(image, pixels="pixel_sha256" in before) != before:
            raise CaptureError("Original image state changed during private capture: " + image.name)
        result = {"image": private if private is not None else image, "owned": private is not None,
                  "record": row, "dependencies": sorted(dependencies.values(), key=lambda item: item["path"])}
        return result
    finally:
        for temporary in reversed(created):
            if result is None or temporary != result["image"]:
                if temporary.name in bpy.data.images:
                    bpy.data.images.remove(temporary)


def _resource_state(node):
    keys = ["mode", "filepath"] + (["script", "bytecode", "bytecode_hash", "use_auto_update"]
                                   if node.bl_idname == "ShaderNodeScript" else ["ies"])
    return {key: getattr(node, key) for key in keys}


def _shader_trees():
    trees = list(bpy.data.node_groups)
    for owner in [*bpy.data.materials, *bpy.data.worlds, *bpy.data.lights]:
        tree = getattr(owner, "node_tree", None)
        if tree is not None:
            trees.append(tree)
    return trees


def _shader_tree_identity(tree):
    for kind, owners in (("MATERIAL", bpy.data.materials), ("WORLD", bpy.data.worlds), ("LIGHT", bpy.data.lights)):
        for owner in owners:
            if getattr(owner, "node_tree", None) == tree:
                return {"kind": kind, "owner": _identity(owner)}
    return {"kind": "NODE_GROUP", "owner": _identity(tree)}


def _copy_resource_owners(objects, resources, remaps, groups):
    """Keep mode changes on private owner graphs: restoring an EXTERNAL OSL
    node itself can trigger Blender to rewrite its source .oso sidecar.
    """
    required = {node.as_pointer() for node in resources}
    owners = list(shader_graph.used_materials(objects))
    if bpy.context.scene.world:
        owners.append(bpy.context.scene.world)
    owners.extend(obj.data for obj in bpy.context.scene.objects if obj.type == "LIGHT")
    visited_owners = set()
    for owner in owners:
        if owner.as_pointer() in visited_owners or getattr(owner, "node_tree", None) is None:
            continue
        visited_owners.add(owner.as_pointer())
        found = []
        _all_shader_dependencies(owner, visit=lambda node, _socket, _context:
                                 found.append(node) if node.as_pointer() in required else None)
        if not found:
            continue
        clone = owner.copy()
        clone.name = "__IG_CAPTURE_OWNER_" + uuid.uuid4().hex
        clone.use_fake_user = False
        # Register ownership before touching a private graph for exception cleanup.
        remaps.append((owner, clone, bool(owner.use_fake_user)))
        pending = [(clone.node_tree, frozenset())]
        while pending:
            tree, ancestors = pending.pop()
            for node in tree.nodes:
                child = getattr(node, "node_tree", None)
                if child is None:
                    continue
                pointer = child.as_pointer()
                if pointer in ancestors:
                    raise CaptureError("Recursive shader group cannot be captured")
                private = child.copy()
                private.name = "__IG_CAPTURE_GROUP_" + uuid.uuid4().hex
                private.use_fake_user = False
                groups.append(private)
                node.node_tree = private
                pending.append((private, ancestors | {pointer}))
        owner.user_remap(clone)


def capture_snapshot(path, objects=None, *, limits=None):
    """Save a private snapshot and return its manifest; original state is restored.

    objects defaults to selected mesh objects. Required shader images, active
    world/light images and selected geometry ID resources form the capture scope.
    Image sequences freeze the scene frame when all live users resolve identically.
    """
    global _BUSY
    if _BUSY:
        raise CaptureError("Source capture is already active")
    _BUSY = True
    directory = None
    manifest = None
    created, remapped, original_images = [], [], []
    resource_texts, resource_owners, resource_groups = [], [], []
    temporary = None
    error = None
    try:
        budget = _Budget(limits)
        destination = Path(path).resolve()
        original_file = bpy.data.filepath
        if not original_file or not Path(original_file).is_file():
            raise CaptureError("Save the source .blend before capturing it")
        if destination == Path(original_file).resolve() or destination.exists():
            raise CaptureError("Snapshot must be a new private .blend path")
        if destination.suffix.lower() != ".blend":
            raise CaptureError("Snapshot requires a .blend destination")
        objects = list(objects if objects is not None else [obj for obj in bpy.context.selected_objects if obj.type == "MESH"])
        if not objects or any(obj.type != "MESH" for obj in objects):
            raise CaptureError("Select nonempty mesh source objects")
        _geometry_guard(objects)
        directory = destination.parent
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / MANIFEST_NAME).exists():
            raise CaptureError("Existing capture metadata must not be overwritten")
        source_hash = _sha(original_file)
        manifest = {"schema": SCHEMA, "capture_id": uuid.uuid4().hex, "status": "CAPTURING",
                    "snapshot": str(destination), "source": str(Path(original_file).resolve()),
                    "source_sha256": source_hash, "objects": [_identity(obj) for obj in objects],
                    "images": [], "shader_resources": [], "dependencies": [], "diagnostics": [],
                    "limits": budget.limits, "resource_policy": "Images and static shader files embedded; linked libraries and OCIO retained externally under checksums"}
        dependencies = {}
        manifest["environment"], register = _environment(budget, dependencies)
        resource_nodes = []
        images, nodes, diagnostics = _dependencies(objects, resource_nodes)
        if diagnostics:
            raise CaptureError("Required source dependencies are unverified: " + "; ".join(diagnostics))
        if resource_nodes:
            _copy_resource_owners(objects, resource_nodes, resource_owners, resource_groups)
            resource_nodes = []
            images, nodes, diagnostics = _dependencies(objects, resource_nodes)
            if diagnostics:
                raise CaptureError("Private shader dependency closure is unverified: " + "; ".join(diagnostics))
        for image in images:
            if image.source == "MOVIE":
                raise CaptureError("Video textures are outside this static-material capture: " + image.name)
        if len(images) > budget.limits["max_images"]:
            raise CaptureError("Source capture image count budget exceeded")
        for image in images:
            if image.source in {"FILE", "GENERATED"}:
                budget.image(image)
        original_images = [(image, _image_state(image, pixels=image.source in {"FILE", "GENERATED"}) if image in images else _basic_image_state(image))
                           for image in list(bpy.data.images)]
        for node in resource_nodes:
            budget.check()
            prepared = shader_resources.prepare_node(node)
            for entry in prepared["dependencies"]:
                budget.file(Path(entry["path"]))
                if _sha(entry["path"]) != entry["sha256"]:
                    raise CaptureError("Shader dependency changed during capture: " + entry["path"])
            record = shader_resources.capture_node(node, resource_texts, prepared=prepared)
            record.update(node=node.name, tree=_shader_tree_identity(node.id_data))
            manifest["shader_resources"].append(record)
        for image in images:
            budget.check()
            row = {"source_identity": _identity(image), "source_state": _image_state(image), "external_tiles": []}
            if image.source == "FILE" and image.filepath:
                source_path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
                if source_path.is_file():
                    budget.file(source_path)
                    row["original_disk_file"] = {"path": str(source_path), "sha256": _sha(source_path), "bytes": source_path.stat().st_size}
            manifest["images"].append(row)
            clone = _private_image(image, nodes.get(image.as_pointer(), []), row, budget, register, created)
            if clone is not None:
                clone.use_fake_user = True
                image.user_remap(clone)
                remapped.append((image, clone))
        manifest["dependencies"] = sorted(dependencies.values(), key=lambda item: item["path"])
        for dependency in manifest["dependencies"]:
            if _sha(dependency["path"]) != dependency["sha256"]:
                raise CaptureError("Source dependency changed during capture: " + dependency["path"])
        for resource in manifest["shader_resources"]:
            for dependency in resource["dependencies"]:
                if _sha(dependency["path"]) != dependency["sha256"]:
                    raise CaptureError("Shader dependency changed during capture: " + dependency["path"])
        temporary = destination.with_name(".capture-" + uuid.uuid4().hex + ".blend")
        if bpy.ops.wm.save_as_mainfile(filepath=str(temporary), copy=True, relative_remap=True) != {"FINISHED"}:
            raise CaptureError("Blender did not save the source snapshot")
        if bpy.data.filepath != original_file:
            raise CaptureError("Saving a copy changed the active source filepath")
        if _sha(original_file) != source_hash:
            raise CaptureError("Source .blend changed during capture")
        budget.check()
        os.replace(temporary, destination)
        manifest["snapshot_sha256"] = _sha(destination)
        manifest["status"] = "CAPTURED"
        manifest["fresh_open_verified"] = False
    except BaseException as exc:
        error = exc
    finally:
        restore_errors = []
        for original, clone, fake_user in reversed(resource_owners):
            try:
                clone.user_remap(original)
                original.use_fake_user = fake_user
                collection = (bpy.data.materials if isinstance(clone, bpy.types.Material) else
                              bpy.data.worlds if isinstance(clone, bpy.types.World) else bpy.data.lights)
                collection.remove(clone)
            except Exception as exc:
                restore_errors.append("Shader owner restoration: " + str(exc))
        for group in reversed(resource_groups):
            try:
                bpy.data.node_groups.remove(group)
            except Exception as exc:
                restore_errors.append("Shader group cleanup: " + str(exc))
        for text in reversed(resource_texts):
            try:
                bpy.data.texts.remove(text)
            except Exception as exc:
                restore_errors.append("Shader text cleanup: " + str(exc))
        for original, clone in reversed(remapped):
            try:
                clone.user_remap(original)
            except Exception as exc:
                restore_errors.append("Image user restoration: " + str(exc))
        for clone in reversed(created):
            try:
                if clone.name in bpy.data.images:
                    bpy.data.images.remove(clone)
            except Exception as exc:
                restore_errors.append("Private image cleanup: " + str(exc))
        for image, before in original_images:
            try:
                if image.use_fake_user != before["fake_user"]:
                    image.use_fake_user = before["fake_user"]
                # save-copy should restore remapped paths; recover them even if
                # a Blender version changes this behavior, then reject the run.
                if image.filepath != before["filepath"]:
                    image.filepath = before["filepath"]
                    restore_errors.append("Snapshot save temporarily changed original image path: " + image.name)
                after = _image_state(image, pixels="pixel_sha256" in before) if "packed" in before else _basic_image_state(image)
                if after != before:
                    restore_errors.append("Original image state changed: " + image.name)
            except Exception as exc:
                restore_errors.append("Original image verification: " + str(exc))
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if restore_errors:
            error = CaptureError("; ".join(restore_errors))
        try:
            if manifest is not None:
                if error is not None:
                    manifest["status"] = "REFUSED"
                    manifest["diagnostics"].append(str(error))
                manifest["source_restored"] = not restore_errors
                _atomic_manifest(directory / MANIFEST_NAME, manifest)
        finally:
            _BUSY = False
    if error is not None:
        raise CaptureError(str(error), manifest) from error
    return manifest


def verify_capture_files(manifest_or_path, *, snapshot_path=None):
    """Pre-open verification; no Blender datablocks are changed."""
    manifest = read_manifest(manifest_or_path)
    path = Path(snapshot_path or manifest["snapshot"]).resolve()
    if not path.is_file() or _sha(path) != manifest.get("snapshot_sha256"):
        raise CaptureError("Captured snapshot checksum mismatch")
    for entry in manifest["dependencies"]:
        dependency = Path(entry["path"])
        if not dependency.is_file() or _sha(dependency) != entry["sha256"]:
            raise CaptureError("Retained source dependency changed: " + str(dependency))
    return {"ok": True, "capture_id": manifest["capture_id"], "stage": "FILES_VERIFIED"}


def verify_snapshot(manifest_or_path, *, require_build=True, snapshot_path=None):
    """Verify a freshly opened snapshot's packed resources, pixels and context."""
    manifest = read_manifest(manifest_or_path)
    verify_capture_files(manifest, snapshot_path=snapshot_path)
    if Path(bpy.data.filepath).resolve() != Path(snapshot_path or manifest["snapshot"]).resolve():
        raise CaptureError("Fresh-open verification must run inside the captured .blend")
    environment = manifest["environment"]
    ocio_environment = os.environ.get("OCIO", "")
    if ((environment["ocio"]["kind"] == "CUSTOM_OCIO" and not ocio_environment) or
            (ocio_environment and Path(ocio_environment).resolve() != Path(environment["ocio"]["config"]).resolve())):
        raise CaptureError("Worker OCIO environment differs from captured configuration")
    if environment["ocio"]["kind"] == "CUSTOM_OCIO":
        dependencies = {}
        actual_environment, _register = _environment(_Budget(manifest.get("limits")), dependencies)
        if actual_environment["ocio"] != environment["ocio"]:
            raise CaptureError("Worker custom OCIO transform closure differs from captured configuration")
    if require_build and (list(bpy.app.version) != environment["blender_version"] or
                          bpy.app.build_hash.decode("utf-8", "replace") != environment["build_hash"]):
        raise CaptureError("Blender build differs from the captured renderer")
    scene = bpy.context.scene
    if scene.name != environment["scene"] or scene.frame_current != environment["frame"] or scene.frame_subframe != environment["subframe"]:
        raise CaptureError("Captured scene/frame/subframe changed")
    if scene.render.engine != environment["render_engine"]:
        raise CaptureError("Captured renderer changed")
    if ("working_space" in environment and
            (bpy.data.colorspace.working_space != environment["working_space"] or
             bpy.data.colorspace.working_space_interop_id != environment["working_space_interop_id"])):
        raise CaptureError("Captured blend-file working color space changed")
    if ("cycles_shading_system" in environment and
            bool(scene.cycles.shading_system) != environment["cycles_shading_system"]):
        raise CaptureError("Captured Cycles shading system changed")
    if ({key: getattr(scene.view_settings, key) for key in environment["view"]} != environment["view"] or
            scene.display_settings.display_device != environment["display_device"] or
            scene.sequencer_colorspace_settings.name != environment["sequencer_colorspace"]):
        raise CaptureError("Captured color-management settings changed")
    objects = []
    for identity in manifest["objects"]:
        matches = [obj for obj in scene.objects if _identity(obj) == identity]
        if len(matches) != 1:
            raise CaptureError("Captured source identity is unavailable or ambiguous: " + identity["name"])
        objects.append(matches[0])
    _geometry_guard(objects)
    required, _nodes, diagnostics = _dependencies(objects)
    if diagnostics:
        raise CaptureError("Captured dependency closure is no longer evaluable: " + "; ".join(diagnostics))
    required_ids = {_identity(image)["name"] + "\0" + _identity(image)["library"] for image in required}
    for row in manifest["images"]:
        identity = row["captured_identity"]
        candidates = [image for image in bpy.data.images if _identity(image) == identity]
        if len(candidates) != 1:
            raise CaptureError("Captured image identity is unavailable or ambiguous: " + identity["name"])
        image = candidates[0]
        if identity["name"] + "\0" + identity["library"] not in required_ids:
            raise CaptureError("Captured image is no longer referenced by the required source: " + image.name)
        expected = row["captured_state"]
        for key, actual in (("source", image.source), ("colorspace", image.colorspace_settings.name),
                            ("alpha_mode", image.alpha_mode), ("is_float", bool(image.is_float)),
                            ("size", list(image.size)), ("tiles", [tile.number for tile in image.tiles])):
            if actual != expected[key]:
                raise CaptureError("Captured image metadata differs (%s): %s" % (key, image.name))
        # Relative path remapping may change packed entry path strings; content
        # identity and number of buffers are what the encoded-data check proves.
        if sorted((entry["sha256"], entry["bytes"]) for entry in _packed(image)) != sorted((entry["sha256"], entry["bytes"]) for entry in expected["packed"]):
            raise CaptureError("Captured packed image bytes changed: " + image.name)
        if "pixel_sha256" in expected and _pixel_hash(_pixels(image)) != expected["pixel_sha256"]:
            raise CaptureError("Captured image pixels differ after fresh open: " + image.name)
        if "decoded_tiles" in row:
            with tempfile.TemporaryDirectory(prefix="import-glue-udim-verify-") as temporary:
                actual = _decoded_tiles(image, [entry["tile"] for entry in row["decoded_tiles"]], temporary)
            if actual != row["decoded_tiles"]:
                raise CaptureError("Captured UDIM tile pixels differ after fresh open: " + image.name)
    for record in manifest.get("shader_resources", []):
        trees = [tree for tree in _shader_trees() if _shader_tree_identity(tree) == record["tree"]]
        if len(trees) != 1 or trees[0].nodes.get(record["node"]) is None:
            raise CaptureError("Captured shader resource identity is unavailable or ambiguous")
        shader_resources.verify_node(trees[0].nodes[record["node"]], record)
    return {"ok": True, "capture_id": manifest["capture_id"], "stage": "FRESH_OPEN_VERIFIED",
            "images_verified": len(manifest["images"]), "shader_resources_verified": len(manifest.get("shader_resources", [])),
            "pixel_contract": "bitwise float32 decoded buffers, including every captured UDIM tile"}
