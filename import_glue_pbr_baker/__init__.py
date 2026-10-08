"""Import Glue PBR Baker Blender add-on."""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.app.handlers import persistent
from bpy.types import Operator, Panel, PropertyGroup

from . import engine, material_capabilities, precheck, background_ui


bl_info = {
    "name": "Import Glue PBR Baker",
    "author": "Sohra",
    "version": (1, 8, 0),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar > Roblox > Import Glue",
    "description": "Convert selected game-rip meshes into Roblox-ready PBR maps",
    "category": "Material",
}


ADDON_VERSION = "1.8.0"
ENGINE_CONFIG_KEYS = (
    "OUTPUT_ROOT",
    "RES",
    "MAX_RES",
    # RES/MAX_RES are ceilings; this is the multiplier that decides what
    # GRAPH_BAKE/PROXY_BAKE ask for in the first place.  Listed here so
    # EngineConfig snapshots and restores it exactly like the other keys.
    "BAKE_DENSITY_SCALE",
    "DEVICE",
    "GPU_DEVICE_ID",
    "OUTPUT_PROFILE",
    "TARGET_PROFILE",
    "ROBLOX_TEXTURE_LIMIT",
    "NATIVE_QUALITY",
    "ROBLOX_MATERIAL_FIT",
    "ROBLOX_FIT_SETTINGS",
    "ROBLOX_GEOMETRY",
    "ROBLOX_GEOMETRY_SETTINGS",
    "ALLOW_CPU_FALLBACK",
    "ALLOW_APPROXIMATION",
    "SOURCE_DIRS",
    "ROUTE_MODE",
    "SOURCE_NORMAL_IS_DIRECTX",
    "EXPORT_GLTF",
    "EXPORT_FBX",
    "AUTO_UNWRAP_NO_UV",
    "DONE_LIST",
    "DURABLE_CHECKPOINTS",
    "TRI_BUDGET",
    "ENFORCE_TRI_BUDGET",
    "CROP_SPLIT_PREPASS",
    "FINALIZE_IN_SESSION",
    "VISUAL_VALIDATION",
    "VISUAL_VALIDATION_GATE",
    "VISUAL_VALIDATION_SETTINGS",
)


def _split_directories(value: str) -> List[str]:
    directories = []
    for line in value.replace("\r", "\n").split("\n"):
        for item in line.split(";"):
            item = item.strip()
            if item:
                directories.append(os.path.abspath(bpy.path.abspath(item)))
    return directories


def _output_root(settings: Any) -> Optional[str]:
    value = settings.output_root.strip()
    return os.path.abspath(bpy.path.abspath(value)) if value else None


def _precheck_directory(settings: Any) -> str:
    blend_path = os.path.abspath(bpy.data.filepath)
    root = _output_root(settings) or os.path.dirname(blend_path)
    stem = os.path.splitext(os.path.basename(blend_path))[0] or "scene"
    return os.path.join(root, stem + "_exports", "precheck")


def _walk_layer_collections(
    layer_collection: Any, parent_path: Tuple[int, ...] = ()
) -> Iterable[Tuple[Tuple[int, ...], Any]]:
    """Yield stable collection-pointer paths plus their current RNA handles."""
    path = parent_path + (int(layer_collection.collection.as_pointer()),)
    yield path, layer_collection
    for child in layer_collection.children:
        yield from _walk_layer_collections(child, path)


def _object_left_scene(obj: Any, scene: Any) -> bool:
    """True only when ``obj`` is no longer linked anywhere under ``scene``.

    Absent from view_layer.objects is cosmetic -- an exclusion we could not put
    back.  Absent from scene.objects is not: finalize_session() runs
    orphans_purge(do_recursive=True) and then overwrites the user's .blend, so
    an object that left the scene is about to be deleted for good.
    """
    try:
        found = scene.objects.get(obj.name)
        return found is None or int(found.as_pointer()) != int(obj.as_pointer())
    except (ReferenceError, RuntimeError, AttributeError):
        return True


def _collection_left_scene(collection: Any, scene: Any) -> bool:
    """True only when ``collection`` is no longer linked under ``scene``.

    _walk_layer_collections() keys on the ancestor-pointer chain, so a
    collection Blender re-parents mid-run reads as "disappeared" while its
    data-block is untouched.  Same orphan-purge reasoning as the object case.
    """
    try:
        if collection == scene.collection:
            return False
        return collection not in scene.collection.children_recursive
    except (ReferenceError, RuntimeError, AttributeError):
        return True


class ContextState:
    """Restore UI state before optional finalization saves the blend."""

    def __init__(self, context: Any, touched_objects: Iterable[Any] = ()):
        self.context = context
        self.view_layer = context.view_layer
        self.active = self.view_layer.objects.active
        self.selected = list(context.selected_objects)
        self.mode = context.mode
        self.restored = False
        self.scene = context.scene
        # Cosmetic restore misses land here instead of raising: a bake whose
        # maps are already on disk must not be graded FAILED because Blender
        # refused to put a viewport flag back.  See restore().
        self.warnings: List[str] = []
        self.layer_flags: List[Tuple[Tuple[int, ...], str, bool, bool]] = []
        self.collection_flags: Dict[int, Tuple[Any, bool]] = {}
        for path, layer in _walk_layer_collections(self.view_layer.layer_collection):
            self.layer_flags.append(
                (path, layer.name, bool(layer.exclude), bool(layer.hide_viewport))
            )
            collection = layer.collection
            self.collection_flags[int(collection.as_pointer())] = (
                collection,
                bool(collection.hide_viewport),
            )
        self.object_flags: List[Tuple[Any, bool, bool]] = []
        tracked = {int(obj.as_pointer()): obj for obj in touched_objects}
        tracked.update({int(obj.as_pointer()): obj for obj in self.selected})
        if self.active is not None:
            tracked[int(self.active.as_pointer())] = self.active
        for obj in tracked.values():
            self.object_flags.append((obj, bool(obj.hide_get()), bool(obj.hide_viewport)))

    def restore(self, force: bool = False) -> None:
        """Put the UI back.  Raise ONLY when the scene is unsafe to save.

        ``errors`` collects cosmetic misses -- visibility flags, selection,
        active object, the return to the user's original mode.  None can lose
        data: an excluded or eye-hidden collection is still linked, so
        orphans_purge() keeps it and its objects (verified on 5.1.2 and
        5.2.0), and every one of them is a single click to undo.  ``fatal``
        collects the two states that are NOT recoverable once
        finalize_session() purges orphans and overwrites the .blend: we could
        not reach Object mode, or tracked content left the scene entirely.
        Only ``fatal`` raises, because raising is what blocks finalize.
        """
        if self.restored and not force:
            return
        errors: List[str] = []
        fatal: List[str] = []
        if bpy.ops.object.mode_set.poll():
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception as exc:
                # Object mode is a precondition for every RNA write below and
                # for the finalize save; nothing after this can be trusted.
                fatal.append("leave current mode: %s" % exc)
        try:
            self.view_layer.update()
        except Exception as exc:
            fatal.append("update view layer before restore: %s" % exc)
        current_layers = dict(_walk_layer_collections(self.view_layer.layer_collection))
        # A forced final restore starts after the pre-finalize restore, when the
        # source may already be excluded again.  Make every originally tracked
        # layer reachable before restoring object-local hide/selection state.
        for path, name, _exclude, _hidden in self.layer_flags:
            layer = current_layers.get(path)
            if layer is None:
                continue
            try:
                layer.exclude = False
                layer.hide_viewport = False
            except Exception as exc:
                errors.append("make layer reachable %s: %s" % (name, exc))
        for collection, _hidden in self.collection_flags.values():
            try:
                collection.hide_viewport = False
            except Exception as exc:
                errors.append("make collection reachable %s: %s" % (collection.name, exc))
        try:
            self.view_layer.update()
        except Exception as exc:
            errors.append("update reachable view layer: %s" % exc)
        current_layers = dict(_walk_layer_collections(self.view_layer.layer_collection))
        # Keep collections reachable until selection, active object, and mode
        # are restored. Re-applying an original parent exclusion first makes
        # its objects disappear from view_layer.objects, so hide_set/select_set
        # cannot restore their per-view-layer state afterward.
        try:
            bpy.ops.object.select_all(action="DESELECT")
        except Exception as exc:
            fallback_error = None
            for obj in self.view_layer.objects:
                try:
                    obj.select_set(False)
                except Exception as inner:
                    fallback_error = inner
            if fallback_error is not None:
                errors.append("clear selection: %s / %s" % (exc, fallback_error))
        for obj in self.selected:
            try:
                if obj.name in self.view_layer.objects:
                    obj.select_set(True)
                elif _object_left_scene(obj, self.scene):
                    fatal.append("selected object left the scene: %s" % obj.name)
                else:
                    errors.append("selected object left active view layer: %s" % obj.name)
            except Exception as exc:
                # obj may already be a freed struct here, so never touch
                # obj.name in the handler -- that re-raises out of restore().
                errors.append("restore selection: %s" % exc)
        try:
            if self.active is not None and self.active.name in self.view_layer.objects:
                self.view_layer.objects.active = self.active
            elif self.active is not None and _object_left_scene(self.active, self.scene):
                fatal.append("active object left the scene: %s" % self.active.name)
            elif self.active is not None:
                errors.append("active object left active view layer: %s" % self.active.name)
        except Exception as exc:
            errors.append("restore active object: %s" % exc)
        for obj, hidden, hidden_global in self.object_flags:
            try:
                obj.hide_viewport = False
            except Exception as exc:
                errors.append("make object reachable %s: %s" % (obj.name, exc))
            try:
                obj.hide_set(hidden)
            except Exception as exc:
                errors.append("object local visibility %s: %s" % (obj.name, exc))
            try:
                obj.hide_viewport = hidden_global
            except Exception as exc:
                errors.append("object global visibility %s: %s" % (obj.name, exc))
        for collection, hidden in self.collection_flags.values():
            try:
                collection.hide_viewport = hidden
            except Exception as exc:
                errors.append("collection %s: %s" % (collection.name, exc))
        for path, name, exclude, _hidden in reversed(self.layer_flags):
            layer = current_layers.get(path)
            if layer is None:
                # ``path`` is the ancestor-pointer chain, so re-parenting a
                # collection during the run reads as "disappeared" although
                # the data-block is intact (reproduced on 5.1.2 and 5.2.0).
                # Only an unlinked collection is fatal -- purge would eat it.
                tracked = self.collection_flags.get(path[-1]) if path else None
                if tracked is None or _collection_left_scene(tracked[0], self.scene):
                    fatal.append("collection left the scene: %s" % name)
                else:
                    errors.append("layer collection was re-parented: %s" % name)
                continue
            try:
                layer.exclude = exclude
            except Exception as exc:
                errors.append("layer collection exclusion %s: %s" % (name, exc))
        # Blender may clear the eye flag while changing exclusion.  Object state
        # is already restored, so reapply per-layer visibility after exclusions.
        for path, name, _exclude, hidden in self.layer_flags:
            layer = current_layers.get(path)
            if layer is None:
                continue
            try:
                layer.hide_viewport = hidden
            except Exception as exc:
                errors.append("layer collection visibility %s: %s" % (name, exc))
        # On Blender 5.x, changing an eye-hidden layer that was made reachable
        # for processing can clear its exclusion in some populated scenes.
        # Reassert exclusion once after visibility; both flags then settle.
        for path, name, exclude, _hidden in reversed(self.layer_flags):
            layer = current_layers.get(path)
            if layer is None:
                continue
            try:
                layer.exclude = exclude
            except Exception as exc:
                errors.append("final layer exclusion %s: %s" % (name, exc))
        # Changing LayerCollection.exclude while Blender is in Edit/Paint modes
        # can be accepted by RNA yet silently revert on dependency-graph update.
        # Restore the user's mode only after every collection flag has settled.
        mode_map = {
            "POSE": "POSE",
            "SCULPT": "SCULPT",
            "SCULPT_CURVES": "SCULPT_CURVES",
            "PAINT_WEIGHT": "WEIGHT_PAINT",
            "PAINT_VERTEX": "VERTEX_PAINT",
            "PAINT_TEXTURE": "TEXTURE_PAINT",
            "PARTICLE": "PARTICLE_EDIT",
            "EDIT_GREASE_PENCIL": "EDIT",
            "PAINT_GREASE_PENCIL": "PAINT_GREASE_PENCIL",
            "SCULPT_GREASE_PENCIL": "SCULPT_GREASE_PENCIL",
            "WEIGHT_GREASE_PENCIL": "WEIGHT_GREASE_PENCIL",
            "VERTEX_GREASE_PENCIL": "VERTEX_GREASE_PENCIL",
        }
        mode = "EDIT" if self.mode.startswith("EDIT_") else mode_map.get(self.mode)
        if self.mode != "OBJECT":
            if mode is None:
                errors.append("unsupported original mode: %s" % self.mode)
            elif not bpy.ops.object.mode_set.poll():
                errors.append("cannot restore original mode: %s" % self.mode)
            else:
                try:
                    bpy.ops.object.mode_set(mode=mode)
                except Exception as exc:
                    errors.append("restore mode %s: %s" % (self.mode, exc))
        # Mode changes and newly linked output collections can rebuild the
        # LayerCollection tree.  Resolve the fresh handles, then apply the
        # original flags once more after that rebuild has completed.
        try:
            self.view_layer.update()
        except Exception as exc:
            errors.append("update view layer after mode restore: %s" % exc)
        current_layers = dict(_walk_layer_collections(self.view_layer.layer_collection))
        for path, name, exclude, hidden in self.layer_flags:
            layer = current_layers.get(path)
            if layer is None:
                continue
            try:
                layer.hide_viewport = hidden
                layer.exclude = exclude
            except Exception as exc:
                errors.append("settle layer flags %s: %s" % (name, exc))
        try:
            self.view_layer.update()
        except Exception as exc:
            errors.append("verify view layer update: %s" % exc)
        current_layers = dict(_walk_layer_collections(self.view_layer.layer_collection))
        for path, name, exclude, hidden in self.layer_flags:
            layer = current_layers.get(path)
            if layer is None:
                continue
            try:
                if bool(layer.exclude) != exclude or bool(layer.hide_viewport) != hidden:
                    errors.append(
                        "layer flags did not verify: %s expected=(%s,%s) current=(%s,%s)"
                        % (
                            name, exclude, hidden,
                            bool(layer.exclude), bool(layer.hide_viewport),
                        )
                    )
            except Exception as exc:
                errors.append("verify layer %s: %s" % (name, exc))
        for collection, hidden in self.collection_flags.values():
            try:
                if bool(collection.hide_viewport) != hidden:
                    errors.append("collection flag did not verify: %s" % collection.name)
            except Exception as exc:
                errors.append("verify collection %s: %s" % (collection.name, exc))
        for obj, hidden, hidden_global in self.object_flags:
            try:
                if bool(obj.hide_get()) != hidden or bool(obj.hide_viewport) != hidden_global:
                    errors.append("object visibility did not verify: %s" % obj.name)
            except Exception as exc:
                errors.append("verify object %s: %s" % (obj.name, exc))
        try:
            expected_selected = {int(obj.as_pointer()) for obj in self.selected}
            current_selected = {int(obj.as_pointer()) for obj in self.context.selected_objects}
            if current_selected != expected_selected:
                errors.append("selection did not verify")
            if self.view_layer.objects.active != self.active:
                errors.append("active object did not verify")
            if self.context.mode != self.mode:
                errors.append("mode did not verify: %s != %s" % (self.context.mode, self.mode))
        except Exception as exc:
            errors.append("verify active selection/mode: %s" % exc)
        self.warnings = errors
        if fatal:
            # Raising is what stops engine.main() from finalizing: the blend
            # must not be packed, purged, and overwritten from a scene we
            # could not put back safely.
            raise RuntimeError("; ".join(fatal[:12]))
        self.restored = True


class EngineConfig:
    def __init__(self, settings: Any):
        self.settings = settings
        self.original = {name: getattr(engine, name) for name in ENGINE_CONFIG_KEYS}

    def __enter__(self) -> "EngineConfig":
        settings = self.settings
        values = {
            "OUTPUT_ROOT": _output_root(settings),
            "RES": int(settings.bake_resolution),
            "MAX_RES": int(settings.max_crop_resolution),
            "BAKE_DENSITY_SCALE": float(settings.bake_density_scale),
            "DEVICE": settings.device,
            "GPU_DEVICE_ID": settings.gpu_device_id.strip(),
            "OUTPUT_PROFILE": settings.output_profile,
            "TARGET_PROFILE": settings.target_profile,
            "ROBLOX_TEXTURE_LIMIT": int(settings.roblox_texture_limit),
            "ROBLOX_MATERIAL_FIT": settings.roblox_material_fit,
            "ROBLOX_FIT_SETTINGS": {"max_candidates": settings.roblox_fit_candidates,
                                    "max_seconds": settings.roblox_fit_seconds},
            "ROBLOX_GEOMETRY": settings.roblox_geometry,
            "ROBLOX_GEOMETRY_SETTINGS": {"subdivisions": settings.roblox_geometry_subdivisions,
                                         "max_triangles": settings.roblox_geometry_triangles},
            "NATIVE_QUALITY": {"absolute_tolerance": float(settings.native_absolute_tolerance),
                               "relative_tolerance": float(settings.native_relative_tolerance),
                               "initial_resolution": settings.native_initial_resolution,
                               "margin_px": settings.native_margin_pixels},
            "ALLOW_CPU_FALLBACK": settings.allow_cpu_fallback,
            "ALLOW_APPROXIMATION": settings.allow_approximation,
            "SOURCE_DIRS": _split_directories(settings.source_directories),
            "ROUTE_MODE": settings.route_mode,
            "SOURCE_NORMAL_IS_DIRECTX": settings.source_normal_is_directx,
            "EXPORT_GLTF": settings.export_glb,
            "EXPORT_FBX": settings.export_fbx,
            "AUTO_UNWRAP_NO_UV": settings.auto_unwrap_no_uv,
            "DONE_LIST": settings.resume_enabled,
            "DURABLE_CHECKPOINTS": settings.resume_enabled and settings.durable_checkpoints,
            "TRI_BUDGET": settings.triangle_budget,
            "ENFORCE_TRI_BUDGET": settings.enforce_triangle_budget,
            "CROP_SPLIT_PREPASS": settings.crop_split_prepass,
            "FINALIZE_IN_SESSION": settings.finalize_in_session,
            "VISUAL_VALIDATION": settings.visual_validation,
            "VISUAL_VALIDATION_GATE": settings.visual_validation and settings.visual_validation_gate,
            "VISUAL_VALIDATION_SETTINGS": {"resolution": int(settings.visual_resolution),
                                           "samples": int(settings.visual_samples),
                                           "probe_resolution": ("auto" if settings.visual_probe == "AUTO"
                                                                else int(settings.visual_probe))},
        }
        for name, value in values.items():
            setattr(engine, name, value)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for name, value in self.original.items():
            setattr(engine, name, value)


def _source_objects(context: Any, settings: Any) -> List[Any]:
    if settings.scope == "SELECTED":
        source = list(context.selected_objects)
    elif settings.scope == "COLLECTION":
        if settings.source_collection is None:
            return []
        source = list(settings.source_collection.all_objects)
    else:
        source = list(context.scene.objects)
    result = []
    seen = set()
    for obj in source:
        pointer = int(obj.as_pointer())
        if pointer in seen or obj.type != "MESH" or (not obj.data.polygons and settings.target_profile == "LEGACY"):
            continue
        if engine.is_previous_output(obj):
            continue
        seen.add(pointer)
        result.append(obj)
    return result


def _select_for_engine(context: Any, objects: Iterable[Any]) -> List[Any]:
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    engine.unexclude_all_collections(context.view_layer)
    bpy.ops.object.select_all(action="DESELECT")
    selected = []
    for obj in objects:
        try:
            if obj.name not in context.view_layer.objects:
                continue
            obj.hide_viewport = False
            obj.hide_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            selected.append(obj)
        except (ReferenceError, RuntimeError) as exc:
            print("Import Glue: could not select %s (%s)" % (obj.name, exc))
    return selected


def _set_status(
    settings: Any, text: str, *, output: Optional[str] = None, report: Optional[str] = None
) -> None:
    settings.last_status = text
    if output is not None:
        settings.last_output = output
    if report is not None:
        settings.last_report = report


CAPABILITY_REPORT_NAME = "material_capabilities.json"


def _capability_preflight(settings: Any, objects: Iterable[Any]) -> Tuple[Dict[str, Any], str]:
    """Read-only capability report beside the precheck report, plus panel text.

    Returns (report, write_error).  It never changes routing, materials or
    image paths; a report that cannot be written is said so, not hidden
    (write_report raises only OSError, encoding failures included).
    """
    profile = "ROBLOX" if settings.target_profile in {"ROBLOX", "BOTH"} else engine.CAPABILITY_PROFILE
    report = material_capabilities.preflight_report(
        list(objects), profile, bool(settings.allow_approximation))
    lines = material_capabilities.summary_lines(report, limit=5)
    error = ""
    try:
        material_capabilities.write_report(
            report, os.path.join(_precheck_directory(settings), CAPABILITY_REPORT_NAME))
    except OSError as exc:
        error = str(exc)
        lines.append("Report not written: %s" % exc)
    settings.last_capability = material_capabilities.summary_text(report)
    if settings.target_profile != "LEGACY":
        settings.last_capability = "Portable PBR assessment: " + settings.last_capability
        lines.append("Native closure/field eligibility is assessed separately during its run.")
    settings.last_capability_lines = "\n".join(lines)
    return report, error


def _visual_note(census: Dict[str, Any]) -> str:
    """Status suffix such as ' | visual: FAIL 1, PASS 2'; empty when nothing was compared."""
    visual = census.get("visual") or {}
    if not visual:
        return ""
    return " | visual: " + ", ".join("%s %d" % row for row in sorted(visual.items()))


def _completion_status(census: Dict[str, Any]) -> str:
    """Final status for a run without failures; approximated outputs are named.

    A run that converted any object under the approximation opt-in is not
    reported as "Clean": its outputs are not faithful conversions.  Parts
    restored from durable checkpoints and the visual verdicts are named too.
    """
    if census.get("target_results"):
        return "Delivered %d target(s), %d explicit approximation(s); see field report" % (
            census.get("ok", 0), census.get("approximated", 0))
    approximated = int(census.get("approximated") or 0)
    restored = int(census.get("checkpoint_restored") or 0)
    if approximated:
        status = ("Completed: %d object(s), %d APPROXIMATED under the explicit opt-in "
                  "(not faithful; see manifest)" % (census.get("ok", 0), approximated))
    else:
        status = "Clean: %d object(s) completed" % census.get("ok", 0)
    if restored:
        status += " (%d restored from checkpoints)" % restored
    return status + _visual_note(census)


def _failure_status(census: Dict[str, Any]) -> str:
    """Final status for a run whose census failed, with any visual verdicts."""
    if census.get("status") == "PARTIAL":
        return "Partial delivery: completed target files retained; see delivery manifest"
    return "Finished with failures: %d failed, %d skipped%s" % (
        census.get("failed", 0), census.get("skipped", 0), _visual_note(census))


def _refresh_capability_summary(operator: Any, settings: Any, objects: Iterable[Any]) -> None:
    """Advisory panel refresh before a Run: it may warn, never cancel the Run.

    The engine applies the capability policy itself; a summary that cannot
    be computed or written must not stop the bake.
    """
    try:
        _capability_preflight(settings, objects)
    except Exception as exc:
        traceback.print_exc()
        settings.last_capability = "Material capability summary unavailable: %s" % exc
        settings.last_capability_lines = ""
        operator.report({"WARNING"}, settings.last_capability)


class IMPORTGLUE_PG_settings(PropertyGroup):
    background_execution: BoolProperty(
        name="Bake in Background", default=True,
        description="Save a private snapshot and bake in a separate Blender process")
    gpu_device_id: StringProperty(
        name="GPU Name or Cycles ID",
        description="Empty selects one GPU automatically; an explicit identity must match")
    target_profile: EnumProperty(
        name="Delivery Target",
        items=(("BOTH", "Blender + Roblox", "Separate native material library and Roblox five-map package"),
               ("BLENDER_NATIVE", "Blender Native", "Preserve closures and bake eligible static input fields"),
               ("ROBLOX", "Roblox", "Five PNG maps, FBX, measured emission and SurfaceAppearance bindings"),
               ("LEGACY", "Legacy Four Maps", "Existing four-map routes and durable checkpoints")),
        default="BOTH",
    )
    roblox_texture_limit: EnumProperty(
        name="Roblox Texture Preset",
        items=(("512", "512", ""), ("1024", "1024", "Conservative delivery preset"),
               ("2048", "2048", "Verify target import support in Studio"),
               ("4096", "4096", "Verify target import support in Studio")),
        default="1024",
    )
    output_profile: EnumProperty(
        name="Output Precision",
        items=(("PBR_BASE", "Standard PNG 8-bit", "Existing four-map contract"),
               ("PBR_HIGH_PRECISION", "PNG 16-bit", "Float graph bake and true 16-bit output")),
        default="PBR_BASE")
    resource_enforce: BoolProperty(
        name="Require Resource Headroom", default=False,
        description="Reject unknown or insufficient free RAM/VRAM using a conservative planning estimate")
    resource_reserve: FloatProperty(name="Free Memory Reserve", default=0.2, min=0, max=0.8,
                                    subtype="FACTOR")
    last_job_dir: StringProperty(name="Background Job", subtype="DIR_PATH")
    last_job_state: StringProperty(name="Worker State")
    uv_mip_level: IntProperty(name="UV Mip Level", default=0, min=0, max=12)
    uv_margin_pixels: FloatProperty(name="UV Margin Pixels", default=2, min=0, max=64)
    last_uv_report: StringProperty(name="UV Quality Report", subtype="FILE_PATH")
    last_target_preflight: StringProperty(name="Target Preflight Report", subtype="FILE_PATH")
    last_target_summary: StringProperty(name="Target Preflight Summary")
    last_target_result_summary: StringProperty(name="Target Result Summary")
    native_absolute_tolerance: FloatProperty(name="Native Absolute Error", default=0.025, min=0.0, max=1.0,
        description="Maximum sampled error allowance added to the relative allowance; failed fields remain native")
    native_relative_tolerance: FloatProperty(name="Native Relative Error", default=0.01, min=0.0, max=1.0,
        description="Relative component of the finite reference comparison; not a mathematical error bound")
    native_initial_resolution: IntProperty(name="Adaptive Starting Resolution", default=128, min=16, max=4096)
    native_margin_pixels: IntProperty(name="Native Seam Padding", default=8, min=1, max=32,
        description="Pad uncovered texels without overwriting other UV islands")
    roblox_material_fit: BoolProperty(name="Fit Roblox Material", default=False,
        description="Measure roughness/metalness candidates against source renders; requires Allow Approximation")
    roblox_fit_candidates: IntProperty(name="Fit Candidate Budget", default=12, min=1, max=32)
    roblox_fit_seconds: FloatProperty(name="Fit Time Budget (seconds)", default=300, min=10, max=3600,
        description="Checked between renders; a Blender render cannot be preempted by this budget")
    roblox_geometry: BoolProperty(name="Capture Static Displacement", default=False,
        description="Convert supported displacement to private mesh geometry within a triangle budget; requires Allow Approximation")
    roblox_geometry_subdivisions: IntProperty(name="Displacement Subdivisions", default=2, min=0, max=6)
    roblox_geometry_triangles: IntProperty(name="Displacement Triangle Budget", default=20000, min=1, max=1000000)
    scope: EnumProperty(
        name="Scope",
        items=(
            ("SELECTED", "Selected", "Bake selected source meshes"),
            ("ALL", "All Scene Meshes", "Bake every source mesh in the current scene"),
            ("COLLECTION", "Collection", "Bake meshes in one collection"),
        ),
        default="SELECTED",
    )
    source_collection: PointerProperty(name="Collection", type=bpy.types.Collection)
    output_root: StringProperty(
        name="Output Root",
        subtype="DIR_PATH",
        description="Empty writes beside the saved .blend",
    )
    bake_resolution: EnumProperty(
        name="Bake Resolution",
        items=(("512", "512", ""), ("1024", "1024", "Conservative Roblox preset"),
               ("2048", "2048", ""), ("4096", "4096", ""), ("8192", "8192", "")),
        default="1024",
    )
    max_crop_resolution: EnumProperty(
        name="Crop Ceiling",
        items=(("512", "512", ""), ("1024", "1024", "Conservative Roblox preset"),
               ("2048", "2048", ""), ("4096", "4096", ""), ("8192", "8192", "")),
        default="1024",
    )
    bake_density_scale: FloatProperty(
        name="Bake Density",
        default=1.0,
        min=0.05, max=8.0,
        soft_min=0.25, soft_max=4.0,
        step=5, precision=2,
        description=(
            "Texel-density multiplier for the Proxy and Graph bake routes. Bake "
            "Resolution is only a ceiling: the size those routes ask for is "
            "sqrt(uv_area) x 4096 x this value, snapped up to a power of two and "
            "then capped. 2.0 is twice the linear density (4x the texels); it "
            "raises nothing already sitting on the ceiling"
        ),
    )
    device: EnumProperty(
        name="Cycles Device",
        items=(("GPU", "GPU", "Use OptiX/CUDA; fail unless CPU fallback is enabled"),
               ("CPU", "CPU", "Bake on the CPU")),
        default="GPU",
    )
    allow_cpu_fallback: BoolProperty(
        name="Allow CPU Fallback",
        default=False,
        description="Continue on CPU when no Cycles GPU is available; this can take many hours",
    )
    allow_approximation: BoolProperty(
        name="Allow Approximated Materials",
        default=False,
        description=(
            "Convert materials whose effects the four maps cannot hold (transmission, coat, "
            "view-dependent inputs, closure mixes...). Outputs are labelled APPROXIMATED "
            "with every reason in the manifest; blocked materials are never converted"
        ),
    )
    route_mode: EnumProperty(
        name="Route",
        items=(
            ("AUTO", "Auto", "Crop when exact; otherwise proxy or graph bake"),
            ("CROP_ONLY", "Crop Only", "Only direct atlas crops"),
            ("PROXY_ONLY", "Proxy Only", "Only proxy-material bakes"),
            ("GRAPH_ONLY", "Graph Only", "Always bake original shader graphs"),
            ("BAKE_ONLY", "Bake Only", "Proxy when resolved, graph otherwise"),
        ),
        default="AUTO",
    )
    source_normal_is_directx: BoolProperty(
        name="Source Normals Are DirectX (-Y)", default=True,
        description="Flip the green channel for Roblox/OpenGL output",
    )
    source_directories: StringProperty(
        name="Extra Texture Folders",
        description="Optional absolute paths separated by semicolons or new lines",
    )
    precheck_enabled: BoolProperty(name="Run Texture Precheck", default=True)
    # v1.2.1: default OFF, to match the headless entry point. The GUI used to
    # default this ON and forward it from BOTH "Check Textures" and "Run", so a
    # plain precheck WROTE INTO THE SOURCE SCENE DIRECTORY -- measured
    # 2026-09-17: running the precheck operator on a scene with a missing
    # relative texture created "textures/fixture_missing.png" beside the user's
    # .blend. Repairing a source tree is a deliberate action, not a side effect
    # of checking it. Ordinary conversion resolves into the staging copy
    # instead (repair_into_staging below).
    repair_missing: BoolProperty(
        name="Repair Missing Textures In The Source Folder", default=False,
        description=(
            "WRITES INTO THE SOURCE SCENE FOLDER: hardlink, symlink or copy a "
            "uniquely matched texture next to the .blend. Leave off unless you "
            "intend to modify the source tree"
        ),
    )
    repair_into_staging: BoolProperty(
        name="Resolve Missing Textures For This Run", default=True,
        description=(
            "Find missing textures in the pool and repoint this run at them "
            "without writing anything into the source scene folder"
        ),
    )
    repair_mode: EnumProperty(
        name="Repair Method",
        items=(
            ("AUTO", "Auto", "Try hardlink, symlink, then copy"),
            ("COPY", "Copy", "Copy texture files"),
            ("HARDLINK", "Hardlink", "Hardlink only; must be on one compatible volume"),
            ("SYMLINK", "Symlink", "Symlink only; may require Windows Developer Mode"),
        ),
        default="AUTO",
    )
    precheck_pool: StringProperty(
        name="Shared Texture Pool", subtype="DIR_PATH",
        description="Optional folder searched after the blend directory",
    )
    allow_missing: BoolProperty(name="Allow Missing Textures", default=False)
    allow_corrupt: BoolProperty(name="Allow Corrupt Textures", default=False)
    # v1.2.1: default ON, matching headless.py, which has treated an
    # unresolved dead absolute path as fatal since the source-preservation
    # patch. A dead absolute reference cannot be repaired into the scene folder
    # and bakes as Blender's magenta placeholder, so continuing produces a
    # confidently wrong map.
    strict_absolute: BoolProperty(
        name="Block Missing Absolute Paths", default=True,
        description="Treat unresolved dead absolute image paths as fatal",
    )
    resume_enabled: BoolProperty(
        name="Resume from Done List",
        default=True,
        description=(
            "Skip objects a previous run already finished, tracked in the "
            "%s file kept beside the output folder. An object that failed %d times "
            "in a row is skipped too, until Rerun Completed Objects clears its strikes"
            % (engine.DONE_LIST_NAME, engine.POISON_STRIKES)
        ),
    )
    durable_checkpoints: BoolProperty(
        name="Durable Checkpoints",
        default=True,
        description=(
            "Save every completed part (its output object and four maps) as a verified "
            "checkpoint beside the done list, so a reopened original file or a crashed "
            "run restores finished parts instead of baking them again. Costs one extra "
            "copy of each part's maps on disk"
        ),
    )
    rerun_completed: BoolProperty(
        name="Rerun Completed Objects",
        default=False,
        description=(
            "Rebake objects the done list already marks OK, and clear the failure "
            "strikes that would otherwise keep a repeatedly-failed object skipped"
        ),
    )
    auto_unwrap_no_uv: BoolProperty(name="Auto-Unwrap Missing UVs", default=True)
    crop_split_prepass: BoolProperty(name="Split Multi-Material Crops", default=True)
    enforce_triangle_budget: BoolProperty(
        name="Require Roblox Triangle Limit",
        default=True,
        description="Decimate Graph bakes and reject any route still over the triangle limit",
    )
    triangle_budget: IntProperty(name="Triangle Limit", default=20000, min=100, max=1000000)
    safe_batch_limit: IntProperty(
        name="Safe Batch Limit",
        default=16,
        min=1,
        max=10000,
        description="Block larger interactive batches unless explicitly overridden",
    )
    allow_oversized_batch: BoolProperty(
        name="Allow Oversized Batch",
        default=False,
        description="Permit an interactive run larger than the safe batch limit",
    )
    export_glb: BoolProperty(name="Export GLB", default=False)
    export_fbx: BoolProperty(name="Export FBX", default=False)
    finalize_in_session: BoolProperty(
        name="Pack, Purge, and Save .blend", default=False,
        description="Pack all images, purge orphans, and overwrite the open blend after a clean run",
    )
    visual_validation: BoolProperty(
        name="Save Visual Comparison", default=False,
        description=(
            "Render each converted part beside its source and save reference, output and "
            "difference images with a PASS / FAIL / INSUFFICIENT_COVERAGE / "
            "UNSUPPORTED_REFERENCE report in the output folder. Adds Cycles renders per part"
        ),
    )
    visual_validation_gate: BoolProperty(
        name="Require Visual PASS (experimental)", default=False,
        description=(
            "EXPERIMENTAL: treat every non-PASS visual verdict as a failure and block the finalize "
            "save. The render thresholds are calibrated on synthetic fixtures only; recalibrate them "
            "on your asset class before relying on this gate"
        ),
    )
    visual_resolution: IntProperty(name="Visual Resolution", default=128, min=32, max=1024)
    visual_samples: IntProperty(name="Visual Samples", default=64, min=1, max=4096)
    visual_probe: EnumProperty(
        name="Visual Probe",
        description=(
            "Surface probe resolution. Auto sizes it from each part's layout (256 to 2048); a "
            "too-small probe is reported as INSUFFICIENT_COVERAGE with the size it needs"
        ),
        items=[("AUTO", "Auto", "Smallest of 256-2048 the layout predicts is enough"),
               ("256", "256", ""), ("512", "512", ""), ("1024", "1024", ""),
               ("2048", "2048", "")],
        default="AUTO",
    )
    show_advanced: BoolProperty(name="Advanced", default=False)
    last_status: StringProperty(name="Last Status")
    last_output: StringProperty(name="Last Output")
    last_report: StringProperty(name="Last Report")
    last_capability: StringProperty(name="Last Capability Summary")
    last_capability_lines: StringProperty(name="Last Capability Findings")


class IMPORTGLUE_OT_precheck(Operator):
    bl_idname = "import_glue.precheck"
    bl_label = "Check Textures"
    bl_description = "Scan material textures and optionally repair missing relative references"

    def execute(self, context: Any):
        ensure_migrated(context.scene)
        settings = context.scene.import_glue_settings
        _set_status(settings, "Checking textures...", output="", report="")
        if not bpy.data.filepath:
            _set_status(settings, "Precheck error: save the .blend first")
            self.report({"ERROR"}, "Save the .blend before running the precheck")
            return {"CANCELLED"}
        targets = _source_objects(context, settings)
        if not targets:
            _set_status(settings, "Precheck error: no source meshes in scope")
            self.report({"ERROR"}, "No non-empty source meshes in the chosen scope")
            return {"CANCELLED"}
        try:
            result = precheck.run(
                objects=targets,
                pool=os.path.abspath(bpy.path.abspath(settings.precheck_pool))
                if settings.precheck_pool.strip() else "",
                marker_dir=_precheck_directory(settings),
                repair=settings.repair_missing,
                repair_unused=False,
                repair_into_staging=settings.repair_into_staging,
                repair_mode=settings.repair_mode,
                allow_missing=settings.allow_missing,
                allow_corrupt=settings.allow_corrupt,
                strict_absolute=settings.strict_absolute,
                raise_on_failure=False,
            )
        except Exception as exc:
            traceback.print_exc()
            _set_status(settings, "Precheck error: %s" % exc)
            self.report({"ERROR"}, "Texture precheck failed: %s" % exc)
            return {"CANCELLED"}
        try:
            capability, capability_error = _capability_preflight(settings, targets)
        except Exception as exc:
            traceback.print_exc()
            capability, capability_error = None, str(exc)
            settings.last_capability = "Material capability check failed: %s" % exc
            settings.last_capability_lines = ""
        summary = "Precheck clean" if result["ok"] else "Precheck blocked (code %d)" % result["code"]
        if settings.visual_validation:
            # Report only: which used materials the visual check can reference.
            # Advisory, like the capability summary: it may say it failed, but
            # it never changes the texture precheck's verdict.
            try:
                from .visual_validation import surface_reference_support
                support = surface_reference_support(targets)
                unsupported = sorted(name for name, row in support.items()
                                     if not row["supported"])
                summary += " | visual reference: %d supported, %d unsupported%s" % (
                    len(support) - len(unsupported), len(unsupported),
                    " (%s)" % ", ".join(unsupported[:3]) if unsupported else "")
            except Exception as exc:
                traceback.print_exc()
                summary += " | visual reference check failed: %s" % exc
        _set_status(settings, summary, report=result["report_path"])
        if capability_error:
            self.report({"WARNING"}, "Material capability report: %s" % capability_error)
        if capability is not None and not capability["policy"]["allowed"]:
            self.report({"WARNING"}, settings.last_capability
                        + "; see " + CAPABILITY_REPORT_NAME)
        if not result["ok"]:
            self.report({"ERROR"}, summary + "; see precheck report")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            "%s: %d repaired, %d repointed" %
            (summary, result["repaired"], result["repointed"]),
        )
        return {"FINISHED"}


class IMPORTGLUE_OT_run(Operator):
    bl_idname = "import_glue.run"
    bl_label = "Run Import Glue"
    bl_description = "Create Roblox-ready PBR maps and duplicate output meshes"

    def invoke(self, context: Any, event: Any):
        settings = context.scene.import_glue_settings
        if settings.finalize_in_session:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context: Any):
        ensure_migrated(context.scene)
        settings = context.scene.import_glue_settings
        _set_status(settings, "Preparing Import Glue...", output="", report="")
        if not bpy.data.filepath:
            _set_status(settings, "Import Glue error: save the .blend first")
            self.report({"ERROR"}, "Save the .blend before running Import Glue")
            return {"CANCELLED"}
        if int(settings.max_crop_resolution) < int(settings.bake_resolution):
            _set_status(settings, "Import Glue error: crop ceiling is below bake resolution")
            self.report({"ERROR"}, "Crop Ceiling must be at least Bake Resolution")
            return {"CANCELLED"}
        targets = _source_objects(context, settings)
        if not targets:
            _set_status(settings, "Import Glue error: no source meshes in scope")
            self.report({"ERROR"}, "No non-empty source meshes in the chosen scope")
            return {"CANCELLED"}
        if (
            len(targets) > int(settings.safe_batch_limit)
            and not settings.allow_oversized_batch
        ):
            message = (
                "Scope contains %d meshes; Safe Batch Limit is %d. "
                "Narrow the scope or enable Allow Oversized Batch in Advanced."
                % (len(targets), int(settings.safe_batch_limit))
            )
            _set_status(settings, "Import Glue blocked: oversized batch")
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        state = ContextState(context, targets)
        window = getattr(context, "window", None)
        if window is not None:
            try:
                window.cursor_set("WAIT")
            except Exception:
                pass
        _set_status(settings, "Running texture precheck...")
        # One owner for the verdict: the body earns a status, the final restore
        # may only downgrade it, and there is exactly one return.  The previous
        # contract returned a mutable set from inside try/ and then mutated
        # that same set in finally/ to override it.  That worked only because
        # CPython hands the caller the object rather than a copy, it would
        # silently stop working the moment any branch returned a set literal
        # like every other return in this file, and it could not express
        # "FINISHED with a warning" at all.
        status = "CANCELLED"
        try:
            status = self._run_bake(context, settings, state, targets)
        except (Exception, SystemExit) as exc:
            # SystemExit from any retained legacy path must never close Blender.
            if not isinstance(exc, SystemExit):
                traceback.print_exc()
            _set_status(settings, "Import Glue error: %s" % exc)
            self.report({"ERROR"}, settings.last_status)
            status = "CANCELLED"
        finally:
            try:
                if not self._finish_state(settings, state):
                    status = "CANCELLED"
            finally:
                engine.restore_log()
                if window is not None:
                    try:
                        window.cursor_set("DEFAULT")
                    except Exception:
                        pass
        return {status}

    def _run_bake(
        self, context: Any, settings: Any, state: "ContextState", targets: List[Any]
    ) -> str:
        """Precheck, bake, grade.  Returns the status the bake itself earned."""
        selected = _select_for_engine(context, targets)
        if not selected:
            raise RuntimeError("No source meshes could be selected in the active view layer")
        if settings.precheck_enabled:
            checked = precheck.run(
                objects=selected,
                pool=os.path.abspath(bpy.path.abspath(settings.precheck_pool))
                if settings.precheck_pool.strip() else "",
                marker_dir=_precheck_directory(settings),
                repair=settings.repair_missing,
                repair_unused=False,
                repair_into_staging=settings.repair_into_staging,
                repair_mode=settings.repair_mode,
                allow_missing=settings.allow_missing,
                allow_corrupt=settings.allow_corrupt,
                strict_absolute=settings.strict_absolute,
                raise_on_failure=False,
            )
            # v1.2.1: a repoint resolves a reference for THIS RUN only. Hand the
            # originals to the engine so finalize puts them back before it writes
            # the deliverable blend, instead of persisting the resolver's choice
            # of path into the user's own file.
            for name, original in checked.get("repointed_originals", []) or []:
                engine.note_repointed_filepath(name, original)
            settings.last_report = checked["report_path"]
            if not checked["ok"]:
                _set_status(settings, "Precheck blocked (code %d)" % checked["code"])
                self.report({"ERROR"}, settings.last_status + "; see precheck report")
                return "CANCELLED"

        # Refresh the panel's capability summary (advisory; the engine applies
        # the policy).  It can only warn, never cancel the Run.
        _refresh_capability_summary(self, settings, selected)
        _set_status(settings, "Baking %d source mesh(es)..." % len(selected))
        args: Dict[str, Any] = {"tag": "Blender add-on %s" % ADDON_VERSION}
        if not settings.resume_enabled:
            args["no-resume"] = True
        if settings.rerun_completed:
            args["rerun"] = True
        if not settings.finalize_in_session:
            args["no-finalize"] = True
        with EngineConfig(settings):
            census = engine.main(args_override=args, before_finalize=state.restore)
        if census is None:
            raise RuntimeError("The engine found no bakeable selected source meshes")
        manifest = census.get("manifest", "")
        output = os.path.dirname(manifest) if manifest else ""
        if census.get("exit_code"):
            _set_status(
                settings,
                _failure_status(census),
                output=output,
                report=manifest,
            )
            self.report({"ERROR"}, settings.last_status + "; see manifest")
            return "CANCELLED"
        _set_status(
            settings,
            _completion_status(census),
            output=output,
            report=manifest,
        )
        resume_warnings = census.get("resume_warnings") or []
        if resume_warnings:
            # Every object baked, but the done-list did not survive: without this the
            # next run silently re-bakes everything and the user never learns why.
            _set_status(
                settings,
                "%s (resume warning: %s)" % (settings.last_status, resume_warnings[0]),
                output=output,
                report=manifest,
            )
            self.report({"WARNING"}, settings.last_status)
        elif census.get("approximated"):
            self.report({"WARNING"}, settings.last_status)
        else:
            self.report({"INFO"}, settings.last_status)
        return "FINISHED"

    def _finish_state(self, settings: Any, state: "ContextState") -> bool:
        """Force the final restore.  False only when the scene is unsafe to leave."""
        try:
            state.restore(force=True)
        except Exception as restore_exc:
            traceback.print_exc()
            _set_status(
                settings,
                "Import Glue error: Blender scene restoration failed: %s" % restore_exc,
            )
            self.report({"ERROR"}, settings.last_status)
            return False
        if state.warnings:
            # The maps are already written; a viewport flag we could not put
            # back is a warning, never a reason to grade the run CANCELLED.
            self.report(
                {"WARNING"},
                "UI state not fully restored (%d item(s)): %s"
                % (len(state.warnings), "; ".join(state.warnings[:4])),
            )
            _set_status(settings, settings.last_status + " [UI state partly restored]")
        return True


class IMPORTGLUE_OT_open_output(Operator):
    bl_idname = "import_glue.open_output"
    bl_label = "Open Output Folder"
    bl_description = "Open the latest output directory in the system file browser"

    @classmethod
    def poll(cls, context: Any) -> bool:
        settings = getattr(context.scene, "import_glue_settings", None)
        return bool(settings and settings.last_output and os.path.isdir(settings.last_output))

    def execute(self, context: Any):
        path = context.scene.import_glue_settings.last_output
        try:
            bpy.ops.wm.path_open(filepath=path)
        except Exception as exc:
            self.report({"ERROR"}, "Could not open output folder: %s" % exc)
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_PT_main(Panel):
    bl_label = "Import Glue"
    bl_idname = "IMPORTGLUE_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Roblox"

    def draw(self, context: Any) -> None:
        layout = self.layout
        settings = context.scene.import_glue_settings
        layout.use_property_split = True

        scope = layout.box()
        scope.label(text="Source", icon="OUTLINER_OB_MESH")
        scope.prop(settings, "scope")
        if settings.scope == "COLLECTION":
            scope.prop(settings, "source_collection")
        targets = _source_objects(context, settings)
        scope.label(text="%d non-empty source mesh(es)" % len(targets))
        if (
            len(targets) > int(settings.safe_batch_limit)
            and not settings.allow_oversized_batch
        ):
            warning = scope.row()
            warning.alert = True
            warning.label(text="Batch exceeds safety limit", icon="ERROR")

        output = layout.box()
        output.label(text="Output", icon="FILE_FOLDER")
        output.prop(settings, "output_root")
        output.prop(settings, "bake_resolution")
        output.prop(settings, "max_crop_resolution")
        output.prop(settings, "bake_density_scale")
        output.prop(settings, "device")
        if settings.device == "GPU":
            output.prop(settings, "gpu_device_id")
        output.prop(settings, "route_mode")
        output.prop(settings, "target_profile")
        if settings.target_profile in {"ROBLOX", "BOTH"}:
            output.prop(settings, "roblox_texture_limit")
            output.prop(settings, "roblox_material_fit")
            if settings.roblox_material_fit:
                output.prop(settings, "roblox_fit_candidates")
                output.prop(settings, "roblox_fit_seconds")
            output.prop(settings, "roblox_geometry")
            if settings.roblox_geometry:
                output.prop(settings, "roblox_geometry_subdivisions")
                output.prop(settings, "roblox_geometry_triangles")
            if settings.roblox_material_fit or settings.roblox_geometry:
                output.label(text="Requires Allow Approximation", icon="INFO")
        if settings.target_profile == "LEGACY":
            output.prop(settings, "output_profile")
        else:
            output.label(text="Verified target, object and native field recovery")
            if settings.target_profile in {"BLENDER_NATIVE", "BOTH"} and settings.show_advanced:
                output.prop(settings, "native_absolute_tolerance")
                output.prop(settings, "native_relative_tolerance")
                output.prop(settings, "native_initial_resolution")
                output.prop(settings, "native_margin_pixels")
        output.prop(settings, "source_normal_is_directx")

        quality = layout.box()
        quality.label(text="UV Quality", icon="UV")
        quality.prop(settings, "uv_mip_level")
        quality.prop(settings, "uv_margin_pixels")
        quality.operator("import_glue.uv_diagnose", icon="VIEWZOOM")
        quality.operator("import_glue.uv_repair_copy", icon="DUPLICATE")

        safety = layout.box()
        safety.label(text="Texture Safety", icon="CHECKMARK")
        safety.prop(settings, "precheck_enabled")
        row = safety.row()
        row.enabled = settings.precheck_enabled
        row.prop(settings, "repair_into_staging")
        row = safety.row()
        row.enabled = settings.precheck_enabled
        row.alert = settings.repair_missing
        row.prop(settings, "repair_missing", icon="ERROR")
        row = safety.row()
        row.enabled = settings.precheck_enabled and settings.repair_missing
        row.prop(settings, "repair_mode")
        safety.operator("import_glue.precheck", icon="VIEWZOOM")
        if settings.last_capability:
            capability = layout.box()
            capability.label(text="Material Capability", icon="MATERIAL")
            capability.label(text=settings.last_capability)
            for line in settings.last_capability_lines.splitlines()[:5]:
                row = capability.row()
                row.alert = line.startswith("BLOCKED")
                row.label(text=line)

        finalize = layout.box()
        finalize.enabled = not settings.background_execution
        finalize.alert = settings.finalize_in_session
        finalize.prop(settings, "finalize_in_session")
        if settings.finalize_in_session:
            finalize.label(text="Overwrites the open .blend after a clean run", icon="ERROR")

        visual = layout.box()
        visual.label(text="Visual Check", icon="IMAGE_REFERENCE")
        visual.prop(settings, "visual_validation")
        sub = visual.column()
        sub.enabled = settings.visual_validation
        sub.prop(settings, "visual_validation_gate")
        sub.prop(settings, "visual_resolution")
        sub.prop(settings, "visual_samples")
        sub.prop(settings, "visual_probe")
        if settings.visual_validation:
            visual.label(text="Skipped or unsupported coverage is never a PASS", icon="INFO")
            if settings.visual_validation_gate:
                visual.label(text="Experimental gate: thresholds calibrated on synthetic fixtures only",
                             icon="ERROR")

        layout.prop(settings, "show_advanced", emboss=False,
                    icon="DISCLOSURE_TRI_DOWN" if settings.show_advanced else "DISCLOSURE_TRI_RIGHT")
        if settings.show_advanced:
            advanced = layout.box()
            advanced.prop(settings, "precheck_pool")
            advanced.prop(settings, "source_directories")
            advanced.prop(settings, "allow_missing")
            advanced.prop(settings, "allow_corrupt")
            advanced.prop(settings, "strict_absolute")
            advanced.prop(settings, "resume_enabled")
            checkpoint_row = advanced.row()
            checkpoint_row.enabled = settings.resume_enabled
            checkpoint_row.prop(settings, "durable_checkpoints")
            advanced.prop(settings, "rerun_completed")
            advanced.prop(settings, "auto_unwrap_no_uv")
            advanced.prop(settings, "crop_split_prepass")
            fallback = advanced.row()
            fallback.enabled = settings.device == "GPU"
            fallback.prop(settings, "allow_cpu_fallback")
            approximation = advanced.row()
            approximation.alert = settings.allow_approximation
            approximation.prop(settings, "allow_approximation", icon="ERROR")
            advanced.prop(settings, "safe_batch_limit")
            advanced.prop(settings, "allow_oversized_batch")
            advanced.prop(settings, "enforce_triangle_budget")
            sub = advanced.row()
            sub.enabled = settings.enforce_triangle_budget
            sub.prop(settings, "triangle_budget")
            advanced.prop(settings, "export_glb")
            advanced.prop(settings, "export_fbx")

        background_ui.draw(layout, settings)

        run = layout.row()
        run.scale_y = 1.6
        run.enabled = settings.last_job_state not in {"RUNNING", "STARTING", "CANCEL_REQUESTED"}
        run.operator("import_glue.background_start" if settings.background_execution
                     else "import_glue.run", icon="RENDER_STILL")

        if settings.last_status:
            results = layout.box()
            results.label(text="Last Run", icon="INFO")
            results.label(text=settings.last_status)
            if settings.last_output:
                results.operator("import_glue.open_output", icon="FILE_FOLDER")
            if settings.last_report:
                results.label(text=os.path.basename(settings.last_report))


CLASSES = (
    IMPORTGLUE_PG_settings,
    IMPORTGLUE_OT_precheck,
    IMPORTGLUE_OT_run,
    IMPORTGLUE_OT_open_output,
    IMPORTGLUE_PT_main,
) + background_ui.CLASSES


# ---------------------------------------------------------------- migration
#
# import_glue_settings is a PointerProperty on Scene, so its values are SAVED
# INSIDE each .blend. Flipping the repair_missing default to False in the class
# does nothing for a scene that already stored True -- it would keep writing
# into the source folder on every precheck. v1.2.1 therefore rewrites the unsafe
# value once per scene and records that it did, so a user who deliberately turns
# source-writing repair back on is never overridden a second time.

SETTINGS_SCHEMA = 2
_SCHEMA_KEY = "import_glue_settings_schema"


def migrate_scene_settings(scene: Any) -> Optional[str]:
    """Bring one scene's stored settings up to SETTINGS_SCHEMA. Returns a note
    when something was actually changed."""
    settings = getattr(scene, "import_glue_settings", None)
    if settings is None:
        return None
    try:
        stored = int(scene.get(_SCHEMA_KEY, 1))
    except Exception:
        stored = 1
    if stored >= SETTINGS_SCHEMA:
        return None
    changes = []
    if settings.repair_missing:
        settings.repair_missing = False
        settings.repair_into_staging = True
        changes.append("source-writing texture repair turned OFF "
                       "(now resolved into this run instead)")
    if not settings.strict_absolute:
        settings.strict_absolute = True
        changes.append("missing absolute texture paths now block the run")
    try:
        scene[_SCHEMA_KEY] = SETTINGS_SCHEMA
    except Exception:
        return None
    if not changes:
        return None
    return "Import Glue %s: scene %r migrated -- %s" % (
        ADDON_VERSION, getattr(scene, "name", "?"), "; ".join(changes))


@persistent
def _migrate_on_load(_dummy: Any) -> None:
    try:
        scenes = list(bpy.data.scenes)
    except AttributeError:
        # bpy.data is restricted (e.g. during registration); the lazy path covers it.
        return
    for scene in scenes:
        note = migrate_scene_settings(scene)
        if note:
            print(note, flush=True)


def ensure_migrated(scene: Any) -> None:
    """Migrate on first use, for a scene that was already open when the add-on
    was enabled (so load_post never fired for it)."""
    try:
        note = migrate_scene_settings(scene)
    except Exception:
        return
    if note:
        print(note, flush=True)


def register() -> None:
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.import_glue_settings = PointerProperty(type=IMPORTGLUE_PG_settings)
        if _migrate_on_load not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_migrate_on_load)
        # NOTE: bpy.data is NOT touched here. During register() Blender hands out
        # a _RestrictData proxy with no .scenes, so iterating scenes raises
        # AttributeError, addon_utils.enable() catches it, and the add-on fails
        # to enable AT ALL -- the operators register, the Scene pointer property
        # does not, and the panel is useless. An earlier version of this migration
        # did exactly that and it only showed up when the packaged zip was
        # installed into a clean profile; importing the source checkout never
        # exercises register() under the restricted context.
        # A scene already open when the add-on is enabled is migrated lazily by
        # ensure_migrated(), called from each operator.
    except Exception:
        if hasattr(bpy.types.Scene, "import_glue_settings"):
            try:
                del bpy.types.Scene.import_glue_settings
            except Exception:
                pass
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        raise


def unregister() -> None:
    background_ui.unregister_timer()
    engine.restore_log()
    if _migrate_on_load in bpy.app.handlers.load_post:
        try:
            bpy.app.handlers.load_post.remove(_migrate_on_load)
        except Exception:
            traceback.print_exc()
    if hasattr(bpy.types.Scene, "import_glue_settings"):
        try:
            del bpy.types.Scene.import_glue_settings
        except Exception:
            traceback.print_exc()
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    register()
