# Import Glue PBR Baker 1.4.0 — user guide

Turns selected game-rip meshes into a portable four-map PBR set:
`_ALB` colour, `_NOR` normal, `_RGH` roughness, `_MET` metalness, plus a
manifest and duplicated output meshes.

This guide covers the 1.4.0 additions (material capability report and policy,
durable checkpoints, visual comparison report), the 1.3.0 improvements, earlier
source-safety changes, what the tool refuses to guess at, and what to do when it
stops.

## Installing

Manifest minimum: Blender 4.3. This release was tested on **5.2.0 LTS (Windows)**
and **5.2.2 LTS (Linux)**; other versions have not been validated in this release.

Edit > Preferences > Add-ons > Install from Disk, pick
`import_glue_pbr_baker-1.4.0-engine3.8.zip`, enable it. The panel is at
3D Viewport > Sidebar (N) > **Roblox** > Import Glue.

## Normal operation

1. Open the rip. Select the mesh objects you want converted.
2. Set **Output Root** if you do not want output beside the .blend.
3. Click **Check Textures**. It reports missing or corrupt references and, by
   default, resolves what it can *for this run only*.
4. Click **Run Import Glue**.

You get, per object: four PNGs, a rebuilt material on a duplicate mesh, and an
entry in `manifest.json`. The run ends with a census line and, headless, a
non-zero exit code if anything failed. **"Saved" is never "worked"** — read the
`OVERALL | CLEAN` / `FAIL` line, not the `DONE | OK=` line.

Headless:

```bash
blender -b scene.blend --factory-startup --python run_headless.py -- --only ObjectName
```

## What changed in 1.4.0 (Milestone 1)

Engine 3.8. Three additions; none of them changes how a supported material is
baked:

1. **Material capability report and policy.** Before converting, every used
   material is classified SUPPORTED, APPROXIMATION REQUIRED or BLOCKED, with the
   exact node instance and a next action. By default an object that needs an
   approximation is **refused** with a named reason instead of being converted
   silently.
2. **Durable checkpoints.** Every finished part is also saved as a verified
   checkpoint. Reopening the original file, or a crashed run, restores finished
   parts instead of baking them again.
3. **Visual comparison report** (optional, off by default). Renders and bakes
   the source beside the converted part and saves reference, output and
   difference images with a PASS / FAIL / INSUFFICIENT_COVERAGE /
   UNSUPPORTED_REFERENCE verdict.

Also fixed: the engine now fingerprints Blender 5.x custom split normals
(`INT16_2D custom_normal`) and `FLOAT4` attributes. Up to 1.3.0 every such mesh
(most imported game meshes) baked but silently lost its resume entry.

### Compatibility with 1.3.0

- Map names, channel encoding, resolutions, routes, exports and the output
  folder layout are unchanged. A SUPPORTED material converts exactly as in
  1.3.0.
- **Default change:** objects whose materials are APPROXIMATION REQUIRED
  (transmission, coat, sheen, subsurface, view-dependent inputs, closure
  mixes, displacement, ...) now fail with `material capability
  REJECTED_APPROXIMATION ...` instead of being converted. To get the 1.3.0
  conversion of those objects, enable **Allow Approximated Materials**
  (Advanced) or pass `--allow-approximation` headless. Their outputs are
  labelled `APPROXIMATED` in the manifest and in the final status, never
  `Clean`.
- **Default change that hits most ripped materials: non-standard IOR or
  Specular IOR Level.** A Principled BSDF whose effective dielectric
  reflectance is not F0 0.04 (anything other than IOR 1.5 with Specular IOR
  Level 0.5, within float noise of 1e-6) is now APPROXIMATION REQUIRED
  (`PBR_INPUT_UNREPRESENTED`, specular) and is **refused by default**. 1.3.0
  converted these silently and wrote maps that render with F0 0.04; Roblox's
  SurfaceAppearance maps (colour, metalness, roughness, normal) have no IOR
  or specular-level input at all, so the delivered part cannot reproduce the
  difference. Rips exported before Blender 4.0 commonly carry IOR 1.45, so
  expect many refusals: on the Jacobs_Ship scene (2,475 meshes) the census
  refuses **1,413 objects** by default, **1,245 of them for this reason
  alone**; 313 of its 511 materials are approximation-required only because
  of IOR (399 Principled nodes at IOR 1.45). To convert them anyway, enable
  **Allow Approximated Materials** (Advanced) or pass `--allow-approximation`
  headless; the outputs are labelled `APPROXIMATED` and the run never reports
  `Clean`. Setting the material to IOR 1.5 / Specular IOR Level 0.5 (on a
  copy) makes it SUPPORTED instead. Details: "Specular reflectance" below.
- **New refusals that cannot be opted in:** surface paths the bake does not
  reproduce (a muted shader, a Reroute carrying the shader, a colour wired
  straight into a shader socket, an empty Mix/Add Shader), the EEVEE-only
  Specular BSDF, and node types or Principled inputs without a rule. 1.3.0
  converted these into maps that do not show what Cycles renders.
- `manifest.json` is schema 3. Its additions are keyed (`material_capabilities`,
  per-object `capability`, `resume.checkpoints`, `restored_from_checkpoint`,
  `visual_validation`), so readers of schema 2 that ignore unknown keys keep
  working.
- Done lists (schema 6, entry schema 4) are unchanged and still readable. Because
  the engine build changed, every 1.3.0 done entry is `CONFIG_CHANGED` once and
  the part is rebaked; a 1.3.0 entry has no checkpoint, which the log and
  `resume.checkpoints.restore_attempts` say explicitly (`NO_CHECKPOINT`).
- Headless options (all off unless given): `--allow-approximation`,
  `--visual-validation`, `--visual-gate`, `--visual-settings <file.json>`.

### Material capability report

#### What it tells you

Before converting anything, Import Glue can tell you, for every material your
selected meshes actually use, whether the delivered map set can carry it:

| Status | Meaning |
| --- | --- |
| **SUPPORTED** | Every required effect maps onto a delivered channel: base colour (with straight alpha), metalness, roughness and tangent-space normal. This is a classification, not a measured visual match; the visual comparison report is a separate check. |
| **APPROXIMATION REQUIRED** | The material can be converted only by dropping or freezing an effect the maps cannot hold (for example transmission, or a Layer Weight facing ramp). |
| **BLOCKED** | It cannot be converted from what is present, or the bake would produce something the render does not show: a missing node registration, a missing or corrupt texture, an EEVEE-only node, an empty material slot, a material slot that only a modifier uses, a node type or Principled input the report has no rule for, or a surface path the bake does not reproduce (a muted closure, a Reroute carrying the closure, a colour wired straight into a shader socket, a Mix/Add Shader with nothing connected). |

Every non-supported verdict names the exact node instance, including nested
group instances (for example
`Cockpit Material/MLSetup Layer 07/Parallax Occlusion Map`), the reason, and
what to do next.

Only what is required counts. Unused material slots, disconnected nodes,
disconnected missing textures and the inactive branch of a Mix Shader with a
constant 0 or 1 factor never affect the verdict. These rules are the same ones
the bake itself uses to decide what is required.

"Used" means used by the **evaluated** mesh, the one Cycles renders and bakes.
A modifier can put faces on a slot that no face of the base mesh uses
(Solidify's material offset) or add a material of its own (Geometry Nodes
*Set Material*). Those materials are analysed like any other, and their
missing textures fail the dependency check with the usual message. The object
itself is BLOCKED with `MODIFIER_MATERIAL_SLOT`, even when the material is
supported: the conversion builds its bake and crop materials from the base
mesh's slots, so those faces would get an invented default surface (in
1.3.0 they silently baked as flat grey). Apply the modifier on a copy, or
assign the material to base-mesh faces, and convert that. Modifiers that keep
the base mesh's slots (Mirror, Array, a Solidify offset into a slot the base
mesh already uses) are not affected. An object outside the view layer is not
evaluated; its report says so (`MODIFIER_MATERIALS_NOT_EVALUATED`) and it is
analysed again when it is converted.

The report is read-only. It does not change routes, install add-ons, repair
paths, rewrite materials or write next to your .blend. Reports are plain JSON
and never contain Blender memory addresses; images are described by name,
authored path and resolved path.

#### Default policy: approximation is refused

By default an APPROXIMATION REQUIRED object is **not converted**. To convert
it anyway, enable **Allow Approximated Materials** (Advanced) or pass
`--allow-approximation` headless. The output is then labelled
`APPROXIMATED`, never `SUPPORTED`, and the manifest keeps every reason. BLOCKED
objects are never converted, with or without the opt-in.

Existing failures keep their established names and messages: a missing
custom node is still `NODE_UNDEFINED`, and a missing texture still fails the
texture precheck or `REQUIRED_IMAGE_INVALID`. An empty used slot still fails
with its existing message while the slot rule is on (the default). If a
headless configuration turns that rule off (`CENSUS_FAIL_ON_NOMAT = False`,
the legacy neutral-grey path), the capability policy refuses such an object
itself, naming the empty slot and anything else it would have approximated.
The report adds detail; it does not replace those gates.

Every decision names the object **and its library**, so a linked object and a
local object with the same name never share a verdict.

#### What you will see

| Code | Status | It means / what to do |
| --- | --- | --- |
| `NODE_UNDEFINED` | Blocked | A node's add-on is not installed (for example the ship's 33 `Parallax Occlusion Map` instances). Install the original implementation; Import Glue never substitutes a guessed one. |
| `REQUIRED_IMAGE_INVALID` | Blocked | A required texture is missing or corrupt at the reported path. Restore it or point **Shared Texture Pool** at it. |
| `IMAGE_UNASSIGNED`, `GROUP_MISSING`, `GROUP_OUTPUT_MISSING`, `GRAPH_CYCLE` | Blocked | Broken required graph; fix the named node. |
| `CYCLES_UNSUPPORTED_NODE` | Blocked | Shader to RGB, or the EEVEE-only Specular BSDF (Cycles renders it black). Replace it with Cycles nodes. |
| `OBJECT_RANDOM_CONTEXT` | Blocked | Object Info Random would change on the bake copy. |
| `UNRECOGNIZED_NODE`, `UNVERIFIED_NODE`, `SCRIPT_NODE` | Blocked | A node whose behaviour has no rule or no evidence (Python-only nodes, OSL scripts, Blender 5 closure/bundle/repeat zones). |
| `EMPTY_MATERIAL_SLOT`, `MISSING_MATERIAL_SLOT` | Blocked | Faces use a slot with no material. |
| `MODIFIER_MATERIAL_SLOT` | Blocked | A modifier puts faces on a material slot no base-mesh face uses; the conversion would give them an invented surface. Apply the modifier on a copy and convert the copy. |
| `MODIFIER_MATERIALS_UNVERIFIED` | Blocked | The modifier stack failed to evaluate, so the materials its faces use are unknown. |
| `PRINCIPLED_INPUT_UNCLASSIFIED` | Blocked | A Principled input this rules version does not know (a newer Blender). Its effect is unknown, so the material is not converted until a rule backed by evidence is added. |
| `MUTED_CLOSURE` | Blocked | A muted shader on the surface path. Cycles renders it as nothing, but the bake would read its inputs as if it were active. Unmute it or remove it. |
| `CLOSURE_PASSTHROUGH` | Blocked | A Reroute (or other pass-through node) carries the shader. The bake replaces it with a default grey probe and loses the shader behind it. Connect the shader directly; reroutes on colour or value links are fine. |
| `DATA_AS_SHADER` | Blocked | A colour or image is wired straight into a shader socket. Cycles shows it as emission, but the bake writes its red, green and blue into metalness, roughness and alpha. Feed it into a shader's colour input instead. |
| `NO_SURFACE_CLOSURE` | Blocked | The Surface input is connected, but no active shader reaches it (an empty Mix/Add Shader or group output). Cycles renders black; the bake would write a transparent mirror. |
| `PBR_INPUT_UNREPRESENTED` | Approximation | A Principled input carries transmission, subsurface, coat, sheen, anisotropy, thin film, diffuse roughness, emission, tinted or non-standard specular. Set it to its neutral value or opt in. |
| `CLOSURE_UNREPRESENTED` | Approximation | A non-Principled closure (Diffuse, Glossy, Emission, Glass, volume...) or a tinted Transparent BSDF; the message says how the bake maps it. |
| `CLOSURE_MIX_BLEND`, `CLOSURE_ADD` | Approximation | A Mix Shader with a factor that is not provably 0 or 1, or an Add Shader. Cycles blends shading lobes; the maps store one blend of parameters, which agrees only where the factor is exactly 0 or 1. |
| `VIEW_DEPENDENT_INPUT`, `CONTEXT_DEPENDENT_INPUT`, `TIME_DEPENDENT_INPUT`, `TIME_DEPENDENT_IMAGE`, `ANIMATED_MATERIAL` | Approximation | Fresnel/Layer Weight/Light Path/camera inputs; AO/Bevel/Wireframe and the topology-dependent Geometry outputs (Pointiness, Random Per Island, Parametric); coordinates read From Instancer; scene time, movies and image sequences, keyframes or drivers. A bake stores one fixed evaluation of the copy it bakes. |
| `OUTPUT_UNREPRESENTED` | Approximation | The material output's Displacement or Volume input is connected; there is no height or volume map. |
| `SURFACE_FALLBACK` | Approximation | Nothing reaches the Cycles Surface output, so the bake would substitute viewport display values. |
| `CUSTOM_GROUP_EVALUATED`, `PROCEDURAL_NODE`, `MODIFIER_MATERIALS_NOT_EVALUATED` | Information | Recorded for review; they do not change the status. |

##### Specular reflectance

A single metallic-roughness surface has a fixed dielectric reflectance
(F0 0.04, which is IOR 1.5 at Specular IOR Level 0.5). The delivered maps
cannot carry IOR or Specular IOR Level, so the report computes the material's
effective F0 and reports any other value as `PBR_INPUT_UNREPRESENTED`
(specular), an approximation. Only float noise (1e-6) is tolerated. Files made
before Blender 4.0 commonly carry IOR 1.45 (F0 0.034); those materials need
the opt-in, or IOR 1.5 if the small change in specular strength is not wanted.
A fully metallic Principled (Metallic 1) has no dielectric reflectance and is
not checked; an IOR or Specular IOR Level driven by a texture or the graph
cannot be proven to be F0 0.04 and is reported the same way.

This is the **owner-level decision on final review finding 2**, recorded as
implemented (rules version 3): the tolerance is 1e-6, not a calibrated
"close enough" band, because no nonzero band passes the visual comparison's
render standard at every roughness (below). An approximation-required
material is refused by default, converted only under **Allow Approximated
Materials** / `--allow-approximation`, labelled `APPROXIMATED` with the
measured F0 difference in its reason, and never counted as `Clean`. The
Roblox maps cannot carry IOR, so there is no setting that would make such a
part match its source; the opt-in accepts the F0 0.04 look knowingly.

This is measured, not assumed. `tests/blender/calibrate_f0_tolerance.py`
compares a sphere at each F0 with the same sphere at F0 0.04 under the visual
comparison's own render presets and tolerances. Under the hard grazing and
highlight lights the local maximum grows by up to 579 per unit of F0
difference at roughness 0.05 on Windows (Blender 5.2.0) and 598 on Linux
(5.2.2); 421 at 0.1, 231 at 0.2 and 8 at 1.0 on both. So an F0 difference of
only 0.0001 already reaches the 0.06 limit on a glossy surface, and IOR 1.45
fails at every roughness up to 0.5 on both hosts. No nonzero tolerance holds
for every roughness. (1.4.0 candidates before the final review accepted a
difference of up to 0.01, calibrated against the surface colour mean under a
uniform world, which hid this.)

#### Where the report is written

**Check Textures** writes `material_capabilities.json` beside the precheck
report (`<blend>_exports/precheck/`) and shows a summary such as
`Material capability (PBR_BASE): BLOCKED; objects: 214 supported,
0 approximation required, 170 blocked; materials: 7 supported,
0 approximation required, 0 blocked` (the closet221 example: every material is
supported, but 170 meshes have no material) followed by the most severe
findings. A run embeds the same report in `manifest.json` under
`material_capabilities`, and each converted object carries its own
`capability` decision.

A run that converted anything under **Allow Approximated Materials** ends with
`Completed: N object(s), M APPROXIMATED under the explicit opt-in (not
faithful; see manifest)` and a warning, never with `Clean`.

If the report cannot be written (a read-only or occupied destination, or a
file path that is not valid text), the check says so and names the path; it
never leaves a partial file behind, and it never stops a Run. The file gets
the same permissions as any other file you create there.

#### Known limits

- SUPPORTED is a classification against the channel contract. It does not
  prove a pixel-level match; use the visual comparison report for that.
- Rules exist for every built-in shader node in Blender 5.2. A newer Blender
  that adds nodes or Principled inputs makes the report block or flag them
  until a rule is added; the regression suite detects this.
- A Mix Shader driven by a texture mask is reported as an approximation even
  when the mask happens to be strictly black and white; the report does not
  read image pixels.
- Relative texture paths inside linked libraries are resolved like the
  existing precheck does (against the open .blend), which can misreport them.

### Durable checkpoints

#### Finished parts survive a reopen or a crash

Before this change, resuming a finished part needed its generated output object
to still be in the open .blend. Reopen the original file, or lose Blender in the
middle of a run, and every finished part was baked again.

Now each part that completes is also saved as a **checkpoint**: a verified copy
of its four maps plus its output object, mesh and material, in a folder beside
the done list:

```
<blendname>_exports/
    _v32_done.json
    _rbx_ckpt/<object>-<hash>/g_<time>-<id>/
        maps/<part>_ALB.png  _MET.png  _RGH.png  _NOR.png
        payload.blend
        manifest.json
    rbx_pbr_smart/ ...
```

On the next run, an object whose output is missing from the open file is
restored from its checkpoint instead of rebaked, and the run reports it:
**"Clean: 3 object(s) completed (2 restored from checkpoints)"**. The manifest
marks each one with `restored_from_checkpoint`, and the log prints
`RESUME: <object> restored from checkpoint <id> as <output>`.

The setting is **Durable Checkpoints** (Advanced, under Resume from Done List).
It is on by default and only works while resume is on.

#### When a checkpoint is not used

A checkpoint is only used when it is still exactly right. Each refusal names
its reason in the log and in `manifest.json` under
`resume.checkpoints.restore_attempts`, and that object is baked again:

| Reason code | Meaning |
| --- | --- |
| `SOURCE_CHANGED` | the source mesh, UVs, materials, images or modifiers changed |
| `CONFIG_CHANGED` | resolution, routing, bake or discovery settings changed, or the engine build changed |
| `BLENDER_CHANGED` | the checkpoint was written by a different Blender release (major.minor) or file-format version |
| `DEPENDENCY_BLOCKED` | a required texture or node is missing or unsupported now |
| `CAPABILITY_REFUSED` | the material capability policy refuses the part now (for example a checkpoint written under **Allow Approximated Materials**, read by a default run) |
| `ARTIFACT_HASH`, `ARTIFACT_SIZE`, `ARTIFACT_MISSING` | a checkpoint file was edited, damaged or deleted |
| `CONTENT_MISMATCH` | the saved object (its settings, data or final evaluated shape) or maps no longer match what was recorded |
| `PATH_ESCAPE` | the checkpoint points outside its own folder (including through a link) |
| `MANIFEST_MALFORMED`, `MANIFEST_INTEGRITY`, `SCHEMA_INCOMPATIBLE` | the manifest is damaged, edited or from another version |
| `PAYLOAD_UNSAFE` | the saved .blend carries something a checkpoint never writes (a script, a driver, extra objects) |
| `CHECKPOINT_MISSING` | the checkpoint is neither where it was written nor under the current output root's `_rbx_ckpt` folder (deleted, or moved on its own) |
| `CHECKPOINT_CHANGED`, `CHECKPOINT_MALFORMED` | the done list points at a different manifest than the one it recorded, or its reference is damaged |
| `NO_CHECKPOINT` | the part was finished by an older version, or its checkpoint could not be written |

The cheap refusals (schema, Blender version, source, dependencies, policy,
settings, source changes) are decided from the sealed manifest first, so an
edited source costs no hashing of the checkpoint's files. Every file is still
hashed before anything is loaded.

A refused checkpoint never changes your scene: anything the attempt loaded is
removed again. A checkpoint is never "repaired", and the engine's own resume
checks are not relaxed for restored parts. A restored part gets a new output
object, so its runtime fingerprint is regenerated. What binds it to the part
that was baked is the checkpoint's canonical content fingerprint, which covers
the object's transform, mesh data and UVs, every modifier setting, its final
evaluated shape, materials and the exact map bytes. On top of that, the part
must pass the engine's own dependency, UV and done-entry checks.

A Blender patch update (for example 5.2.0 to 5.2.1) keeps checkpoints usable,
and the restore reports both versions. A new Blender release, or one that saves
.blend files in a newer format, gives `BLENDER_CHANGED`, and the parts are
rebaked once.

#### When a checkpoint is not written

Some outputs cannot be captured faithfully, and the tool says so rather than
saving an approximation. The part still completes normally; the run ends with a
resume warning such as
`durable checkpoint unavailable for Gun: UNSUPPORTED: output is parented to 'Armature'`.
The current limits:

- outputs parented to another object, or with constraints;
- outputs with animation data or drivers (checkpoints never store or run scripts);
- outputs whose materials reference anything other than the part's four
  published maps (packed or generated images, other objects);
- modifiers whose result depends on state outside their settings: simulations
  and caches (cloth, soft body, collision, dynamic paint, fluid, particles,
  explode, ocean, mesh caches), Skin, bound Mesh Deform / Surface Deform /
  Laplacian Deform / Corrective Smooth, Multires with subdivision levels, and
  Geometry Nodes with bakes or simulation zones;
- modifiers that point at other objects, and any setting the content
  fingerprint cannot represent exactly.

Every other modifier is captured with all of its settings, nested ones
included (Geometry Nodes inputs, custom bevel profiles, falloff curves).

Such a part can still resume while its output object is in the open file, as
before.

#### Interrupted runs

A checkpoint is written to a private folder first, every file is flushed and
re-read, the saved object is loaded back and compared, and the `manifest.json`
that makes it count is written **last**. If Blender stops at any point before
that, the half-written folder is ignored and any earlier checkpoint of the same
object stays usable. This was tested by killing Blender at every step on
Windows and Linux. It was tested on local disks only; no guarantee is made for
network drives.

#### Disk use and cleanup

Each checkpoint holds one extra copy of the part's four maps plus a small
.blend. Older generations are kept and are not cleaned up automatically. To
reclaim the space, delete `_rbx_ckpt` while no run is active. Parts are
then rebaked when their output objects are missing.

You can move the whole output folder (`<blendname>_exports`, with its done
list and `_rbx_ckpt`) and point **Output Root** at the new place: the done list
finds its checkpoints there. Moving `_rbx_ckpt` on its own, away from its done
list, gives `CHECKPOINT_MISSING`.

Checkpoints are tied to the exact source files and settings. Moving the source
project, or editing its textures, makes them refuse (`SOURCE_CHANGED`) and the
parts are rebaked.

### Visual comparison report

A converted part can look wrong even when every map exists and every UV check
passes: a colour pocket, an inverted normal patch, lost transparency, or a
roughness change. The visual comparison renders and bakes the **source** and the
**converted output** with Cycles. It saves reference, output, difference and
coverage-mask images with a JSON verdict, so the evidence can be reviewed later.

#### Verdicts

| Status | Meaning |
| --- | --- |
| `PASS` | Every requested layer was compared and every metric is within its threshold. |
| `FAIL` | A measured difference exceeds a threshold, or something went wrong (error, missing artifact, wrong device, the object changed during the comparison). |
| `UNSUPPORTED_REFERENCE` | The source cannot serve as an independent reference for at least one layer. Examples: a missing node or image, a view-dependent input, an input outside the colour/metal/rough/normal/alpha contract, or a shader beyond Cycles' closure budget. |
| `INSUFFICIENT_COVERAGE` | Too little of the surface or too few camera views could be compared, or the requested surface correspondence does not exist (changed topology). |

Several problems can apply at once. The status takes the first that applies, in
the order FAIL, UNSUPPORTED_REFERENCE, INSUFFICIENT_COVERAGE, and every reason is
listed. **A skipped, unsupported or partial comparison is never reported as
PASS.**

#### What is compared

**Surface layer: channel values in a shared surface correspondence.**
Source and output get an identical temporary *probe* UV layout, built once from
the source. That layout is only the bake target. The UV layer that textures
sample through (the render-active one) is left alone. So texel (x, y) is the same
surface point on both sides, however differently the output's maps are laid out:
crop, repack, mirror. Channels: base colour (linear), metallic, roughness, alpha
and the object-space shading normal.

The correspondence exists in two cases:
* **Loop-identical topology** (same loops, same vertex positions): every
  ordinary conversion, which duplicates the source mesh.
* **Same polygons, reordered or with split vertices** (the CROP_SPLIT join):
  each output polygon is matched to the one source polygon with the same
  corners in the same winding, within the topology tolerance. An ambiguous or
  incomplete match means no correspondence (`SURFACE_CORRESPONDENCE_UNAVAILABLE`).

The probe layout packs **connected charts**: neighbouring polygons whose normals
stay within 45° of the chart's seed normal are projected together, so a local
window sees a contiguous patch of surface. A one-sample **polygon-ID bake**
records exactly which polygons received a probe texel; the sampled-area figure
in the report is measured, not estimated.

A colour/metal/rough/alpha value needs an *independent* reference:

* `PRINCIPLED_DIRECT`: a Principled BSDF is provably the Cycles surface (through
  reroutes only), and every input outside the channel contract is neutral
  (transmission, subsurface, coat, sheen, emission, anisotropy, thin film,
  specular IOR level 0.5, IOR 1.5, specular tint white, diffuse roughness 0).
* `REFERENCE_MATERIALS`: you supply emission materials that compute the expected
  colour and packed metal/rough/alpha (`settings["channel_references"]`). This is
  the pattern of the resilient-engine 100-layer oracle.

Anything else, including a Mix Shader of two Principled nodes, is
`UNSUPPORTED_REFERENCE`. The first top-level Principled is never taken as the
answer.

**Render layer: fixed-camera paired renders.** Orthographic renders from six
axis-aligned directions (±X, ±Y, ±Z) use the same framing for source and output,
under three lights:

| Preset | Light | Sensitive to |
| --- | --- | --- |
| `diffuse` | uniform white world | albedo, alpha |
| `grazing` | hard sun 75° off the view axis | normal relief |
| `highlight` | hard sun 20° off the view axis | roughness / specular |

Linear premultiplied colour and alpha are read from 32-bit EXR files and compared
separately. A material-override render of each object gives its geometric
coverage. Views where neither side is visible (for example a flat plane seen
edge-on) are listed, not averaged in. This is the only layer that works when the
topology changed. The report then states its scope as **render-only**, and that
the surface correspondence was not tested.

**Metrics** (per channel, per view and light): mean error, outlier fraction
(share of pixels beyond `outlier_threshold`), **local-region maximum** (largest
mean error in any 4×4 window with at least half its texels valid), the
**local-window coverage** (share of compared texels inside at least one such
window), p95/p99/max, and coverage counts. The local
maximum is what catches small defects. In the bundled wrong-colour example
(6×6 texels, 0.9% of the surface) the mean is 0.0051 and the outlier fraction
0.0081, both within threshold, but the local maximum is 0.590 against a
threshold of 0.10, so it FAILs.

#### Running it

```python
from addon import visual_validation as vv

settings = vv.default_settings()              # complete, documented defaults
settings["device"] = "CPU"                    # or "GPU" (needs an enabled GPU in Preferences)
report = vv.compare_pair(source_obj, output_obj, "/path/to/report_dir", settings)
print(report["status"], [r["code"] for r in report["reasons"]])

vv.inspect_report("/path/to/report_dir")      # re-verify hashes and verdict from disk
```

Headless, with the existing probe (backward-compatible options; without them
the probe behaves exactly as in 1.3.0):

```bash
blender -b --factory-startup scene.blend -t 4 --python tests/blender/source_vs_output.py -- \
    --addon addon --route AUTO --results svo.json \
    --channels color,rough,metal,normal,alpha --visual-report visual_reports --strict
```

Each of `--channels`, `--visual-report` and `--strict` switches on the
*extended* probe as a whole:
* alpha-safe probing;
* exact coverage (a black texel counts as covered);
* pairing through the run manifest.

1.3.0 matched by name and silently dropped renamed parts such as
`Rig16.002 → RBX_Rig16_002`. `--strict` also exits 1 on errors, unpaired or
uncompared objects/channels and non-PASS visual verdicts.

##### Settings

`resolution`, `samples`, `device`, `views`, `lighting_presets` and `tolerances`
are **required**: a missing key, an unknown key, or a partial `tolerances` block
is rejected before anything is rendered. Optional keys:
* `probe_resolution`: surface probe texels per side, or `"auto"`. Auto picks
  the smallest of 256, 512, 1024 and 2048 that the chart layout predicts to be
  enough for this mesh (else 2048); the report keeps `"auto"` as the setting and
  records the size used and the rule in `coverage.surface`.
* `layers`: `["surface", "render"]`.
* `keep_linear`: save the EXR renders.
* `channel_references`.

The exact values used, including every threshold, are saved in every report.

| Default | Value |
| --- | --- |
| render resolution / probe resolution | 128 px / 256 texels |
| samples | 64 (the `diffuse` preset is Monte Carlo; its noise falls as 1/√samples) |
| views / presets | all six / `diffuse`, `grazing`, `highlight` |
| surface thresholds (mean) | colour 0.025, metal 0.03, rough 0.03, normal 0.05, alpha 0.03 |
| surface local max | colour 0.10, metal/rough/alpha 0.12, normal 0.15 |
| outliers | no more than 2% of texels off by more than 0.25 |
| render thresholds | colour mean 0.02, local max 0.06, outliers 2% beyond 0.15; alpha mean 0.02, local max 0.10 |
| coverage | at least 64 surface texels; 95% of the surface area sampled (measured by the polygon-ID bake); 95% of the compared texels inside an evaluable local window, per channel; at least one view with 16 interior pixels, and an evaluable local window in every covered view |

The surface means are the starting values of `source_vs_output.py`. They are
**starting test parameters, not a perceptual guarantee.**

The render thresholds were calibrated on synthetic fixtures (Blender 5.2.0, CPU,
48 px), with raw data in `evidence/C_visual/windows/calibration/`:
* An identical input rendered twice differs by exactly 0.
* Faithful GRAPH_BAKE conversions stay at colour mean ≤ 0.010 and local max
  ≤ 0.027 at 64 samples.
* Small corruptions reach local max 0.42 (colour), 0.45 (normal) and 0.70 (alpha).

Recalibrate on your own asset class before making the render layer a release
gate.

#### The saved report

```
report_dir/
  report.json            verdict, reasons, settings, thresholds, metrics, coverage,
                         fingerprints, Blender/OS/device, artifact SHA-256 list
  report.json.sha256     hash of report.json (tamper check)
  surface/<channel>_reference.png, _output.png, _difference.png, mask.png
  render/<view>_<light>_reference.png, _output.png, _color_difference.png,
         _alpha_difference.png, _mask.png
  render/linear/<view>_<light>_reference.exr, _output.exr   (the data that was compared)
```

Reports are published atomically. They are built in a hidden staging folder
next to the destination and renamed into place only when complete, so a
cancelled comparison leaves nothing that looks like a verdict.
`inspect_report()` re-hashes every artifact, refuses artifact paths outside the
report folder, and re-derives the verdict. A deleted artifact, an edited image,
or a `report.json` claiming PASS over failing metrics all come back as not PASS.

Artifact names never contain object names, so names with `/`, `:` or non-ASCII
characters are safe. Destinations with spaces and non-ASCII characters are
tested on Windows and Linux. An existing non-empty folder, a file, or a symlink
at the destination is never overwritten (`FileExistsError`). A read-only parent
fails with `OSError` before any rendering.

**Display images only** are tone-mapped: render RGB is un-premultiplied, clipped
to [0, 1] and sRGB-encoded (no exposure, look, AgX or Filmic). Metal, rough and
alpha are written as linear grey, and the normal as 0.5·n + 0.5. In difference
images, grey scales so that twice the local threshold is white; red is above
the outlier threshold, dark blue is uncovered, magenta covers only one side, and
yellow is non-finite. Metrics never read the PNGs.

The fail example above is in
`evidence/C_visual/review_fixes/windows/examples/example_fail_wrong_color/` (module
1.1.0; the 1.0.0 reports are kept under `evidence/C_visual/windows/examples/`).
`example_pass_identical/` and `example_pass_engine_graph_bake/` are passing reports.

#### What it will not claim

| Code | Why the comparison is limited |
| --- | --- |
| `NODE_UNDEFINED`, `IMAGE_MISSING`, `GROUP_MISSING`, … | The source cannot be shown as authored. Both layers are UNSUPPORTED_REFERENCE. The output's own problems are noted but not graded. |
| `OBJECT_RANDOM_CONTEXT`, `CONTEXT_DEPENDENT_INPUT` | Identity- or scene-dependent inputs (Object Info Random, AO, object/view-layer attributes) that a copy cannot reproduce. |
| `VIEW_DEPENDENT_INPUT` | Fresnel, Layer Weight, Light Path, Camera Data, incoming/backfacing or camera/window/reflection coordinates. Fixed maps cannot hold them. |
| `PRINCIPLED_INPUT_OUTSIDE_CONTRACT` | Transmission, subsurface, coat, sheen, emission, anisotropy, thin film, or non-default specular/IOR. The render layer still compares what is visible. |
| `SURFACE_NOT_DIRECT_PRINCIPLED` | No provable channel source and no `channel_references`. |
| `CLOSURE_LIMIT` | More than ~64 closures. Cycles drops the rest, so its render of the source is not the authored mix. Measured: 12 chained metallic Principled BSDFs render correctly, 13 do not. |
| `TIME_DEPENDENT_IMAGE` | A movie or image-sequence texture. A fixed map holds one frame and one rendered frame is not a reference for it, so both layers are UNSUPPORTED_REFERENCE; the scope names the frame. |
| `SURFACE_CORRESPONDENCE_UNAVAILABLE` | No loop-identical topology and no unique polygon-by-polygon match, or the mesh has no UV layer, or it already has 8. Only renders are compared. |
| `SURFACE_COVERAGE_INSUFFICIENT` | The probe is too small for this mesh: too few texels, too little of the area sampled, or too few texels inside an evaluable local window. The message gives the measured figures and the `probe_resolution` the layout predicts to be enough (a geometry-only estimate that can be one doubling conservative), or says that no size up to 8192 is. Genuinely degenerate geometry is reported as such. |
| `RENDER_COVERAGE_INSUFFICIENT` | Too few covered views, or a covered view with no evaluable local window. Raise `resolution`, or check for edge-on or very thin geometry. |
| `INVALID_GEOMETRY` | The evaluated mesh has NaN/inf vertex positions. Nothing is rendered or baked; the report is published as FAIL. |
| `INVARIANCE_UNVERIFIED` | An object's fingerprint never gave two equal consecutive reads, so whether the comparison left it unchanged is not verified (INSUFFICIENT_COVERAGE, never blamed on the comparison). `SOURCE_MODIFIED` / `OUTPUT_MODIFIED` (FAIL) need two settled fingerprints that differ; `INVARIANCE_CHECK_FAILED` (FAIL) means the object could not be re-fingerprinted with the same algorithm afterwards. |
| `REPORT_MALFORMED`, `FINDING_CLASS_UNKNOWN`, `LAYER_NOT_COMPARED` | Raised by `inspect_report()` / `derive_status()` on a report that is not well formed, carries a finding of an unknown class, or drops a requested layer without explanation. Never PASS. |
| `DEVICE_UNAVAILABLE` | GPU requested but no GPU is enabled in Cycles preferences. A CPU render is never labelled GPU. |

#### Known limitations

* **Render-only scope has limited roughness sensitivity.** A 6×6-texel roughness
  change reaches a render local max of only 0.047, below 0.06. The surface layer
  measures it directly (0.53).
* **Near-mirror surfaces under the hard-sun presets** produce sub-pixel
  highlights that shift with tiny normal differences. Expect render FAILs on
  roughness below ~0.1, and judge those parts from the surface layer.
* **Non-tiling textures with `REPEAT` extension** wrap across the UV border. A
  re-baked map cannot reproduce that, and the grazing render shows a one-pixel
  border (local max 0.13 on a faithful bake). This is a real difference.
* **Mixing GGX lobes of different roughness is not linear**. A layered material
  keeps a small highlight residual (local max 0.027) after a faithful bake.
* If any used material lacks an independent reference, the **whole surface
  layer** is unsupported for that object (no per-material masking yet).
* The proxy copies carry modifiers, transforms, object colour and pass index. They
  do not carry custom properties (object attributes are reported as
  context-dependent) or scene context such as AO. Materials are analysed from
  the **evaluated** mesh, so a modifier that puts faces on another slot (Solidify
  material offset, Geometry Nodes Set Material) is analysed too.
* **Production-size meshes need a larger probe.** Measured on Windows: a
  70×70 grid and a 128×64 UV sphere compare at 256; a 1,882-polygon CP2077 part
  (201 charts) needs 512; a 54,234-polygon planter (1,932 charts) passes at 1024
  and 2048 (`"auto"` picks 2048, about a minute on CPU).
* Fingerprints: up to 1.3.0 the engine's `source_fingerprint()` could not hash
  Blender 5.x custom split normals (`INT16_2D custom_normal`); 1.4.0 hashes them.
  If an engine fingerprint still cannot be computed, the report falls back to the
  module's `content-v1` fingerprint and notes the engine error. Every
  fingerprint is read until two consecutive reads agree (at most three): in a
  freshly opened file a MOVIE image reports an empty colour space until its
  first load, which is what the first read does.
* Rendering replaces the transient *Render Result* buffer. It is removed again if
  it did not exist before.
* Cost: up to 8 renders per view (2 coverage renders, plus one render per side
  for each of the 3 presets; a view with no coverage stops after its 2), so at
  most 48 for the default six views; `counts.renders` in every report has the
  exact number. Bakes: 7–9 per comparison (one polygon-ID
  bake, plus colour, packed metal/rough/alpha and one or two normal bakes per
  side). A few seconds per small part on CPU at the defaults.

#### Turning it on for a run

In the panel, **Visual Check > Save Visual Comparison** compares every part the
run converts or carries over; **Require Visual PASS (experimental)** turns every
non-PASS verdict into a census failure that blocks finalize. Headless:

```bash
blender -b scene.blend --factory-startup --python run_headless.py -- \
    --visual-validation [--visual-gate] [--visual-settings visual.json]
```

`visual.json` holds only the keys to change (for example
`{"resolution": 256, "samples": 128}`); it is checked with the same rule the
comparison uses before anything runs, and an unknown key or a bad value stops
the run with exit code 2. Reports go to `<run folder>/visual/<run id>/<part>/`;
each manifest item carries `visual_validation = {status, report, reasons,
fidelity_scope}`. A part restored from a checkpoint is compared against its
restored output. A part with no comparison is counted `NOT_RUN`, never PASS.
The comparison renders on the device the run is configured for (Device, with
the same GPU choice and CPU-fallback rule as the bake), not on the Cycles
device the .blend was saved with. The report records the device actually used;
with CPU fallback off, an unusable GPU gives `VISUAL_DEVICE_UNAVAILABLE` (FAIL).

## What changed in 1.3.0

Complex shader graphs now produce metallic, roughness and opacity together in
one scalar pass. Alpha-bearing graph jobs use three Cycles passes instead of
five; opaque jobs use three instead of four.
Both packed and separate evaluation preserve Blender's RGB-to-scalar conversion.
`PACK_MRA_GRAPH_BAKE = False` in `engine.py` selects the separate path for diagnosis.
Resolution is not automatically reduced during recovery.

Before publication, the engine checks required shader nodes and output UVs.
Missing custom-node registrations, required Shader to RGB nodes under Cycles,
and duplicate-dependent Object Info Random inputs fail with a named reason.
Texture filenames alone cannot justify a fast conversion of an unproven surface.
Disconnected images and unused material slots do not block selected-input
precheck. Copy finalization still packs the full scene and deliberately fails
if any required packable image is missing, including an unrelated image.

Unique bake UVs must be finite, inside the unit tile, nondegenerate and free of
positive-area overlap. Shared boundaries and mirrored winding are accepted.
CROP intentionally allows overlap because it preserves the source atlas mapping.
If the bounded overlap check cannot finish, publication fails explicitly.

With `ALLOW_CPU_FALLBACK` enabled, eligible GPU/backend resource failures receive
one CPU retry at the same resolution. Shader errors are not retried. The manifest
records attempts, actual device, validation and pass counts.

Copy finalization delivers verified movies and supported PNG/JPEG/TGA sequences
beside the output blend. Original live image paths are restored even when saving
or packing fails. Missing packable images still block delivery. Movie staging
checks readability and byte identity; it does not validate every decoded frame.
EXR/HDR/BMP/TIFF sequences are explicitly refused by this bounded staging path.

The independent 100-layer fixtures test supported registered groups, spatial
color/scalars and normals against Blender references. Missing MLSetup/POM node
implementations and native Cycles closure limits remain real limits. See
`RESILIENT_BAKING_RELEASE.md` for measured timings, exact coverage and known inputs
that cannot be completed from the supplied archive.

## Earlier changes retained from 1.2.1

### Your .blend is no longer overwritten

Before, finishing a run saved over the file you opened, with no `.blend1` to go
back to. Now the packed deliverable is written to
`<output>/<scene>_IMPORTGLUE.blend` and your input file is left alone. Your
Blender session also stays on your own file rather than being silently moved onto
a generated one.

If you want the old behaviour, set `FINALIZE_IN_PLACE = True` in `engine.py`.

### "Check Textures" no longer writes into your scene folder

Before, a plain precheck defaulted to *repairing* — copying textures it found into
`textures/` next to your .blend. Checking something should not modify it, so:

- **Resolve Missing Textures For This Run** (on by default) points the material
  at the file it found, in memory, and writes nothing. The paths are put back
  before the deliverable is saved.
- **Repair Missing Textures In The Source Folder** (off, marked with a warning
  icon) is the old behaviour. Turn it on only when you intend to modify the
  source tree.

A scene saved with the old default is migrated once, the first time it is opened
with 1.2.1, and you will see a line in the console saying so. If you deliberately
turn source-writing repair back on, it stays on.

### Missing absolute paths now stop the run

**Block Missing Absolute Paths** defaults on, matching the headless behaviour. A
dead absolute reference cannot be repaired and bakes as Blender's magenta
placeholder, so continuing would produce a confidently wrong map.

### More materials take the slow path — on purpose

1.2.1 refuses the fast pixel-copy route in cases where 1.2.0 took it and produced
a wrong map. If a job that used to be `CROP` is now `GRAPH_BAKE`, that is the
fix working. The route reason now says which property caused it, for example:

```
original graph required; unresolved slots [0]; slot 0: unresolved;
node trace: mixed-closure:mix_shader
```

Materials that were genuinely simple still take the fast path, and their output
bytes are unchanged from 1.2.0.

### Normal maps: both conventions now work

1.2.0 had a single "Source Normals Are DirectX" switch for a whole run, and
refused the fast path whenever a Normal Map node disagreed with it. 1.2.1 reads
the convention each Normal Map node declares, so a batch mixing DirectX game rips
with OpenGL library assets is correct for every member. The switch remains as the
fallback for a texture wired straight in with nothing declaring a convention.

## What it will not guess at

The tool refuses rather than invent. Each of these appears in the manifest and the
run log with the reason:

| You will see | It means |
| --- | --- |
| `uv-transform:` | a Mapping node moves the UVs, so copied texels would be the wrong ones |
| `uv-layer:` | the texture samples a different UV layer than the output carries |
| `coord-source:` | Object/Generated/Camera coordinates, not UVs |
| `mixed-closure:` | a Mix/Add Shader blends two surfaces, so one Principled node is not the result |
| `normal-map-uv-layer:` | the Normal Map node builds its tangent basis in another UV layer |
| `packed-unknown-layout` | a packed file (`_DO`, `_NR`, `_CNRS`, `_PM`) whose channel layout is not established |
| `unresolved-semantics` | an `_A` file that could be alpha or ambient occlusion |
| `adapter_required` | a real texture Blender cannot decode, such as `.xbm` or `.dds` |

`_ORM` **is** decoded — roughness from green, metalness from blue — because that
layout was read off 255 real node wirings, not assumed. Its red (occlusion) is
left alone, because there is no occlusion output map to put it in.

## What the four maps cannot carry

Emission, occlusion, height, transmission, clearcoat, anisotropy, subsurface and
sheen have no delivered role. Files carrying them are *named in the manifest*, so
you can see what was present and not converted, but nothing invents a channel for
them. Volumes and displacement refuse the fast path and are not baked.

Output is **8-bit PNG**. A 16-bit or float source loses its extra precision, and
any value above 1.0 is clipped — reported as `hdr_clipped` with the fraction of
pixels affected, never silently.

## Converting textures Blender cannot read

REDengine `.xbm` (Cyberpunk 2077) and similar containers need an external
converter first. The run reports them with the command:

```bash
WolvenKit.CLI.exe export <in-dir> --outpath <out-dir> --uext png --gamepath "<game dir>"
```

`--gamepath` is required even for texture-only input. Verified on real files:
2048x2048 8-bit RGBA PNGs out.

## When it stops

| Symptom | Cause and fix |
| --- | --- |
| "Save the .blend first" | the precheck needs a file path to resolve relative textures against |
| Precheck blocked, code 11 | a texture is missing. Read the precheck report it names; set a **Shared Texture Pool** if the files live elsewhere |
| Precheck blocked, code 12 | a texture is corrupt. The report names it |
| `nothing_selected`, exit 3 | `--only` matched no object, or the selection was empty |
| Magenta output maps | Blender's missing-image placeholder reached the bake. The census fails on this deliberately |
| A folder of textures indexed as nothing | check the log for `MACHINE|adapter_required` |
| Object skipped, "empty mesh" | zero polygons |
| Long-path failures on Windows | see below |

### Windows long paths

With `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` set to
0, files whose full path reaches 260 characters cannot be opened. Measured on the
development machine: 605 of 3,044 files in one real asset tree, at path lengths
260 to 289.

The usual `\\?\` extended-length prefix **did not help** there, because the tree
sits under a OneDrive cloud-placeholder reparse point whose filter driver does not
serve that syntax. What does work is shortening the prefix — a directory junction
near the drive root brought the same file to a 43-character path, where it opened
normally:

```bat
mklink /J C:\igln1 "C:\very\long\path\to\textures"
```

Enabling long-path support in the OS is the better fix where you can.

## Settings worth knowing

| Setting | Default | Notes |
| --- | --- | --- |
| Resolve Missing Textures For This Run | on | writes nothing |
| Repair Missing Textures In The Source Folder | **off** | writes into your scene folder |
| Block Missing Absolute Paths | **on** | a dead absolute path is fatal |
| Source Normals Are DirectX | on | now only a fallback; a Normal Map node's own declaration wins |
| Resume from Done List | on | skips objects a previous run finished |
| Durable Checkpoints | on (with resume) | 1.4.0: restores finished parts after a reopen or crash; one extra copy of each part's maps |
| Rerun Completed Objects | off | also clears failure strikes |
| Allow Approximated Materials | **off** | 1.4.0: converts APPROXIMATION REQUIRED materials, labelled `APPROXIMATED`; BLOCKED never converts |
| Save Visual Comparison | off | 1.4.0: per-part visual report; adds Cycles renders per part |
| Require Visual PASS (experimental) | off | 1.4.0: non-PASS verdicts fail the census and block finalize |
| Route Mode | AUTO | `CROP_ONLY`/`PROXY_ONLY`/`GRAPH_ONLY`/`BAKE_ONLY` for diagnosis |
| Pack, Purge and Save | see above | now writes a copy, not over your input |

## Reading the manifest

Per map, 1.2.1 records what was actually done rather than leaving it implicit:

```json
{
  "source": "T_Barrel01_D.tga",
  "sampling": "DEFAULT",
  "source_colorspace": "sRGB",
  "conversion": "DIRECT",
  "colour_steps": "srgb-passthrough",
  "output_colorspace": "sRGB",
  "output_bit_depth": 8
}
```

`colour_steps` is the colour maths applied: `srgb-passthrough` means the decode
and the encode cancelled and the texels were copied untouched; `srgb-encode` or
`srgb-decode` means one was needed. `alpha_origin` says whether opacity came from
a traced source or from the material's constant. `stage_timing` gives a per-stage
breakdown of where the run's time went, with the fraction of wall time it accounts
for.
