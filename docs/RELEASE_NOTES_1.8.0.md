# Import Glue PBR Baker 1.8.0 / engine 4.2 — release notes

This release includes the work built as 1.7.0 (never published) and the new 1.8
features. It follows 1.6.0 / engine 4.0.

To roll back, install the 1.6.0 ZIP
(sha256 `54fe0445192cc96fab0630d53b66d4a1380544f3bf61ff2451a5c3a8eb064b9c`)
from the `v1.6.0-engine4.0` release.

> **Requires Blender 5.2 or newer.** 1.6.0 worked from Blender 4.3. If you are
> on an older Blender, stay on 1.6.0.

> **Test status.** The package was installed and checked in Blender 5.2.2 on
> Windows and Linux, and the full test suite ran on Linux with an NVIDIA RTX 4090.
> No import into Roblox Studio has been tested, and the panel's appearance in
> normal interactive use is not covered by the tests.

## New in 1.8: measured Roblox options

These are off by default. They apply to the **Roblox** and **Blender + Roblox**
targets and need **Allow Approximated Materials** turned on.

- **Fit Roblox Material.** Adjusts the roughness and metalness maps of each
  object by two global offsets, then renders the delivered maps against the
  original Blender material. Training views pick the best candidate, and
  separate withheld camera and light angles can reject it. If no candidate
  helps, the normal result is kept and the reason is recorded. Colour, normal
  and emissive maps are not changed. Defaults: 12 candidates and 300 seconds per
  object (**Fit Candidate Budget**, **Fit Time Budget**).
- **Capture Static Displacement.** Turns a narrow, supported kind of
  displacement (direct object-space Displacement Only, with a constant height or
  a supported static UV image) into real mesh geometry before baking and FBX
  export. Defaults: 2 subdivision levels and 20,000 triangles. Anything outside
  that subset is refused with a reason. The source mesh is not changed.
- **Large source images.** The 4K limit on verifying source images is removed.
  8K images can be fingerprinted and kept at full size in Blender Native output.
  This is memory-hungry: the 8K test peaked at about 10 GiB. Roblox maps remain
  capped at 4096 pixels.
- **Preview Targets and Cost.** A new button that reports the planned work,
  refusal reasons and estimated memory before you bake. On the command line,
  use `--preflight-only`.

## New in 1.7: native quality and recovery

- **Adaptive native sampling.** Baked inputs start at 128 pixels and double until
  they meet the error tolerance, measured against a separate bake at twice the
  resolution and two coarser mip levels. If the tolerance cannot be met, the
  original node setup is kept. Settings: **Native Absolute Error**, **Native
  Relative Error**, **Adaptive Starting Resolution**.
- **Seam padding.** Eight pixels of padding are filled from the same UV island
  (**Native Seam Padding**). Neighbouring islands are never blended together.
- **Native visual comparisons.** Checks across several lights, views and
  projected sizes, with difference images, an HTML comparison gallery and an
  optional required-PASS gate.
- **Leaner native output.** Shared images and baked inputs are stored once, and
  unreachable private nodes are removed.
- **Checkpoints for the new targets.** Accepted native inputs, finished objects
  and finished targets are each checkpointed and verified, so **Resume Saved
  Job** no longer rebakes completed work. Successful native output from a
  partial delivery can now be appended from the panel.
- **More sources handled.** Dirty UDIM tiles, proven image sequences, bounded
  OSL and IES files, custom OCIO configurations and Generated coordinates on
  supported deformed meshes. Unproven cases are still refused.

## Changed from 1.6.0

- Minimum Blender version is now **5.2** (was 4.3).
- The resource estimate now includes retained output fields, nested node groups
  and every known UDIM tile.
- Background jobs seal the fitting and displacement settings. Changing them
  invalidates matching checkpoints.
- Several 1.6.0 known limits are addressed: native seam padding and error
  control, OSL/IES capture, and Generated coordinates on deformed meshes.

## Validation

| Check | Result |
|---|---|
| Python unit tests, including the Luau importer with mocks | 74 passed |
| Filename and suffix rules | 29 of 29 |
| Blender regression on Linux, 28 suites | 435 tests: 430 passed, 5 skipped, 0 failed |
| Fitter and pipeline rerun after the final fitter change (RTX 4090) | 18 of 18 |
| ZIP installed in a clean Blender 5.2.2 profile, Windows | 12 of 12 |
| ZIP installed in a clean Blender 5.2.2 profile, Linux | 12 of 12 |

The five skipped tests are two Windows-only path cases, one read-only filesystem
case that cannot run as root, and two optional real-asset visual cases.

**Fitting example.** On a clearcoat sphere the fitter chose a roughness offset of
−0.2. Mean rendered error fell by about 22% on the training views (0.01033 to
0.00803) and about 15% on the withheld grazing view (0.02882 to 0.02439). This is
one fixture compared in Cycles, not a measurement in Roblox and not a guarantee
for other materials.

**8K example.** Hashing a full 8192 × 8192 float image took about 1.1 seconds and
about 1 GiB of extra memory. Preserving the native graph and image took about
24 seconds and peaked at 10.3 GiB.

## Known limits

- **Not tested:** import into Roblox Studio, and the panel in normal interactive
  use.
- **Fitting** has two global values per object. It is not a per-texel inverse
  renderer and cannot reproduce transmission, coat, anisotropy, volumes or
  view-dependent shading in every condition. It uses Cycles as a stand-in for
  Roblox.
- **Displacement capture** covers only the documented subset, with finite
  tessellation.
- **8K support** is for source verification and native preservation. It is not
  an 8K bake, and Roblox output stays at 4096 or below.
- **Roblox emission** still ties emitted colour to albedo, with one tint per mesh.
- **Videos** remain excluded in every target.

## Package

`import_glue_pbr_baker-1.8.0-engine4.2.zip` (35 files)

```text
sha256 5f0bd1d7e120577f3f0c69d86e42d3834d8b484391a8bff8b99e01a9800aff06
```

Requires Blender 5.2 or newer.
