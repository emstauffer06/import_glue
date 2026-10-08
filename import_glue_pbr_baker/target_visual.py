"""Native-target visual evidence using preserved closures and isolated renders.

Native outputs can keep layered shaders outside the portable channel oracle.
Their default comparison is consequently full rendered appearance at multiple
projected sizes. This is finite, declared coverage, never all-view equivalence.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
import uuid

import bpy

from . import background_jobs, engine, visual_validation
from .run_control import RunCancelled


def _gallery(folder, rows):
    body = ["<!doctype html><meta charset='utf-8'><title>Import Glue comparison</title>",
            "<style>body{font:16px system-ui;background:#171b23;color:#edf1fa;margin:32px}"
            "a{color:#92c4ff}.images{display:flex;gap:12px;flex-wrap:wrap}"
            "img{width:240px;max-width:90vw;background:#555}figure{margin:0 0 16px}"
            "figcaption{font-size:13px}section{border-top:1px solid #485367;padding:16px 0}</style>",
            "<h1>Native material comparison</h1><p>Finite camera, light and projected-size tests. "
            "Cycles results do not establish Roblox Studio parity. Difference images use the "
            "threshold legend in each JSON report.</p>"]
    for row in rows:
        body.append("<section><h2>%s — %s</h2>" % (html.escape(row["source"]), html.escape(row["status"])))
        if row.get("report"):
            path = Path(row["report"])
            rel = path.relative_to(folder).as_posix()
            body.append('<p><a href="%s">Full report and thresholds</a></p>' % html.escape(rel, quote=True))
            report = json.loads(path.read_text(encoding="utf-8"))
            body.append('<div class="images">')
            for name, artifact in report["artifacts"].items():
                if artifact["layer"] != "render" or artifact["role"] not in {"reference", "output", "difference"}:
                    continue
                image = (path.parent / name).relative_to(folder).as_posix()
                body.append('<figure><img loading="lazy" src="%s"><figcaption>%s</figcaption></figure>' %
                            (html.escape(image, quote=True), html.escape(name)))
            body.append("</div>")
        else:
            body.append("<p>%s</p>" % html.escape(row.get("error", "Comparison unavailable")))
        body.append("</section>")
    (folder / "index.html").write_text("\n".join(body), encoding="utf-8")


def compare_native(objects, result, target_root, *, run_control=None):
    """Attach verified per-object visual evidence; gate only when requested.

    Always recompare live outputs, including a restored checkpoint. An older
    image/report is not evidence for a newly loaded or modified output.
    """
    if not engine.VISUAL_VALIDATION:
        return result
    sources = {obj.name: obj for obj in objects}
    root = Path(target_root) / ("visual_" + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    rows, census = [], {}
    overrides = dict(engine.VISUAL_VALIDATION_SETTINGS or {})
    settings = visual_validation.default_settings()
    settings.update(layers=["render"], render_scales=[1.0, 2.0, 4.0])
    settings.update(overrides)
    for index, item in enumerate(result.get("objects", [])):
        if item.get("status") not in {"BAKED", "RETAINED_ONLY", "RESTORED"}:
            continue
        if run_control:
            run_control.check_cancel()
            run_control.emit({"kind": "stage", "stage": "native_visual", "object": item.get("source"), "current": index})
        row = {"source": item.get("source", ""), "output": item.get("output_object"), "status": "FAIL"}
        try:
            source = sources.get(item.get("source"))
            output = bpy.data.objects.get(item.get("output_object", ""))
            if source is None or output is None:
                raise ValueError("Live source and native output are required for visual comparison")
            component = "%04d_%s" % (index, re.sub(r"[^A-Za-z0-9_-]", "_", source.name)[:48])
            with engine.SceneSettingsGuard(snapshot_cycles_preferences=engine.DEVICE == "GPU"):
                device = engine.ensure_cycles_device()
                # User visual settings cannot silently change hardware identity.
                settings["device"] = "CPU" if device == "CPU" else "GPU"
                report = visual_validation.compare_pair(source, output, root / component, settings)
            check = visual_validation.inspect_report(str(root / component))
            if not check["ok"]:
                raise ValueError("Visual report/artifact verification failed: %s" % check)
            path = root / component / "report.json"
            row.update(status=report["status"], report=str(path), sha256=background_jobs.sha256_file(path),
                       scope=report["coverage"].get("fidelity_scope"),
                       reasons=sorted({reason["code"] for reason in report["reasons"]}))
        except (RunCancelled, KeyboardInterrupt):
            raise
        except Exception as exc:
            row.update(error=str(exc))
        rows.append(row)
        item["visual_validation"] = row
        census[row["status"]] = census.get(row["status"], 0) + 1
    if not rows:
        census["NOT_RUN"] = 1
    _gallery(root, rows)
    result.update(visual=census, visual_reports=rows, visual_gallery=str(root / "index.html"))
    if engine.VISUAL_VALIDATION_GATE and any(status != "PASS" for status in census):
        result.update(exit_code=1, status="VISUAL_VALIDATION_FAILED")
    return result
