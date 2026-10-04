# Import Glue PBR Baker 1.3.0 / engine 3.7

The add-on and background launcher convert selected game-rip meshes into duplicated
Roblox-oriented meshes and four PBR PNG maps per part:

- `_ALB` — albedo
- `_MET` — metalness
- `_RGH` — roughness
- `_NOR` — OpenGL tangent-space normal

The source objects, source meshes, source UVs, and source materials are not
edited. The pipeline creates a new output collection and writes a manifest,
run log, resume list, and PNG files.

## Install

1. Use `import_glue_pbr_baker-1.3.0-engine3.7.zip`; do not unzip it.
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Choose **Install from Disk**, select the archive, then enable
   **Import Glue PBR Baker**.
4. In the 3D Viewport press **N**, open the **Roblox** tab, and use
   **Import Glue**.

The manifest minimum is Blender 4.3; this release was verified on Windows 5.2.0
and Linux 5.2.2. Restart an already open Blender after replacing the add-on so
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

Outputs are written under:

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
`--route`, and `--output-root`. Without `--res` / `--max-res`, headless mode
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
- The bake is synchronous because Blender data and bake operators must execute
  on Blender's main thread. Blender can appear busy during large jobs; progress
  remains visible in the system console and `run_log.txt`.
- Texture corruption checks are intentionally shallow PNG/JPEG container checks,
  not full image decodes for every external file.
