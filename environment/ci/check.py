#!/usr/bin/env python3
"""Delta Foundry public self-check (offline).

  /app/ci/check.sh --fast   48 small cases + 2 public compression cases
  /app/ci/check.sh --full   192 small cases + 6 public compression cases (one per family)

It measures /app/build/git (built by /app/ci/build.sh from the allowlisted
files in /app/git) against the pristine baseline with the same oracle, gate,
limits, profile and six alternating pairs as the sealed verifier, on public
cases only. The objective is averaged over the public cases actually run.
"""

import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dfcore import build, oracle, runner, scoring  # noqa: E402

PUB = "/opt/delta-foundry"
BASELINE_GIT = "/usr/local/bin/git"
ORACLE_GIT = "/usr/local/bin/git"
CASES = PUB + "/public-cases"
PRISTINE_SRC = PUB + "/pristine-src"
BUILD_TREE = "/app/build"
SUBMISSION = "/app/git"
WORKLOADS = HERE + "/public-workloads.json"
UIDS = {"enc": (61011, 61011), "ora": (61012, 61012)}


fingerprint = build.fingerprint


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fast", action="store_true")
    g.add_argument("--full", action="store_true")
    ap.add_argument("--case", action="append", help="run only these case ids")
    ap.add_argument("--pairs", type=int, default=runner.PAIRS, help="alternating pairs (default 6, as verifier)")
    ap.add_argument("--report", default="/tmp/delta-foundry-check/report.json")
    args = ap.parse_args()
    mode = "full" if args.full else "fast"
    wl = json.load(open(WORKLOADS))
    cases = [c for c in wl["cases"] if mode == "full" or c.get("fast")]
    if args.case:
        cases = [c for c in cases if c["id"] in set(args.case)]
    small = [c for c in cases if c["kind"] == "small"]
    large = [c for c in cases if c["kind"] == "large"]
    cand = os.path.join(BUILD_TREE, "git")
    if not os.path.isfile(cand):
        print("error: %s does not exist; run /app/ci/build.sh first" % cand)
        return 2
    fp_src = fingerprint(SUBMISSION)
    try:
        fp_built = open(os.path.join(BUILD_TREE, ".df-fingerprint")).read().strip()
    except OSError:
        fp_built = None
    if fp_built != fp_src:
        print("error: /app/build/git was not built from the current /app/git allowlisted sources; "
              "run /app/ci/build.sh first")
        return 2
    root = os.geteuid() == 0
    tools = oracle.Tools(ORACLE_GIT, *(UIDS["ora"] if root else (None, None)))
    ident = runner.Identity(*(UIDS["enc"] if root else (None, None)), *(UIDS["ora"] if root else (None, None)))
    work = "/tmp/delta-foundry-check/work"
    os.makedirs(work, exist_ok=True)
    os.chmod(os.path.dirname(work), 0o711)
    os.chmod(work, 0o711)
    stray_roots = [r for r in ("/tmp/delta-foundry-check", "/var/tmp", "/dev/shm") if os.path.isdir(r)]
    t0 = time.monotonic()
    print("Delta Foundry self-check (%s): %d small + %d compression cases, %d pairs, source fingerprint %s"
          % (mode, len(small), len(large), args.pairs, fp_src[:12]), flush=True)
    small_ok, recs = [], []
    for spec in small:
        cdir = os.path.join(CASES, spec["id"])
        case = runner.CaseData(cdir, os.path.join(cdir, "repo.git"), oracle.Tools(ORACLE_GIT), work)
        rec = runner.run_small(case, spec.get("profile") or wl["profile"], cand, tools, ident, work, stray_roots)
        recs.append(rec)
        small_ok.append(rec["ok"])
        if not rec["ok"]:
            print("  FAIL small %s: %s" % (spec["id"], rec["error"]), flush=True)
    print("small cases: %d/%d passed (%.1f s)" % (sum(small_ok), len(small), time.monotonic() - t0), flush=True)
    large_recs = []
    for spec in large:
        cdir = os.path.join(CASES, spec["id"])
        case = runner.CaseData(cdir, os.path.join(cdir, "repo.git"), oracle.Tools(ORACLE_GIT), work)
        rec = runner.run_large(case, spec.get("profile") or wl["profile"], BASELINE_GIT, cand, tools, ident, work,
                               stray_roots, pairs=args.pairs)
        case.drop()
        large_recs.append(rec)
        r = rec["ratios"]
        if r:
            print("  %-14s %-19s B/C=%.4f  C/B=%.4f  pack=%.3f  index=%.3f  read=%s  %s" % (
                spec["id"], spec["family"], r["size_gain"], r["size_ratio"], r["pack_time_ratio"],
                r["index_time_ratio"], "/".join("%.3f" % r["read_time_ratio"][k] for k in runner.READ_SCHEDULES),
                "ok" if scoring.within_limits(r) else "LIMIT EXCEEDED"), flush=True)
        else:
            print("  %-14s FAIL: %s" % (spec["id"], rec["error"]), flush=True)
    rw = scoring.aggregate(small_ok, len(small), [{"ok": r["ok"], "ratios": r["ratios"]} for r in large_recs],
                           len(large))
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w") as f:
        json.dump({"mode": mode, "rewards": rw, "source_fingerprint": fp_src, "small_failures":
                   [r for r in recs if not r["ok"]], "large": large_recs,
                   "seconds": time.monotonic() - t0}, f, indent=1, sort_keys=True, default=str)
    print("")
    print("correctness_passed=%d  geo_mean_size_ratio=%.6f  (public %s subset: %d cases)" % (
        rw["correctness_passed"], rw["geo_mean_size_ratio"], mode, len(small) + len(large)))
    print("max ratios: pack=%.3f index=%.3f read=%.3f size=%.4f  (limits 1.25/1.25/1.25/1.02)" % (
        rw["max_pack_time_ratio"], rw["max_index_time_ratio"], rw["max_read_time_ratio"], rw["max_pack_size_ratio"]))
    print("report: %s  elapsed %.1f s" % (args.report, time.monotonic() - t0))
    return 0 if rw["correctness_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
