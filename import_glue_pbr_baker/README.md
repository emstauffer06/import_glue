# Import Glue PBR Baker 1.8.0 / engine 4.2 candidate

Choose **Blender Native**, **Roblox**, **Blender + Roblox** or **Legacy Four Maps**.
The default add-on target is Blender + Roblox. This candidate adds adaptive
native field sampling with island-aware padding and independent error checks,
native comparison galleries across lights/views/projected sizes, shared-field
and image reuse, private graph pruning, target/object/field recovery, and
per-target preflight with verified partial-native result append.

Native materials preserve their closure graphs. Eligible static fields become
signed/HDR float32 EXR only when the declared finite quality checks pass; other
fields remain procedural. Roblox receives five PNG8 maps (including emissive
mask), triangulated FBX and a binding manifest/Luau importer. Upload your assets
and provide their real Roblox asset IDs. Studio import testing is not part of
this candidate's validation.

Static capture includes dirty UDIM, proved sequence frames, bounded OSL/IES and
verified custom OCIO dependencies. Videos, unproved shader/geometry context and
incompatible target materials refuse explicitly. Large live-image fingerprints use a file-backed contiguous extraction with RAM
and temporary-disk admission. 8K source preservation is tested; full native capture
peaked around 10 GiB RAM on the fixture. This is not a constant-memory operation.
Custom OCIO files remain external under checksums. Finite sampling/render tests
do not establish fidelity for every shader, filter or view.

The retained **Legacy Four Maps** workflow converts selected game-rip meshes into
duplicated Roblox-oriented meshes and four PBR PNG maps per part:

- `_ALB` — albedo
- `_MET` — metalness
- `_RGH` — roughness
- `_NOR` — OpenGL tangent-space normal

The source objects, source meshes, source UVs, and source materials are not
edited. The pipeline creates a new output collection and writes a manifest,
run log, resume list, and PNG files.

## Optional Roblox approximation controls

Enable **Allow Approximation**, then **Fit Roblox Material** and/or **Capture
Static Displacement** in the Roblox delivery controls. Fitting tests bounded
roughness/metalness changes against source renders; only an improvement that
survives separate camera/light comparisons is accepted. Otherwise the baseline
is retained with an explicit reason. Albedo, normals and emissive maps stay fixed.
This is a finite Cycles comparison, not a Roblox renderer certificate.

Geometry capture supports direct object-space scalar Displacement Only, using a
constant or supported static UV height image. It refuses residual bump, vector or
arbitrary procedural displacement and unproved coordinate contexts. Subdivision
is bounded by the requested limit and the engine triangle budget. Sources remain
unchanged. Final visual checks run after fitting and geometry conversion.

## Install

1. Use `import_glue_pbr_baker-1.8.0-engine4.2.zip`; do not unzip it.
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Choose **Install from Disk**, select the archive, then enable
   **Import Glue PBR Baker**.
4. In the 3D Viewport press **N**, open the **Roblox** tab, and use
   **Import Glue**.

The minimum is Blender 5.2. Tests use Blender 5.2.2 on Windows and Linux, including
installed-package operator checks in disposable profiles. Interactive GUI appearance
is not certified by those tests. Restart Blender after replacing the add-on so
its imported Python modules reload.

To rebuild the deterministic archive from the source repository root, run:

```powershell
python tools/build_zip.py
```

## Resolution settings

The panel defaults to 1024 for both Bake Resolution and Crop Ceiling. Set the
ceiling for the asset and available memory. Higher settings are available but
can exceed memory on complex graphs. Recovery does not reduce resolution.

1. Save the `.blend`.
2. Select source meshes, or choose All Scene Meshes / Collection in Scope.
3. Leave Texture Precheck enabled. Set Shared Texture Pool only when textures
   are staged elsewhere.
4. Press **Run Import Glue**.

Native/Roblox deliveries create a unique `import_glue_<target>_<id>` directory
under Output Root, with `delivery.json`, target reports and appendable native
libraries. **Preview Targets and Cost** explains the proposed work.
**Resume Saved Job** retries its immutable snapshot and validates checkpoints.
**Append Available Results** loads a verified complete result or the successful
native library from a partial delivery.

Legacy outputs are written under:

```text
<blend folder>/<blend name>_exports/rbx_pbr_smart[N]/
```

Set Output Root to move the `<blend name>_exports` tree elsewhere.

## Safety behavior added for add-on use

- Engine caches and logging are reset between runs.
- Resume entries fingerprint source/evaluated geometry, every UV layer,
  active UV/color selectors, named/color attributes, skin weights, shape keys,
  materials and node graphs, live decoded texture pixels plus file provenance,
  output-affecting settings, the generated Blender object, and the dimensions/
  content of every output PNG. Changed inputs, edited outputs, or legacy entries
  rerun instead of silently carrying stale maps.
- Carried maps are copied into the new run folder, including across exFAT and
  other external volumes, so resumed deliverables are independent and
  self-contained.
- Selection, active object, mode, object visibility, and nested collection
  visibility/exclusion are restored before an optional save and once more when
  the operator returns.
- Output names are reserved across resumed, CROP_SPLIT, and newly planned
  objects. Existing files are never silently overwritten.
- CROP_SPLIT is transactional: a declined or failed slot removes partial PNGs,
  duplicate meshes, preview materials, and loaded preview images; incomplete
  rollback blocks the run.
- Missing materials fail planning. Black/magenta crop pixels are now graded,
  and the Roblox triangle limit is censused on the evaluated modifier result
  for every route. GRAPH_BAKE can decimate; other routes are rejected when
  still over the configured limit.
- Interactive runs default to a 16-object safety limit and refuse silent CPU
  fallback. Both can be explicitly overridden in Advanced settings.
- Texture precheck is scoped to the meshes being baked rather than every mesh
  in the file.
- Precheck follows required shader images and polygon-used material slots;
  disconnected images and unused material slots do not block the selection.
- Interactive precheck returns an operator error instead of raising
  `SystemExit` inside Blender.
- Relative texture repair will not write through a directory symlink into a
  shared pool. It repoints the image in memory instead.
- Repair Auto mode tries hardlink, symlink, then copy, which works on Windows
  machines where symlink creation is unavailable.
- Ambiguous basename matches are refused instead of silently choosing one.
- The add-on never saves on an engine-level crash.
- Blender's global backup-version preference is restored after finalization.

## Milestone 1 additions (1.4.0 / engine 3.8)

- **Material capability report and policy.** Check Textures writes
  `material_capabilities.json` beside the precheck report and shows a summary;
  every run embeds the same report in `manifest.json` (schema 3) and records a
  `capability` decision per object. Each used material is SUPPORTED,
  APPROXIMATION REQUIRED or BLOCKED, with the full node-instance path and a
  next action. By default an object that needs an approximation is refused
  (FAILED, route NONE, named reason). **Allow Approximated Materials**
  (Advanced) / `--allow-approximation` converts it anyway, labelled
  `APPROXIMATED` with every reason; such a run never ends `Clean`. BLOCKED
  objects never convert. Existing dependency errors (`NODE_UNDEFINED`,
  missing textures, Shader to RGB, Object Info Random) keep their messages and
  precedence.
- **Durable checkpoints** (on with resume). Each finished part is saved as an
  immutable, hash-verified generation under `<blend>_exports/_rbx_ckpt/`
  (maps, a self-contained output object/mesh/material payload, manifest written
  last). When a part's output is not in the open file (a reopened original, a
  crash), the run restores it from its checkpoint, re-runs the unchanged
  `validate_done_entry()` gate, and reports
  `Clean: N object(s) completed (K restored from checkpoints)`. A tampered,
  incomplete, moved-away or incompatible checkpoint is refused with a reason
  code and the part is rebaked. Done lists from 1.3.0 stay readable; they have
  no checkpoint (`NO_CHECKPOINT`).
- **Visual comparison report** (off by default). **Save Visual Comparison** /
  `--visual-validation` saves reference, output, difference and mask images
  plus a hashed `report.json` per part under `<run folder>/visual/`. Verdicts
  are PASS, FAIL, INSUFFICIENT_COVERAGE and UNSUPPORTED_REFERENCE; a skipped or
  unsupported comparison is never PASS. **Require Visual PASS (experimental)**
  / `--visual-gate` makes non-PASS verdicts fail the census; its render
  thresholds are calibrated on synthetic fixtures only.
- Blender 5.x custom split normals (`INT16_2D`) and `FLOAT4` attributes are now
  fingerprinted, so resume and checkpoints work on typical imported meshes.

See `docs/USER_GUIDE.md` for codes, settings and limits.

## Copy finalization

**Pack, Purge, and Save .blend** is off by default in the UI. When enabled, a
clean run writes `<scene>_IMPORTGLUE.blend` under the output folder by default.
Movies and supported sequences are staged beside that copy; packable images
still must pack successfully. The input blend and authored live image paths are
preserved. `FINALIZE_IN_PLACE = True` explicitly opts into saving over the source.
Filesystem saves are not covered by Blender Undo. Headless retains its default
of finalizing a clean run.

## Resilient graph baking

Supported complex graphs use three Cycles passes with packed
metallic/roughness/opacity, versus five separate passes when alpha is needed or
four for opaque jobs. Both packed and separate paths retain Blender's
RGB-to-scalar conversion. `PACK_MRA_GRAPH_BAKE = False` selects separate probes.
Proven direct sampling paths remain; filenames alone cannot prove a surface is
safe to convert through an atlas shortcut.

Required missing custom-node registrations, Shader to RGB under Cycles and
Object Info Random contexts that change on duplication fail explicitly with node
context. Unique output UVs must be finite, in range, nondegenerate and free of
positive-area overlap; shared boundaries are allowed. CROP permits intentional
source-atlas overlap. Incomplete bounded UV validation cannot publish success.

With `ALLOW_CPU_FALLBACK` enabled, eligible GPU/backend resource failures receive
one same-resolution CPU retry. Attempts, actual device and validation are recorded
in the manifest. Shader errors are not retried.

## Headless use

After extracting the folder, the familiar `-P` form still works:

```powershell
blender -b "D:\path\scene.blend" -P "D:\path\import_glue_pbr_baker\run_headless.py"
blender -b "D:\path\scene.blend" -P "D:\path\import_glue_pbr_baker\run_headless.py" -- --collection "Body" --res 1024 --max-res 1024
```

Core flags are preserved: `--only`, `--done`, `--no-resume`, `--rerun`,
`--no-finalize`, `--tag`, and `--mem-gate`. Headless exit codes are 0 for a
clean census, 1 for census failures, 2 for launcher selection/setup errors, 3
for an empty engine scope, 11 for missing material textures, and 12 for corrupt
material textures.

The package also accepts `--res`, `--max-res`, `--density`, `--device`,
`--route`, and `--output-root`. 1.4.0 adds `--allow-approximation`,
`--visual-validation`, `--visual-gate` (requires `--visual-validation`) and
`--visual-settings <file.json>`; each is off unless given, and a bad value
stops the launcher with exit code 2 before any output is written. Without `--res` / `--max-res`, headless mode
retains the engine's 4096 bake / 8192 crop defaults. Pass both explicitly for a
bounded run, using the resolution validated for your asset and machine.

`--res` and `--max-res` are ceilings. `--density` (add-on: **Bake Density**,
0.05-8.0, default 1.0) is the multiplier that decides what the Proxy and Graph
bake routes ask for before those ceilings apply: the estimate is
`sqrt(uv_area) x 4096 x density`, snapped up to a power of two. Use it when
bakes come out soft at a resolution the ceiling is not the one limiting; it
changes nothing for a part already pinned to the ceiling. The value each run
used is recorded as `config.bake_density_scale` in `manifest.json`.

The shared texture pool can be supplied through `BAKE_POOL`. The original
Linux default remains for compatibility; on this Windows farm set it explicitly
when using a pool.

## Known engine limits

- Missing MLSetup/POM implementations and source assets cannot be reconstructed
  by the baker. Registered shader groups are supported, but arbitrary materials
  and unlimited layers are not guaranteed. A native Cycles closure-limit warning
  bounds the independent 100-layer normal-reference checks.
- Portable media staging supports PNG/JPEG/TGA sequences. Other listed sequence
  formats fail explicitly. Movie staging checks readability and byte identity,
  not every frame. Native synchronous decoder calls cannot be forcibly timed out.

- Source modifiers are preserved on duplicates and reported for review; the
  add-on does not yet offer an explicit apply-evaluated-versus-preserve-rig
  policy. GRAPH_BAKE can decimate over-budget geometry, while CROP,
  CROP_SPLIT, and PROXY_BAKE fail the census rather than altering UV/material
  boundaries automatically.
- Exact live-image resume fingerprints are bounded to one 256 MiB buffer (up
  to 4K RGBA). Larger source images still process normally, but that object is
  deliberately not cached for resume instead of consuming 8K-scale memory.
- Baking runs in a separate Blender process by default. Snapshot preparation
  can briefly pause the UI. The optional synchronous mode blocks Blender during
  native baking; progress remains visible in the console and `run_log.txt`.
- Texture corruption checks are intentionally shallow PNG/JPEG container checks,
  not full image decodes for every external file.
# 1.5.0 candidate additions

The sidebar now offers a separate background Blender worker, cancellation,
checkpoint resume and verified result append. Source scenes remain open. Large
snapshots and dependency hashing can briefly block preparation; native bake
cancellation takes effect at the next safe boundary. Advanced settings include
measured resource admission and an exact GPU selector. UV diagnostics and repair
copies are available before baking.

Choose **PNG 16-bit** for float-buffer graph baking of the four existing maps.
Precision applies to loose PNGs; exporter re-encoding is unverified. Emission,
height, DDS decoding and atlas consolidation remain outside this candidate.
Native normal endpoint overshoot of at most one UNORM16 step is clamped and
reported; other out-of-range or nonfinite samples are rejected.
