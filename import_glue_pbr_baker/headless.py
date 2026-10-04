"""Background-mode entry point for the packaged Import Glue engine."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional

import bpy

from . import engine, precheck


# Every flag this launcher and engine.main() actually consume.  cli_args()
# harvests any --token, so a key nobody reads is a typo rather than an option:
# v1.1.0 accepted `--collectin Body` and silently baked the whole scene.
KNOWN_FLAGS = frozenset({
    "collection", "only", "done", "no-resume", "rerun", "no-finalize", "repair",
    "tag", "mem-gate", "res", "max-res", "density", "device", "route",
    "output-root",
})


def _setup_error(message: str) -> SystemExit:
    """Exit 2 is the documented launcher setup/selection code; 3 means the
    scope parsed fine but resolved to nothing.  Every refusal of user input
    goes through here so the machine-readable line is never forgotten."""
    print("MACHINE|setup_error|" + message, flush=True)
    print("run_headless: " + message, flush=True)
    return SystemExit(2)


def _flag(name: str) -> Optional[str]:
    """Read one value-taking flag from the tail after ``--``, failing closed.

    v1.1.0 returned None whenever the next token began with ``--``, and took
    the FIRST of a repeated flag while engine.cli_args() took the LAST, so
    `--collection --res 512` silently meant "whole scene" and
    `--collection A --collection B` validated B but scoped the run to A.
    """
    if "--" not in sys.argv:
        return None
    tail = sys.argv[sys.argv.index("--") + 1:]
    if name not in tail:
        return None
    if tail.count(name) > 1:
        raise _setup_error("%s given %d times; supply it once" % (name, tail.count(name)))
    index = tail.index(name)
    if index + 1 >= len(tail) or tail[index + 1].startswith("--"):
        raise _setup_error("%s requires a value" % name)
    value = tail[index + 1].strip()
    if not value:
        raise _setup_error("%s requires a non-empty value" % name)
    return value


def _unexclude(layer_collection: Any) -> None:
    layer_collection.exclude = False
    layer_collection.hide_viewport = False
    try:
        layer_collection.collection.hide_viewport = False
    except Exception:
        pass
    for child in layer_collection.children:
        _unexclude(child)


def select_sources() -> List[Any]:
    view_layer = bpy.context.view_layer
    _unexclude(view_layer.layer_collection)
    collection_name = _flag("--collection")
    if collection_name:
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            print(
                "run_headless: no collection named %r; available: %s"
                % (collection_name, [collection.name for collection in bpy.data.collections]),
                flush=True,
            )
            raise SystemExit(2)
        pool = list(collection.all_objects)
    else:
        pool = list(view_layer.objects)
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    selected = []
    for obj in pool:
        if obj.type != "MESH" or not obj.data.polygons or engine.is_previous_output(obj):
            continue
        try:
            # Linked object IDs can make this global flag read-only.  Failure
            # here must not prevent the writable view-layer selection below.
            obj.hide_viewport = False
        except Exception as exc:
            print(
                "run_headless: could not clear global visibility for %s (%s)"
                % (obj.name, exc),
                flush=True,
            )
        try:
            obj.hide_set(False)
            obj.select_set(True)
            view_layer.objects.active = obj
            selected.append(obj)
        except Exception as exc:
            print("run_headless: could not select %s (%s)" % (obj.name, exc), flush=True)
    print(
        "run_headless: selected %d source mesh object(s) of %d considered"
        % (len(selected), len(pool)),
        flush=True,
    )
    if not selected:
        # 2 means the operator must fix the invocation.  A valid scope that
        # simply holds nothing bakeable is the same "empty engine scope"
        # engine.main() reports as 3 when it is the one to notice first.
        print("MACHINE|empty_scope|source=select_sources considered=%d" % len(pool), flush=True)
        raise SystemExit(3)
    return selected


def _apply_runtime_flags(args: Any) -> None:
    def fail(message: str) -> None:
        print("run_headless: " + message, flush=True)
        raise SystemExit(2)

    def integer_flag(name: str) -> Optional[int]:
        if name not in args:
            return None
        value = args.get(name)
        if not isinstance(value, str):
            fail("--%s requires a value" % name)
        try:
            parsed = int(value)
        except ValueError:
            fail("--%s must be an integer" % name)
        if not 4 <= parsed <= 8192:
            fail("--%s must be between 4 and 8192" % name)
        return parsed

    def density_flag(name: str) -> Optional[float]:
        """Same fail-closed shape as integer_flag(), for BAKE_DENSITY_SCALE.

        The range mirrors the add-on's Bake Density hard limits (0.05-8.0).
        Refusing here matters more than for --res: the density multiplies an
        estimate rather than capping one, so a value that quietly fell back to
        1.0 would ship a whole run at the wrong texel density with nothing in
        the log to say so.  `nan`/`inf` parse as floats and are caught by the
        range comparison below, which is false for both.
        """
        if name not in args:
            return None
        value = args.get(name)
        if not isinstance(value, str):
            fail("--%s requires a value" % name)
        try:
            parsed = float(value)
        except ValueError:
            fail("--%s must be a number" % name)
        if not 0.05 <= parsed <= 8.0:
            fail("--%s must be between 0.05 and 8.0" % name)
        return parsed

    resolution = integer_flag("res")
    maximum = integer_flag("max-res")
    density = density_flag("density")
    if resolution is not None:
        engine.RES = resolution
    if maximum is not None:
        engine.MAX_RES = maximum
    if engine.MAX_RES < engine.RES:
        fail("--max-res must be at least --res")
    if density is not None:
        engine.BAKE_DENSITY_SCALE = density
    if "device" in args:
        if not isinstance(args.get("device"), str):
            fail("--device requires GPU or CPU")
        device = args["device"].upper()
        if device not in {"GPU", "CPU"}:
            fail("--device must be GPU or CPU")
        engine.DEVICE = device
    if "route" in args:
        if not isinstance(args.get("route"), str):
            fail("--route requires a value")
        route = args["route"].upper()
        if route not in {"AUTO", "CROP_ONLY", "PROXY_ONLY", "GRAPH_ONLY", "BAKE_ONLY"}:
            fail("--route is not a supported route mode")
        engine.ROUTE_MODE = route
    if "output-root" in args:
        if not isinstance(args.get("output-root"), str) or not args["output-root"].strip():
            fail("--output-root requires a path")
        engine.OUTPUT_ROOT = os.path.abspath(args["output-root"].strip())
    if "mem-gate" in args:
        # engine.main() only calls float() on this after planning has already
        # minted a run folder, so a typo must be refused before any output.
        if not isinstance(args.get("mem-gate"), str):
            fail("--mem-gate requires a value in GB")
        try:
            gate = float(args["mem-gate"])
        except ValueError:
            fail("--mem-gate must be a number of GB")
        if not 0.0 < gate <= 1024.0:
            fail("--mem-gate must be between 0 and 1024 GB")


def main() -> None:
    if not bpy.app.background:
        raise RuntimeError("The headless entry point requires Blender -b/background mode.")
    args = engine.cli_args()
    unknown = sorted(set(args) - KNOWN_FLAGS)
    if unknown:
        raise _setup_error(
            "unknown flag(s) %s; supported: %s"
            % (", ".join("--" + name for name in unknown),
               ", ".join("--" + name for name in sorted(KNOWN_FLAGS)))
        )
    _apply_runtime_flags(args)
    if "collection" in args:
        # Checked here because select_sources() unexcludes every collection as
        # its first act: a refused run must not have mutated the scene.
        collection_name = args.get("collection")
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise _setup_error("--collection requires a collection name")
    selected = select_sources()
    only_scope = engine.only_names(args.get("only"))
    precheck_objects = selected if only_scope is None else [
        obj for obj in selected if engine.datablock_name(obj) in only_scope
    ]
    if engine.DONE_LIST and not args.get("no-resume"):
        done_override = args.get("done")
        if done_override and done_override is not True:
            done_path = engine.safe_text(done_override)
        else:
            blend_dir = engine.OUTPUT_ROOT or os.path.dirname(bpy.data.filepath) or bpy.app.tempdir
            stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "scene"
            done_path = os.path.join(blend_dir, stem + "_exports", engine.DONE_LIST_NAME)
        done = engine.load_done_list(done_path).get("objects", {})
        filtered = []
        for obj in precheck_objects:
            entry = done.get(engine.datablock_name(obj), {})
            # v1.1.1: --rerun clears strikes in engine.main(), so an object that is
            # about to be re-baked must still be prechecked -- otherwise the one gate
            # that would explain WHY it kept failing is the one thing skipped.
            poisoned = (
                not args.get("rerun")
                and int(entry.get("strikes", 0)) >= engine.POISON_STRIKES
            )
            # Content-addressed resume validation requires the resolver's source
            # index, which engine.main() builds once and reuses.  Prechecking an
            # apparently carried source is cheap and prevents stale/missing
            # inputs from being hidden before that authoritative validation.
            if not poisoned:
                filtered.append(obj)
        precheck_objects = filtered
    if precheck_objects:
        # v1.2.1 (patch D): precheck must never write into the source tree.
        # Markers + report land under the output root; repair (which
        # materializes files beside the .blend by design) is explicit opt-in
        # via --repair; dead ABSOLUTE references block like relative ones.
        repair_opt = bool(args.get("repair"))
        stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "scene"
        if engine.OUTPUT_ROOT:
            precheck_dir = os.path.join(engine.OUTPUT_ROOT, "precheck_" + stem)
        else:
            blend_dir = os.path.dirname(bpy.data.filepath) or bpy.app.tempdir
            precheck_dir = os.path.join(blend_dir, stem + "_exports", "precheck")
        checked = precheck.run(
            objects=precheck_objects,
            marker_dir=precheck_dir,
            repair=repair_opt,
            repair_unused=repair_opt,
            # v1.2.1: resolve a missing reference by repointing the datablock for
            # this run instead of writing into the source tree. --repair remains
            # the explicit opt-in for materialising files beside the .blend.
            repair_into_staging=not repair_opt,
            strict_absolute=True,
            raise_on_failure=True,
        )
        for name, original in (checked.get("repointed_originals") or []):
            engine.note_repointed_filepath(name, original)
    census = engine.main()
    if census is None:
        raise SystemExit(3)
    exit_code = int(census.get("exit_code", 1))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
