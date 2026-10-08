"""Read-only, per-target workload explanation before snapshot capture or baking."""
from __future__ import annotations

import bpy

from . import engine, material_capabilities, native_graph, native_quality, resources, shader_graph, uv_validation, ocio_capture


def analyze(objects, settings=None):
    settings = dict(settings or {})
    get = lambda name: settings.get(name, getattr(engine, name))
    objects = list(objects)
    target = get("TARGET_PROFILE")
    kinds = ("BLENDER_NATIVE", "ROBLOX") if target == "BOTH" else (target,)
    quality = (native_quality.settings(settings.get("NATIVE_QUALITY", getattr(engine, "NATIVE_QUALITY", None)))
               if "BLENDER_NATIVE" in kinds else None)
    color = ocio_capture.target_contract()
    fit_config = geometry_config = None
    if "ROBLOX" in kinds:
        from . import material_fit, geometry_delivery
        if get("ROBLOX_MATERIAL_FIT"):
            fit_config = material_fit.settings(get("ROBLOX_FIT_SETTINGS"))
        if get("ROBLOX_GEOMETRY"):
            geometry_config = geometry_delivery.settings({**get("ROBLOX_GEOMETRY_SETTINGS"), "enabled": True})
            geometry_config["max_triangles"] = min(geometry_config["max_triangles"], engine.TRI_BUDGET)
    rows, image_ids = [], set()
    geometry_working_bytes = 0
    eligible_fields = material_nodes = material_instances = 0
    for obj in objects:
        row = {"object": obj.name, "targets": {}, "warnings": []}
        # Evaluated topology is checked by the actual baker; do not create private geometry here.
        if obj.modifiers or obj.data.shape_keys:
            row["warnings"].append("Evaluated topology and UVs are checked during baking; preflight counts the source mesh")
        usage = shader_graph.material_usage(obj)
        slots = (usage["evaluated"] if usage["state"] == "EVALUATED" else
                 {slot: obj.material_slots[slot].material if slot < len(obj.material_slots) else None
                  for slot in engine.used_material_slots(obj.data)})
        plans, blockers = [], []
        if usage["state"] in {"ERROR", "NOT_EVALUATED"}:
            blockers.append("Evaluated material scope unavailable: " + usage["detail"])
        for slot, material in slots.items():
            if material is None:
                blockers.append("Used material slot %d is empty" % slot)
                continue
            material_instances += 1
            if material.node_tree is None:
                blockers.append("Material %s has no node tree" % material.name)
                continue
            plan = native_graph.inspect_material(material)
            plans.append(plan)
            blockers.extend(item.get("reason", item.get("code", "Unsupported dependency")) for item in plan["blockers"])
            image_ids.update(plan["dependency"]["images"])
            seen = set()
            def count(tree):
                nonlocal material_nodes
                if tree.as_pointer() in seen:
                    return
                seen.add(tree.as_pointer())
                material_nodes += len(tree.nodes)
                for node in tree.nodes:
                    if node.type == "GROUP" and node.node_tree:
                        count(node.node_tree)
            count(material.node_tree)
        if "BLENDER_NATIVE" in kinds and not color["native_supported"]:
            blockers.extend(color["native_reasons"])
        fields = [field for plan in plans for field in plan["fields"]]
        count_fields = sum(bool(field["eligible"]) for field in fields)
        eligible_fields += count_fields
        if "BLENDER_NATIVE" in kinds:
            uv_name = engine.choose_source_uv(obj.data)
            gate = uv_validation.validate_uv(obj, uv_name, require_unique=True)
            action = "REFUSE" if blockers else "BAKE_FIELDS" if count_fields and gate["ok"] else "PRESERVE_GRAPH"
            row["targets"]["BLENDER_NATIVE"] = {
                "action": action, "eligible_fields": count_fields,
                "retained_fields": len(fields) - count_fields, "blockers": blockers,
                "uv_ready": bool(gate["ok"]),
                "reason": "; ".join(blockers) if blockers else
                    "Sample eligible fields with quality checks; preserve closure graph" if action == "BAKE_FIELDS" else
                    "Preserve native graph; no static optimization is promised",
                "retained_domains": sorted({domain for plan in plans for domain in plan["retained_domains"]}),
            }
        if "ROBLOX" in kinds or "LEGACY" in kinds:
            profile = "ROBLOX" if "ROBLOX" in kinds else engine.CAPABILITY_PROFILE
            report = material_capabilities.preflight_report([obj], profile, bool(get("ALLOW_APPROXIMATION")))
            decisions = report["object_decisions"]
            decision = next(iter(decisions.values())) if isinstance(decisions, dict) else decisions[0]
            row["targets"]["ROBLOX" if "ROBLOX" in kinds else "LEGACY"] = {
                "action": "REFUSE" if not decision.get("allowed", False) else
                    "APPROXIMATE" if report["status"] == material_capabilities.APPROXIMATION_REQUIRED else "BAKE_PBR",
                "status": report["status"], "decision": decision,
                "reason": material_capabilities.summary_text(report),
                "findings": material_capabilities.summary_lines(report, limit=12),
                "emission_fit": "Measured after sampling; preflight cannot guarantee representability",
            }
            if "ROBLOX" in kinds and not color["roblox_supported"]:
                row["targets"]["ROBLOX"].update(action="REFUSE", color_blockers=color["roblox_reasons"])
            if "ROBLOX" in kinds and (fit_config is not None or geometry_config is not None):
                delivery = row["targets"]["ROBLOX"]
                if not get("ALLOW_APPROXIMATION"):
                    delivery.update(action="REFUSE", reason="Fitting/geometry conversion requires Allow Approximation")
                elif delivery["action"] != "REFUSE":
                    delivery["action"] = "APPROXIMATE"
                if fit_config is not None:
                    delivery["material_fit"] = {"settings": fit_config, "status": "PLANNED",
                        "scope": "Quantized scalar roughness/metalness biases; independent withheld render gate; Cycles surrogate only"}
                if geometry_config is not None:
                    capture = geometry_delivery.inspect(obj, settings=geometry_config)
                    delivery["geometry_delivery"] = capture
                    geometry_working_bytes += capture.get("estimated_working_bytes", 0)
                    if capture["status"] == "REFUSED":
                        delivery.update(action="REFUSE", reason=capture["reason"])
        rows.append(row)
    images = [image for image in bpy.data.images if image.as_pointer() in image_ids]
    stats = resources.source_statistics(objects, images, bpy.data.filepath)
    stats.update(objects=len(objects), eligible_fields=eligible_fields,
                 material_nodes=material_nodes, material_instances=material_instances,
                 geometry_working_bytes=geometry_working_bytes,
                 fit_temporary_bytes=(max(get("RES"), get("MAX_RES")) ** 2 * 16 * 10
                                      + fit_config["resolution"] ** 2 * 16 * 24) if fit_config else 0)
    estimate = resources.estimate_job(stats, resolution=max(get("RES"), get("MAX_RES")),
                                     target=target, quality_settings=quality)
    return {"schema": 1, "target": target, "objects": rows, "resource_estimate": estimate,
            "source_stats": stats, "native_quality": quality, "roblox_fit": fit_config,
            "roblox_geometry": geometry_config, "color_management": color, "read_only": True,
            "capture": "Snapshot capture subsequently validates pixels, external resources and scene context",
            "recovery": "Completed targets, objects and native fields require matching inputs and every artifact checksum",
            "limitations": ["Planning estimates do not reserve memory or predict runtime",
                            "Preflight does not prove a material's visual match in Blender or Roblox Studio"]}


def summary_lines(report):
    lines = []
    for row in report["objects"]:
        for target, item in row["targets"].items():
            lines.append("%s / %s: %s" % (row["object"], target, item["action"].replace("_", " ")))
    estimate = report["resource_estimate"]
    lines.append("Planning estimate: RAM %.2f GiB; VRAM %.2f GiB" %
                 (estimate["ram_bytes"] / 1024 ** 3, estimate["vram_bytes"] / 1024 ** 3))
    return lines
