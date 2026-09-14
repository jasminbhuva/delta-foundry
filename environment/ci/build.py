#!/usr/bin/env python3
"""Build /app/build/git from the allowlisted files in /app/git.

This applies exactly the verifier's collection rules and build recipe (see
/app/ci/SOURCE_RULES.md): only allowlisted regular files are taken from
/app/git; every other file comes from the pristine Git v2.55.0 tree. The
build is incremental in /app/build.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dfcore import build  # noqa: E402

PRISTINE = "/opt/delta-foundry/pristine-src"
TREE = "/app/build"
SUBMISSION = "/app/git"
FP = os.path.join(TREE, ".df-fingerprint")


def main():
    if "--install-pristine" in sys.argv:
        tree = sys.argv[sys.argv.index("--install-pristine") + 1]
        ok, wall, tail = build.make(tree, log_path=os.path.join(tree, ".build.log"))
        if not ok:
            print(tail[-3000:])
            return 1
        build.install(tree)
        print("built and installed pristine git in %.1f s" % wall)
        return 0
    if "--prebuild" in sys.argv:
        ok, wall, tail = build.make(TREE, log_path=os.path.join(TREE, ".build.log"))
        if not ok:
            print(tail)
            return 1
        with open(FP, "w") as f:
            f.write(build.fingerprint(TREE) + "\n")
        print("prebuilt pristine tree in %.1f s" % wall)
        return 0
    t0 = time.monotonic()
    try:
        overlay, report = build.collect(SUBMISSION, PRISTINE)
    except build.CollectError as e:
        print("REJECTED by source rules: %s" % e)
        print("(the verifier would fail the gate with the same error)")
        return 2
    print("changed allowlisted files: %s" % (", ".join(report["changed_allowlisted"]) or "none"))
    if report["ignored_changes"]:
        print("warning: these files differ from pristine but are NOT built (outside the allowlist):")
        for p in report["ignored_changes"]:
            print("  " + p)
    written = build.apply_overlay(TREE, PRISTINE, overlay)
    if os.path.exists(FP):
        os.unlink(FP)
    ok, wall, tail = build.make(TREE, log_path=os.path.join(TREE, ".build.log"))
    if not ok:
        print(tail[-6000:])
        print("BUILD FAILED (%.1f s); full log: %s" % (wall, os.path.join(TREE, ".build.log")))
        return 1
    with open(FP, "w") as f:
        f.write(build.fingerprint(TREE) + "\n")
    print("built %s/git in %.1f s (%d file(s) updated; recipe: %s)" % (TREE, time.monotonic() - t0,
                                                                    len(written), build.recipe_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
