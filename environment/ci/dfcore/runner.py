"""Case execution shared by the sealed verifier and the public self-check.

Small cases: one candidate pack per case, fully verified.
Large cases: six alternating baseline/candidate pairs; every output (both
engines) is fully verified, indexed with stock Git (timed) and read with
three stock read schedules (timed). Nothing reported by the encoder is used.
"""

import json
import os
import shutil
import stat
import time

from . import oracle, sandbox, scoring

PAIRS = 6
PROCESS_WALL = 120.0
ENCODER_LIMITS = dict(wall=PROCESS_WALL, address_space=2 << 30, file_size=512 << 20, nofile=256, nproc=64)
ORACLE_LIMITS = dict(wall=PROCESS_WALL, address_space=4 << 30, file_size=512 << 20, nofile=256, nproc=64)
SCRATCH_LIMIT = 4 << 30
SCRATCH_FILES = 100000
STDOUT_CAP = 512 << 20
READ_SCHEDULES = ("uniform", "tip", "ancestor")


class Identity:
    """UIDs for the encoder and the oracle; None when not running as root."""

    def __init__(self, enc_uid=None, enc_gid=None, ora_uid=None, ora_gid=None):
        self.enc = (enc_uid, enc_gid)
        self.ora = (ora_uid, ora_gid)


def profile_argv(profile):
    return ["pack-objects", "--stdout", "-q"] + list(profile)


def allows_ofs(profile):
    return "--delta-base-offset" in profile


def depth_of(profile):
    for a in profile:
        if a.startswith("--depth="):
            return int(a.split("=", 1)[1])
    return 50


def _fresh_dir(path, owner):
    if os.path.lexists(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    if owner[0] is not None and os.geteuid() == 0:
        os.chown(path, owner[0], owner[1])
    os.chmod(path, 0o700)


def _scratch_usage(path):
    total = 0
    files = 0
    for root, dirs, fs in os.walk(path):
        for f in fs + dirs:
            files += 1
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total, files


def run_encoder(git, case_dir, profile, job_dir, ident, out_path):
    """Run one engine on one case. Returns (result dict, error or None)."""
    repo = os.path.join(case_dir, "repo.git")
    _fresh_dir(job_dir, ident.enc)
    home = os.path.join(job_dir, "home")
    tmp = os.path.join(job_dir, "tmp")
    for d in (home, tmp):
        os.makedirs(d)
        if ident.enc[0] is not None and os.geteuid() == 0:
            os.chown(d, ident.enc[0], ident.enc[1])
    env = sandbox.base_env(home, tmp)
    argv = [git, "--git-dir=" + repo, "-c", "safe.directory=*", "-c", "core.fsync=none"] + profile_argv(profile)
    lim = sandbox.Limits(**ENCODER_LIMITS)
    res = sandbox.run(argv, cwd=tmp, env=env, limits=lim, stdin_path=os.path.join(case_dir, "stdin.txt"),
                      stdout_path=out_path, uid=ident.enc[0], gid=ident.enc[1])
    res["limit"] = PROCESS_WALL
    err = sandbox.describe_failure(res, "pack-objects")
    if res.get("survivors"):
        err = err or "pack-objects left %d background process(es) running" % res["survivors"]
    if res.get("ipc"):
        err = err or "pack-objects left %d SysV IPC object(s)" % res["ipc"]
    used, nfiles = _scratch_usage(job_dir)
    if used > SCRATCH_LIMIT or nfiles > SCRATCH_FILES:
        err = err or "scratch usage %d bytes / %d files exceeds limits" % (used, nfiles)
    shutil.rmtree(job_dir, ignore_errors=True)
    return res, err


def check_stray_files(uid, roots):
    """Return paths owned by the encoder UID outside its (removed) job directory."""
    if uid is None:
        return []
    bad = []
    for r in roots:
        for root, dirs, files in os.walk(r):
            for n in dirs + files:
                p = os.path.join(root, n)
                try:
                    if os.lstat(p).st_uid == uid:
                        bad.append(p)
                except OSError:
                    pass
            if len(bad) > 20:
                return bad
    return bad


class CaseData:
    def __init__(self, case_dir, expected_repo, tools, workdir):
        self.dir = case_dir
        self.info = json.load(open(os.path.join(case_dir, "case.json")))
        self.spec = self.info["spec"]
        self.fmt = self.spec["object_format"]
        self.manifest = oracle.load_manifest(case_dir)
        self.sizes = {o: s for o, _, s in self.manifest}
        self.expected = oracle.load_expected(tools, expected_repo, self.manifest, workdir)

    def drop(self):
        self.expected = None


def run_small(case, profile, cand_git, tools, ident, work, stray_roots):
    out = os.path.join(work, "out.pack")
    res, err = run_encoder(cand_git, case.dir, profile, os.path.join(work, "job"), ident, out)
    rec = {"id": case.spec["id"], "ok": False, "t_pack": res.get("wall")}
    if err is None:
        stray = check_stray_files(ident.enc[0], stray_roots)
        if stray:
            err = "encoder wrote files outside its scratch directory: %s" % stray[:3]
    if err is None:
        v = oracle.verify_output(out, fmt=case.fmt, manifest=case.manifest, expected=case.expected,
                                 depth_limit=depth_of(profile), tools=tools, workdir=os.path.join(work, "ora"),
                                 limits=sandbox.Limits(**ORACLE_LIMITS), allow_ofs=allows_ofs(profile))
        rec.update({"bytes": (v["pack_bytes"] or 0) + (v["idx_bytes"] or 0), "max_depth": v["max_depth"]})
        err = v["error"]
    rec["ok"] = err is None
    rec["error"] = err
    for p in (out, os.path.join(work, "ora")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.unlink(p)
    return rec


def measure_output(case, engine_git, profile, tools, ident, work, stray_roots, tag):
    """Timed pack + verification + timed index + timed reads for one output."""
    out = os.path.join(work, "out-%s.pack" % tag)
    oracle.warm_files([os.path.join(case.dir, "repo.git"), os.path.join(case.dir, "stdin.txt"), engine_git])
    res, err = run_encoder(engine_git, case.dir, profile, os.path.join(work, "job"), ident, out)
    rec = {"engine": tag, "t_pack": res.get("wall"), "error": None}
    if err is None:
        stray = check_stray_files(ident.enc[0], stray_roots)
        if stray:
            err = "encoder wrote files outside its scratch directory: %s" % stray[:3]
    if err is None:
        ora = os.path.join(work, "ora-" + tag)
        lim = sandbox.Limits(**ORACLE_LIMITS)
        oracle.warm_files([out])
        v = oracle.verify_output(out, fmt=case.fmt, manifest=case.manifest, expected=case.expected,
                                 depth_limit=depth_of(profile), tools=tools, workdir=ora, limits=lim,
                                 index_reps=int(case.spec.get("index_reps", 1)), allow_ofs=allows_ofs(profile))
        err = v["error"]
        rec.update({"pack_bytes": v["pack_bytes"], "idx_bytes": v["idx_bytes"], "t_index": v["t_index"],
                    "t_index_runs": v.get("t_index_runs"), "max_depth": v["max_depth"], "stats": v["stats"]})
        if err is None:
            rec["bytes"] = v["pack_bytes"] + v["idx_bytes"]
            rec["t_read"] = {}
            rec["t_read_runs"] = {}
            reps = case.spec.get("read_reps", {})
            for s in READ_SCHEDULES:
                sched = os.path.join(case.dir, "schedules", s + ".txt")
                wall, walls, rerr = oracle.timed_schedule(tools, v["fresh"], sched, case.sizes, ora, lim,
                                                          reps=int(reps.get(s, 1)), warm=[v["fresh"]])
                if rerr:
                    err = rerr
                    break
                rec["t_read"][s] = wall
                rec["t_read_runs"][s] = walls
        shutil.rmtree(ora, ignore_errors=True)
    if os.path.exists(out):
        os.unlink(out)
    rec["error"] = err
    return rec


def run_large(case, profile, base_git, cand_git, tools, ident, work, stray_roots, pairs=PAIRS, deadline=None):
    """Six alternating pairs. Returns dict with ok, ratios and every raw run."""
    runs = {"baseline": [], "candidate": []}
    err = None
    for k in range(pairs):
        order = ("baseline", "candidate") if k % 2 == 0 else ("candidate", "baseline")
        for eng in order:
            if deadline is not None and time.monotonic() > deadline:
                err = "verifier deadline reached during %s" % case.spec["id"]
                break
            g = base_git if eng == "baseline" else cand_git
            rec = measure_output(case, g, profile, tools, ident, work, stray_roots, eng)
            rec["pair"] = k
            runs[eng].append(rec)
            if rec["error"]:
                err = "%s output (pair %d): %s" % (eng, k, rec["error"])
                break
        if err:
            break
    out = {"id": case.spec["id"], "family": case.spec["family"], "runs": runs, "ok": err is None, "error": err,
           "ratios": None}
    if err is None:
        out["ratios"] = scoring.case_ratios(runs["baseline"], runs["candidate"])
        out["within_limits"] = scoring.within_limits(out["ratios"])
    return out


def lock_down_world_writable(roots=("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue")):
    """Remove world-write from shared temp dirs (root only)."""
    changed = []
    if os.geteuid() != 0:
        return changed
    for r in roots:
        try:
            st = os.lstat(r)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o002:
            os.chmod(r, 0o700)
            changed.append(r)
    return changed
