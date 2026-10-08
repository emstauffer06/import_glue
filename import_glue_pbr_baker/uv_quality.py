"""Read-only UV quality diagnostics and explicit repair previews on private copies.

Validity remains uv_validation.validate_uv's job. Density is pixels per world
Blender unit after matrix_world; anisotropy is the ratio of the two singular
values of each triangle's world-to-pixel Jacobian. These are sampling-quality
metrics, not a promise about appearance, texture content, or bake fidelity.
"""
from __future__ import annotations

import math

from .uv_validation import validate_uv

SCHEMA_VERSION = 1


def _settings(resolution, mip_level, margin_pixels, target_density,
              density_ratio_limit, anisotropy_limit, max_pair_checks):
    if isinstance(resolution, int) and not isinstance(resolution, bool):
        resolution = (resolution, resolution)
    if (not isinstance(resolution, (tuple, list)) or len(resolution) != 2
            or any(type(x) is not int or not 1 <= x <= 32768 for x in resolution)):
        raise ValueError("resolution must be an integer or (width,height) in 1..32768")
    if type(mip_level) is not int or not 0 <= mip_level <= 16:
        raise ValueError("mip_level must be an integer from 0 through 16")
    for name, value in (("margin_pixels", margin_pixels), ("density_ratio_limit", density_ratio_limit),
                        ("anisotropy_limit", anisotropy_limit)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(name + " must be finite")
    if margin_pixels < 0 or density_ratio_limit < 1 or anisotropy_limit < 1:
        raise ValueError("margin must be nonnegative and quality ratios must be >=1")
    if not math.isfinite(2*margin_pixels*2**mip_level):
        raise ValueError("mip-scaled margin must remain finite")
    if target_density is not None and (isinstance(target_density, bool)
            or not isinstance(target_density, (int, float))
            or not math.isfinite(target_density) or target_density <= 0):
        raise ValueError("target_density must be finite and positive")
    if type(max_pair_checks) is not int or not 0 <= max_pair_checks <= 10_000_000:
        raise ValueError("max_pair_checks must be an integer from 0 through 10000000")
    return tuple(resolution)


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _triangle_metrics(world, uv, resolution):
    e1, e2 = _sub(world[1], world[0]), _sub(world[2], world[0])
    length = math.hypot(*e1)
    twice_area = math.hypot(*_cross(e1, e2))
    if not math.isfinite(length) or not math.isfinite(twice_area) or length == 0 or twice_area == 0:
        return None
    b, c = _dot(e1, e2)/length, twice_area/length
    if not math.isfinite(b) or not math.isfinite(c) or c == 0:
        return None
    dx1, dy1 = (uv[1][0]-uv[0][0])*resolution[0], (uv[1][1]-uv[0][1])*resolution[1]
    dx2, dy2 = (uv[2][0]-uv[0][0])*resolution[0], (uv[2][1]-uv[0][1])*resolution[1]
    j00, j10 = dx1/length, dy1/length
    j01, j11 = (dx2-j00*b)/c, (dy2-j10*b)/c
    aa, bb, ab = j00*j00+j10*j10, j01*j01+j11*j11, j00*j01+j10*j11
    determinant = j00*j11-j01*j10
    maximum = math.sqrt(max(0, (aa+bb+math.hypot(aa-bb, 2*ab))/2))
    minimum = abs(determinant)/maximum if maximum else 0
    anisotropy = maximum/minimum if minimum else None
    area_pixels = abs(dx1*dy2-dy1*dx2)*0.5
    values = (area_pixels, maximum, minimum, anisotropy)
    if any(value is None or not math.isfinite(value) for value in values):
        return None
    return {"world_area": twice_area*0.5, "uv_area": area_pixels/(resolution[0]*resolution[1]),
            "pixel_area": area_pixels, "anisotropy": anisotropy,
            "singular_values_px_per_world_unit": [maximum, minimum],
            "mirrored": determinant < 0}


def _islands(mesh, uv):
    parent = list(range(len(mesh.polygons)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        a, b = find(a), find(b)
        parent[max(a, b)] = min(a, b)

    edge_groups = {}
    for face in mesh.polygons:
        loops = list(face.loop_indices)
        for i, loop in enumerate(loops):
            nxt = loops[(i+1) % len(loops)]
            va, vb = mesh.loops[loop].vertex_index, mesh.loops[nxt].vertex_index
            a, b = uv[loop], uv[nxt]
            canonical = (a, b) if va < vb else (b, a)
            key = (mesh.loops[loop].edge_index, *canonical)
            edge_groups.setdefault(key, []).append((face.index, loop, nxt, a, b))
    for group in edge_groups.values():
        for edge in group[1:]:
            union(group[0][0], edge[0])
    roots, islands, face_island = {}, [], {}
    for face in mesh.polygons:
        root = find(face.index)
        if root not in roots:
            roots[root] = len(islands)
            islands.append({"id": len(islands), "faces": [], "loops": [], "boundary": []})
        island = islands[roots[root]]
        island["faces"].append(face.index)
        island["loops"].extend(face.loop_indices)
        face_island[face.index] = island["id"]
    nonmanifold = False
    for group in edge_groups.values():
        if len(group) > 2:
            nonmanifold = True
        if len(group) == 1:
            face, loop, nxt, a, b = group[0]
            islands[face_island[face]]["boundary"].append({"face": face, "loops": [loop, nxt],
                                                         "a": a, "b": b})
    return islands, face_island, nonmanifold


def _point_segment(p, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    d = dx*dx+dy*dy
    u = min(1, max(0, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/d)) if d else 0
    return math.hypot(p[0]-a[0]-u*dx, p[1]-a[1]-u*dy)


def _segment_distance(a, b, c, d):
    def side(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    if side(a, b, c)*side(a, b, d) < 0 and side(c, d, a)*side(c, d, b) < 0:
        return 0.0
    return min(_point_segment(a, c, d), _point_segment(b, c, d),
               _point_segment(c, a, b), _point_segment(d, a, b))


def inspect_uv_quality(obj, layer_name=None, *, resolution=(1024, 1024), mip_level=0,
                       margin_pixels=2.0, target_density=None, density_ratio_limit=2.0,
                       anisotropy_limit=4.0, max_pair_checks=200_000, require_unique=True):
    """Return JSON-ready quality findings; source and selection remain untouched.

    margin_pixels is desired padding on EACH side at mip_level. Thus base-image
    border clearance is margin_pixels*2**mip_level and inter-island separation
    is twice that. Only sub-threshold gaps are searched: no global minimum gap
    is claimed. Bounded scans mark incomplete coverage instead of certifying it.
    """
    resolution = _settings(resolution, mip_level, margin_pixels, target_density,
                           density_ratio_limit, anisotropy_limit, max_pair_checks)
    validity = validate_uv(obj, layer_name, require_unique=require_unique)
    result = {"schema_version": SCHEMA_VERSION, "validity": validity, "complete": False,
              "ok": False, "status": "VALIDITY_BLOCKED", "faces": [], "islands": [],
              "geometry_basis": "stored mesh with matrix_world; modifiers are not evaluated",
              "diagnostics": [], "offending_faces": [], "offending_islands": [],
              "occupancy": {"summed_uv_area": None, "tile_coverage": None},
              "target": {"resolution": list(resolution), "mip_level": mip_level,
                         "margin_pixels_per_side_at_mip": margin_pixels,
                         "required_border_pixels": margin_pixels*2**mip_level,
                         "required_island_gap_pixels": 2*margin_pixels*2**mip_level,
                         "density_units": "pixels per world Blender unit",
                         "target_density": target_density, "density_ratio_limit": density_ratio_limit,
                         "anisotropy_limit": anisotropy_limit}}
    counts = validity["counts"]
    if (validity["layer"] is None or not counts["faces"] or counts["nonfinite_loops"]
            or counts["degenerate_triangles"] or any(d["code"] in ("invalid_object", "edit_mode", "invalid_layer")
                                                    for d in validity["diagnostics"])):
        result["diagnostics"].append({"code": "quality_unavailable", "message": "Resolve missing/invalid UV data before quality analysis."})
        return result
    mesh, width, height = obj.data, *resolution
    layer = mesh.uv_layers.get(validity["layer"])
    uv = [tuple(item.uv) for item in layer.data]
    islands, face_island, nonmanifold = _islands(mesh, uv)
    world = [tuple(obj.matrix_world @ vertex.co) for vertex in mesh.vertices]
    face_metrics = {face.index: {"face": face.index, "island": face_island[face.index],
                    "world_area": 0.0, "uv_area": 0.0, "pixel_area": 0.0,
                    "max_anisotropy": 1.0, "mirrored_triangles": 0, "invalid_triangles": 0}
                    for face in mesh.polygons}
    for triangle in mesh.loop_triangles:
        values = _triangle_metrics([world[i] for i in triangle.vertices],
                                   [uv[i] for i in triangle.loops], resolution)
        face = face_metrics[triangle.polygon_index]
        if values is None:
            face["invalid_triangles"] += 1
            continue
        for key in ("world_area", "uv_area", "pixel_area"):
            face[key] += values[key]
        face["max_anisotropy"] = max(face["max_anisotropy"], values["anisotropy"])
        face["mirrored_triangles"] += int(values["mirrored"])
    valid_area = sum(face["world_area"] for face in face_metrics.values())
    pixel_area = sum(face["pixel_area"] for face in face_metrics.values())
    reference_density = target_density or (math.sqrt(pixel_area/valid_area) if valid_area > 0 else None)
    bad_faces, bad_islands = set(), set()
    for face in face_metrics.values():
        face["density"] = math.sqrt(face["pixel_area"]/face["world_area"]) if face["world_area"] > 0 else None
        face["density_ratio"] = max(face["density"]/reference_density, reference_density/face["density"]) \
            if reference_density and face["density"] else None
        face["issues"] = []
        if face["density_ratio"] is not None and not math.isfinite(face["density_ratio"]):
            face["density_ratio"] = None
            face["issues"].append("density_ratio_unrepresentable")
        if face["invalid_triangles"]:
            face["issues"].append("invalid_world_or_jacobian")
        if face["max_anisotropy"] > anisotropy_limit:
            face["issues"].append("anisotropy")
        if face["density_ratio"] is not None and face["density_ratio"] > density_ratio_limit:
            face["issues"].append("density")
        if face["issues"]:
            bad_faces.add(face["face"])
            bad_islands.add(face["island"])
    result["faces"] = list(face_metrics.values())
    result["reference_density"] = reference_density
    summed_area = sum(face["uv_area"] for face in face_metrics.values())
    unique_complete = validity["ok"] and validity["overlap_checked"] and validity["overlap_complete"]
    result["occupancy"] = {"summed_uv_area": summed_area,
                           "tile_coverage": summed_area if unique_complete else None,
                           "coverage_basis": "sum of proven non-overlapping in-tile triangles" if unique_complete
                           else "unknown: sum is not a union when stacking/range/coverage is unresolved"}
    required_border, required_gap = margin_pixels*2**mip_level, 2*margin_pixels*2**mip_level
    boundaries = []
    for island in islands:
        points = [uv[index] for index in island["loops"]]
        border = min(min(p[0]*width, (1-p[0])*width, p[1]*height, (1-p[1])*height) for p in points)
        island.update(border_clearance_pixels=border, margin_deficit_pixels=max(0, required_border-border),
                      insufficient_gap_pairs=[], issues=[])
        if border < required_border:
            island["issues"].append("tile_border_margin")
        for edge in island["boundary"]:
            a, b = (edge["a"][0]*width, edge["a"][1]*height), (edge["b"][0]*width, edge["b"][1]*height)
            boundaries.append((min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]),
                               island["id"], edge["face"], a, b))
    boundaries.sort(key=lambda edge: edge[0])
    checks, complete = 0, True
    gap_pairs = {}
    for index, a in enumerate(boundaries):
        if not complete or required_gap == 0:
            break
        for other_index in range(index+1, len(boundaries)):
            b = boundaries[other_index]
            if b[0]-a[1] >= required_gap:
                break
            if checks >= max_pair_checks:
                complete = False
                break
            checks += 1
            if a[4] == b[4] or b[2]-a[3] >= required_gap or a[2]-b[3] >= required_gap:
                continue
            distance = _segment_distance(a[6], a[7], b[6], b[7])
            if distance < required_gap:
                key = tuple(sorted((a[4], b[4])))
                if key not in gap_pairs or distance < gap_pairs[key]["clearance_pixels"]:
                    gap_pairs[key] = {"islands": list(key), "faces": [a[5], b[5]], "clearance_pixels": distance}
    for pair in gap_pairs.values():
        for island_id in pair["islands"]:
            island = islands[island_id]
            island["insufficient_gap_pairs"].append(pair)
            island["margin_deficit_pixels"] += required_gap-pair["clearance_pixels"]
            if "island_gap" not in island["issues"]:
                island["issues"].append("island_gap")
    for island in islands:
        island["density"] = math.sqrt(sum(face_metrics[f]["pixel_area"] for f in island["faces"])
                                     / sum(face_metrics[f]["world_area"] for f in island["faces"])) \
            if sum(face_metrics[f]["world_area"] for f in island["faces"]) > 0 else None
        if island["issues"]:
            bad_islands.add(island["id"])
            bad_faces.update(island["faces"])
        # Loop ids permit precise selection/repair without serializing duplicate boundary coordinates.
        island.pop("boundary")
    result["islands"] = islands
    result["margin_scan"] = {"complete": complete, "pair_checks": checks, "budget": max_pair_checks,
                             "insufficient_gaps": list(gap_pairs.values())}
    if not complete:
        result["diagnostics"].append({"code": "margin_budget", "message": "Margin scan incomplete; listed gaps are only a subset."})
    if nonmanifold:
        result["diagnostics"].append({"code": "nonmanifold_uv_edge", "message": "More than two matching faces share an edge; island boundary coverage is incomplete."})
    if any(face["invalid_triangles"] for face in face_metrics.values()):
        result["diagnostics"].append({"code": "invalid_world_geometry", "message": "Some world triangles or Jacobians could not be measured."})
    if any("density_ratio_unrepresentable" in face["issues"] for face in face_metrics.values()):
        result["diagnostics"].append({"code": "density_range", "message": "Requested density ratio exceeds finite numerical range."})
    result["complete"] = complete and not nonmanifold and not result["diagnostics"]
    result["offending_faces"], result["offending_islands"] = sorted(bad_faces), sorted(bad_islands)
    result["ok"] = validity["ok"] and result["complete"] and not bad_faces
    result["status"] = "PASS" if result["ok"] else "QUALITY_WARNING" if result["complete"] else "INCOMPLETE"
    return result


def repair_uv_copy(obj, face_indices=None, island_ids=None, *, mode="MARGIN",
                   shrink_factor=0.9, **quality_options):
    """Return (accepted unlinked private-mesh object, report), or (None, report).

    Explicit opt-in. MARGIN contracts selected whole islands about their UV
    bounds centers; DENSITY uniformly scales toward target_density. Distortion
    is deliberately not 'fixed' by smoothing or inventing a new unwrap.
    Face selection expands to complete existing islands. Existing baked maps
    no longer correspond to changed UVs and MUST be rebaked on this copy.
    """
    if mode not in ("MARGIN", "DENSITY"):
        raise ValueError("mode must be MARGIN or DENSITY")
    if isinstance(shrink_factor, bool) or not isinstance(shrink_factor, (int, float)) \
            or not math.isfinite(shrink_factor) or not 0 < shrink_factor < 1:
        raise ValueError("shrink_factor must lie strictly between zero and one")
    before = inspect_uv_quality(obj, **quality_options)
    report = {"schema_version": SCHEMA_VERSION, "mode": mode, "status": "REFUSED",
              "accepted": False, "before": before, "after": None, "changes": [],
              "source_modified": False, "requires_rebake": True,
              "note": "UV preview on an unlinked private mesh; source maps are not remapped or reused."}
    if not before["validity"]["ok"] or not before["complete"]:
        report["reason"] = "Source validity and complete quality coverage are required."
        return None, report
    if mode == "DENSITY" and quality_options.get("target_density") is None:
        report["reason"] = "DENSITY repair requires an explicit target_density."
        return None, report
    all_faces = {f["face"] for f in before["faces"]}
    all_islands = {i["id"] for i in before["islands"]}
    selected_faces = set(face_indices or [])
    selected_islands = set(island_ids or [])
    if any(type(i) is not int for i in selected_faces | selected_islands) \
            or not selected_faces <= all_faces or not selected_islands <= all_islands:
        report["reason"] = "Requested face/island IDs are invalid."
        return None, report
    selected_islands.update(f["island"] for f in before["faces"] if f["face"] in selected_faces)
    if face_indices is None and island_ids is None:
        selected_islands = set(before["offending_islands"])
    if not selected_islands:
        report["status"], report["reason"] = "NO_CHANGE", "No islands selected."
        return None, report
    import bpy
    duplicate = obj.copy()
    duplicate.data = obj.data.copy()
    duplicate.name = obj.name + "_UV_QUALITY_PREVIEW"
    duplicate["import_glue_uv_quality_preview"] = True
    try:
        layer = duplicate.data.uv_layers.get(before["validity"]["layer"])
        for island in before["islands"]:
            if island["id"] not in selected_islands:
                continue
            points = [tuple(layer.data[index].uv) for index in island["loops"]]
            center = tuple((min(p[axis] for p in points)+max(p[axis] for p in points))/2 for axis in (0, 1))
            scale = shrink_factor if mode == "MARGIN" else quality_options["target_density"]/island["density"]
            if not math.isfinite(scale) or not 1e-6 <= scale <= 1e6:
                raise ValueError("requested island scale is outside the bounded repair range")
            for index in island["loops"]:
                point = layer.data[index].uv
                point[:] = (center[0]+scale*(point[0]-center[0]), center[1]+scale*(point[1]-center[1]))
            report["changes"].append({"island": island["id"], "faces": island["faces"],
                                      "loops": island["loops"], "uniform_scale": scale, "uv_center": list(center)})
        duplicate.data.update()
        after = inspect_uv_quality(duplicate, **quality_options)
        report["after"] = after
        # Independent publication validation is deliberately explicit here, even though
        # inspect invokes it: acceptance must never depend on quality heuristics alone.
        report["copy_validity"] = validate_uv(duplicate, before["validity"]["layer"], require_unique=True)
        def score(quality):
            selected = [i for i in quality["islands"] if i["id"] in selected_islands]
            return sum(i["margin_deficit_pixels"] for i in selected) if mode == "MARGIN" else \
                sum(abs(math.log(i["density"]/quality_options["target_density"])) for i in selected)
        improved = after["complete"] and score(after) < score(before)-1e-9
        report["score_before"], report["score_after"] = score(before), score(after) if after["islands"] else None
        if report["copy_validity"]["ok"] and improved:
            report.update(status="ACCEPTED_PREVIEW", accepted=True,
                          reason="Selected quality measure improved; inspect other tradeoffs before rebaking.")
            return duplicate, report
        report["reason"] = "Copy failed validity/coverage or did not improve the selected quality measure."
    except Exception as error:
        report["reason"] = "%s: %s" % (type(error).__name__, error)
    mesh = duplicate.data
    bpy.data.objects.remove(duplicate, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    return None, report
