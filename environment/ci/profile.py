#!/usr/bin/env python3
"""Profile one public compression case and compare pack structure with the baseline.

usage: /app/ci/profile.sh [--case ID] [--engine candidate|baseline] [--perf] [--callgrind]

Prints (1) Git's trace2 phase timings for pack-objects, (2) a per-type
breakdown of stored bytes, whole-object vs delta entries and the delta-depth
histogram for both engines (trusted parser), and (3) optionally a sampled CPU
profile with `perf` (when the container allows perf_event_open) or a
callgrind profile. Uses the exact verifier profile and case inputs.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dfcore import packparse, sandbox  # noqa: E402

CASES = "/opt/delta-foundry/public-cases"
BASE = "/usr/local/bin/git"
CAND = "/app/build/git"


def pack(git, case, profile, out, extra_env=None):
    env = sandbox.base_env("/tmp", "/tmp")
    env.update(extra_env or {})
    t0 = time.monotonic()
    with open(os.path.join(case, "stdin.txt"), "rb") as i, open(out, "wb") as o:
        p = subprocess.run([git, "--git-dir=" + os.path.join(case, "repo.git"), "-c", "safe.directory=*",
                            "pack-objects", "--stdout", "-q"] + profile, stdin=i, stdout=o,
                           stderr=subprocess.PIPE, env=env)
    return p.returncode, time.monotonic() - t0, p.stderr.decode(errors="replace")


def structure(path, fmt):
    data = open(path, "rb").read()
    parsed = packparse.parse_pack(data, fmt)
    offs = {e[0] for e in parsed["entries"]}
    # ref-delta bases need the index; profiles use --delta-base-offset so ofs covers nearly all
    depths = packparse.chain_depths([e for e in parsed["entries"] if e[4] is None or e[4][0] == "ofs"], {})
    st = packparse.pack_stats([e for e in parsed["entries"] if e[0] in depths], depths)
    st["pack_bytes"] = len(data)
    st["entries_total"] = len(offs)
    return st


def trace2_regions(path):
    rows = []
    for line in open(path, errors="replace"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 9 and parts[3] == "region_leave":
            rows.append((parts[6], parts[7], parts[8]))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    wl = json.load(open(os.path.join(HERE, "public-workloads.json")))
    large = [c for c in wl["cases"] if c["kind"] == "large"]
    ap.add_argument("--case", default=large[0]["id"], choices=[c["id"] for c in large])
    ap.add_argument("--engine", default="candidate", choices=["candidate", "baseline"])
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--callgrind", action="store_true")
    a = ap.parse_args()
    spec = next(c for c in large if c["id"] == a.case)
    case = os.path.join(CASES, a.case)
    fmt = spec["object_format"]
    prof = spec.get("profile") or wl["profile"]
    tmp = tempfile.mkdtemp(prefix="df-profile-")
    print("case %s (%s, %s), profile: %s" % (a.case, spec["family"], fmt, " ".join(prof)))
    res = {}
    for eng, git in (("baseline", BASE), ("candidate", CAND)):
        if not os.path.exists(git):
            print("missing %s; run /app/ci/build.sh" % git)
            return 2
        tr = os.path.join(tmp, eng + ".trace2")
        out = os.path.join(tmp, eng + ".pack")
        rc, wall, err = pack(git, case, prof, out, {"GIT_TRACE2_PERF": tr})
        if rc:
            print("%s pack-objects failed (%d): %s" % (eng, rc, err[-500:]))
            return 1
        res[eng] = {"wall": wall, "structure": structure(out, fmt)}
        print("\n== %s: %.3f s wall, %d bytes" % (eng, wall, res[eng]["structure"]["pack_bytes"]))
        for cat, label, dur in trace2_regions(tr):
            if cat in ("pack-objects", "progress") or "pack" in label:
                print("   %-40s %s" % (cat + "/" + label, dur))
        s = res[eng]["structure"]
        print("   entries by stored type: " + ", ".join("%s=%d (%d bytes)" % (k, v["count"], v["stored_bytes"])
                                                   for k, v in sorted(s["by_type"].items())))
        hist = sorted((int(k), v) for k, v in s["depth_hist"].items())
        print("   max delta depth %d; depth histogram (depth:count) %s" % (
            s["max_depth"], " ".join("%d:%d" % kv for kv in hist[:12]) + (" ..." if len(hist) > 12 else "")))
    b, c = res["baseline"]["structure"]["pack_bytes"], res["candidate"]["structure"]["pack_bytes"]
    print("\npack bytes baseline/candidate = %.4f (the verifier adds the trusted .idx to both); "
          "pack time ratio %.3f" % (b / c, res["candidate"]["wall"] / res["baseline"]["wall"]))
    git = CAND if a.engine == "candidate" else BASE
    if a.perf:
        data = os.path.join(tmp, "perf.data")
        with open(os.path.join(case, "stdin.txt"), "rb") as i:
            p = subprocess.run(["perf", "record", "-g", "-o", data, "--", git,
                                "--git-dir=" + os.path.join(case, "repo.git"), "-c", "safe.directory=*",
                                "pack-objects", "--stdout", "-q"] + prof, stdin=i, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
        if p.returncode:
            print("perf unavailable here (%s); try --callgrind" % p.stderr.decode(errors="replace").strip()[-200:])
        else:
            subprocess.run(["perf", "report", "-i", data, "--stdio", "--no-children", "--percent-limit", "1.5"])
    if a.callgrind:
        out = os.path.join(tmp, "callgrind.out")
        with open(os.path.join(case, "stdin.txt"), "rb") as i:
            subprocess.run(["valgrind", "--tool=callgrind", "--callgrind-out-file=" + out, git,
                            "--git-dir=" + os.path.join(case, "repo.git"), "-c", "safe.directory=*",
                            "pack-objects", "--stdout", "-q"] + prof, stdin=i, stdout=subprocess.DEVNULL)
        subprocess.run(["callgrind_annotate", out], stdout=sys.stdout)
    print("\nartifacts in %s" % tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
