"""Finite, independent UV sampling checks. These are measurements, not proofs.

No Blender dependency: raster ownership, padding, reconstruction and residuals
can be tested independently of the renderer producing the candidate samples.
"""
from __future__ import annotations

import numpy as np
from numbers import Real

DEFAULTS = {"absolute_tolerance": .025, "relative_tolerance": .01,
            "initial_resolution": 128, "margin_px": 8,
            "max_reference_resolution": 8192, "reference_scale": 2,
            "mip_levels": 2, "schema": 1}


def settings(overrides=None):
    if overrides is not None and not isinstance(overrides,dict):
        raise ValueError("Native quality settings must be an object/dictionary")
    result = {**DEFAULTS, **(overrides or {})}
    if set(result) != set(DEFAULTS):
        raise ValueError("Unknown native quality setting")
    for key in ("absolute_tolerance", "relative_tolerance"):
        if isinstance(result[key],bool) or not isinstance(result[key],Real) or not np.isfinite(result[key]) or result[key] < 0:
            raise ValueError("Native quality tolerances must be finite and nonnegative")
    for key in ("initial_resolution", "margin_px", "max_reference_resolution", "reference_scale", "mip_levels", "schema"):
        if isinstance(result[key], bool) or not isinstance(result[key], int):
            raise ValueError("Native quality integer setting required: " + key)
    if result["reference_scale"] != 2 or result["schema"] != 1:
        raise ValueError("Unsupported native quality reference/schema")
    if not 16 <= result["initial_resolution"] <= 8192 or not 32 <= result["max_reference_resolution"] <= 16384:
        raise ValueError("Native quality resolution outside supported range")
    if not 1 <= result["margin_px"] <= 32 or not 0 <= result["mip_levels"] <= 3:
        raise ValueError("Native padding/mip budget outside supported range")
    return result


def resolutions(maximum, config):
    ceiling = min(maximum, config["max_reference_resolution"] // 2)
    size = min(ceiling, config["initial_resolution"])
    while True:
        yield size
        if size == ceiling:
            return
        size = min(ceiling, size * 2)


def rasterize(triangles, island_ids, resolution):
    """Pixel-center ownership; conflicting island centers are explicitly -2.

    Triangle hit accounting prevents a subpixel face silently disappearing.
    Work is row-chunked; memory is O(atlas pixels), not O(faces * pixels).
    """
    owners = np.full((resolution, resolution), -1, dtype=np.int32)
    hits = []
    for triangle, island in zip(triangles, island_ids):
        points = np.asarray(triangle, dtype=np.float64) * resolution - .5
        a, b, c = points
        denominator = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
        if abs(denominator) < 1e-14:
            hits.append(0)
            continue
        lower = np.maximum(np.ceil(points.min(axis=0)).astype(int), 0)
        upper = np.minimum(np.floor(points.max(axis=0)).astype(int), resolution-1)
        total = 0
        for start in range(lower[1], upper[1]+1, 128):
            yy, xx = np.mgrid[start:min(start+128, upper[1]+1), lower[0]:upper[0]+1]
            u = ((b[1]-c[1])*(xx-c[0]) + (c[0]-b[0])*(yy-c[1])) / denominator
            v = ((c[1]-a[1])*(xx-c[0]) + (a[0]-c[0])*(yy-c[1])) / denominator
            inside = (u >= -1e-9) & (v >= -1e-9) & (u+v <= 1+1e-9)
            view = owners[start:start+yy.shape[0], lower[0]:upper[0]+1]
            conflict = inside & (view != -1) & (view != island)
            view[inside & (view == -1)] = island
            view[conflict] = -2
            total += int(inside.sum())
        hits.append(total)
    return owners, {"covered_pixels": int((owners >= 0).sum()),
                    "conflicting_pixels": int((owners == -2).sum()),
                    "triangles": len(hits), "unsampled_triangles": sum(n == 0 for n in hits)}


def _shift(array, dy, dx, fill):
    result = np.full_like(array, fill)
    h, w = array.shape[:2]
    result[max(0,dy):min(h,h+dy), max(0,dx):min(w,w+dx)] = array[max(0,-dy):min(h,h-dy), max(0,-dx):min(w,w-dx)]
    return result


def pad_islands(values, owners, margin):
    """Extend nearest Chebyshev shells; competing owners never blend.

    Colliding shells are left unowned and reported. Existing samples, including
    adjacent islands, are never overwritten. No implicit periodic wrap occurs.
    """
    result, labels = np.array(values, copy=True), owners.copy()
    collisions = np.zeros(owners.shape, dtype=bool)
    original = owners >= 0
    for _ in range(margin):
        proposal = np.full_like(labels, -1)
        sums = np.zeros_like(result)
        counts = np.zeros(labels.shape, dtype=np.int16)
        conflict = np.zeros(labels.shape, dtype=bool)
        for dy, dx in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
            neighbor = _shift(labels, dy, dx, -1)
            valid = (labels == -1) & ~collisions & (neighbor >= 0)
            conflict |= valid & (proposal >= 0) & (proposal != neighbor)
            proposal[valid & (proposal == -1)] = neighbor[valid & (proposal == -1)]
            shifted = _shift(result, dy, dx, 0)
            sums[valid] += shifted[valid]
            counts[valid] += 1
        accepted = (proposal >= 0) & ~conflict
        result[accepted] = sums[accepted] / counts[accepted,None]
        labels[accepted] = proposal[accepted]
        collisions |= conflict
    return result, labels, {"requested_margin_px": margin,
                            "padded_pixels": int(((labels >= 0) & ~original).sum()),
                            "competing_padding_pixels": int(collisions.sum()),
                            "policy": "island-owned shell extension; collisions never mixed"}


def bilinear(values, uv):
    h, w = values.shape[:2]
    xy = np.asarray(uv) * (w, h) - .5
    lower = np.floor(xy).astype(np.int64)
    fraction = xy-lower
    x0, y0 = np.clip(lower[:,0],0,w-1), np.clip(lower[:,1],0,h-1)
    x1, y1 = np.clip(lower[:,0]+1,0,w-1), np.clip(lower[:,1]+1,0,h-1)
    x, y = fraction[:,0,None], fraction[:,1,None]
    return ((1-x)*(1-y)*values[y0,x0] + x*(1-y)*values[y0,x1]
            + (1-x)*y*values[y1,x0] + x*y*values[y1,x1])


def downsample(values):
    h, w = values.shape[:2]
    h, w = h//2*2, w//2*2
    return values[:h,:w].reshape(h//2,2,w//2,2,-1).mean(axis=(1,3))


def measure(candidate, reference, reference_owners, config):
    """Compare against a separate 2x bake at different texel centers.

    All covered reference samples count, including island boundaries. Mip
    comparisons use ideal box mipmaps and report finite-filter behavior; GPU
    anisotropy/view derivatives require the separate render comparison suite.
    """
    if not np.isfinite(candidate).all() or not np.isfinite(reference).all():
        return {"accepted": False, "reason": "nonfinite samples"}
    records = []
    for level in range(config["mip_levels"]+1):
        resolution = reference.shape[0]
        mask = reference_owners >= 0
        count, total, square, maximum, ratio = 0, 0., 0., 0., 0.
        for row in range(0, resolution, 128):
            yy, xx = np.nonzero(mask[row:row+128])
            yy += row
            if not len(xx):
                continue
            actual = bilinear(candidate, np.stack(((xx+.5)/resolution, (yy+.5)/resolution), axis=1))
            expected = reference[yy,xx]
            error = np.abs(actual-expected)
            allowance = config["absolute_tolerance"] + config["relative_tolerance"]*np.abs(expected)
            normalized = np.divide(error, allowance, out=np.full_like(error, np.inf), where=allowance > 0)
            normalized[(allowance == 0) & (error == 0)] = 0
            count += int(error.size)
            total += float(error.sum(dtype=np.float64))
            square += float(np.square(error,dtype=np.float64).sum())
            maximum = max(maximum, float(error.max()))
            ratio = max(ratio, float(normalized.max()))
        records.append({"level": level, "samples": count//3, "max_absolute": maximum,
                        "mean_absolute": total/max(count,1), "rms": (square/max(count,1))**.5,
                        "max_tolerance_ratio": ratio, "accepted": count > 0 and ratio <= 1})
        if min(candidate.shape[:2]) < 4 or level == config["mip_levels"]:
            break
        candidate, reference = downsample(candidate), downsample(reference)
        # Retain a mip center only when its whole reference footprint belongs
        # to one island; mixed boundaries remain covered by level-zero test.
        labels = reference_owners[:resolution//2*2,:resolution//2*2].reshape(resolution//2,2,resolution//2,2)
        low, high = labels.min(axis=(1,3)), labels.max(axis=(1,3))
        reference_owners = np.where((low == high) & (low >= 0), low, -1)
    return {"accepted": all(r["accepted"] for r in records), "levels": records,
            "absolute_tolerance": config["absolute_tolerance"], "relative_tolerance": config["relative_tolerance"],
            "reference": "independent 2x Cycles bake; different texel centers", "boundary_samples_included": True,
            "scope": "finite UV and box-mip samples; not a formal frequency or arbitrary-view guarantee"}
