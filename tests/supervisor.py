#!/usr/bin/env python3
"""Delta Foundry sealed verifier supervisor (runs as root in the verifier image).

Order: lock down shared writable paths, collect the allowlisted sources from
/app/git, rebuild them over the pristine tree with the fixed recipe as an
unprivileged build user, then run all 768 small correctness cases and the 24
compression cases (six alternating baseline/candidate pairs each) with the
encoder and the oracle under separate unprivileged UIDs. Rewards are computed
by one predicate and written atomically; test.sh has already written complete
failure rewards before this program starts.
"""

import hashlib
import json
import os
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dfcore import build, oracle, runner, scoring  # noqa: E402

ROOT = "/opt/df"
SUBMISSION = "/app/git"
PRISTINE_SRC = ROOT + "/pristine-src"
BUILD_TREE = "/work/cand"
BASELINE_GIT = "/usr/local/bin/git"
ORACLE_GIT = "/usr/local/bin/git"
RUN_DIR = ROOT + "/run"
CASES = ROOT + "/cases"
# DF_WORKLOADS lets the authoring security tests point the supervisor at a small
# subset of the already-materialized cases for a fast run. It is never set by
# Harbor or the shipped test.sh, so the platform always uses the full 792.
WORKLOADS = os.environ.get("DF_WORKLOADS", HERE + "/workloads.json")
INTEGRITY = ROOT + "/sealed/integrity.json"
REWARD_DIR = os.environ.get("DF_REWARD_DIR", "/logs/verifier")
BUDGET = float(os.environ.get("DF_BUDGET_SEC", "3300"))
UIDS = {"build": (61010, 61010), "enc": (61011, 61011), "ora": (61012, 61012)}
MAX_SMALL_FAILURES = 8


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def log(msg):
    print("[supervisor %7.1fs] %s" % (time.monotonic() - T0, msg), flush=True)


def grant(case_dir, on):
    os.chmod(case_dir, 0o755 if on else 0o700)


def finish(rw, message, details):
    clean = scoring.write_rewards(REWARD_DIR, rw, message, details)
    log("final rewards: %s" % json.dumps(clean, sort_keys=True))
    log(message)


def main():
    details = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "phases": {}}
    wl = json.load(open(WORKLOADS))
    small_specs = [c for c in wl["cases"] if c["kind"] == "small"]
    large_specs = [c for c in wl["cases"] if c["kind"] == "large"]
    n_small, n_large = len(small_specs), len(large_specs)
    total = n_small + n_large
    deadline = T0 + BUDGET

    def fail(code, message):
        rw = scoring.failure_rewards(total, code)
        finish(rw, message, details)
        return 0

    # --- setup and integrity -------------------------------------------------
    details["world_writable_locked"] = runner.lock_down_world_writable()
    os.chmod(REWARD_DIR, 0o755)
    integ = json.load(open(INTEGRITY))
    for name, path in (("baseline_git", BASELINE_GIT), ("oracle_git", ORACLE_GIT)):
        if sha256_file(path) != integ[name]:
            return fail(scoring.E_SETUP, "%s does not match its recorded checksum" % name)
    details["profile"] = wl["profile"]
    # --- collect and build ---------------------------------------------------
    t = time.monotonic()
    try:
        overlay, report = build.collect(SUBMISSION, PRISTINE_SRC)
    except build.CollectError as e:
        details["phases"]["collect"] = {"error": str(e)}
        return fail(scoring.E_COLLECT, "source collection rejected: %s" % e)
    except OSError as e:
        details["phases"]["collect"] = {"error": str(e)}
        return fail(scoring.E_COLLECT, "source collection failed: %s" % e)
    details["phases"]["collect"] = report
    details["source_fingerprint"] = hashlib.sha256(
        b"".join(r.encode() + b"\0" + hashlib.sha256(overlay[r]).digest() for r in sorted(overlay))).hexdigest()
    log("collected %d changed allowlisted file(s): %s" % (len(overlay), ", ".join(sorted(overlay)) or "none"))
    written = build.apply_overlay(BUILD_TREE, PRISTINE_SRC, overlay, owner=UIDS["build"])
    os.makedirs(RUN_DIR, exist_ok=True)
    home = "/work/build-home"
    os.makedirs(home, exist_ok=True)
    os.chown(home, *UIDS["build"])
    ok, wall, tail = build.make(BUILD_TREE, uid=UIDS["build"][0], gid=UIDS["build"][1], home=home,
                                log_path="/work/build.log")
    details["phases"]["build"] = {"ok": ok, "seconds": wall, "written": written, "recipe": build.recipe_text(),
                                  "log_tail": tail[-3000:]}
    log("build %s in %.1f s" % ("ok" if ok else "FAILED", wall))
    if not ok:
        return fail(scoring.E_BUILD, "candidate build failed: " + tail[-500:].replace("\n", " | "))
    cand_git = os.path.join(RUN_DIR, "candidate-git")
    shutil.copyfile(os.path.join(BUILD_TREE, "git"), cand_git)
    os.chmod(cand_git, 0o755)
    details["candidate_git_sha256"] = sha256_file(cand_git)
    details["phases"]["build"]["total_seconds"] = time.monotonic() - t
    # --- cases ---------------------------------------------------------------
    tools = oracle.Tools(ORACLE_GIT, *UIDS["ora"])
    ident = runner.Identity(*UIDS["enc"], *UIDS["ora"])
    work = "/work/run"
    os.makedirs(work, exist_ok=True)
    os.chmod(work, 0o711)
    stray_roots = ["/tmp", "/var/tmp", "/dev/shm", "/work", "/app", "/opt", "/root", "/home"]
    stray_roots = [r for r in stray_roots if os.path.isdir(r)]
    small_ok = []
    small_recs = []
    failures = 0
    t = time.monotonic()
    for spec in small_specs:
        if time.monotonic() > deadline or failures >= MAX_SMALL_FAILURES:
            break
        cdir = os.path.join(CASES, spec["id"])
        grant(cdir, True)
        try:
            case = runner.CaseData(cdir, os.path.join(cdir, "repo.git"), oracle.Tools(ORACLE_GIT), work)
            rec = runner.run_small(case, spec.get("profile") or wl["profile"], cand_git, tools, ident, work,
                                   stray_roots)
        finally:
            grant(cdir, False)
        small_recs.append(rec)
        small_ok.append(rec["ok"])
        if not rec["ok"]:
            failures += 1
            log("small %s FAILED: %s" % (spec["id"], rec["error"]))
    details["phases"]["small"] = {"seconds": time.monotonic() - t, "completed": len(small_recs),
                                  "passed": sum(small_ok),
                                  "failures": [r for r in small_recs if not r["ok"]][:20],
                                  "max_t_pack": max([r["t_pack"] or 0 for r in small_recs] or [0])}
    log("small cases: %d/%d passed (%d completed)" % (sum(small_ok), n_small, len(small_recs)))
    timed = len(small_recs) == n_small and all(small_ok)
    large_recs = []
    t = time.monotonic()
    for spec in large_specs:
        if time.monotonic() > deadline:
            break
        cdir = os.path.join(CASES, spec["id"])
        grant(cdir, True)
        try:
            case = runner.CaseData(cdir, os.path.join(cdir, "repo.git"), oracle.Tools(ORACLE_GIT), work)
            if timed:
                rec = runner.run_large(case, spec.get("profile") or wl["profile"], BASELINE_GIT, cand_git, tools,
                                       ident, work, stray_roots, deadline=deadline)
            else:
                # Gate already red: one untimed candidate output for the pass count.
                one = runner.run_small(case, spec.get("profile") or wl["profile"], cand_git, tools, ident, work,
                                       stray_roots)
                rec = {"id": spec["id"], "ok": one["ok"], "error": one["error"], "ratios": None,
                       "untimed": True}
            case.drop()
        finally:
            grant(cdir, False)
        large_recs.append(rec)
        r = rec.get("ratios")
        log("large %s: %s%s" % (spec["id"], "ok" if rec["ok"] else "FAILED " + str(rec["error"]),
                                 "" if not r else " gain=%.4f size=%.4f pack=%.3f index=%.3f read=%s" % (
                                     r["size_gain"], r["size_ratio"], r["pack_time_ratio"], r["index_time_ratio"],
                                     {k: round(v, 3) for k, v in r["read_time_ratio"].items()})))
    details["phases"]["large"] = {"seconds": time.monotonic() - t, "timed": timed}
    details["large_cases"] = large_recs
    fatal = scoring.E_NONE
    if time.monotonic() > deadline and (len(small_recs) < n_small or len(large_recs) < n_large):
        fatal = scoring.E_TIMEOUT
    if not timed:
        # Completed large comparisons still count; limits cannot be assessed.
        large_for_score = [{"ok": r["ok"], "ratios": None} for r in large_recs]
    else:
        large_for_score = [{"ok": r["ok"] and r.get("ratios") is not None, "ratios": r.get("ratios")}
                           for r in large_recs]
    rw = scoring.aggregate(small_ok, n_small, large_for_score, n_large, fatal)
    msgs = []
    if fatal == scoring.E_TIMEOUT:
        msgs.append("verifier deadline reached before all cases completed")
    bad_small = [r for r in small_recs if not r["ok"]]
    if bad_small:
        msgs.append("small case %s: %s" % (bad_small[0]["id"], bad_small[0]["error"]))
    bad_large = [r for r in large_recs if not r["ok"]]
    if bad_large:
        msgs.append("large case %s: %s" % (bad_large[0]["id"], bad_large[0]["error"]))
    over = [r["id"] for r in large_recs if r.get("ratios") and not scoring.within_limits(r["ratios"])]
    if over:
        msgs.append("performance/size limits exceeded in: %s" % ", ".join(over))
    if not msgs:
        msgs.append("all %d cases passed; geo_mean_size_ratio=%.6f" % (total, rw["geo_mean_size_ratio"]))
    details["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    details["elapsed_seconds"] = time.monotonic() - T0
    finish(rw, "; ".join(msgs), details)
    return 0


if __name__ == "__main__":
    T0 = time.monotonic()
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        try:
            wl = json.load(open(WORKLOADS))
            total = len(wl["cases"])
        except Exception:
            total = scoring.TOTAL_TESTS
        scoring.write_rewards(REWARD_DIR, scoring.failure_rewards(total, scoring.E_INTERNAL),
                              "verifier internal error: " + tb.strip().splitlines()[-1],
                              {"traceback": tb})
        sys.exit(0)
