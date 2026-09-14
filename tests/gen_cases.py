#!/usr/bin/env python3
"""Materialize every case of a workload file.

usage: gen_cases.py GIT WORKLOAD.json EXCERPT_DIR OUT_DIR [--only ID ...] [--jobs N]
"""

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dfcore import corpus  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("git")
    ap.add_argument("workload")
    ap.add_argument("excerpts")
    ap.add_argument("out")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    wl = json.load(open(args.workload))
    cases = wl["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    names = sorted({c["params"]["excerpt"] for c in cases if "excerpt" in c["params"]})
    os.makedirs(args.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="dfgen-", dir=os.environ.get("DF_GEN_TMP", "/tmp"))
    ex = corpus.load_excerpts(args.git, args.excerpts, names, work)
    summary = {}
    t0 = time.time()
    for c in cases:
        t = time.time()
        info = corpus.materialize(c, args.git, os.path.join(args.out, c["id"]), ex, work)
        summary[c["id"]] = {"objects": info["objects"], "raw_bytes": info["raw_bytes"],
                            "seconds": round(time.time() - t, 2)}
        if c["kind"] == "large":
            print(c["id"], json.dumps(summary[c["id"]]), flush=True)
    for e in ex.values():
        e.close()
    print("materialized %d cases in %.1f s" % (len(cases), time.time() - t0), flush=True)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
