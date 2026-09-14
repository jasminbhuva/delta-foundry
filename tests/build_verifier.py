#!/usr/bin/env python3
"""Verifier image build step (runs once during `docker build`, as root).

1. Build the pristine tree with the fixed recipe; install the result as the
   baseline and oracle binaries (root-owned, read-only).
2. Keep that prebuilt tree at /work/cand, owned by the unprivileged build
   user, so a candidate rebuild only recompiles what the overlay changes.
3. Verify the sealed excerpt bundles against the workload's pinned digests
   and materialize every sealed case under /opt/df/cases.
4. Record integrity digests in /opt/df/sealed/integrity.json.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dfcore import build, corpus  # noqa: E402

ROOT = "/opt/df"
BUILD_UID = 61010


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    src = ROOT + "/pristine-src"
    # Pristine tree built and installed to /usr/local so baseline/oracle git
    # (and case materialization, which spawns git-index-pack) are self-contained.
    pristine = "/work/pristine-build"
    shutil.copytree(src, pristine, symlinks=True)
    ok, wall, tail = build.make(pristine, log_path="/work/prebuild.log")
    if not ok:
        sys.exit("pristine build failed:\n" + tail)
    build.install(pristine)
    print("pristine build+install: %.1f s" % wall, flush=True)
    git = "/usr/local/bin/git"
    # Candidate build tree: a second pristine copy, owned by the build user, so a
    # candidate rebuild recompiles only what the overlay changed.
    tree = "/work/cand"
    shutil.copytree(src, tree, symlinks=True)
    subprocess.run(["cp", os.path.join(pristine, "git"), os.path.join(tree, "git")], check=True)
    build.make(tree, log_path="/work/cand-prebuild.log")  # ensure objects present for incremental
    subprocess.run(["chown", "-R", "%d:%d" % (BUILD_UID, BUILD_UID), tree], check=True)
    os.chmod("/work", 0o711)
    wl = json.load(open(os.path.join(HERE, "workloads.json")))
    exdir = os.path.join(ROOT, "sealed", "excerpts")
    for name, digest in sorted(wl["excerpts"].items()):
        got = sha256_file(os.path.join(exdir, name + ".bundle"))
        if got != digest:
            sys.exit("excerpt %s digest mismatch: %s" % (name, got))
    cases_dir = os.path.join(ROOT, "cases")
    os.makedirs(cases_dir)
    work = tempfile.mkdtemp(prefix="dfgen-", dir="/work")
    names = sorted({c["params"]["excerpt"] for c in wl["cases"] if "excerpt" in c["params"]})
    ex = corpus.load_excerpts(git, exdir, names, work)
    integ = {"baseline_git": sha256_file(git), "oracle_git": sha256_file(git), "manifests": {}}
    for c in wl["cases"]:
        out = os.path.join(cases_dir, c["id"])
        corpus.materialize(c, git, out, ex, work)
        for r, dirs, files in os.walk(out):
            for d in dirs:
                os.chmod(os.path.join(r, d), 0o755)
            for f in files:
                os.chmod(os.path.join(r, f), 0o644)
        os.chmod(out, 0o700)
        integ["manifests"][c["id"]] = sha256_file(os.path.join(out, "manifest.tsv"))
    for e in ex.values():
        e.close()
    shutil.rmtree(work)
    os.chmod(cases_dir, 0o711)
    with open(os.path.join(ROOT, "sealed", "integrity.json"), "w") as f:
        json.dump(integ, f, indent=1, sort_keys=True)
    os.chmod(os.path.join(ROOT, "sealed", "integrity.json"), 0o600)
    shutil.rmtree(pristine, ignore_errors=True)
    print("materialized %d sealed cases" % len(wl["cases"]), flush=True)


if __name__ == "__main__":
    main()
