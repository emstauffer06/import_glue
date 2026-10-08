"""Bounded, render-measured fitting of delivered Roblox roughness/metalness.

This is a Cycles surrogate, not Roblox renderer equivalence. Only final scalar
PNG8 maps change; source materials, colour, normal and emission never change.
The withheld views/lights are consulted once, after training has selected its
candidate, and may veto it. A veto retains the unmodified delivered baseline.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import time
import uuid


SCHEMA_VERSION = 1
ALGORITHM = "quantized_scalar_coordinate_search_v1"


def settings(overrides=None):
    result = {"resolution": 64, "samples": 32, "max_candidates": 12,
              "max_seconds": 300.0, "steps": [0.2, 0.08],
              "minimum_relative_improvement": 0.01, "minimum_absolute_improvement": 0.00001,
              "holdout_absolute_slack": 0.00001,
              "train": [{"view": "+Z", "light": "diffuse", "scale": 1.0},
                        {"view": "-Z", "light": "highlight", "scale": 1.0}],
              "holdout": [{"view": "+Z", "light": "grazing", "scale": 1.0},
                          {"view": "-Z", "light": "diffuse", "scale": 2.0}]}
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - set(result):
            raise ValueError("Unknown material fit settings")
        result.update(copy.deepcopy(overrides))
    for name, low, high in (("resolution", 16, 256), ("samples", 1, 512), ("max_candidates", 1, 32)):
        value = result[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("%s must be an integer in [%d,%d]" % (name, low, high))
    for name, low, high in (("max_seconds", 1, 3600), ("minimum_relative_improvement", 0, 1),
                            ("minimum_absolute_improvement", 0, 1), ("holdout_absolute_slack", 0, .001)):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid material fit " + name)
    steps = result["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 4:
        raise ValueError("One to four coordinate steps required")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= .5 for v in steps):
        raise ValueError("Coordinate steps must be in (0,.5]")
    signatures = {}
    for split in ("train", "holdout"):
        cases = result[split]
        if not isinstance(cases, list) or not 1 <= len(cases) <= 4:
            raise ValueError("Each split needs one to four render cases")
        signatures[split] = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"view", "light", "scale"}:
                raise ValueError("Render cases require view, light and scale")
            if case["view"] not in ("+X", "-X", "+Y", "-Y", "+Z", "-Z") or case["light"] not in ("diffuse", "highlight", "grazing"):
                raise ValueError("Unknown render camera/light")
            scale = case["scale"]
            if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or not .5 <= scale <= 4:
                raise ValueError("Render scale must be in [.5,4]")
            key = (case["view"], case["light"], float(scale))
            if key in signatures[split]:
                raise ValueError("Duplicate render case")
            signatures[split].add(key)
    if signatures["train"] & signatures["holdout"]:
        raise ValueError("Training and withheld cases must be disjoint")
    # Different projected scale alone is not independent camera/light evidence.
    a = {(v, l) for v, l, _ in signatures["train"]}
    b = {(v, l) for v, l, _ in signatures["holdout"]}
    if a & b:
        raise ValueError("Holdout must use independent camera/light combinations")
    return result


def transform_scalar(raw, bias):
    """Apply one bounded global bias to original PNG8 samples, then quantize."""
    import numpy as np
    raw = np.asarray(raw)
    if raw.dtype != np.uint8 or raw.ndim != 2 or not raw.size:
        raise ValueError("A nonempty scalar PNG8 field is required")
    if isinstance(bias, bool) or not isinstance(bias, (int, float)) or not math.isfinite(bias) or not -.8 <= bias <= .8:
        raise ValueError("Scalar bias must be finite and in [-.8,.8]")
    return np.rint(np.clip(raw.astype(np.float64) / 255.0 + bias, 0, 1) * 255).astype(np.uint8)


def accept_candidate(baseline_train, candidate_train, baseline_holdout, candidate_holdout, config):
    """Independent gate; never use holdout to select a second candidate."""
    values = (baseline_train, candidate_train)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
        return False, "INVALID_TRAINING_METRIC"
    required = max(config["minimum_absolute_improvement"], baseline_train * config["minimum_relative_improvement"])
    if baseline_train - candidate_train <= required:
        return False, "NO_MEASURED_TRAINING_IMPROVEMENT"
    if not baseline_holdout or set(baseline_holdout) != set(candidate_holdout):
        return False, "HOLDOUT_COVERAGE_MISMATCH"
    slack = config["holdout_absolute_slack"]
    for key in baseline_holdout:
        for metric in ("mean", "local_max", "alpha_mean", "alpha_local_max"):
            original, candidate = baseline_holdout[key].get(metric), candidate_holdout[key].get(metric)
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (original, candidate)):
                return False, "INVALID_HOLDOUT_METRIC"
            if candidate > original + slack:
                return False, "HOLDOUT_REGRESSION"
    return True, "MEASURED_IMPROVEMENT"


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_scalar(path):
    import bpy
    import numpy as np
    from . import roblox_output
    info = roblox_output._png_metadata(path)
    if info["png_color_type"] not in (0, 2, 6):
        raise ValueError("Unsupported scalar PNG layout")
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = "Non-Color"
        width, height = image.size
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        if not np.isfinite(values).all() or np.any(values < 0) or np.any(values > 1):
            raise ValueError("Non-finite or unnormalized PNG data")
        raw = np.rint(values.reshape(height, width, 4) * 255).astype(np.uint8)
        if not np.array_equal(raw[..., 0], raw[..., 1]) or not np.array_equal(raw[..., 0], raw[..., 2]):
            raise ValueError("Scalar map is not replicated grayscale")
        return raw[..., 0].copy()
    finally:
        bpy.data.images.remove(image)


class _BudgetExceeded(Exception):
    pass


def fit_roblox_object(source, output, folder, binding, *, fit_settings=None, run_control=None):
    """Return fit evidence and a refreshed binding. Rejecting never edits maps.

    Call after engine map finalization and preview creation, before bundle hash
    publication. Hardware uses engine.DEVICE / explicit selected Cycles device.
    Native render calls are not interruptible: budget/cancellation are checked
    before AND after each individual paired comparison, and before publishing.
    """
    import bpy
    import numpy as np
    from . import engine, roblox_output, source_capture, visual_validation as vv
    from .run_control import RunCancelled

    config = settings(fit_settings)
    root = Path(folder).resolve()
    evidence = root / ("material_fit_" + uuid.uuid4().hex[:12])
    evidence.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": SCHEMA_VERSION, "algorithm": ALGORITHM, "settings": config,
              "accepted": False, "status": "BASELINE_RETAINED", "studio_verified": False,
              "scope": "Offline Cycles surrogate of a single Roblox-like Principled closure; finite sampled views/lights only",
              "parameters": "Global additive roughness/metalness bias, clipped [0,1] and quantized to PNG8 before every render",
              "unchanged_channels": ["color", "normal", "emissive"], "candidates": [],
              "budget_scope": "Cooperative admission between native paired renders; one in-flight comparison may exceed wall budget"}
    refreshed = copy.deepcopy(binding)
    start = time.perf_counter()
    candidate = None
    private_mesh = None
    private_materials = []
    private_images = []
    original_bytes = {}
    published = False
    source_before = None
    paths = {}
    output_images = []

    def boundary():
        if run_control:
            run_control.check_cancel()
        if time.perf_counter() - start > config["max_seconds"]:
            raise _BudgetExceeded("Material fitting exceeded its wall-clock admission budget")

    def write_maps(values):
        for semantic in ("rough", "metal"):
            path = evidence / (semantic + ".png")
            roblox_output.write_emissive_mask(path, values[semantic])
            if not np.array_equal(_read_scalar(path), values[semantic]):
                raise ValueError("Final scalar PNG decode disagrees with candidate")
        for image in private_images:
            image.reload()

    def evaluate(label, split, values):
        write_maps(values)
        records = {}
        for index, case in enumerate(config[split]):
            boundary()
            if run_control:
                run_control.emit({"kind": "stage", "stage": "material_fit", "object": source.name,
                                  "candidate": label, "split": split, "case": index})
            visual = vv.default_settings()
            visual.update(resolution=config["resolution"], samples=config["samples"], device=device,
                          layers=["render"], views=[case["view"]], lighting_presets=[case["light"]],
                          render_scales=[case["scale"]], keep_linear=True)
            destination = evidence / (label + "_" + split + "_%d" % index)
            result = vv.compare_pair(source, candidate, destination, visual)
            boundary()
            integrity = vv.inspect_report(str(destination))
            # Ordinary mismatch is the optimization objective. Context errors,
            # incomplete coverage and invariance failures invalidate the oracle.
            reasons = {row["code"] for row in result.get("reasons", [])}
            if (not integrity["ok"] or result.get("errors") or
                    result["coverage"]["render"].get("status") != "COMPARED" or
                    reasons - {"METRIC_EXCEEDED"}):
                raise ValueError("Untrustworthy render reference: %s" % sorted(reasons))
            rows = result.get("metrics", {}).get("render", {})
            if len(rows) != 1:
                raise ValueError("Render case did not produce exactly one measured view")
            row = next(iter(rows.values()))
            if row["coverage"]["invalid_pixels"] or not row["coverage"]["covered"]:
                raise ValueError("Render case lacks finite interior coverage")
            metrics = {"mean": row["color"]["mean_error"], "local_max": row["color"]["local_max"],
                       "alpha_mean": row["alpha"]["mean_error"], "alpha_local_max": row["alpha"]["local_max"]}
            if any(value is None or not math.isfinite(value) for value in metrics.values()):
                raise ValueError("Render case has unmeasured local error")
            records[str(index)] = {**metrics, "case": case, "report": str(destination / "report.json"),
                                   "sha256": _digest(destination / "report.json")}
        return sum(row["mean"] for row in records.values()) / len(records), records

    try:
        if source is output or source.data is output.data:
            raise ValueError("Source and output must be independent mesh objects")
        if not engine.ALLOW_APPROXIMATION:
            raise ValueError("Material fitting requires explicit ALLOW_APPROXIMATION")
        source_before = vv.content_fingerprint(source)
        maps = binding.get("maps", {})
        if set(maps) != {"color", "metal", "rough", "normal", "emissive"}:
            raise ValueError("A finalized five-map binding is required")
        for semantic, value in maps.items():
            path = (root / value).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Delivered map is missing or outside target folder")
            paths[semantic] = path
        # A caller may accidentally choose a source texture directory as its
        # export root. Never turn fitting into an edit of an authored resource.
        source_image_ids, visited = set(), set()
        def source_resources(tree):
            if tree is None or tree.as_pointer() in visited:
                return
            visited.add(tree.as_pointer())
            for node in tree.nodes:
                image = getattr(node, "image", None)
                if image is not None:
                    source_image_ids.add(image.as_pointer())
                    if image.filepath and Path(bpy.path.abspath(image.filepath, library=image.library)).resolve() in paths.values():
                        raise ValueError("A delivered map aliases an authored source image")
                source_resources(getattr(node, "node_tree", None))
        for material in vv._used_materials(source):
            source_resources(material.node_tree if material else None)
        source_images, _, _ = source_capture._dependencies([source])
        for image in source_images:
            source_image_ids.add(image.as_pointer())
            if image.filepath and Path(bpy.path.abspath(image.filepath, library=image.library)).resolve() in paths.values():
                raise ValueError("A delivered map aliases an authored geometry/scene image")
        expected = binding.get("emission", {}).get("report", {}).get("delivered_files", {})
        if set(expected) != set(paths) or any(_digest(path) != expected[key] for key, path in paths.items()):
            raise ValueError("Binding hashes disagree with delivered baseline maps")
        report["baseline_hashes"] = dict(expected)
        originals = {key: _read_scalar(paths[key]) for key in ("rough", "metal")}
        if originals["rough"].shape != originals["metal"].shape:
            raise ValueError("Scalar dimensions differ")
        original_bytes = {key: paths[key].read_bytes() for key in originals}
        candidate = output.copy()
        private_mesh = output.data.copy()
        candidate.data = private_mesh
        candidate.name = "__IG_Fit_" + uuid.uuid4().hex[:10]
        bpy.context.collection.objects.link(candidate)
        # Only engine-generated simple output materials are permitted. Rebuild
        # scalar image nodes privately; leave every other shader setting intact.
        for slot in candidate.material_slots:
            original = slot.material
            if original is None or not original.use_nodes:
                raise ValueError("Output preview material missing")
            analysis = vv.analyze_material(original, role="output")
            if analysis["kind"] != "PRINCIPLED_DIRECT" or analysis["render_issues"]:
                raise ValueError("Output must be one direct, renderable Principled closure")
            material = original.copy()
            private_materials.append(material)
            slot.material = material
            principled = material.node_tree.nodes[analysis["principled"]]
            mapped_nodes = {}
            # Every candidate samples delivered files freshly, including the
            # unchanged channels; dirty/packed preview buffers are not proof of
            # what the user will receive on disk.
            for node in material.node_tree.nodes:
                if node.type != "TEX_IMAGE":
                    continue
                image = node.image
                if image is None:
                    raise ValueError("Output image is missing")
                path = Path(bpy.path.abspath(image.filepath)).resolve()
                key = next((key for key, target in paths.items() if target == path), None)
                if key is None:
                    raise ValueError("Output preview samples an image outside its binding")
                if key in ("rough", "metal"):
                    if image.as_pointer() in source_image_ids or image.packed_file is not None or image.is_dirty:
                        raise ValueError("Output scalar preview must be an independent clean file image")
                    output_images.append(image)
                    path = evidence / (key + ".png")
                    roblox_output.write_emissive_mask(path, originals[key])
                image = bpy.data.images.load(str(path), check_existing=False)
                image.colorspace_settings.name = "sRGB" if key == "color" else "Non-Color"
                private_images.append(image)
                node.image = image
                mapped_nodes[node.as_pointer()] = key
            for key, socket_name in (("rough", "Roughness"), ("metal", "Metallic")):
                links = list(principled.inputs[socket_name].links)
                if len(links) != 1 or links[0].from_node.type != "TEX_IMAGE":
                    raise ValueError("Output scalar channels must use direct delivered image nodes")
                node = links[0].from_node
                if mapped_nodes.get(node.as_pointer()) != key:
                    raise ValueError("Output preview scalar image does not match its binding")
        if not private_materials:
            raise ValueError("No output material to fit")
        with engine.SceneSettingsGuard(snapshot_cycles_preferences=engine.DEVICE == "GPU"):
            selected = engine.ensure_cycles_device()
            device = "CPU" if selected == "CPU" else "GPU"
            report["device"] = {"requested": engine.DEVICE, "selected": selected,
                                  "gpu_device_id": engine.GPU_DEVICE_ID}
            baseline_score, train = evaluate("baseline", "train", originals)
            report["baseline_train"] = {"score": baseline_score, "cases": train}
            best_score, best_bias, best_values = baseline_score, [0., 0.], originals
            count = 0
            seen = {tuple(hashlib.sha256(originals[key].tobytes()).hexdigest() for key in ("rough", "metal"))}
            for step in config["steps"]:
                for dimension in range(2):
                    centre = list(best_bias)
                    for sign in (-1, 1):
                        if count >= config["max_candidates"]:
                            break
                        bias = list(centre)
                        bias[dimension] = max(-.8, min(.8, bias[dimension] + sign * step))
                        values = {key: transform_scalar(originals[key], bias[i]) for i, key in enumerate(("rough", "metal"))}
                        signature = tuple(hashlib.sha256(values[key].tobytes()).hexdigest() for key in ("rough", "metal"))
                        if signature in seen:
                            continue
                        seen.add(signature)
                        label = "candidate_%02d" % count
                        score, cases = evaluate(label, "train", values)
                        count += 1
                        row = {"label": label, "bias": bias, "score": score, "cases": cases}
                        report["candidates"].append(row)
                        if score < best_score:
                            best_score, best_bias, best_values = score, bias, values
                            report["selected_candidate"] = label
            report["selected_bias"] = best_bias
            improvement = baseline_score - best_score
            required = max(config["minimum_absolute_improvement"], baseline_score * config["minimum_relative_improvement"])
            if improvement <= required:
                report["reason"] = "NO_MEASURED_TRAINING_IMPROVEMENT"
            else:
                _, baseline_holdout = evaluate("baseline", "holdout", originals)
                _, candidate_holdout = evaluate("selected", "holdout", best_values)
                report["holdout"] = {"baseline": baseline_holdout, "candidate": candidate_holdout,
                                     "policy": "Evaluated once after train selection; never used to choose another candidate"}
                accepted, reason = accept_candidate(baseline_score, best_score, baseline_holdout, candidate_holdout, config)
                report["reason"] = reason
                if accepted:
                    boundary()
                    if source_before != vv.content_fingerprint(source):
                        raise ValueError("Source content changed while fitting")
                    # Delivery is all-or-restored inside the supervised worker.
                    # Original bytes remain in memory until every decode/hash
                    # and preview reload has completed.
                    for key, raw in best_values.items():
                        roblox_output.write_emissive_mask(paths[key], raw)
                    for key, raw in best_values.items():
                        if not np.array_equal(_read_scalar(paths[key]), raw):
                            raise ValueError("Published PNG decode differs from rendered candidate")
                    for image in set(output_images):
                        image.reload()
                    refreshed["emission"]["report"]["delivered_files"] = {key: _digest(path) for key, path in paths.items()}
                    report["delivered_hashes"] = dict(refreshed["emission"]["report"]["delivered_files"])
                    report.update(accepted=True, status="FITTED_APPROXIMATION",
                                  training_improvement_absolute=improvement,
                                  training_improvement_relative=improvement / max(baseline_score, 1e-30))
                    published = True
    except (RunCancelled, KeyboardInterrupt):
        published = False
        report.update(reason="CANCELLED", accepted=False, status="BASELINE_RETAINED")
        raise
    except _BudgetExceeded as exc:
        published = False
        refreshed = copy.deepcopy(binding)
        report.update(reason="BUDGET_EXHAUSTED", error=str(exc), accepted=False, status="BASELINE_RETAINED")
    except Exception as exc:
        published = False
        refreshed = copy.deepcopy(binding)
        report.update(reason="REFERENCE_OR_FIT_UNAVAILABLE", error=str(exc), accepted=False, status="BASELINE_RETAINED")
    finally:
        final_errors = []
        source_changed = False
        source_verified = False
        try:
            if source_before is not None:
                try:
                    source_changed = source_before != vv.content_fingerprint(source)
                    source_verified = True
                except Exception as exc:
                    final_errors.append("Source invariance verification failed: " + str(exc))
                    published = False
            if source_changed:
                published = False
                final_errors.append("Source content changed during material fitting")
            if not published and original_bytes:
                # A failure restoring one file must not prevent attempts to
                # restore the other files or remove private Blender IDs.
                for key, payload in original_bytes.items():
                    try:
                        try:
                            current = paths[key].read_bytes()
                        except OSError:
                            current = None
                        if current != payload:
                            roblox_output._atomic_bytes(paths[key], payload)
                        if paths[key].read_bytes() != payload:
                            raise OSError("Restored bytes do not match the baseline")
                    except Exception as exc:
                        final_errors.append("Rollback %s failed: %s" % (key, exc))
                for image in set(output_images):
                    try:
                        image.reload()
                    except Exception as exc:
                        final_errors.append("Output preview reload failed: " + str(exc))
            if original_bytes:
                # These hashes name current disk contents, even when rollback
                # failed. A finalization error prevents their use as a binding.
                try:
                    report["delivered_hashes"] = {key: _digest(path) for key, path in paths.items()}
                except Exception as exc:
                    report.pop("delivered_hashes", None)
                    final_errors.append("Delivered file verification failed: " + str(exc))
        finally:
            # This must run even if fingerprinting, filesystem access or an
            # unexpected BaseException interrupts verification/rollback.
            for kind, blocks in (("objects", [candidate]), ("meshes", [private_mesh]),
                                 ("materials", private_materials), ("images", private_images)):
                for block in blocks:
                    if block is None:
                        continue
                    try:
                        if kind == "objects" or block.users == 0:
                            getattr(bpy.data, kind).remove(block, do_unlink=True)
                        else:
                            raise RuntimeError("Private ID still has users")
                    except Exception as exc:
                        final_errors.append("Private %s cleanup failed: %s" % (kind, exc))
        report["elapsed_seconds"] = time.perf_counter() - start
        if source_before is not None:
            report["source_unchanged"] = not source_changed if source_verified else None
        if final_errors:
            report.update(accepted=False, status="FAILED_FINALIZATION", reason="DELIVERY_UNVERIFIED",
                          finalization_errors=final_errors)
        report_path = evidence / "fit_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
        if final_errors:
            raise RuntimeError("Material fit finalization failed; output is not usable: " + "; ".join(final_errors))
    return {"status": report["status"], "accepted": report["accepted"], "reason": report.get("reason"),
            "binding": refreshed, "report": str(report_path), "sha256": _digest(report_path),
            "studio_verified": False}
