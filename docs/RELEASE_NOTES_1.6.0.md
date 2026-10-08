# Import Glue PBR Baker 1.6.0 / engine 4.0 — release notes

The first public release since 1.4.0. It includes the background-job work that
was built as 1.5.0 (never published) and adds two new delivery targets:
Blender Native and Roblox.

Baseline: 1.4.0 / engine 3.8. To roll back, install the 1.4.0 ZIP
(sha256 `18d54254ac43698e87cd3af6a9213e7853957393a22f395188d9f3f17ca4f07f`)
from the `v1.4.0-engine3.8` release.

> **Test status, read this first.** 1.6.0 was validated in headless Blender
> 5.2.2 on Linux, on CPU and on one NVIDIA RTX 4090. The Blender GUI on Windows
> has not been tested with this build, and no Roblox Studio import has been
> verified. Install it in a spare Blender profile first if you depend on 1.4.0.

## New: delivery targets

The panel's **Delivery Target** now defaults to **Blender + Roblox**.

- **Blender Native.** Keeps the shader structure (closures, nested groups,
  Principled layers) and bakes eligible static colour, scalar and vector inputs
  into signed, scene-linear float32 EXR on the existing UV layout. Anything not
  converted stays in the material. A part with nothing to convert is reported as
  `RETAINED_ONLY`. Results are saved as an appendable `native_outputs.blend`.
- **Roblox.** Five PNG8 maps per part: colour (RGB or RGBA), OpenGL tangent
  normal, metalness, roughness and an emissive mask. The mesh is triangulated
  and exported as FBX with tangents. A JSON manifest and a generated Luau
  importer bind the maps to exact mesh names. You import the mesh, upload the
  textures yourself, and give the importer your real asset IDs.
- **Blender + Roblox.** Runs both from the original inputs. If Roblox refuses a
  layered material but the native result succeeds, the delivery is reported as
  `PARTIAL` and is not appended automatically.
- **Legacy Four Maps.** The 1.4.0 workflow (`_ALB`, `_MET`, `_RGH`, `_NOR`) with
  its checkpoints. The command line still defaults to this.

**Emission for Roblox.** Roblox ties emitted colour to albedo and allows one
tint and one strength per mesh. The exporter fits a mask, tint and strength to
the baked emission, measures the result against the delivered colour map, and
refuses the part when the error exceeds 1% maximum or 0.2% mean, unless you
explicitly allow approximation. Approximated parts are labelled as such.

## New: background jobs

- **Bake in Background** is on by default. The bake runs in a separate Blender
  process from a saved snapshot of your scene.
- **Cancel After Current Pass** stops at the next safe point and keeps finished
  checkpoints. **Terminate Owned Worker** kills only a worker this Blender
  session started.
- **Resume Saved Job** starts a new job from the original snapshot and reuses
  verified checkpoints. It refuses to resume if the code or inputs changed.
- **Append Completed Results** verifies the result file, its images and the
  manifest, then adds a new collection. Source objects are kept.
- **Recover Job Folder** (Advanced) reattaches to a job after reopening Blender.

## New: source snapshots

Each job works from a private snapshot with packed texture copies. Unsaved
texture painting is captured. A fresh Blender process verifies the snapshot
before baking, and your original file, images and materials are left unchanged.

## New: resources, UVs and precision

- **Resource measurement.** Free RAM on Windows and Linux and free VRAM on NVIDIA
  cards are measured before a job. **Require Resource Headroom** is an optional
  gate, off by default. Resolution is never reduced silently.
- **One GPU, chosen explicitly.** You can select a single Cycles GPU by ID or
  unique name.
- **UV diagnostics.** Reports texel density, distortion and mip-aware island
  clearance. Repair runs on a separate copy of the mesh.
- **PNG16.** The four legacy maps can be baked as true 16-bit PNG from float
  buffers on the graph-bake route.

## Changed from 1.4.0

- The panel default is now **Blender + Roblox**, not four maps. Pick **Legacy
  Four Maps** for the 1.4.0 behaviour.
- The new targets always bake from source. They do not reuse four-map
  checkpoints.
- Video textures are excluded in every target, and the approximation option does
  not override that.
- Restart Blender after replacing the add-on so its modules reload.

## Validation

All Blender results are from headless Blender 5.2.2 on Linux.

| Check | Result |
|---|---|
| Python unit tests (Windows) | 53 passed |
| Filename and suffix rules | 29 of 29 |
| Blender regression, 20 suites | 347 executions: 342 passed, 5 skipped |
| Blender Native suite | 13 passed |
| Source capture suite | 12 passed |
| Roblox suite | 6 passed |
| Legacy four-map fidelity | 26 of 26 |
| Resume invalidation | 12 of 12 |
| ZIP installed in a clean Blender profile | 12 of 12, including a native + Roblox bake |

The five skipped cases are two production-asset visual cases (no asset corpus
configured), two Windows-only path cases, and one read-only filesystem case.

A CPU run and an RTX 4090 (OptiX) run of the same fixture produced identical
native EXR roughness and identical metalness, roughness and normal PNGs. The
reconstructed Roblox emission differed by 0.56% maximum and 0.18% mean, inside
the 1% / 0.3% cross-device thresholds. This is an offline check against a
reference model, not a comparison inside Roblox Studio.

## Known limits

- **Not yet tested:** the Blender GUI on Windows, and Roblox Studio import
  (tangents, alpha, emission, texture compression).
- **Blender Native:** no seam padding or adaptive error control yet. Static
  fields need unique, valid UVs or they stay native. External OSL and IES files
  are refused. Generated coordinates on modifiers and shape keys are not handled.
- **Roblox:** black albedo cannot emit, and one tint is shared across a mesh.
  The texture size limit is a setting (default 1024, up to 4096), not a claimed
  engine limit. There is no complete Marketplace export profile.
- **PNG16:** applies to the four loose PNG files. GLB and FBX re-encoding is not
  certified to preserve it.
- **Resources:** VRAM measurement is NVIDIA only, so the headroom gate may refuse
  AMD, Apple and Intel GPUs that Cycles can still bake on.
- **Not implemented:** atlas consolidation, UDIM remapping, DDS decoding,
  multi-worker scheduling, supersampling, and height baking.
- **Packaging note:** the README inside the ZIP still carries a 1.5.0 heading
  and file name. The package itself is 1.6.0 / engine 4.0.

## Package

`import_glue_pbr_baker-1.6.0-engine4.0.zip`

```text
sha256 54fe0445192cc96fab0630d53b66d4a1380544f3bf61ff2451a5c3a8eb064b9c
```

Requires Blender 4.3 or newer.
