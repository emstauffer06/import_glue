"""Read-only publication gate for finite, packed, nondegenerate UVs.

Counts refer to stored UV loops and Blender's loop triangles; overlaps count
triangle pairs, including pairs within the same polygon. Diagnostics retain at
most eight examples while counts remain complete unless ``overlap_complete``
is false. With ``require_unique=False`` overlap work is skipped and its count
is None (intentional stacking is allowed); all other checks still apply.

No geometric epsilon discards small but representable triangles or overlaps.
Orientation uses a floating-point error bound and exact rational fallback near
cancellation. A balanced bounding-box tree avoids all-pairs work on packed
meshes; its worst-case query work has an explicit budget. Budget exhaustion
always fails publication and marks the overlap count as a lower bound.

The only Blender cache operation is ``calc_loop_triangles``. No stored mesh,
UV, object, material, selection, mode, or file state is written.
"""
from __future__ import annotations

import math
from fractions import Fraction

_EXAMPLE_LIMIT = 8
_OVERLAP_WORK_BUDGET = 2_000_000


def _orient(a, b, c):
    """Sign of the exact determinant of three represented float coordinates."""
    if a == b or a == c or b == c:
        return 0
    x, y = (b[0] - a[0]) * (c[1] - a[1]), (b[1] - a[1]) * (c[0] - a[0])
    determinant = x - y
    # Conservative error bound for the two subtractions/products and sum.
    if abs(determinant) > (abs(x) + abs(y)) * 1e-15:
        return 1 if determinant > 0 else -1
    ax, ay, bx, by, cx, cy = map(Fraction, (*a, *b, *c))
    exact = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return (exact > 0) - (exact < 0)


def _positive_overlap(a, b):
    """Strict separating-axis test: touching boundaries have zero area.

    Input triangles are nondegenerate and counterclockwise. Their interiors
    intersect iff no edge of either triangle separates their interiors.
    """
    for first, second in ((a, b), (b, a)):
        for i in range(3):
            start, end = first[i], first[(i + 1) % 3]
            if all(_orient(start, end, point) <= 0 for point in second):
                return False
    return True


def _bounds(points):
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _bounds_intersect(a, b):
    # Positive area requires a positive interval intersection in both axes.
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _build_tree(triangles, indices):
    """Balanced median split; storage O(n), depth O(log n), no depth cap."""
    boxes = [triangles[i][2] for i in indices]
    bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes))
    last_index = max(indices)
    if len(indices) <= 8:
        return bounds, last_index, indices, None, None
    axis = 0 if bounds[2] - bounds[0] >= bounds[3] - bounds[1] else 1
    indices.sort(key=lambda i: triangles[i][2][axis] + triangles[i][2][axis + 2])
    middle = len(indices) // 2
    return (bounds, last_index, None, _build_tree(triangles, indices[:middle]),
            _build_tree(triangles, indices[middle:]))


def _overlaps(triangles):
    count, examples, work = 0, [], 0
    if len(triangles) < 2:
        return count, examples, work, True
    root = _build_tree(triangles, list(range(len(triangles))))
    for index, (points, face, bounds, source_index) in enumerate(triangles):
        stack = [root]
        while stack:
            if work >= _OVERLAP_WORK_BUDGET:
                return count, examples, work, False
            node = stack.pop()
            work += 1
            node_bounds, last_index, leaves, left, right = node
            if last_index <= index or not _bounds_intersect(bounds, node_bounds):
                continue
            if leaves is None:
                stack.extend((left, right))
                continue
            for other in leaves:
                if other <= index:
                    continue
                if work >= _OVERLAP_WORK_BUDGET:
                    return count, examples, work, False
                work += 1
                other_points, other_face, other_bounds, other_source_index = triangles[other]
                if (_bounds_intersect(bounds, other_bounds)
                        and _positive_overlap(points, other_points)):
                    count += 1
                    if len(examples) < _EXAMPLE_LIMIT:
                        examples.append({"faces": [face, other_face],
                                         "triangles": [source_index, other_source_index]})
    return count, examples, work, True


def validate_uv(obj, layer_name=None, require_unique=True):
    """Return publication eligibility, exact defect counts and repair guidance.

    ``layer_name=None`` selects the active layer; a supplied name is never
    replaced with another layer. Nonfinite loops are counted separately from
    finite out-of-range loops. Degeneracy/overlap are evaluated only for finite
    triangles; those checks are still exhaustive over that eligible subset.
    Each diagnostic gives source face/loop indices where relevant.
    """
    result = {
        "ok": False, "layer": None, "require_unique": bool(require_unique),
        "counts": {"faces": 0, "loops": 0, "triangles": 0,
                   "nonfinite_loops": 0, "out_of_range_loops": 0,
                   "degenerate_triangles": 0, "overlaps": None},
        "diagnostics": [], "overlap_checked": False, "overlap_complete": False,
        "overlap_count_is_lower_bound": False, "overlap_work": 0,
    }
    counts, diagnostics = result["counts"], result["diagnostics"]

    def diagnose(code, count, message, examples=()):
        diagnostics.append({"code": code, "severity": "ERROR", "count": count,
                            "message": message, "examples": list(examples)})

    if obj is None or getattr(obj, "type", None) != "MESH":
        diagnose("invalid_object", 1, "UV validation requires a mesh object.")
        return result
    mesh = obj.data
    # Edit-mode changes live in a BMesh, not necessarily the stored Mesh.
    if mesh.is_editmode:
        diagnose("edit_mode", 1, "Exit Edit Mode before validating the published mesh UVs.")
        return result
    counts["faces"], counts["loops"] = len(mesh.polygons), len(mesh.loops)
    layer = mesh.uv_layers.active if layer_name is None else mesh.uv_layers.get(layer_name)
    if layer is None:
        diagnose("missing_layer", 1, "Create or select the required UV layer %r before publishing."
                 % layer_name)
        return result
    result["layer"] = layer.name
    if not mesh.polygons:
        diagnose("empty_mesh", 1, "The mesh has no faces to validate or bake.")
        return result
    coordinates = [tuple(item.uv) for item in layer.data]
    if len(coordinates) != counts["loops"]:
        diagnose("invalid_layer", 1, "UV loop count does not match the mesh; rebuild the UV layer.")
        return result
    nonfinite, out_of_range = [], []
    finite = []
    for index, uv in enumerate(coordinates):
        valid = all(math.isfinite(value) for value in uv)
        finite.append(valid)
        if not valid:
            counts["nonfinite_loops"] += 1
            if len(nonfinite) < _EXAMPLE_LIMIT:
                nonfinite.append({"loop": index, "uv": list(uv)})
        elif any(value < 0 or value > 1 for value in uv):
            counts["out_of_range_loops"] += 1
            if len(out_of_range) < _EXAMPLE_LIMIT:
                out_of_range.append({"loop": index, "uv": list(uv)})
    if counts["nonfinite_loops"]:
        diagnose("nonfinite", counts["nonfinite_loops"],
                 "Replace NaN/infinite UV coordinates or unwrap the affected loops.", nonfinite)
    if counts["out_of_range_loops"]:
        diagnose("out_of_range", counts["out_of_range_loops"],
                 "Pack the affected UV coordinates inside the inclusive [0, 1] tile.", out_of_range)

    mesh.calc_loop_triangles()
    counts["triangles"] = len(mesh.loop_triangles)
    triangles, degenerate = [], []
    for triangle in mesh.loop_triangles:
        loops = tuple(triangle.loops)
        if not all(finite[loop] for loop in loops):
            continue
        points = tuple(coordinates[loop] for loop in loops)
        orientation = _orient(*points)
        if orientation == 0:
            counts["degenerate_triangles"] += 1
            if len(degenerate) < _EXAMPLE_LIMIT:
                degenerate.append({"face": triangle.polygon_index, "loops": list(loops)})
            continue
        if orientation < 0:
            points = points[::-1]
        triangles.append((points, triangle.polygon_index, _bounds(points), triangle.index))
    if counts["degenerate_triangles"]:
        diagnose("degenerate", counts["degenerate_triangles"],
                 "Unwrap collapsed or collinear UV triangles to give them nonzero area.", degenerate)
    if require_unique:
        count, examples, work, complete = _overlaps(triangles)
        counts["overlaps"] = count
        result.update(overlap_checked=True, overlap_complete=complete,
                      overlap_count_is_lower_bound=not complete, overlap_work=work)
        if count:
            diagnose("overlap", count,
                     "Repack overlapping UV faces into separate islands; shared boundaries are allowed."
                     + (" Count is a lower bound because the work budget was exhausted." if not complete else ""),
                     examples)
        if not complete:
            diagnose("overlap_budget", 1,
                     "UV overlap validation exhausted its %d-operation spatial query budget. "
                     "Reduce dense stacking/repack UVs before publication; overlap count is incomplete."
                     % _OVERLAP_WORK_BUDGET)
    result["ok"] = not diagnostics
    return result
