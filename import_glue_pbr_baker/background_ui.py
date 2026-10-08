"""Small UI bridge; Blender remains on its main thread, baking does not."""
from __future__ import annotations

import json
import os
from pathlib import Path

import bpy
from bpy.types import Operator


def _api():
    from . import background_jobs
    return background_jobs


def snapshot_scene(path, objects=None):
    """Include unsaved pixel buffers, with verified private image copies."""
    from .source_capture import capture_snapshot
    return capture_snapshot(path, objects=objects)


def _refresh(settings):
    status = _api().poll(settings.last_job_dir)
    state = str(status.get("state", "UNKNOWN"))
    settings.last_job_state = state
    detail = status.get("message") or status.get("reason") or ""
    progress = status.get("event") or {}
    if isinstance(progress, dict):
        detail = progress.get("message") or " / ".join(str(progress[key]) for key in
                ("stage", "target", "object", "semantic", "field") if progress.get(key)) or detail
    settings.last_status = "Worker %s%s" % (state, ": " + str(detail)[:180] if detail else "")
    targets = (status.get("census") or {}).get("target_results", {})
    if targets:
        lines = []
        for name, item in targets.items():
            lines.append("%s: %s" % (name, item.get("status", "UNKNOWN")))
            if "converted_fields" in item:
                lines.append("%d sampled fields; %d retained inputs" % (item["converted_fields"], item.get("retained_fields", 0)))
            if item.get("visual"):
                lines.append("Visual: " + ", ".join("%s %s" % pair for pair in item["visual"].items()))
        settings.last_target_result_summary = "\n".join(lines)
    if state in {"FAILED", "CANCELLED"} and Path(settings.last_job_dir, "partial_result.json").is_file():
        settings.last_target_result_summary += "\nPartial native library recorded; append verifies its files"
    settings.last_report = os.path.join(settings.last_job_dir, "status.json")
    settings.last_output = settings.last_job_dir
    return status


def _poll_timer():
    active = False
    for scene in bpy.data.scenes:
        settings = getattr(scene, "import_glue_settings", None)
        if settings is None or not settings.last_job_dir:
            continue
        try:
            status = _refresh(settings)
        except Exception as exc:
            settings.last_status = "Worker polling error: %s" % exc
            continue
        active |= bool(status.get("process_alive"))
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 0.5 if active else None


def _start_timer():
    if not bpy.app.timers.is_registered(_poll_timer):
        bpy.app.timers.register(_poll_timer, first_interval=0.5)


def unregister_timer():
    if bpy.app.timers.is_registered(_poll_timer):
        bpy.app.timers.unregister(_poll_timer)


def _preflight(context, settings, objects, config):
    from . import preflight, target_checkpoints, _precheck_directory
    report = preflight.analyze(objects, config)
    directory = Path(_precheck_directory(settings))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "target_preflight.json"
    target_checkpoints.atomic_json(path, report)
    settings.last_target_preflight = str(path)
    lines = preflight.summary_lines(report)
    settings.last_target_summary = "\n".join(lines[:7] + lines[-1:] if len(lines) > 8 else lines)
    settings.last_report = str(path)
    return report


class IMPORTGLUE_OT_target_preflight(Operator):
    bl_idname = "import_glue.target_preflight"
    bl_label = "Preview Targets and Cost"
    bl_description = "Inspect what each target can bake, preserve, approximate or refuse without changing source data"

    def execute(self, context):
        from . import EngineConfig, ENGINE_CONFIG_KEYS, engine, _source_objects, ensure_migrated
        settings = context.scene.import_glue_settings
        try:
            ensure_migrated(context.scene)
            objects = _source_objects(context, settings)
            if not objects:
                raise ValueError("No source meshes in the chosen scope")
            with EngineConfig(settings):
                config = {key: getattr(engine, key) for key in ENGINE_CONFIG_KEYS}
            _preflight(context, settings, objects, config)
            settings.last_status = "Target preflight saved for %d source meshes" % len(objects)
            self.report({"INFO"}, settings.last_status)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_open_preflight(Operator):
    bl_idname = "import_glue.open_preflight"
    bl_label = "Open Target Report Folder"

    def execute(self, context):
        path = Path(context.scene.import_glue_settings.last_target_preflight)
        if not path.is_file():
            self.report({"ERROR"}, "Run target preflight first")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=str(path.parent))
        return {"FINISHED"}


class IMPORTGLUE_OT_background_start(Operator):
    bl_idname = "import_glue.background_start"
    bl_label = "Bake in Background"
    bl_description = "Bake a snapshot; retain this scene and load completed results separately"

    def execute(self, context):
        from . import EngineConfig, ENGINE_CONFIG_KEYS, engine, _source_objects, _output_root, ensure_migrated
        settings = context.scene.import_glue_settings
        ensure_migrated(context.scene)
        try:
            if settings.last_job_dir and Path(settings.last_job_dir, "job.json").is_file():
                previous = _api().poll(settings.last_job_dir)
                if previous.get("process_alive"):
                    raise ValueError("The current background worker is still running")
            if not bpy.data.filepath:
                raise ValueError("Save the source .blend before creating a background job")
            if context.mode != "OBJECT":
                raise ValueError("Switch to Object Mode before saving the background snapshot")
            if int(settings.max_crop_resolution) < int(settings.bake_resolution):
                raise ValueError("Crop ceiling is below bake resolution")
            objects = _source_objects(context, settings)
            if not objects:
                raise ValueError("No source meshes in the chosen scope")
            if len(objects) > settings.safe_batch_limit and not settings.allow_oversized_batch:
                raise ValueError("Batch exceeds the configured safety limit")
            if settings.repair_missing:
                raise ValueError("Background jobs require source-folder repair to be disabled")
            with EngineConfig(settings):
                config = {key: getattr(engine, key) for key in ENGINE_CONFIG_KEYS}
            config["FINALIZE_IN_SESSION"] = False
            root = _output_root(settings) or os.path.dirname(bpy.data.filepath)
            config["OUTPUT_ROOT"] = root
            _preflight(context, settings, objects, config)
            args = {"no-finalize": True}
            if not settings.resume_enabled:
                args["no-resume"] = True
            if settings.rerun_completed:
                args["rerun"] = True
            spec_objects = [{"name": obj.name,
                             "library": bpy.path.abspath(obj.library.filepath) if obj.library else None}
                            for obj in objects]
            dependencies = set()
            for library in bpy.data.libraries:
                path = bpy.path.abspath(library.filepath)
                if os.path.isfile(path):
                    dependencies.add(os.path.abspath(path))
            for image in bpy.data.images:
                if image.packed_file or image.source != "FILE" or not image.filepath:
                    continue
                path = bpy.path.abspath(image.filepath, library=image.library)
                if os.path.isfile(path):
                    dependencies.add(os.path.abspath(path))
            job_dir = _api().create_job(
                os.path.join(root, ".import_glue_jobs"), bpy.data.filepath, spec_objects, config,
                snapshot_writer=lambda path: snapshot_scene(path, objects), engine_args=args,
                dependency_files=sorted(dependencies),
                resource_policy={"enforce": settings.resource_enforce,
                                 "reserve_fraction": settings.resource_reserve,
                                 "device_id": settings.gpu_device_id.strip()},
                precheck_settings={"enabled": settings.precheck_enabled,
                                   "pool": bpy.path.abspath(settings.precheck_pool) if settings.precheck_pool else "",
                                   "repair": False, "repair_into_staging": settings.repair_into_staging,
                                   "repair_mode": settings.repair_mode,
                                   "allow_missing": settings.allow_missing,
                                   "allow_corrupt": settings.allow_corrupt,
                                   "strict_absolute": settings.strict_absolute})
            _api().launch(job_dir, bpy.app.binary_path)
            settings.last_job_dir = str(job_dir)
            settings.last_target_result_summary = ""
            _refresh(settings)
            _start_timer()
        except Exception as exc:
            settings.last_status = "Background job refused: %s" % exc
            self.report({"ERROR"}, settings.last_status)
            return {"CANCELLED"}
        self.report({"INFO"}, "Background worker started; the source scene remains open")
        return {"FINISHED"}


class IMPORTGLUE_OT_background_poll(Operator):
    bl_idname = "import_glue.background_poll"
    bl_label = "Refresh Job"

    def execute(self, context):
        try:
            _refresh(context.scene.import_glue_settings)
            _start_timer()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_background_cancel(Operator):
    bl_idname = "import_glue.background_cancel"
    bl_label = "Cancel After Current Pass"

    def execute(self, context):
        settings = context.scene.import_glue_settings
        try:
            _api().cancel(settings.last_job_dir)
            _refresh(settings)
            _start_timer()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_background_stop(Operator):
    bl_idname = "import_glue.background_stop"
    bl_label = "Terminate Owned Worker"
    bl_description = "Stop this worker immediately; the current uncommitted part is lost"

    def execute(self, context):
        settings = context.scene.import_glue_settings
        try:
            _api().terminate_owned(settings.last_job_dir, force=True)
            _refresh(settings)
            _start_timer()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_background_resume(Operator):
    bl_idname = "import_glue.background_resume"
    bl_label = "Resume Saved Job"
    bl_description = "Retry the original snapshot; reuse only matching, complete and checksum-verified checkpoints"

    def execute(self, context):
        settings = context.scene.import_glue_settings
        try:
            new_job = _api().resume_job(settings.last_job_dir, str(Path(settings.last_job_dir).parent))
            _api().launch(new_job, bpy.app.binary_path)
            settings.last_job_dir = str(new_job)
            settings.last_target_result_summary = ""
            _refresh(settings)
            _start_timer()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_background_append(Operator):
    bl_idname = "import_glue.background_append"
    bl_label = "Append Available Results"
    bl_description = "Append verified complete results, or the successful native library from a partial delivery"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.import_glue_settings
        try:
            result = _api().load_result(settings.last_job_dir, allow_partial=True)
            result_path = result["result_path"]
            names = result["output_objects"]
            if not names:
                raise ValueError("The result contains no output objects")
            with bpy.data.libraries.load(result_path, link=False) as (available, wanted):
                missing = set(names) - set(available.objects)
                if missing:
                    raise ValueError("Result object list does not match saved blend")
                wanted.objects = names
            if any(obj is None for obj in wanted.objects):
                raise RuntimeError("Blender could not load every verified output object")
            collection = bpy.data.collections.new("Import Glue Results")
            context.scene.collection.children.link(collection)
            for obj in wanted.objects:
                collection.objects.link(obj)
            qualifier = "partial native" if result.get("state") == "PARTIAL" else "complete"
            self.report({"INFO"}, "Appended %d %s result objects; source objects retained" % (len(names), qualifier))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_uv_diagnose(Operator):
    bl_idname = "import_glue.uv_diagnose"
    bl_label = "Analyze UV Quality"

    def execute(self, context):
        from . import _source_objects, _precheck_directory, uv_quality
        settings = context.scene.import_glue_settings
        try:
            if not bpy.data.filepath:
                raise ValueError("Save the scene before writing a UV report")
            objects = _source_objects(context, settings)
            if not objects:
                raise ValueError("No source meshes in scope")
            res = int(settings.bake_resolution)
            report = {"schema": 1, "objects": [uv_quality.inspect_uv_quality(
                obj, resolution=(res, res), mip_level=settings.uv_mip_level,
                margin_pixels=settings.uv_margin_pixels) for obj in objects]}
            path = Path(_precheck_directory(settings)) / "uv_quality.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
            settings.last_uv_report = str(path)
            settings.last_report = str(path)
            settings.last_status = "UV diagnostics saved for %d source meshes" % len(objects)
            self.report({"INFO"}, settings.last_status)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORTGLUE_OT_uv_repair_copy(Operator):
    bl_idname = "import_glue.uv_repair_copy"
    bl_label = "Repair UVs on Active Mesh Copy"
    bl_description = "Create a separately validated mesh copy; texture rebaking may be needed"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import uv_quality
        settings = context.scene.import_glue_settings
        obj = context.active_object
        try:
            if obj is None or obj.type != "MESH" or context.mode != "OBJECT":
                raise ValueError("Select one mesh in Object Mode")
            res = int(settings.bake_resolution)
            copy, report = uv_quality.repair_uv_copy(
                obj, resolution=(res, res), mip_level=settings.uv_mip_level,
                margin_pixels=settings.uv_margin_pixels)
            if copy is None:
                raise ValueError("UV repair was not accepted; source retained")
            context.collection.objects.link(copy)
            settings.last_status = "Created UV repair copy %s; inspect mapping before use" % copy.name
            self.report({"INFO"}, settings.last_status)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def draw(layout, settings):
    preview = layout.box()
    preview.operator("import_glue.target_preflight", icon="VIEWZOOM")
    if settings.last_target_summary:
        for line in settings.last_target_summary.splitlines():
            preview.label(text=line)
        preview.label(text="Estimate only; no RAM/VRAM reservation", icon="INFO")
        preview.operator("import_glue.open_preflight", icon="FILE_FOLDER")
    box = layout.box()
    box.label(text="Background Worker", icon="TIME")
    box.prop(settings, "background_execution")
    if settings.background_execution:
        box.prop(settings, "resource_enforce")
        box.prop(settings, "resource_reserve")
        box.label(text="Snapshot first; source file stays open", icon="INFO")
    if settings.last_job_dir:
        box.label(text=settings.last_job_state or "Saved job")
        for line in settings.last_target_result_summary.splitlines():
            box.label(text=line)
        row = box.row(align=True)
        row.operator("import_glue.background_poll")
        row.operator("import_glue.background_cancel")
        row = box.row(align=True)
        row.operator("import_glue.background_resume")
        row.operator("import_glue.background_stop")
        box.operator("import_glue.background_append")
    if settings.show_advanced:
        box.prop(settings, "last_job_dir", text="Recover Job Folder")


CLASSES = (IMPORTGLUE_OT_background_start, IMPORTGLUE_OT_background_poll,
           IMPORTGLUE_OT_background_cancel, IMPORTGLUE_OT_background_stop,
           IMPORTGLUE_OT_background_resume, IMPORTGLUE_OT_background_append,
           IMPORTGLUE_OT_uv_diagnose, IMPORTGLUE_OT_uv_repair_copy,
           IMPORTGLUE_OT_target_preflight, IMPORTGLUE_OT_open_preflight)
