"""
import_glue.py -- unified crop / bake pipeline for game-rip -> Roblox conversion.

INTERACTIVE BUILD.  Open the .blend, select any object or selection of objects
in the 3D Viewport, open this file in Blender's Scripting tab, click Run Script
(or Alt+P with the cursor in the Text Editor).  You get four PNGs per object --
_ALB / _MET / _RGH / _NOR -- plus a manifest, and the .blend is saved once with
everything packed.  Nothing about the source objects, meshes, materials or UVs
is ever touched.  Everything you would plausibly want to change lives in the
CONFIG block right below the imports, with the three settings you're most
likely to actually touch (OUTPUT_ROOT, RES/MAX_RES, DEVICE) explained first.

Merged from:
  import_glue_smart_v2.py (V2.1)  -- routing engine: CROP / PROXY_BAKE / GRAPH_BAKE / SKIP,
                                     material resolution, UV repack, bake-resolution estimation
  import_glue_bake_v3.py  (V3.1)  -- granularity engine: atlas pooling, spatial chunking under a
                                     triangle budget, RES_MODE texel-density strategies with
                                     power-of-two snapping, decal alpha handling, scene state
                                     save/restore, collection-driven selection.
                                     main() never calls into this engine's own entry point
                                     (per_object_chunks) -- see the GRANULARITY CONFIG block
                                     below for exactly which of its knobs are live anyway.

Pipeline (what main() actually does, in order)
  1 SELECT       whatever mesh objects are selected, narrowed by --only when run headless.
                 Objects from a previous run (RBX_/MAX_/MAXJ_ prefixes) are ignored.
                 Collections excluded OR eye-hidden from the view layer are un-hidden first,
                 so their objects can be selected at all (see unexclude_all_collections()).
  2 GRANULARITY  one selected object = one output part.  The only regrouping this build
                 performs is CROP_SPLIT_PREPASS, which splits a multi-material object into
                 one part per material slot when every slot resolves to its own atlas.
  3 ROUTE        per part: CROP if it resolves to one flat atlas with compact in-range UVs,
                 PROXY_BAKE if it resolves but needs repacking, GRAPH_BAKE otherwise -- and
                 GRAPH_BAKE also covers a part with no usable UV layer at all, which gets a
                 real smart-project unwrap on its duplicate first (AUTO_UNWRAP_NO_UV).
  4 RESOLUTION   CROP takes the native cropped rectangle, power-of-two-ceiled and clamped to
                 MIN_RES..MAX_RES.  PROXY_BAKE and GRAPH_BAKE estimate a square side from
                 source texel coverage, clamped to min(MAX_RES, RES) -- see bake_res_ceiling().
  5 OUTPUT       four PNGs per part + manifest.json + run_log.txt in a fresh run folder
                 (under OUTPUT_ROOT if you set one), an in-blend output collection, and
                 (FINALIZE_IN_SESSION) one save of the .blend with every image packed.
                 glTF/FBX export is opt-in and off by default.

Guarantees carried over from every prior build
  * Source objects, meshes, materials, UVs and node trees are never modified.
  * Every duplicate owns private mesh AND material data -- bpy.ops.object.duplicate()
    shares material data unless Preferences > Editing > Duplicate Data > Material is on.
  * Every bake pass owns private nested node groups.
  * The .blend is saved ONCE, at the end of the run, by the v3.2 finalize stage
    (FINALIZE_IN_SESSION=False restores the old never-saves guarantee).

V3.2 (portal13 campaign fold-in)
  * face-slot capture/restore + degenerate-source-UV repair (kept from V3.1+zenfix)
  * every duplicate is force-cleared of inherited hide_render / hide_viewport
  * view-layer force-include (un-exclude) before selection is read
  * in-session finalize: purge + pack_all + save_version=0 + save
  * FAILURE CENSUS at run end, read back from this run's own manifest, and a
    nonzero process exit when anything failed -- "saved" is never "worked"
  * native per-item done-list, --only scoping, on-disk poison-strike counting
  * benign-black classification: BLACK_OK vs BLACK_SUSPECT, never a bare warning
  * CROP_SPLIT route: multi-material all-simple-slots -> split by slot -> crop
  * provenance banner (version + file md5) at run start, plus VERSION_CHECK()
  * manifest schema v2: status / route / reconstructed / prior_failure_reason

V3.3 / V3.3b (ops stress test, ~95 runs on real GPU pods, 2026-08-23)
  * MEM_GATE measures the cgroup WORKING SET (current minus reclaimable page
    cache), not raw memory.current -- the old gate idled up to 900s per run
    with the GPU at 0% on a pod streaming rsync in the background
  * RES is now an honoured ceiling for GRAPH_BAKE and PROXY_BAKE resolution
    (bake_res_ceiling() = min(MAX_RES, RES)) -- previously only the V3.1
    granularity engine's own piece_res() (which main() never calls) obeyed it,
    so GRAPH_BAKE/PROXY_BAKE silently went straight to MAX_RES every time
  * the Roblox triangle cap is now enforced on the GRAPH_BAKE route too
    (enforce_budget() before repack+bake, same TRI_BUDGET/ENFORCE_TRI_BUDGET
    the GRANULARITY block below defines) -- previously only reachable from the
    V3.1 chunking entry point, which main() never calls
  * main() now runs at the very end of the file, after the ported V3.1
    section, instead of partway through it -- the merge used to execute
    main() before its own second half was even defined, so any accidental
    call into that section raised NameError
  * CENSUS_FAIL_ON_SKIP / _NOMAT / _MAGENTA: a SKIPPED object, an empty
    material slot baked as default grey, and Blender's own (1,0,1)
    missing-image placeholder now all fail the census instead of reading
    CLEAN -- a bypassed precheck used to let a scene with genuinely missing
    textures report ok=6 exit=0
  * --only "" (empty) now scopes to zero objects instead of the whole scene;
    --only with no match now exits 3 with a MACHINE|nothing_selected line
    instead of exiting 0 with no manifest, indistinguishable from success
  * OVERALL | CLEAN/FAIL line after the census, since "DONE | OK=N FAILED=0"
    used to print even when the process was about to exit 1

V3.4 (interactive-use + speed pass, 2026-08-24)
  * LayerCollection.hide_viewport -- the ordinary per-view-layer eye icon, a
    THIRD flag distinct from `exclude` and Collection.hide_viewport, and the
    one most people reach for first -- is now cleared by
    unexclude_all_collections() same as the other two. An object left under
    an eye-hidden collection was invisible to context.selected_objects (and
    therefore to bpy.ops.object.bake()) with zero warning, even though
    select_set()/hide_get() reported it as fine
  * a mesh with ZERO usable UV layers now gets a real smart-project unwrap on
    its duplicate (AUTO_UNWRAP_NO_UV) instead of being SKIPped
  * a 0-polygon selected object now gets an explicit SKIPPED manifest/census
    entry instead of silently vanishing before plan_jobs() ever sees it
  * a bare non-Principled specular closure (Glossy, Anisotropic -- used
    directly for chrome/mirror surfaces with no Principled node anywhere)
    now bakes metal=1.0 via GRAPH_BAKE instead of always 0.0
  * a use_nodes=False material's actual Viewport Display colour/metallic/
    roughness now seeds the GRAPH_BAKE fallback instead of generic 0.8 grey
  * sys.exit() at the very end of this file is now gated to headless (-b)
    runs only (bpy.app.background) -- previously an interactive Scripting-tab
    run with a single SKIPPED/black-suspect/magenta object (and every
    CENSUS_FAIL_ON_* flag defaulting True) could raise SystemExit inside your
    live Blender session; it now prints the same information and continues
  * OUTPUT_ROOT (declared since V3.1, never read until now) is wired into
    output_directory()
  * three real speed fixes, no output change: image_stats() stopped paying
    for NaN-safe reductions on bake output that is never actually NaN (measured
    18% of one real GRAPH_BAKE run's wall time); build_disk_index()/
    build_packed_index() stopped eagerly decoding every candidate texture in
    the whole pool up front regardless of what's selected, deferring that to
    first actual use (measured on a real 1739-object scene: planning phase
    124s -> 4.8s, zero difference in which atlas wins for any material);
    metal+rough+alpha now bake in one Cycles pass instead of three for
    PROXY_BAKE specifically -- checked and NOT done for GRAPH_BAKE, where a
    Mix/Add Shader branch could make that silently wrong
  * everything above was verified by actually baking real objects and
    diffing PNG output against the previous build, not by reasoning about it

Changes from the originals (config defaults):
  * MIN_RES 64 -> 4 (do not inflate native crops)
  * MAX_RES 1024 -> 8192, RES 1024 -> 4096 (raised for a "highest resolution
    the card holds" workflow -- see the RES/MAX_RES note in CONFIG below)
  * SOURCE_NORMAL_IS_DIRECTX False -> True (this build's source atlases are
    DirectX -Y; flip back to False if yours are already OpenGL +Y)
  * MEM_GATE_GB 85 -> 200 (sized for this pod; see MEM_GATE_GB below)
  * EXPORT_GLTF default True -> False
  * EXPORT_FBX stays False

Known gaps, left unpatched (found, not fixed -- know about them rather than
be surprised by them)
  * DEVICE = "CPU" stops Cycles baking on the GPU but does NOT stop it probing
    the CUDA/OptiX driver first -- on a machine whose GPU is busy or absent
    that probe can itself raise. For a genuinely CPU-only run also blank the
    GPU at the OS level before launching Blender:
      Linux/macOS: CUDA_VISIBLE_DEVICES='' HIP_VISIBLE_DEVICES='' blender ...
      Windows PowerShell: $env:CUDA_VISIBLE_DEVICES=""; $env:HIP_VISIBLE_DEVICES=""
  * a carried-over item from a resumed run (DONE_LIST) is reported OK, but its
    maps and duplicate object live in the PREVIOUS run's export folder, never
    re-attached to the resumed run's own folder
  * save_version=0 can still leave a stray .blend1 behind if pack_all() itself
    errors partway through the same finalize pass
  * the metal/rough/alpha channel-pack and the use_nodes=False fallback were
    verified by direct A/B bakes; the interactive-session sys.exit() gate was
    verified by code inspection only (no GUI display was available to test
    Blender's actual Scripting-tab "Run Script" button against)

Additions
  * import-safe under importlib -- self-registers in sys.modules before the dataclasses
    are evaluated, so exec_module() without prior registration no longer raises.
  * OUTPUT_ROOT pins the output directory instead of writing beside the open .blend.
  * "_d" is a recognised colour suffix, and MULTI_SUFFIX_RULES lets ONE packed file
    feed several channels ("_orm" -> rough from G, metal from B).  X_D/X_N/X_ORM rip
    sets used to fragment into a colour-only "x_d", a colour-only "x_orm" (an ORM
    indexed as an ALBEDO) and a colourless "x" that atlas_is_valid() then discarded,
    which made resolve_material()'s filename vote see competing sets and refuse --
    forcing GRAPH_BAKE on parts plain filename grouping can serve.  See
    suffix_channels() and MULTI_SUFFIX_RULES.

Caller-side hazards (NOT bugs in this file -- documented because they cost real runs)
  * Never compare Blender RNA objects with `is`. Blender returns a fresh Python wrapper
    on every attribute access, so `link.to_node is some_node` is always False. Use `==`
    or compare names. Both source scripts are clean here; downstream drivers were not.
  * Atlas naming is not universal. Resolve by socket name first and filename suffix only
    as a fallback -- `T_VMGen_BaseColorMap_<hash>` and role-then-quality tags like
    `_C_HD` both defeat suffix matching.

Blender 4.3 - 5.2, Cycles/OptiX.  numpy is required and already ships inside Blender.
"""
from __future__ import annotations



import bpy
import bmesh
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import traceback
import zlib
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

if not __package__:
    # Keep file-based headless/Text Editor loading supported without executing
    # the add-on initializer a second time. Helpers still use package imports.
    import types as _support_types
    _support_name = "_import_glue_support_" + hashlib.sha256(
        os.path.abspath(__file__).encode()).hexdigest()[:12]
    if _support_name not in sys.modules:
        _support = _support_types.ModuleType(_support_name)
        _support.__path__ = [os.path.dirname(os.path.abspath(__file__))]
        sys.modules[_support_name] = _support
    __package__ = _support_name

from . import material_capabilities
from .shader_graph import material_dependencies, used_materials
from .uv_validation import validate_uv
from .media_delivery import stage_external_media, restore_media_paths
from . import checkpoints as durable_checkpoints
from .run_control import RunCancelled
from . import output_contract

# Script/Text-Editor runs execute this file as __main__: bind the checkpoint
# module to THIS engine so it sees the same config globals and caches.
durable_checkpoints.bind_engine(sys.modules[__name__])

# One engine run per Blender main thread. The previous controller is restored
# by main(), including on error; ordinary synchronous callers pay only a None
# check. Controllers must not call bpy from another thread.
_RUN_CONTROL: Any = None
_RUN_DONE_PATH = ""
_NORMAL_ENCODING_ADJUSTMENTS: List[Dict[str, Any]] = []


def _run_event(kind: str, **fields: Any) -> None:
    if _RUN_CONTROL is not None:
        _RUN_CONTROL.emit({"kind": kind, **fields})


def _run_boundary(stage_name: str, **fields: Any) -> None:
    _run_event("stage", stage=stage_name, **fields)
    if _RUN_CONTROL is not None:
        _RUN_CONTROL.check_cancel()


def _mark_cancelled(report: Dict[str, Any], exc: BaseException) -> None:
    report["cancelled"] = True
    report["status"] = "CANCELLED"
    report["cancel_reason"] = str(exc) or "Cancellation requested"
    report["run_error"] = report["cancel_reason"]


# ================================ CONFIG =====================================
# EVERYTHING A USER NEEDS TO SET LIVES IN THIS BLOCK.  Nothing below it needs
# editing to run the tool.  The three most likely to need changing are
# OUTPUT_ROOT, RES/MAX_RES, and DEVICE -- they're first.

# Where the finished PNGs go.  None = write them next to the .blend you opened,
# in "<blendname>_exports/rbx_pbr_smart/".  Set an absolute path (e.g.
# "/home/you/out/my_model") to send every run somewhere else instead -- useful
# when the .blend lives on a drive/volume you would rather not write to.
OUTPUT_ROOT = None        # absolute path, or None to write beside the .blend

# Output sizing.  There are TWO ceilings and they mean different things:
#   MAX_RES  the absolute cap for every route, including CROP (native crops
#            are never upscaled past their own source resolution regardless).
#   RES      the ceiling GRAPH_BAKE/PROXY_BAKE actually bake at -- always
#            min(MAX_RES, RES), see bake_res_ceiling().  If you only touch
#            one number, touch RES; MAX_RES only matters if you also want to
#            lower/raise the CROP route's ceiling independently.
# 8192/4096 here means "bake at up to 4K, crop can go up to 8K" -- this
# build's own defaults for a highest-resolution-the-card-holds workflow.
# Roblox's per-image limit is 1024; drop RES to 1024 (and MAX_RES to match,
# if you also want CROP capped there) for a Roblox-target run.
MIN_RES = 4
MAX_RES = 8192
RES = 4096

# Which device Cycles bakes on.  "GPU" tries OptiX, CUDA, HIP, Metal, oneAPI in
# that order and falls back to CPU if none is usable.  "CPU" is slower but
# never fights another program for the graphics card -- see the DEVICE known
# gap further up in this docstring before relying on it for a shared GPU.
DEVICE = "GPU"                        # GPU | CPU
ALLOW_CPU_FALLBACK = True              # headless compatibility; add-on defaults False
# Backends ensure_cycles_device() tries for DEVICE="GPU", in this order.
GPU_BACKENDS = ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI")
GPU_DEVICE_ID = ""                    # exact Cycles id or unique name; empty picks one GPU
_LAST_DEVICE_SELECTION: Dict[str, Any] = {}
# Milestone 1: a material whose effects the delivered maps cannot hold is
# refused unless explicitly opted in; an opted-in output is labelled
# APPROXIMATED with its reasons, never SUPPORTED.  BLOCKED never converts.
ALLOW_APPROXIMATION = False
CAPABILITY_PROFILE = "PBR_BASE"       # material_capabilities output profile
OUTPUT_PROFILE = "PBR_BASE"           # PBR_BASE (8-bit) | PBR_HIGH_PRECISION (PNG16)
TARGET_PROFILE = "LEGACY"            # LEGACY | BLENDER_NATIVE | ROBLOX | BOTH
ROBLOX_TEXTURE_LIMIT = 1024           # Configurable delivery preset, not an engine maximum.
_TARGET_PIPELINE_ACTIVE = False
PACK_MRA_GRAPH_BAKE = True             # equivalent metal/rough/opacity in one pass

# Source discovery.  Keep this empty for portable use.  The saved .blend
# directory plus texture folders beside its parents are discovered
# automatically.  Add project-specific absolute folders only when needed.
SOURCE_DIRS: List[str] = []
SOURCE_PARENT_LEVELS = 3
SOURCE_FOLDER_NAMES = ("textures", "texture", "tex")
INDEX_PACKED_IMAGES = True

# AUTO chooses exact crop, then proxy bake, then original-graph bake.
# BAKE_ONLY is retained for compatibility: it chooses PROXY_BAKE when every
# slot resolves, otherwise GRAPH_BAKE.
ROUTE_MODE = "AUTO"  # AUTO | CROP_ONLY | PROXY_ONLY | GRAPH_ONLY | BAKE_ONLY
CROP_MIN_FILL = 0.08                 # below this, repack+bake wastes fewer texels
UV_OUTSIDE_TOL = 0.0001              # crop route requires UVs inside 0-1

# Power-of-two snapping (CROP and PROXY_BAKE/GRAPH_BAKE resolution) always
# rounds UP, never down.
FORCE_SQUARE_CROPS = False
CROP_PAD_SOURCE_PX = 8               # padding measured in source ColorMap px
BAKE_MARGIN_PX = 12
# Texel-density multiplier for the two ESTIMATED routes.  RES/MAX_RES are only
# ceilings; the size GRAPH_BAKE/PROXY_BAKE actually asks for is
# sqrt(uv_area) * GRAPH_TEXELS_FULL_UV * BAKE_DENSITY_SCALE, snapped up to a
# power of two and then clamped by bake_res_ceiling().  2.0 is twice the linear
# density (4x the texels) BEFORE that clamp; 0.5 is half.
# 2026-08-25: this is LIVE and user-facing -- the add-on's "Bake Density" field
# and headless --density both write it through the same paths RES/MAX_RES use,
# and manifest_config() reports it.  Before that it was hashed into
# config_fingerprint() only, so a run's real density was unreadable from its own
# manifest and the only way to change it was editing this line.
BAKE_DENSITY_SCALE = 1.0
GRAPH_TEXELS_FULL_UV = 4096

# Roblox requires OpenGL (+Y) tangent-space normal maps.  Leave False when the
# exported source atlas is already OpenGL.  Set True only for DirectX (-Y) maps
# -- True here because this build's own source atlases are DirectX.
SOURCE_NORMAL_IS_DIRECTX = True
PROXY_IMAGE_EXTENSION = "REPEAT"       # REPEAT | EXTEND | CLIP

# Output maps use Roblox's documented suffix convention.
OUTPUT_SUFFIX = {
    "color": "_ALB",
    "metal": "_MET",
    "rough": "_RGH",
    "normal": "_NOR",
    "emissive": "_EMI",
}

# Missing maps become explicit neutral maps so every output set is complete.
DEFAULT_METAL = 0.0
DEFAULT_ROUGH = 0.5
DEFAULT_NORMAL = (0.5, 0.5, 1.0)
DEFAULT_MAP_RES = 4

# Export and cleanup behavior.
EXPORT_GLTF = False
EXPORT_FBX = False                    # GLB is the reliable PBR handoff
STRIP_VERTEX_COLORS = True            # avoids unintended glTF color multiply
OUTPUT_SUBDIR = "rbx_pbr_smart"
OUTPUT_COLLECTION = "RBX_PBR_SMART_OUTPUT"
TRIANGLE_WARN = 20_000

PERSISTENT_DATA = False               # safer for changing many proxy materials

# Objects/materials produced by previous runs are ignored when users select
# everything in the scene.
PREVIOUS_OBJECT_PREFIXES = ("RBX_", "MAX_", "MAXJ_")
PREVIOUS_MATERIAL_SUFFIXES = ("_RBX_PBR", "_BAKED")

# Direct crop cache.  A decoded 4K PBR set can be hundreds of MiB.
PIXEL_CACHE_MAX_MB = 768

# Baked output is disposable intermediate data, not an archival deliverable --
# PNG compression level trades file size for CPU time with zero effect on
# pixel fidelity.  1 (fast) is this build's default; raise it if you'd rather
# spend CPU time for smaller files. Used by write_png() and, through it,
# save_blender_image() -- see that function's docstring for why it writes PNGs
# itself instead of using Blender's own image.save()/save_render().
PNG_COMPRESS_LEVEL = 1

# Conservative material detection.
MIN_COLOR_SIZE = 256
PLACEHOLDER_RE = re.compile(
    r"^(black|white|default|debug|checker|grey|gray|empty|none|pink)", re.I
)
DECAL_HINT = "decal"

# Manual overrides: "when a material's name contains this text, use that atlas".
# The tool resolves materials on its own (direct node trace first); a rule only
# wins when the named atlas is actually found on disk, so a stale rule is
# harmless -- add new ones freely when the automatic resolution guesses wrong
# for a specific model. Left side = lowercase text to look for in the material
# name. Right side = the atlas base name (the filename without its
# _alb/_nor/... suffix). This build's own verified rules, from the Smasher
# (Cyberpunk 2077) rips this pipeline processes:
MATERIAL_ATLAS_RULES = [
    ("_001_mm_body", "Smasher_001_Body"),
    ("_002_mm_armor", "Smasher_002_body"),
    ("_003_mm_slot", "Smasher_003_holster"),
    ("holster", "Smasher_003_holster"),
    ("_004_mm_arms", "Smasher_004_Arms"),
    ("_005_mm_weapons", "Smasher_005_Weapons"),
    ("_006_mm_launcher", "Smasher_06_Launcher"),
    ("_007_mm_launcher", "Smasher_07_Launcher"),
    ("_008_mm_head", "Smasher_08_Head"),
    ("_020_mm_legs", "Smasher_020_Legs"),
    ("knife_masksset", "Smasher_001_Knife"),
    ("frag_grenade", "Smasher_FragGrenade"),
    ("skin", "t0_009_face"),
]

# (suffix, semantic channel, packed component).  _rm01 is deliberately only
# accepted as roughness/R; its metallic layout is not guessed.
SUFFIX_RULES = [
    ("_alb", "color", None),
    ("_met", "metal", None),
    ("_rgh", "rough", None),
    ("_nor", "normal", None),
    ("_basecolor", "color", None),
    ("_roughness", "rough", None),
    ("_metallic", "metal", None),
    ("_normal", "normal", None),
    ("_color", "color", None),
    ("_rough", "rough", None),
    ("_metal", "metal", None),
    ("_d01", "color", None),
    ("_m01", "metal", None),
    ("_r01", "rough", None),
    ("_n01", "normal", None),
    ("_rm01", "rough", "R"),
    # v1.1.1a: "_d" (diffuse) is as common in game rips as "_alb" and used to
    # match NOTHING, so X_D.tga was indexed as its own colour-only set under base
    # "x_d" while its X_N.tga sibling grouped under "x" -- leaving "x" with no
    # ColorMap at all, so atlas_is_valid() threw the real set away.  Appended
    # rather than inserted: no existing suffix in this table ends in "d", so
    # first-match order for every rule above is unchanged.
    # v3.6: _diff appears as Base Color 8 times and its alpha as Alpha 8 times in
    # the surveyed rip, and matched nothing before (no rule ends in "ff"), so it
    # became its own colour-only set. Only the colour claim is made here; the
    # alpha one needs a node graph to prove, and the node-trace route already
    # finds it there.
    ("_diff", "color", None),
    ("_d", "color", None),
    ("_m", "metal", None),
    ("_r", "rough", None),
    ("_n", "normal", None),
]

# (suffix, [(semantic channel, packed component), ...]) -- ONE file feeding more
# than one output channel.  Consulted by suffix_channels() BEFORE SUFFIX_RULES,
# so a multi-channel suffix always wins over a shorter single-channel one.
#
# v1.1.1a: "_orm" matched no single-channel rule either, so an ORM -- a packed
# NON-colour map -- was landing in the index as a COLOR atlas under its own base
# "x_orm", and the vote in resolve_material() then saw two or three competing
# bogus sets and refused, forcing GRAPH_BAKE on material sets plain filename
# grouping could have rescued.  The G=rough / B=metal layout here is not a guess:
# it is read straight off this family's own node graphs, where Metallic comes
# from the ORM's Blue via SeparateColor and Roughness from its Green.
# Occlusion (R) is deliberately NOT mapped -- there is no occlusion output
# channel (see CHANNELS/OUTPUT_SUFFIX), so claiming it would invent one.
MULTI_SUFFIX_RULES = [
    ("_orm", [("rough", "G"), ("metal", "B")]),
    # v3.6: confirmed, not assumed. tools/survey_graphs.py read 524 real
    # materials from a rip that ships this family and found 255 wirings of an
    # _ORM file: Metallic <- Blue 209 times, Roughness <- Green 46 times, and
    # Occlusion (Red) never, which is why R stays unclaimed. The same survey
    # found only 2 wirings of _ORMS, both Roughness <- Green, so that is all it
    # claims; its Blue is not evidenced and is left to the graph route.
    ("_orms", [("rough", "G")]),
]

# Suffixes that name a role which is NOT one of the four delivered maps.
#
# Recognising them is not about claiming them. build_disk_index() turns an
# UNRECOGNISED stem into its own colour-only set (`base, channels = stem,
# [("color", None)]`), so every one of these used to invent a competing atlas
# under its own base name. That is exactly the failure the _orm rule was added
# to cure in v1.1.1, still live for everything else: resolve_material()'s vote
# then saw several plausible sets, refused, and pushed a material whose real set
# was sitting right there to GRAPH_BAKE.
#
# Measured against the local corpus (5393 files, corpus/CORPUS_MANIFEST.json):
# _tcm 151, _tm 137, _masksset_N 495, _pm 12, _do 9, _cnrs 7, _h 27, _e 14,
# _nr 2, _a 49, _mask 8 -- roughly 900 files that were each fabricating an
# atlas. A file matched here contributes its BASE for grouping and NO channel,
# so its siblings group correctly and nothing is invented from it.
#
# Several of these could plausibly be decoded (_DO is diffuse+opacity on all 9
# local files, which are 32-bit TGAs; _NR is probably normal+roughness). They
# stay unclaimed deliberately: the survey found no node graph wiring any of them,
# so a layout would be a guess, and a guessed opacity or roughness channel is the
# kind of confidently-wrong output this whole patch exists to stop. They are
# reported as recognised-but-unrepresented instead.
AUXILIARY_SUFFIX_RULES = [
    ("_masksset", "layer-mask"),          # REDengine MLSetup per-layer mask
    ("_tintcolormask", "tint-colour-mask"),
    ("_tcm", "tint-colour-mask"),
    ("_cnrs", "packed-unknown-layout"),
    ("_nr", "packed-unknown-layout"),
    ("_do", "packed-unknown-layout"),
    ("_pm", "packed-mask"),
    ("_tm", "tint-mask"),
    ("_ao", "ambient-occlusion"),
    ("_mask", "mask"),
    ("_emit", "emission"),
    ("_emissive", "emission"),
    ("_height", "height"),
    ("_spec", "specular"),
    ("_gloss", "glossiness"),
    ("_curv", "curvature"),
    ("_id", "id-mask"),
    ("_h", "height"),
    ("_e", "emission"),
    # _a is left auxiliary on purpose: in this corpus it could be alpha or
    # ambient occlusion and nothing on disk settles it. Claiming it as alpha
    # would risk making opaque assets transparent.
    ("_a", "unresolved-semantics"),
]

# Hand-assembled texture sets, for the case where a model's map filenames do not
# share a common base name and so cannot be grouped by SUFFIX_RULES above. A set
# is only used if every filename listed is actually present in one of the
# source folders, so an entry for a model you don't currently have is harmless.
# This build's own verified set, from a real Smasher rip:
EXTRA_SETS = {
    "t0_009_face": {
        "color": "t0_009_mm_face__adam_smasher_d01.png",
        "normal": "t0_009_mm_face__adam_smasher_n01.png",
        "rough": "t0_009_mm_face__adam_smasher_rm01.png",
    }
}

# v3.6: added the float and modern formats Blender decodes natively. A float
# source is not free -- write_png() is 8-bit, so any value above 1.0 is clipped.
# That is detected and reported per map (see process_crop's hdr_clipped note)
# rather than happening silently. .dds is deliberately absent: Blender's support
# depends on the compression the file uses, so an extension test would promise
# more than it can decode -- such files need the same adapter route as .xbm.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff",
              ".exr", ".hdr", ".webp"}

# Extensions that ARE textures but that Blender cannot decode, so this tool
# cannot read them directly. They are listed so a folder full of them produces a
# specific, actionable line instead of looking empty: engine <=3.5 filtered on
# IMAGE_EXTS and said nothing, which is the "silently omitted asset" failure --
# 1707 .xbm files sit in the local corpus and the index reported zero candidates
# for them with no explanation.
#
# The named commands are how the files were actually converted while testing
# (verified 2026-09-17: three real .xbm produced 2048x2048 8-bit RGBA PNGs), not
# a suggestion to install anything. Conversion stays OUTSIDE this add-on: it
# needs a game installation and a separate tool, and doing it implicitly would
# hide a dependency the operator has to satisfy anyway.
ADAPTER_REQUIRED_EXTS = {
    ".xbm": ("REDengine CR2W texture -- convert with WolvenKit.CLI: "
             "export <dir> --outpath <dir> --uext png --gamepath <game dir>"),
    ".dds": ("DirectDraw Surface -- Blender's support depends on the "
             "compression used; convert to PNG/TGA first"),
    ".ktx": "Khronos texture container -- convert to PNG/TGA first",
    ".ktx2": "Khronos texture container -- convert to PNG/TGA first",
    ".psd": "layered Photoshop document -- flatten to PNG/TGA first",
    ".tex": "engine-specific texture container -- convert to PNG/TGA first",
}
CHANNELS = ("color", "metal", "rough", "normal")
SOURCE_CHANNELS = CHANNELS + ("alpha",)
MAX_TRACE_DEPTH = 48

# ============================================================================



import sys as _sys, types as _types
if __name__ not in _sys.modules:
    _stub = _types.ModuleType(__name__)
    _stub.__dict__.update(globals())
    _sys.modules[__name__] = _stub
    del _stub


# ==========================================================================
# GRANULARITY / TEXEL-DENSITY CONFIG  (ported from import_glue_bake_v3.py V3.1)
#
# RES, TRI_BUDGET and ENFORCE_TRI_BUDGET ARE live -- RES sets the GRAPH_BAKE/
# PROXY_BAKE resolution ceiling (see RES in the main CONFIG block above, and
# bake_res_ceiling()); TRI_BUDGET/ENFORCE_TRI_BUDGET are read by
# enforce_budget(), called directly from process_graph_bake() before every
# GRAPH_BAKE. Everything ELSE in this block -- RES_MODE, DENSITY_MIN_RES,
# TEXELS_FULL_UV, POT_SNAP, DECIMATE_RATIO, INCLUDE_DECALS -- is read only by
# piece_res()/per_object_chunks() at the end of this file, which main() never
# calls, so those specific six values have no effect on a normal run.  Left in
# place rather than deleted so the file stays a faithful, diffable copy of the
# validated build; ignore them unless you're wiring per_object_chunks() in
# yourself.
# ==========================================================================
RES_MODE           = 'FLAT'   # FLAT | RELATIVE | DENSITY | DENSITY_CLAMPED
DENSITY_MIN_RES    = 256      # floor for RELATIVE / DENSITY (was MIN_RES in bake_v3;
                              # renamed to avoid colliding with the crop floor above)
TEXELS_FULL_UV     = 4096     # resolution a piece filling the WHOLE source UV earns
POT_SNAP           = True     # snap every computed resolution to a power of two
TRI_BUDGET         = 20000    # Roblox per-MeshPart triangle cap
ENFORCE_TRI_BUDGET = True     # collapse-decimate any chunk still over budget
DECIMATE_RATIO     = 1.0      # 1.0 = off
INCLUDE_DECALS     = True     # decals bake alongside everything else
_T0                = time.time()

# ==========================================================================
# V3.2 CONFIG -- portal13 campaign fold-in.  Every default reproduces the
# campaign-proven behaviour; set a flag False for exact V3.1 semantics.
# V3.3/V3.3b and V3.4 flags are folded into this same block, each marked.
# ==========================================================================
TOOL_NAME             = "import_glue"
TOOL_VERSION          = "4.0"  # v4.0: verified source capture and explicit native/Roblox targets.
                              #       durable completed-part checkpoints, optional visual
                              #       comparison reports, INT16_2D/FLOAT4 attribute fingerprints
                              # v3.7: required graph/UV gates, packed scalar parity, CPU recovery,
                              #       portable media delivery and source path restoration
                              # v3.6: sampling contract, colour-space invariant, opacity contract,
                              #       closure-structure gate (see the v3.6 notes below)
                              # v3.5: transactional map publication, normal-map convention gate,
                               # fail-closed headless CLI, durable done-list. The parallel Codex
                               # derivative of the same 3.4 base reports "3.4.1"; the two engines
                               # are different code, so they must not share a version string.

FORCE_VISIBLE_OUTPUT  = True    # clear hide_render/hide_viewport on every duplicate
ISOLATE_GRAPH_BAKE    = True    # v1.1.1: cull the scene to ONE duplicate while its GRAPH_BAKE
                                 # passes run (hide_render on every other object + exclude every
                                 # root layer collection that does not hold it), so Cycles re-syncs
                                 # that object's own material instead of all 500+ of them on each
                                 # of the 4-5 passes. Measured 27s -> 1.3s per pass on a real
                                 # 2622-object rip. See GraphBakeIsolation for why both mechanisms
                                 # are needed and why it cannot change a texel. False restores
                                 # stock behaviour exactly.
FORCE_VIEW_LAYER      = True    # un-exclude collections before object selection
FINALIZE_IN_SESSION   = True    # pack_all + save_version=0 + save, same session as the bake
# v3.6: where finalize writes. False (default) = a COPY in the output folder, so
# the user's input .blend is never touched; True = the pre-3.6 behaviour of
# overwriting the file that was opened. See finalize_session() for why.
FINALIZE_IN_PLACE     = False
FINALIZE_COPY_SUFFIX  = "_IMPORTGLUE"
FINALIZE_PURGE_PASSES = 3       # orphan purges before pack_all
CENSUS_EXIT_NONZERO   = True    # nonzero process exit when the census finds a failure -- headless (-b) runs only,
                                 # see the __main__ guard at file end: an interactive Text-Editor run must not sys.exit()
CENSUS_EXIT_INTERACTIVE_TOO = False  # v3.4: set True to force sys.exit() even inside the Blender GUI (not recommended)
AUTO_UNWRAP_NO_UV     = True    # v3.4: give a mesh with ZERO usable UV layers a smart-project unwrap instead of
                                 # SKIPping it outright (same mechanism already used for a single collapsed UV layer)
PACK_MRA_PROXY_BAKE   = True    # v3.4: metal+rough+alpha in one Cycles EMIT pass for PROXY_BAKE (safe: proxy graphs
                                 # are always flat/unbranched by construction -- NOT done for GRAPH_BAKE, see notes
CENSUS_FAIL_ON_BLACK  = True    # BLACK_SUSPECT counts as a failure for the exit code
CENSUS_FAIL_ON_SKIP   = True    # v3.3: a SKIPPED (unplannable) object counts as a failure for the exit code
CENSUS_FAIL_ON_NOMAT  = True    # v3.3: a used slot with no material (baked default grey) counts as a failure
CENSUS_FAIL_ON_MAGENTA = True   # v3.3: a colour bake that is Blender's missing-image magenta counts as a failure
MAGENTA_MIN_RB        = 0.80    # v3.3: mean R and B at/above this ...
MAGENTA_MAX_G         = 0.20    # v3.3: ... with mean G at/below this reads as the (1,0,1) placeholder
DONE_LIST             = True    # native per-item done-list + --only resume
DONE_LIST_NAME        = "_v32_done.json"
DURABLE_CHECKPOINTS   = True    # M1-B: verified per-part checkpoint generations beside the done-list
POISON_STRIKES        = 3       # consecutive failures before an item is auto-skipped
BENIGN_BLACK          = True    # emission-only/no-diffuse black bakes report BLACK_OK
BLACK_MAX_LEVEL       = 1e-4    # image_stats()["max"] at/below this reads as black
CROP_SPLIT_PREPASS    = True    # multi-material all-simple-slots -> split-by-slot -> CROP
# Memory gate: before the first bake, wait until the machine's memory use drops
# below MEM_GATE_GB. LINUX cgroup-v2 ONLY -- it reads CGROUP_MEM_CURRENT below.
# On Windows and macOS that file does not exist, so the gate prints
# "gate: skipped" and the run continues immediately. Measures the WORKING SET
# (v3.3: current minus reclaimable page cache), not raw memory.current.
MEM_GATE_GB           = 200.0    # cgroup memory ceiling checked before the bake loop; lower to suit your machine
MEM_GATE_WAIT_S       = 900.0   # max seconds to wait for memory to fall below the gate
MEM_GATE_POLL_S       = 15.0
QUIET_LOG             = True    # drop known-benign recurring warnings from the mirrored log
QUIET_LOG_PATTERNS    = (
    "HIPEW initialization failed",
    "More than one shader node tex image used for a texture",
)
# M1-C visual comparison (addon/visual_validation.py).  Off by default: it adds
# Cycles renders per part.  It never changes generated maps or UVs, so it is NOT
# part of config_fingerprint(); its settings are recorded in manifest_config().
VISUAL_VALIDATION          = False   # save a source-vs-output visual report per OK part
VISUAL_VALIDATION_GATE     = False   # True: any non-PASS verdict blocks finalize and fails the census
VISUAL_VALIDATION_SETTINGS = None    # dict overriding visual_validation.default_settings()
_VISUAL_RUN_ID: Optional[str] = None  # one report folder per run; set by main()
# v3 (M1) adds material_capabilities + per-object capability; the checkpoint
# (resume.checkpoints, restored_from_checkpoint) and visual_validation keys are
# additive and optional.
MANIFEST_SCHEMA       = 3
DONE_LIST_SCHEMA      = 6             # v6 also fingerprints the generated Blender object
DONE_ENTRY_SCHEMA     = 4             # validates resumable per-object metadata
CGROUP_MEM_CURRENT    = "/sys/fs/cgroup/memory.current"

# ===========================================================================
# v3.6 COLOUR MANAGEMENT
#
# The invariant: reproduce Blender's own pipeline, IN ITS ORDER, and then encode
# into whatever the output image declares.
#
#   Blender:  filter the STORED texels -> decode if the image says sRGB
#             -> apply the socket's conversion (named channel, or Rec.709 luma
#                for a Color output landing on a float socket)
#   here:     resample the STORED texels -> decode if the source says sRGB
#             -> apply the socket's conversion -> encode for the output map
#
# The order is not a preference, it is measured. On this Blender (5.2.0 LTS,
# Cycles CPU, 2026-09-17) a two-texel sRGB image sampled with Linear
# interpolation exactly between its texel centres evaluates to 0.21404125.
# Filtering in encoded space and decoding after predicts 0.21404114 (delta
# 1e-7); filtering in linear light predicts 0.31846605. Closest-interpolation
# controls at both texel centres returned the decoded values exactly, so the
# fixture is sound. Blender filters in the stored encoding. An earlier version
# of this patch decoded BEFORE resampling and changed 32.6% of an ordinary
# colour map by up to 69/255 -- correct-looking, and wrong.
#
# What engine <=3.5 got wrong was not the order but the CONVERSIONS: _load()
# forced every source to Non-Color and returned the stored texels, and the crop
# path wrote them unchanged, while load_preview_texture() declares the colour
# map sRGB and the data maps Non-Color. Two opposite errors followed:
#
#   * a Non-Color / linear source used as Base Color was written as-is and then
#     decoded as sRGB by the output material. Measured 2026-09-17: source
#     [0.2000, 0.5020, 0.8000] came out [0.0331, 0.2158, 0.6038], exactly
#     srgb_to_linear() of the source (to 1e-7, float32 noise). Visibly darker.
#     It needs the ENCODE that was missing.
#   * an sRGB-authored ROUGHNESS/METALNESS map IS decoded by Blender before it
#     reaches a float socket, but the crop path copied the encoded texels into a
#     map it declared Non-Color. Measured: Blender evaluated 0.215861, the output
#     carried 0.501953. It needs the DECODE that was missing. This direction was
#     not in the 2026-09-17 review; it is the same defect mirrored, and it is the
#     common case, because Blender tags a freshly loaded PNG sRGB unless someone
#     sets Non-Color by hand.
#
# For the ordinary case -- an sRGB source feeding Base Color, output declared
# sRGB -- the decode and the encode cancel, so convert_after_resample() applies
# NEITHER and the written bytes are identical to engine 3.5's. That is checked
# by tests/blender/byte_identity.py, not assumed.

# Colour spaces whose stored texels are sRGB-encoded and which Blender decodes
# on the way to a shader socket. Anything not listed is treated as already
# scene-linear (Non-Color, Raw, Linear*, ACES*, and friends), which is the
# conservative choice: it applies no conversion rather than inventing one.
_SRGB_ENCODED_COLORSPACES = {
    "srgb", "srgb eotf", "srgb - texture", "srgb texture",
    "utility - srgb - texture", "input - srgb - texture",
    "srgb encoded rec.709 (sRGB)".lower(),
    "srgb encoded rec709 - texture", "srgb - display",
}


def colorspace_is_srgb_encoded(name: Optional[str]) -> bool:
    if not name:
        return False
    return str(name).strip().lower() in _SRGB_ENCODED_COLORSPACES


def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    """Decode sRGB-encoded texels to scene-linear, as Blender does on input."""
    v = np.asarray(values, dtype=np.float32)
    return np.where(
        v <= 0.04045,
        v / np.float32(12.92),
        np.power(np.maximum((v + np.float32(0.055)) / np.float32(1.055),
                            np.float32(0.0)), np.float32(2.4)),
    ).astype(np.float32)


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """Encode scene-linear to sRGB, for a map the output declares as sRGB."""
    v = np.asarray(values, dtype=np.float32)
    return np.where(
        v <= 0.0031308,
        v * np.float32(12.92),
        np.float32(1.055) * np.power(np.maximum(v, np.float32(0.0)),
                                     np.float32(1.0) / np.float32(2.4))
        - np.float32(0.055),
    ).astype(np.float32)


# Blender's implicit colour -> float-socket conversion is the Rec.709 luma of
# the scene-linear colour, not its red channel. engine <=3.5 used index 0 for
# any scalar source with no explicit component, so a green roughness texture
# read 0.0 where the real shader evaluated 0.715168 (measured 2026-09-17):
# a rough surface shipped mirror-smooth.
_LUMA_709 = (0.2126, 0.7152, 0.0722)


def rgb_to_scalar(rgb: np.ndarray) -> np.ndarray:
    """Blender's colour -> value conversion for a float socket."""
    a = np.asarray(rgb, dtype=np.float32)
    return (a[..., 0] * np.float32(_LUMA_709[0])
            + a[..., 1] * np.float32(_LUMA_709[1])
            + a[..., 2] * np.float32(_LUMA_709[2])).astype(np.float32)


def source_needs_decode(src: Optional["SourceRef"]) -> bool:
    """True when the source's STORED texels are sRGB-encoded, so Blender would
    have decoded them on the way to the socket.

    A SourceRef records the colour space its Image Texture node declared at
    trace time. The disk-index route has no node to ask, so it records None; for
    those, Blender's own default for a loaded 8-bit file applies, which is sRGB
    for a colour map and Non-Color only if something set it. Guessing sRGB for a
    DATA map there would corrupt every plain roughness/normal file, so None is
    treated as "no decode" and the decision is recorded in the manifest.
    """
    if src is None:
        return False
    return colorspace_is_srgb_encoded(src.colorspace)


# ===========================================================================
# v3.6 SAMPLING CONTRACT
#
# engine <=3.5 recorded an Image Texture node as nothing but its image name and
# an optional channel. It never looked at the node's Vector input, uv_map,
# extension, interpolation or projection, so a Mapping node translating U by
# 0.5 was invisible: the shader evaluated blue and CROP emitted red, and the
# run reported one OK object, CLEAN, exit 0 (measured 2026-09-17).
#
# A direct pixel copy is an assertion that the output UV layout samples the
# same texels the source shader did. That is only true when the coordinates are
# the plain UVs the output will carry, with no transform and a flat projection.
# Anything else has to go through a real evaluation.

# =============================== STAGE TIMING ===============================
#
# engine <=3.5 reported one number for a whole run (`elapsed_seconds`) plus
# per-channel prints inside the V3.1 section main() never calls. That is enough
# to say a run was slow and nothing else, so every previous speed claim rested on
# whole-run wall time and an argument about which change caused it.
#
# This records, per named stage: call count, INCLUSIVE seconds (the stage and
# everything under it) and EXCLUSIVE seconds (the stage's own time, with nested
# stages subtracted). Exclusive time is what sums to the total, so a breakdown
# cannot quietly add up to more than the run took -- the mistake that makes
# "stage X is 80% of runtime" claims unfalsifiable.
#
# Cost: one perf_counter() pair and a few dict lookups per stage entry. Stages are
# placed around work measured in milliseconds or more, never inside a per-pixel
# loop, so the instrument does not become the thing being measured.

_STAGE_TOTALS: Dict[str, List[float]] = {}
_STAGE_STACK: List[List[float]] = []   # [start, child_time_accumulated]
_STAGE_ENABLED = True


def reset_stage_timers() -> None:
    _STAGE_TOTALS.clear()
    del _STAGE_STACK[:]


class stage:
    """Context manager recording inclusive and exclusive wall time for a stage."""

    __slots__ = ("name", "_frame")

    def __init__(self, name: str):
        self.name = name
        self._frame = None

    def __enter__(self):
        if _STAGE_ENABLED:
            self._frame = [time.perf_counter(), 0.0]
            _STAGE_STACK.append(self._frame)
        return self

    def __exit__(self, *exc):
        if self._frame is None:
            return False
        inclusive = time.perf_counter() - self._frame[0]
        if _STAGE_STACK and _STAGE_STACK[-1] is self._frame:
            _STAGE_STACK.pop()
        exclusive = inclusive - self._frame[1]
        if _STAGE_STACK:
            _STAGE_STACK[-1][1] += inclusive
        row = _STAGE_TOTALS.get(self.name)
        if row is None:
            _STAGE_TOTALS[self.name] = [1.0, inclusive, exclusive]
        else:
            row[0] += 1.0
            row[1] += inclusive
            row[2] += exclusive
        return False


def stage_report(total_wall: Optional[float] = None) -> Dict[str, Any]:
    """Machine-readable stage breakdown, sorted by exclusive time."""
    rows = []
    for name, (calls, inclusive, exclusive) in _STAGE_TOTALS.items():
        rows.append({
            "stage": name, "calls": int(calls),
            "inclusive_s": round(inclusive, 4),
            "exclusive_s": round(exclusive, 4),
        })
    rows.sort(key=lambda r: -r["exclusive_s"])
    accounted = sum(r["exclusive_s"] for r in rows)
    out: Dict[str, Any] = {
        "stages": rows,
        "accounted_exclusive_s": round(accounted, 4),
    }
    if total_wall:
        out["total_wall_s"] = round(total_wall, 4)
        out["unaccounted_s"] = round(max(0.0, total_wall - accounted), 4)
        out["accounted_fraction"] = round(accounted / total_wall, 4) if total_wall else None
        for r in rows:
            r["pct_of_wall"] = round(100.0 * r["exclusive_s"] / total_wall, 2)
    return out


def print_stage_report(total_wall: Optional[float] = None) -> Dict[str, Any]:
    report = stage_report(total_wall)
    print("MACHINE|stages accounted=%.3f total=%.3f" % (
        report["accounted_exclusive_s"], report.get("total_wall_s") or 0.0), flush=True)
    print("STAGE BREAKDOWN (exclusive seconds)", flush=True)
    for r in report["stages"][:24]:
        print("  %-28s calls=%-6d excl=%8.3fs incl=%8.3fs%s" % (
            r["stage"], r["calls"], r["exclusive_s"], r["inclusive_s"],
            ("  %5.1f%%" % r["pct_of_wall"]) if "pct_of_wall" in r else ""), flush=True)
    if "unaccounted_s" in report:
        print("  %-28s %33.3fs  %5.1f%%" % (
            "(unaccounted)", report["unaccounted_s"],
            100.0 * report["unaccounted_s"] / report["total_wall_s"]
            if report["total_wall_s"] else 0.0), flush=True)
    return report


@dataclass(frozen=True)
class SamplingContract:
    """How an image texture is actually sampled. Frozen so a SourceRef stays
    hashable and usable as a cache / fingerprint key."""
    coord_source: str = "DEFAULT"          # DEFAULT|UV|OBJECT|GENERATED|...
    uv_layer: Optional[str] = None         # None = the active render UV layer
    transform: Optional[Tuple[float, ...]] = None   # None = identity
    extension: str = "REPEAT"
    interpolation: str = "Linear"
    projection: str = "FLAT"

    def direct_copy_reason(self, output_uv: Optional[str]) -> Optional[str]:
        """None when a pixel copy is provably equivalent; else why it is not."""
        if self.coord_source not in ("DEFAULT", "UV"):
            return "coord-source:" + self.coord_source
        if self.transform is not None:
            return "uv-transform:" + ",".join("%g" % v for v in self.transform)
        if self.projection != "FLAT":
            return "projection:" + self.projection
        if (self.uv_layer is not None and output_uv is not None
                and self.uv_layer != output_uv):
            return "uv-layer:%s!=%s" % (self.uv_layer, output_uv)
        return None

    def label(self) -> str:
        bits = [self.coord_source]
        if self.uv_layer:
            bits.append("uv=" + self.uv_layer)
        if self.transform is not None:
            bits.append("xform")
        if self.projection != "FLAT":
            bits.append("proj=" + self.projection)
        if self.interpolation != "Linear":
            bits.append("interp=" + self.interpolation)
        if self.extension != "REPEAT":
            bits.append("ext=" + self.extension)
        return "/".join(bits)


IDENTITY_SAMPLING = SamplingContract()

# How a traced source is turned into the number the socket consumed.
CONV_DIRECT = "DIRECT"        # take the channel/RGBA as stored (after decode)
CONV_LUMINANCE = "LUMINANCE"  # Blender's implicit colour -> float conversion


@dataclass(frozen=True)
class SourceRef:
    kind: str                            # DISK | IMAGE
    key: str                             # absolute path or Blender image name
    component: Optional[str] = None      # R/G/B/A -- EXPLICIT extraction only
    conversion: str = CONV_DIRECT        # CONV_DIRECT | CONV_LUMINANCE
    colorspace: Optional[str] = None     # colour space the source is authored in
    sampling: Optional[SamplingContract] = None
    # v3.6: tangent-space convention DECLARED by the Normal Map node that this
    # source reached the shader through: "OPENGL" | "DIRECTX", or None when
    # nothing declared one (a raw _n file wired straight into Normal, or the
    # filename-index route), in which case SOURCE_NORMAL_IS_DIRECTX is the
    # documented fallback.
    normal_convention: Optional[str] = None

    @property
    def label(self) -> str:
        base = os.path.basename(self.key) if self.kind == "DISK" else self.key
        out = base + ((":" + self.component) if self.component else "")
        if self.conversion != CONV_DIRECT:
            out += ":" + self.conversion.lower()
        return out

    @property
    def sampling_or_identity(self) -> SamplingContract:
        return self.sampling or IDENTITY_SAMPLING

    def semantics(self) -> Tuple[Any, ...]:
        """Everything that changes the numbers this source yields. Goes into
        resume fingerprints, so two materials that differ only in channel
        selection, colour space or sampling can never be conflated."""
        s = self.sampling_or_identity
        return (self.kind, self.key, self.component, self.conversion,
                (self.colorspace or "").lower(), self.normal_convention,
                s.coord_source, s.uv_layer,
                s.transform, s.extension, s.interpolation, s.projection)


@dataclass
class AtlasSet:
    name: str
    maps: Dict[str, SourceRef]
    rough_default: float = DEFAULT_ROUGH
    metal_default: float = DEFAULT_METAL
    # v3.6: opacity is part of the material, not an accident of the colour
    # image. engine <=3.5 stored no constant alpha at all and process_crop()
    # fell back to the colour image's own alpha channel, which produced two
    # opposite measured errors: a constant Alpha of 0.25 shipped fully opaque,
    # and an UNUSED 0.25 image-alpha turned an opaque material transparent.
    alpha_default: float = 1.0
    alpha_linked: bool = False
    method: str = "unknown"
    confidence: str = "LOW"
    notes: List[str] = field(default_factory=list)

    def signature(self) -> Tuple[Any, ...]:
        return tuple(
            (ch,
             self.maps[ch].semantics() if ch in self.maps else None,
             round(self.rough_default, 6), round(self.metal_default, 6),
             round(self.alpha_default, 6), self.alpha_linked)
            for ch in SOURCE_CHANNELS
        )

    def direct_copy_reason(self, output_uv: Optional[str]) -> Optional[str]:
        """None when every traced map can be copied rather than evaluated."""
        for semantic, src in sorted(self.maps.items()):
            reason = src.sampling_or_identity.direct_copy_reason(output_uv)
            if reason:
                return "%s:%s" % (semantic, reason)
        return None


@dataclass
class UVStats:
    bounds: Tuple[float, float, float, float]
    triangle_area: float
    fill_ratio: float
    min_face_area: float
    outside_01: bool


@dataclass
class Job:
    source: Any
    file_base: str
    source_uv: str
    used_slots: List[int]
    sets_by_slot: Dict[int, AtlasSet]
    materials_by_slot: Dict[int, Optional[Any]]
    resolution_by_slot: Dict[int, str]
    unresolved_slots: List[int]
    uv: UVStats
    route: str
    route_reason: str
    warnings: List[str] = field(default_factory=list)


_LOG_STATE = {"file": None, "stdout": None}
_NOTHING_SELECTED = False   # v3.3: set by main() when selection/scoping leaves nothing to bake
_USED_BASES: Set[str] = set()
# v1.1.1: resume bookkeeping used to fail in total silence.  save_done_list() and
# load_done_list() only printed MACHINE|WARN, which no UI surface reads, so an
# unwritable or corrupt done-list looked exactly like a clean run -- and silently
# re-baked every object on the next run.  The manifest holds a LIVE reference to
# this list, so anything appended before the manifest is serialized ships in it.
_DONE_LIST_WARNINGS: List[str] = []
# v3.6: originals of any image filepath the precheck repointed in this session,
# so finalize can put them back before writing anything. See
# restore_repointed_filepaths().
_REPOINTED_FILEPATHS: List[Tuple[str, str]] = []
# ext -> paths seen in the last index build that need an external converter, so
# the manifest can name them instead of leaving the operator to wonder.
_LAST_INDEX_ADAPTER_NEEDED: Dict[str, List[str]] = {}
_MAT_CACHE: Dict[int, Tuple[Optional[AtlasSet], str]] = {}
_DIM_CACHE: Dict[Tuple[str, str], Tuple[int, int]] = {}
_SOURCE_FILE_DIGEST_CACHE: Dict[Tuple[str, int, int], str] = {}
_LIVE_IMAGE_DIGEST_CACHE: Dict[Tuple[int, int, int], str] = {}


def reset_run_state() -> None:
    """Reset state that was naturally fresh when this file ran as a script.

    A Blender add-on keeps imported modules alive between operator calls.  The
    standalone build did not need to clear these caches because every headless
    invocation started a new Python process.  Without this reset, a second UI
    run can inherit object-pointer caches, claimed output names, a stale
    nothing-selected flag, and a redirected ``sys.stdout``.
    """
    reset_stage_timers()
    global _NOTHING_SELECTED, _CENSUS, _T0
    restore_log()
    _NOTHING_SELECTED = False
    _CENSUS = None
    _T0 = time.time()
    for cache_name in (
        "_USED_BASES", "_DONE_LIST_WARNINGS", "_MAT_CACHE", "_DIM_CACHE",
        "_ATLAS_VALID_CACHE",
        "_SOURCE_FILE_DIGEST_CACHE", "_LIVE_IMAGE_DIGEST_CACHE", "_BAKE_EVENTS",
        "_NORMAL_ENCODING_ADJUSTMENTS",
    ):
        cache = globals().get(cache_name)
        if hasattr(cache, "clear"):
            cache.clear()


class Tee:
    def __init__(self, file_obj: Any, original: Any):
        self.file_obj = file_obj
        self.original = original

    def write(self, text: str) -> None:
        # v3.2: two known-benign warnings recur on literally every run (HIPEW on a
        # CPU-only pod, the glTF multi-tex-image sampler note on every multi-material
        # export) and make real warnings harder to grep for.  Best effort only:
        # Blender's C-level stdout does not pass through this Tee.
        if QUIET_LOG and any(p in text for p in QUIET_LOG_PATTERNS):
            return
        self.original.write(text)
        try:
            self.file_obj.write(text)
            self.file_obj.flush()
        except Exception:
            pass

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass


def install_log(folder: str) -> None:
    restore_log()
    path = os.path.join(folder, "run_log.txt")
    f = open(path, "w", encoding="utf-8")
    _LOG_STATE["file"] = f
    _LOG_STATE["stdout"] = sys.stdout
    sys.stdout = Tee(f, sys.stdout)
    print("Console mirrored to:", path)


def restore_log() -> None:
    if _LOG_STATE.get("stdout") is not None:
        sys.stdout = _LOG_STATE["stdout"]
        _LOG_STATE["stdout"] = None
    f = _LOG_STATE.get("file")
    if f is not None:
        try:
            f.close()
        except Exception:
            pass
        _LOG_STATE["file"] = None


# ---------------------------- v3.2 provenance ---------------------------------

def tool_file_md5(path: Optional[str] = None) -> str:
    """md5 of this source file, so a stale private copy self-identifies."""
    try:
        target = path or os.path.abspath(__file__)
        with open(target, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return "unknown"


def provenance() -> Dict[str, str]:
    try:
        path = os.path.abspath(__file__)
    except Exception:
        path = "<unknown>"
    return {
        "tool": TOOL_NAME, "version": TOOL_VERSION,
        "file": path, "md5": tool_file_md5(path),
    }


def provenance_banner(tag: str = "") -> Dict[str, str]:
    """Print version + file md5 at every run start (drift receipt: a private
    import_glue_wave2.py copy diverged silently mid-campaign)."""
    info = provenance()
    print("=" * 78)
    print("%s v%s%s" % (info["tool"], info["version"], (" | " + tag) if tag else ""))
    print("file: %s" % info["file"])
    print("md5 : %s" % info["md5"])
    print("=" * 78, flush=True)
    print("MACHINE|provenance tool=%s version=%s md5=%s file=%s"
          % (info["tool"], info["version"], info["md5"], info["file"]), flush=True)
    return info


def VERSION_CHECK(expected_version: Optional[str] = None,
                  expected_md5: Optional[str] = None,
                  strict: bool = False) -> Dict[str, Any]:
    """Callers assert which build they imported.  With strict=True a mismatch
    raises instead of warning, so a driver can refuse to run against a stale copy."""
    info = provenance()
    problems = []
    if expected_version and expected_version != info["version"]:
        problems.append("version %s != expected %s" % (info["version"], expected_version))
    if expected_md5 and expected_md5 != info["md5"]:
        problems.append("md5 %s != expected %s" % (info["md5"], expected_md5))
    result = dict(info)
    result["ok"] = not problems
    result["problems"] = problems
    if problems:
        message = "VERSION_CHECK FAILED: %s (%s)" % ("; ".join(problems), info["file"])
        if strict:
            raise RuntimeError(message)
        print("MACHINE|WARN " + message, flush=True)
    return result


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_text(value: Any, fallback: str = "unnamed") -> str:
    """Best-effort text conversion for damaged rip-derived ID names."""
    try:
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)
    except (UnicodeDecodeError, UnicodeEncodeError):
        try:
            return repr(value).encode("utf-8", "replace").decode("utf-8")
        except Exception:
            return fallback
    except Exception:
        return fallback


def datablock_name(value: Any, fallback: str = "unnamed") -> str:
    try:
        return safe_text(value.name, fallback)
    except Exception:
        return fallback


def pow2_ceil(value: float, minimum: int = 1, maximum: Optional[int] = None) -> int:
    n = max(minimum, int(math.ceil(value)))
    p = 1 << (n - 1).bit_length()
    if maximum is not None:
        p = min(p, maximum)
    return p


def _output_base_key(base: str) -> str:
    """Use the destination filesystem's case rules for namespace claims."""
    return os.path.normcase(base)


def safe_unique_name(name: str) -> str:
    name = safe_text(name, "part")
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "part"
    clean = clean[:54]
    base = clean
    if _output_base_key(base) in _USED_BASES:
        digest = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:8]
        base = (clean[:45] + "_" + digest)[:54]
        counter = 2
        while _output_base_key(base) in _USED_BASES:
            base = (clean[:42] + "_" + digest + "_%d" % counter)[:54]
            counter += 1
    _USED_BASES.add(_output_base_key(base))
    return base


def _entry_output_bases(entry: Dict[str, Any]) -> List[str]:
    """Return every namespace claim made by a resumable output entry."""
    values: List[str] = []
    root = safe_text(entry.get("file_base"), "")
    if root:
        values.append(root)
    for value in entry.get("file_bases") or []:
        base = safe_text(value, "")
        if base and base not in values:
            values.append(base)
    return values


def _reserve_output_bases(bases: Sequence[str]) -> None:
    """Claim pre-existing output names before any new jobs are planned."""
    normalized: List[str] = []
    keys: Set[str] = set()
    for value in bases:
        base = safe_text(value, "")
        if (
            not base or len(base) > 54
            or re.fullmatch(r"[A-Za-z0-9_-]+", base) is None
        ):
            raise RuntimeError("resume entry has an unsafe output base: %r" % base)
        key = _output_base_key(base)
        if key in keys:
            continue
        if key in _USED_BASES:
            raise RuntimeError("output base is already claimed in this run: %s" % base)
        normalized.append(base)
        keys.add(key)
    _USED_BASES.update(keys)


def output_directory() -> str:
    # v3.4: OUTPUT_ROOT was documented ("pins the output directory instead of writing beside
    # the open .blend") since the file's own header comment but never referenced anywhere --
    # dead config. Wire it up so the documented behaviour actually exists.
    if OUTPUT_ROOT:
        blend_dir = OUTPUT_ROOT
    else:
        blend_dir = os.path.dirname(bpy.data.filepath)
        if not blend_dir:
            blend_dir = bpy.app.tempdir
    stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "scene"
    parent = os.path.join(blend_dir, stem + "_exports")
    os.makedirs(parent, exist_ok=True)
    i = 1
    while True:
        name = OUTPUT_SUBDIR if i == 1 else "%s%d" % (OUTPUT_SUBDIR, i)
        path = os.path.join(parent, name)
        if not os.path.exists(path):
            os.makedirs(path)
            return path
        i += 1


def output_collection() -> Any:
    base = OUTPUT_COLLECTION
    name = base
    i = 2
    while bpy.data.collections.get(name) is not None:
        name = "%s_%d" % (base, i)
        i += 1
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    return coll


def triangle_count(obj: Any) -> int:
    """Count the evaluated mesh that exporters and Cycles actually consume."""
    if obj is None or getattr(obj, "type", None) != "MESH":
        raise RuntimeError("triangle count requires a mesh object")
    if not obj.modifiers:
        obj.data.calc_loop_triangles()
        return len(obj.data.loop_triangles)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_obj = obj.evaluated_get(depsgraph)
    evaluated_mesh = None
    try:
        evaluated_mesh = evaluated_obj.to_mesh()
        if evaluated_mesh is None:
            raise RuntimeError("evaluated modifier stack produced no mesh")
        evaluated_mesh.calc_loop_triangles()
        return len(evaluated_mesh.loop_triangles)
    finally:
        if evaluated_mesh is not None:
            evaluated_obj.to_mesh_clear()


def choose_source_uv(mesh: Any) -> Optional[str]:
    if not mesh.uv_layers:
        return None
    valid: List[Tuple[Any, str]] = []
    for layer in mesh.uv_layers:
        try:
            name = safe_text(layer.name, "")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        # Blender-internal edit/selection layers are not texture coordinates.
        if not name or name.startswith("."):
            continue
        valid.append((layer, name))
    if not valid:
        return None
    for layer, name in valid:
        if getattr(layer, "active_render", False):
            return name
    active = mesh.uv_layers.active
    for layer, name in valid:
        if layer == active:
            return name
    return valid[0][1]


def is_previous_output(obj: Any) -> bool:
    name = datablock_name(obj, "")
    if name.startswith(PREVIOUS_OBJECT_PREFIXES):
        return True
    for slot in getattr(obj, "material_slots", ()): 
        material = getattr(slot, "material", None)
        if material is None:
            continue
        if datablock_name(material, "").endswith(PREVIOUS_MATERIAL_SUFFIXES):
            return True
    return False


def uv_layer_by_name(mesh: Any, layer_name: str) -> Optional[Any]:
    """Decode-safe UV lookup without bpy_prop_collection.get().

    Some imported meshes contain a malformed CustomData layer name.  Blender's
    collection.get(name) may decode every existing name while searching and can
    therefore raise before it reaches the requested, perfectly valid layer.
    Index access does not require decoding the name; only inspect each name
    inside its own exception boundary.
    """
    for index in range(len(mesh.uv_layers)):
        layer = mesh.uv_layers[index]
        try:
            if layer.name == layer_name:
                return layer
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return None


def uv_layer_index_by_name(mesh: Any, layer_name: str) -> Optional[int]:
    for index in range(len(mesh.uv_layers)):
        layer = mesh.uv_layers[index]
        try:
            if layer.name == layer_name:
                return index
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return None


def read_uvs(mesh: Any, layer_ref: Any) -> np.ndarray:
    layer = uv_layer_by_name(mesh, layer_ref) if isinstance(layer_ref, str) else layer_ref
    if layer is None:
        label = layer_ref if isinstance(layer_ref, str) else "<UV layer>"
        raise RuntimeError("UV layer disappeared: " + safe_text(label, "<UV layer>"))
    buf = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.data.foreach_get("uv", buf)
    return buf.reshape(-1, 2)


def uv_stats(mesh: Any, layer_ref: Any) -> UVStats:
    uvs = read_uvs(mesh, layer_ref)
    if uvs.size == 0 or not np.isfinite(uvs).all():
        raise RuntimeError("empty or non-finite UV data")
    u0, v0 = (float(x) for x in uvs.min(axis=0))
    u1, v1 = (float(x) for x in uvs.max(axis=0))
    mesh.calc_loop_triangles()
    if not mesh.loop_triangles:
        raise RuntimeError("mesh has no triangles")
    idx = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", idx)
    tri = uvs[idx].reshape(-1, 3, 2)
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    areas = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
    bbox_area = max((u1 - u0) * (v1 - v0), 1e-20)
    total = float(areas.sum())
    fill = clamp(total / bbox_area, 0.0, 1.0)
    outside = (
        u0 < -UV_OUTSIDE_TOL or v0 < -UV_OUTSIDE_TOL
        or u1 > 1.0 + UV_OUTSIDE_TOL or v1 > 1.0 + UV_OUTSIDE_TOL
    )
    return UVStats(
        bounds=(u0, v0, u1, v1),
        triangle_area=total,
        fill_ratio=fill,
        min_face_area=float(areas.min()) if areas.size else 0.0,
        outside_01=outside,
    )


def used_material_slots(mesh: Any) -> List[int]:
    return sorted({int(p.material_index) for p in mesh.polygons})


def image_dimensions_from_file(path: str) -> Optional[Tuple[int, int]]:
    """Read width/height from a file HEADER, without decoding the image.

    v3.6 adds TGA, BMP and GIF alongside PNG. When this returns None the caller
    falls back to bpy.data.images.load() plus a .size access, which decodes the
    whole image just to measure it -- and resolve_material() measures every
    candidate colour map for the MIN_COLOR_SIZE gate. The local corpus holds 1019
    TGA files, so on that family the expensive fallback was the common case.

    JPEG is deliberately left to the fallback: its dimensions sit behind a
    variable-length marker walk rather than at a fixed offset, and reading that
    subtly wrong would mis-gate a real texture.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except Exception:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(head[16:20], "big"),
                    int.from_bytes(head[20:24], "big"))
        if head[:2] == b"BM" and len(head) >= 26:
            return (int.from_bytes(head[18:22], "little", signed=True),
                    abs(int.from_bytes(head[22:26], "little", signed=True)))
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return (int.from_bytes(head[6:8], "little"),
                    int.from_bytes(head[8:10], "little"))
        # TGA carries no magic number, so validate the fields that must hold for
        # its real image types before trusting them; otherwise any 18-byte file
        # named .tga would report a size.
        if len(head) >= 18 and path.lower().endswith(".tga"):
            if head[2] in (1, 2, 3, 9, 10, 11):
                width = int.from_bytes(head[12:14], "little")
                height = int.from_bytes(head[14:16], "little")
                if width > 0 and height > 0 and head[16] in (8, 15, 16, 24, 32):
                    return width, height
    except Exception:
        return None
    return None


def source_dimensions(src: SourceRef) -> Tuple[int, int]:
    # v3.6: key on the IMAGE, not the whole SourceRef. Dimensions depend on the
    # file alone, while a SourceRef now also carries component, colour space,
    # conversion, sampling and normal convention -- so keying on it measured the
    # same image once per channel it feeds, and every miss can cost a full
    # decode. Widening SourceRef for the fidelity work is what made that bite.
    cache_key = (src.kind, src.key)
    if cache_key in _DIM_CACHE:
        return _DIM_CACHE[cache_key]
    if src.kind == "IMAGE":
        img = bpy.data.images.get(src.key)
        if img is None:
            raise RuntimeError("image datablock missing: " + src.key)
        dims = (int(img.size[0]), int(img.size[1]))
    else:
        dims = image_dimensions_from_file(src.key)
        if dims is None:
            img = bpy.data.images.load(src.key, check_existing=False)
            try:
                dims = (int(img.size[0]), int(img.size[1]))
            finally:
                bpy.data.images.remove(img)
    if min(dims) <= 0:
        raise RuntimeError("zero-size image: " + src.label)
    _DIM_CACHE[cache_key] = dims
    return dims


# --------------------------- source set discovery ----------------------------

def source_dirs() -> List[str]:
    out: List[str] = []
    blend_dir = os.path.dirname(bpy.data.filepath)
    candidates: List[str] = []
    if blend_dir:
        current = os.path.abspath(blend_dir)
        for _ in range(max(0, SOURCE_PARENT_LEVELS) + 1):
            for folder_name in SOURCE_FOLDER_NAMES:
                candidates.append(os.path.join(current, folder_name))
            # The .blend directory itself commonly contains exported maps.
            if current == os.path.abspath(blend_dir):
                candidates.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    candidates.extend(SOURCE_DIRS)
    seen: Set[str] = set()
    for path in candidates:
        norm = os.path.normcase(os.path.abspath(path)) if path else ""
        if path and norm not in seen and os.path.isdir(path):
            seen.add(norm)
            out.append(path)
    return out


def suffix_match(stem: str) -> Optional[Tuple[str, str, Optional[str]]]:
    lower = stem.lower()
    for suffix, semantic, component in SUFFIX_RULES:
        if lower.endswith(suffix.lower()):
            return stem[:-len(suffix)], semantic, component
    return None


def auxiliary_match(stem: str) -> Optional[Tuple[str, str]]:
    """(base, role) when a stem names a recognised non-delivered role."""
    lower = stem.lower()
    for suffix, role in AUXILIARY_SUFFIX_RULES:
        if lower.endswith(suffix):
            return stem[:-len(suffix)], role
        # MLSetup masks are indexed: <base>_masksset_3
        if suffix == "_masksset":
            marker = "_masksset_"
            at = lower.rfind(marker)
            if at >= 0 and lower[at + len(marker):].isdigit():
                return stem[:at], role
    return None


def suffix_channels(stem: str) -> Optional[Tuple[str, List[Tuple[str, Optional[str]]]]]:
    """Group one filename stem into (base, [(semantic, component), ...]).

    v1.1.1a: added as a SIBLING of suffix_match() rather than a replacement so
    the single-channel table keeps its own byte-identical behaviour and its own
    callers.  A SUFFIX_RULES hit comes back here as a one-item list holding the
    exact triple suffix_match() already returned, so every pre-existing rule
    still yields the same base, semantic and component it always did; only the
    MULTI_SUFFIX_RULES entries can produce more than one pair.
    """
    lower = stem.lower()
    for suffix, channels in MULTI_SUFFIX_RULES:
        if lower.endswith(suffix.lower()):
            return stem[:-len(suffix)], list(channels)
    match = suffix_match(stem)
    if match is None:
        return None
    base, semantic, component = match
    return base, [(semantic, component)]


def build_disk_index() -> Dict[str, AtlasSet]:
    records: Dict[str, Dict[str, SourceRef]] = defaultdict(dict)
    display: Dict[str, str] = {}
    # base -> [(role, path)] for files whose role exists but is not delivered.
    auxiliary: Dict[str, List[Tuple[str, str]]] = {}
    # ext -> [paths] for textures that need an external converter first.
    adapter_needed: Dict[str, List[str]] = {}
    # v1.1.1a: a packed multi-channel file is only a FALLBACK for the channels it
    # carries.  Recording it inline would let X_ORM.tga claim rough via
    # setdefault() before the sorted listing ever reaches a dedicated X_RGH.tga
    # ("O" sorts before "R"), silently preferring a packed component to the
    # explicit map beside it.  Collect those hits here and replay them after
    # every folder has contributed its single-channel maps.
    deferred: List[Tuple[str, str, List[Tuple[str, Optional[str]]]]] = []
    dirs = source_dirs()
    for folder in dirs:
        try:
            names = sorted(os.listdir(folder))
        except Exception:
            continue
        for filename in names:
            stem, ext = os.path.splitext(filename)
            low_ext = ext.lower()
            if low_ext not in IMAGE_EXTS:
                if low_ext in ADAPTER_REQUIRED_EXTS:
                    adapter_needed.setdefault(low_ext, []).append(
                        os.path.join(folder, filename))
                continue
            full = os.path.join(folder, filename)
            # v1.1.1a: suffix_channels(), not suffix_match(), so one packed file
            # (an ORM) can feed two channels of the same set.  The no-match and
            # single-rule branches produce exactly what they produced before.
            match = suffix_channels(stem)
            if match is None:
                # v3.6: a recognised auxiliary role contributes its base for
                # grouping and NO channel, instead of fabricating a colour-only
                # atlas that then competes with the real set.
                aux = auxiliary_match(stem)
                if aux is not None:
                    aux_base, aux_role = aux
                    auxiliary.setdefault(aux_base.lower(), []).append(
                        (aux_role, full))
                    display.setdefault(aux_base.lower(), aux_base)
                    continue
                base, channels = stem, [("color", None)]
            else:
                base, channels = match
            key = base.lower()
            display.setdefault(key, base)
            if len(channels) > 1:
                deferred.append((key, full, channels))
                continue
            for semantic, component in channels:
                records[key].setdefault(semantic, SourceRef("DISK", full, component))
    for key, full, channels in deferred:
        for semantic, component in channels:
            records[key].setdefault(semantic, SourceRef("DISK", full, component))

    for base, mapping in EXTRA_SETS.items():
        key = base.lower()
        for folder in dirs:
            found: Dict[str, SourceRef] = {}
            for semantic, filename in mapping.items():
                path = os.path.join(folder, filename)
                if os.path.isfile(path):
                    component = "R" if filename.lower().endswith("_rm01.png") else None
                    found[semantic] = SourceRef("DISK", path, component)
            if "color" in found:
                records[key].update(found)
                display[key] = base
                break

    # v3.4: the MIN_COLOR_SIZE dimension check used to run HERE, eagerly, for every
    # candidate found in the folder listing -- decoding a file just to measure it,
    # whether or not any selected object's material ever references it. Filter by
    # name only (free) and defer the dimension check to atlas_is_valid(), called
    # lazily by resolve_material() only for a candidate actually about to be used.
    # See build_packed_index()'s docstring for the measured cost this avoids.
    result: Dict[str, AtlasSet] = {}
    for key, maps in records.items():
        color = maps.get("color")
        if color is None or PLACEHOLDER_RE.search(display.get(key, key)):
            continue
        # v3.6: carry the recognised-but-unrepresented siblings into the set's
        # notes, so the manifest states what was present and not delivered rather
        # than leaving the operator to notice the file count does not add up.
        aux_notes = []
        for role, path in sorted(auxiliary.get(key, [])):
            aux_notes.append("auxiliary:%s=%s" % (role, os.path.basename(path)))
        result[key] = AtlasSet(
            name=display.get(key, key), maps=dict(maps),
            method="disk-index", confidence="HIGH", notes=aux_notes
        )
    orphan_aux = sorted(k for k in auxiliary if k not in result)
    if orphan_aux:
        # An auxiliary file with no delivered siblings is genuinely nothing this
        # tool can convert. Saying so once beats indexing it as a fake colour set.
        print("MACHINE|aux_only_groups=%d sample=%s"
              % (len(orphan_aux), orphan_aux[:6]))
    if auxiliary:
        print("Texture index: %d recognised non-delivered file(s) in %d group(s)"
              % (sum(len(v) for v in auxiliary.values()), len(auxiliary)))
    for low_ext, paths in sorted(adapter_needed.items()):
        print("MACHINE|adapter_required ext=%s count=%d hint=%s"
              % (low_ext, len(paths), ADAPTER_REQUIRED_EXTS[low_ext]))
        print("  %d %s file(s) found but not decodable here; sample: %s"
              % (len(paths), low_ext, ", ".join(os.path.basename(p) for p in paths[:3])))
    _LAST_INDEX_ADAPTER_NEEDED.clear()
    _LAST_INDEX_ADAPTER_NEEDED.update(
        {k: list(v) for k, v in adapter_needed.items()})
    print("Texture index: %d candidate set(s) from %d folder(s)" % (len(result), len(dirs)))
    for folder in dirs:
        print("  source:", folder)
    return result


def build_packed_index() -> Dict[str, AtlasSet]:
    """Index packed/generated Blender image datablocks by normal suffix rules.

    v3.4: no longer touches image.size for every image datablock in the .blend.
    Accessing .size on a packed-but-not-yet-decoded image forces Blender to decode
    it right there -- this was the dominant cost of the whole function (measured
    9.7s of a 44s run, on a file with a moderate texture count; scales with the
    WHOLE blend's image count, not with what's actually selected). Classification
    here is by NAME only (free); the size check moves into atlas_is_valid(),
    called lazily by resolve_material() only for a candidate about to be used.
    """
    if not INDEX_PACKED_IMAGES:
        return {}
    records: Dict[str, Dict[str, SourceRef]] = defaultdict(dict)
    display: Dict[str, str] = {}
    # v1.1.1a: deferred exactly as in build_disk_index() -- an explicit
    # <base>_rgh datablock must outrank <base>_orm's green component regardless
    # of the order bpy.data.images happens to iterate in.
    deferred: List[Tuple[str, str, List[Tuple[str, Optional[str]]]]] = []
    for image in bpy.data.images:
        try:
            name = datablock_name(image, "")
            if not name or image.source not in {"FILE", "GENERATED"}:
                continue
            stem = os.path.splitext(os.path.basename(name))[0]
            # v1.1.1a: same multi-channel grouping as build_disk_index().  The
            # packed index feeds the SAME merged index resolve_material() votes
            # against (build_source_index()), so leaving it on the single-channel
            # path would keep re-injecting the bogus "<base>_orm as COLOR" set
            # this patch exists to remove.
            match = suffix_channels(stem)
            if match is None:
                base, channels = stem, [("color", None)]
            else:
                base, channels = match
            key = base.lower()
            display.setdefault(key, base)
            if len(channels) > 1:
                deferred.append((key, name, channels))
                continue
            for semantic, component in channels:
                records[key].setdefault(semantic, SourceRef("IMAGE", name, component))
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        except Exception:
            continue
    for key, name, channels in deferred:
        for semantic, component in channels:
            records[key].setdefault(semantic, SourceRef("IMAGE", name, component))
    result: Dict[str, AtlasSet] = {}
    for key, maps in records.items():
        color = maps.get("color")
        if color is None or PLACEHOLDER_RE.search(display.get(key, key)):
            continue
        result[key] = AtlasSet(
            name=display.get(key, key), maps=dict(maps),
            method="packed-image-index", confidence="MEDIUM",
            notes=["resolved from Blender image datablocks"],
        )
    print("Packed image index: %d candidate set(s)" % len(result))
    return result


def build_source_index() -> Dict[str, AtlasSet]:
    """Merge sources without letting weaker packed guesses replace disk sets."""
    with stage("index.packed"):
        packed = build_packed_index()
    with stage("index.disk"):
        disk = build_disk_index()
    merged = dict(packed)
    merged.update(disk)
    return merged


def cycles_output_node(tree: Any) -> Optional[Any]:
    """Return the output Cycles actually evaluates, never the EEVEE output."""
    try:
        node = tree.get_output_node("CYCLES")
        if node is not None:
            return node
    except Exception:
        pass
    candidates = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    cycles = [n for n in candidates if getattr(n, "target", "ALL") == "CYCLES"]
    common = [n for n in candidates if getattr(n, "target", "ALL") == "ALL"]
    for group in (cycles, common, candidates):
        active = next((n for n in group if getattr(n, "is_active_output", False)), None)
        if active is not None:
            return active
        if group:
            return group[0]
    return None


def match_socket(sockets: Sequence[Any], reference: Any) -> Optional[Any]:
    for socket in sockets:
        if getattr(socket, "identifier", None) == getattr(reference, "identifier", None):
            return socket
    for socket in sockets:
        if socket.name == reference.name:
            return socket
    return None


def group_output_node(tree: Any) -> Optional[Any]:
    nodes = [n for n in tree.nodes if n.type == "GROUP_OUTPUT"]
    return next((n for n in nodes if getattr(n, "is_active_output", False)), None) \
        or (nodes[0] if nodes else None)


def _mix_shader_active_branches(node: Any) -> Tuple[List[Any], Optional[str]]:
    """Which shader inputs of a Mix/Add Shader actually contribute.

    A Mix Shader whose Fac is an unlinked 0.0 or 1.0 is a constant fold: only
    one branch reaches the surface, so treating the graph as that branch is an
    equivalence. Any other factor -- linked, or strictly between 0 and 1 --
    means both branches contribute and the result is NOT a single closure.
    Add Shader always sums both. Returns (contributing sockets, fold-note).
    """
    shader_inputs = [i for i in node.inputs if i.type == "SHADER"]
    if node.type == "ADD_SHADER" or len(shader_inputs) != 2:
        return shader_inputs, None
    fac = node.inputs.get("Fac")
    if fac is None or fac.is_linked:
        return shader_inputs, None
    try:
        value = float(fac.default_value)
    except Exception:
        return shader_inputs, None
    if math.isclose(value, 0.0, abs_tol=1e-6):
        return [shader_inputs[0]], "mix-fold=0"
    if math.isclose(value, 1.0, abs_tol=1e-6):
        return [shader_inputs[1]], "mix-fold=1"
    return shader_inputs, None


def collect_principled(
    tree: Any,
    socket: Any,
    stack: List[Tuple[Any, Any]],
    found: List[Tuple[Any, Any, List[Tuple[Any, Any]]]],
    depth: int = 0,
    structure: Optional[Dict[str, Any]] = None,
) -> None:
    """Collect Principled nodes reachable from a surface socket.

    v3.6 also records the CLOSURE STRUCTURE in `structure`. engine <=3.5
    counted unique Principled nodes and called one "simple": a Principled mixed
    with a Transparent BSDF at factor 0.5 therefore resolved to CROP and
    shipped fully opaque, discarding the transparent branch entirely (measured
    2026-09-17). Counting one reachable Principled does not prove one effective
    surface.
    """
    if socket is None or depth > MAX_TRACE_DEPTH:
        return
    for link in socket.links:
        node, from_socket = link.from_node, link.from_socket
        kind = node.type
        if kind == "BSDF_PRINCIPLED":
            found.append((node, tree, list(stack)))
        elif kind == "REROUTE":
            collect_principled(tree, node.inputs[0], stack, found, depth + 1,
                               structure)
        elif kind in {"MIX_SHADER", "ADD_SHADER"}:
            branches, fold = _mix_shader_active_branches(node)
            if structure is not None:
                if fold:
                    structure.setdefault("folds", []).append(
                        "%s:%s" % (kind.lower(), fold))
                if len(branches) > 1:
                    structure.setdefault("mixes", []).append(kind.lower())
            for inp in branches:
                collect_principled(tree, inp, stack, found, depth + 1, structure)
        elif kind == "GROUP" and node.node_tree is not None:
            gout = group_output_node(node.node_tree)
            inner = match_socket(gout.inputs, from_socket) if gout else None
            if inner is not None:
                collect_principled(
                    node.node_tree, inner, stack + [(tree, node)], found,
                    depth + 1, structure
                )
        elif kind == "GROUP_INPUT" and stack:
            outer_tree, group_node = stack[-1]
            outer = match_socket(group_node.inputs, from_socket)
            if outer is not None:
                collect_principled(outer_tree, outer, stack[:-1], found,
                                   depth + 1, structure)
        elif structure is not None:
            # A non-Principled closure on the surface path (Transparent, Glass,
            # Emission, Volume...) is part of the result even though it has no
            # Principled inputs to read. Record it so eligibility can refuse.
            structure.setdefault("other_closures", []).append(kind)


SEPARATE_TYPES = {"SEPARATE_COLOR", "SEPRGB", "SEPARATE_XYZ", "SEPXYZ"}
COMPONENT_NAMES = {
    "R": "R", "RED": "R", "X": "R",
    "G": "G", "GREEN": "G", "Y": "G",
    "B": "B", "BLUE": "B", "Z": "B",
    "A": "A", "ALPHA": "A",
}


# Vector-input nodes that resolve to a plain, describable sampling contract.
# Anything outside this set makes the coordinates unknown, which is a hard
# refusal for the copy shortcut rather than something to guess at.
_TEXCOORD_OUTPUT_TO_SOURCE = {
    "UV": "UV", "OBJECT": "OBJECT", "GENERATED": "GENERATED",
    "NORMAL": "NORMAL", "CAMERA": "CAMERA", "WINDOW": "WINDOW",
    "REFLECTION": "REFLECTION",
}


def resolve_vector_chain(
    tree: Any, socket: Any, stack: List[Tuple[Any, Any]], depth: int = 0,
) -> Tuple[Optional[SamplingContract], Optional[str]]:
    """Describe what feeds an Image Texture node's Vector input.

    Returns (contract-fragment, reason). The fragment carries coord_source,
    uv_layer and transform only; the caller fills in the node's own extension /
    interpolation / projection. An unlinked Vector means "the active render UV
    layer, untransformed", which is the one case a pixel copy can honour.
    """
    if depth > MAX_TRACE_DEPTH:
        return None, "vector-trace-depth"
    if socket is None or not socket.links:
        return SamplingContract(coord_source="DEFAULT"), None
    if len(socket.links) != 1:
        return None, "vector-multilink"
    link = socket.links[0]
    node, from_socket = link.from_node, link.from_socket
    kind = node.type
    if kind == "REROUTE":
        return resolve_vector_chain(tree, node.inputs[0], stack, depth + 1)
    if kind == "UVMAP":
        # An empty uv_map means the active render layer, same as unlinked.
        name = safe_text(getattr(node, "uv_map", ""), "") or None
        if getattr(node, "from_instancer", False):
            return None, "uvmap-from-instancer"
        return SamplingContract(coord_source="UV", uv_layer=name), None
    if kind == "TEX_COORD":
        if getattr(node, "object", None) is not None:
            return None, "texcoord-object-override"
        src = _TEXCOORD_OUTPUT_TO_SOURCE.get(from_socket.name.upper())
        if src is None:
            return None, "texcoord-output:" + safe_text(from_socket.name, "?")
        return SamplingContract(coord_source=src), None
    if kind == "ATTRIBUTE":
        return None, "attribute-coords:" + safe_text(
            getattr(node, "attribute_name", ""), "?")
    if kind == "MAPPING":
        inner, reason = resolve_vector_chain(
            tree, node.inputs.get("Vector"), stack, depth + 1)
        if inner is None:
            return None, reason
        values: List[float] = []
        for name in ("Location", "Rotation", "Scale"):
            sock = node.inputs.get(name)
            if sock is None:
                continue
            if sock.is_linked:
                return None, "mapping-linked:" + name.lower()
            try:
                values.extend(float(v) for v in sock.default_value)
            except Exception:
                return None, "mapping-unreadable:" + name.lower()
        vector_type = safe_text(getattr(node, "vector_type", "POINT"), "POINT")
        identity = (len(values) == 9
                    and all(math.isclose(v, 0.0, abs_tol=1e-6) for v in values[:6])
                    and all(math.isclose(v, 1.0, abs_tol=1e-6) for v in values[6:]))
        if identity and vector_type == "POINT":
            # A Mapping node left at its defaults changes nothing, so the
            # shortcut survives. This is an equivalence, not an assumption:
            # location 0, rotation 0, scale 1, POINT.
            return inner, None
        # A non-identity transform (or a TEXTURE/VECTOR/NORMAL mapping type,
        # whose maths differs even at default values) is recorded so
        # direct_copy_reason() can refuse, and so it lands in the fingerprint.
        marker = {"POINT": 0.0, "TEXTURE": 1.0, "VECTOR": 2.0, "NORMAL": 3.0}
        return dataclass_replace(
            inner,
            transform=tuple([marker.get(vector_type, 9.0)] + values)), None
    if kind == "GROUP" and node.node_tree is not None:
        gout = group_output_node(node.node_tree)
        dinner = match_socket(gout.inputs, from_socket) if gout else None
        if dinner is None:
            return None, "vector-group-output-unmapped"
        return resolve_vector_chain(
            node.node_tree, dinner, stack + [(tree, node)], depth + 1)
    if kind == "GROUP_INPUT" and stack:
        outer_tree, group_node = stack[-1]
        outer = match_socket(group_node.inputs, from_socket)
        if outer is None:
            return None, "vector-group-input-unmapped"
        return resolve_vector_chain(outer_tree, outer, stack[:-1], depth + 1)
    return None, "vector-node:" + kind


def image_sampling(tree: Any, node: Any, stack: List[Tuple[Any, Any]]
                   ) -> Tuple[Optional[SamplingContract], Optional[str]]:
    """The full sampling contract of one Image Texture node."""
    base, reason = resolve_vector_chain(tree, node.inputs.get("Vector"), stack)
    if base is None:
        return None, reason
    return dataclass_replace(
        base,
        extension=safe_text(getattr(node, "extension", "REPEAT"), "REPEAT"),
        interpolation=safe_text(getattr(node, "interpolation", "Linear"), "Linear"),
        projection=safe_text(getattr(node, "projection", "FLAT"), "FLAT"),
    ), None


def trace_direct_image(
    tree: Any,
    socket: Any,
    stack: List[Tuple[Any, Any]],
    component: Optional[str] = None,
    depth: int = 0,
    target_kind: str = "COLOR",
) -> Tuple[Optional[SourceRef], Optional[str]]:
    """Trace only lossless routing nodes.  Mix/Math/ColorRamp is ambiguous.

    target_kind is what the destination socket consumes -- "COLOR", "SCALAR" or
    "VECTOR". It decides whether a Color output reaching a float socket is an
    implicit luminance conversion (SCALAR) or a plain colour read (COLOR);
    engine <=3.5 had no way to tell those apart and silently took red.
    """
    if socket is None or depth > MAX_TRACE_DEPTH:
        return None, "trace-depth"
    if len(socket.links) != 1:
        return None, "unlinked-or-multilink"
    link = socket.links[0]
    node, from_socket = link.from_node, link.from_socket
    kind = node.type
    if kind == "TEX_IMAGE":
        if node.image is None:
            return None, "image-node-empty"
        name = datablock_name(node.image, "")
        if not name:
            return None, "image-name-unreadable"
        sampling, reason = image_sampling(tree, node, stack)
        if sampling is None:
            return None, reason
        is_alpha_socket = from_socket.name.upper() == "ALPHA"
        comp = "A" if is_alpha_socket else component
        # An explicitly extracted component, or an alpha read, is already a
        # scalar. A Color output landing on a float socket is not: Blender
        # converts it by Rec.709 luma.
        conversion = CONV_DIRECT
        if comp is None and target_kind == "SCALAR":
            conversion = CONV_LUMINANCE
        try:
            colorspace = safe_text(node.image.colorspace_settings.name, "") or None
        except Exception:
            colorspace = None
        return SourceRef("IMAGE", name, comp, conversion, colorspace, sampling), None
    if kind == "REROUTE":
        return trace_direct_image(tree, node.inputs[0], stack, component,
                                  depth + 1, target_kind)
    if kind == "NORMAL_MAP":
        # v1.1.1 refused the shortcut when the Normal Map node's own convention
        # contradicted SOURCE_NORMAL_IS_DIRECTX, because flipping green would
        # invert data that was already correct. That was the right call while the
        # convention was a single global, but it is a refusal where an answer
        # exists: a Blender 4.x+ Normal Map node STATES its convention, and that
        # statement is the truth for this source.
        #
        # v3.6 records it per source instead. The green flip downstream is then
        # driven by what this source declared, so a DirectX map is flipped, an
        # OpenGL map is not, and a batch mixing both families is correct for
        # every member -- which one global flag could never be. The global
        # remains the documented fallback for a source that declares nothing.
        # This also restores the fast path for OpenGL normal maps, which the
        # 1.1.1 gate refused outright whenever the flag was left at its
        # shipped True.
        declared_convention = safe_text(
            getattr(node, "convention", "OPENGL"), "OPENGL").upper()
        if declared_convention not in ("OPENGL", "DIRECTX"):
            return None, "normal-map-convention-unknown:" + declared_convention
        space = safe_text(getattr(node, "space", "TANGENT"), "TANGENT")
        if space != "TANGENT":
            return None, "normal-map-space:" + space
        strength = node.inputs.get("Strength")
        if strength is not None:
            if strength.is_linked:
                return None, "normal-map-strength-linked"
            try:
                if not math.isclose(float(strength.default_value), 1.0, abs_tol=1e-6):
                    return None, "normal-map-strength=%g" % float(strength.default_value)
            except Exception:
                return None, "normal-map-strength-unreadable"
        # v3.6: the Normal Map node's OWN uv_map selects the UV layer Blender
        # builds the TANGENT BASIS in. It is a second, independent sampling
        # input to the one on the Image Texture node below, and engine <=3.5
        # read neither. A normal map authored against a second UV layer was
        # accepted as a direct trace and then cropped against
        # choose_source_uv()'s pick, which produces a tangent-space map whose
        # basis does not match the geometry it ships with. An empty uv_map means
        # "the active render layer", which is what the output carries.
        nm_uv = safe_text(getattr(node, "uv_map", ""), "") or None
        if nm_uv is not None:
            return None, "normal-map-uv-layer:" + nm_uv
        result, reason = trace_direct_image(
            tree, node.inputs.get("Color"), stack, component, depth + 1,
            "VECTOR",
        )
        if result is None:
            return None, reason
        # A tangent-space normal map is encoded data, not colour. If it is
        # tagged as an sRGB-encoded space, Blender DOES decode it before the
        # Normal Map node, so the vector the shader used is not the stored
        # texels -- and the decode does not commute with the green flip or the
        # renormalisation the copy path applies ((n+1)/2 is defined on the
        # stored value, so 1-decode(g) is not decode(1-g)). Rather than invent
        # an ordering, refuse the shortcut and let the Cycles NORMAL bake derive
        # the map from the real shading normal.
        if source_needs_decode(result):
            return None, "normal-map-source-colorspace:" + safe_text(
                result.colorspace, "?")
        return dataclass_replace(result, normal_convention=declared_convention), None
    if kind in SEPARATE_TYPES:
        comp = component or COMPONENT_NAMES.get(from_socket.name.upper())
        inputs = [i for i in node.inputs if i.type in {"RGBA", "VECTOR"}]
        if len(inputs) != 1:
            return None, "ambiguous-separate-input"
        return trace_direct_image(tree, inputs[0], stack, comp, depth + 1)
    if kind == "GROUP" and node.node_tree is not None:
        gout = group_output_node(node.node_tree)
        inner = match_socket(gout.inputs, from_socket) if gout else None
        if inner is None:
            return None, "group-output-unmapped"
        return trace_direct_image(
            node.node_tree, inner, stack + [(tree, node)], component, depth + 1
        )
    if kind == "GROUP_INPUT" and stack:
        outer_tree, group_node = stack[-1]
        outer = match_socket(group_node.inputs, from_socket)
        if outer is None:
            return None, "group-input-unmapped"
        return trace_direct_image(outer_tree, outer, stack[:-1], component, depth + 1)
    return None, "procedural-node:" + kind


def socket_default(node: Any, name: str, fallback: float) -> float:
    socket = node.inputs.get(name)
    if socket is None or socket.is_linked:
        return fallback
    try:
        return float(socket.default_value)
    except Exception:
        return fallback


def atlas_from_node_graph(material: Any) -> Tuple[Optional[AtlasSet], str]:
    tree = getattr(material, "node_tree", None)
    if not material.use_nodes or tree is None:
        return None, "nodes-disabled"
    output = cycles_output_node(tree)
    if output is None:
        return None, "no-cycles-output"
    found: List[Tuple[Any, Any, List[Tuple[Any, Any]]]] = []
    structure: Dict[str, Any] = {}
    collect_principled(tree, output.inputs.get("Surface"), [], found, 0, structure)
    unique: Dict[Tuple[int, int], Tuple[Any, Any, List[Tuple[Any, Any]]]] = {}
    for node, inner_tree, stack in found:
        unique[(node.as_pointer(), inner_tree.as_pointer())] = (node, inner_tree, stack)
    if len(unique) != 1:
        return None, "principled-count:%d" % len(unique)
    # v3.6: one reachable Principled is necessary but not sufficient. If a
    # Mix/Add Shader genuinely blends branches, or a second non-Principled
    # closure sits on the surface path, the evaluated result is not this
    # Principled node and no copy of its textures can represent it.
    if structure.get("mixes"):
        return None, "mixed-closure:" + ",".join(sorted(set(structure["mixes"])))
    if structure.get("other_closures"):
        return None, "extra-closure:" + ",".join(
            sorted(set(structure["other_closures"])))
    if output.inputs.get("Volume") is not None and output.inputs["Volume"].is_linked:
        return None, "volume-linked"
    displacement = output.inputs.get("Displacement")
    if displacement is not None and displacement.is_linked:
        return None, "displacement-linked"
    principled, inner_tree, stack = next(iter(unique.values()))
    # What each destination socket consumes. Base Color is a colour, Normal is
    # a vector, the rest are floats -- and a Color output landing on a float
    # socket is an implicit luminance conversion, not a red-channel read.
    names = {
        "color": ("Base Color", "COLOR"), "metal": ("Metallic", "SCALAR"),
        "rough": ("Roughness", "SCALAR"), "normal": ("Normal", "VECTOR"),
        "alpha": ("Alpha", "SCALAR"),
    }
    maps: Dict[str, SourceRef] = {}
    notes: List[str] = []
    for semantic, (socket_name, target_kind) in names.items():
        socket = principled.inputs.get(socket_name)
        if socket is None or not socket.is_linked:
            continue
        src, reason = trace_direct_image(inner_tree, socket, stack,
                                         target_kind=target_kind)
        if src is not None:
            maps[semantic] = src
        elif reason:
            notes.append("%s=%s" % (semantic, reason))
    color = maps.get("color")
    if color is None:
        return None, "no-direct-color" + ((";" + ";".join(notes)) if notes else "")
    # A crop/proxy resolution is an assertion about the complete evaluated PBR
    # result, not just Base Color.  If any linked channel crosses an unsupported
    # Mix/Math/ramp/ambiguous path, defaulting that channel would be silently
    # lossy.  Leave genuinely unlinked channels at their explicit defaults, but
    # send every rejected linked channel to GRAPH_BAKE.
    if notes:
        return None, "non-flat-linked-channel;" + ";".join(notes)
    try:
        w, h = source_dimensions(color)
    except Exception as exc:
        return None, "color-unreadable:%s" % exc
    if min(w, h) < MIN_COLOR_SIZE or PLACEHOLDER_RE.search(color.label):
        return None, "placeholder-color:" + color.label
    alpha_socket = principled.inputs.get("Alpha")
    alpha_linked = bool(alpha_socket is not None and alpha_socket.is_linked)
    result = AtlasSet(
        name="node:" + color.label,
        maps=maps,
        rough_default=socket_default(principled, "Roughness", DEFAULT_ROUGH),
        metal_default=socket_default(principled, "Metallic", DEFAULT_METAL),
        # v3.6: the material's real opacity. An unlinked Alpha is a CONSTANT
        # that has to be honoured (0.25 must ship translucent, 1.0 must ship
        # opaque even when the colour image happens to carry a 0.25 alpha
        # channel nothing reads).
        alpha_default=socket_default(principled, "Alpha", 1.0),
        alpha_linked=alpha_linked,
        method="direct-node-trace",
        confidence="HIGH",
        notes=notes + (["closure-folds:" + ",".join(structure["folds"])]
                       if structure.get("folds") else []),
    )
    return result, result.method


def iter_image_names(tree: Any, seen: Optional[Set[int]] = None) -> Iterable[str]:
    if tree is None:
        return
    if seen is None:
        seen = set()
    key = tree.as_pointer()
    if key in seen:
        return
    seen.add(key)
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            name = datablock_name(node.image, "")
            if name:
                yield name
            try:
                if node.image.filepath:
                    yield os.path.basename(bpy.path.abspath(node.image.filepath))
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        elif node.type == "GROUP" and node.node_tree is not None:
            yield from iter_image_names(node.node_tree, seen)


def infer_disk_base(raw_name: str, disk_index: Dict[str, AtlasSet]) -> Optional[str]:
    stem = os.path.splitext(os.path.basename(raw_name))[0]
    # v1.1.1a: must use the SAME grouping the index was built with, or an
    # X_ORM.tga in the graph votes for a base no set is filed under and the
    # vote loses the very evidence the index just learned to keep.
    match = suffix_channels(stem)
    base = match[0] if match else stem
    key = base.lower()
    # v3.4: validity (dimension check) is lazy now -- check it here so an invalid
    # candidate is excluded from the vote exactly as it would have been when the
    # index filtered it out eagerly (see atlas_is_valid()).
    if key in disk_index and atlas_is_valid(disk_index[key]):
        return key
    return None


def safe_image_vote(node_reason: str, votes: Dict[str, int]) -> Tuple[bool, str]:
    """Filename ingredients cannot establish evaluated surface equivalence.

    Proven direct-node sampling and explicit MATERIAL_ATLAS_RULES retain their
    fast paths. Any failed direct trace needs graph evaluation; this includes
    zero-Principled surfaces and disconnected images even with a single vote.
    """
    if not votes:
        return False, "no matching image set"
    if len(votes) != 1:
        return False, "%d competing image sets" % len(votes)
    return False, "filename matches do not prove evaluated surface equivalence: " + node_reason


_ATLAS_VALID_CACHE: Dict[int, bool] = {}


def atlas_is_valid(atlas: AtlasSet) -> bool:
    """v3.4: the MIN_COLOR_SIZE dimension check, deferred from index-build time to
    first actual use (see build_disk_index/build_packed_index docstrings) and cached
    by object identity so a candidate consulted by many materials is only decoded
    once. Behaviourally identical to the old eager filter -- just computed lazily.
    """
    key = id(atlas)
    cached = _ATLAS_VALID_CACHE.get(key)
    if cached is not None:
        return cached
    color = atlas.maps.get("color")
    try:
        w, h = source_dimensions(color) if color is not None else (0, 0)
        ok = min(w, h) >= MIN_COLOR_SIZE
    except Exception:
        ok = False
    _ATLAS_VALID_CACHE[key] = ok
    return ok


def resolve_material(material: Any, disk_index: Dict[str, AtlasSet]) -> Tuple[Optional[AtlasSet], str]:
    cache_key = material.as_pointer()
    if cache_key in _MAT_CACHE:
        with stage("resolve.cache_hit"):
            return _MAT_CACHE[cache_key]
    lower_name = datablock_name(material, "material").lower()

    # 1. Explicit, verified atlas rules.  These intentionally outrank the
    # placeholder-heavy CP2077 material graph.
    for pattern, atlas_name in MATERIAL_ATLAS_RULES:
        key = atlas_name.lower()
        if pattern in lower_name and key in disk_index and atlas_is_valid(disk_index[key]):
            src = disk_index[key]
            result = AtlasSet(
                name=src.name, maps=dict(src.maps), method="material-rule:" + pattern,
                confidence="HIGH", notes=["rule overrides imported node graph"]
            )
            _MAT_CACHE[cache_key] = (result, result.method)
            return result, result.method

    # 2. Conservative direct link trace from the Cycles output.
    with stage("resolve.node_trace"):
        node_set, node_reason = atlas_from_node_graph(material)
    if node_set is not None:
        _MAT_CACHE[cache_key] = (node_set, node_reason)
        return node_set, node_reason

    # 3. Conservative name fallback.  Packed/disk image sets may participate,
    # but an ingredient of a Mix/Math graph is never called its final atlas.
    votes: Dict[str, int] = defaultdict(int)
    for raw in iter_image_names(getattr(material, "node_tree", None)):
        base = infer_disk_base(raw, disk_index)
        if base:
            votes[base] += 1
    vote_ok, vote_reason = safe_image_vote(node_reason, votes)
    if vote_ok:
        key = max(votes, key=lambda k: (votes[k], k))
        src = disk_index[key]
        result = AtlasSet(
            name=src.name, maps=dict(src.maps),
            method="image-vote:%d" % votes[key], confidence="LOW",
            notes=["node trace rejected: " + node_reason, vote_reason]
        )
        _MAT_CACHE[cache_key] = (result, result.method)
        return result, result.method

    reason = "unresolved; node trace: " + node_reason
    if votes:
        reason += "; image vote rejected: " + vote_reason
    _MAT_CACHE[cache_key] = (None, reason)
    return None, reason


# ------------------------------- job planning --------------------------------

def plan_jobs(selected: List[Any], disk_index: Dict[str, AtlasSet]) -> Tuple[List[Job], List[Dict[str, Any]]]:
    with stage("plan.jobs"):
        return _plan_jobs_inner(selected, disk_index)


def _plan_jobs_inner(selected: List[Any], disk_index: Dict[str, AtlasSet]) -> Tuple[List[Job], List[Dict[str, Any]]]:
    jobs: List[Job] = []
    skipped: List[Dict[str, Any]] = []
    for obj in selected:
        object_name = datablock_name(obj, "mesh_object")
        _run_boundary("planning_object", object=object_name)
        try:
            if len(obj.data.polygons) == 0:
                raise RuntimeError("empty mesh")
            source_uv = choose_source_uv(obj.data)
            no_source_uv = source_uv is None
            if no_source_uv:
                # v3.4: don't SKIP outright -- force GRAPH_BAKE (see the route override
                # below) and let create_missing_source_uv() smart-project a real UV layer
                # on the DUPLICATE before baking. There is nothing to measure yet, so this
                # is a placeholder Job.uv; process_graph_bake() replaces it with the real
                # post-unwrap stats. AUTO_UNWRAP_NO_UV=False restores the old behaviour.
                if not AUTO_UNWRAP_NO_UV:
                    raise RuntimeError("no usable UV layer (internal/invalid layers ignored)")
                source_uv = NO_SOURCE_UV_SENTINEL
                stats = UVStats(bounds=(0.0, 0.0, 1.0, 1.0), triangle_area=0.0,
                                 fill_ratio=0.0, min_face_area=0.0, outside_01=False)
            else:
                stats = uv_stats(obj.data, source_uv)
            used = used_material_slots(obj.data)
            sets: Dict[int, AtlasSet] = {}
            materials: Dict[int, Optional[Any]] = {}
            reasons: Dict[int, str] = {}
            unresolved: List[int] = []
            resolution_notes: List[str] = []
            for slot_index in used:
                if slot_index >= len(obj.material_slots):
                    material = None
                    atlas, reason = None, "missing-material-slot"
                else:
                    material = obj.material_slots[slot_index].material
                    if material is None:
                        atlas, reason = None, "empty-material-slot"
                    elif not getattr(material, "use_nodes", False):
                        atlas, reason = None, "nodes-disabled"
                    else:
                        atlas, reason = resolve_material(material, disk_index)
                materials[slot_index] = material
                reasons[slot_index] = reason
                material_name = datablock_name(material, "<empty>") if material else "<empty>"
                resolution_notes.append("slot %d %s -> %s" % (
                    slot_index, material_name, reason
                ))
                if atlas is None:
                    unresolved.append(slot_index)
                else:
                    sets[slot_index] = atlas

            # A missing/empty used material slot cannot contribute meaningful
            # pixels.  The census already rejects such an output, so sending it
            # through four expensive GRAPH_BAKE passes only wastes GPU time and
            # can turn a large materialless scene into hundreds of doomed jobs.
            # Keep the legacy neutral-grey path available when that census rule
            # is explicitly disabled in a custom/headless configuration.
            missing_material_slots = [
                slot for slot, resolution_reason in reasons.items()
                if resolution_reason in {"missing-material-slot", "empty-material-slot"}
            ]
            if CENSUS_FAIL_ON_NOMAT and missing_material_slots:
                raise RuntimeError(
                    "used material slot(s) are missing or empty: %s"
                    % ",".join(str(slot) for slot in missing_material_slots)
                )

            signatures = {atlas.signature() for atlas in sets.values()}
            all_resolved = bool(used) and not unresolved and len(sets) == len(used)
            compact = stats.fill_ratio >= CROP_MIN_FILL

            # v3.6 SAMPLING GATE. Both pixel-reuse routes assume the output
            # samples the same texels the source shader did:
            #   * CROP copies a rectangle of source texels and rewrites UVs;
            #   * PROXY_BAKE rebuilds the material with source_socket(), which
            #     wires a plain UVMap node straight into the image Vector.
            # Neither can honour a Mapping transform, a second UV layer, a
            # non-UV coordinate source or a non-flat projection. engine <=3.5
            # never looked, so a Mapping node translating U by 0.5 produced a
            # confidently wrong map that the census passed as CLEAN.
            sampling_block: Optional[str] = None
            for slot_index in sorted(sets):
                reason_text = sets[slot_index].direct_copy_reason(source_uv)
                if reason_text:
                    sampling_block = "slot %d %s" % (slot_index, reason_text)
                    break
            if sampling_block:
                resolution_notes.append("sampling blocks pixel reuse: " + sampling_block)

            crop_safe = all_resolved and len(signatures) == 1 \
                and not stats.outside_01 and compact and not sampling_block
            proxy_safe = all_resolved and not sampling_block
            mode = ROUTE_MODE.upper()
            if high_precision_output():
                if mode in {"CROP_ONLY", "PROXY_ONLY"}:
                    raise RuntimeError("PBR_HIGH_PRECISION requires GRAPH_BAKE; incompatible forced route")
                route, reason = "GRAPH_BAKE", "PBR_HIGH_PRECISION requires float graph bake buffers"
            elif mode == "CROP_ONLY":
                if not crop_safe:
                    raise RuntimeError(
                        "CROP_ONLY but crop prerequisites failed"
                        + ("; " + sampling_block if sampling_block else ""))
                route, reason = "CROP", "one atlas set; compact in-range UV rectangle"
            elif mode == "PROXY_ONLY":
                if not all_resolved:
                    raise RuntimeError("PROXY_ONLY but %d used slot(s) are unresolved" % len(unresolved))
                if sampling_block:
                    raise RuntimeError(
                        "PROXY_ONLY but source sampling cannot be reproduced: "
                        + sampling_block)
                route, reason = "PROXY_BAKE", "forced by PROXY_ONLY"
            elif mode == "GRAPH_ONLY":
                route, reason = "GRAPH_BAKE", "forced by GRAPH_ONLY"
            elif mode == "BAKE_ONLY":
                if proxy_safe:
                    route, reason = "PROXY_BAKE", "forced BAKE_ONLY; all slots resolved"
                elif all_resolved and sampling_block:
                    route, reason = "GRAPH_BAKE", \
                        "forced BAKE_ONLY; source sampling needs evaluation: " + sampling_block
                else:
                    route, reason = "GRAPH_BAKE", \
                        "forced BAKE_ONLY; unresolved slots %s" % unresolved
            elif crop_safe:
                route, reason = "CROP", "one atlas set; compact in-range UV rectangle"
            elif not all_resolved:
                route = "GRAPH_BAKE"
                # v3.6: carry the per-slot resolution reason into the route
                # reason. "unresolved slots [0]" alone told an operator nothing
                # actionable -- which node or property defeated resolution was
                # only in the warnings list. The census and manifest both read
                # route_reason, so this is where it has to appear.
                detail_bits = [
                    "slot %d: %s" % (slot, reasons.get(slot, "?"))
                    for slot in unresolved[:4]
                ]
                reason = "original graph required; unresolved slots %s%s" % (
                    unresolved,
                    ("; " + "; ".join(detail_bits)) if detail_bits else "")
            elif sampling_block:
                # A resolved set whose sampling cannot be reproduced by pixel
                # reuse is not a PROXY candidate either: it must be evaluated.
                route = "GRAPH_BAKE"
                reason = "source sampling needs evaluation: " + sampling_block
            else:
                route = "PROXY_BAKE"
                why = []
                if len(signatures) != 1:
                    why.append("multiple atlas sets")
                if stats.outside_01:
                    why.append("UVs outside 0-1")
                if not compact:
                    why.append("scattered UV fill %.3f" % stats.fill_ratio)
                reason = "; ".join(why) or "crop prerequisites failed"

            # v3.4: a mesh with no source UV layer at all cannot take CROP or PROXY_BAKE
            # (there is no fill/bounds data to base that decision on, and PROXY_BAKE's
            # repack_uv() needs a real layer to already exist on the duplicate). Force
            # GRAPH_BAKE, which auto-unwraps the duplicate before baking -- see
            # create_missing_source_uv(). CROP_ONLY/PROXY_ONLY are explicit narrow modes
            # left to fail loudly as before (crop_safe is already false for a no-UV mesh,
            # so CROP_ONLY already raises above; PROXY_ONLY still fails later, clearly,
            # at repack_uv if forced with no real UV to repack).
            if no_source_uv and mode not in {"CROP_ONLY", "PROXY_ONLY"}:
                route = "GRAPH_BAKE"
                reason = "no source UV layer; will be auto-unwrapped before baking"

            if mode not in {"AUTO", "CROP_ONLY", "PROXY_ONLY", "GRAPH_ONLY", "BAKE_ONLY"}:
                raise RuntimeError("unknown ROUTE_MODE: %s" % ROUTE_MODE)

            warnings: List[str] = []
            tris = triangle_count(obj)
            if tris > TRIANGLE_WARN:
                warnings.append("triangle count %d exceeds warning %d" % (tris, TRIANGLE_WARN))
            if obj.modifiers:
                warnings.append("object has %d modifier(s); verify evaluated export" % len(obj.modifiers))
            warnings.extend(resolution_notes)
            jobs.append(Job(
                source=obj, file_base=safe_unique_name(object_name), source_uv=source_uv,
                used_slots=used, sets_by_slot=sets, materials_by_slot=materials,
                resolution_by_slot=reasons, unresolved_slots=unresolved, uv=stats,
                route=route, route_reason=reason, warnings=warnings,
            ))
        except Exception as exc:
            skipped.append({"object": object_name, "status": "SKIPPED", "reason": safe_text(exc)})
            print("SKIP %s: %s" % (object_name, safe_text(exc)[:200]), flush=True)
    return jobs, skipped


# ------------------------- raw pixel crop pipeline ----------------------------

class PixelCache:
    """Decoded source texels, cached twice over.

    v3.6 adds a RAW tier keyed on the image alone, under the per-usage tier that
    was already here. Decoding is the expensive half of a pixel-reuse job -- a
    4096x4096 RGBA image is 268 MB of float32 through
    `image.pixels.foreach_get`, and the Blender API has no partial read -- while
    deriving one channel from an already-decoded buffer is a cheap slice.

    Before, the cache key was (SourceRef, usage), and a SourceRef carries its
    component. So a packed ORM file, whose whole point is that ONE file feeds
    roughness from green and metalness from blue, arrived as two different keys
    and was decoded TWICE. The local corpus holds 199 `_ORM` files, every one of
    them paying that. The same applies to a `_D` file whose alpha feeds opacity.

    The raw tier is keyed on (kind, key) only, because the stored texels do not
    depend on which channel a caller wants or what it will convert them to --
    v3.6 moved every conversion after resampling, precisely so this is true.
    """

    def __init__(self, max_mb: int):
        self.max_bytes = max_mb * 1024 * 1024
        self.bytes = 0
        self.items: OrderedDict[Tuple[SourceRef, str], np.ndarray] = OrderedDict()
        # (kind, key) -> full RGBA float32, shared by every usage of that image.
        self.raw: OrderedDict[Tuple[str, str], np.ndarray] = OrderedDict()
        self.raw_bytes = 0
        self.stats = {"usage_hit": 0, "usage_miss": 0,
                      "raw_hit": 0, "raw_decode": 0,
                      "evicted_usage": 0, "evicted_raw": 0}

    # ---------------------------------------------------------------- raw tier

    def _raw_rgba(self, src: SourceRef) -> np.ndarray:
        key = (src.kind, src.key)
        cached = self.raw.get(key)
        if cached is not None:
            self.raw.move_to_end(key)
            self.stats["raw_hit"] += 1
            return cached
        with stage("pixels.decode"):
            arr = self._decode_rgba(src)
        self.stats["raw_decode"] += 1
        # The raw tier gets at most half the budget, so caching a whole 4K image
        # cannot starve the per-usage results the caller is actually consuming.
        limit = self.max_bytes // 2
        while self.raw and self.raw_bytes + arr.nbytes > limit:
            _, old = self.raw.popitem(last=False)
            self.raw_bytes -= old.nbytes
            self.stats["evicted_raw"] += 1
        if arr.nbytes <= limit:
            self.raw[key] = arr
            self.raw_bytes += arr.nbytes
        return arr

    # -------------------------------------------------------------- usage tier

    def get(self, src: SourceRef, usage: str) -> np.ndarray:
        key = (src, usage)
        if key in self.items:
            arr = self.items.pop(key)
            self.items[key] = arr
            self.stats["usage_hit"] += 1
            return arr
        self.stats["usage_miss"] += 1
        arr = self._load(src, usage)
        while self.items and self.bytes + arr.nbytes > self.max_bytes:
            _, old = self.items.popitem(last=False)
            self.bytes -= old.nbytes
            self.stats["evicted_usage"] += 1
        if arr.nbytes <= self.max_bytes:
            self.items[key] = arr
            self.bytes += arr.nbytes
        return arr

    def _load(self, src: SourceRef, usage: str) -> np.ndarray:
        """Return RAW STORED texels for `usage`, with colour conversion DEFERRED.

        The decode itself is _decode_rgba(), reached through the shared raw tier
        so one image costs one decode however many channels read it. This method
        only selects what `usage` needs from that buffer.

        No colour conversion happens here. It is applied afterwards by
        convert_after_resample(), because the order matters and it is measurable.

        Measured on this Blender (5.2.0 LTS, Cycles CPU, 2026-09-17): a two-texel
        sRGB image sampled with Linear interpolation exactly between its texel
        centres evaluates to 0.21404125. Filtering in ENCODED space and decoding
        afterwards predicts 0.21404114 (delta 1e-7); filtering in linear light
        predicts 0.31846605. Closest-interpolation controls at both texel centres
        returned the decoded values exactly, so the fixture is sound.
        **Blender filters in the stored encoding and decodes after.** Therefore
        so must this path: resample the raw texels, then convert. Decoding first
        would change 32.6% of an ordinary colour map by up to 69/255.

        Shapes, chosen so the deferred step has what it needs:
          color  -> (H, W, 4) raw
          normal -> (H, W, 3) raw
          scalar -> (H, W) raw channel when a component is named (channel
                    selection commutes with filtering, so it is safe to do
                    early and saves resampling three channels);
                    (H, W, 3) raw RGB when the conversion is LUMINANCE on an
                    sRGB-encoded source, because luma(decode(x)) cannot be
                    recovered from luma(x);
                    (H, W) pre-collapsed luma when the source is already
                    linear, where luma and filtering do commute.
        """
        rgba = self._raw_rgba(src)
        # Every branch below produces an array the caller OWNS. It must never be
        # a view onto the shared raw buffer: process_crop() writes into what it
        # gets back (the green flip, the alpha composite), and a view would
        # corrupt the cache for every later consumer of the same image. np.array
        # with copy=True says that outright instead of relying on
        # ascontiguousarray, which returns the SAME object for an already
        # contiguous input -- the aliasing hazard this tier would otherwise make
        # reachable on every job rather than only on a full-width crop.
        if usage == "color":
            return np.array(rgba, dtype=np.float32, copy=True)
        if usage == "normal":
            return np.array(rgba[..., :3], dtype=np.float32, copy=True)
        if src.component:
            index = "RGBA".index(src.component)
            return np.array(rgba[..., index], dtype=np.float32, copy=True)
        if source_needs_decode(src):
            return np.array(rgba[..., :3], dtype=np.float32, copy=True)
        return np.ascontiguousarray(rgb_to_scalar(rgba[..., :3]), dtype=np.float32)

    @staticmethod
    def _decode_rgba(src: SourceRef) -> np.ndarray:
        """Decode one image to a full RGBA float32 buffer of STORED texels.

        Non-Color is forced on a PRIVATE copy of the datablock so `pixels` hands
        back the stored texels deterministically, independent of the scene's
        colour management, and without changing the source image's own settings.
        """
        remove = True
        if src.kind == "DISK":
            image = bpy.data.images.load(src.key, check_existing=False)
        else:
            original = bpy.data.images.get(src.key)
            if original is None:
                raise RuntimeError("image datablock missing: " + src.key)
            image = original.copy()
        try:
            try:
                image.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            try:
                image.alpha_mode = "CHANNEL_PACKED"
            except Exception:
                pass
            w, h = int(image.size[0]), int(image.size[1])
            if w <= 0 or h <= 0:
                raise RuntimeError("image has no pixels: " + src.label)
            rgba = np.empty(w * h * 4, dtype=np.float32)
            image.pixels.foreach_get(rgba)
            return rgba.reshape(h, w, 4)
        finally:
            if remove and image.name in bpy.data.images:
                bpy.data.images.remove(image)

    def clear(self) -> None:
        self.items.clear()
        self.bytes = 0
        self.raw.clear()
        self.raw_bytes = 0

    def report(self) -> Dict[str, Any]:
        """Hit/miss counts, so a claim about avoided decodes is checkable."""
        out = dict(self.stats)
        out["usage_entries"] = len(self.items)
        out["usage_bytes"] = self.bytes
        out["raw_entries"] = len(self.raw)
        out["raw_bytes"] = self.raw_bytes
        decodes = out["raw_decode"]
        asked = out["usage_miss"]
        if asked:
            out["decodes_avoided"] = max(0, asked - decodes)
            out["decode_reuse_ratio"] = round(1.0 - (decodes / float(asked)), 4)
        return out


def resize_bilinear_timed(array: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    with stage("pixels.resize"):
        return resize_bilinear(array, out_h, out_w)


def resize_bilinear(array: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    # v3.4: separable resize (width pass, then height pass) instead of a joint 4-corner
    # gather. Bilinear interpolation is mathematically separable -- this produces the
    # same result as the original 4-corner-blend version (verified numerically) -- but
    # the intermediate array is only (in_h, out_w) instead of doing every gather against
    # the full (in_h, in_w) source twice, roughly halving peak temporary memory and
    # float32 multiply-adds. Measured as the single largest tottime item in a profiled
    # CROP-route run: 16.5s of a 44s total, entirely CPU/numpy work with 0% GPU use.
    in_h, in_w = array.shape[:2]
    if (in_h, in_w) == (out_h, out_w):
        return np.ascontiguousarray(array)
    is_3d = array.ndim == 3

    xs = np.linspace(0.0, max(in_w - 1, 0), out_w, dtype=np.float32)
    x0 = np.floor(xs).astype(np.int32)
    x1 = np.minimum(x0 + 1, in_w - 1)
    wx = xs - x0
    if is_3d:
        wx3 = wx[None, :, None]
        stage1 = array[:, x0] * (1.0 - wx3) + array[:, x1] * wx3
    else:
        wx2 = wx[None, :]
        stage1 = array[:, x0] * (1.0 - wx2) + array[:, x1] * wx2

    ys = np.linspace(0.0, max(in_h - 1, 0), out_h, dtype=np.float32)
    y0 = np.floor(ys).astype(np.int32)
    y1 = np.minimum(y0 + 1, in_h - 1)
    wy = ys - y0
    if is_3d:
        wy3 = wy[:, None, None]
        out = stage1[y0] * (1.0 - wy3) + stage1[y1] * wy3
    else:
        wy2 = wy[:, None]
        out = stage1[y0] * (1.0 - wy2) + stage1[y1] * wy2
    return np.ascontiguousarray(out, dtype=np.float32)


def normal_is_directx(src: Optional[SourceRef]) -> bool:
    """Whether this normal source needs its green channel flipped for output.

    The output contract is OpenGL (+Y), which is what Roblox documents. A source
    that DECLARED its convention is believed; one that declared nothing falls
    back to SOURCE_NORMAL_IS_DIRECTX, which is what every pre-3.6 build used for
    everything.
    """
    if src is not None and src.normal_convention:
        return src.normal_convention == "DIRECTX"
    return bool(SOURCE_NORMAL_IS_DIRECTX)


def normalize_normal_map(rgb: np.ndarray) -> np.ndarray:
    vec = rgb * 2.0 - 1.0
    length = np.linalg.norm(vec, axis=2, keepdims=True)
    vec = vec / np.maximum(length, 1e-8)
    return np.clip(vec * 0.5 + 0.5, 0.0, 1.0)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: str, array: np.ndarray) -> None:
    """Write uint8 PNG using only Python stdlib; array is bottom-up Blender order."""
    with stage("png.write"):
        return _write_png_inner(path, array)


# v3.6 PARALLEL ENCODE
#
# Where png.write's time actually goes, measured on this host at PNG_COMPRESS_LEVEL 1:
#
#   size     scanline assembly   zlib.compress    assembly share
#   512^2            0.35 ms         13.49 ms          2.5%
#   1024^2           1.09 ms         54.52 ms          2.0%
#   2048^2           3.47 ms        220.86 ms          1.5%
#
# So png.write IS compression, and nothing about the Python around it matters.
# Compression of one map is independent of every other map, and zlib.compress
# releases the GIL, so the four maps of a part can compress at the same time on
# different cores. That is the one legitimate way to reduce this cost without
# touching output: the bytes are identical, only the wall time overlaps.
#
# Bounded on purpose. Each in-flight encode holds its own raw buffer plus the
# compressed result, so an unbounded pool on 4K maps would multiply peak memory
# by the worker count for no benefit beyond the number of maps in a part.
PNG_ENCODE_WORKERS = 4

# ...and gated, because a pool is not free. Measured across four benchmark
# classes, overlapping helped exactly where there was something to overlap:
#
#   packed  12 objects, four real maps each   11.000 s -> 10.392 s   -5.5%
#   many    48 objects, ONE real map + three 4x4 constants
#                                             15.538 s -> 15.646 s   +0.7%
#   heavy    3 objects, 0.80 s total           0.803 s ->  0.815 s   +1.5%
#   batch   12 objects, dominated by 2 GPU bakes
#                                             11.931 s -> 11.997 s   +0.6%
#
# The regressions are inside run-to-run noise, but they are consistent, and their
# cause is plain: creating a ThreadPoolExecutor once per object to compress one
# real map and three 4x4 constants is pure overhead. A map only repays the pool
# if compressing it costs more than setting the pool up -- at
# PNG_COMPRESS_LEVEL 1 a 512x512 map takes ~13.5 ms, which does, and a 4x4
# constant takes ~0, which does not. So the pool is used only when at least two
# maps are big enough to pay for it.
PNG_ENCODE_MIN_PIXELS = 256 * 256
PNG_ENCODE_MIN_ITEMS = 2


def _png_payload(array: np.ndarray) -> bytes:
    """Everything except the file write. Pure, so it is safe off the main thread.

    Deliberately calls no stage() -- the stage stack is a module-level list and is
    not thread safe. The caller times the batch instead.
    """
    data = np.asarray(array)
    if data.dtype != np.uint8:
        data = np.clip(np.rint(data * 255.0), 0, 255).astype(np.uint8)
    if data.ndim == 2:
        channels, color_type = 1, 0
    elif data.ndim == 3 and data.shape[2] in (3, 4):
        channels = data.shape[2]
        color_type = 2 if channels == 3 else 6
    else:
        raise ValueError("PNG array must be HxW, HxWx3, or HxWx4")
    h, w = data.shape[:2]
    rows = np.flipud(np.ascontiguousarray(data)).reshape(h, w * channels)
    scanlines = np.empty((h, w * channels + 1), dtype=np.uint8)
    scanlines[:, 0] = 0
    scanlines[:, 1:] = rows
    raw = scanlines.tobytes()
    header = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    payload = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
    payload += png_chunk(b"IDAT", zlib.compress(raw, PNG_COMPRESS_LEVEL))
    payload += png_chunk(b"IEND", b"")
    return payload


def write_png_batch(items: Sequence[Tuple[str, np.ndarray]]) -> None:
    """Encode several PNGs concurrently, then write them.

    Produces byte-for-byte what a sequence of write_png() calls produces; only
    the wall time differs. An exception in any encode propagates, and nothing is
    written unless every encode succeeded -- a half-written map set is exactly
    what the staging/publication machinery exists to prevent, so this must not
    create one behind its back.
    """
    if not items:
        return
    with stage("png.write"):
        # Only overlap when at least two maps are big enough to repay the pool.
        substantial = sum(1 for _, arr in items
                          if int(np.asarray(arr).shape[0]) * int(np.asarray(arr).shape[1])
                          >= PNG_ENCODE_MIN_PIXELS)
        if (PNG_ENCODE_WORKERS <= 1 or len(items) < 2
                or substantial < PNG_ENCODE_MIN_ITEMS):
            payloads = [_png_payload(arr) for _, arr in items]
        else:
            from concurrent.futures import ThreadPoolExecutor
            workers = min(PNG_ENCODE_WORKERS, substantial)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                payloads = list(pool.map(lambda pair: _png_payload(pair[1]), items))
        for (path, _), payload in zip(items, payloads):
            with open(path, "wb") as fh:
                fh.write(payload)


def _write_png_inner(path: str, array: np.ndarray) -> None:
    data = np.asarray(array)
    if data.dtype != np.uint8:
        data = np.clip(np.rint(data * 255.0), 0, 255).astype(np.uint8)
    if data.ndim == 2:
        channels, color_type = 1, 0
    elif data.ndim == 3 and data.shape[2] in (3, 4):
        channels = data.shape[2]
        color_type = 2 if channels == 3 else 6
    else:
        raise ValueError("PNG array must be HxW, HxWx3, or HxWx4")
    h, w = data.shape[:2]
    rows = np.flipud(np.ascontiguousarray(data)).reshape(h, w * channels)
    # v3.6: build the filtered scanlines in ONE numpy allocation instead of a
    # Python loop over rows. PNG prefixes every scanline with its filter byte, and
    # engine <=3.5 did that with
    #     b"".join(b"\x00" + row.tobytes() for row in rows)
    # which is H iterations, H temporary bytes objects and H concatenations -- 2048
    # of each for a 2K map, per map. Measured on a 48-object CROP batch, png.write
    # was 12.5% of engine wall time. Writing filter byte 0 into column 0 of an
    # (H, W*C+1) buffer and copying the pixels into the rest produces the SAME
    # bytes (filter 0 is "None", so the payload is unchanged) in one pass.
    scanlines = np.empty((h, w * channels + 1), dtype=np.uint8)
    scanlines[:, 0] = 0
    scanlines[:, 1:] = rows
    raw = scanlines.tobytes()
    header = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    payload = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
    payload += png_chunk(b"IDAT", zlib.compress(raw, PNG_COMPRESS_LEVEL)) + png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(payload)


def crop_target_dims(width: int, height: int) -> Tuple[int, int]:
    if FORCE_SQUARE_CROPS:
        side = pow2_ceil(max(width, height), MIN_RES, MAX_RES)
        return side, side
    return (
        pow2_ceil(width, MIN_RES, MAX_RES),
        pow2_ceil(height, MIN_RES, MAX_RES),
    )


def duplicate_mesh_object(source: Any, collection: Any, name: str) -> Any:
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    duplicate.name = name
    duplicate.data.name = name + "_MESH"
    try:
        duplicate.animation_data_clear()
    except Exception:
        pass
    collection.objects.link(duplicate)
    force_visible(duplicate)
    return duplicate


def force_visible(obj: Any) -> Dict[str, bool]:
    """Clear inherited hide flags on a tool-created duplicate.

    Object.copy() copies hide_render/hide_viewport.  A source flagged
    hidden-from-render (disabled reference/backup copies are common in rips)
    produced an output that baked fine and then never rendered, and Cycles
    hard-crashes the bake driver outright with
    `RuntimeError: Object "X" is not enabled for rendering` -- the same three
    Cozy_Cafe objects died this way on three consecutive bake attempts before
    anyone toggled visibility by hand.
    """
    before = {"hide_render": bool(getattr(obj, "hide_render", False)),
              "hide_viewport": bool(getattr(obj, "hide_viewport", False))}
    if not FORCE_VISIBLE_OUTPUT:
        return before
    try:
        obj.hide_render = False
        obj.hide_viewport = False
    except Exception:
        pass
    try:
        obj.hide_set(False)          # view-layer local hide; needs the object in the layer
    except Exception:
        pass
    if before["hide_render"] or before["hide_viewport"]:
        print("    VISIBILITY: cleared inherited hide flags (%s)" % before)
    return before


def finalize_single_uv(mesh: Any, keep_name: str) -> None:
    keep_index = uv_layer_index_by_name(mesh, keep_name)
    if keep_index is None:
        raise RuntimeError("target UV layer missing at finalize")
    # Remove by descending index.  This neither decodes unrelated layer names
    # nor keeps RNA handles alive across CustomData reallocations.
    for index in range(len(mesh.uv_layers) - 1, -1, -1):
        if index != keep_index:
            mesh.uv_layers.remove(mesh.uv_layers[index])
    if len(mesh.uv_layers) != 1:
        raise RuntimeError("could not reduce output to one UV layer")
    layer = mesh.uv_layers[0]
    layer.name = "UVMap"
    layer.active_render = True
    mesh.uv_layers.active = layer


def strip_colors(mesh: Any) -> None:
    if not STRIP_VERTEX_COLORS:
        return
    try:
        while mesh.color_attributes:
            mesh.color_attributes.remove(mesh.color_attributes[0])
    except Exception:
        pass


def remap_crop_uv(mesh: Any, source_uv: str, crop: Tuple[float, float, float, float]) -> None:
    u0, v0, u1, v1 = crop
    du, dv = u1 - u0, v1 - v0
    if du <= 1e-12 or dv <= 1e-12:
        raise RuntimeError("zero-size crop bounds")
    source = read_uvs(mesh, source_uv)
    output = np.empty_like(source)
    output[:, 0] = (source[:, 0] - u0) / du
    output[:, 1] = (source[:, 1] - v0) / dv
    layer = uv_layer_by_name(mesh, source_uv)
    if layer is None:
        raise RuntimeError("source UV missing during crop remap")
    layer.data.foreach_set("uv", np.ascontiguousarray(output).ravel())
    finalize_single_uv(mesh, source_uv)
    mesh.update()


def output_path(folder: str, base: str, semantic: str) -> str:
    return os.path.join(folder, base + OUTPUT_SUFFIX[semantic] + ".png")


# What each delivered map declares when it is read back. Must stay in step with
# load_preview_texture(), which is the reconstruction side of this contract.
OUTPUT_COLORSPACE = {
    "color": "sRGB", "metal": "Non-Color", "rough": "Non-Color",
    "normal": "Non-Color",
}


def convert_after_resample(array: np.ndarray, src: Optional[SourceRef],
                           semantic: str) -> Tuple[np.ndarray, str]:
    """Turn resampled RAW texels into what the output map must contain.

    This is the one place the colour maths lives, and it runs AFTER resampling
    because that is the order Blender itself uses (see PixelCache._load).
    The chain replicated here, per semantic:

      colour  shader saw  decode_if_srgb(filter(raw))
              output declares sRGB, so the output shader will decode whatever
              we write. Write encode(decode_if_srgb(filter(raw))).
              For an sRGB source those cancel exactly, so the bytes are
              untouched -- engine 3.5's output for an ordinary material is
              reproduced bit for bit. For a linear/Non-Color source only the
              encode applies, which is the missing step that made such a
              material ship visibly too dark.

      scalar  shader saw  decode_if_srgb(filter(raw)) then the socket's own
              conversion (a named channel, or Rec.709 luma for a Color output
              on a float socket). Output declares Non-Color, so write that
              value as it is.

      normal  a tangent-space vector, not colour. Decoded if the source says
              it is sRGB-encoded (rare, and wrong authoring, but then the
              original shader decoded it too), otherwise untouched.

    Returns (array, note) where note records what was applied, so the manifest
    can show it rather than leaving it implicit.
    """
    declared = OUTPUT_COLORSPACE.get(semantic, "Non-Color")
    decode = source_needs_decode(src)
    steps: List[str] = []
    out = array

    if semantic == "color":
        out = np.array(out, dtype=np.float32, copy=True)
        if out.ndim != 3 or out.shape[2] < 3:
            raise RuntimeError("colour map needs RGB(A)")
        if decode and declared == "sRGB":
            steps.append("srgb-passthrough")          # decode then encode cancel
        elif decode:
            out[..., :3] = srgb_to_linear(out[..., :3])
            steps.append("srgb-decode")
        elif declared == "sRGB":
            out[..., :3] = linear_to_srgb(out[..., :3])
            steps.append("srgb-encode")
        else:
            steps.append("none")
        return out, "+".join(steps)

    if semantic == "normal":
        # The tracer refuses an sRGB-tagged normal source outright
        # (normal-map-source-colorspace), precisely because the decode does not
        # commute with the green flip and the renormalisation applied upstream of
        # here. So a normal map reaching this point is stored data and is written
        # as-is. The disk-index route carries no colour space, so it lands here
        # too, which is the correct reading for a plain _n/_nor file.
        if decode:
            raise RuntimeError(
                "normal source reached the writer with an sRGB-encoded colour "
                "space (%s); the copy path cannot order the decode against the "
                "green flip, and the tracer should have refused it"
                % (src.colorspace if src is not None else "?"))
        steps.append("none")
        return out, "+".join(steps)

    # scalar semantics (rough / metal / alpha)
    out = np.asarray(out, dtype=np.float32)
    if src is not None and src.component == "A":
        # Alpha is never colour-managed, by Blender or by us.
        steps.append("alpha-raw")
    elif decode:
        out = srgb_to_linear(out)
        steps.append("srgb-decode")
    else:
        steps.append("none")
    if out.ndim == 3:
        # Deferred luminance: _load kept three channels precisely so the decode
        # above could happen first.
        out = rgb_to_scalar(out)
        steps.append("luma709")
    if declared == "sRGB":
        out = linear_to_srgb(out)
        steps.append("srgb-encode")
    return np.ascontiguousarray(out, dtype=np.float32), "+".join(steps)


def encode_constant_for_output(array: np.ndarray, semantic: str) -> np.ndarray:
    """Encode a CONSTANT map (no source image) into the output's encoding.

    DEFAULT_NORMAL is already an encoded tangent-space vector and the data maps
    declare Non-Color, so in practice only a constant colour map would convert.
    Routed through one function so nothing decides encoding on its own.
    """
    if OUTPUT_COLORSPACE.get(semantic) != "sRGB":
        return array
    out = np.array(array, dtype=np.float32, copy=True)
    if out.ndim == 3 and out.shape[2] >= 3:
        out[..., :3] = linear_to_srgb(out[..., :3])
        return out
    return linear_to_srgb(out)


def process_crop(job: Job, duplicate: Any, folder: str, cache: PixelCache) -> Dict[str, Any]:
    with stage("route.crop"):
        require_output_profile_route("CROP")
        dependency = require_material_dependencies(job_dependency_materials(job))
        result = _process_crop_inner(job, duplicate, folder, cache)
        result["output_contract"] = output_contract.build_output_contract(OUTPUT_PROFILE)
        result["uv_validation"] = require_output_uv(duplicate, "CROP")
        result["dependency_validation"] = dependency
        return result


def _process_crop_inner(job: Job, duplicate: Any, folder: str,
                        cache: PixelCache) -> Dict[str, Any]:
    atlas = next(iter(job.sets_by_slot.values()))
    color_src = atlas.maps["color"]
    color_w, color_h = source_dimensions(color_src)
    u0, v0, u1, v1 = job.uv.bounds
    x0 = max(0, int(math.floor(u0 * color_w)) - CROP_PAD_SOURCE_PX)
    x1 = min(color_w, int(math.ceil(u1 * color_w)) + CROP_PAD_SOURCE_PX)
    y0 = max(0, int(math.floor(v0 * color_h)) - CROP_PAD_SOURCE_PX)
    y1 = min(color_h, int(math.ceil(v1 * color_h)) + CROP_PAD_SOURCE_PX)
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    crop_norm = (x0 / color_w, y0 / color_h, x1 / color_w, y1 / color_h)

    result: Dict[str, Any] = {"maps": {}, "crop_source_px": [x0, y0, x1, y1]}
    alpha_used = False
    # v3.6: collect the maps and encode them together at the end. png.write is
    # dominated by zlib (220 ms of a 2048^2 map against 3.5 ms of everything
    # else), the maps are independent, and zlib releases the GIL -- so the four
    # compress the wall-clock of roughly one. The bytes are unchanged.
    pending_writes: List[Tuple[str, np.ndarray]] = []
    for semantic in CHANNELS:
        _run_boundary("pass", object=datablock_name(job.source), route=job.route,
                      semantic=semantic)
        src = atlas.maps.get(semantic)
        path = output_path(folder, job.file_base, semantic)
        if src is None:
            size = DEFAULT_MAP_RES
            if semantic == "metal":
                arr = np.full((size, size), atlas.metal_default, dtype=np.float32)
            elif semantic == "rough":
                arr = np.full((size, size), atlas.rough_default, dtype=np.float32)
            elif semantic == "normal":
                arr = np.empty((size, size, 3), dtype=np.float32)
                arr[...] = DEFAULT_NORMAL
            else:
                raise RuntimeError("resolved atlas has no ColorMap")
            # DEFAULT_NORMAL is already encoded (0.5, 0.5, 1.0); the scalar
            # constants are linear and the data maps declare Non-Color, so
            # neither needs the colour encode. Routed through the same call so
            # there is one place that decides encoding.
            pending_writes.append((path, encode_constant_for_output(arr, semantic)))
            result["maps"][semantic] = {
                "size": [size, size], "source": "DEFAULT",
                "constant": float(arr.reshape(-1)[0]) if arr.ndim == 2 else None,
            }
            continue

        usage = semantic if semantic in {"color", "normal"} else "scalar"
        whole = cache.get(src, usage)
        h, w = whole.shape[:2]
        xm0 = max(0, min(w - 1, int(math.floor(crop_norm[0] * w))))
        xm1 = max(xm0 + 1, min(w, int(math.ceil(crop_norm[2] * w))))
        ym0 = max(0, min(h - 1, int(math.floor(crop_norm[1] * h))))
        ym1 = max(ym0 + 1, min(h, int(math.ceil(crop_norm[3] * h))))
        # copy=True, not ascontiguousarray: a full-width, full-height slice of a
        # contiguous array IS contiguous, so ascontiguousarray hands back the
        # cached buffer itself and the in-place writes below corrupt it.
        sub = np.array(whole[ym0:ym1, xm0:xm1], dtype=np.float32, copy=True)
        out_w, out_h = crop_target_dims(xm1 - xm0, ym1 - ym0)
        sub = resize_bilinear_timed(sub, out_h, out_w)
        if semantic == "normal":
            if normal_is_directx(src):
                sub[..., 1] = 1.0 - sub[..., 1]
            sub = normalize_normal_map(sub)
            result["normal_convention"] = (src.normal_convention
                                           or ("DIRECTX" if SOURCE_NORMAL_IS_DIRECTX
                                               else "OPENGL") + "(fallback)")
        elif semantic == "color":
            # v3.6 opacity contract. The material's Alpha decides this, never
            # the colour image's incidental alpha channel:
            #   * an explicitly traced alpha source is sampled and used;
            #   * otherwise the Principled Alpha CONSTANT is applied, which is
            #     1.0 for an ordinary opaque material and must be honoured when
            #     it is not (0.25 has to ship translucent).
            # engine <=3.5 read sub[..., 3] -- the colour image's own alpha --
            # whenever no alpha source existed, so it both dropped real
            # constant opacity and invented opacity nothing asked for.
            alpha_src = atlas.maps.get("alpha")
            alpha_origin = "none"
            if alpha_src is not None:
                alpha_whole = cache.get(alpha_src, "scalar")
                ah, aw = alpha_whole.shape[:2]
                ax0 = max(0, min(aw - 1, int(math.floor(crop_norm[0] * aw))))
                ax1 = max(ax0 + 1, min(aw, int(math.ceil(crop_norm[2] * aw))))
                ay0 = max(0, min(ah - 1, int(math.floor(crop_norm[1] * ah))))
                ay1 = max(ay0 + 1, min(ah, int(math.ceil(crop_norm[3] * ah))))
                alpha_crop = np.array(alpha_whole[ay0:ay1, ax0:ax1],
                                      dtype=np.float32, copy=True)
                alpha_crop = resize_bilinear_timed(alpha_crop, out_h, out_w)
                sub[..., 3] = np.clip(alpha_crop, 0.0, 1.0)
                alpha_origin = "source:" + alpha_src.label
            else:
                sub[..., 3] = np.float32(clamp(atlas.alpha_default, 0.0, 1.0))
                alpha_origin = "constant:%g" % atlas.alpha_default
            alpha_used = bool(np.any(sub[..., 3] < 0.999))
            sub = sub if alpha_used else sub[..., :3]
            result["alpha_origin"] = alpha_origin
        written, colour_steps = convert_after_resample(sub, src, semantic)
        pending_writes.append((path, written))
        # v3.6: write_png() is 8-bit, so a float/HDR source above 1.0 is clipped.
        # That is a real loss of the source's range and it is reported per map
        # rather than left for someone to notice in a render.
        over = float(np.max(written)) if written.size else 0.0
        clipped_fraction = float(np.mean(written > 1.0)) if written.size else 0.0
        map_detail = {
            "size": [out_w, out_h], "source": src.label,
            "sampling": src.sampling_or_identity.label(),
            "source_colorspace": src.colorspace,
            "conversion": src.conversion,
            "colour_steps": colour_steps,
            "output_colorspace": OUTPUT_COLORSPACE.get(semantic, "Non-Color"),
            "output_bit_depth": 8,
            "source_crop_px": [xm0, ym0, xm1, ym1],
        }
        if semantic == "color":
            # Stats describe what was WRITTEN, so the census's magenta and
            # black-suspect rules keep operating on the same scale as v3.5.
            rgb = written[..., :3]
            map_detail["stats"] = {
                "min": float(rgb.min()), "max": float(rgb.max()),
                "mean": float(rgb.mean()),
                "mean_rgb": [float(value) for value in rgb.mean(axis=(0, 1))],
            }
        if clipped_fraction > 0.0:
            map_detail["hdr_clipped"] = {
                "max_source_value": round(over, 6),
                "fraction_above_one": round(clipped_fraction, 6),
                "note": ("8-bit output clipped values above 1.0; the four-map "
                         "contract cannot carry this source's range"),
            }
            print("MACHINE|WARN hdr_clipped map=%s base=%s max=%.4f frac=%.4f"
                  % (semantic, job.file_base, over, clipped_fraction))
        result["maps"][semantic] = map_detail

    _run_boundary("encode", object=datablock_name(job.source), route=job.route)
    write_png_batch(pending_writes)
    remap_crop_uv(duplicate.data, job.source_uv, crop_norm)
    result["alpha"] = alpha_used
    return result


# ---------------------------- Cycles proxy bake -------------------------------

class ProxyImageCache:
    """Private image copies, so source image color-space settings stay untouched."""
    def __init__(self):
        self.images: Dict[Tuple[SourceRef, str], Any] = {}

    def get(self, src: SourceRef, semantic: str) -> Any:
        key = (src, semantic)
        if key in self.images:
            return self.images[key]
        if src.kind == "DISK":
            image = bpy.data.images.load(src.key, check_existing=False)
        else:
            original = bpy.data.images.get(src.key)
            if original is None:
                raise RuntimeError("image datablock missing: " + src.key)
            image = original.copy()
        image.name = "__RBX_SRC_%s_%s" % (semantic, len(self.images))
        # v3.6: use the colour space the source is AUTHORED in, so Blender
        # applies to the proxy exactly the conversion it applied to the
        # original. engine <=3.5 decided from the semantic alone -- sRGB for
        # colour, Non-Color for everything else -- which is the same
        # double-conversion defect the crop route had, in the proxy route:
        #   * a Non-Color base colour was force-tagged sRGB and therefore
        #     decoded a second time (too dark);
        #   * an sRGB-authored roughness map was force-tagged Non-Color, so the
        #     decode the original DID get was skipped.
        # This route was not covered by the 2026-09-17 review, which reproduced
        # the crop side only.
        target_space = src.colorspace
        if not target_space:
            target_space = "sRGB" if semantic == "color" else "Non-Color"
        try:
            image.colorspace_settings.name = target_space
        except Exception:
            # An unknown/renamed colour space in this Blender's OCIO config is
            # not something to silently substitute: fall back to the semantic
            # default and let the caller's notes record it.
            try:
                image.colorspace_settings.name = (
                    "sRGB" if semantic == "color" else "Non-Color")
            except Exception:
                pass
        self.images[key] = image
        return image

    def close(self) -> None:
        for image in list(self.images.values()):
            if image.name in bpy.data.images:
                bpy.data.images.remove(image)
        self.images.clear()


def set_cycles_output(output: Any) -> None:
    try:
        output.target = "CYCLES"
    except Exception:
        pass


def source_socket(
    tree: Any, src: SourceRef, semantic: str, source_uv: str,
    images: ProxyImageCache, y: float
) -> Any:
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = source_uv
    uv.location = (-900, y)
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = images.get(src, semantic)
    tex.interpolation = "Linear"
    tex.extension = PROXY_IMAGE_EXTENSION
    tex.location = (-650, y)
    tree.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    if src.component == "A":
        return tex.outputs["Alpha"]
    if src.component:
        sep = tree.nodes.new("ShaderNodeSeparateColor")
        sep.mode = "RGB"
        sep.location = (-400, y)
        tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
        names = {"R": "Red", "G": "Green", "B": "Blue", "A": "Alpha"}
        return sep.outputs.get(names[src.component], sep.outputs[0])
    # component=None means the trace reached this image through its Color
    # output (e.g. Principled.Alpha fed by a grayscale mask's Color socket).
    # Sampling the image's own alpha channel here would read the wrong data;
    # the crop route's scalar loader reads the color channels for the same
    # reference, so the proxy must sample Color as well.
    return tex.outputs["Color"]


def normal_source_socket(
    tree: Any, src: SourceRef, source_uv: str, images: ProxyImageCache
) -> Any:
    color = source_socket(tree, src, "normal", source_uv, images, -250)
    if not normal_is_directx(src):
        return color
    sep = tree.nodes.new("ShaderNodeSeparateColor")
    combine = tree.nodes.new("ShaderNodeCombineColor")
    invert = tree.nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.inputs[0].default_value = 1.0
    tree.links.new(color, sep.inputs["Color"])
    tree.links.new(sep.outputs["Red"], combine.inputs["Red"])
    tree.links.new(sep.outputs["Green"], invert.inputs[1])
    tree.links.new(invert.outputs[0], combine.inputs["Green"])
    tree.links.new(sep.outputs["Blue"], combine.inputs["Blue"])
    return combine.outputs["Color"]


def make_proxy_material(
    atlas: AtlasSet,
    semantic: str,
    target: Any,
    source_uv: str,
    images: ProxyImageCache,
    name: str,
) -> Any:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    set_cycles_output(output)
    output.location = (450, 0)
    target_node = tree.nodes.new("ShaderNodeTexImage")
    target_node.image = target
    target_node.label = "__RBX_BAKE_TARGET__"
    target_node.location = (-200, 300)

    def activate_target() -> None:
        for node in tree.nodes:
            node.select = False
        target_node.select = True
        tree.nodes.active = target_node

    if semantic == "normal":
        principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        src = atlas.maps.get("normal")
        if src is not None:
            tree.links.new(
                normal_source_socket(tree, src, source_uv, images),
                normal_map.inputs["Color"],
            )
            tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
        tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
        activate_target()
        return material

    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    if semantic == "alpha":
        # v3.6: same opacity contract as process_crop(). An explicitly traced
        # alpha source is sampled; otherwise the material's Alpha CONSTANT is
        # emitted. engine <=3.5 fell back to the COLOUR image's alpha channel
        # here, which is the proxy-route twin of the two measured crop-route
        # opacity errors: constant opacity was lost, and an unused image alpha
        # became opacity nothing had asked for.
        alpha_src = atlas.maps.get("alpha")
        if alpha_src is not None:
            tree.links.new(
                source_socket(tree, alpha_src, "alpha", source_uv, images, 0),
                emission.inputs["Color"],
            )
        else:
            value = clamp(atlas.alpha_default, 0.0, 1.0)
            emission.inputs["Color"].default_value = (value, value, value, 1.0)
    else:
        src = atlas.maps.get(semantic)
        if src is not None:
            tree.links.new(
                source_socket(tree, src, semantic, source_uv, images, 0),
                emission.inputs["Color"],
            )
        else:
            value = atlas.metal_default if semantic == "metal" else atlas.rough_default
            emission.inputs["Color"].default_value = (value, value, value, 1.0)
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    activate_target()
    return material


def make_proxy_material_mra(
    atlas: AtlasSet,
    target: Any,
    source_uv: str,
    images: ProxyImageCache,
    name: str,
    needs_alpha: bool,
) -> Any:
    """v3.4: pack metal(R)/rough(G)/alpha(B) into ONE emission-probe material, so
    PROXY_BAKE needs one Cycles bake() call for these three channels instead of
    three separate ones. Safe ONLY because a proxy material's graph is always
    flat/unbranched by construction -- make_proxy_material() (and this function)
    build it directly from resolved image references or constants, never from a
    Mix/Add Shader tree, so there is no shading math whose result depends on more
    than one input at a time. This packing is NOT used for GRAPH_BAKE: a graph
    that reached GRAPH_BAKE may have Mix/Add Shader branches feeding metal/rough/
    alpha differently per branch, and Cycles only computes that blend correctly
    when it evaluates the branch as an actual shader -- collapsing three such
    branches into one CombineColor of raw values would silently drop the mix.
    """
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    set_cycles_output(output)
    output.location = (600, 0)
    target_node = tree.nodes.new("ShaderNodeTexImage")
    target_node.image = target
    target_node.label = "__RBX_BAKE_TARGET__"
    target_node.location = (-200, 400)

    combine = tree.nodes.new("ShaderNodeCombineColor")
    combine.mode = "RGB"
    combine.location = (150, 0)

    def wire(semantic: str, socket_name: str, y: float) -> None:
        if semantic == "alpha":
            alpha_src = atlas.maps.get("alpha")
            color_src = atlas.maps.get("color")
            if alpha_src is not None:
                tree.links.new(
                    source_socket(tree, alpha_src, "alpha", source_uv, images, y),
                    combine.inputs[socket_name],
                )
            elif color_src is None:
                combine.inputs[socket_name].default_value = 1.0
            else:
                alpha_ref = SourceRef(color_src.kind, color_src.key, "A")
                tree.links.new(
                    source_socket(tree, alpha_ref, "alpha", source_uv, images, y),
                    combine.inputs[socket_name],
                )
        else:
            src = atlas.maps.get(semantic)
            if src is not None:
                tree.links.new(
                    source_socket(tree, src, semantic, source_uv, images, y),
                    combine.inputs[socket_name],
                )
            else:
                value = atlas.metal_default if semantic == "metal" else atlas.rough_default
                combine.inputs[socket_name].default_value = value

    wire("metal", "Red", 300)
    wire("rough", "Green", 0)
    if needs_alpha:
        wire("alpha", "Blue", -300)
    else:
        combine.inputs["Blue"].default_value = 1.0

    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    emission.location = (350, 0)
    tree.links.new(combine.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    for node in tree.nodes:
        node.select = False
    target_node.select = True
    tree.nodes.active = target_node
    return material


def channel_stats(rgba: np.ndarray, channel: int) -> Dict[str, Any]:
    """v3.4: same fast-path-unless-non-finite policy as image_stats(), for one
    channel of an already-read-back (N, 4) pixel buffer."""
    col = rgba[:, channel]
    finite = bool(np.isfinite(col).all())
    if finite:
        return {"min": float(col.min()), "max": float(col.max()), "mean": float(col.mean()),
                "finite": True}
    return {"min": float(np.nanmin(col)), "max": float(np.nanmax(col)), "mean": float(np.nanmean(col)),
            "finite": False}


def merge_alpha_from_channel(color: Any, packed: Any, channel: int) -> bool:
    """v3.4: same contract as merge_alpha(), reading the alpha source from one
    channel of a packed multi-channel bake instead of a dedicated alpha image."""
    count = len(color.pixels)
    cbuf = np.empty(count, dtype=np.float32)
    pbuf = np.empty(len(packed.pixels), dtype=np.float32)
    color.pixels.foreach_get(cbuf)
    packed.pixels.foreach_get(pbuf)
    c = cbuf.reshape(-1, 4)
    a = pbuf.reshape(-1, 4)[:, channel]
    c[:, 3] = a
    color.pixels.foreach_set(c.ravel())
    color.update()
    return bool(np.any(a < output_alpha_cutoff()))


def ensure_active_object(obj: Any) -> None:
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.hide_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def estimate_bake_resolution(job: Job) -> int:
    # v3.4: vectorised (foreach_get + numpy), same technique used_material_slots()
    # already applied and uv_stats() already used from the start -- this was the one
    # remaining per-triangle Python loop in the smart-lineage routing engine.
    # Untouched on meshes with zero triangles or an empty sets_by_slot (falls through
    # to the same MIN_RES floor as before).
    mesh = job.source.data
    uvs = read_uvs(mesh, job.source_uv)
    mesh.calc_loop_triangles()
    ntris = len(mesh.loop_triangles)
    pixel_area = 0.0
    if ntris and job.sets_by_slot:
        loop_idx = np.empty(ntris * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", loop_idx)
        mat_idx = np.empty(ntris, dtype=np.int32)
        mesh.loop_triangles.foreach_get("material_index", mat_idx)
        tri = uvs[loop_idx].reshape(-1, 3, 2)
        ab = tri[:, 1] - tri[:, 0]
        ac = tri[:, 2] - tri[:, 0]
        uv_areas = 0.5 * np.abs(ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0])
        for slot in np.unique(mat_idx):
            atlas = job.sets_by_slot.get(int(slot))
            if atlas is None:
                continue
            w, h = source_dimensions(atlas.maps["color"])
            pixel_area += float(uv_areas[mat_idx == slot].sum()) * w * h
    raw = math.sqrt(max(pixel_area, MIN_RES * MIN_RES)) * BAKE_DENSITY_SCALE
    res = pow2_ceil(raw, MIN_RES, bake_res_ceiling())
    print("MACHINE|res route=PROXY_BAKE pixel_area=%.0f raw=%.0f ceiling=%d -> %d"
          % (pixel_area, raw, bake_res_ceiling(), res), flush=True)
    return res


def mesh_face_islands(mesh: Any) -> List[List[int]]:
    """Connected-face islands (loose parts) as lists of face indices."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    seen = [False] * len(bm.faces)
    islands: List[List[int]] = []
    for face in bm.faces:
        if seen[face.index]:
            continue
        stack = [face]
        island: List[int] = []
        while stack:
            current = stack.pop()
            if seen[current.index]:
                continue
            seen[current.index] = True
            island.append(current.index)
            for edge in current.edges:
                for linked in edge.link_faces:
                    if not seen[linked.index]:
                        stack.append(linked)
        islands.append(island)
    bm.free()
    return islands


def bbox_fill_uv(mesh: Any, packed_name: str, resolution: int) -> None:
    """Uniform-scale the packed layer's bounding box to fill 0-1."""
    packed = uv_layer_by_name(mesh, packed_name)
    if packed is None:
        raise RuntimeError("packed UV layer disappeared")
    uvs = read_uvs(mesh, packed)
    u0, v0 = (float(x) for x in uvs.min(axis=0))
    u1, v1 = (float(x) for x in uvs.max(axis=0))
    margin = BAKE_MARGIN_PX / float(resolution)
    span = max(u1 - u0, v1 - v0, 1e-9)
    scale = (1.0 - 2.0 * margin) / span
    out = np.empty_like(uvs)
    out[:, 0] = (uvs[:, 0] - u0) * scale + margin
    out[:, 1] = (uvs[:, 1] - v0) * scale + margin
    packed.data.foreach_set("uv", np.ascontiguousarray(out).ravel())


def shelf_fill_uv(mesh: Any, packed_name: str, resolution: int) -> None:
    """Shelf-pack each island's UV bbox into 0-1 without any operator context.

    Ported from bake_v3's `_shelf_fill`: real packing for scattered loose
    parts when `uv.pack_islands` is unavailable (headless/job-runner runs).
    Degenerate cases defer to `bbox_fill_uv`.
    """
    try:
        islands = mesh_face_islands(mesh)
    except Exception:
        islands = []
    if len(islands) <= 1:
        bbox_fill_uv(mesh, packed_name, resolution)
        return
    packed = uv_layer_by_name(mesh, packed_name)
    if packed is None:
        raise RuntimeError("packed UV layer disappeared")
    buf = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    packed.data.foreach_get("uv", buf)
    margin = BAKE_MARGIN_PX / float(resolution)
    boxes: List[Dict[str, Any]] = []
    for island in islands:
        loops: List[int] = []
        u0 = v0 = 1e30
        u1 = v1 = -1e30
        for face_index in island:
            polygon = mesh.polygons[face_index]
            start = polygon.loop_start
            for loop_index in range(start, start + polygon.loop_total):
                u = float(buf[2 * loop_index])
                v = float(buf[2 * loop_index + 1])
                loops.append(loop_index)
                u0 = min(u0, u)
                u1 = max(u1, u)
                v0 = min(v0, v)
                v1 = max(v1, v)
        boxes.append({
            "loops": loops, "u0": u0, "v0": v0,
            "w": max(u1 - u0, 1e-6), "h": max(v1 - v0, 1e-6),
        })
    available = max(1e-3, 1.0 - 2.0 * margin)
    order = sorted(range(len(boxes)), key=lambda i: -boxes[i]["h"])

    def place(scale: float) -> Optional[Dict[int, Tuple[float, float]]]:
        positions: Dict[int, Tuple[float, float]] = {}
        x = y = shelf_h = 0.0
        for i in order:
            w = boxes[i]["w"] * scale
            h = boxes[i]["h"] * scale
            if w > available or h > available:
                return None
            if x + w > available + 1e-9:
                y += shelf_h
                x = 0.0
                shelf_h = 0.0
            if y + h > available + 1e-9:
                return None
            positions[i] = (x, y)
            x += w
            shelf_h = max(shelf_h, h)
        return positions

    lo, hi = 0.0, 8.0
    best: Optional[Tuple[float, Dict[int, Tuple[float, float]]]] = None
    for _ in range(28):
        mid = (lo + hi) * 0.5
        positions = place(mid)
        if positions is not None:
            best = (mid, positions)
            lo = mid
        else:
            hi = mid
    if best is None:
        bbox_fill_uv(mesh, packed_name, resolution)
        return
    scale, positions = best
    for i, box in enumerate(boxes):
        px, py = positions[i]
        for loop_index in box["loops"]:
            buf[2 * loop_index] = margin + px + (buf[2 * loop_index] - box["u0"]) * scale
            buf[2 * loop_index + 1] = margin + py + (buf[2 * loop_index + 1] - box["v0"]) * scale
    packed.data.foreach_set("uv", buf)


def repack_uv(obj: Any, source_uv: str, resolution: int) -> Tuple[str, Dict[str, Any]]:
    with stage("uv.repack"):
        return _repack_uv_inner(obj, source_uv, resolution)


def _repack_uv_inner(obj: Any, source_uv: str, resolution: int) -> Tuple[str, Dict[str, Any]]:
    mesh = obj.data
    source = uv_layer_by_name(mesh, source_uv)
    if source is None:
        raise RuntimeError("source UV missing on duplicate")
    mesh.uv_layers.active = source
    packed = mesh.uv_layers.new(name="RBX_PACKED", do_init=True)
    if packed is None:
        raise RuntimeError("could not allocate destination UV layer")
    # Edit Mode/UV operators can reallocate Mesh CustomData and invalidate RNA
    # layer handles.  Freeze the Python string before entering Edit Mode and
    # reacquire the active layer afterward.  Separately, all name lookup in this
    # module is index-based so a different malformed layer name cannot poison a
    # bpy_prop_collection.get() scan.
    packed_name = safe_text(packed.name, "RBX_PACKED")
    if not packed_name:
        raise RuntimeError("destination UV layer has no readable name")
    # Adding a layer can itself reallocate CustomData, so reacquire source too.
    source_live = uv_layer_by_name(mesh, source_uv)
    if source_live is None:
        raise RuntimeError("source UV lost while allocating packed layer")
    source_live.active_render = True
    mesh.uv_layers.active = packed
    ensure_active_object(obj)
    settings = bpy.context.tool_settings
    old_sync = settings.use_uv_select_sync
    pack_error: Optional[str] = None
    try:
        settings.use_uv_select_sync = True
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action="SELECT")
        try:
            bpy.ops.uv.pack_islands(
                rotate=True,
                margin_method="ADD",
                margin=BAKE_MARGIN_PX / float(resolution),
            )
        except Exception as exc:
            pack_error = safe_text(exc)
    finally:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode="OBJECT")
        settings.use_uv_select_sync = old_sync
    if pack_error is not None:
        # Headless/job-runner sessions can lack the operator context that
        # uv.pack_islands needs.  Fall back to a real shelf pack of island
        # bounding boxes (bake_v3 parity) so PROXY/GRAPH routes still produce
        # output instead of failing the whole object.
        mesh = obj.data
        try:
            shelf_fill_uv(mesh, packed_name, resolution)
            print("    UV pack: shelf-fill fallback (pack_islands: %s)" % pack_error)
        except Exception as exc:
            bbox_fill_uv(mesh, packed_name, resolution)
            print("    UV pack: bbox fallback (%s)" % safe_text(exc))
    packed_live = mesh.uv_layers.active
    if packed_live is None:
        raise RuntimeError("packed UV layer lost after island packing")
    stats = uv_stats(mesh, packed_live)
    return packed_name, {
        "packed_fill": stats.fill_ratio,
        "packed_min_triangle_px2": stats.min_face_area * resolution * resolution,
    }


def assign_proxy_slots(obj: Any, proxies: Dict[int, Any], fallback: Any) -> None:
    mesh = obj.data
    for index in range(len(mesh.materials)):
        mesh.materials[index] = proxies.get(index, fallback)


def high_precision_output() -> bool:
    return output_contract.build_output_contract(OUTPUT_PROFILE)["profile"] == "PBR_HIGH_PRECISION"


def output_alpha_cutoff() -> float:
    """Keep every opacity difference representable in the selected format."""
    return 1.0 - 0.5 / 65535.0 if high_precision_output() else 0.999


def require_output_profile_route(route: str) -> None:
    contract = output_contract.build_output_contract(OUTPUT_PROFILE)
    result = output_contract.assess_output_feasibility(contract, {
        "route": route, "float_bake_buffer": high_precision_output(), "png16_writer": True,
    })
    if not result["ok"]:
        raise RuntimeError("Output contract cannot be delivered by %s: %s" % (route, result))


def write_output_pixels(path: str, pixels: np.ndarray) -> None:
    """Pixels already have the declared transfer function; never encode twice."""
    if high_precision_output():
        output_contract.write_png16(path, pixels, compression=PNG_COMPRESS_LEVEL)
    else:
        write_png(path, pixels)


def new_bake_image(name: str, resolution: int, semantic: str) -> Any:
    high = high_precision_output() or semantic == "emission"
    image = bpy.data.images.new(
        name, width=resolution, height=resolution,
        alpha=(semantic in {"color", "alpha"}), float_buffer=high,
    )
    # Float targets retain raw scene-linear EMIT values. The output writer
    # applies sRGB only to base-color RGB; data channels and alpha stay linear.
    image.colorspace_settings.name = "sRGB" if semantic == "color" and not high else "Non-Color"
    image["import_glue_semantic"] = semantic
    return image


def configure_bake(resolution: int) -> None:
    scene = bpy.context.scene
    scene.render.bake.use_clear = True
    scene.render.bake.use_selected_to_active = False
    scene.render.bake.margin = min(BAKE_MARGIN_PX, max(1, resolution // 8))
    try:
        scene.render.bake.margin_type = "EXTEND"
    except Exception:
        pass
    try:
        scene.render.bake.normal_space = "TANGENT"
        scene.render.bake.normal_r = "POS_X"
        scene.render.bake.normal_g = "POS_Y"
        scene.render.bake.normal_b = "POS_Z"
    except Exception:
        pass


def save_blender_image(image: Any, path: str) -> None:
    with stage("bake.save_image"):
        return _save_blender_image_inner(image, path)


def _save_blender_image_inner(image: Any, path: str) -> None:
    # v3.4: writes via write_png() (this file's own stdlib PNG writer, already used by
    # the CROP route and the MRA-packed metal/rough channels) instead of Blender's own
    # image.save(). Two reasons, found in that order:
    #  1. image.save() has no exposed PNG compression control (effective zlib level
    #     ~1 on this Blender build) -- write_png() honours PNG_COMPRESS_LEVEL.
    #  2. A first attempt used image.save_render() (which DOES expose compression via
    #     scene.render.image_settings) and it silently corrupted every ColorMap: this
    #     scene's view_transform is AgX (Blender's own default since 4.x), and
    #     save_render() applies the scene's full colour-management view transform,
    #     while plain save() does not. Caught by an A/B pixel diff against baseline
    #     before shipping (measured max channel diff 91/255) -- image.pixels via
    #     foreach_get() is unaffected by the view transform (already proven bit-exact
    #     via the MRA metal/rough/alpha channels, which use the same technique), so
    #     reading pixels directly and writing them with write_png() is both correct
    #     and avoids Blender's own encoder entirely.
    buf = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(buf)
    h, w = image.size[1], image.size[0]
    rgba = buf.reshape(h, w, 4)
    if high_precision_output():
        if not image.is_float:
            raise RuntimeError("PBR_HIGH_PRECISION refuses an 8-bit bake buffer")
        # Blender's native normal_compress() adds a 1e-5 bias to encoded
        # normal RGB (render/intern/bake.cc), so a flat normal can be
        # 1.00001001358. Only clamp native normal endpoints, within one
        # UNORM16 code step; alpha, color and scalars retain strict bounds.
        # Do not subtract the bias from all pixels: cleared background/margin
        # values do not necessarily carry it. Record every such adjustment.
        if image.get("import_glue_semantic") == "normal" and np.isfinite(rgba).all():
            rgb = rgba[..., :3]
            minimum, maximum = float(rgb.min()), float(rgb.max())
            tolerance = 1.0 / 65535.0
            if minimum >= -tolerance and maximum <= 1.0 + tolerance \
                    and (minimum < 0.0 or maximum > 1.0):
                adjustment = {
                    "kind": "native_normal_endpoint_clamp", "map": os.path.basename(path),
                    "minimum_before": minimum, "maximum_before": maximum,
                    "clamped_components": int(np.count_nonzero((rgb < 0.0) | (rgb > 1.0))),
                    "maximum_correction": max(0.0, -minimum, maximum - 1.0),
                    "allowed_endpoint_excursion": tolerance,
                }
                np.clip(rgb, 0.0, 1.0, out=rgb)
                _NORMAL_ENCODING_ADJUSTMENTS.append(adjustment)
                _run_event(**adjustment)
        if not np.isfinite(rgba).all() or np.any(rgba < 0) or np.any(rgba > 1):
            raise RuntimeError("PNG16 requires finite normalized samples; it is not an HDR format "
                               "(semantic=%s, min=%.12g, max=%.12g, finite=%s)" %
                               (image.get("import_glue_semantic"), float(np.nanmin(rgba)),
                                float(np.nanmax(rgba)), bool(np.isfinite(rgba).all())))
        if image.get("import_glue_semantic") == "color":
            rgba[..., :3] = linear_to_srgb(rgba[..., :3])
    has_alpha = bool(np.any(rgba[..., 3] < output_alpha_cutoff()))
    write_output_pixels(path, rgba if has_alpha else rgba[..., :3])


def image_stats(image: Any) -> Dict[str, Any]:
    with stage("bake.image_stats"):
        return _image_stats_inner(image)


def _image_stats_inner(image: Any) -> Dict[str, Any]:
    # v3.4: a freshly-baked EMIT/NORMAL image (samples=1, no denoiser) is essentially
    # always finite -- the nan-safe reductions were paying to defend against a case
    # that practically never happens, on every single bake pass. Measured cost on a
    # real 6-object GRAPH_BAKE run: 46s of a 251s total wall time (18%), almost all of
    # it np.nanmin/nanmax/nanmean's _replace_nan copying the whole array to mask NaNs.
    # Check finiteness ONCE (already needed for the "finite" field either way) and use
    # the plain fast reducers in the common case; only pay for nan-safety when there
    # actually is something non-finite to be safe about.
    buf = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(buf)
    rgb = buf.reshape(-1, 4)[:, :3]
    finite = bool(np.isfinite(rgb).all())
    if finite:
        mn, mx, mean = float(rgb.min()), float(rgb.max()), float(rgb.mean())
        mean_rgb = [float(x) for x in rgb.mean(axis=0)]
    else:
        mn, mx, mean = float(np.nanmin(rgb)), float(np.nanmax(rgb)), float(np.nanmean(rgb))
        mean_rgb = [float(x) for x in np.nanmean(rgb, axis=0)]
    return {
        "min": mn, "max": mx, "mean": mean,
        "mean_rgb": mean_rgb,   # v3.3: lets the census see magenta
        "finite": finite,
    }


def bake_pass(
    job: Job, obj: Any, semantic: str, resolution: int,
    source_uv: str, images: ProxyImageCache,
) -> Tuple[Any, Dict[str, Any], List[Any]]:
    _run_boundary("pass", object=datablock_name(job.source), route=job.route,
                  semantic=semantic, resolution=resolution)
    target = new_bake_image("__RBX_BAKE_%s_%s" % (job.file_base, semantic), resolution, semantic)
    proxies: Dict[int, Any] = {}
    created: List[Any] = []
    try:
        for slot in range(len(obj.data.materials)):
            atlas = job.sets_by_slot.get(slot)
            if atlas is None:
                atlas = next(iter(job.sets_by_slot.values()))
            proxy = make_proxy_material(
                atlas, semantic, target, source_uv, images,
                "__RBX_PROXY_%s_%s_%d" % (job.file_base, semantic, slot),
            )
            proxies[slot] = proxy
            created.append(proxy)
        fallback = next(iter(proxies.values()))
        assign_proxy_slots(obj, proxies, fallback)
        ensure_active_object(obj)
        configure_bake(resolution)
        bake_type = "NORMAL" if semantic == "normal" else "EMIT"
        bpy.context.scene.cycles.bake_type = bake_type
        with stage("bake.cycles.proxy"):
            cycles_bake(bake_type, resolution)
        return target, image_stats(target), created
    except BaseException:
        obj.data.materials.clear()
        if target.name in bpy.data.images:
            bpy.data.images.remove(target)
        for material in created:
            if material.name in bpy.data.materials and material.users == 0:
                bpy.data.materials.remove(material)
        raise


def bake_pass_mra(
    job: Job, obj: Any, resolution: int,
    source_uv: str, images: ProxyImageCache, needs_alpha: bool,
) -> Tuple[Any, List[Any]]:
    """v3.4: one Cycles bake() call for metal+rough+alpha instead of up to three.
    See make_proxy_material_mra() for why this is safe for PROXY_BAKE specifically."""
    _run_boundary("pass", object=datablock_name(job.source), route=job.route,
                  semantic="mra", resolution=resolution)
    target = new_bake_image("__RBX_BAKE_%s_MRA" % job.file_base, resolution, "metal")
    proxies: Dict[int, Any] = {}
    created: List[Any] = []
    try:
        for slot in range(len(obj.data.materials)):
            atlas = job.sets_by_slot.get(slot)
            if atlas is None:
                atlas = next(iter(job.sets_by_slot.values()))
            proxy = make_proxy_material_mra(
                atlas, target, source_uv, images,
                "__RBX_PROXY_%s_MRA_%d" % (job.file_base, slot), needs_alpha,
            )
            proxies[slot] = proxy
            created.append(proxy)
        fallback = next(iter(proxies.values()))
        assign_proxy_slots(obj, proxies, fallback)
        ensure_active_object(obj)
        configure_bake(resolution)
        bpy.context.scene.cycles.bake_type = "EMIT"
        with stage("bake.cycles.proxy_mra"):
            cycles_bake("EMIT", resolution)
        return target, created
    except BaseException:
        obj.data.materials.clear()
        if target.name in bpy.data.images:
            bpy.data.images.remove(target)
        for material in created:
            if material.name in bpy.data.materials and material.users == 0:
                bpy.data.materials.remove(material)
        raise


def merge_alpha(color: Any, alpha: Any) -> bool:
    count = len(color.pixels)
    cbuf = np.empty(count, dtype=np.float32)
    abuf = np.empty(count, dtype=np.float32)
    color.pixels.foreach_get(cbuf)
    alpha.pixels.foreach_get(abuf)
    c = cbuf.reshape(-1, 4)
    a = abuf.reshape(-1, 4)[:, 0]
    c[:, 3] = a
    color.pixels.foreach_set(c.ravel())
    color.update()
    return bool(np.any(a < output_alpha_cutoff()))


def material_requests_alpha(material: Any) -> bool:
    if material is None:
        return False
    if DECAL_HINT in datablock_name(material, "").lower():
        return True
    tree = getattr(material, "node_tree", None)
    if not getattr(material, "use_nodes", False) or tree is None:
        return float(material.diffuse_color[3]) < output_alpha_cutoff()
    output = cycles_output_node(tree)
    if output is None or not output.inputs["Surface"].is_linked:
        return float(material.diffuse_color[3]) < output_alpha_cutoff()
    # Blender 5.2 exposes legacy blend_method=HASHED even for opaque shaders.
    # Opacity must come from the evaluated closure, not that compatibility flag.
    for inner_tree in iter_node_trees(tree):
        for node in inner_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                alpha = node.inputs.get("Alpha")
                if alpha is not None:
                    if alpha.is_linked:
                        return True
                    try:
                        if float(alpha.default_value) < output_alpha_cutoff():
                            return True
                    except Exception:
                        pass
            if node.type in {"BSDF_TRANSPARENT", "HOLDOUT"}:
                return True
    return False


def job_requests_alpha(job: Job) -> bool:
    if any("alpha" in atlas.maps for atlas in job.sets_by_slot.values()):
        return True
    return any(
        slot < len(job.source.material_slots)
        and material_requests_alpha(job.source.material_slots[slot].material)
        for slot in job.used_slots
    )


def process_bake(job: Job, duplicate: Any, folder: str) -> Dict[str, Any]:
    with stage("route.proxy_bake"):
        require_output_profile_route("PROXY_BAKE")
        return run_bake_route(_process_bake_inner, job, duplicate, folder)


def _process_bake_inner(job: Job, duplicate: Any, folder: str) -> Dict[str, Any]:
    resolution = estimate_bake_resolution(job)
    packed_name, packed_report = repack_uv(duplicate, job.source_uv, resolution)
    require_output_uv(duplicate, "PROXY_BAKE", packed_name)
    images = ProxyImageCache()
    result: Dict[str, Any] = {
        "resolution": resolution, "maps": {}, **packed_report,
    }
    all_materials: List[Any] = []
    live_images: List[Any] = []
    alpha_used = False
    needs_alpha = job_requests_alpha(job)
    try:
        color_img, stats, mats = bake_pass(
            job, duplicate, "color", resolution, job.source_uv, images
        )
        live_images.append(color_img)
        all_materials.extend(mats)
        if stats["max"] < 1e-4:
            job.warnings.append("ColorMap bake is numerically all black")

        if PACK_MRA_PROXY_BAKE:
            # v3.4: metal+rough+(alpha) in ONE Cycles bake() call instead of up to
            # three. Output contract is unchanged -- still 4 separate PNG files on
            # disk (_ALB/_MET/_RGH/_NOR) -- this only changes how many times Cycles
            # resets/bakes/reads back internally. See make_proxy_material_mra().
            mra_img, mats = bake_pass_mra(
                job, duplicate, resolution, job.source_uv, images, needs_alpha
            )
            live_images.append(mra_img)
            all_materials.extend(mats)
            mra_buf = np.empty(len(mra_img.pixels), dtype=np.float32)
            mra_img.pixels.foreach_get(mra_buf)
            mra_rgba = mra_buf.reshape(-1, 4)
            if needs_alpha:
                alpha_used = merge_alpha_from_channel(color_img, mra_img, 2)
                result["alpha_stats"] = channel_stats(mra_rgba, 2)
            save_blender_image(color_img, output_path(folder, job.file_base, "color"))
            result["maps"]["color"] = {"size": [resolution, resolution], "stats": stats}
            bpy.data.images.remove(color_img)
            live_images.remove(color_img)

            # write_png() itself flips bottom-up Blender pixel order to top-down PNG
            # row order internally -- pass it the raw (unflipped) buffer, matching
            # every other write_png() caller in this file (e.g. process_crop()).
            h = w = resolution
            metal_plane = mra_rgba[:, 0].reshape(h, w)
            rough_plane = mra_rgba[:, 1].reshape(h, w)
            write_png(output_path(folder, job.file_base, "metal"), metal_plane)
            write_png(output_path(folder, job.file_base, "rough"), rough_plane)
            result["maps"]["metal"] = {"size": [resolution, resolution], "stats": channel_stats(mra_rgba, 0)}
            result["maps"]["rough"] = {"size": [resolution, resolution], "stats": channel_stats(mra_rgba, 1)}
            bpy.data.images.remove(mra_img)
            live_images.remove(mra_img)

            image, stats, mats = bake_pass(
                job, duplicate, "normal", resolution, job.source_uv, images
            )
            live_images.append(image)
            all_materials.extend(mats)
            save_blender_image(image, output_path(folder, job.file_base, "normal"))
            result["maps"]["normal"] = {"size": [resolution, resolution], "stats": stats}
            if stats["max"] < 0.25:
                job.warnings.append("normal bake appears blank")
            bpy.data.images.remove(image)
            live_images.remove(image)
        else:
            if needs_alpha:
                alpha_img, alpha_stats, mats = bake_pass(
                    job, duplicate, "alpha", resolution, job.source_uv, images
                )
                live_images.append(alpha_img)
                all_materials.extend(mats)
                merge_alpha(color_img, alpha_img)
                alpha_used = True
                result["alpha_stats"] = alpha_stats
            save_blender_image(color_img, output_path(folder, job.file_base, "color"))
            result["maps"]["color"] = {"size": [resolution, resolution], "stats": stats}
            bpy.data.images.remove(color_img)
            live_images.remove(color_img)
            if needs_alpha:
                bpy.data.images.remove(alpha_img)
                live_images.remove(alpha_img)

            for semantic in ("metal", "rough", "normal"):
                image, stats, mats = bake_pass(
                    job, duplicate, semantic, resolution, job.source_uv, images
                )
                live_images.append(image)
                all_materials.extend(mats)
                save_blender_image(image, output_path(folder, job.file_base, semantic))
                result["maps"][semantic] = {
                    "size": [resolution, resolution], "stats": stats,
                }
                if semantic == "normal" and stats["max"] < 0.25:
                    job.warnings.append("normal bake appears blank")
                bpy.data.images.remove(image)
                live_images.remove(image)
    finally:
        for image in live_images:
            if image.name in bpy.data.images:
                bpy.data.images.remove(image)
        images.close()
        # Slot references are cleared by the final preview material below; any
        # now-unused proxy datablocks can then be removed.
        duplicate.data.materials.clear()
        for material in all_materials:
            if material.name in bpy.data.materials and material.users == 0:
                bpy.data.materials.remove(material)

    finalize_single_uv(duplicate.data, packed_name)
    result["alpha"] = alpha_used
    return result


# ---------------------- original material graph bake -------------------------

_STRUCTURAL_SHADER_NODES = {
    "MIX_SHADER", "ADD_SHADER", "GROUP", "GROUP_INPUT", "GROUP_OUTPUT",
    "OUTPUT_MATERIAL", "OUTPUT_WORLD", "OUTPUT_LIGHT",
}


def clone_nested_groups(tree: Any, created: List[Any], memo: Dict[int, Any]) -> None:
    """Make every nested shader group private before semantic instrumentation."""
    for node in list(tree.nodes):
        if getattr(node, "node_tree", None) is None:
            continue
        original = node.node_tree
        key = original.as_pointer()
        clone = memo.get(key)
        if clone is None:
            clone = original.copy()
            clone.name = "__RBX_GRAPH_%s" % datablock_name(original, "group")
            # ID.copy() preserves use_fake_user; a fake user would keep the
            # private clone alive forever and defeat users==0 cleanup.
            try:
                clone.use_fake_user = False
            except Exception:
                pass
            memo[key] = clone
            created.append(clone)
            clone_nested_groups(clone, created, memo)
        node.node_tree = clone


def remove_group_clones(groups: List[Any]) -> None:
    """Remove private group copies once nothing references them.

    Clones are created parents-first, but a child shared by two parents can
    sit between them in creation order, so no single linear order is safe:
    a child checked while any parent clone is alive reports users > 0 and
    would leak.  Sweep until a full pass removes nothing; every parent
    removal releases its children for the next pass.
    """
    pending = list(groups)
    while pending:
        remaining: List[Any] = []
        removed_any = False
        for group in pending:
            try:
                if group.name not in bpy.data.node_groups:
                    continue
                if group.users == 0:
                    bpy.data.node_groups.remove(group)
                    removed_any = True
                else:
                    remaining.append(group)
            except Exception:
                continue
        if not removed_any:
            break
        pending = remaining


def iter_node_trees(tree: Any, seen: Optional[Set[int]] = None) -> Iterable[Any]:
    if seen is None:
        seen = set()
    key = tree.as_pointer()
    if key in seen:
        return
    seen.add(key)
    yield tree
    for node in tree.nodes:
        if getattr(node, "node_tree", None) is not None:
            yield from iter_node_trees(node.node_tree, seen)


def shader_output(node: Any) -> Optional[Any]:
    return next((socket for socket in node.outputs if socket.type == "SHADER"), None)


# v3.4: closures with no Metallic socket that are still the specular-reflector-style
# closure, not a dielectric one -- brushed metal is the canonical Anisotropic use case,
# and a Glossy BSDF is a bare specular reflector (used directly by artists for chrome /
# mirror-like surfaces without going through Principled at all).
_SPECULAR_METAL_LIKE_NODE_TYPES = {"BSDF_GLOSSY", "BSDF_ANISOTROPIC"}


def semantic_input(node: Any, semantic: str) -> Tuple[Optional[Any], float]:
    """Return a data socket and scalar fallback for a terminal surface shader."""
    ntype = node.type
    if semantic == "alpha":
        if ntype in {"BSDF_TRANSPARENT", "HOLDOUT"}:
            return None, 0.0
        if ntype == "BSDF_PRINCIPLED":
            return node.inputs.get("Alpha"), 1.0
        return None, 1.0
    if semantic == "metal":
        if ntype == "BSDF_PRINCIPLED":
            return node.inputs.get("Metallic"), DEFAULT_METAL
        # v3.4: every non-Principled closure used to bake metal=0.0 regardless of type.
        # Cycles has no generic "metalness" input outside Principled, but its other
        # closures aren't all dielectric either -- Glossy/Anisotropic ARE the specular-
        # reflector closures (brushed-metal materials are the canonical Anisotropic use
        # case), so defaulting them to 0 silently produced a non-metal bake for content
        # that is visually metallic. Confirmed present on real content: BSDF_GLOSSY
        # appears in 4/15 surveyed production scenes.
        return None, (1.0 if ntype in _SPECULAR_METAL_LIKE_NODE_TYPES else DEFAULT_METAL)
    if semantic == "rough":
        socket = node.inputs.get("Roughness")
        return socket, DEFAULT_ROUGH
    # Albedo/color: cover common Cycles surface closures.  Existing Emission
    # nodes are sampled from Color, which also rescues emissive decal sheets.
    for name in ("Base Color", "Color"):
        socket = node.inputs.get(name)
        if socket is not None:
            return socket, 0.8
    if ntype in {"BSDF_TRANSPARENT", "HOLDOUT"}:
        return None, 0.0
    return None, 0.8


def set_emission_color(tree: Any, emission: Any, source: Optional[Any], fallback: float) -> None:
    target = emission.inputs.get("Color")
    if source is not None and source.is_linked:
        tree.links.new(source.links[0].from_socket, target)
        return
    value: Any = fallback if source is None else source.default_value
    try:
        if isinstance(value, (int, float)):
            gray = float(value)
            target.default_value = (gray, gray, gray, 1.0)
        else:
            seq = tuple(float(x) for x in value)
            if len(seq) >= 4:
                target.default_value = seq[:4]
            elif len(seq) == 3:
                target.default_value = seq + (1.0,)
            else:
                gray = float(seq[0]) if seq else fallback
                target.default_value = (gray, gray, gray, 1.0)
    except Exception:
        target.default_value = (fallback, fallback, fallback, 1.0)


def set_scalar_probe_input(tree: Any, target: Any, source: Optional[Any], fallback: float) -> None:
    """Retain the original Float socket's implicit conversion in every probe."""
    if source is not None and source.is_linked:
        tree.links.new(source.links[0].from_socket, target)
    else:
        target.default_value = float(source.default_value) if source is not None else fallback


def instrument_graph_semantic(root_tree: Any, semantic: str) -> int:
    """Replace terminal closures with emission probes while retaining mixes."""
    replaced = 0
    for tree in iter_node_trees(root_tree):
        for node in list(tree.nodes):
            if node.type in _STRUCTURAL_SHADER_NODES or "VOLUME" in node.type \
                    or getattr(node, "node_tree", None) is not None:
                continue
            out = shader_output(node)
            if out is None or not out.is_linked:
                continue
            outgoing = list(out.links)
            if not outgoing:
                continue
            emission = tree.nodes.new("ShaderNodeEmission")
            emission.name = "__RBX_%s_%d" % (semantic.upper(), replaced)
            emission.label = "__RBX_GRAPH_PROBE__"
            emission.location = (node.location.x + 180.0, node.location.y)
            emission.inputs["Strength"].default_value = 1.0
            if semantic == "emission":
                # Evaluate authored radiance before reducing it to Roblox's
                # albedo-modulated grayscale mask. Preserve Color/Float casts.
                color_name = "Emission Color" if node.type == "BSDF_PRINCIPLED" else "Color"
                strength_name = "Emission Strength" if node.type == "BSDF_PRINCIPLED" else "Strength"
                if node.type in {"BSDF_PRINCIPLED", "EMISSION"}:
                    set_emission_color(tree, emission, node.inputs.get(color_name), 0.0)
                    set_scalar_probe_input(tree, emission.inputs["Strength"],
                                           node.inputs.get(strength_name), 1.0)
                else:
                    emission.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
            elif semantic == "mra":
                # Float inputs preserve Blender's implicit color-to-scalar
                # conversion. Mix/Add Shader then applies the same arithmetic
                # independently to every RGB component of the emission.
                combine = tree.nodes.new("ShaderNodeCombineXYZ")
                for index, component in enumerate(("metal", "rough", "alpha")):
                    source, fallback = semantic_input(node, component)
                    set_scalar_probe_input(tree, combine.inputs[index], source, fallback)
                tree.links.new(combine.outputs[0], emission.inputs["Color"])
            elif semantic in {"metal", "rough", "alpha"}:
                source, fallback = semantic_input(node, semantic)
                scalar = tree.nodes.new("ShaderNodeMath")
                scalar.operation = "ADD"
                scalar.inputs[1].default_value = 0.0
                set_scalar_probe_input(tree, scalar.inputs[0], source, fallback)
                tree.links.new(scalar.outputs[0], emission.inputs["Color"])
            else:
                source, fallback = semantic_input(node, semantic)
                set_emission_color(tree, emission, source, fallback)
            for link in outgoing:
                destination = link.to_socket
                tree.links.remove(link)
                tree.links.new(emission.outputs["Emission"], destination)
            replaced += 1
    return replaced


def ensure_graph_surface(tree: Any, semantic: str, original: Optional[Any] = None) -> bool:
    """Build a fallback surface when the tree has none linked yet.

    v3.4: `original` lets a use_nodes=False material's real "Viewport Display"
    color/metallic/roughness drive the fallback instead of the generic engine
    defaults. Before this fix a plain non-node material -- the SIMPLEST
    possible material a user can make -- baked a flat 0.8 grey ColorMap no
    matter what colour was actually set (confirmed: a material with
    diffuse_color=(0.9,0.05,0.05) baked as (0.8,0.8,0.8)). Node-based
    materials are unaffected: for them `surface.is_linked` is already True
    and this function returns before reaching the fallback branch at all.
    """
    output = cycles_output_node(tree)
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        set_cycles_output(output)
    surface = output.inputs.get("Surface")
    if surface is not None and surface.is_linked:
        return False
    if semantic == "normal":
        node = tree.nodes.new("ShaderNodeBsdfPrincipled")
        tree.links.new(node.outputs["BSDF"], surface)
    else:
        node = tree.nodes.new("ShaderNodeEmission")
        diffuse = getattr(original, "diffuse_color", None) if original is not None else None
        if semantic == "mra":
            rgba = (float(getattr(original, "metallic", DEFAULT_METAL)),
                    float(getattr(original, "roughness", DEFAULT_ROUGH)),
                    float(diffuse[3]) if diffuse is not None and len(diffuse) >= 4 else 1.0, 1.0)
        elif semantic == "color":
            rgba = tuple(diffuse)[:4] if diffuse is not None else (0.8, 0.8, 0.8, 1.0)
            if len(rgba) == 3:
                rgba = rgba + (1.0,)
        elif semantic == "emission":
            rgba = (0.0, 0.0, 0.0, 1.0)
        elif semantic == "alpha":
            a = float(diffuse[3]) if diffuse is not None and len(diffuse) >= 4 else 1.0
            rgba = (a, a, a, 1.0)
        elif semantic == "metal":
            m = float(getattr(original, "metallic", DEFAULT_METAL)) if original is not None else DEFAULT_METAL
            rgba = (m, m, m, 1.0)
        else:  # rough
            r = float(getattr(original, "roughness", DEFAULT_ROUGH)) if original is not None else DEFAULT_ROUGH
            rgba = (r, r, r, 1.0)
        node.inputs["Color"].default_value = rgba
        tree.links.new(node.outputs["Emission"], surface)
    return True


def make_graph_bake_material(
    original: Optional[Any], semantic: str, target: Any, name: str,
) -> Tuple[Any, List[Any], int]:
    groups: List[Any] = []
    material = None
    try:
        if original is not None and getattr(original, "use_nodes", False) \
                and getattr(original, "node_tree", None) is not None:
            material = original.copy()
            material.name = name
            try:
                material.use_fake_user = False
            except Exception:
                pass
            tree = material.node_tree
            clone_nested_groups(tree, groups, {})
        else:
            material = bpy.data.materials.new(name)
            material.use_nodes = True
            tree = material.node_tree
            tree.nodes.clear()
        fallback = ensure_graph_surface(tree, semantic, original)
        probes = 0 if semantic == "normal" or fallback else instrument_graph_semantic(tree, semantic)
        target_node = tree.nodes.new("ShaderNodeTexImage")
        target_node.image = target
        target_node.label = "__RBX_BAKE_TARGET__"
        for node in tree.nodes:
            node.select = False
        target_node.select = True
        tree.nodes.active = target_node
        return material, groups, probes
    except BaseException:
        if material is not None and material.users == 0:
            bpy.data.materials.remove(material)
        remove_group_clones(groups)
        raise


def bake_res_ceiling() -> int:
    """RES is documented as the resolution CEILING for bakes; MAX_RES is the absolute (crop) ceiling.
    Before 2026-08-23 only piece_res() (v3 lineage) honoured RES; graph/proxy bakes silently went to MAX_RES."""
    try:
        return int(max(MIN_RES, min(MAX_RES, RES)))
    except Exception:
        return MAX_RES


def estimate_graph_bake_resolution(job: Job) -> int:
    # Unresolved materials have no trustworthy atlas dimensions.  Preserve a
    # configurable source-UV density and clamp to the configured bake ceiling.
    raw = math.sqrt(max(job.uv.triangle_area, 1e-12)) * GRAPH_TEXELS_FULL_UV
    raw *= BAKE_DENSITY_SCALE
    res = pow2_ceil(raw, MIN_RES, bake_res_ceiling())
    print("MACHINE|res route=GRAPH_BAKE uv_area=%.4f texels_full_uv=%d raw=%.0f ceiling=%d -> %d"
          % (job.uv.triangle_area, GRAPH_TEXELS_FULL_UV, raw, bake_res_ceiling(), res), flush=True)
    return res


def job_dependency_materials(job: Job) -> List[Any]:
    """Materials a job's dependency gate must check: its planned slots plus every
    material only the source's EVALUATED mesh uses.

    M1 final review finding 1: plan_jobs reads base-mesh slots, so a material a
    modifier puts on faces (Solidify material offset, Geometry Nodes Set
    Material) never reached this gate.  The capability policy defers to this
    gate for dependency codes, so it must see the same materials.
    """
    result: List[Any] = []
    seen: Set[int] = set()
    for material in list(job.materials_by_slot.values()) + used_materials([job.source]):
        if material is not None and material.as_pointer() not in seen:
            seen.add(material.as_pointer())
            result.append(material)
    return result


def require_material_dependencies(materials: Iterable[Any]) -> Dict[str, Any]:
    """Fail closed for required unsupported nodes and object-copy context."""
    diagnostics: List[Dict[str, Any]] = []
    images: Set[int] = set()
    for material in materials:
        if material is None:
            continue
        dependency = material_dependencies(material)
        images.update(dependency["images"])
        for item in dependency["diagnostics"]:
            diagnostics.append({"material": material.name, **item})
    blocking = [item for item in diagnostics if item["severity"] == "ERROR"
                or item["code"] == "OBJECT_RANDOM_CONTEXT"]
    # Precheck may be disabled in headless runs; do not let required missing
    # file textures turn into a successful magenta bake.
    from .precheck import _image_paths, file_looks_valid
    for image in bpy.data.images:
        if image.as_pointer() not in images or image.packed_file:
            continue
        if image.source in {"FILE", "TILED", "SEQUENCE", "MOVIE"}:
            for _authored, path, _tile in _image_paths(image):
                valid, reason = file_looks_valid(path)
                if not valid:
                    blocking.append({"code": "REQUIRED_IMAGE_INVALID", "message":
                                     "%s: %s (%s)" % (image.name, reason, path)})
    if blocking:
        messages = []
        for item in blocking:
            context = "; ".join("%s=%s" % (key, item[key])
                                for key in ("node", "node_instance") if item.get(key))
            messages.append("%s%s: %s" % (
                item["code"], " [%s]" % context if context else "", item["message"]))
        raise RuntimeError("Required shader dependencies: " + "; ".join(messages))
    return {"ok": True, "diagnostics": diagnostics, "required_images": len(images)}


def require_output_uv(obj: Any, route: str, layer_name: Optional[str] = None) -> Dict[str, Any]:
    result = validate_uv(obj, layer_name=layer_name,
                         require_unique=route not in {"CROP", "CROP_SPLIT"})
    if not result["ok"]:
        raise RuntimeError("Output UV validation failed: " + "; ".join(
            "%s: %s" % (item["code"], item["message"]) for item in result["diagnostics"]))
    return result


_BAKE_EVENTS: List[Dict[str, Any]] = []


def cycles_bake(bake_type: str, resolution: int) -> None:
    """Retry one recognized GPU resource failure on CPU, without resizing.

    The CPU stays selected for the remaining passes of this object; the route
    wrapper restores the caller's device after recording all attempts.
    """
    scene = bpy.context.scene
    original_error = None
    for attempt in range(2):
        _run_boundary("native_bake", bake_type=bake_type, resolution=resolution,
                      attempt=attempt + 1, device=scene.cycles.device)
        event = {"type": bake_type, "resolution": resolution,
                 "device": scene.cycles.device, "status": "FAILED"}
        _BAKE_EVENTS.append(event)
        try:
            status = bpy.ops.object.bake(type=bake_type)
            if "FINISHED" not in status:
                raise RuntimeError("Cycles bake returned %r" % status)
            event["status"] = "OK"
            _run_boundary("native_bake_complete", bake_type=bake_type,
                          resolution=resolution, attempt=attempt + 1)
            return
        except Exception as exc:
            event["error"] = safe_text(exc)
            message = safe_text(exc).lower()
            resource_error = any(token in message for token in (
                "out of memory", "failed to allocate", "cuda error", "optix error",
                "hip error", "device lost", "device removed", "device unavailable",
                "failed to create cuda", "failed to create optix", "illegal address"))
            input_error = any(token in message for token in (
                "shader", "compilation", "missing", "texture", "uv", "node"))
            if attempt == 0 and event["device"] == "GPU" and ALLOW_CPU_FALLBACK \
                    and resource_error and not input_error:
                original_error = safe_text(exc)
                event["retry"] = "CPU_SAME_RESOLUTION"
                scene.cycles.device = "CPU"
                continue
            if original_error:
                raise RuntimeError("GPU bake failed (%s); same-resolution CPU retry failed (%s)"
                                   % (original_error, safe_text(exc))) from exc
            raise


def run_bake_route(operation: Any, job: Job, duplicate: Any, folder: str) -> Dict[str, Any]:
    dependency = require_material_dependencies(job_dependency_materials(job))
    device = bpy.context.scene.cycles.device
    start = len(_BAKE_EVENTS)
    normal_adjustment_start = len(_NORMAL_ENCODING_ADJUSTMENTS)
    try:
        result = operation(job, duplicate, folder)
        result["output_contract"] = output_contract.build_output_contract(OUTPUT_PROFILE)
        if len(_NORMAL_ENCODING_ADJUSTMENTS) > normal_adjustment_start:
            result["normal_encoding_adjustments"] = _NORMAL_ENCODING_ADJUSTMENTS[normal_adjustment_start:]
        attempts = _BAKE_EVENTS[start:]
        result["dependency_validation"] = dependency
        result["uv_validation"] = require_output_uv(duplicate, job.route)
        result.update({"bake_attempts": attempts, "bake_attempt_count": len(attempts),
                       "bake_pass_count": sum(x["status"] == "OK" for x in attempts),
                       "cpu_retry": any(x.get("retry") for x in attempts)})
        return result
    except Exception as exc:
        exc.bake_attempts = _BAKE_EVENTS[start:]
        raise
    finally:
        bpy.context.scene.cycles.device = device


def graph_bake_pass(
    job: Job, obj: Any, semantic: str, resolution: int, face_slots=None,
) -> Tuple[Any, Dict[str, Any], int]:
    _run_boundary("pass", object=datablock_name(job.source), route=job.route,
                  semantic=semantic, resolution=resolution)
    require_material_dependencies(job_dependency_materials(job))
    require_output_uv(obj, "GRAPH_BAKE")
    target = new_bake_image(
        "__RBX_GRAPH_%s_%s" % (job.file_base, semantic), resolution, semantic
    )
    materials: List[Any] = []
    groups: List[Any] = []
    probe_count = 0
    slot_count = max(len(job.source.data.materials), max(job.used_slots, default=-1) + 1, 1)
    # mesh.materials.clear() below resets every polygon material_index to 0, so
    # capture the source assignment first and re-apply it once the bake
    # materials are in place -- otherwise the whole mesh bakes with slot 0.
    if face_slots is None:
        face_slots = source_face_slots(job, obj)
    if face_slots is None:
        # Triangulation/decimation changed face count. The duplicate's face
        # assignments are authoritative for the final bake topology.
        face_slots = np.empty(len(obj.data.polygons), dtype=np.int32)
        obj.data.polygons.foreach_get("material_index", face_slots)
    try:
        obj.data.materials.clear()
        for slot in range(slot_count):
            original = job.materials_by_slot.get(slot)
            material, nested, probes = make_graph_bake_material(
                original, semantic, target,
                "__RBX_GRAPH_%s_%s_%d" % (job.file_base, semantic, slot),
            )
            obj.data.materials.append(material)
            materials.append(material)
            groups.extend(nested)
            probe_count += probes
        restore_face_slots(obj, face_slots)
        ensure_active_object(obj)
        configure_bake(resolution)
        bake_type = "NORMAL" if semantic == "normal" else "EMIT"
        bpy.context.scene.cycles.bake_type = bake_type
        with stage("bake.cycles.graph"):
            cycles_bake(bake_type, resolution)
        return target, image_stats(target), probe_count
    except BaseException:
        if target.name in bpy.data.images:
            bpy.data.images.remove(target)
        raise
    finally:
        obj.data.materials.clear()
        for material in materials:
            if material.name in bpy.data.materials and material.users == 0:
                bpy.data.materials.remove(material)
        remove_group_clones(groups)


UV_DEGENERATE_AREA = 1e-9            # collapsed source UV layer: nothing to pack
NO_SOURCE_UV_SENTINEL = "__RBX_NO_SOURCE_UV__"  # v3.4: job.source_uv placeholder when
                                                 # plan_jobs found no usable UV layer at all


def create_missing_source_uv(obj: Any, job: Any) -> Optional[Dict[str, Any]]:
    """v3.4: give a mesh with ZERO usable UV layers a real unwrap on the duplicate,
    instead of the object being SKIPped outright at plan time.

    Mirrors repair_degenerate_source_uv's mechanism (smart_project on the DUPLICATE,
    never the source -- the "source objects are never modified" guarantee holds) for
    the wider case "no UV layer exists at all" rather than "one exists but is
    collapsed to a point". plan_jobs() cannot compute real UV stats with nothing to
    measure, so it force-routes such objects to GRAPH_BAKE with job.source_uv set to
    NO_SOURCE_UV_SENTINEL and a placeholder job.uv; this replaces both with the
    freshly unwrapped layer's real name and stats before resolution/packing run.
    Runs AFTER tri-budget decimation so the unwrap matches the final exported
    topology, same reasoning as the decimate-before-bake ordering above.
    """
    if job.source_uv != NO_SOURCE_UV_SENTINEL:
        return None
    if not AUTO_UNWRAP_NO_UV:
        raise RuntimeError("no usable UV layer (internal/invalid layers ignored)")
    mesh = obj.data
    new_layer = mesh.uv_layers.new(name="UVMap", do_init=True)
    if new_layer is None:
        raise RuntimeError("could not allocate a UV layer for an unwrapped mesh")
    # v3.4 bugfix (caught by verification, not by me): Edit Mode/UV operators can
    # reallocate Mesh CustomData and invalidate RNA layer handles -- the exact trap
    # repack_uv() already documents and guards against a few functions down. The
    # first version of this function read new_layer.name AFTER the smart_project
    # mode round trip below and got back garbage (a stray internal attribute name,
    # or raw undecodable bytes raising UnicodeDecodeError) because `new_layer` no
    # longer pointed at anything real by then. Freeze the name now, before Edit Mode.
    name = safe_text(new_layer.name, "UVMap")
    if not name:
        raise RuntimeError("new UV layer has no readable name")
    mesh.uv_layers.active = new_layer
    ensure_active_object(obj)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.002)
    except Exception as exc:
        print("    UV auto-unwrap FAILED: %s" % safe_text(exc))
        raise
    finally:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode="OBJECT")
    # Reacquire by the frozen name (index-based lookup, decode-safe) rather than
    # trusting the pre-round-trip `new_layer` RNA handle for anything further.
    mesh = obj.data
    if uv_layer_by_name(mesh, name) is None:
        raise RuntimeError("unwrapped UV layer lost after smart_project")
    job.source_uv = name
    job.uv = uv_stats(mesh, name)
    print("    UV auto-unwrap: no source UV layer -- smart-projected one (area %.6f)"
          % job.uv.triangle_area)
    return {"uv_created": True, "uv_area_after": job.uv.triangle_area}


def repair_degenerate_source_uv(obj: Any, job: Any) -> Optional[Dict[str, Any]]:
    """Rebuild a source UV layer that is collapsed to a single point.

    Some source meshes ship a UV layer whose every loop sits on one point
    (bounds 0,0,0,0 / zero triangle area).  uv.pack_islands then has no island
    area to place: it raises nothing, so the shelf/bbox fallbacks never fire and
    the packed layer stays collapsed.  The bake rasterises no texels at all,
    every EMIT map reads exactly 0.0, and the tool misreports that as
    "GRAPH_BAKE ColorMap is numerically all black".  Give the duplicate a real
    unwrap and refresh the job UV stats so the resolution estimate sees the
    rebuilt density instead of clamping to MIN_RES.
    """
    mesh = obj.data
    try:
        before = uv_stats(mesh, job.source_uv)
    except Exception:
        return None
    if before.triangle_area > UV_DEGENERATE_AREA:
        return None
    layer = uv_layer_by_name(mesh, job.source_uv)
    if layer is None:
        return None
    mesh.uv_layers.active = layer
    ensure_active_object(obj)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.002)
    except Exception as exc:
        print("    UV repair FAILED: %s" % safe_text(exc))
        return None
    finally:
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode="OBJECT")
    after = uv_stats(mesh, job.source_uv)
    job.uv = after
    print("    UV repair: collapsed source layer rebuilt (area %.6f -> %.6f)"
          % (before.triangle_area, after.triangle_area))
    return {"uv_repaired": True,
            "uv_area_before": before.triangle_area,
            "uv_area_after": after.triangle_area}


def source_face_slots(job: Any, obj: Any) -> Optional[Any]:
    """Per-face material_index read from the untouched source mesh."""
    src = getattr(getattr(job, "source", None), "data", None)
    if src is None or len(src.polygons) != len(obj.data.polygons):
        return None
    buf = np.empty(len(src.polygons), dtype=np.int32)
    src.polygons.foreach_get("material_index", buf)
    return buf


def restore_face_slots(obj: Any, face_slots: Optional[Any]) -> None:
    """Re-apply per-face slot assignment after the slot list was rebuilt.

    mesh.materials.clear() resets every polygon material_index to 0, so without
    this every multi-material mesh bakes entirely with slot 0's material and the
    other slots never contribute a texel.
    """
    if face_slots is None:
        return
    mesh = obj.data
    count = len(mesh.materials)
    if count <= 1 or len(face_slots) != len(mesh.polygons):
        return
    np.clip(face_slots, 0, count - 1, out=face_slots)
    mesh.polygons.foreach_set("material_index", face_slots)
    mesh.update()


def _walk_layer_collections(layer: Any, path: Tuple[str, ...] = ()) -> List[Tuple[Tuple[str, ...], Any]]:
    """Every layer collection under `layer`, pre-order, keyed by name path.

    Keyed by PATH rather than by RNA handle: LayerCollection wrappers are views
    onto the view layer's own tree, and the file's caller-side hazard note about
    Blender handing back fresh wrappers applies to them as much as to objects.
    Pre-order matters on restore -- a parent's exclude write propagates to its
    children, so parents must be written before the children's own recorded
    values go back on top.
    """
    found = [(path + (safe_text(layer.name, "layer"),), layer)]
    for child in layer.children:
        found.extend(_walk_layer_collections(child, path + (safe_text(layer.name, "layer"),)))
    return found


class GraphBakeIsolation:
    """Cull the scene down to one duplicate for the duration of that object's
    GRAPH_BAKE passes.  Enter ONCE around all of them, never per pass.

    WHY.  PERSISTENT_DATA is False, so Cycles tears down and re-syncs its whole
    scene on every bpy.ops.object.bake() call -- every material in the render
    depsgraph re-compiled, every image it references re-loaded, every mesh
    re-pushed and the BVH rebuilt.  GRAPH_BAKE issues four or five bake() calls
    per object (color, optional alpha, metal, rough, normal) and pays that bill
    on the WHOLE scene each time, even though a bake reads exactly one object.

    WHY TWO MECHANISMS, measured not assumed.  On the real 2622-object /
    516-material Jacobs_Ship rip, one EMIT bake of one small greeble duplicate
    (tests/probe_cull_modes.py, CPU, 256px, two passes per condition):
        stock, whole scene resident        27.0s / 31.6s
        + hide_render on the other 2585    13.9s / 12.8s   <- and STILL spams
                                                              "Failed to load 3
                                                              image files" every
                                                              single pass
        + exclude the other root layers     2.4s /  1.3s   <- spam gone entirely
    hide_render only flags an object out of the render ITERATOR; its material
    and images stay in depsgraph.ids, so Cycles still builds all 516 shader
    graphs and still retries all 1059 images (three of which have dead paths in
    this rip) on every pass.  LayerCollection.exclude removes the objects from
    the DEPSGRAPH, which is the only thing that takes their materials with them.
    Neither alone is enough: exclude cannot reach an object linked straight into
    the scene's master collection, or one sharing the duplicate's own output
    collection (every earlier part of the same run), and those are exactly what
    hide_render covers.  So both, under one flag.

    WHY THIS CANNOT CHANGE A PIXEL.  Only two bake types run under this guard,
    both chosen in graph_bake_pass(): EMIT for color/alpha/metal/rough, and
    NORMAL for normal.  configure_bake() pins use_selected_to_active=False for
    both.  An EMIT bake shades the target surface's own emission closure and
    casts no ray into the scene; a NORMAL bake with selected-to-active off
    reads the target mesh's own normals.  Neither samples other geometry, other
    materials or the world, so removing them cannot alter a texel.  (A COMBINED
    / AO / SHADOW bake, or use_selected_to_active=True, WOULD be affected --
    do not reuse this guard for one.)  Verified, not argued: the same object
    baked with the flag off and on produced byte-identical _ALB/_MET/_RGH/_NOR.

    Reading job.source's mesh from inside the guard (source_face_slots) is
    unaffected: exclusion is view-layer membership only, and never unlinks a
    datablock from bpy.data.

    RESTORE.  Object hide_render: only objects actually changed are recorded,
    each with the exact prior value read before the write.  Layer exclude: the
    WHOLE view-layer tree is snapshotted first, because writing a root's
    exclude propagates to its descendants and would otherwise flatten a
    partially-excluded subtree on the way back out.  __exit__ runs on the
    exception path too, so a pass that dies mid-bake still leaves the scene as
    it found it.  Nothing ever writes a blanket False, so this cannot fight the
    hide_render clearing FORCE_VISIBLE_OUTPUT/force_visible() already did on
    this run's duplicates -- they were False on entry and go back False.
    `keep` itself, and whichever root collection holds it, are skipped outright.
    """

    def __init__(self, keep: Any):
        self.keep = keep
        self.hidden: List[Tuple[Any, bool]] = []
        self.layer_snapshot: List[Tuple[Tuple[str, ...], bool]] = []

    def _holds_keep(self, collection: Any) -> bool:
        # `==`, never `is`: Blender returns a fresh wrapper per access, so an
        # identity test here would exclude the collection holding the bake
        # target and Cycles would answer with
        # `RuntimeError: Object "X" is not enabled for rendering`.
        try:
            return any(obj == self.keep for obj in collection.all_objects)
        except Exception:
            return True          # unreadable: assume it holds the target, keep it

    def __enter__(self) -> "GraphBakeIsolation":
        if not ISOLATE_GRAPH_BAKE:
            return self
        read_only = 0
        # Nothing below may raise OUT of __enter__: an exception here means the
        # `with` block is never entered, so __exit__ never runs and whatever was
        # already hidden stays hidden for the rest of the session.  Every failure
        # degrades to "less isolation", never to "unrestored scene".
        try:
            for obj in bpy.context.scene.objects:
                if obj == self.keep:
                    continue
                try:
                    prior = bool(obj.hide_render)
                    if prior:
                        continue       # already out of the render depsgraph
                    obj.hide_render = True
                except Exception:
                    # Library-linked IDs can make this read-only, exactly as
                    # force_visible() documents for the same flag.  Such an
                    # object stays in the depsgraph: slower, still correct.
                    read_only += 1
                    continue
                self.hidden.append((obj, prior))
        except Exception as exc:
            print("MACHINE|WARN isolate_hide_failed object=%s err=%s"
                  % (datablock_name(self.keep, "object"), safe_text(exc)), flush=True)

        excluded = 0
        try:
            root = bpy.context.view_layer.layer_collection
            self.layer_snapshot = [
                (path, bool(layer.exclude)) for path, layer in _walk_layer_collections(root)
            ]
            for layer in root.children:
                # Only ROOT children: the master collection itself cannot be
                # excluded, and excluding a root already takes its whole subtree.
                if layer.exclude or self._holds_keep(layer.collection):
                    continue
                layer.exclude = True
                excluded += 1
        except Exception as exc:
            # A partial exclude is still consistent -- __exit__ restores from the
            # snapshot taken before any of it -- and the hide_render cull above
            # already stands on its own.  Degrade, never fail the bake.
            print("MACHINE|WARN isolate_exclude_failed object=%s err=%s"
                  % (datablock_name(self.keep, "object"), safe_text(exc)), flush=True)

        if self.hidden or excluded or read_only:
            print("    ISOLATE: culled %d object(s) + %d collection(s) for this object's "
                  "bake passes%s"
                  % (len(self.hidden), excluded,
                     "" if not read_only
                     else " (%d read-only, left visible)" % read_only))
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Restore in reverse order of application: put the view layer back
        # first, so every object whose hide_render is about to be rewritten is
        # reachable again.
        if self.layer_snapshot:
            try:
                layers = dict(_walk_layer_collections(bpy.context.view_layer.layer_collection))
                for path, prior in self.layer_snapshot:
                    layer = layers.get(path)
                    if layer is None:
                        continue     # tree reshaped under us; nothing to put back
                    if bool(layer.exclude) != prior:
                        layer.exclude = prior
            except Exception as restore_exc:
                print("MACHINE|WARN isolate_restore_failed scope=layer_collections err=%s"
                      % safe_text(restore_exc), flush=True)
            self.layer_snapshot = []
        for obj, prior in self.hidden:
            try:
                obj.hide_render = prior
            except Exception as restore_exc:
                print("MACHINE|WARN isolate_restore_failed object=%s err=%s"
                      % (datablock_name(obj, "object"), safe_text(restore_exc)), flush=True)
        self.hidden = []


def process_graph_bake(job: Job, duplicate: Any, folder: str) -> Dict[str, Any]:
    with stage("route.graph_bake"):
        require_output_profile_route("GRAPH_BAKE")
        return run_bake_route(_process_graph_bake_inner, job, duplicate, folder)


def _process_graph_bake_inner(job: Job, duplicate: Any, folder: str) -> Dict[str, Any]:
    # v3.3: the Roblox triangle cap was only enforced on the PROXY/chunk path; GRAPH_BAKE shipped
    # 895k-tri MeshParts with ENFORCE_TRI_BUDGET=True. Decimate the duplicate BEFORE repack + bake so
    # the baked maps match the exported topology (same order per_object_chunks uses).
    try:
        bpy.context.view_layer.objects.active = duplicate
        duplicate.select_set(True)
        b0, b1 = enforce_budget(duplicate)
        if b1 != b0:
            print("MACHINE|tri_budget object=%s before=%d after=%d budget=%d" % (job.file_base, b0, b1, TRI_BUDGET), flush=True)
    except Exception as exc:
        print("MACHINE|WARN tri_budget_failed object=%s err=%s" % (job.file_base, safe_text(exc)), flush=True)
    if TARGET_PROFILE == "ROBLOX":
        # Freeze the triangle topology before normal baking and FBX tangent
        # export, rather than relying on the destination's quad diagonal.
        bm = bmesh.new()
        try:
            bm.from_mesh(duplicate.data)
            bmesh.ops.triangulate(bm, faces=list(bm.faces))
            bm.to_mesh(duplicate.data)
            duplicate.data.update()
        finally:
            bm.free()
    uv_created = create_missing_source_uv(duplicate, job)
    uv_repair = repair_degenerate_source_uv(duplicate, job)
    if uv_created:
        uv_repair = {**uv_created, **(uv_repair or {})}
    resolution = estimate_graph_bake_resolution(job)
    packed_name, packed_report = repack_uv(duplicate, job.source_uv, resolution)
    result: Dict[str, Any] = {
        "resolution": resolution, "maps": {}, "graph_unresolved_slots": job.unresolved_slots,
        **packed_report,
    }
    require_output_uv(duplicate, "GRAPH_BAKE", packed_name)
    result["packed_scalars"] = PACK_MRA_GRAPH_BAKE
    if uv_repair:
        result.update(uv_repair)
    # Preserve final-topology assignments outside the per-pass material cleanup.
    graph_face_slots = np.empty(len(duplicate.data.polygons), dtype=np.int32)
    duplicate.data.polygons.foreach_get("material_index", graph_face_slots)
    live_images: List[Any] = []
    alpha_used = False
    # ISOLATE_GRAPH_BAKE: ONE cull around ALL of this object's passes, not one
    # per pass.  Entering and leaving each costs a depsgraph visibility flush,
    # and the whole point is that the four-or-five bake() calls below share a
    # depsgraph that already holds nothing but this duplicate.  Everything above
    # (decimate, auto-unwrap, UV repair, repack) is mesh work Cycles never sees,
    # so it stays outside.
    with GraphBakeIsolation(duplicate):
        try:
            color, stats, probes = graph_bake_pass(job, duplicate, "color", resolution, graph_face_slots)
            live_images.append(color)
            if stats["max"] < 1e-4:
                job.warnings.append("GRAPH_BAKE ColorMap is numerically all black")
            result["maps"]["color"] = {
                "size": [resolution, resolution], "stats": stats, "probes": probes,
            }

            if PACK_MRA_GRAPH_BAKE:
                packed, _packed_stats, packed_probes = graph_bake_pass(
                    job, duplicate, "mra", resolution, graph_face_slots)
                live_images.append(packed)
                raw = np.empty(len(packed.pixels), dtype=np.float32)
                packed.pixels.foreach_get(raw)
                rgba = raw.reshape(-1, 4)
                if job_requests_alpha(job):
                    alpha_used = merge_alpha_from_channel(color, packed, 2)
                result["alpha_stats"] = channel_stats(rgba, 2)
                result["alpha_probes"] = packed_probes
                for index, semantic in enumerate(("metal", "rough")):
                    write_output_pixels(output_path(folder, job.file_base, semantic),
                                        rgba[:, index].reshape(resolution, resolution))
                    result["maps"][semantic] = {"size": [resolution, resolution],
                        "stats": channel_stats(rgba, index), "probes": packed_probes}
                bpy.data.images.remove(packed)
                live_images.remove(packed)
            elif job_requests_alpha(job):
                alpha, alpha_stats, alpha_probes = graph_bake_pass(
                    job, duplicate, "alpha", resolution, graph_face_slots)
                live_images.append(alpha)
                alpha_used = merge_alpha(color, alpha)
                result["alpha_stats"] = alpha_stats
                result["alpha_probes"] = alpha_probes

            save_blender_image(color, output_path(folder, job.file_base, "color"))
            bpy.data.images.remove(color)
            live_images.remove(color)

            for semantic in (("normal",) if PACK_MRA_GRAPH_BAKE else ("metal", "rough", "normal")):
                image, pass_stats, pass_probes = graph_bake_pass(job, duplicate, semantic, resolution, graph_face_slots)
                live_images.append(image)
                save_blender_image(image, output_path(folder, job.file_base, semantic))
                result["maps"][semantic] = {"size": [resolution, resolution],
                    "stats": pass_stats, "probes": pass_probes}
                bpy.data.images.remove(image)
                live_images.remove(image)
            if TARGET_PROFILE == "ROBLOX":
                from .roblox_output import finalize_roblox_object
                emission, emission_stats, emission_probes = graph_bake_pass(
                    job, duplicate, "emission", resolution, graph_face_slots)
                live_images.append(emission)
                pixels = np.empty(len(emission.pixels), dtype=np.float32)
                emission.pixels.foreach_get(pixels)
                binding = finalize_roblox_object(
                    folder,
                    color_path=output_path(folder, job.file_base, "color"),
                    metal_path=output_path(folder, job.file_base, "metal"),
                    rough_path=output_path(folder, job.file_base, "rough"),
                    normal_path=output_path(folder, job.file_base, "normal"),
                    emission_pixels_linear=pixels.reshape(resolution, resolution, 4)[..., :3],
                    dimensions=(resolution, resolution), binding_id=job.file_base,
                    mesh_name=duplicate.name, allow_approximation=ALLOW_APPROXIMATION)
                result["roblox_binding"] = binding
                result["maps"]["emissive"] = {"size": [resolution, resolution],
                    "stats": emission_stats, "probes": emission_probes,
                    "factorization": binding["emission"]}
        finally:
            for image in live_images:
                if image.name in bpy.data.images:
                    bpy.data.images.remove(image)
            duplicate.data.materials.clear()
    finalize_single_uv(duplicate.data, packed_name)
    result["alpha"] = alpha_used
    return result


# ---------------------------- preview and export ------------------------------

def load_preview_texture(tree: Any, path: str, semantic: str, y: float) -> Any:
    image = bpy.data.images.load(path, check_existing=True)
    image.colorspace_settings.name = "sRGB" if semantic == "color" else "Non-Color"
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.location = (-500, y)
    return node


def assign_preview_material(obj: Any, folder: str, base: str, alpha: bool,
                            roblox_binding: Optional[Dict[str, Any]] = None) -> None:
    material = bpy.data.materials.new(base + "_RBX_PBR")
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    output.location = (500, 0)
    principled.location = (150, 0)
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    color = load_preview_texture(tree, output_path(folder, base, "color"), "color", 300)
    metal = load_preview_texture(tree, output_path(folder, base, "metal"), "metal", 50)
    rough = load_preview_texture(tree, output_path(folder, base, "rough"), "rough", -200)
    normal = load_preview_texture(tree, output_path(folder, base, "normal"), "normal", -450)
    tree.links.new(color.outputs["Color"], principled.inputs["Base Color"])
    tree.links.new(metal.outputs["Color"], principled.inputs["Metallic"])
    tree.links.new(rough.outputs["Color"], principled.inputs["Roughness"])
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.location = (-150, -450)
    tree.links.new(normal.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    if alpha:
        tree.links.new(color.outputs["Alpha"], principled.inputs["Alpha"])
        try:
            material.surface_render_method = "DITHERED"
        except Exception:
            try:
                material.blend_method = "HASHED"
            except Exception:
                pass
    if roblox_binding:
        emission = roblox_binding["emission"]
        mask = load_preview_texture(tree, output_path(folder, base, "emissive"), "emissive", -600)
        tint = tree.nodes.new("ShaderNodeVectorMath")
        tint.operation = "MULTIPLY"
        tint.inputs[1].default_value = emission["tint_linear"]
        tree.links.new(color.outputs["Color"], tint.inputs[0])
        tree.links.new(tint.outputs["Vector"], principled.inputs["Emission Color"])
        strength = tree.nodes.new("ShaderNodeMath")
        strength.operation = "MULTIPLY"
        strength.inputs[1].default_value = emission["strength"]
        tree.links.new(mask.outputs["Color"], strength.inputs[0])
        tree.links.new(strength.outputs[0], principled.inputs["Emission Strength"])
    obj.data.materials.clear()
    obj.data.materials.append(material)


def run_visual_validation(source: Any, output: Any, folder: str, file_base: str) -> Dict[str, Any]:
    """M1-C: compare one converted part with its source and save the report.

    Returns a JSON-safe manifest summary.  An exception inside the comparison
    is recorded as FAIL, never hidden; cancellation (KeyboardInterrupt)
    propagates like any other cancelled bake.  Reports go to
    <folder>/visual/<run id>/<file_base>/ so a rerun into the same output
    folder never overwrites an earlier verdict.  ``file_base`` is sanitised
    into one path component (an object name may hold '/', '\\' or '..').
    The probe resolution defaults to "auto" (sized from each part's layout).
    """
    if source is None or output is None:
        return {"status": "NOT_RUN", "report": None, "reasons": ["VISUAL_OBJECT_UNAVAILABLE"]}
    from .visual_validation import compare_pair, default_settings
    settings = default_settings()
    settings["probe_resolution"] = "auto"
    raw = safe_text(file_base, "part")
    component = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")[:48] or "part"
    if component != raw:
        component += "_" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]
    destination = os.path.join(folder, "visual", _VISUAL_RUN_ID or "run", component)
    # The device follows the ENGINE's configuration, resolved exactly as for a
    # bake (same GPU choice and CPU-fallback rule), never the device the .blend
    # happened to be saved with: CROP_SPLIT and resumed parts are compared
    # outside the bake loop, where the scene still holds the saved setting.
    # The guard restores the scene and Cycles preferences afterwards; nothing
    # here raises into the caller (a failed restore is a FAIL verdict too).
    try:
        with SceneSettingsGuard(snapshot_cycles_preferences=DEVICE == "GPU"):
            try:
                device = ensure_cycles_device()
            except Exception as exc:
                return {"status": "FAIL", "report": None,
                        "reasons": ["VISUAL_DEVICE_UNAVAILABLE"], "error": safe_text(exc)}
            settings["device"] = "CPU" if device == "CPU" else "GPU"
            settings.update(VISUAL_VALIDATION_SETTINGS or {})
            report = compare_pair(source, output, destination, settings)
    except Exception as exc:
        return {"status": "FAIL", "report": None, "reasons": ["VISUAL_COMPARISON_NOT_RUN"],
                "error": safe_text(exc)}
    return {"status": report["status"], "report": os.path.join(destination, "report.json"),
            "reasons": sorted({row["code"] for row in report["reasons"]}),
            "fidelity_scope": report["coverage"].get("fidelity_scope")}


def ensure_cycles_device() -> str:
    """Choose exactly one GPU; an explicit identity never silently selects another."""
    global _LAST_DEVICE_SELECTION
    scene = bpy.context.scene
    _LAST_DEVICE_SELECTION = {"requested": DEVICE, "requested_id": GPU_DEVICE_ID,
                              "backend": "CPU", "id": "", "name": "CPU"}
    if DEVICE != "GPU":
        scene.cycles.device = "CPU"
        return "CPU"
    requested = str(GPU_DEVICE_ID or "").strip()
    errors = []
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except Exception as exc:
        prefs = None
        errors.append(safe_text(exc))
    if prefs is not None:
        for kind in GPU_BACKENDS:
            _run_boundary("device_probe", backend=kind)
            try:
                prefs.compute_device_type = kind
                prefs.get_devices()
                all_devices = list(prefs.devices)
                devices = [(index, device) for index, device in enumerate(all_devices)
                           if device.type == kind]
            except Exception as exc:
                errors.append("%s: %s" % (kind, safe_text(exc)))
                continue
            if requested:
                matches = [(index, device) for index, device in devices
                           if requested == str(getattr(device, "id", ""))]
                if not matches:
                    matches = [(index, device) for index, device in devices
                               if requested == device.name]
                if len(matches) > 1:
                    raise RuntimeError("GPU device name is ambiguous; select an exact Cycles id: %s"
                                       % requested)
            else:
                matches = devices[:1]
            if not matches:
                continue
            chosen_index, chosen = matches[0]
            for index, device in enumerate(all_devices):
                device.use = index == chosen_index
            scene.cycles.device = "GPU"
            _LAST_DEVICE_SELECTION.update(backend=kind, id=str(getattr(chosen, "id", "")),
                                          name=str(chosen.name), enabled_device_count=1)
            _run_event("device_selected", **_LAST_DEVICE_SELECTION)
            print("Cycles device: %s (%s; id=%s)" % (kind, chosen.name,
                  _LAST_DEVICE_SELECTION["id"]))
            return kind
    if requested:
        raise RuntimeError("requested Cycles GPU device is unavailable: %s" % requested)
    if not ALLOW_CPU_FALLBACK:
        raise RuntimeError("no supported Cycles GPU device was available and CPU fallback is disabled")
    scene.cycles.device = "CPU"
    _LAST_DEVICE_SELECTION.update(fallback=True, probe_errors=errors)
    _run_event("device_selected", **_LAST_DEVICE_SELECTION)
    print("Cycles device: CPU fallback")
    return "CPU"


class SceneSettingsGuard:
    def __init__(self, snapshot_cycles_preferences: bool = False):
        self.scene = bpy.context.scene
        self.values: Dict[str, Any] = {}
        self.snapshot_cycles_preferences = bool(snapshot_cycles_preferences)

    def __enter__(self) -> "SceneSettingsGuard":
        scene = self.scene
        self.values["engine"] = scene.render.engine
        self.values["samples"] = scene.cycles.samples
        self.values["bake_type"] = getattr(scene.cycles, "bake_type", None)
        self.values["persistent"] = getattr(scene.render, "use_persistent_data", False)
        self.values["device"] = getattr(scene.cycles, "device", "CPU")
        bake = scene.render.bake
        self.values["bake"] = {
            name: getattr(bake, name)
            for name in (
                "use_clear", "use_selected_to_active", "margin", "margin_type",
                "normal_space", "normal_r", "normal_g", "normal_b",
            )
            if hasattr(bake, name)
        }
        if self.snapshot_cycles_preferences:
            try:
                prefs = bpy.context.preferences.addons["cycles"].preferences
                prefs.get_devices()
                self.values["cycles_prefs"] = {
                    "compute_device_type": prefs.compute_device_type,
                    "uses": {(str(getattr(d, "id", d.name)), d.type): bool(d.use) for d in prefs.devices},
                }
            except Exception as exc:
                raise RuntimeError(
                    "could not snapshot Cycles preferences before GPU bake: %s"
                    % safe_text(exc)
                ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        scene = self.scene
        errors: List[str] = []
        for label, setter in (
            ("render engine", lambda: setattr(scene.render, "engine", self.values["engine"])),
            ("cycles samples", lambda: setattr(scene.cycles, "samples", self.values["samples"])),
            ("cycles bake type", lambda: setattr(scene.cycles, "bake_type", self.values["bake_type"])
             if self.values.get("bake_type") is not None else None),
            ("persistent data", lambda: setattr(scene.render, "use_persistent_data", self.values["persistent"])),
            ("cycles device", lambda: setattr(scene.cycles, "device", self.values["device"])),
        ):
            try:
                setter()
            except Exception as restore_exc:
                errors.append("%s: %s" % (label, safe_text(restore_exc)))
        for name, value in self.values.get("bake", {}).items():
            try:
                setattr(scene.render.bake, name, value)
            except Exception as restore_exc:
                errors.append("bake.%s: %s" % (name, safe_text(restore_exc)))
        saved = self.values.get("cycles_prefs")
        if saved:
            try:
                prefs = bpy.context.preferences.addons["cycles"].preferences
                prefs.compute_device_type = saved["compute_device_type"]
                prefs.get_devices()
                for device in prefs.devices:
                    key = (str(getattr(device, "id", device.name)), device.type)
                    if key in saved["uses"]:
                        device.use = saved["uses"][key]
            except Exception as restore_exc:
                errors.append("Cycles preferences: %s" % safe_text(restore_exc))
        if errors:
            raise RuntimeError("could not restore scene settings: " + "; ".join(errors))


def export_outputs(objects: List[Any], folder: str, report: Dict[str, Any]) -> None:
    if not EXPORT_GLTF and not EXPORT_FBX:
        return
    if not objects:
        if EXPORT_GLTF:
            report["exports"]["glb_error"] = "no output objects are available to export"
        if EXPORT_FBX:
            report["exports"]["fbx_error"] = "no output objects are available to export"
        return
    selected_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active
    hidden_before = [(obj, bool(obj.hide_get())) for obj in objects]
    try:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.hide_set(False)
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0]
        if EXPORT_GLTF:
            _run_boundary("export", format="GLB")
            path = os.path.join(folder, "rbx_pbr_smart.glb")
            try:
                operator_result = bpy.ops.export_scene.gltf(
                    filepath=path, use_selection=True, export_format="GLB",
                    export_apply=True,
                )
                if "FINISHED" not in operator_result or not os.path.isfile(path) or os.path.getsize(path) <= 0:
                    raise RuntimeError("GLB exporter did not produce a non-empty file (%s)" % operator_result)
                report["exports"]["glb"] = path
                print("GLB exported:", path)
            except Exception as exc:
                report["exports"]["glb_error"] = str(exc)
                print("GLB export failed:", exc)
        if EXPORT_FBX:
            _run_boundary("export", format="FBX")
            path = os.path.join(folder, "rbx_pbr_smart.fbx")
            try:
                operator_result = bpy.ops.export_scene.fbx(
                    filepath=path, use_selection=True, object_types={"MESH"},
                    path_mode="STRIP", embed_textures=False, bake_anim=False,
                    use_mesh_modifiers=True,
                    use_tspace=(TARGET_PROFILE == "ROBLOX"),
                )
                if "FINISHED" not in operator_result or not os.path.isfile(path) or os.path.getsize(path) <= 0:
                    raise RuntimeError("FBX exporter did not produce a non-empty file (%s)" % operator_result)
                report["exports"]["fbx"] = path
                print("FBX exported:", path)
            except Exception as exc:
                report["exports"]["fbx_error"] = str(exc)
                print("FBX export failed:", exc)
    finally:
        restore_errors: List[str] = []
        try:
            bpy.ops.object.select_all(action="DESELECT")
        except Exception as exc:
            restore_errors.append("clear export selection: %s" % safe_text(exc))
        for obj in selected_before:
            try:
                if obj.name in bpy.context.view_layer.objects:
                    obj.select_set(True)
            except Exception as exc:
                restore_errors.append("restore selection %s: %s" % (datablock_name(obj), safe_text(exc)))
        try:
            if active_before is None or active_before.name in bpy.context.view_layer.objects:
                bpy.context.view_layer.objects.active = active_before
        except Exception as exc:
            restore_errors.append("restore active object: %s" % safe_text(exc))
        for obj, hidden in hidden_before:
            try:
                obj.hide_set(hidden)
            except Exception as exc:
                restore_errors.append("restore visibility %s: %s" % (datablock_name(obj), safe_text(exc)))
        if restore_errors:
            report["exports"]["state_error"] = "; ".join(restore_errors)


# ==========================================================================
# V3.2 MACHINERY -- view-layer include, memory gate, done-list/resume,
# black classification, output verification, crop-split route, finalize,
# failure census.  All of it is opt-outable through the V3.2 CONFIG block.
# ==========================================================================

def unexclude_all_collections(view_layer: Any = None) -> int:
    """Un-exclude every layer collection so its objects are selectable.

    Source scenes park mesh objects in collections that are EXCLUDED from the
    active view layer (Hangar_X's "Hangar x REFS" subtree, exclude=True in the
    ORIGINAL source file).  Objects outside the active view layer are absent
    from view_layer.objects entirely -- select_set() cannot reach them, so they
    were silently invisible to selection and never baked.  Hit twice on
    Hangar_X; every per-scene driver since carries a private copy of this.

    v3.4: also clears LayerCollection.hide_viewport -- the per-view-layer eye
    icon, a THIRD and separate flag from both `exclude` (the checkbox above)
    and `Collection.hide_viewport` (the data-level "disable in viewports"
    monitor icon, already handled below). Confirmed on a real delivered asset
    (JS_GEN_Books01.blend, "Assets" collection): objects under an eye-hidden
    ancestor are present in view_layer.objects, select_set()/hide_get() report
    them fine, but object.visible_get() is False and bpy.context.selected_objects
    -- which every bpy.ops operator (including bake()) reads from -- silently
    excludes them. No error, no warning, no manifest entry: the object is just
    never processed. This is the eye icon most artists reach for FIRST, more
    common in practice than `exclude`.
    """
    if not FORCE_VIEW_LAYER:
        return 0
    view_layer = view_layer or bpy.context.view_layer
    changed = 0

    def walk(layer_coll: Any) -> None:
        nonlocal changed
        if layer_coll.exclude:
            layer_coll.exclude = False
            changed += 1
        if layer_coll.hide_viewport:
            layer_coll.hide_viewport = False
            changed += 1
        try:
            if layer_coll.collection is not None and layer_coll.collection.hide_viewport:
                layer_coll.collection.hide_viewport = False
        except Exception:
            pass
        for child in layer_coll.children:
            walk(child)

    walk(view_layer.layer_collection)
    if changed:
        print("UNEXCLUDE: un-excluded %d collection(s) from the view layer" % changed)
    return changed


def ensure_reachable(obj: Any, view_layer: Any = None) -> bool:
    """Last resort for an object linked into no collection of this scene."""
    view_layer = view_layer or bpy.context.view_layer
    if obj.name in view_layer.objects:
        return True
    unexclude_all_collections(view_layer)
    if obj.name in view_layer.objects:
        return True
    try:
        if obj.name not in bpy.context.scene.collection.objects:
            bpy.context.scene.collection.objects.link(obj)
            print("REACHABLE: linked %s into the scene master collection" % obj.name)
    except Exception as exc:
        print("MACHINE|WARN could_not_link object=%s err=%s" % (obj.name, safe_text(exc)))
    return obj.name in view_layer.objects


# ------------------------------ memory gate -----------------------------------

def _cgroup_stat_bytes(key: str) -> Optional[int]:
    try:
        with open(os.path.join(os.path.dirname(CGROUP_MEM_CURRENT), "memory.stat"), "r", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == key:
                    return int(v.strip())
    except Exception:
        pass
    return None


def cgroup_memory_limit_gb() -> Optional[float]:
    try:
        with open(os.path.join(os.path.dirname(CGROUP_MEM_CURRENT), "memory.max"), "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return None if raw == "max" else int(raw) / (1024.0 ** 3)
    except Exception:
        return None


def cgroup_memory_gb() -> Optional[float]:
    """Working set in GB: memory.current minus reclaimable page cache (inactive_file).

    v3.2 read raw memory.current, which on a pod streaming hundreds of GB through rsync is mostly page
    cache -> the gate waited its full budget (900 s) before every bake loop while the GPU sat idle
    (2026-08-22 hotfix raised the number; 2026-08-23 ops stress test hit it again at 263 GB cache on a
    1 TB pod). The kernel reclaims inactive_file on demand, so it is not pressure.
    """
    try:
        with open(CGROUP_MEM_CURRENT, "r", encoding="utf-8") as f:
            current = int(f.read().strip())
    except Exception:
        return None
    inactive = _cgroup_stat_bytes("inactive_file") or 0
    return max(0, current - inactive) / (1024.0 ** 3)


def memory_gate(threshold_gb: Optional[float] = None,
                wait_s: Optional[float] = None, tag: str = "") -> Dict[str, Any]:
    """Block until cgroup memory falls below the gate before a bake loop starts.

    Six independent lane identities hand-rolled this same /sys/fs/cgroup check
    inline before launching; the drivers themselves only ever had reactive
    OOM-retry.  Returns a report dict; never raises.
    """
    threshold = MEM_GATE_GB if threshold_gb is None else threshold_gb
    limit = cgroup_memory_limit_gb()
    if limit and threshold > 0:
        threshold = min(threshold, 0.85 * limit)   # never gate above what the cgroup can actually give
    budget = MEM_GATE_WAIT_S if wait_s is None else wait_s
    current = cgroup_memory_gb()
    print("MACHINE|memgate-basis working_set=%.2fGB inactive_file=%.2fGB limit=%s threshold=%.2fGB"
          % (current or -1.0, (_cgroup_stat_bytes("inactive_file") or 0) / (1024.0 ** 3),
             ("%.0fGB" % limit) if limit else "none", threshold), flush=True)
    if current is None or threshold <= 0:
        return {"gate": "skipped", "reason": "no cgroup memory.current readable"}
    waited = 0.0
    while current is not None and current >= threshold and waited < budget:
        _run_boundary("memory_wait", waited_seconds=waited)
        print("MACHINE|memgate-wait%s mem=%.2fGB threshold=%.2fGB waited=%.0fs"
              % (("-" + tag) if tag else "", current, threshold, waited), flush=True)
        time.sleep(MEM_GATE_POLL_S)
        waited += MEM_GATE_POLL_S
        current = cgroup_memory_gb()
    passed = current is not None and current < threshold
    print("MACHINE|memgate%s mem=%.2fGB threshold=%.2fGB waited=%.0fs pass=%s"
          % (("-" + tag) if tag else "", current or -1.0, threshold, waited, passed), flush=True)
    return {"gate": "pass" if passed else "timeout", "memory_gb": current,
            "threshold_gb": threshold, "waited_seconds": waited}


# --------------------------- done-list and resume ------------------------------

def cli_args(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Parse this tool's own flags after the `--` separator.

    blender -b scene.blend -P import_glue_v32.py -- --only remaining.json
    Supported: --only <json|csv>  --done <path>  --no-resume  --rerun
               --no-finalize  --tag <text>  --mem-gate <GB>
    """
    argv = list(sys.argv if argv is None else argv)
    if "--" not in argv:
        return {}
    tail = argv[argv.index("--") + 1:]
    out: Dict[str, Any] = {}
    index = 0
    while index < len(tail):
        token = tail[index]
        if token.startswith("--"):
            key = token[2:]
            value: Any = True
            if index + 1 < len(tail) and not tail[index + 1].startswith("--"):
                value = tail[index + 1]
                index += 1
            out[key] = value
        index += 1
    return out


def only_names(spec: Any) -> Optional[Set[str]]:
    """--only accepts a JSON file ({"objects": [...]} or a bare list) or a
    comma-separated list of object names.  Five scenes hand-rolled this exact
    scoped-resume flag after an OOM SIGKILL; it is native now."""
    if spec is None:
        return None
    if spec is True:
        # A bare `--only` returned None, i.e. "no scoping at all" -- the exact
        # opposite of the request, and on a farm that is a whole-scene bake.
        print("MACHINE|setup_error|--only requires a JSON path or a comma-separated name list", flush=True)
        raise SystemExit(2)
    text = safe_text(spec)
    if not text.strip():
        print("MACHINE|only_empty note='--only given with an empty value: scoping to ZERO objects (v3.3)'", flush=True)
        return set()
    if os.path.exists(text):
        try:
            with open(text, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("objects", [])
            if not isinstance(data, (list, tuple, set)):
                raise ValueError("expected a list of names, got %s" % type(data).__name__)
        except Exception as exc:
            # Returning None here widened the scope to everything.  A supplied
            # but unreadable spec is a setup error, never "bake it all".
            print("MACHINE|setup_error|bad_only_file path=%s err=%s" % (text, safe_text(exc)), flush=True)
            raise SystemExit(2)
        return {safe_text(x) for x in data}
    return {part.strip() for part in text.split(",") if part.strip()}


def done_list_path(folder: str, override: Any = None) -> str:
    """One done-list per scene, NOT per run: output_directory() mints a fresh
    rbx_pbr_smart<N>/ every invocation, so the list lives in the parent."""
    if override and override is not True:
        return safe_text(override)
    return os.path.join(os.path.dirname(os.path.abspath(folder)), DONE_LIST_NAME)


def _discard_done_list(path: str, why: str) -> Dict[str, Any]:
    """A done-list we cannot parse means starting from zero: every object is re-baked
    with no explanation.  Say so, and move the bad file aside -- otherwise the first
    save_done_list() of this run overwrites the only copy of the evidence."""
    message = ("done-list at %s is %s; starting a fresh list, so every object will be "
               "re-baked" % (path, why))
    kept = ""
    try:
        kept = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
        os.replace(path, kept)
        message += "; the unreadable file was kept as %s" % os.path.basename(kept)
    except Exception as exc:
        kept = ""
        message += "; it could not be set aside (%s) and will be overwritten" % safe_text(exc)
    if message not in _DONE_LIST_WARNINGS:
        _DONE_LIST_WARNINGS.append(message)
    print("RESUME WARNING: %s" % message, flush=True)
    print("MACHINE|WARN done_list_unreadable path=%s reason=%s kept=%s"
          % (path, safe_text(why), kept), flush=True)
    return {"version": DONE_LIST_SCHEMA, "objects": {}}


def load_done_list(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # No list yet: the ordinary first run.  Nothing to tell the user about.
        return {"version": DONE_LIST_SCHEMA, "objects": {}}
    except Exception as exc:
        return _discard_done_list(path, "unreadable (%s)" % safe_text(exc))
    if not (isinstance(data, dict) and isinstance(data.get("objects"), dict)):
        return _discard_done_list(path, "malformed (no 'objects' mapping)")
    if data.get("version") != DONE_LIST_SCHEMA:
        print(
            "RESUME: legacy done-list schema %r; OK entries will be revalidated/rerun"
            % data.get("version"),
            flush=True,
        )
    data["version"] = DONE_LIST_SCHEMA
    # main() and record_done() do int(entry["strikes"]) unguarded; a hand-edited or
    # half-written list with a null/garbage strike count would abort the whole run
    # with a TypeError before a single object is baked.  Normalize once, here.
    for entry in data["objects"].values():
        if not isinstance(entry, dict):
            continue
        try:
            entry["strikes"] = max(0, int(entry.get("strikes", 0)))
        except (TypeError, ValueError):
            entry["strikes"] = 0
    return data


def save_done_list(path: str, data: Dict[str, Any]) -> None:
    """tmp + os.replace, written only AFTER the item's outputs are verified, so
    a done-list entry always means "on disk" and a SIGKILL cannot corrupt it."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        # Not cosmetic: nothing baked in this run will be resumable, so the next run
        # silently redoes all of it.  MACHINE|WARN alone never reaches the add-on UI.
        message = "done-list could not be written to %s: %s" % (path, safe_text(exc))
        if message not in _DONE_LIST_WARNINGS:
            _DONE_LIST_WARNINGS.append(message)
        print("RESUME WARNING: %s -- this run will not be resumable." % message, flush=True)
        print("MACHINE|WARN done_list_write_failed path=%s err=%s" % (path, safe_text(exc)))


def record_done(done: Dict[str, Any], path: str, name: str, entry: Dict[str, Any]) -> None:
    if entry.get("status") == "OK":
        output = bpy.data.objects.get(entry.get("output_object", ""))
        if output is None:
            raise RuntimeError("UV validation requires the final output object")
        entry["uv_validation"] = require_output_uv(output, entry.get("route", ""))
        if not entry.get("dependency_validation", {}).get("ok"):
            raise RuntimeError("Required dependency validation is absent from done entry")
    previous = done["objects"].get(name, {})
    strikes = int(previous.get("strikes", 0))
    done["version"] = DONE_LIST_SCHEMA
    entry["resume_schema"] = DONE_ENTRY_SCHEMA
    entry["strikes"] = 0 if entry.get("status") == "OK" else strikes + 1
    entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    done["objects"][name] = entry
    save_done_list(path, done)


def checkpoint_done_entry(done: Dict[str, Any], path: str, name: str, output_obj: Any) -> None:
    """M1-B: persist a just-recorded OK part as a durable checkpoint generation.

    Never fails the part: an output that cannot be captured faithfully keeps its
    OK entry (live-object resume still works) and says why it has no checkpoint,
    visibly, as a resume warning.
    """
    entry = done["objects"].get(name)
    if not DURABLE_CHECKPOINTS or not entry or entry.get("status") != "OK":
        return
    try:
        store = durable_checkpoints.object_store_dir(
            durable_checkpoints.checkpoint_store_root(entry["folder"]), name)
        entry["checkpoint"] = durable_checkpoints.write_checkpoint(
            store, output_obj, entry, source_name=name)
        entry.pop("checkpoint_unavailable", None)
    except Exception as exc:
        entry.pop("checkpoint", None)
        entry["checkpoint_unavailable"] = safe_text(exc)
        message = "durable checkpoint unavailable for %s: %s" % (name, safe_text(exc))
        if message not in _DONE_LIST_WARNINGS:
            _DONE_LIST_WARNINGS.append(message)
        print("MACHINE|WARN checkpoint_unavailable obj=%s err=%s" % (name, safe_text(exc)),
              flush=True)
    save_done_list(path, done)


def _digest_token(hasher: Any, *values: Any) -> None:
    for value in values:
        data = safe_text(value, "").encode("utf-8", "surrogatepass")
        hasher.update(struct.pack(">Q", len(data)))
        hasher.update(data)


def _simple_rna_value(value: Any) -> Optional[Any]:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "bl_rna") and hasattr(value, "name"):
        return [value.__class__.__name__, safe_text(value.name, "")]
    try:
        sequence = list(value)
    except (TypeError, ValueError, ReferenceError):
        return None
    if len(sequence) <= 32 and all(isinstance(item, (bool, int, float, str)) for item in sequence):
        return sequence
    return None


def _hash_rna_properties(hasher: Any, value: Any, excluded: Set[str]) -> None:
    try:
        properties = value.bl_rna.properties
    except Exception:
        return
    rows = []
    for prop in properties:
        name = prop.identifier
        if name in excluded or name == "rna_type":
            continue
        try:
            stable = _simple_rna_value(getattr(value, name))
        except Exception:
            continue
        if stable is not None:
            rows.append((name, stable))
    _digest_token(hasher, json.dumps(rows, sort_keys=True, separators=(",", ":")))


def _file_content_digest(path: str) -> Tuple[int, str]:
    normalized = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    stat = os.stat(normalized)
    cache_key = (normalized, int(stat.st_size), int(stat.st_mtime_ns))
    digest = _SOURCE_FILE_DIGEST_CACHE.get(cache_key)
    if digest is None:
        stream = hashlib.sha256()
        with open(normalized, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                stream.update(block)
        digest = stream.hexdigest()
        _SOURCE_FILE_DIGEST_CACHE[cache_key] = digest
    return int(stat.st_size), digest


def _packed_content_digest(packed: Any) -> Tuple[int, str]:
    data = packed.data
    return int(getattr(packed, "size", len(data))), hashlib.sha256(data).hexdigest()


def _live_image_pixel_digest(image: Any, width: int, height: int) -> str:
    """Hash the decoded pixels that IMAGE SourceRefs actually sample."""
    float_count = max(0, width * height * 4)
    byte_count = float_count * np.dtype(np.float32).itemsize
    max_bytes = 256 * 1024 * 1024  # one 4K RGBA float buffer; 8K safely disables resume
    if byte_count > max_bytes:
        raise RuntimeError(
            "image %s needs %.1f MiB for an exact live-pixel fingerprint; "
            "resume is disabled above %.0f MiB"
            % (
                datablock_name(image, "<image>"), byte_count / (1024.0 * 1024.0),
                max_bytes / (1024.0 * 1024.0),
            )
        )
    cacheable = (
        not bool(getattr(image, "is_dirty", False))
        and getattr(image, "source", "") != "GENERATED"
    )
    cache_key = (int(image.as_pointer()), width, height)
    if cacheable and cache_key in _LIVE_IMAGE_DIGEST_CACHE:
        return _LIVE_IMAGE_DIGEST_CACHE[cache_key]
    pixels = np.empty(float_count, dtype=np.float32)
    if float_count:
        image.pixels.foreach_get(pixels)
    digest = hashlib.sha256(memoryview(pixels).cast("B")).hexdigest()
    if cacheable:
        _LIVE_IMAGE_DIGEST_CACHE[cache_key] = digest
    return digest


def _hash_image_provenance(hasher: Any, image: Any) -> None:
    raw = safe_text(getattr(image, "filepath", ""), "")
    _digest_token(
        hasher, "image", datablock_name(image, ""), getattr(image, "source", ""), raw,
        safe_text(getattr(getattr(image, "colorspace_settings", None), "name", ""), ""),
        getattr(image, "alpha_mode", ""),
    )
    try:
        width, height = int(image.size[0]), int(image.size[1])
        _digest_token(hasher, width, height)
    except Exception:
        width, height = 0, 0
        _digest_token(hasher, "size-unreadable")

    # Generated images remain pixel-defined even if a later finalizer packs
    # them.  Hash the pixels on both sides of that state change.
    if getattr(image, "source", "") == "GENERATED":
        _digest_token(
            hasher, "generated", getattr(image, "generated_type", ""),
            _simple_rna_value(getattr(image, "generated_color", None)),
        )
        _digest_token(hasher, "live-pixels", _live_image_pixel_digest(image, width, height))
        return

    # IMAGE SourceRefs are consumed from the live datablock, not reloaded from
    # filepath.  An edited FILE image can therefore differ from both its packed
    # bytes and disk file.  Refuse to mint a reusable entry instead of silently
    # fingerprinting the wrong pixels (the bake itself is still allowed).
    if bool(getattr(image, "is_dirty", False)):
        raise RuntimeError(
            "image %s has unsaved pixel edits; save/repack it before using resume"
            % datablock_name(image, "<image>")
        )

    # Disk/packed bytes are provenance, but Blender's already-loaded decoded
    # buffer is the effective bake input.  Hash both so replacing a file without
    # reloading Blender cannot mint metadata for pixels that were never baked.
    _digest_token(hasher, "live-pixels", _live_image_pixel_digest(image, width, height))

    try:
        absolute = bpy.path.abspath(raw) if raw else ""
    except Exception:
        absolute = raw
    if absolute and "<UDIM>" in absolute:
        numbers = [
            int(getattr(tile, "number", 1001)) for tile in getattr(image, "tiles", [])
        ] or [1001]
        paths = [absolute.replace("<UDIM>", str(number)) for number in numbers]
    else:
        paths = [absolute] if absolute else []

    packed_items = list(getattr(image, "packed_files", ()))
    packed = getattr(image, "packed_file", None)
    if packed_items or packed is not None:
        try:
            if not packed_items:
                packed_items = [None]
            rows = []
            for index, item in enumerate(packed_items):
                payload = getattr(item, "packed_file", None) if item is not None else packed
                if payload is None:
                    raise RuntimeError("packed item has no payload")
                size, digest = _packed_content_digest(payload)
                if len(packed_items) == 1 and len(paths) == 1:
                    locator_path = paths[0]
                else:
                    locator_path = safe_text(getattr(item, "filepath", ""), "")
                    try:
                        locator_path = bpy.path.abspath(locator_path) if locator_path else ""
                    except Exception:
                        pass
                    if not locator_path and index < len(paths):
                        locator_path = paths[index]
                locator = (
                    os.path.normcase(os.path.realpath(os.path.abspath(locator_path)))
                    if locator_path else ""
                )
                rows.append((locator, size, digest))
            # Use the same token shape/order as external files.  pack_all() then
            # leaves fingerprints unchanged when it packs identical bytes, while
            # deliberately different packed payloads win over stale disk files.
            for locator, size, digest in sorted(rows):
                _digest_token(hasher, "content", locator, size, digest)
        except Exception as exc:
            raise RuntimeError(
                "could not fingerprint packed image %s: %s"
                % (datablock_name(image, "<image>"), safe_text(exc))
            )
        return


    disk_sources = sorted(path for path in paths if os.path.isfile(path))
    if disk_sources:
        for path in disk_sources:
            normalized = os.path.normcase(os.path.realpath(os.path.abspath(path)))
            size, digest = _file_content_digest(normalized)
            _digest_token(hasher, "content", normalized, size, digest)
        return

    _digest_token(hasher, "missing-content", [os.path.abspath(path) for path in paths])


def _hash_node_tree(hasher: Any, tree: Any, seen: Set[int]) -> None:
    if tree is None:
        _digest_token(hasher, "no-node-tree")
        return
    pointer = int(tree.as_pointer())
    if pointer in seen:
        _digest_token(hasher, "node-tree-reference", datablock_name(tree, ""))
        return
    seen.add(pointer)
    _digest_token(hasher, "node-tree", datablock_name(tree, ""))
    nodes = sorted(list(tree.nodes), key=lambda node: (safe_text(node.name, ""), node.type))
    excluded = {
        "name", "label", "location", "width", "width_hidden", "height", "dimensions",
        "select", "show_options", "show_preview", "show_texture", "parent", "inputs", "outputs",
        "internal_links", "node_tree", "image", "color", "warning_propagation",
    }
    for node in nodes:
        _digest_token(hasher, "node", node.type, safe_text(node.name, ""), bool(node.mute))
        _hash_rna_properties(hasher, node, excluded)
        image = getattr(node, "image", None)
        if image is not None:
            _hash_image_provenance(hasher, image)
            image_user = getattr(node, "image_user", None)
            if image_user is not None:
                _hash_rna_properties(hasher, image_user, set())
        referenced_object = getattr(node, "object", None)
        if referenced_object is not None:
            try:
                matrix = [
                    [float(value) for value in row]
                    for row in referenced_object.matrix_world
                ]
            except Exception:
                matrix = []
            _digest_token(
                hasher, "node-object-dependency", datablock_name(referenced_object, ""),
                json.dumps(matrix, separators=(",", ":")),
                _simple_rna_value(getattr(referenced_object, "color", None)),
                getattr(referenced_object, "pass_index", None),
            )
        color_ramp = getattr(node, "color_ramp", None)
        if color_ramp is not None:
            _digest_token(
                hasher, "color-ramp", color_ramp.color_mode, color_ramp.interpolation,
                getattr(color_ramp, "hue_interpolation", ""),
            )
            for element in color_ramp.elements:
                _digest_token(
                    hasher, float(element.position),
                    json.dumps(list(element.color), separators=(",", ":")),
                )
        mapping = getattr(node, "mapping", None)
        if mapping is not None:
            _digest_token(
                hasher, "curve-mapping", getattr(mapping, "tone", ""),
                _simple_rna_value(getattr(mapping, "black_level", None)),
                _simple_rna_value(getattr(mapping, "white_level", None)),
            )
            for curve_index, curve in enumerate(getattr(mapping, "curves", ())):
                for point in curve.points:
                    _digest_token(
                        hasher, curve_index, _simple_rna_value(point.location),
                        getattr(point, "handle_type", ""),
                    )
        for direction, sockets in (("input", node.inputs), ("output", node.outputs)):
            for socket in sockets:
                if not hasattr(socket, "default_value"):
                    continue
                stable = _simple_rna_value(socket.default_value)
                if stable is not None:
                    identifier = getattr(socket, "identifier", None) or getattr(
                        socket, "name", ""
                    )
                    _digest_token(
                        hasher, direction, safe_text(identifier, ""), bool(socket.is_linked),
                        json.dumps(stable, separators=(",", ":")),
                    )
        subgroup = getattr(node, "node_tree", None)
        if subgroup is not None:
            _hash_node_tree(hasher, subgroup, seen)
    links = sorted(
        (
            safe_text(link.from_node.name, ""),
            safe_text(
                getattr(link.from_socket, "identifier", None)
                or getattr(link.from_socket, "name", ""), ""
            ),
            safe_text(link.to_node.name, ""),
            safe_text(
                getattr(link.to_socket, "identifier", None)
                or getattr(link.to_socket, "name", ""), ""
            ),
        )
        for link in tree.links
    )
    _digest_token(hasher, json.dumps(links, separators=(",", ":")))


def _hash_mesh_geometry(hasher: Any, mesh: Any, label: str) -> None:
    _digest_token(
        hasher, label, len(mesh.vertices), len(mesh.edges), len(mesh.loops), len(mesh.polygons)
    )
    def hash_array(name: str, values: Any) -> None:
        _digest_token(hasher, name, values.dtype.str, len(values))
        hasher.update(memoryview(values).cast("B"))

    if len(mesh.vertices):
        values = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", values)
        hash_array("vertices", values)
    if len(mesh.edges):
        values = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", values)
        hash_array("edges", values)
    if len(mesh.loops):
        values = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", values)
        hash_array("loops", values)
    if len(mesh.polygons):
        for property_name in ("loop_start", "loop_total", "material_index"):
            values = np.empty(len(mesh.polygons), dtype=np.int32)
            mesh.polygons.foreach_get(property_name, values)
            hash_array("polygons-" + property_name, values)
        values = np.empty(len(mesh.polygons), dtype=np.bool_)
        mesh.polygons.foreach_get("use_smooth", values)
        hash_array("polygons-use_smooth", values)
    if len(mesh.edges):
        for property_name in ("use_edge_sharp", "use_seam"):
            if not hasattr(mesh.edges[0], property_name):
                continue
            values = np.empty(len(mesh.edges), dtype=np.bool_)
            mesh.edges.foreach_get(property_name, values)
            hash_array("edges-" + property_name, values)
    _digest_token(
        hasher, "uv-active-index", getattr(mesh.uv_layers, "active_index", -1)
    )
    for uv_layer in mesh.uv_layers:
        _digest_token(
            hasher, "uv", safe_text(uv_layer.name, ""), len(uv_layer.data),
            bool(getattr(uv_layer, "active_render", False)),
            bool(getattr(uv_layer, "active_clone", False)),
        )
        values = np.empty(len(uv_layer.data) * 2, dtype=np.float32)
        uv_layer.data.foreach_get("uv", values)
        hash_array("uv-data", values)

    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is not None:
        _digest_token(
            hasher, "color-attribute-selection",
            getattr(color_attributes, "active_color_index", -1),
            getattr(color_attributes, "render_color_index", -1),
            datablock_name(getattr(color_attributes, "active_color", None), ""),
        )

    # Named attributes (including color attributes) are shader inputs in a
    # GRAPH_BAKE.  Hash them one at a time so even a large scene never retains
    # more than one temporary buffer.  Unknown future Blender attribute types
    # disable resume for that object rather than allowing a false cache hit.
    attribute_specs = {
        "FLOAT": ("value", 1, np.float32),
        "INT": ("value", 1, np.int32),
        "FLOAT_VECTOR": ("vector", 3, np.float32),
        "FLOAT_COLOR": ("color", 4, np.float32),
        "BYTE_COLOR": ("color", 4, np.float32),
        "BOOLEAN": ("value", 1, np.bool_),
        "FLOAT2": ("vector", 2, np.float32),
        "INT8": ("value", 1, np.int32),
        "INT32_2D": ("value", 2, np.int32),
        "QUATERNION": ("value", 4, np.float32),
        "FLOAT4X4": ("value", 16, np.float32),
        # Blender 5.x stores custom split normals as INT16_2D "custom_normal";
        # without these two, every such mesh lost its done entry (M1, REVIEW_B 2).
        "INT16_2D": ("value", 2, np.int32),
        "FLOAT4": ("vector", 4, np.float32),
    }
    attributes = sorted(
        list(getattr(mesh, "attributes", ())),
        key=lambda item: (safe_text(item.name, ""), item.domain, item.data_type),
    )
    for attribute in attributes:
        _digest_token(
            hasher, "attribute", safe_text(attribute.name, ""),
            attribute.domain, attribute.data_type, len(attribute.data),
        )
        if attribute.data_type == "STRING":
            for element in attribute.data:
                _digest_token(hasher, safe_text(getattr(element, "value", ""), ""))
            continue
        spec = attribute_specs.get(attribute.data_type)
        if spec is None:
            raise RuntimeError(
                "unsupported mesh attribute type %s on %s"
                % (attribute.data_type, safe_text(attribute.name, "<attribute>"))
            )
        property_name, width, dtype = spec
        values = np.empty(len(attribute.data) * width, dtype=dtype)
        if len(values):
            attribute.data.foreach_get(property_name, values)
        hash_array("attribute-data", values)

    # Corner normals include custom/split normals and the result of smooth/sharp
    # flags.  They affect normal bakes even when no named attribute exposes them.
    try:
        corner_normals = mesh.corner_normals
        _digest_token(hasher, "corner-normals", len(corner_normals))
        if len(corner_normals):
            values = np.empty(len(corner_normals) * 3, dtype=np.float32)
            corner_normals.foreach_get("vector", values)
            hash_array("corner-normal-data", values)
    except Exception as exc:
        raise RuntimeError("could not fingerprint corner normals: %s" % safe_text(exc))


def _hash_object_deform_state(hasher: Any, obj: Any) -> None:
    """Hash skin weights and morph data that survive into exported outputs."""
    groups = sorted(list(obj.vertex_groups), key=lambda group: int(group.index))
    _digest_token(
        hasher,
        "vertex-group-definitions",
        json.dumps(
            [
                [int(group.index), safe_text(group.name, ""), bool(group.lock_weight)]
                for group in groups
            ],
            separators=(",", ":"),
        ),
    )
    hasher.update(b"vertex-group-weights-v1\x00")
    for vertex in obj.data.vertices:
        memberships = sorted(vertex.groups, key=lambda membership: int(membership.group))
        hasher.update(struct.pack(">II", int(vertex.index), len(memberships)))
        for membership in memberships:
            hasher.update(
                struct.pack(">If", int(membership.group), float(membership.weight))
            )

    shape_keys = getattr(obj.data, "shape_keys", None)
    if shape_keys is None:
        _digest_token(hasher, "no-shape-keys")
        return
    _digest_token(
        hasher, "shape-keys", datablock_name(shape_keys, ""),
        bool(getattr(shape_keys, "use_relative", True)),
        getattr(shape_keys, "eval_time", None),
    )
    for block in shape_keys.key_blocks:
        _digest_token(
            hasher, "shape-key", safe_text(block.name, ""),
            safe_text(getattr(getattr(block, "relative_key", None), "name", ""), ""),
            getattr(block, "interpolation", ""), bool(getattr(block, "mute", False)),
            getattr(block, "slider_min", None), getattr(block, "slider_max", None),
            getattr(block, "value", None), getattr(block, "vertex_group", ""),
            len(block.data),
        )
        values = np.empty(len(block.data) * 3, dtype=np.float32)
        if len(values):
            block.data.foreach_get("co", values)
        _digest_token(hasher, "shape-key-co", values.dtype.str, len(values))
        hasher.update(memoryview(values).cast("B"))


def _hash_source_ref(hasher: Any, semantic: str, source: SourceRef) -> None:
    _digest_token(hasher, "resolved-source", semantic, source.kind, source.key, source.component)
    if source.kind == "DISK":
        normalized = os.path.normcase(os.path.realpath(os.path.abspath(source.key)))
        try:
            size, digest = _file_content_digest(normalized)
            _digest_token(hasher, "content", normalized, size, digest)
        except OSError as exc:
            _digest_token(hasher, "missing", normalized, getattr(exc, "errno", ""))
        return
    image = bpy.data.images.get(source.key)
    if image is None:
        _digest_token(hasher, "missing-image", source.key)
        return
    _hash_image_provenance(hasher, image)


def _hash_resolved_sources(hasher: Any, obj: Any, source_index: Dict[str, AtlasSet]) -> None:
    for slot_index in used_material_slots(obj.data):
        material = (
            obj.material_slots[slot_index].material
            if slot_index < len(obj.material_slots) else None
        )
        if material is None:
            _digest_token(hasher, "resolved-slot", slot_index, "missing-material")
            continue
        atlas, reason = resolve_material(material, source_index)
        _digest_token(
            hasher, "resolved-slot", slot_index, safe_text(reason, ""),
            datablock_name(atlas, "") if atlas is not None else "unresolved",
        )
        if atlas is None:
            continue
        _digest_token(
            hasher, atlas.name, atlas.method, atlas.confidence,
            round(float(atlas.rough_default), 8), round(float(atlas.metal_default), 8),
            json.dumps(list(atlas.notes), separators=(",", ":")),
        )
        for semantic in SOURCE_CHANNELS:
            source = atlas.maps.get(semantic)
            if source is None:
                _digest_token(hasher, "resolved-source", semantic, "default")
            else:
                _hash_source_ref(hasher, semantic, source)


def source_fingerprint(
    obj: Any, source_index: Optional[Dict[str, AtlasSet]] = None
) -> str:
    """Digest one source object without retaining scene-sized buffers."""
    hasher = hashlib.sha256()
    _digest_token(hasher, "source-v2", datablock_name(obj, ""), obj.type)
    _digest_token(
        hasher,
        json.dumps(
            [[float(value) for value in row] for row in obj.matrix_world],
            separators=(",", ":"),
        ),
        _simple_rna_value(getattr(obj, "color", None)),
        getattr(obj, "pass_index", None),
    )
    custom_properties = []
    try:
        for key in sorted(obj.keys()):
            stable = _simple_rna_value(obj[key])
            if stable is not None:
                custom_properties.append((safe_text(key, ""), stable))
    except Exception:
        pass
    _digest_token(
        hasher,
        "object-custom-properties",
        json.dumps(custom_properties, sort_keys=True, separators=(",", ":")),
    )
    _hash_mesh_geometry(hasher, obj.data, "source-mesh")
    _hash_object_deform_state(hasher, obj)
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        _digest_token(hasher, "material-slot", index, datablock_name(material, "<empty>"))
        if material is not None:
            _digest_token(
                hasher, bool(getattr(material, "use_nodes", False)),
                _simple_rna_value(getattr(material, "diffuse_color", None)),
                getattr(material, "metallic", None), getattr(material, "roughness", None),
                getattr(material, "surface_render_method", ""),
                getattr(material, "blend_method", ""),
                getattr(material, "alpha_threshold", None),
                getattr(material, "pass_index", None),
                bool(getattr(material, "use_backface_culling", False)),
            )
            _hash_node_tree(hasher, getattr(material, "node_tree", None), set())
    modifier_excluded = {
        "name", "type", "show_expanded", "show_on_cage", "show_in_editmode",
        "show_viewport", "show_render", "show_in_editmode", "execution_time",
    }
    for modifier in obj.modifiers:
        _digest_token(
            hasher, "modifier", modifier.type, safe_text(modifier.name, ""),
            bool(modifier.show_viewport), bool(modifier.show_render),
        )
        _hash_rna_properties(hasher, modifier, modifier_excluded)
    evaluated = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_obj = obj.evaluated_get(depsgraph)
        evaluated = evaluated_obj.to_mesh()
        if evaluated is not None:
            _hash_mesh_geometry(hasher, evaluated, "evaluated-mesh")
    except Exception as exc:
        _digest_token(hasher, "evaluated-mesh-unavailable", safe_text(exc))
    finally:
        if evaluated is not None:
            try:
                evaluated_obj.to_mesh_clear()
            except Exception:
                pass
    if source_index is not None:
        _hash_resolved_sources(hasher, obj, source_index)
    return hasher.hexdigest()


def output_object_fingerprint(obj: Any) -> str:
    """Digest the actual Blender object that will be finalized/exported."""
    if obj is None or getattr(obj, "type", None) != "MESH" or obj.data is None:
        raise RuntimeError("recorded output is not a mesh object")
    return source_fingerprint(obj, None)


def config_fingerprint() -> str:
    """Hash only settings that can change generated geometry, UVs, or maps."""
    payload = {
        "engine_md5": tool_file_md5(),
        "tool_version": TOOL_VERSION,
        "scene_frame": [
            int(getattr(bpy.context.scene, "frame_current", 0)),
            float(getattr(bpy.context.scene, "frame_subframe", 0.0)),
        ],
        "route_mode": ROUTE_MODE,
        "output_profile": output_contract.build_output_contract(OUTPUT_PROFILE)["profile"],
        "target_profile": TARGET_PROFILE,
        "roblox_texture_limit": ROBLOX_TEXTURE_LIMIT,
        "resolution": {
            "min": MIN_RES, "bake": RES, "max": MAX_RES,
            "density_min": DENSITY_MIN_RES, "mode": RES_MODE,
            "texels_full_uv": TEXELS_FULL_UV,
            "graph_texels_full_uv": GRAPH_TEXELS_FULL_UV,
            "bake_density_scale": BAKE_DENSITY_SCALE,
            "pot_snap": POT_SNAP,
        },
        "crop": {
            "min_fill": CROP_MIN_FILL, "uv_outside_tol": UV_OUTSIDE_TOL,
            "force_square": FORCE_SQUARE_CROPS,
            "source_padding": CROP_PAD_SOURCE_PX,
            "split_prepass": CROP_SPLIT_PREPASS,
        },
        "bake": {
            "margin": BAKE_MARGIN_PX,
            "auto_unwrap": AUTO_UNWRAP_NO_UV,
            "pack_mra": PACK_MRA_PROXY_BAKE,
            "pack_mra_graph": PACK_MRA_GRAPH_BAKE,
            "proxy_extension": PROXY_IMAGE_EXTENSION,
            "source_normal_is_directx": SOURCE_NORMAL_IS_DIRECTX,
        },
        "mesh": {
            "strip_vertex_colors": STRIP_VERTEX_COLORS,
            "triangle_budget": TRI_BUDGET,
            "enforce_triangle_budget": ENFORCE_TRI_BUDGET,
            "decimate_ratio": DECIMATE_RATIO,
            "include_decals": INCLUDE_DECALS,
        },
        "defaults": {
            "metal": DEFAULT_METAL, "rough": DEFAULT_ROUGH,
            "normal": list(DEFAULT_NORMAL), "map_resolution": DEFAULT_MAP_RES,
        },
        "discovery": {
            "source_dirs": [
                os.path.normcase(os.path.realpath(os.path.abspath(path)))
                for path in SOURCE_DIRS
            ],
            "parent_levels": SOURCE_PARENT_LEVELS,
            "folder_names": list(SOURCE_FOLDER_NAMES),
            "index_packed_images": INDEX_PACKED_IMAGES,
            "minimum_color_size": MIN_COLOR_SIZE,
            "material_atlas_rules": MATERIAL_ATLAS_RULES,
            "suffix_rules": SUFFIX_RULES,
            # v1.1.1a: the multi-channel table is part of how a set was grouped,
            # so it belongs in the same config digest as the single-channel one.
            # Added as a NEW key -- "suffix_rules" keeps its exact old meaning.
            "multi_suffix_rules": MULTI_SUFFIX_RULES,
            "extra_sets": EXTRA_SETS,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _png_fingerprint(path: str) -> Dict[str, Any]:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        header = handle.read(24)
        hasher.update(header)
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise RuntimeError("output is not a valid PNG container: %s" % path)
    width, height = struct.unpack(">II", header[16:24])
    return {
        "sha256": hasher.hexdigest(), "size": os.path.getsize(path),
        "width": int(width), "height": int(height),
    }


def output_fingerprints(folder: str, file_bases: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for file_base in file_bases:
        for semantic in CHANNELS:
            path = output_path(folder, file_base, semantic)
            result[os.path.basename(path)] = _png_fingerprint(path)
    return result


def resume_metadata(
    source: Any, folder: str, file_bases: Sequence[str],
    source_index: Dict[str, AtlasSet], output_object: Any,
) -> Dict[str, Any]:
    return {
        "dependency_validation": require_material_dependencies(used_materials([source])),
        "source_fingerprint": source_fingerprint(source, source_index),
        "config_fingerprint": config_fingerprint(),
        "output_fingerprints": output_fingerprints(folder, file_bases),
        "output_object_fingerprint": output_object_fingerprint(output_object),
    }


def local_object(name: Optional[str]) -> Optional[Any]:
    """The LOCAL object called ``name``: generated and restored outputs are always
    local, and a linked object may share the name (M1 final review finding 8)."""
    if not name:
        return None
    return bpy.data.objects.get((name, None))


def capability_decision(
    source: Any, capability_cache: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """M1-A: the live capability decision for one source under the current policy.

    Read-only.  The decision is found by (name, library), never by bare name;
    None means no decision exists for this object and callers must refuse.
    """
    return material_capabilities.decision_for(
        material_capabilities.object_decisions(
            material_capabilities.analyze_objects(
                [source], CAPABILITY_PROFILE, cache=capability_cache),
            ALLOW_APPROXIMATION),
        source)


def validate_done_entry(
    entry: Dict[str, Any], source: Optional[Any] = None,
    source_index: Optional[Dict[str, AtlasSet]] = None,
    capability_cache: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Verify a resume entry still has both its Blender object and PNG sets."""
    if entry.get("resume_schema") != DONE_ENTRY_SCHEMA:
        return False, "legacy done entry lacks trustworthy census provenance"
    if source is None:
        return False, "current source object is unavailable for resume validation"
    if source_index is None:
        return False, "current source index is unavailable for resume validation"
    if not entry.get("uv_validation", {}).get("ok") or not entry.get("dependency_validation", {}).get("ok"):
        return False, "legacy done entry lacks UV/dependency validation"
    try:
        require_material_dependencies(used_materials([source]))
    except Exception as exc:
        return False, safe_text(exc)
    # An output converted under an approximation opt-in must not be carried
    # into a run whose policy refuses it; analysis is read-only and live.
    # The decision is found by (name, library) and a missing one fails closed.
    try:
        capability = capability_decision(source, capability_cache)
    except Exception as exc:
        return False, "could not analyze material capability: %s" % safe_text(exc)
    if capability is None or not capability["allowed"]:
        return False, "material capability %s under the current policy" % (
            (capability or {}).get("outcome", "UNKNOWN"))
    expected_config = safe_text(entry.get("config_fingerprint"), "")
    try:
        current_config = config_fingerprint()
    except Exception as exc:
        return False, "could not fingerprint run configuration: %s" % safe_text(exc)
    if not expected_config or expected_config != current_config:
        return False, "run configuration changed since the recorded output"
    expected_source = safe_text(entry.get("source_fingerprint"), "")
    try:
        current_source = source_fingerprint(source, source_index)
    except Exception as exc:
        return False, "could not fingerprint current source: %s" % safe_text(exc)
    if not expected_source or expected_source != current_source:
        return False, "source geometry, materials, images, or modifiers changed"
    if "triangles" not in entry:
        return False, "done entry lacks final triangle-count provenance"
    required_census = ("black", "black_reason", "magenta", "magenta_reason")
    missing_metadata = [key for key in required_census if key not in entry]
    if entry.get("route") != "CROP_SPLIT" and "materials" not in entry:
        missing_metadata.append("materials")
    if missing_metadata:
        return False, "done entry lacks census metadata: %s" % ",".join(missing_metadata)
    output_name = safe_text(entry.get("output_object"), "")
    output_obj = bpy.data.objects.get(output_name) if output_name else None
    if output_obj is None:
        return False, "recorded output object is absent from this blend"
    if getattr(output_obj, "type", None) != "MESH" or output_obj.data is None:
        return False, "recorded output object is no longer a mesh"
    expected_object = safe_text(entry.get("output_object_fingerprint"), "")
    try:
        require_output_uv(output_obj, entry.get("route", ""))
        current_object = output_object_fingerprint(output_obj)
        current_triangles = triangle_count(output_obj)
    except Exception as exc:
        return False, "could not fingerprint recorded output object: %s" % safe_text(exc)
    if not expected_object or current_object != expected_object:
        return False, "recorded output geometry, materials, or modifiers changed"
    if int(entry.get("triangles", -1)) != current_triangles:
        return False, "recorded output triangle count changed"
    if bool(entry.get("triangle_budget_exceeded")) != (current_triangles > TRI_BUDGET):
        return False, "recorded triangle-budget verdict is stale"
    if entry.get("missing_channels"):
        return False, "done entry already records missing channels"
    folder = safe_text(entry.get("folder"), "")
    bases = entry.get("file_bases") or ([entry.get("file_base")] if entry.get("file_base") else [])
    if not folder or not bases:
        return False, "done entry lacks output folder/file base provenance"
    for file_base in bases:
        missing = verify_part_outputs(folder, safe_text(file_base, ""))
        if missing:
            return False, "%s is missing %s" % (file_base, ",".join(missing))
    expected_outputs = entry.get("output_fingerprints")
    if not isinstance(expected_outputs, dict) or not expected_outputs:
        return False, "done entry lacks output-content fingerprints"
    try:
        current_outputs = output_fingerprints(folder, [safe_text(base, "") for base in bases])
    except Exception as exc:
        return False, "could not fingerprint recorded outputs: %s" % safe_text(exc)
    if current_outputs != expected_outputs:
        return False, "recorded output PNG content or dimensions changed"
    return True, ""


def materialize_resume_outputs(
    entry: Dict[str, Any], destination_folder: str
) -> Dict[str, int]:
    """Make a carried item part of this run's self-contained output folder."""
    source_folder = os.path.abspath(safe_text(entry.get("folder"), ""))
    destination_folder = os.path.abspath(destination_folder)
    if os.path.normcase(source_folder) == os.path.normcase(destination_folder):
        return {"linked": 0, "copied": 0}
    bases = entry.get("file_bases") or (
        [entry.get("file_base")] if entry.get("file_base") else []
    )
    created: List[str] = []
    image_paths_before: Dict[int, Tuple[Any, str]] = {}
    entry_before = {
        "folder": entry.get("folder"),
        "output_fingerprints": entry.get("output_fingerprints"),
        "output_object_fingerprint": entry.get("output_object_fingerprint"),
    }
    counts = {"linked": 0, "copied": 0}
    try:
        for file_base in bases:
            for semantic in CHANNELS:
                source_path = output_path(source_folder, safe_text(file_base, ""), semantic)
                destination_path = output_path(
                    destination_folder, safe_text(file_base, ""), semantic
                )
                if os.path.lexists(destination_path):
                    raise RuntimeError(
                        "resume refuses to overwrite %s" % os.path.basename(destination_path)
                    )
                created.append(destination_path)
                # A copy is intentionally used instead of a hardlink: a later
                # edit in either run must never mutate the other run's inode.
                shutil.copy2(source_path, destination_path)
                counts["copied"] += 1
        current = output_fingerprints(
            destination_folder, [safe_text(base, "") for base in bases]
        )
        if current != entry.get("output_fingerprints"):
            raise RuntimeError("materialized resume PNG fingerprints do not match")

        output_obj = bpy.data.objects.get(safe_text(entry.get("output_object"), ""))
        destinations = {
            os.path.basename(output_path(destination_folder, safe_text(base, ""), semantic)):
            output_path(destination_folder, safe_text(base, ""), semantic)
            for base in bases for semantic in CHANNELS
        }
        if output_obj is not None:
            for slot in output_obj.material_slots:
                material = slot.material
                if material is None or not getattr(material, "use_nodes", False):
                    continue
                for tree in iter_node_trees(material.node_tree):
                    for node in tree.nodes:
                        image = getattr(node, "image", None)
                        if image is None:
                            continue
                        raw = safe_text(getattr(image, "filepath", ""), "")
                        destination = destinations.get(os.path.basename(raw))
                        if destination:
                            pointer = int(image.as_pointer())
                            image_paths_before.setdefault(pointer, (image, raw))
                            image.filepath = destination
        entry["folder"] = destination_folder
        entry["output_fingerprints"] = current
        if output_obj is None:
            raise RuntimeError("recorded output object disappeared during materialization")
        entry["output_object_fingerprint"] = output_object_fingerprint(output_obj)
        return counts
    except Exception:
        entry.update(entry_before)
        for image, raw in image_paths_before.values():
            try:
                image.filepath = raw
            except Exception:
                pass
        for path in reversed(created):
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError:
                pass
        raise


# -------------------- black classification and output verification -------------

def _socket_is_lit(socket: Any) -> bool:
    """True when a shader input can contribute non-zero colour."""
    if socket is None:
        return False
    if socket.is_linked:
        return True
    try:
        value = socket.default_value
        if isinstance(value, (int, float)):
            return float(value) > BLACK_MAX_LEVEL
        return max(float(x) for x in tuple(value)[:3]) > BLACK_MAX_LEVEL
    except Exception:
        return False


_DIFFUSE_NODE_TYPES = {
    "BSDF_DIFFUSE", "BSDF_GLOSSY", "BSDF_GLASS", "BSDF_REFRACTION", "BSDF_TOON",
    "BSDF_VELVET", "BSDF_SHEEN", "BSDF_TRANSLUCENT", "BSDF_ANISOTROPIC",
    "SUBSURFACE_SCATTERING", "BSDF_HAIR", "BSDF_HAIR_PRINCIPLED",
}


def material_emission_only(material: Any) -> Tuple[bool, str]:
    """Does this material provably have no diffuse colour contribution?

    Used only to explain an already-black colour bake.  Emission-driven parts
    (lights, exit signs) legitimately bake an all-black ALB map.
    """
    if material is None:
        return True, "empty material slot"
    if not getattr(material, "use_nodes", False) or material.node_tree is None:
        return False, "nodes disabled (cannot prove emission-only)"
    emissive = False
    diffuse = False
    try:
        for tree in iter_node_trees(material.node_tree):
            for node in tree.nodes:
                ntype = node.type
                if ntype == "EMISSION":
                    strength = node.inputs.get("Strength")
                    if _socket_is_lit(node.inputs.get("Color")) and (
                            strength is None or _socket_is_lit(strength)):
                        emissive = True
                elif ntype == "BSDF_PRINCIPLED":
                    if _socket_is_lit(node.inputs.get("Base Color")):
                        diffuse = True
                    emission_socket = (node.inputs.get("Emission Color")
                                       or node.inputs.get("Emission"))
                    strength = node.inputs.get("Emission Strength")
                    if _socket_is_lit(emission_socket) and (
                            strength is None or _socket_is_lit(strength)):
                        emissive = True
                elif ntype in _DIFFUSE_NODE_TYPES:
                    if _socket_is_lit(node.inputs.get("Color")):
                        diffuse = True
    except Exception as exc:
        return False, "graph walk failed: %s" % safe_text(exc)
    if diffuse:
        return False, "material has a lit diffuse input"
    if emissive:
        return True, "emission-only shader; no diffuse contribution"
    return True, "no lit colour input anywhere in the graph"


def classify_black(job: Job, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Split an all-black colour bake into BLACK_OK (provable) and BLACK_SUSPECT.

    Deliberately conservative: the portal13 census found that EVERY black bake
    that was actually investigated traced to a real defect (face-slot reset or
    a degenerate source UV), so the two known defect signatures are reported as
    SUSPECT even when the material graph would otherwise excuse them.
    """
    stats = ((detail.get("maps") or {}).get("color") or {}).get("stats")
    if not isinstance(stats, dict) or "max" not in stats:
        return None                       # CROP route copies source pixels: no bake stats
    try:
        if float(stats["max"]) > BLACK_MAX_LEVEL:
            return None
    except Exception:
        return None
    if detail.get("uv_repaired"):
        return {"black": "BLACK_SUSPECT",
                "black_reason": "still black after degenerate-source-UV repair"}
    if getattr(job.uv, "triangle_area", 1.0) <= UV_DEGENERATE_AREA:
        return {"black": "BLACK_SUSPECT",
                "black_reason": "collapsed source UV (degenerate_source_uv mechanism)"}
    if job.route == "GRAPH_BAKE" and len(job.used_slots) > 1:
        return {"black": "BLACK_SUSPECT",
                "black_reason": "multi-material GRAPH_BAKE: face-slot-reset signature, "
                                "needs a pixel-diff before it is believed"}
    if not BENIGN_BLACK:
        return {"black": "BLACK_SUSPECT", "black_reason": "colour bake is numerically black"}
    verdicts = [material_emission_only(job.materials_by_slot.get(slot))
                for slot in job.used_slots] or [(False, "no used material slots")]
    if all(ok for ok, _ in verdicts):
        return {"black": "BLACK_OK", "black_reason": verdicts[0][1]}
    return {"black": "BLACK_SUSPECT",
            "black_reason": "; ".join(sorted({why for ok, why in verdicts if not ok}))}


def classify_magenta(job: Job, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """v3.3: Blender paints (1,0,1) wherever a source image failed to load. With precheck bypassed (or a
    file that exists but does not decode) the bake 'succeeds' and the census said CLEAN. Two signals:
    the colour bake's per-channel means, and any image in the job's materials that Blender could not load."""
    reasons = []
    stats = ((detail.get("maps") or {}).get("color") or {}).get("stats")
    mean_rgb = stats.get("mean_rgb") if isinstance(stats, dict) else None
    if mean_rgb and len(mean_rgb) == 3:
        r, g, b = mean_rgb
        if r >= MAGENTA_MIN_RB and b >= MAGENTA_MIN_RB and g <= MAGENTA_MAX_G:
            reasons.append("colour bake mean rgb=(%.2f,%.2f,%.2f) is the missing-image placeholder" % (r, g, b))
    unloadable = []
    seen = set()
    def walk(tree):
        if tree is None:
            return
        for node in tree.nodes:
            img = getattr(node, "image", None)
            if img is not None and img.name not in seen:
                seen.add(img.name)
                try:
                    if img.source == "FILE" and (img.size[0] == 0 or img.size[1] == 0):  # v3.3b: packed too
                        unloadable.append(img.name)
                except Exception:
                    pass
            sub = getattr(node, "node_tree", None)
            if sub is not None:
                walk(sub)
    for slot in job.used_slots:
        mat = job.materials_by_slot.get(slot)
        if mat is not None and getattr(mat, "use_nodes", False):
            walk(mat.node_tree)
    if unloadable:
        reasons.append("%d source image(s) could not be loaded: %s" % (len(unloadable), unloadable[:5]))
    if not reasons:
        return None
    return {"magenta": "MAGENTA_SUSPECT", "magenta_reason": "; ".join(reasons)}


def verify_part_outputs(folder: str, file_base: str) -> List[str]:
    """Every channel PNG must exist and be non-empty.  'Saved' is not 'worked'."""
    missing = []
    for semantic in CHANNELS:
        path = output_path(folder, file_base, semantic)
        try:
            if not os.path.exists(path) or os.path.getsize(path) <= 0:
                missing.append(semantic)
        except Exception:
            missing.append(semantic)
    return missing


# ---------------------- transactional output publication ----------------------
# v1.1.1: a part used to write its four PNGs one at a time, straight to their
# final names.  Anything that stopped it after the second map -- a bake
# exception, a full disk, a cancelled run -- left the first two behind as a
# partial set that is indistinguishable on disk from a complete one, and the
# collision check in main() then refuses to rebuild that base, so the orphans
# poison the retry.  Maps now land in a private staging directory inside the run
# folder and are moved into place only once all four exist and decode.
#
# The staging directory doubles as the journal: it is created before the first
# byte is written and removed only after the last os.replace() has landed, so
# finding one at the start of a later run means "a publish for this base did not
# finish".  recover_staged_parts() then deletes that base's destinations
# (published or not) and the directory, turning a half-published set back into
# no set at all -- the only state the collision check can safely proceed from.
STAGING_PREFIX = ".rbx_stage_"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_COLOR_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}  # PNG colour type -> samples


class PublishError(RuntimeError):
    """A staged output set failed verification or could not be moved into place."""


def staging_path(folder: str, file_base: str) -> str:
    """Private per-part directory, a direct child of the run folder.

    Same filesystem as the destinations by construction, which is what lets the
    publish step use os.replace() (a rename) instead of a copy.  The dot prefix
    keeps it out of build_disk_index(), which only looks at file extensions.
    """
    return os.path.join(folder, STAGING_PREFIX + file_base)


def begin_staging(folder: str, file_base: str) -> str:
    staging = staging_path(folder, file_base)
    if os.path.lexists(staging):
        # Only reachable if recover_staged_parts() could not clear it or a
        # second process is writing this folder.  Refuse rather than mix two
        # attempts' maps into one published set.
        raise PublishError(
            "staging directory already exists: %s" % os.path.basename(staging)
        )
    os.makedirs(staging)
    return staging


def _flush_to_disk(path: str) -> None:
    """Force a staged file's bytes out of the OS cache before it is published.

    write_png() closes its handle but never fsyncs, so a set could otherwise be
    renamed into place while its bytes were still only in the page cache.
    Windows: os.fsync() is _commit(), which needs a writable handle -- hence
    "rb+" rather than "rb".
    """
    try:
        with open(path, "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PublishError("cannot flush %s: %s" % (os.path.basename(path), safe_text(exc)))


def verify_png_file(path: str) -> Tuple[int, int]:
    """Decode a PNG structurally; raise PublishError if it is not a whole image.

    Existence and a non-zero size are not evidence: a write that died mid-IDAT
    leaves a decodable-looking prefix.  Every chunk CRC is checked and the IDAT
    stream is actually inflated and length-checked against IHDR, so a truncated
    or torn map cannot be published.
    """
    name = os.path.basename(path)
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        raise PublishError("cannot read %s: %s" % (name, safe_text(exc)))
    if not blob:
        raise PublishError("%s is empty" % name)
    if not blob.startswith(_PNG_SIGNATURE):
        raise PublishError("%s is not a PNG" % name)
    pos = len(_PNG_SIGNATURE)
    width = height = depth = 0
    color_type = -1
    idat: List[bytes] = []
    ended = False
    while pos + 8 <= len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        kind = blob[pos + 4:pos + 8]
        end = pos + 8 + length
        if end + 4 > len(blob):
            raise PublishError("%s is truncated inside a %s chunk" % (name, kind))
        data = blob[pos + 8:end]
        if struct.unpack(">I", blob[end:end + 4])[0] != (zlib.crc32(kind + data) & 0xFFFFFFFF):
            raise PublishError("%s has a corrupt %s chunk (CRC mismatch)" % (name, kind))
        if kind == b"IHDR":
            if length < 13:
                raise PublishError("%s has a short IHDR" % name)
            width, height, depth, color_type = struct.unpack(">IIBB", data[:10])
        elif kind == b"IDAT":
            idat.append(data)
        elif kind == b"IEND":
            ended = True
            break
        pos = end + 4
    if not ended:
        raise PublishError("%s has no IEND chunk" % name)
    channels = _PNG_COLOR_CHANNELS.get(color_type)
    if not width or not height or channels is None or depth not in (8, 16):
        raise PublishError(
            "%s has an unusable IHDR (%dx%d type=%d depth=%d)"
            % (name, width, height, color_type, depth)
        )
    if not idat:
        raise PublishError("%s has no image data" % name)
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise PublishError("%s does not decode: %s" % (name, safe_text(exc)))
    expected = height * (width * channels * (depth // 8) + 1)
    if len(raw) != expected:
        raise PublishError(
            "%s decoded to %d bytes, expected %d" % (name, len(raw), expected)
        )
    return width, height


def publish_staged_maps(folder: str, file_base: str, staging: str) -> List[str]:
    """Verify one part's staged maps, then move the whole set into `folder`.

    Returns the published paths.  Raises PublishError without moving anything if
    any map is missing or does not decode; the caller discards the attempt.
    """
    moves: List[Tuple[str, str]] = []
    for semantic in (CHANNELS + ("emissive",) if TARGET_PROFILE == "ROBLOX" else CHANNELS):
        source = output_path(staging, file_base, semantic)
        if not os.path.exists(source):
            raise PublishError("staged %s map was never written" % semantic)
        _flush_to_disk(source)
        verify_png_file(source)
        moves.append((source, output_path(folder, file_base, semantic)))
    for _source, destination in moves:
        # os.replace() overwrites silently, on Windows too, so "never overwrite
        # an existing output" has to be enforced here and not only at plan time.
        if os.path.lexists(destination):
            raise PublishError(
                "refusing to overwrite existing output: %s"
                % os.path.basename(destination)
            )
    published: List[str] = []
    try:
        for source, destination in moves:
            # Link without overwrite, on the same filesystem as the staging
            # directory. A concurrent collision cannot replace someone else's
            # file. The attempt owns only links it successfully created.
            os.link(source, destination)
            published.append(destination)
            os.remove(source)
    except BaseException as exc:
        exc.published_paths = list(published)
        raise
    return published


def finish_staging(staging: str) -> None:
    """Drop the marker directory once its whole set has been published."""
    try:
        os.rmdir(staging)  # empty by construction after a complete publish
    except OSError:
        try:
            # Something stray landed in it.  It must not survive: a leftover
            # marker makes the next run delete this part's good output set.
            shutil.rmtree(staging)
        except OSError as exc:
            print("MACHINE|WARN publish_staging_survived path=%s err=%s"
                  % (staging, safe_text(exc)), flush=True)


def discard_staged_part(folder: str, file_base: str, owned_paths=None) -> None:
    """Erase every trace of one part's publish attempt, finished or not.

    Destinations first, staging directory last: the directory is the marker that
    says "this base is mid-publish", so it has to outlive what it points at.
    Windows: an open handle blocks deletion -- nothing here holds one (write_png
    closes its file, and preview textures are only ever loaded from published
    paths, after publication), so a failure is reported, not worked around.
    """
    destinations = (list(owned_paths) if owned_paths is not None else
                    [output_path(folder, file_base, semantic)
                     for semantic in (CHANNELS + ("emissive",) if TARGET_PROFILE == "ROBLOX" else CHANNELS)])
    for destination in destinations:
        try:
            if os.path.lexists(destination):
                os.remove(destination)
        except OSError as exc:
            print("MACHINE|WARN publish_discard_output path=%s err=%s"
                  % (destination, safe_text(exc)), flush=True)
    staging = staging_path(folder, file_base)
    if not os.path.isdir(staging):
        return
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        print("MACHINE|WARN publish_discard_staging path=%s err=%s"
              % (staging, safe_text(exc)), flush=True)


def recover_staged_parts(folder: str) -> List[str]:
    """Clear every unfinished publish left in `folder` by an earlier run.

    A staging directory that outlived its run means the process died between the
    first and the last os.replace(): some of that base's four maps may be in
    place and some not, and nothing downstream can tell a half-published set
    from a complete one.  Delete the whole set so the part is simply rebuilt.
    """
    recovered: List[str] = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return recovered
    for name in names:
        if not name.startswith(STAGING_PREFIX):
            continue
        if not os.path.isdir(os.path.join(folder, name)):
            continue
        base = name[len(STAGING_PREFIX):]
        # safe_unique_name() is the only producer of output bases; anything else
        # under this prefix is not ours to delete.
        if not base or re.fullmatch(r"[A-Za-z0-9_-]+", base) is None:
            continue
        discard_staged_part(folder, base)
        recovered.append(base)
        print("MACHINE|publish_recovered base=%s (unfinished publish removed)"
              % base, flush=True)
    return recovered


# ------------------------------ crop-split route -------------------------------

class CropSplitRollbackError(RuntimeError):
    """Raised when a declined split cannot be restored to its exact snapshot."""


def isolate_slot(mesh: Any, keep_slot: int) -> None:
    """Delete every face whose material_index != keep_slot, in place."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    doomed = [f for f in bm.faces if f.material_index != keep_slot]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(mesh)
    bm.free()


def try_crop_split(obj: Any, source_index: Dict[str, AtlasSet], folder: str,
                   cache: PixelCache, collection: Any) -> Optional[Dict[str, Any]]:
    """Split-by-material CROP for one multi-material object.

    The stock CROP route only fires when a WHOLE object resolves to one shared
    atlas signature.  A multi-material mesh whose every slot individually
    resolves to a simple image atlas can still take the fast no-Cycles path:
    split by slot, crop each slot from its own atlas, rejoin under one name.
    Returns None -- leaving the ORIGINAL object completely untouched for the
    normal router -- if any precondition fails.
    """
    mesh = obj.data
    used = used_material_slots(mesh)
    if len(used) < 2:
        return None                       # single-material: stock CROP already handles it
    if obj.modifiers:
        # Joining independently isolated duplicates would retain only the active
        # duplicate's modifier stack and can silently change rigged/evaluated
        # geometry.  Let the normal whole-object route preserve the stack.
        return None
    for slot in used:
        if slot >= len(obj.material_slots):
            return None
        material = obj.material_slots[slot].material
        if material is None or not getattr(material, "use_nodes", False):
            return None
        atlas, _reason = resolve_material(material, source_index)
        if atlas is None:
            return None

    used_bases_before = set(_USED_BASES)
    file_base_root = safe_unique_name(datablock_name(obj, "mesh_object"))
    produced: List[Any] = []
    file_bases: List[str] = []
    created_paths: List[str] = []
    slot_verdicts: List[
        Tuple[int, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]
    ] = []
    original_active = bpy.context.view_layer.objects.active
    original_selected = list(bpy.context.selected_objects)
    before = {
        "objects": {int(block.as_pointer()) for block in bpy.data.objects},
        "meshes": {int(block.as_pointer()) for block in bpy.data.meshes},
        "materials": {int(block.as_pointer()) for block in bpy.data.materials},
        "images": {int(block.as_pointer()) for block in bpy.data.images},
    }
    committed = False

    def rollback() -> None:
        """Remove every filesystem/datablock side effect made by this attempt."""
        errors: List[str] = []
        _USED_BASES.clear()
        _USED_BASES.update(used_bases_before)
        for path in reversed(created_paths):
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError as exc:
                errors.append("remove file %s: %s" % (path, safe_text(exc)))
                print(
                    "MACHINE|WARN crop_split_rollback_file path=%s err=%s"
                    % (path, safe_text(exc)),
                    flush=True,
                )
        for label, blocks in (
            ("objects", bpy.data.objects),
            ("materials", bpy.data.materials),
            ("images", bpy.data.images),
            ("meshes", bpy.data.meshes),
        ):
            for block in list(blocks):
                try:
                    pointer = int(block.as_pointer())
                except Exception:
                    continue
                if pointer in before[label]:
                    continue
                try:
                    blocks.remove(block, do_unlink=True)
                except Exception as exc:
                    errors.append(
                        "remove %s %s: %s"
                        % (label, datablock_name(block, "<unknown>"), safe_text(exc))
                    )
                    print(
                        "MACHINE|WARN crop_split_rollback_datablock type=%s name=%s err=%s"
                        % (label, datablock_name(block, "<unknown>"), safe_text(exc)),
                        flush=True,
                    )
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for selected_obj in original_selected:
                if selected_obj.name in bpy.context.view_layer.objects:
                    selected_obj.select_set(True)
            if (
                original_active is not None
                and original_active.name in bpy.context.view_layer.objects
            ):
                bpy.context.view_layer.objects.active = original_active
        except Exception:
            errors.append("restore active object/selection failed")
        for path in created_paths:
            if os.path.lexists(path):
                errors.append("file survived rollback: %s" % path)
        for label, blocks in (
            ("objects", bpy.data.objects),
            ("meshes", bpy.data.meshes),
            ("materials", bpy.data.materials),
            ("images", bpy.data.images),
        ):
            survivors = []
            for block in blocks:
                try:
                    if int(block.as_pointer()) not in before[label]:
                        survivors.append(datablock_name(block, "<unknown>"))
                except Exception:
                    continue
            if survivors:
                errors.append("%s survived rollback: %s" % (label, ",".join(survivors[:8])))
        if errors:
            raise CropSplitRollbackError("; ".join(errors))

    try:
        for slot in used:
            _run_boundary("crop_split_slot", object=datablock_name(obj), slot=slot)
            dup = duplicate_mesh_object(
                obj, collection, "__SPLIT_%s_%d" % (file_base_root, slot)
            )
            isolate_slot(dup.data, slot)
            if len(dup.data.polygons) == 0:
                bpy.data.objects.remove(dup, do_unlink=True)
                continue
            jobs, _skipped = plan_jobs([dup], source_index)
            if not jobs or jobs[0].route != "CROP":
                return None
            job = jobs[0]
            job.file_base = safe_unique_name(file_base_root + "_slot%d" % slot)
            paths = [output_path(folder, job.file_base, semantic) for semantic in CHANNELS]
            collisions = [path for path in paths if os.path.lexists(path)]
            if collisions:
                raise RuntimeError(
                    "CROP_SPLIT refuses to overwrite existing output: %s"
                    % os.path.basename(collisions[0])
                )
            # v1.1.1: same transactional publication as the ordinary routes.
            # rollback() below still owns the published files; it now only ever
            # sees complete sets, and a hard kill mid-publish leaves the staging
            # marker for recover_staged_parts() instead of two orphan PNGs.
            staging = begin_staging(folder, job.file_base)
            try:
                detail = process_crop(job, dup, staging, cache)
                created_paths.extend(
                    publish_staged_maps(folder, job.file_base, staging)
                )
            except BaseException:
                discard_staged_part(folder, job.file_base)
                raise
            finish_staging(staging)
            slot_verdicts.append(
                (slot, classify_black(job, detail), classify_magenta(job, detail))
            )
            strip_colors(dup.data)
            assign_preview_material(dup, folder, job.file_base, bool(detail.get("alpha")))
            produced.append(dup)
            file_bases.append(job.file_base)

        if len(produced) < 2:
            return None

        bpy.ops.object.select_all(action="DESELECT")
        for other in produced:
            other.select_set(True)
        bpy.context.view_layer.objects.active = produced[0]
        join_result = bpy.ops.object.join()
        if "FINISHED" not in set(join_result):
            raise RuntimeError("CROP_SPLIT join returned %r" % (join_result,))
        joined = bpy.context.view_layer.objects.active
        if joined is None or joined not in bpy.data.objects[:]:
            raise RuntimeError("CROP_SPLIT join did not produce an active object")
        validation = require_output_uv(joined, "CROP_SPLIT")
        dependency = require_material_dependencies(used_materials([obj]))
        joined.name = "RBX_" + file_base_root
        force_visible(joined)
        committed = True
        result = {"object": datablock_name(obj, "mesh_object"),
                "output_object": joined.name,
                "status": "OK", "route": "CROP_SPLIT",
                "route_reason": "every material slot resolves to its own atlas; "
                                "split by slot and cropped without Cycles",
                "file_base": file_base_root, "file_bases": file_bases,
                "slots_split": len(file_bases), "joined_object": joined,
                "uv_validation": validation, "dependency_validation": dependency}
        black_suspects = [
            "slot%d: %s" % (slot, verdict.get("black_reason", ""))
            for slot, verdict, _magenta in slot_verdicts
            if verdict and verdict.get("black") == "BLACK_SUSPECT"
        ]
        if black_suspects:
            result["black"] = "BLACK_SUSPECT"
            result["black_reason"] = "; ".join(black_suspects)
        magenta_suspects = [
            "slot%d: %s" % (slot, verdict.get("magenta_reason", ""))
            for slot, _black, verdict in slot_verdicts
            if verdict and verdict.get("magenta") == "MAGENTA_SUSPECT"
        ]
        if magenta_suspects:
            result["magenta"] = "MAGENTA_SUSPECT"
            result["magenta_reason"] = "; ".join(magenta_suspects)
        return result
    finally:
        if not committed:
            rollback()


def crop_split_prepass(candidates: List[Any], source_index: Dict[str, AtlasSet],
                       folder: str, cache: PixelCache,
                       collection: Any, on_committed_result: Any = None
                       ) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """Route multi-material all-simple-slot objects through CROP_SPLIT.

    Returns (results, handled_objects).  Objects the pre-pass declines are
    untouched and fall through to the stock router in the same session.
    A callback publishes each completed object's resume/checkpoint bookkeeping
    before the next object starts. It runs outside the route-failure handler:
    a bookkeeping/cancellation exception must never reroute a committed object.
    """
    if not CROP_SPLIT_PREPASS or high_precision_output():
        return [], []
    multi = [o for o in candidates if len(used_material_slots(o.data)) >= 2]
    if not multi:
        return [], []
    print("CROP_SPLIT: %d multi-material candidate(s)" % len(multi), flush=True)
    results: List[Dict[str, Any]] = []
    handled: List[Any] = []
    for obj in multi:
        _run_boundary("crop_split_object", object=datablock_name(obj),
                      completed=len(results), total=len(multi))
        try:
            result = try_crop_split(obj, source_index, folder, cache, collection)
        except CropSplitRollbackError:
            raise
        except Exception as exc:
            print("CROP_SPLIT FAIL obj=%s err=%s" % (datablock_name(obj), safe_text(exc)))
            traceback.print_exc()
            result = None
        if result is None:
            continue
        handled.append(obj)
        results.append(result)
        if on_committed_result is not None:
            on_committed_result(result, obj)
        print("CROP_SPLIT OK obj=%s -> %s slots_split=%d"
              % (result["object"], result["output_object"], result["slots_split"]), flush=True)
    cache.clear()
    print("MACHINE|crop_split candidates=%d split_ok=%d" % (len(multi), len(results)), flush=True)
    return results, handled


# --------------------------------- finalize ------------------------------------

# ------------------------- in-session filepath ledger -------------------------
#
# precheck.run() may REPOINT an image datablock at a texture it located, which is
# the non-destructive way to resolve a missing reference: nothing is written to
# disk. But the repoint lives on the datablock, so anything that later saves the
# blend would persist it. Whoever repoints records the original here, and
# finalize restores it after packing and before saving.

def note_repointed_filepath(image_name: str, original: str) -> None:
    _REPOINTED_FILEPATHS.append((image_name, original))


def restore_repointed_filepaths() -> List[str]:
    """Put every repointed image path back. Returns the names restored."""
    done: List[str] = []
    for name, original in reversed(_REPOINTED_FILEPATHS):
        image = bpy.data.images.get(name)
        if image is None:
            continue
        try:
            image.filepath = original
            done.append(name)
        except Exception:
            pass
    _REPOINTED_FILEPATHS.clear()
    return done


def finalize_session(report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with stage("finalize"):
        try:
            return _finalize_session_inner(report)
        finally:
            restore_repointed_filepaths()


def _finalize_session_inner(report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Purge, pack every image, and save the .blend IN THE SAME SESSION.

    Baked PNGs arrive through bpy.data.images.load(), i.e. EXTERNAL file
    references: without pack_all() the deliverable .blend ships broken texture
    links (the textureless-Rooftop receipt).  A separate later session cannot
    do this reliably -- the slice-C finding is that finalize must run in the
    bake's own session.  save_version=0 keeps a .blend1 out of the deliverable
    (standing head amendment).
    """
    result: Dict[str, Any] = {"ran": False}
    if not FINALIZE_IN_SESSION:
        result["reason"] = "FINALIZE_IN_SESSION is off"
        return result
    source_path = bpy.data.filepath
    if not source_path:
        result["reason"] = "blend has no filepath (never saved); nothing to finalize"
        print("FINALIZE: skipped -- %s" % result["reason"])
        return result
    # v3.6: the deliverable is written BESIDE THE OUTPUT, not on top of the
    # user's input file. engine <=3.5 called save_mainfile() on bpy.data.filepath
    # with save_version=0, i.e. it overwrote the scene it had just read and left
    # no .blend1 to fall back to. Two things made that worse than it looks:
    #   * the precheck legitimately REPOINTS image.filepath on live datablocks to
    #     resolve a missing texture, so an ordinary run persisted its own
    #     bookkeeping into the user's file;
    #   * a prior fleet note (seat2, 2026-09-15) already records "Pack, Purge and
    #     Save .blend overwrites the open file and cannot be undone. Keep it off."
    # The stated purpose of finalize -- a .blend whose baked maps are packed
    # rather than externally linked -- is served just as well by a copy in the
    # output folder, and that is what a deliverable should be. FINALIZE_IN_PLACE
    # restores the old destructive behaviour for anyone who actually wants it.
    if FINALIZE_IN_PLACE:
        save_path = source_path
        result["save_mode"] = "IN_PLACE"
    else:
        stem = os.path.splitext(os.path.basename(source_path))[0]
        try:
            folder = output_directory()
        except Exception:
            folder = os.path.dirname(source_path)
        save_path = os.path.join(folder, "%s%s.blend" % (stem, FINALIZE_COPY_SUFFIX))
        result["save_mode"] = "OUTPUT_COPY"
        result["source_blend"] = source_path
    result["save_target"] = save_path
    try:
        for _ in range(FINALIZE_PURGE_PASSES):
            bpy.ops.outliner.orphans_purge(
                do_local_ids=True, do_linked_ids=True, do_recursive=True
            )
    except Exception as exc:
        result["purge_error"] = safe_text(exc)
    external = [image for image in bpy.data.images if image.source in {"MOVIE", "SEQUENCE"}]
    media_plan = stage_external_media(
        external, os.path.join(os.path.dirname(save_path), "import_glue_media"))
    # The plan captured precheck's resolved paths. Live restoration must use the
    # authored paths even after restore_repointed_filepaths clears its ledger.
    authored = {}
    for name, original in _REPOINTED_FILEPATHS:
        authored.setdefault(name, original)
    for record in media_plan["original_paths"]:
        record["filepath"] = authored.get(record["image"].name, record["filepath"])
    result["external_media"] = {"files": media_plan["files"], "errors": media_plan["errors"]}
    if media_plan["errors"]:
        result["pack_error"] = "External media delivery failed: " + "; ".join(media_plan["errors"])
        result["reason"] = "media verification failed; blend was not saved"
        if report is not None:
            report["finalize"] = result
        return result
    try:
        bpy.ops.file.pack_all()
    except Exception as exc:
        result["pack_error"] = safe_text(exc)
        print("FINALIZE: pack_all warning: %s" % safe_text(exc))
    unpacked = [
        im.name for im in bpy.data.images
        if not im.packed_file
        and im.source in {"FILE", "TILED"}
    ]
    total = len(bpy.data.images)
    result.update({
        "images_total": total,
        "images_packed": sum(1 for im in bpy.data.images if im.packed_file),
        "images_unpacked": len(unpacked),
        "unpacked_sample": unpacked[:10],
    })
    if unpacked:
        print("MACHINE|WARN images_still_unpacked=%d sample=%s" % (len(unpacked), unpacked[:10]))
        result.setdefault(
            "pack_error",
            "pack_all left %d external file image(s) unpacked" % len(unpacked),
        )
    if result.get("purge_error") or result.get("pack_error"):
        result["reason"] = "purge/pack verification failed; blend was not saved"
        print("FINALIZE: refusing save -- %s" % result["reason"], flush=True)
        if report is not None:
            report["finalize"] = result
        return result
    # v3.6: undo the precheck's in-session filepath repointing before anything is
    # written. pack_all() above has already copied the pixels into the blend, so
    # the deliverable stays self-contained while the recorded paths go back to
    # what the user authored. Without this, a run that resolved a missing texture
    # would bake the resolver's own choice of path into the saved file.
    restored = restore_repointed_filepaths()
    if restored:
        result["filepaths_restored"] = restored
        print("FINALIZE: restored %d repointed image path(s)" % len(restored))
    filepaths = bpy.context.preferences.filepaths
    old_save_version = getattr(filepaths, "save_version", None)
    try:
        filepaths.save_version = 0   # never write a .blend1 for this save only
        for record in media_plan["path_updates"]:
            record["image"].filepath = record["filepath"]
        if FINALIZE_IN_PLACE:
            save_result = bpy.ops.wm.save_mainfile(filepath=save_path, compress=True,
                                                  relative_remap=False)
        else:
            # save_as_mainfile(copy=True) writes the deliverable WITHOUT making it
            # the session's current file, so an interactive user is not silently
            # moved onto a generated blend.
            save_result = bpy.ops.wm.save_as_mainfile(
                filepath=save_path, compress=True, copy=True, relative_remap=False)
        if "FINISHED" not in set(save_result):
            raise RuntimeError("Blender save operator returned %r" % (save_result,))
        if not os.path.isfile(save_path) or os.path.getsize(save_path) <= 0:
            raise RuntimeError("Blender reported success but the saved blend is absent or empty")
        result["ran"] = True
        result["saved"] = save_path
        result["size_bytes"] = os.path.getsize(save_path)
        result["blend1"] = os.path.exists(save_path + "1")
    except Exception as exc:
        result["save_error"] = safe_text(exc)
        print("FINALIZE: save FAILED: %s" % safe_text(exc))
    finally:
        try:
            restore_media_paths(media_plan)
        finally:
            if old_save_version is not None:
                filepaths.save_version = old_save_version
    print("MACHINE|finalize saved=%s images_total=%s packed=%s unpacked=%s blend1=%s"
          % (result.get("saved"), result.get("images_total"), result.get("images_packed"),
             result.get("images_unpacked"), result.get("blend1")), flush=True)
    if report is not None:
        report["finalize"] = result
    return result


# ------------------------------ failure census ---------------------------------

def failure_census(manifest_path: str,
                   fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read this run's OWN manifest back off disk and grade it.

    'Saved' is never 'worked': the campaign shipped 186 corrupted parts that
    every driver had reported OK.  Re-reading the manifest also proves it was
    written and is parseable at all -- four scenes died before writing one.
    """
    census: Dict[str, Any] = {
        "manifest": manifest_path, "manifest_readable": False,
        "total": 0, "ok": 0, "failed": 0, "skipped": 0,
        "black_ok": 0, "black_suspect": 0, "missing_channel": 0,
        "magenta_suspect": 0, "no_material": 0, "over_triangle_budget": 0,
        "finalize_error": 0,
        "export_error": 0, "state_restore_error": 0,
        "reconstructed": 0, "checkpoint_restored": 0, "routes": {},
        "failed_items": [], "black_suspect_items": [], "missing_channel_items": [],
        "magenta_suspect_items": [], "no_material_items": [],
        "over_triangle_budget_items": [], "skipped_items": [],
        "approximated": 0, "approximated_items": [],
    }
    report = fallback
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        census["manifest_readable"] = True
    except Exception as exc:
        census["manifest_error"] = safe_text(exc)
    items = (report or {}).get("objects", []) if isinstance(report, dict) else []
    config = (report or {}).get("config", {}) if isinstance(report, dict) else {}
    try:
        triangle_budget = int(config.get("triangle_budget", TRI_BUDGET))
    except (TypeError, ValueError):
        triangle_budget = int(TRI_BUDGET)
    enforce_triangle_budget = bool(
        config.get("enforce_triangle_budget", ENFORCE_TRI_BUDGET)
    )
    census["triangle_budget"] = triangle_budget
    for item in items:
        name = item.get("object", "?")
        status = item.get("status", "?")
        census["total"] += 1
        if status == "OK":
            census["ok"] += 1
        elif status == "FAILED":
            census["failed"] += 1
            census["failed_items"].append("%s(%s)" % (name, item.get("reason", "")[:60]))
        elif status == "SKIPPED":
            census["skipped"] += 1
            census["skipped_items"].append("%s(%s)" % (name, str(item.get("reason", ""))[:60]))
        route = item.get("route", "NONE")
        census["routes"][route] = census["routes"].get(route, 0) + 1
        decision = item.get("capability") if isinstance(item.get("capability"), dict) else {}
        emission_approximated = ((item.get("roblox_binding") or {}).get("emission") or {}).get("status") == "APPROXIMATED"
        if status == "OK" and (decision.get("outcome") == "APPROXIMATED" or emission_approximated):
            census["approximated"] += 1
            census["approximated_items"].append("%s(%s)" % (name, ",".join(
                sorted({str(r.get("code")) for r in decision.get("reasons") or []}
                       | ({"EMISSION_FACTORIZATION"} if emission_approximated else set())))[:60]))
        if item.get("reconstructed"):
            census["reconstructed"] += 1
        if item.get("restored_from_checkpoint"):
            census["checkpoint_restored"] += 1
        if item.get("black") == "BLACK_OK":
            census["black_ok"] += 1
        elif item.get("black") == "BLACK_SUSPECT":
            census["black_suspect"] += 1
            census["black_suspect_items"].append(
                "%s(%s)" % (name, item.get("black_reason", "")[:60]))
        if item.get("missing_channels"):
            census["missing_channel"] += 1
            census["missing_channel_items"].append(
                "%s(%s)" % (name, ",".join(item["missing_channels"])))
        if item.get("magenta") == "MAGENTA_SUSPECT":
            census["magenta_suspect"] += 1
            census["magenta_suspect_items"].append("%s(%s)" % (name, str(item.get("magenta_reason", ""))[:80]))
        mats = item.get("materials") or {}
        if status == "OK" and any(
            any(token in str((m or {}).get("resolution_reason", ""))
                for token in ("missing-material-slot", "empty-material-slot"))
            for m in (mats.values() if isinstance(mats, dict) else [])
        ):
            census["no_material"] += 1
            census["no_material_items"].append(name)
        try:
            triangles = int(item.get("triangles", 0))
        except (TypeError, ValueError):
            triangles = 0
        if status == "OK" and (
            bool(item.get("triangle_budget_exceeded"))
            or triangles > triangle_budget
        ):
            census["over_triangle_budget"] += 1
            census["over_triangle_budget_items"].append(
                "%s(%d>%d)" % (name, triangles, triangle_budget)
            )
    visual_counts: Dict[str, int] = {}
    visual_not_pass: List[str] = []
    visual_on = bool(config.get("visual_validation"))
    for item in items:
        if item.get("status") != "OK":
            continue
        if "visual_validation" not in item and not visual_on:
            continue
        # With the check on, an OK part without a verdict is NOT_RUN, never silent.
        verdict = str((item.get("visual_validation") or {}).get("status") or "NOT_RUN")
        visual_counts[verdict] = visual_counts.get(verdict, 0) + 1
        if verdict != "PASS":
            visual_not_pass.append("%s(%s)" % (item.get("object", "?"), verdict))
    census["visual"] = visual_counts
    census["visual_not_pass_items"] = visual_not_pass
    visual_gate = bool(config.get("visual_validation") and config.get("visual_validation_gate"))
    census["visual_gate"] = visual_gate
    resume = (report or {}).get("resume") if isinstance(report, dict) else None
    census["resume_warnings"] = (
        [safe_text(w) for w in (resume.get("warnings") or [])][:20]
        if isinstance(resume, dict) else []
    )
    if isinstance(report, dict) and report.get("run_error"):
        census["run_error"] = report["run_error"]
    if isinstance(report, dict) and report.get("state_restore_error"):
        census["state_restore_error"] = 1
        census["state_restore_error_text"] = str(report["state_restore_error"])[:200]
    exports = (report or {}).get("exports") if isinstance(report, dict) else None
    if isinstance(exports, dict):
        export_errors = [str(value) for key, value in exports.items() if key.endswith("_error")]
        if export_errors:
            census["export_error"] = len(export_errors)
            census["export_error_text"] = "; ".join(export_errors)[:200]
    fin = (report or {}).get("finalize") if isinstance(report, dict) else None
    if isinstance(fin, dict) and (fin.get("save_error") or fin.get("pack_error") or fin.get("purge_error")):
        census["finalize_error"] = 1
        census["finalize_error_text"] = str(
            fin.get("save_error") or fin.get("pack_error") or fin.get("purge_error")
        )[:200]
    hard = (census["failed"] + census["missing_channel"] + census["finalize_error"]
            + census["export_error"] + census["state_restore_error"])
    if not census["manifest_readable"] or census.get("run_error"):
        hard += 1
    if CENSUS_FAIL_ON_BLACK:
        hard += census["black_suspect"]
    if CENSUS_FAIL_ON_SKIP:
        hard += census["skipped"]
    if CENSUS_FAIL_ON_NOMAT:
        hard += census["no_material"]
    if CENSUS_FAIL_ON_MAGENTA:
        hard += census["magenta_suspect"]
    if enforce_triangle_budget:
        hard += census["over_triangle_budget"]
    if visual_gate:
        hard += len(visual_not_pass)
    census["cancelled"] = bool(isinstance(report, dict) and report.get("cancelled"))
    census["exit_code"] = 130 if census["cancelled"] else (1 if hard else 0)
    census["status"] = ("CANCELLED" if census["cancelled"] else
                        "FAILED" if hard else "COMPLETED")
    if census["cancelled"]:
        census["cancel_reason"] = report.get("cancel_reason", "Cancellation requested")
        census["done_list"] = (resume or {}).get("done_list", "")
    print("=" * 78)
    print("FAILURE CENSUS | %s" % manifest_path)
    print("  manifest_readable=%s total=%d" % (census["manifest_readable"], census["total"]))
    print("  ok=%d failed=%d skipped=%d reconstructed=%d"
          % (census["ok"], census["failed"], census["skipped"], census["reconstructed"]))
    print("  black_ok=%d black_suspect=%d missing_channel=%d magenta_suspect=%d no_material=%d "
          "over_triangle_budget=%d finalize_error=%d export_error=%d state_restore_error=%d"
          % (census["black_ok"], census["black_suspect"], census["missing_channel"],
             census["magenta_suspect"], census["no_material"], census["over_triangle_budget"],
             census["finalize_error"],
             census["export_error"], census["state_restore_error"]))
    for warning in census.get("resume_warnings") or []:
        # Deliberately NOT an exit-code failure: the bakes are fine, only the resume
        # bookkeeping is.  Failing here would block the finalize save for no reason.
        print("  RESUME WARNING  %s" % warning)
    if census.get("finalize_error_text"):
        print("  FINALIZE ERROR: %s" % census["finalize_error_text"])
    if census.get("export_error_text"):
        print("  EXPORT ERROR: %s" % census["export_error_text"])
    if census.get("state_restore_error_text"):
        print("  STATE RESTORE ERROR: %s" % census["state_restore_error_text"])
    print("  routes=%s" % census["routes"])
    if visual_counts:
        print("  visual=%s gate=%s" % (visual_counts, visual_gate))
        for entry in visual_not_pass[:20]:
            print("  %-15s %s" % ("VISUAL", entry))
    if census.get("run_error"):
        print("  RUN ERROR: %s" % census["run_error"])
    for label, key in (("FAILED", "failed_items"),
                       ("BLACK_SUSPECT", "black_suspect_items"),
                       ("MISSING_CHANNEL", "missing_channel_items"),
                       ("MAGENTA_SUSPECT", "magenta_suspect_items"),
                       ("NO_MATERIAL", "no_material_items"),
                       ("OVER_TRI_BUDGET", "over_triangle_budget_items"),
                       ("SKIPPED", "skipped_items")):
        for entry in census[key][:20]:
            print("  %-15s %s" % (label, entry))
        if len(census[key]) > 20:
            print("  %-15s ... and %d more" % (label, len(census[key]) - 20))
    for entry in census["approximated_items"][:20]:
        print("  %-15s %s" % ("APPROXIMATED", entry))
    verdict = ("CANCELLED" if census["cancelled"] else
               "CLEAN" if not census["exit_code"] else "FAILURES PRESENT")
    if not census["exit_code"] and census["approximated"]:
        verdict = ("COMPLETED WITH %d APPROXIMATED OUTPUT(S) (explicit opt-in; not "
                   "faithful conversions)" % census["approximated"])
    print("  VERDICT: %s (exit_code=%d)" % (verdict, census["exit_code"]))
    print("=" * 78, flush=True)
    print("MACHINE|capability approximated=%d" % census["approximated"], flush=True)
    print("MACHINE|census total=%d ok=%d failed=%d skipped=%d black_ok=%d black_suspect=%d missing_channel=%d "
          "magenta_suspect=%d no_material=%d over_triangle_budget=%d finalize_error=%d "
          "export_error=%d state_restore_error=%d exit=%d"
          % (census["total"], census["ok"], census["failed"], census["skipped"], census["black_ok"],
             census["black_suspect"], census["missing_channel"], census["magenta_suspect"], census["no_material"],
             census["over_triangle_budget"], census["finalize_error"], census["export_error"],
             census["state_restore_error"],
             census["exit_code"]), flush=True)
    return census


def manifest_config() -> Dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "done_list_schema": DONE_LIST_SCHEMA,
        "pack_mra_graph": PACK_MRA_GRAPH_BAKE,
        "allow_cpu_fallback": ALLOW_CPU_FALLBACK,
        "allow_approximation": ALLOW_APPROXIMATION,
        "capability_profile": CAPABILITY_PROFILE,
        "target_profile": TARGET_PROFILE,
        "roblox_texture_limit": ROBLOX_TEXTURE_LIMIT,
        "output_profile": output_contract.build_output_contract(OUTPUT_PROFILE)["profile"],
        "capability_rules_version": material_capabilities.RULES_VERSION,
        "force_visible_output": FORCE_VISIBLE_OUTPUT,
        "isolate_graph_bake": ISOLATE_GRAPH_BAKE,
        "force_view_layer": FORCE_VIEW_LAYER,
        "finalize_in_session": FINALIZE_IN_SESSION,
        "done_list": DONE_LIST,
        "durable_checkpoints": DURABLE_CHECKPOINTS,
        "checkpoint_schema": durable_checkpoints.CHECKPOINT_SCHEMA,
        "poison_strikes": POISON_STRIKES,
        "benign_black": BENIGN_BLACK,
        "crop_split_prepass": CROP_SPLIT_PREPASS,
        "census_fail_on_black": CENSUS_FAIL_ON_BLACK,
        "mem_gate_gb": MEM_GATE_GB,
        "route_mode": ROUTE_MODE,
        "source_parent_levels": SOURCE_PARENT_LEVELS,
        "index_packed_images": INDEX_PACKED_IMAGES,
        "crop_min_fill": CROP_MIN_FILL,
        "min_res": MIN_RES,
        "bake_res": RES,
        "max_res": MAX_RES,
        "device": DEVICE,
        "gpu_device_id": GPU_DEVICE_ID,
        "export_glb": EXPORT_GLTF,
        "export_fbx": EXPORT_FBX,
        "triangle_budget": TRI_BUDGET,
        "enforce_triangle_budget": ENFORCE_TRI_BUDGET,
        "force_square_crops": FORCE_SQUARE_CROPS,
        "crop_pad_source_px": CROP_PAD_SOURCE_PX,
        "bake_margin_px": BAKE_MARGIN_PX,
        "graph_texels_full_uv": GRAPH_TEXELS_FULL_UV,
        # 2026-08-25: BAKE_DENSITY_SCALE decides the GRAPH/PROXY size together
        # with graph_texels_full_uv, but only config_fingerprint() ever saw it,
        # and that is a hash -- a manifest could not answer "what density was
        # this baked at?" even though the answer changes every output PNG.
        "bake_density_scale": BAKE_DENSITY_SCALE,
        "source_normal_is_directx": SOURCE_NORMAL_IS_DIRECTX,
        "proxy_image_extension": PROXY_IMAGE_EXTENSION,
        "strip_vertex_colors": STRIP_VERTEX_COLORS,
        "visual_validation": VISUAL_VALIDATION,
        "visual_validation_gate": VISUAL_VALIDATION_GATE,
        "visual_validation_settings": VISUAL_VALIDATION_SETTINGS,
    }


def pre_finalize_blockers(report: Dict[str, Any]) -> List[str]:
    """Return failures already known before broad pack/purge/save finalization."""
    blockers: List[str] = []
    if report.get("run_error"):
        blockers.append("run_error")
    if report.get("state_restore_error"):
        blockers.append("state_restore_error")
    exports = report.get("exports") or {}
    if isinstance(exports, dict):
        blockers.extend("EXPORT:%s" % key for key in exports if key.endswith("_error"))
    for item in report.get("objects", []):
        name = safe_text(item.get("object", "?"))
        status = item.get("status")
        if status == "OK" and (not item.get("uv_validation", {}).get("ok")
                               or not item.get("dependency_validation", {}).get("ok")):
            blockers.append("VALIDATION_MISSING:%s" % name)
        if status == "FAILED":
            blockers.append("FAILED:%s" % name)
        elif status == "SKIPPED" and CENSUS_FAIL_ON_SKIP:
            blockers.append("SKIPPED:%s" % name)
        if item.get("missing_channels"):
            blockers.append("MISSING_CHANNEL:%s" % name)
        if ENFORCE_TRI_BUDGET and item.get("triangle_budget_exceeded"):
            blockers.append("OVER_TRIANGLE_BUDGET:%s" % name)
        if CENSUS_FAIL_ON_BLACK and item.get("black") == "BLACK_SUSPECT":
            blockers.append("BLACK_SUSPECT:%s" % name)
        if CENSUS_FAIL_ON_MAGENTA and item.get("magenta") == "MAGENTA_SUSPECT":
            blockers.append("MAGENTA_SUSPECT:%s" % name)
        materials = item.get("materials") or {}
        if CENSUS_FAIL_ON_NOMAT and status == "OK" and isinstance(materials, dict):
            if any(
                any(token in str((material or {}).get("resolution_reason", ""))
                    for token in ("missing-material-slot", "empty-material-slot"))
                for material in materials.values()
            ):
                blockers.append("NO_MATERIAL:%s" % name)
        if VISUAL_VALIDATION and VISUAL_VALIDATION_GATE and status == "OK":
            visual = item.get("visual_validation") or {}
            if visual.get("status") != "PASS":
                blockers.append("VISUAL_%s:%s" % (visual.get("status") or "NOT_RUN", name))
    return blockers


def main(args_override: Optional[Dict[str, Any]] = None,
         before_finalize: Optional[Any] = None,
         run_control: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Run conversion; optional cooperative control never interrupts native calls.

    Cancellation returns exit_code=130/cancelled=True. Finished parts remain
    resumable; an incomplete run never saves a final blend or earns CLEAN.
    """
    global _RUN_CONTROL, _RUN_DONE_PATH, _TARGET_PIPELINE_ACTIVE
    previous, previous_done = _RUN_CONTROL, _RUN_DONE_PATH
    _RUN_CONTROL, _RUN_DONE_PATH = run_control, ""
    try:
        _run_boundary("prepare")
        if TARGET_PROFILE != "LEGACY" and not _TARGET_PIPELINE_ACTIVE:
            from . import pipeline
            _TARGET_PIPELINE_ACTIVE = True
            try:
                result = pipeline.run(args_override, run_control=run_control,
                                      before_finalize=before_finalize)
            finally:
                _TARGET_PIPELINE_ACTIVE = False
        else:
            result = _main_inner(args_override, before_finalize)
        _run_event("run_finished", status=("CANCELLED" if result and result.get("cancelled")
                   else "FAILED" if result and result.get("exit_code") else "COMPLETED" if result else "EMPTY"),
                   census=result)
        return result
    except (RunCancelled, KeyboardInterrupt) as exc:
        # Cancellation before _main_inner's report/finally exists (selection,
        # source indexing or resume) must also be a distinct non-success.
        result = {"exit_code": 130, "cancelled": True, "status": "CANCELLED",
                  "cancel_reason": str(exc), "manifest": "", "total": 0,
                  "ok": 0, "failed": 0, "skipped": 0, "done_list": _RUN_DONE_PATH}
        if callable(before_finalize):
            try:
                before_finalize()
            except Exception as restore_exc:
                result["state_restore_error"] = safe_text(restore_exc)
        _run_event("run_finished", status="CANCELLED", census=result)
        return result
    finally:
        try:
            restore_repointed_filepaths()
            restore_log()
        finally:
            _RUN_CONTROL, _RUN_DONE_PATH = previous, previous_done


def _main_inner(args_override: Optional[Dict[str, Any]] = None,
                before_finalize: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Run the pipeline and return its failure census.

    ``args_override`` and ``before_finalize`` are the persistent-add-on API.
    Standalone callers keep the original CLI behavior by omitting both.  The
    callback runs immediately before optional pack/purge/save finalization, so
    an add-on can restore selection and visibility before the blend is saved.
    """
    reset_run_state()
    global _VISUAL_RUN_ID
    _VISUAL_RUN_ID = "%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid())
    args = dict(args_override) if args_override is not None else cli_args()
    run_tag = safe_text(args.get("tag"), "").strip() if args.get("tag") not in (None, True) else ""
    banner = provenance_banner("run start" + ((" | " + run_tag) if run_tag else ""))
    _run_boundary("selection")
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    # Force-include BEFORE selection is read: objects in an excluded collection
    # are absent from view_layer.objects and cannot be selected at all.
    unexcluded = unexclude_all_collections()
    selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    raw_selected = [obj for obj in selected_meshes if len(obj.data.polygons) > 0]
    # v3.4: a 0-polygon mesh used to just vanish from the list comprehension above --
    # absent from the manifest AND the census, worse than a SKIPPED entry (which at
    # least shows up). Keep it and report it explicitly further down.
    empty_mesh = [obj for obj in selected_meshes if len(obj.data.polygons) == 0]
    scope = only_names(args.get("only"))
    scoped_out: List[str] = []
    if scope is not None:
        scoped_out = [datablock_name(o) for o in raw_selected if datablock_name(o) not in scope]
        raw_selected = [o for o in raw_selected if datablock_name(o) in scope]
        empty_mesh = [o for o in empty_mesh if datablock_name(o) in scope]
        print("SCOPED by --only to %d of %d selected object(s)"
              % (len(raw_selected), len(raw_selected) + len(scoped_out)))
    ignored = [obj for obj in raw_selected if is_previous_output(obj)]
    selected = [obj for obj in raw_selected if not is_previous_output(obj)]
    if not selected:
        global _NOTHING_SELECTED
        _NOTHING_SELECTED = True
        print("import_glue_smart: select one or more unprocessed source mesh objects.")
        print("MACHINE|nothing_selected raw=%d scoped_out=%d ignored_previous=%d"
              % (len(raw_selected), len(scoped_out), len(ignored)), flush=True)
        if ignored:
            print("Ignored %d previous output object(s)." % len(ignored))
        return None

    _run_boundary("output_setup", total=len(selected))
    folder = output_directory()
    install_log(folder)
    # v1.1.1: a run folder can only hold a staging directory if an earlier
    # publish into it died between its first and last os.replace().  Some of
    # that base's maps may be in place and some not, and nothing downstream can
    # tell that from a complete set, so clear the whole set before this run
    # plans anything.  output_directory() normally mints a fresh folder, where
    # this is one no-op listdir; it earns its keep whenever a folder is reused
    # (OUTPUT_ROOT pinned to a fixed path, a hand-picked output directory).
    recover_staged_parts(folder)
    provenance_banner("log start")
    start = time.time()
    _run_event("run_directory", output=folder)

    # Build once and reuse for both resume validation and planning.  Resume must
    # fingerprint the actual atlas files selected by the current resolver.
    _run_boundary("source_index")
    source_index = build_source_index()
    _run_boundary("source_index_complete")
    # One texture-validity cache for this run's capability analyses (resume
    # validation, then the preflight); source files do not change in between.
    capability_cache: Dict[str, Any] = {}

    # ---- resume: native done-list, one per scene (see done_list_path docstring)
    use_done = DONE_LIST and not args.get("no-resume")
    done_path = done_list_path(folder, args.get("done"))
    global _RUN_DONE_PATH
    _RUN_DONE_PATH = done_path
    _run_boundary("resume", done_list=done_path)
    done = load_done_list(done_path) if use_done else {
        "version": DONE_LIST_SCHEMA, "objects": {}
    }
    carried: List[Dict[str, Any]] = []
    # id(carried row) -> the source object resume validated for it.  Later
    # steps use this object, never a bare-name lookup: a linked source can
    # share its name with a local object (M1 final review finding 8).
    carried_sources: Dict[int, Any] = {}
    poisoned: List[Dict[str, Any]] = []
    checkpoint_events: List[Dict[str, Any]] = []
    restored_collection = None
    if use_done and done["objects"]:
        keep: List[Any] = []
        for obj in selected:
            name = datablock_name(obj, "mesh_object")
            _run_boundary("resume_object", object=name)
            entry = done["objects"].get(name)
            if entry and entry.get("status") == "OK" and not args.get("rerun"):
                resume_ok, resume_reason = validate_done_entry(
                    entry, obj, source_index, capability_cache=capability_cache)
                restored = None
                if not resume_ok and DURABLE_CHECKPOINTS and entry.get("checkpoint") is not None:
                    if restored_collection is None:
                        restored_collection = bpy.data.collections.new(
                            OUTPUT_COLLECTION + "_RESTORED")
                        bpy.context.scene.collection.children.link(restored_collection)
                    # store_root: resolve the store-relative manifest path first,
                    # so a moved output root still finds its generations.
                    outcome = durable_checkpoints.restore_for_entry(
                        entry, obj, source_index, collection=restored_collection,
                        store_root=durable_checkpoints.checkpoint_store_root(folder),
                        capability_cache=capability_cache)
                    checkpoint_events.append({
                        "object": name, "restored": bool(outcome.get("restored")),
                        "code": outcome.get("code", ""), "reason": outcome.get("reason", ""),
                        "checkpoint_id": outcome.get("checkpoint_id"),
                        "output_object": outcome.get("object"),
                        "manifest": outcome.get("manifest"),
                        "blender": outcome.get("blender"),
                    })
                    if outcome.get("restored"):
                        restored = outcome
                        recorded_entry = entry
                        entry = outcome["entry"]
                        done["objects"][name] = entry
                        # The existing gate, unweakened, on the restored part.
                        resume_ok, resume_reason = validate_done_entry(
                            entry, obj, source_index, capability_cache=capability_cache)
                        print("RESUME: %s restored from checkpoint %s as %s"
                              % (name, outcome["checkpoint_id"], outcome["object"]), flush=True)
                    else:
                        resume_reason = "%s; checkpoint not used: %s" % (
                            resume_reason, outcome.get("reason"))
                elif not resume_ok and DURABLE_CHECKPOINTS:
                    # An entry from 1.3.0 (or one whose checkpoint could not be
                    # written) stays readable; say explicitly why nothing is
                    # restored instead of leaving only the live-object reason.
                    unavailable = entry.get("checkpoint_unavailable")
                    why = safe_text(unavailable) if unavailable else (
                        "the done entry predates durable checkpoints or was written "
                        "with them off")
                    checkpoint_events.append({
                        "object": name, "restored": False, "code": "NO_CHECKPOINT",
                        "reason": why, "checkpoint_id": None, "output_object": None,
                        "manifest": None, "blender": None,
                    })
                    resume_reason = "%s; no durable checkpoint: %s" % (resume_reason, why)
                live_capability = None
                if resume_ok:
                    # A carried part is labelled with the live decision that
                    # validate_done_entry() just accepted, never with the done
                    # entry's recorded copy: rules or policy may have changed.
                    try:
                        live_capability = capability_decision(obj, capability_cache)
                    except Exception as exc:
                        resume_ok = False
                        resume_reason = ("could not analyze material capability: %s"
                                         % safe_text(exc))
                transfer = {"linked": 0, "copied": 0}
                if resume_ok:
                    used_bases_before = set(_USED_BASES)
                    try:
                        _reserve_output_bases(_entry_output_bases(entry))
                        transfer = materialize_resume_outputs(entry, folder)
                        save_done_list(done_path, done)
                    except Exception as exc:
                        _USED_BASES.clear()
                        _USED_BASES.update(used_bases_before)
                        resume_ok = False
                        resume_reason = "could not materialize carried outputs: %s" % safe_text(exc)
                if not resume_ok and restored is not None:
                    # Withdraw the restored part and keep the recorded entry (and
                    # its checkpoint reference) for the rebake's bookkeeping.
                    durable_checkpoints.discard_restored(restored)
                    entry = recorded_entry
                    done["objects"][name] = entry
                if resume_ok:
                    carried.append({
                        "object": name,
                        "output_object": entry.get("output_object"),
                        "status": "OK", "route": entry.get("route", "UNKNOWN"),
                        "route_reason": entry.get("route_reason", ""),
                        "black": entry.get("black"), "black_reason": entry.get("black_reason"),
                        "magenta": entry.get("magenta"),
                        "magenta_reason": entry.get("magenta_reason"),
                        "materials": entry.get("materials") or {},
                        "triangles": entry.get("triangles"),
                        "triangle_budget_exceeded": bool(
                            entry.get("triangle_budget_exceeded")
                        ),
                        "missing_channels": [],
                        "reconstructed": True,
                        "reconstructed_from": done_path,
                        "prior_failure_reason": entry.get("prior_failure_reason"),
                        "output_folder": entry.get("folder"),
                        "file_base": entry.get("file_base"),
                        "file_bases": entry.get("file_bases") or [],
                        "resume_materialized": transfer,
                        "uv_validation": entry.get("uv_validation"),
                        "dependency_validation": entry.get("dependency_validation"),
                        "capability": live_capability,
                        "capability_recorded": entry.get("capability"),
                        "normal_encoding_adjustments": entry.get("normal_encoding_adjustments", []),
                        "restored_from_checkpoint": entry.get("restored_from_checkpoint")
                        if restored is not None else None,
                    })
                    carried_sources[id(carried[-1])] = obj
                    _run_event("object_resumed", object=name, completed=len(carried),
                               checkpoint=entry.get("checkpoint"))
                    continue
                print("RESUME INVALID: %s -> rerun (%s)" % (name, resume_reason), flush=True)
            if entry and args.get("rerun") and int(entry.get("strikes", 0)):
                # v1.1.1: --rerun / "Rerun Completed Objects" is the user explicitly saying
                # "try this one again".  Previously it only bypassed the OK-carry check, so a
                # poisoned item stayed SKIPPED forever even after its cause was fixed -- and
                # SKIPPED fails the census (CENSUS_FAIL_ON_SKIP), which then blocks the
                # finalize save.  The only escapes were disabling resume entirely or deleting
                # the done-list by hand, neither discoverable from the UI.
                print("RESUME: clearing %d strike(s) for %s (rerun requested)"
                      % (int(entry.get("strikes", 0)), name), flush=True)
                entry["strikes"] = 0
                done["objects"][name] = entry
                save_done_list(done_path, done)
            if entry and int(entry.get("strikes", 0)) >= POISON_STRIKES:
                poisoned.append({
                    "object": name, "status": "SKIPPED", "route": "NONE",
                    "reason": "poison-skip: %d consecutive failures (last: %s)"
                              % (int(entry["strikes"]), entry.get("reason", "?")),
                    "prior_failure_reason": entry.get("reason"),
                    "reconstructed": False,
                })
                print("POISON SKIP: %s (%d consecutive failures)" % (name, int(entry["strikes"])))
                continue
            keep.append(obj)
        selected = keep
        if restored_collection is not None and not restored_collection.objects:
            bpy.data.collections.remove(restored_collection)
        print("RESUME: done-list %s -> already_ok=%d (restored_from_checkpoint=%d, "
              "checkpoint_not_used=%d) poison_skipped=%d remaining=%d"
              % (done_path, len(carried), sum(1 for e in checkpoint_events if e["restored"]),
                 sum(1 for e in checkpoint_events if not e["restored"]), len(poisoned),
                 len(selected)))

    report: Dict[str, Any] = {
        "script": "import_glue.py v%s" % TOOL_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "provenance": banner,
        "blender": bpy.app.version_string,
        "blend": bpy.data.filepath,
        "output": folder,
        "config": manifest_config(),
        "output_contract": output_contract.build_output_contract(OUTPUT_PROFILE),
        "target_profile": TARGET_PROFILE,
        "target_output_contract": ({"profile": "ROBLOX_SURFACEAPPEARANCE",
            "channels": ["color", "metal", "rough", "normal", "emissive"],
            "texture_limit": ROBLOX_TEXTURE_LIMIT, "bits": 8,
            "normal_convention": "OpenGL tangent space; triangulated before baking",
            "emission": "quantized mask/tint/strength measured against final decoded color",
            "studio_verified": False} if TARGET_PROFILE == "ROBLOX" else None),
        "args": {k: (v if isinstance(v, str) else bool(v)) for k, v in args.items()},
        # "warnings" is a live reference on purpose: save_done_list() appends to it
        # long after this dict is built, and the manifest is serialized at run end.
        "resume": {"done_list": done_path, "enabled": bool(use_done),
                   "carried_over": len(carried), "poison_skipped": len(poisoned),
                   "scoped_out": len(scoped_out), "warnings": _DONE_LIST_WARNINGS,
                   "checkpoints": {"enabled": bool(use_done and DURABLE_CHECKPOINTS),
                                   "schema": durable_checkpoints.CHECKPOINT_SCHEMA,
                                   "restore_attempts": checkpoint_events}},
        "view_layer": {"collections_unexcluded": unexcluded},
        "objects": [],
        "exports": {},
    }
    report["objects"].extend(carried)
    report["objects"].extend(poisoned)
    cache = PixelCache(PIXEL_CACHE_MAX_MB)
    outputs: List[Any] = [
        obj for item in carried
        for obj in [local_object(item.get("output_object"))]
        if obj is not None
    ]
    collection = output_collection()
    census: Dict[str, Any] = {}

    try:
        _run_boundary("capability_preflight", total=len(selected))
        for obj in ignored:
            report["objects"].append({
                "object": datablock_name(obj, "previous_output"),
                "status": "SKIPPED", "route": "NONE", "reconstructed": False,
                "reason": "previous generated output",
            })
        for obj in empty_mesh:
            # v3.4: was silently dropped before ever reaching plan_jobs/skipped.
            report["objects"].append({
                "object": datablock_name(obj, "empty_mesh_object"),
                "status": "SKIPPED", "route": "NONE", "reconstructed": False,
                "reason": "empty mesh (0 polygons)",
            })
        # Milestone 1 (A): read-only material capability preflight.  It never
        # changes routes; the policy only decides whether an object may be
        # converted at all.  Decisions are found by (name, library), never by
        # bare name, and a missing one is refused.  Only gates that are on in
        # this run keep their own failure: the dependency gate always, the
        # empty/missing-slot gate only while CENSUS_FAIL_ON_NOMAT is set.
        capability = material_capabilities.preflight_report(
            selected, CAPABILITY_PROFILE, ALLOW_APPROXIMATION, cache=capability_cache
        )
        _run_boundary("capability_preflight_complete")
        report["material_capabilities"] = capability
        capability_index = material_capabilities.decision_index(
            capability["object_decisions"])
        refused_by_capability: Set[int] = set()
        for obj in selected:
            _run_boundary("capability_object", object=datablock_name(obj))
            decision = capability_index.get(material_capabilities.object_identity(obj))
            refusal = material_capabilities.policy_refusal(
                decision, slot_gate=bool(CENSUS_FAIL_ON_NOMAT))
            if refusal is None:
                continue
            refused_by_capability.add(obj.as_pointer())
            name = datablock_name(obj, "mesh_object")
            print("    CAPABILITY REFUSED %s: %s" % (name, refusal), flush=True)
            # A deterministic policy refusal is not a crash: no done-list
            # strike is recorded, so it cannot become a poison skip.
            report["objects"].append({
                "object": name, "status": "FAILED", "route": "NONE",
                "reason": refusal, "capability": decision,
                "reconstructed": False, "missing_channels": [],
            })
        if refused_by_capability:
            selected = [o for o in selected if o.as_pointer() not in refused_by_capability]
        # Crop-split pre-pass: multi-material all-simple-slot objects take the
        # no-Cycles CROP path per slot; everything it declines falls through to
        # the stock router untouched, in this same session.
        def commit_split_result(result: Dict[str, Any], split_source: Any) -> None:
            joined = result.pop("joined_object", None)
            file_base = result.pop("file_base", "")
            if joined is not None:
                outputs.append(joined)
                result["triangles"] = triangle_count(joined)
                if VISUAL_VALIDATION:
                    result["visual_validation"] = run_visual_validation(
                        split_source, joined, folder,
                        file_base or result["object"])
            result["triangle_budget_exceeded"] = (
                int(result.get("triangles", 0)) > TRI_BUDGET
            )
            if result["triangle_budget_exceeded"]:
                print(
                    "    OVER TRIANGLE BUDGET: %d > %d"
                    % (result["triangles"], TRI_BUDGET),
                    flush=True,
                )
            # One PNG set per split slot, so verify each of them.
            missing: List[str] = []
            for slot_base in result.get("file_bases", []):
                missing.extend("%s:%s" % (slot_base, ch)
                               for ch in verify_part_outputs(folder, slot_base))
            result["missing_channels"] = missing
            result["reconstructed"] = False
            result["capability"] = capability_index.get(
                material_capabilities.object_identity(split_source)
            ) if split_source is not None else None
            if missing:
                print("    MISSING CHANNELS: %s" % ",".join(missing))
            report["objects"].append(result)
            if use_done:
                try:
                    source = split_source
                    if source is None:
                        raise RuntimeError("CROP_SPLIT source is unavailable for resume metadata")
                    file_bases = result.get("file_bases", [])
                    record_done(done, done_path, result["object"], {
                        "status": "OK", "route": "CROP_SPLIT",
                        "route_reason": result.get("route_reason", ""),
                        "output_object": result.get("output_object"),
                        "black": result.get("black"),
                        "black_reason": result.get("black_reason"),
                        "magenta": result.get("magenta"),
                        "magenta_reason": result.get("magenta_reason"),
                        "triangles": result.get("triangles", 0),
                        "triangle_budget_exceeded": result.get(
                            "triangle_budget_exceeded", False
                        ),
                        "missing_channels": missing,
                        "capability": result.get("capability"),
                        "folder": folder, "file_base": file_base,
                        "file_bases": file_bases,
                        **resume_metadata(
                            source, folder, file_bases, source_index, joined
                        ),
                    })
                    checkpoint_done_entry(done, done_path, result["object"], joined)
                except Exception as book_exc:
                    done["objects"].pop(result["object"], None)
                    save_done_list(done_path, done)
                    print(
                        "MACHINE|WARN split_resume_metadata_failed obj=%s err=%s"
                        % (result["object"], safe_text(book_exc)),
                        flush=True,
                    )
                    traceback.print_exc()

            entry = done["objects"].get(result["object"], {})
            _run_event("object_committed", object=result["object"], route="CROP_SPLIT",
                       checkpoint=entry.get("checkpoint"),
                       completed=sum(i.get("status") == "OK" for i in report["objects"]))

        _run_boundary("crop_split")
        split_results, split_handled = crop_split_prepass(
            selected, source_index, folder, cache, collection,
            on_committed_result=commit_split_result,
        )
        if split_handled:
            handled_names = {datablock_name(o) for o in split_handled}
            selected = [o for o in selected if datablock_name(o) not in handled_names]
        _run_boundary("planning", total=len(selected))

        jobs, skipped = plan_jobs(selected, source_index)
        for entry in skipped:
            entry.setdefault("route", "NONE")
            entry.setdefault("reconstructed", False)
        report["objects"].extend(skipped)
        print("=" * 78)
        print("SMART PBR | selected=%d planned=%d skipped=%d" % (
            len(selected), len(jobs), len(skipped)
        ))
        print("routes: crop=%d proxy=%d graph=%d crop_split=%d" % (
            sum(j.route == "CROP" for j in jobs),
            sum(j.route == "PROXY_BAKE" for j in jobs),
            sum(j.route == "GRAPH_BAKE" for j in jobs),
            len(split_results),
        ))
        print("=" * 78)

        # v3.4: shared by the memory gate below and the device-probe skip further down --
        # a pure-CROP/CROP_SPLIT run never calls bpy.ops.object.bake() at all.
        needs_cycles = any(j.route in ("PROXY_BAKE", "GRAPH_BAKE") for j in jobs)

        # Proactive gate: OOM/SIGKILL was the dominant failure mode of the
        # campaign and the only defence anywhere was reactive retry.
        if needs_cycles:
            _run_boundary("memory_admission")
            report["memory_gate"] = memory_gate(
                float(args["mem-gate"]) if isinstance(args.get("mem-gate"), str) else None,
                tag="bake",
            )

        with SceneSettingsGuard(
            snapshot_cycles_preferences=needs_cycles and DEVICE == "GPU"
        ):
            bpy.context.scene.render.engine = "CYCLES"
            bpy.context.scene.cycles.samples = 1
            bpy.context.scene.render.use_persistent_data = PERSISTENT_DATA
            if needs_cycles:
                _run_boundary("device_setup")
                ensure_cycles_device()
                report["device_selection"] = dict(_LAST_DEVICE_SELECTION)
            # v3.4: prefs.get_devices() (called inside ensure_cycles_device) re-probes
            # every GPU/compute backend on EVERY run regardless of whether anything will
            # actually touch Cycles -- measured 0.25s wasted on a run that resolved to
            # CROP/CROP_SPLIT only (0 bake() calls, 0% GPU use for its entire 44s wall
            # time). Skipping it here doesn't change device selection for any run that
            # actually bakes; it only skips needless work for ones that don't.

            # Stable sort improves atlas cache reuse without changing names.
            jobs.sort(key=lambda j: (
                safe_text(next(iter(j.sets_by_slot.values())).name, "atlas").lower()
                if j.sets_by_slot else "~graph",
                j.route, datablock_name(j.source, "mesh_object"),
            ))
            for index, job in enumerate(jobs, 1):
                _run_boundary("object", object=datablock_name(job.source),
                              route=job.route, completed=index - 1, total=len(jobs))
                duplicate = None
                produced: List[str] = []
                print("[%d/%d] %s -> %s (%s)" % (
                    index, len(jobs), datablock_name(job.source, "mesh_object"),
                    job.route, job.route_reason
                ), flush=True)
                try:
                    require_material_dependencies(job_dependency_materials(job))
                    ensure_reachable(job.source)
                    planned_paths = [
                        output_path(folder, job.file_base, ch)
                        for ch in (CHANNELS + ("emissive",) if TARGET_PROFILE == "ROBLOX" else CHANNELS)
                    ]
                    collisions = [path for path in planned_paths if os.path.lexists(path)]
                    if collisions:
                        raise RuntimeError(
                            "refusing to overwrite existing output: %s"
                            % os.path.basename(collisions[0])
                        )
                    duplicate = duplicate_mesh_object(
                        job.source, collection, "RBX_" + job.file_base
                    )
                    # v1.1.1: the four maps are written into a private staging
                    # directory and moved into place as one verified set, so a
                    # route that dies on its third map leaves nothing behind.
                    # `produced` is filled in only once they are all published:
                    # the except branch below deletes what it lists, and before
                    # publication there is nothing in `folder` to delete.
                    staging = begin_staging(folder, job.file_base)
                    try:
                        if job.route == "CROP":
                            detail = process_crop(job, duplicate, staging, cache)
                        elif job.route == "PROXY_BAKE":
                            detail = process_bake(job, duplicate, staging)
                        else:
                            detail = process_graph_bake(job, duplicate, staging)
                        detail["uv_validation"] = require_output_uv(duplicate, job.route)
                        _run_boundary("publish", object=datablock_name(job.source),
                                      route=job.route)
                        produced = publish_staged_maps(folder, job.file_base, staging)
                    except BaseException as publish_exc:
                        # BaseException, not Exception: a cancelled run raises
                        # KeyboardInterrupt, which the handler below does not
                        # catch -- that is exactly how partial sets survived.
                        discard_staged_part(folder, job.file_base,
                                            owned_paths=getattr(publish_exc, "published_paths", []))
                        raise
                    finish_staging(staging)
                    strip_colors(duplicate.data)
                    assign_preview_material(
                        duplicate, folder, job.file_base, bool(detail.get("alpha")),
                        detail.get("roblox_binding")
                    )
                    tris = triangle_count(duplicate)
                    _run_boundary("validate", object=datablock_name(job.source),
                                  route=job.route)
                    visual = (run_visual_validation(job.source, duplicate, folder, job.file_base)
                              if VISUAL_VALIDATION else None)
                    _run_boundary("commit", object=datablock_name(job.source),
                                  route=job.route)
                    outputs.append(duplicate)
                    item = {
                        "object": datablock_name(job.source, "mesh_object"),
                        "output_object": datablock_name(duplicate, "output_object"),
                        "status": "OK",
                        "route": job.route,
                        "route_reason": job.route_reason,
                        "source_uv": job.source_uv,
                        "source_uv_bounds": list(job.uv.bounds),
                        "source_uv_fill": job.uv.fill_ratio,
                        "triangles": tris,
                        "triangle_budget_exceeded": tris > TRI_BUDGET,
                        "materials": {
                            str(slot): ({
                                "material": datablock_name(
                                    job.materials_by_slot.get(slot), "<empty>"
                                ) if job.materials_by_slot.get(slot) else "<empty>",
                                "resolved": True,
                                "resolution_reason": job.resolution_by_slot.get(slot, ""),
                                "set": job.sets_by_slot[slot].name,
                                "method": job.sets_by_slot[slot].method,
                                "confidence": job.sets_by_slot[slot].confidence,
                                "maps": {
                                    ch: src.label
                                    for ch, src in job.sets_by_slot[slot].maps.items()
                                },
                                "notes": job.sets_by_slot[slot].notes,
                            } if slot in job.sets_by_slot else {
                                "material": datablock_name(
                                    job.materials_by_slot.get(slot), "<empty>"
                                ) if job.materials_by_slot.get(slot) else "<empty>",
                                "resolved": False,
                                "resolution_reason": job.resolution_by_slot.get(slot, ""),
                            })
                            for slot in job.used_slots
                        },
                        "warnings": job.warnings,
                        "capability": capability_index.get(
                            material_capabilities.object_identity(job.source)),
                        **detail,
                    }
                    # Per-part verification: every channel must be on disk, and a
                    # numerically black colour bake is graded, not shrugged at.
                    # Bookkeeping runs in its own guard: a defect in it must never
                    # reach the except branch below, which deletes this part's PNGs.
                    if visual is not None:
                        item["visual_validation"] = visual
                    item["missing_channels"] = []
                    item["reconstructed"] = False
                    try:
                        if item["triangle_budget_exceeded"]:
                            warning = "final triangle count %d exceeds budget %d" % (
                                tris, TRI_BUDGET
                            )
                            item["warnings"].append(warning)
                            print("    OVER TRIANGLE BUDGET: " + warning, flush=True)
                        item["missing_channels"] = verify_part_outputs(folder, job.file_base)
                        prior = done["objects"].get(item["object"], {})
                        if prior.get("status") == "FAILED":
                            item["prior_failure_reason"] = prior.get("reason")
                        verdict = classify_black(job, detail)
                        if verdict:
                            item.update(verdict)
                            print("    %s: %s" % (verdict["black"], verdict["black_reason"]))
                        mverdict = classify_magenta(job, detail)
                        if mverdict:
                            item.update(mverdict)
                            print("    %s: %s" % (mverdict["magenta"], mverdict["magenta_reason"]))
                        if item["missing_channels"]:
                            print("    MISSING CHANNELS: %s" % ",".join(item["missing_channels"]))
                        if use_done:
                            record_done(done, done_path, item["object"], {
                                "status": "OK", "route": job.route,
                                "route_reason": job.route_reason,
                                "output_object": item["output_object"],
                                "black": item.get("black"),
                                "black_reason": item.get("black_reason"),
                                "magenta": item.get("magenta"),
                                "magenta_reason": item.get("magenta_reason"),
                                "materials": item.get("materials") or {},
                                "triangles": item.get("triangles", 0),
                                "triangle_budget_exceeded": item.get(
                                    "triangle_budget_exceeded", False
                                ),
                                "missing_channels": item["missing_channels"],
                                "capability": item.get("capability"),
                                "normal_encoding_adjustments": item.get("normal_encoding_adjustments", []),
                                "folder": folder, "file_base": job.file_base,
                                "file_bases": [job.file_base],
                                "prior_failure_reason": item.get("prior_failure_reason"),
                                **resume_metadata(
                                    job.source, folder, [job.file_base], source_index,
                                    duplicate,
                                ),
                            })
                            checkpoint_done_entry(done, done_path, item["object"], duplicate)
                    except Exception as book_exc:
                        done["objects"].pop(item["object"], None)
                        save_done_list(done_path, done)
                        print("MACHINE|WARN bookkeeping_failed obj=%s err=%s"
                              % (item["object"], safe_text(book_exc)))
                        traceback.print_exc()
                    report["objects"].append(item)
                    print("    OK %d tris | %s" % (tris, job.file_base), flush=True)
                except (RunCancelled, KeyboardInterrupt):
                    # The current object has not reached its durable commit.
                    # Remove only this attempt's output; do not record a FAILED
                    # done entry or count cancellation as a material strike.
                    if duplicate is not None:
                        try:
                            bpy.data.objects.remove(duplicate, do_unlink=True)
                        except Exception:
                            pass
                    for path in produced:
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                        except Exception:
                            pass
                    report["objects"].append({
                        "object": datablock_name(job.source), "status": "CANCELLED",
                        "route": job.route, "reason": "cancelled before object commit",
                    })
                    raise
                except Exception as exc:
                    print("    FAIL:", exc)
                    traceback.print_exc()
                    if duplicate is not None:
                        try:
                            bpy.data.objects.remove(duplicate, do_unlink=True)
                        except Exception:
                            pass
                    for path in produced:
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                        except Exception:
                            pass
                    object_name = datablock_name(job.source, "mesh_object")
                    prior = done["objects"].get(object_name, {})
                    report["objects"].append({
                        "object": object_name,
                        "status": "FAILED",
                        "route": job.route,
                        "reason": safe_text(exc),
                        "bake_attempts": getattr(exc, "bake_attempts", []),
                        "emission_conversion": getattr(exc, "result", None),
                        "prior_failure_reason": prior.get("reason"),
                        "reconstructed": False,
                        "missing_channels": [],
                        "warnings": job.warnings,
                    })
                    if use_done:
                        # Strike counting lives on disk, not in a supervisor's
                        # shell variables: 5 supervisor restarts silently threw
                        # away partial strike counts during the campaign.
                        record_done(done, done_path, object_name, {
                            "status": "FAILED", "route": job.route,
                            "reason": safe_text(exc), "folder": folder,
                            "bake_attempts": getattr(exc, "bake_attempts", []),
                            "file_base": job.file_base,
                            "prior_failure_reason": prior.get("reason"),
                        })

                # Outside the failure/cleanup region: a newly requested cancel
                # must preserve this completed object's files and checkpoint.
                if report["objects"][-1].get("status") == "OK":
                    entry = done["objects"].get(datablock_name(job.source), {})
                    _run_event("object_committed", object=datablock_name(job.source),
                               route=job.route, completed=index, total=len(jobs),
                               checkpoint=entry.get("checkpoint"))
                _run_boundary("object_complete", object=datablock_name(job.source),
                              completed=index, total=len(jobs))

        if VISUAL_VALIDATION:
            for item in carried:
                _run_boundary("visual_validation", object=item.get("object"))
                live = local_object(item.get("output_object"))
                source = carried_sources.get(id(item))
                item["visual_validation"] = (
                    run_visual_validation(source, live, folder, item.get("file_base") or item["object"])
                    if live is not None and source is not None else
                    {"status": "NOT_RUN", "report": None, "reasons": ["VISUAL_OUTPUT_NOT_LIVE"]})
            for item in report["objects"]:
                if item.get("status") == "OK" and "visual_validation" not in item:
                    item["visual_validation"] = {"status": "NOT_RUN", "report": None,
                                                 "reasons": ["VISUAL_NOT_RUN_FOR_ROUTE"],
                                                 "route": item.get("route")}
        _run_boundary("exports")
        export_outputs(outputs, folder, report)
        _run_boundary("exports_complete")
    except (RunCancelled, KeyboardInterrupt) as cancel_exc:
        _mark_cancelled(report, cancel_exc)
    except Exception as run_exc:
        # A run that died before planning finished would otherwise write a
        # manifest holding only the parts it managed, and the census would grade
        # that partial list CLEAN.  Record the death in the manifest itself.
        report["run_error"] = safe_text(run_exc)
        raise
    finally:
        cache.clear()
        report["elapsed_seconds"] = round(time.time() - start, 3)
        try:
            report["pixel_cache"] = cache.report()
        except Exception as exc:
            report["pixel_cache_error"] = safe_text(exc)
        # v3.6: a per-stage breakdown beside the single wall-clock number, so a
        # speed claim can be attributed to a stage instead of argued about.
        try:
            report["stage_timing"] = print_stage_report(time.time() - start)
        except Exception as exc:
            report["stage_timing_error"] = safe_text(exc)
        report["summary"] = {
            "ok": sum(x.get("status") == "OK" for x in report["objects"]),
            "failed": sum(x.get("status") == "FAILED" for x in report["objects"]),
            "skipped": sum(x.get("status") == "SKIPPED" for x in report["objects"]),
        }
        # Finalize BEFORE the manifest is written, in this same session, so the
        # manifest describes the .blend that actually shipped.
        state_restore_error = None
        if callable(before_finalize):
            try:
                before_finalize()
            except (RunCancelled, KeyboardInterrupt) as cancel_exc:
                _mark_cancelled(report, cancel_exc)
            except Exception as exc:
                state_restore_error = safe_text(exc)
                report["state_restore_error"] = state_restore_error
                print("STATE RESTORE before finalize FAILED: %s" % state_restore_error)
                traceback.print_exc()
        # Last cooperative boundary before an indivisible final save. A request
        # arriving during that save is too late to undo a completed artifact.
        if not report.get("cancelled"):
            try:
                _run_boundary("finalize")
            except (RunCancelled, KeyboardInterrupt) as cancel_exc:
                _mark_cancelled(report, cancel_exc)
        finalize_blockers = pre_finalize_blockers(report)
        if report.get("cancelled"):
            report["finalize"] = {"ran": False, "reason": "cancelled; completed checkpoints retained"}
        elif finalize_blockers:
            report["pre_finalize_blockers"] = finalize_blockers
            report["finalize"] = {
                "ran": False,
                "reason": "known failures; refusing to save a partial result",
                "blockers": finalize_blockers[:50],
            }
        elif state_restore_error is not None:
            report["finalize"] = {
                "ran": False,
                "reason": "state restore failed; refusing to save altered UI state",
                "state_restore_error": state_restore_error,
            }
        elif args.get("no-finalize"):
            report["finalize"] = {"ran": False, "reason": "--no-finalize"}
        else:
            try:
                finalize_session(report)
            except Exception as exc:
                report["finalize"] = {
                    "ran": False,
                    "save_error": safe_text(exc),
                    "reason": "unexpected finalizer exception",
                }
                traceback.print_exc()
        restore_repointed_filepaths()
        manifest = os.path.join(folder, "manifest.json")
        try:
            with open(manifest, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, sort_keys=True)
        except Exception:
            traceback.print_exc()
        print("=" * 78)
        print("DONE | OK={ok} FAILED={failed} SKIPPED={skipped}".format(**report["summary"]))
        print("Output:", folder)
        print("Manifest:", manifest)
        print("Original objects and materials were not modified.")
        if report.get("finalize", {}).get("ran"):
            print("Blend saved in-session by the finalize stage (packed, no .blend1).")
        print("=" * 78)
        try:
            census = failure_census(manifest, report)
            print("OVERALL | %s exit=%d | the DONE line above is per-object counts only (v3.3)"
                  % ("CLEAN" if not census.get("exit_code") else "FAIL", int(census.get("exit_code", 1))), flush=True)
            report["census"] = census
        finally:
            restore_log()
    return census


# v3.3b: main() now runs at the END of the file (after the ported V3.1 section), see the tail.
_CENSUS: Optional[Dict[str, Any]] = None


# ============================================================================
# PORTED FROM import_glue_bake_v3.py (V3.1) -- granularity + texel-density engine.
# Names verified non-colliding with the routing engine above; bake_v3's MIN_RES
# was renamed DENSITY_MIN_RES because the router already owns that name.
# ============================================================================

def group_key(obj):
    """First non-decal material name; if the object is decal-ONLY, its
    first decal material (when INCLUDE_DECALS) else None (= skip)."""
    first_decal = None
    for s in obj.material_slots:
        if s.material:
            if not is_decal_mat(s.material):
                return db_name(s.material, "material")
            if first_decal is None:
                first_decal = db_name(s.material, "material")
    return first_decal if INCLUDE_DECALS else None


def mesh_islands(me):
    """Connected-face islands (loose parts) as lists of face indices."""
    bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table()
    seen = [False] * len(bm.faces); islands = []
    for f in bm.faces:
        if seen[f.index]: continue
        stack = [f]; isl = []
        while stack:
            g = stack.pop()
            if seen[g.index]: continue
            seen[g.index] = True; isl.append(g.index)
            for e in g.edges:
                for lf in e.link_faces:
                    if not seen[lf.index]: stack.append(lf)
        islands.append(isl)
    bm.free(); return islands


def island_stats(me, islands):
    uv_name = choose_source_uv(me)
    if uv_name is None: raise RuntimeError("no usable source UV layer")
    uvl = uv_layer_by_name(me, uv_name)
    if uvl is None: raise RuntimeError("source UV layer disappeared")
    n = len(me.loops); buf = [0.0] * (n * 2)
    uvl.data.foreach_get("uv", buf)
    stats = []
    for isl in islands:
        u0 = v0 = 1e30; u1 = v1 = -1e30; tris = 0
        for fi in isl:
            p = me.polygons[fi]
            tris += max(p.loop_total - 2, 0)
            for li in range(p.loop_start, p.loop_start + p.loop_total):
                u = buf[2*li]; v = buf[2*li + 1]
                if u < u0: u0 = u
                if u > u1: u1 = u
                if v < v0: v0 = v
                if v > v1: v1 = v
        stats.append({'faces': isl, 'tris': tris,
                      'area': max(u1 - u0, 0.0) * max(v1 - v0, 0.0)})
    return stats


def make_bins(stats, nbins, budget):
    """Greedy: big islands first, into the emptiest bin that fits."""
    bins = []
    for st in sorted(stats, key=lambda s: -s['area']):
        if len(bins) < nbins:
            bins.append({'faces': list(st['faces']),
                         'tris': st['tris'], 'area': st['area']})
            continue
        cand = [b for b in bins if b['tris'] + st['tris'] <= budget]
        if not cand:
            bins.append({'faces': list(st['faces']),
                         'tris': st['tris'], 'area': st['area']})
        else:
            b = min(cand, key=lambda b: b['area'])
            b['faces'] += st['faces']; b['tris'] += st['tris']
            b['area'] += st['area']
    return [b for b in bins if b['faces']]


def spatial_bins(j, stats, nbins, budget):
    """Contiguous sections: sort loose parts along the weapon's longest
    axis, then fill bins in order. A screw stays with the section it
    lives in; chunks read as stock / receiver / barrel / grip."""
    me = j.data
    axis = max(range(3), key=lambda i: j.dimensions[i])
    for st in stats:
        faces = st['faces']
        step = max(1, len(faces) // 64)
        sample = faces[::step][:64]
        st['pos'] = sum(me.polygons[fi].center[axis] for fi in sample) / len(sample)
    order = sorted(stats, key=lambda s: s['pos'])
    total = sum(s['tris'] for s in order)
    share = max(1, min(budget, -(-total // max(nbins, 1))))
    bins = []
    cur = {'faces': [], 'tris': 0, 'area': 0.0}
    for st in order:
        if cur['faces'] and cur['tris'] + st['tris'] > share:
            bins.append(cur)
            cur = {'faces': [], 'tris': 0, 'area': 0.0}
        cur['faces'] += st['faces']; cur['tris'] += st['tris']
        cur['area'] += st['area']
    if cur['faces']: bins.append(cur)
    return bins


def per_object_chunks(sel, folder):
    """PER_OBJECT mode: every selected object -> its own repacked bake."""
    chunks, ok = [], 0
    pool = [o for o in sel if group_key(o) is not None]
    areas = {o.as_pointer(): src_uv_area(o) for o in pool}
    max_area = max(areas.values()) if areas else 1.0
    print("PER_OBJECT: %d object(s) (%d skipped: %s)"
          % (len(pool), len(sel) - len(pool),
             "no bakeable materials" if INCLUDE_DECALS else "decal-only/unbakeable"))
    for oi, o in enumerate(pool, 1):
        cbase = safe_name(db_name(o, "mesh_object"))
        _tp = time.time()
        vrule("[%d/%d] %s" % (oi, len(pool), cbase))
        c = None; mats = []
        try:
            bpy.ops.object.select_all(action='DESELECT')
            o.hide_set(False); o.select_set(True); vl.objects.active = o
            bpy.ops.object.duplicate()
            c = vl.objects.active; c.name = "MAX_" + cbase
            make_single_user([c])       # V2: never share data with originals
            bpy.ops.object.select_all(action='DESELECT')
            c.select_set(True); vl.objects.active = c
            strip_unbakeable(c)
            if len(c.data.polygons) == 0 or choose_source_uv(c.data) is None:
                print("   SKIP (no bakeable faces / no UVs)")
                bpy.data.objects.remove(c, do_unlink=True); continue
            mats = []
            for s_ in c.material_slots:
                m = s_.material
                if m and (INCLUDE_DECALS or not is_decal_mat(m)) \
                        and m not in mats:
                    mats.append(m)
            if not mats:
                print("   SKIP (no bakeable materials)")
                bpy.data.objects.remove(c, do_unlink=True); continue
            chunk_alpha = INCLUDE_DECALS and any(mat_has_alpha(m) for m in mats)
            src_area = src_uv_area(c)          # AFTER unbakeable faces stripped
            vlog("materials (%d):" % len(mats), 1)
            for m in mats:
                if not m.use_nodes or not find_output_and_principled(m.node_tree)[1]:
                    vlog("%s uses graph-probe fallback (group/mixed/non-Principled)"
                         % db_name(m, 'material')[:30], 2)
                else:
                    vlog("%-26s alpha=%-5s decal=%s" % (db_name(m, 'material')[:26],
                         mat_has_alpha(m), is_decal_mat(m)), 2)
            vlog("uv layers: %s | source UV area=%.5f"
                 % ([l.name for l in c.data.uv_layers], src_area), 1)
            t_pre = triangle_count(c)
            decimate_chunk(c)
            if triangle_count(c) != t_pre:
                vlog("decimate %.2f: %d -> %d tris" % (DECIMATE_RATIO, t_pre, triangle_count(c)), 1)
            b0, b1 = enforce_budget(c)
            if b1 != b0:
                vlog("tri-budget: collapse-decimated %d -> %d (<= %d)" % (b0, b1, TRI_BUDGET), 1)
            res = piece_res(src_area, max_area)
            require_material_dependencies(used_materials([o]))
            packed_name, gain = repack_uv(c, res)
            require_output_uv(c, "GRAPH_BAKE", packed_name)
            vlog("RES_MODE=%s -> %dpx  (src area %.5f / max %.5f, UV pack gain x%.2f)"
                 % (RES_MODE, res, src_area, max_area, gain), 1)
            for ch in ('Color','Rough','Metal','Normal'):
                if DO.get(ch):
                    _tb = time.time()
                    vlog("bake %-6s @%dpx ..." % (ch, res), 1)
                    _p = bake_channel(c, mats, ch, folder, cbase, res)
                    vlog("%-6s %5.1fs -> %s (%s)" % (ch, time.time() - _tb,
                         os.path.basename(_p or "?"), _fsize(_p) if _p else "?"), 2)
            if chunk_alpha and DO.get('Color'):
                _tb = time.time()
                vlog("bake Alpha (decal cutout) ...", 1)
                bake_alpha_into_color(c, mats, folder, cbase, res)
                vlog("Alpha %5.1fs" % (time.time() - _tb), 2)
            finalize_uv(c, packed_name)
            require_output_uv(c, "GRAPH_BAKE")
            strip_vertex_colors(c.data)   # AFTER bake, before export
            assign_preview(c, folder, cbase, alpha=chunk_alpha)
            remove_unused_materials(mats)
            t = triangle_count(c)
            flag = "  <-- OVER %d!" % TRI_BUDGET if t > TRI_BUDGET else ""
            vlog("done: %d tris, %dpx, UV density x%.1f, piece %.1fs%s"
                 % (t, res, gain, time.time() - _tp, flag), 1)
            chunks.append(c); ok += 1
        except Exception as e:
            print("   FAIL %s: %s" % (cbase, e)); traceback.print_exc()
            if c is not None:
                try: bpy.data.objects.remove(c, do_unlink=True)
                except Exception: pass
            remove_unused_materials(mats)
            for ch in ('Color','Rough','Metal','Normal'):
                try:
                    path = os.path.join(folder, "%s_%s.png" % (cbase, ch))
                    if os.path.exists(path): os.remove(path)
                except Exception: pass
    return chunks, ok


def extract_chunk(j, keep, name):
    bpy.ops.object.select_all(action='DESELECT')
    j.hide_set(False); j.select_set(True); vl.objects.active = j
    bpy.ops.object.duplicate()
    c = vl.objects.active; c.name = name
    make_single_user([c])          # V2: never share mesh data with the source join
    me = c.data
    bm = bmesh.new(); bm.from_mesh(me); bm.faces.ensure_lookup_table()
    rm = [f for f in bm.faces if f.index not in keep]
    if rm: bmesh.ops.delete(bm, geom=rm, context='FACES')
    bm.to_mesh(me); bm.free()
    return c


def duplicate_and_join(objs, name):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs: o.hide_set(False); o.select_set(True)
    vl.objects.active = objs[0]
    bpy.ops.object.duplicate()
    dups = [o for o in bpy.context.selected_objects]
    make_single_user(dups)          # V2: never share data with originals
    vl.objects.active = dups[0]
    if len(dups) > 1: bpy.ops.object.join()
    j = vl.objects.active; j.name = name; return j


def enforce_budget(c):
    """If a chunk still exceeds TRI_BUDGET, collapse-decimate it to fit
    BEFORE baking so the exported MeshPart is import-ready and its baked
    textures match the final topology. Returns (before, after) tri counts."""
    t = triangle_count(c)
    if not ENFORCE_TRI_BUDGET or t <= TRI_BUDGET:
        return t, t
    ratio = max(0.02, min(0.99, TRI_BUDGET / float(t)))
    m = c.modifiers.new("__BUDGET", 'DECIMATE')
    m.ratio = ratio
    try: bpy.ops.object.modifier_apply(modifier=m.name)
    except Exception:
        try: c.modifiers.remove(m)
        except Exception: pass
    return t, triangle_count(c)


def decimate_chunk(c):
    """Optional tri shave BEFORE baking so textures match the final mesh."""
    if DECIMATE_RATIO >= 0.999: return
    m = c.modifiers.new("__DEC", 'DECIMATE')
    m.ratio = DECIMATE_RATIO
    bpy.ops.object.modifier_apply(modifier=m.name)


def make_single_user(objs):
    """Force each object to own PRIVATE copies of its mesh AND materials.
    bpy.ops.object.duplicate() shares this data with the source unless the
    matching Preferences > Editing > Duplicate Data boxes are ticked
    (Material is OFF by default), so without this the bake's UV repack /
    join / temp bake nodes can mutate the REAL objects. Call right after
    every duplicate -- makes the "originals untouched" promise real."""
    for o in objs:
        if o.data is not None and o.data.users > 1:
            o.data = o.data.copy()
        for sl in o.material_slots:
            if sl.material is not None and sl.material.users > 1:
                sl.material = sl.material.copy()


def strip_unbakeable(j):
    bad = {i for i, s in enumerate(j.material_slots)
           if s.material is None or (not INCLUDE_DECALS and is_decal_mat(s.material))}
    if not bad:
        return
    me = j.data
    if len(bad) < len(j.material_slots):
        bm = bmesh.new(); bm.from_mesh(me)
        rm = [f for f in bm.faces if f.material_index in bad]
        if rm: bmesh.ops.delete(bm, geom=rm, context='FACES')
        bm.to_mesh(me); bm.free()
    else:
        me.clear_geometry()
    try: bpy.ops.object.material_slot_remove_unused()
    except Exception: pass


def _pot(n):
    """Smallest power of two >= ceil(n) (min 8). Rounds UP so a computed
    resolution never silently undershoots (e.g. 180 -> 256, not 128)."""
    import math
    n = max(8, int(math.ceil(n)))
    p = 1
    while p < n: p <<= 1
    return p


def piece_res(src_uv_area, max_uv_area):
    """Bake resolution for ONE piece from its SOURCE UV footprint (the
    area it fills in its original, pre-repack UV layer). See RES_MODE:
      FLAT            -> RES for everyone
      RELATIVE        -> RES * sqrt(area / biggest area), floor DENSITY_MIN_RES
      DENSITY         -> sqrt(area) * TEXELS_FULL_UV, no clamp
      DENSITY_CLAMPED -> DENSITY clamped to [DENSITY_MIN_RES, RES]"""
    if RES_MODE == 'FLAT' or src_uv_area <= 0.0:
        r = RES
    elif RES_MODE == 'RELATIVE':
        ratio = (src_uv_area / max_uv_area) ** 0.5 if max_uv_area > 1e-12 else 1.0
        r = max(DENSITY_MIN_RES, RES * ratio)
    else:
        r = (src_uv_area ** 0.5) * TEXELS_FULL_UV
        if RES_MODE == 'DENSITY_CLAMPED':
            r = min(RES, max(DENSITY_MIN_RES, r))
    return _pot(r) if POT_SNAP else int(max(8, round(r)))


def uv_area(me, layer_ref):
    """Total UV-space area of all triangles in a layer."""
    me.calc_loop_triangles()
    layer = uv_layer_by_name(me, layer_ref) if isinstance(layer_ref, str) else layer_ref
    if layer is None: raise RuntimeError("UV layer disappeared")
    uvd = layer.data
    a = 0.0
    for tri in me.loop_triangles:
        l0, l1, l2 = tri.loops
        u0, v0 = uvd[l0].uv
        u1, v1 = uvd[l1].uv
        u2, v2 = uvd[l2].uv
        a += abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0)) * 0.5
    return a


def src_uv_area(obj):
    """Total triangle area of the renderer/source UV layer."""
    me = obj.data
    name = choose_source_uv(me)
    return uv_area(me, name) if name else 0.0


def face_uv_areas(me):
    """Source-UV triangle area summed per polygon index (active layer).
    Lets group-mode size each chunk by the UV footprint of its faces on
    the joined mesh, BEFORE repack rescales everything to fill 0-1."""
    me.calc_loop_triangles()
    uv_name = choose_source_uv(me)
    if uv_name is None: return {}
    layer = uv_layer_by_name(me, uv_name)
    if layer is None: return {}
    uvd = layer.data
    per = {}
    for tri in me.loop_triangles:
        l0, l1, l2 = tri.loops
        u0, v0 = uvd[l0].uv; u1, v1 = uvd[l1].uv; u2, v2 = uvd[l2].uv
        a = abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0)) * 0.5
        per[tri.polygon_index] = per.get(tri.polygon_index, 0.0) + a
    return per


def _find_uv_area():
    """Find an open Image/UV editor for a valid pack_islands context
    (returns window, area, region -- or three Nones if none is open)."""
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                    if region is not None:
                        return win, area, region
    except Exception:
        pass
    return None, None, None


def _collection_tris():
    """{name: (n_mesh_objs, total_tris)} over collections with meshes
    (uses all_objects, so nested child collections are included)."""
    out = {}
    for coll in bpy.data.collections:
        meshes = [o for o in coll.all_objects if o.type == 'MESH']
        if not meshes: continue
        tt = 0
        for o in meshes:
            try: o.data.calc_loop_triangles(); tt += len(o.data.loop_triangles)
            except Exception: pass
        out[coll.name] = (len(meshes), tt)
    return out


def is_decal_mat(m): return m is not None and DECAL_HINT in db_name(m, "").lower()


def mat_has_alpha(m):
    """Alpha-blended material (decals, glass): needs RGBA ColorMap."""
    if not (m and m.use_nodes): return False
    try:
        if getattr(m, "blend_method", 'OPAQUE') != 'OPAQUE': return True
    except Exception: pass
    for nt in iter_node_trees(m.node_tree):
        for node in nt.nodes:
            if node.type in {'BSDF_TRANSPARENT', 'HOLDOUT'}:
                return True
            if node.type == 'BSDF_PRINCIPLED':
                a = node.inputs.get('Alpha')
                if a is None: continue
                if a.is_linked: return True
                try:
                    if float(a.default_value) < 0.999: return True
                except Exception: pass
    return False


def _snapshot_scene():
    state = {}
    for key, owner, attr in (
        ('engine', scn.render, 'engine'), ('samples', scn.cycles, 'samples'),
        ('persistent', scn.render, 'use_persistent_data'),
        ('device', scn.cycles, 'device')):
        try: state[key] = getattr(owner, attr)
        except Exception: pass
    state['bake'] = {}
    for attr in ('use_clear','use_selected_to_active','margin','margin_type',
                 'normal_space','normal_r','normal_g','normal_b','target'):
        try: state['bake'][attr] = getattr(scn.render.bake, attr)
        except Exception: pass
    try:
        cp = bpy.context.preferences.addons['cycles'].preferences
        cp.get_devices()
        state['cycles_prefs'] = (cp.compute_device_type,
            {(d.name, d.type): bool(d.use) for d in cp.devices})
    except Exception:
        state['cycles_prefs'] = None
    return state


def _restore_scene():
    try:
        if 'engine' in _SCENE_STATE: scn.render.engine = _SCENE_STATE['engine']
        if 'samples' in _SCENE_STATE: scn.cycles.samples = _SCENE_STATE['samples']
        if 'persistent' in _SCENE_STATE:
            scn.render.use_persistent_data = _SCENE_STATE['persistent']
        if 'device' in _SCENE_STATE: scn.cycles.device = _SCENE_STATE['device']
        for attr, value in _SCENE_STATE.get('bake', {}).items():
            try: setattr(scn.render.bake, attr, value)
            except Exception: pass
        saved = _SCENE_STATE.get('cycles_prefs')
        if saved:
            cp = bpy.context.preferences.addons['cycles'].preferences
            cp.compute_device_type = saved[0]; cp.get_devices()
            for device in cp.devices:
                key = (device.name, device.type)
                if key in saved[1]: device.use = saved[1][key]
    except Exception:
        pass


def ensure_gpu():
    """scn.cycles.device='GPU' is only a scene flag -- the REAL device is
    Preferences > System > Cycles Render Devices. If that is NONE, Cycles
    silently bakes on CPU. Force OptiX (CUDA fallback) at the prefs level,
    same as highest_quality.py, and report what is actually in use."""
    try: scn.cycles.device = 'GPU'
    except Exception: pass
    try:
        cprefs = bpy.context.preferences.addons['cycles'].preferences
        picked = None
        for dtype in ('OPTIX', 'CUDA'):
            try:
                cprefs.compute_device_type = dtype
                cprefs.get_devices()
                if any(d.type == dtype for d in cprefs.devices):
                    picked = dtype; break
            except Exception:
                continue
        if picked:
            names = []
            for d in cprefs.devices:
                d.use = (d.type == picked)
                if d.use: names.append(d.name)
            print("CYCLES DEVICE: %s -> %s" % (picked, ", ".join(names)))
            vlog("all detected compute devices:", 1)
            for d in cprefs.devices:
                vlog("%-6s use=%-5s %s" % (d.type, d.use, d.name), 2)
            return picked
        print("CYCLES DEVICE: !! no OptiX/CUDA GPU found -> baking on CPU")
        print("   (RTX 50xx needs Blender 4.3+ AND a current NVIDIA driver)")
    except Exception as e:
        print("CYCLES DEVICE: !! could not set GPU prefs (%s) -> CPU" % e)
    try: scn.cycles.device = 'CPU'
    except Exception: pass
    return 'CPU'


def _headless_select(target):
    """Select all meshes in a collection (job-runner use). target is a
    collection name, or 'AUTO_MIN_TRIS' to pick the fewest-triangle one."""
    vrule("HEADLESS COLLECTION SELECT: %r" % target)
    tris = _collection_tris()
    if not tris:
        vlog("no collections contain mesh objects", 1); return
    for name, (n, t) in sorted(tris.items(), key=lambda kv: kv[1][1]):
        vlog("%-40s objs=%-4d tris=%d" % (name[:40], n, t), 1)
    pick = (min(tris.items(), key=lambda kv: kv[1][1])[0]
            if target == 'AUTO_MIN_TRIS' else target)
    if pick not in bpy.data.collections:
        vlog("!! collection %r not found -- keeping selection" % pick, 1); return
    bpy.ops.object.select_all(action='DESELECT')
    n = 0
    for o in bpy.data.collections[pick].all_objects:
        if o.type != 'MESH': continue
        try:
            o.hide_set(False); o.select_set(True); n += 1
            bpy.context.view_layer.objects.active = o
        except Exception as e:
            vlog("could not select %s (%s)" % (o.name, e), 2)
    vlog("-> picked %r : %d mesh object(s) selected" % (pick, n), 1)


def vlog(msg="", indent=0):
    print("[%8.2fs] %s%s" % (time.time() - _T0, "  " * indent, msg), flush=True)


def vrule(title):
    print("\n" + "=" * 72, flush=True)
    print("[%8.2fs] %s" % (time.time() - _T0, title), flush=True)
    print("=" * 72, flush=True)


def warn_unsupported(mats, tag=""):
    """Report materials that require the V3 graph-probe fallback."""
    bad = [db_name(m, 'material') for m in mats
           if not m.use_nodes or find_output_and_principled(m.node_tree)[1] is None]
    if bad:
        print("   [%s] graph-probe fallback for group/mixed/non-Principled: %s" % (tag, bad))


def safe_name(s):
    s = safe_text(s, "part")
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40]
    return "%s_%s" % (base, hashlib.md5(s.encode("utf-8", "replace")).hexdigest()[:6])


def db_name(value, fallback="unnamed"):
    try: return safe_text(value.name, fallback)
    except Exception: return fallback



# captured once at import, restored by _restore_scene() after a bake run
try:
    _SCENE_STATE = _snapshot_scene()
except Exception:
    _SCENE_STATE = None


# v3.3b: the run itself. Everything in this file - routing engine AND the ported V3.1 granularity
# section - is defined by now, so enforce_budget / piece_res / per_object_chunks are reachable from main().
if __name__ == "__main__":
    try:
        _CENSUS = main()
    finally:
        restore_log()


# "saved" is never "worked": a direct script run that produced a failed,
# black-suspect or channel-incomplete part leaves the process with a nonzero
# exit code, so a supervisor cannot mistake a written manifest for a good one.
# Last statement in the file: everything above is defined by the time it fires.
#
# v3.4: sys.exit() here used to fire unconditionally under __name__=="__main__" --
# which Blender's Text Editor "Run Script" ALSO sets, exactly the interactive
# click-an-object-and-run path this build targets. With every CENSUS_FAIL_ON_*
# flag defaulting True, one SKIPPED/black-suspect/magenta object in an ordinary
# messy scene would raise SystemExit inside the user's live Blender session --
# after finalize_session() already saved the .blend, so no work is lost, but an
# uncontrolled SystemExit inside an interactive script run is still a startling,
# unnecessary way to report a failure to someone sitting at the keyboard. A
# headless (`blender -b ... -P/--python`) run has no "someone at the keyboard"
# to startle and a supervisor genuinely needs the process exit code, so this
# still exits there. bpy.app.background is True only for -b launches; it is not
# a config flag, it reflects how Blender was actually started.
_SHOULD_EXIT_ON_CENSUS = __name__ == "__main__" and CENSUS_EXIT_NONZERO and (
    bpy.app.background or CENSUS_EXIT_INTERACTIVE_TOO
)
if _SHOULD_EXIT_ON_CENSUS and _CENSUS and _CENSUS.get("exit_code"):
    print("EXIT %d per FAILURE CENSUS" % int(_CENSUS["exit_code"]), flush=True)
    sys.exit(int(_CENSUS["exit_code"]))
elif __name__ == "__main__" and CENSUS_EXIT_NONZERO and _CENSUS and _CENSUS.get("exit_code") \
        and not _SHOULD_EXIT_ON_CENSUS:
    print("CENSUS reported failures (exit_code=%d) but this is an interactive session -- "
          "not exiting Blender. The .blend was already saved by finalize; see the census "
          "above for what needs attention. Set CENSUS_EXIT_INTERACTIVE_TOO=True to change this."
          % int(_CENSUS["exit_code"]), flush=True)
if _SHOULD_EXIT_ON_CENSUS and _NOTHING_SELECTED:
    # v3.3: a run that selected nothing wrote no manifest; exit 0 here was indistinguishable from success
    print("EXIT 3 nothing selected (bad --only name, or nothing bakeable)", flush=True)
    sys.exit(3)
elif __name__ == "__main__" and CENSUS_EXIT_NONZERO and _NOTHING_SELECTED and not _SHOULD_EXIT_ON_CENSUS:
    print("Nothing selected (bad --only name, or nothing bakeable) -- not exiting Blender "
          "(interactive session).", flush=True)
