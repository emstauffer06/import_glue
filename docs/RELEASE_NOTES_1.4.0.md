# Import Glue PBR Baker 1.4.0 / engine 3.8 — release notes

Milestone 1 of the cross-platform reliability roadmap.

Baseline: 1.3.0 / engine 3.7. To roll back, install the 1.3.0 release ZIP
(sha256 `c34815c6e70b2135402d213857a6bae0d1f986c1ff84ac731f460efaf1c106d4`)
from the `v1.3.0-engine3.7` release.

## New

- **Material capability report and policy** (`addon/material_capabilities.py`,
  rules version 3). Every used material is SUPPORTED, APPROXIMATION REQUIRED or
  BLOCKED, with the full node-instance path, a reason code and a next action.
  Check Textures writes `material_capabilities.json` and a panel summary; every
  run embeds the report in the manifest and a `capability` decision per object.
  Default: approximation-required objects are refused; the explicit opt-in
  (**Allow Approximated Materials**, `--allow-approximation`) converts them,
  labelled `APPROXIMATED` with every reason, and the run never reports `Clean`.
  BLOCKED never converts. Existing dependency diagnostics keep precedence.
- **Durable completed-part checkpoints** (`addon/checkpoints.py`, checkpoint
  schema 2). Each finished part is written as an immutable, hash-verified
  generation (maps + self-contained output payload, manifest committed last).
  When the output is not in the open file, resume restores it into a
  validation collection, compares canonical content, then runs the unchanged
  `validate_done_entry()` before exposing it. Tampered, incomplete, moved-away,
  other-Blender or other-config checkpoints are refused with a code and the part
  is rebaked. 1.3.0 done lists remain readable (`NO_CHECKPOINT`).
- **Visual comparison report** (`addon/visual_validation.py`, report schema 1),
  off by default: **Save Visual Comparison** / `--visual-validation`, optional
  experimental gate `--visual-gate`, settings file `--visual-settings`.
  Verdicts PASS / FAIL / INSUFFICIENT_COVERAGE / UNSUPPORTED_REFERENCE; skipped
  or unsupported coverage is never PASS. Render thresholds are calibrated on
  synthetic fixtures only.

## Changed

- Manifest schema 2 -> 3 (additive keys only). Done list 6 / done entry 4
  unchanged. `config_fingerprint()` keys unchanged, but the engine file changed,
  so 1.3.0 done entries are `CONFIG_CHANGED` once and rebake.
- New BLOCKED surface-path rules (muted closure, closure through a Reroute,
  colour wired into a shader socket, empty Mix/Add Shader, EEVEE Specular,
  unknown nodes and Principled inputs): 1.3.0 converted these into maps the
  Cycles render does not show.
- The status line names approximated outputs, parts restored from checkpoints
  and visual verdicts.
- Material use is read from the **evaluated** mesh. A slot that only a
  modifier uses (Solidify material offset, Geometry Nodes Set Material) is
  BLOCKED as `MODIFIER_MATERIAL_SLOT`; 1.3.0 baked those faces as a flat 0.8
  grey default and reported the object as clean. The hidden material's own
  issues and missing textures are reported, and the dependency gates see it.
- **Specular / IOR (change from 1.3.0; refuses many ripped materials by
  default).** Any Principled dielectric reflectance other than F0 0.04, that
  is any IOR or Specular IOR Level other than IOR 1.5 at level 0.5, is
  APPROXIMATION REQUIRED (`PBR_INPUT_UNREPRESENTED`, specular). The tolerance
  is float noise only (1e-6). By default such objects are refused (FAILED,
  route NONE, `REJECTED_APPROXIMATION`); they convert only with **Allow
  Approximated Materials** (Advanced) or `--allow-approximation`, are labelled
  `APPROXIMATED` with the measured F0 difference, and the run never reports
  `Clean`. 1.3.0 converted them without a word. The Roblox map set (colour,
  metalness, roughness, normal) cannot carry IOR, so these parts render with
  F0 0.04 whatever the source said. Measured reach on the Jacobs_Ship rip
  (final census, Linux): **1,413 of 2,475 mesh objects are refused by
  default, 1,245 of them only because of IOR**; 313 of 511 materials are
  approximation-required for IOR alone (399 Principled nodes at IOR 1.45).
  This is the owner-level ruling on final review finding 2: earlier 1.4.0
  candidates accepted an F0 difference up to 0.01 (IOR 1.45 included) as
  SUPPORTED, but the visual comparison's render metrics fail IOR 1.45 at every
  roughness up to 0.5 on both hosts, and no nonzero tolerance passes at every
  roughness (`tests/blender/calibrate_f0_tolerance.py`;
  `evidence/M1_final_fixes/{windows,linux}/f0/`).

## Fixed

- `INT16_2D` (Blender 5.x custom split normals) and `FLOAT4` mesh attributes are
  fingerprinted. In 1.3.0 every such part baked but lost its done entry
  (`bookkeeping_failed`), so resume never applied to typical imported meshes.

## Known limitations

- **Checkpoint cost on large parts** (final review finding 6, measured, not
  fixed). On a 102,400-vertex / 203,522-triangle part (CROP route, a weight
  on every vertex): Windows (Blender 5.2.0, i7-11800H) writes the checkpoint
  in 4.7 s, restores it in 17.9 s and the whole resume takes 26.5 s against
  a 14.5 s first run; Linux (5.2.2) 1.4 s, 6.5 s, 9.3 s and 5.6 s. One
  generation is 17.1 MB. More than half of the restore is two full runs of
  the unchanged `validate_done_entry()` gate (each re-validates the output
  UVs and fingerprints the source), not artifact hashing (0.24 s / 0.01 s).
  Generations are never pruned, the per-vertex weight
  digest is a Python loop, and a 203k-triangle GRAPH_BAKE output exceeds the
  strict UV-overlap check's fixed budget (a 1.3.0 limit), so the cost of a
  baked part that large was not measured.
- **GPU coverage is thin** (finding 4). GPU runs are the fresh-process
  recovery acceptance on each host (a 16 px GRAPH_BAKE part and a CROP_SPLIT
  part); suites, actual-asset runs and visual comparisons ran on CPU.
- **Checkpoint stores and done lists are keyed by bare object name**
  (finding 8, partly fixed). Carried parts now keep the source object that
  resume validated, so their visual comparison uses the right object; a
  checkpoint restore stays bound to its source by the source fingerprint.
- **Visual render thresholds** are calibrated on synthetic fixtures and the
  F0 grid only; the visual gate is experimental and off by default.

## Not changed

- Bake routes, passes, resolutions, map names, colour handling and exports.
- The 1.3.0 GRAPH_BAKE behaviours that the capability rules now refuse
  (`instrument_graph_semantic` ignores mute, probes Reroutes, writes raw colour
  without a closure) are documented and refused, not changed.
- Milestones 2-5 (background workers, resource scheduling, UV quality, channel
  and precision contracts) are not started.
