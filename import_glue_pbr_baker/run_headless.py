"""Standalone launcher retained for ``blender -b file.blend -P`` workflows.

This file may stay inside an extracted add-on folder or be copied elsewhere.
It locates the package from its own parent, then delegates to the packaged
headless entry point.
"""
import os
import sys


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_PARENT = os.path.dirname(PACKAGE_DIR)
if PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, PACKAGE_PARENT)

try:
    from import_glue_pbr_baker.headless import main
except Exception as _import_error:  # launcher setup only
    # `blender -P` swallows a module-level exception and still exits 0, which
    # the contract reads as a clean census.  A folder renamed away from the
    # package name every relative import needs must fail closed as setup (2).
    print("MACHINE|setup_error|cannot import import_glue_pbr_baker from %s (%s); "
          "the extracted folder must keep that exact name"
          % (PACKAGE_PARENT, _import_error), flush=True)
    raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _run_error:
        # Same reason: an unhandled engine error must never be reported as 0.
        import traceback
        traceback.print_exc()
        print("MACHINE|run_error|%s: %s" % (type(_run_error).__name__, _run_error), flush=True)
        raise SystemExit(1)

