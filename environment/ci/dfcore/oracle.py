"""Exact, independent verification of a pack produced by an untrusted encoder.

All Git processes here are the pinned, unmodified build. They run in a fresh
repository that has no access to the input object database, under resource
limits and (when the supervisor is root) as a dedicated oracle UID.
"""

import mmap
import os
import shutil

from . import packparse, sandbox

HEADER_MAX = 256


class Tools:
    def __init__(self, git, uid=None, gid=None):
        self.git = git
        self.uid = uid
        self.gid = gid


def load_manifest(case_dir):
    rows = []
    with open(os.path.join(case_dir, "manifest.tsv"), "rb") as f:
        for line in f:
            oid, typ, size = line.split()
            rows.append((oid.decode(), typ.decode(), int(size)))
    return rows


def load_expected(tools, repo, manifest, workdir):
    """Read every expected object from the trusted repository `repo`."""
    listing = os.path.join(workdir, "expected-list.txt")
    with open(listing, "wb") as f:
        f.write(b"".join(o.encode() + b"\n" for o, _, _ in manifest))
    box = {}

    def reader(pipe):
        out = {}
        for oid, typ, size in manifest:
            hdr = pipe.readline(HEADER_MAX).split()
            if len(hdr) != 3 or hdr[0].decode() != oid or hdr[1].decode() != typ or int(hdr[2]) != size:
                raise RuntimeError("trusted repository disagrees with manifest at %s" % oid)
            data = pipe.read(size)
            if len(data) != size or pipe.read(1) != b"\n":
                raise RuntimeError("short read from trusted repository at %s" % oid)
            out[oid] = data
        box["v"] = out
        return len(out)

    env = sandbox.base_env(workdir, workdir)
    res = sandbox.run([tools.git, "--git-dir=" + repo, "cat-file", "--batch"], cwd=workdir, env=env,
                      limits=sandbox.Limits(wall=600, address_space=8 << 30, file_size=1 << 20, nproc=0),
                      stdin_path=listing, stdout_pipe_reader=reader)
    if res.get("reader_error") or res["returncode"] != 0 or "v" not in box:
        raise RuntimeError("cannot load expected objects: %s %s" % (res.get("reader_error"),
                                                                    sandbox.describe_failure(res, "cat-file")))
    return box["v"]


def _prep_dir(path, tools):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    if tools.uid is not None and os.geteuid() == 0:
        os.chown(path, tools.uid, tools.gid)
    os.chmod(path, 0o700)


def verify_output(pack_path, *, fmt, manifest, expected, depth_limit, tools, workdir,
                  limits, index_reps=1, allow_ofs=True):
    """Check one candidate output. Returns a dict with ok/error and measurements.

    On success result['fresh'] is a fresh repository holding exactly the
    candidate pack and its trusted index (used for timed reads).
    """
    r = {"ok": False, "error": None, "pack_bytes": None, "idx_bytes": None,
         "t_index": None, "max_depth": None, "stats": None}
    try:
        st = os.lstat(pack_path)
    except OSError:
        r["error"] = "no pack output"
        return r
    if not os.path.isfile(pack_path) or os.path.islink(pack_path):
        r["error"] = "pack output is not a regular file"
        return r
    r["pack_bytes"] = st.st_size
    if st.st_size == 0:
        r["error"] = "empty pack output"
        return r
    with open(pack_path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            try:
                parsed = packparse.parse_pack(mm, fmt, max_entries=len(manifest) + 1)
            except packparse.PackError as e:
                r["error"] = "malformed pack: %s" % e
                return r
            trailer = bytes(mm[-packparse.HASH_LEN[fmt]:])
        finally:
            mm.close()
    if parsed["count"] != len(manifest):
        r["error"] = "pack holds %d entries, manifest requires %d" % (parsed["count"], len(manifest))
        return r
    if not allow_ofs and any(e[2] == packparse.OBJ_OFS_DELTA for e in parsed["entries"]):
        r["error"] = "ofs-delta entries present but --delta-base-offset was not given"
        return r
    _prep_dir(workdir, tools)
    fresh = os.path.join(workdir, "fresh.git")
    env = sandbox.base_env(workdir, workdir)
    init = sandbox.run([tools.git, "init", "-q", "--bare", "--template=", "--object-format=" + fmt, fresh],
                       cwd=workdir, env=env, limits=limits, uid=tools.uid, gid=tools.gid)
    if init["returncode"] != 0:
        r["error"] = sandbox.describe_failure(init, "oracle init")
        return r
    name = "pack-" + trailer.hex()
    dst = os.path.join(fresh, "objects", "pack", name + ".pack")
    shutil.copyfile(pack_path, dst)
    if tools.uid is not None and os.geteuid() == 0:
        os.chown(dst, tools.uid, tools.gid)
    os.chmod(dst, 0o444)
    idx = os.path.join(fresh, "objects", "pack", name + ".idx")
    ip = sandbox.run([tools.git, "--git-dir=" + fresh, "-c", "pack.writeReverseIndex=false",
                      "-c", "core.fsync=none", "index-pack", "--strict", "--threads=2", "--no-rev-index",
                      "-o", idx, dst], cwd=workdir, env=env, limits=limits, uid=tools.uid, gid=tools.gid)
    r["t_index"] = ip["wall"]
    if ip["returncode"] != 0 or ip["timed_out"]:
        r["error"] = sandbox.describe_failure(ip, "index-pack --strict") or "index-pack failed"
        return r
    # Timed indexing = sum of `index_reps` fresh strict index-pack processes of
    # the same pack (the first is the verification run above).
    walls = [ip["wall"]]
    for k in range(1, index_reps):
        extra_idx = os.path.join(workdir, "rep-%d.idx" % k)
        rp = sandbox.run([tools.git, "--git-dir=" + fresh, "-c", "pack.writeReverseIndex=false",
                          "-c", "core.fsync=none", "index-pack", "--strict", "--threads=2", "--no-rev-index",
                          "-o", extra_idx, dst], cwd=workdir, env=env, limits=limits, uid=tools.uid, gid=tools.gid)
        if rp["returncode"] != 0 or rp["timed_out"]:
            r["error"] = sandbox.describe_failure(rp, "index-pack --strict (repeat)") or "index-pack failed"
            return r
        walls.append(rp["wall"])
        os.unlink(extra_idx)
    r["t_index"] = sum(walls)
    r["t_index_runs"] = walls
    for extra in os.listdir(os.path.join(fresh, "objects", "pack")):
        if extra not in (name + ".pack", name + ".idx"):
            r["error"] = "unexpected file after indexing: %s" % extra
            return r
    with open(idx, "rb") as f:
        idx_data = f.read()
    r["idx_bytes"] = len(idx_data)
    try:
        entries, pack_sum = packparse.parse_idx(idx_data, fmt)
    except packparse.PackError as e:
        r["error"] = "trusted index unreadable: %s" % e
        return r
    if pack_sum != trailer:
        r["error"] = "index/pack checksum disagreement"
        return r
    want = {o for o, _, _ in manifest}
    got = [o for o, _ in entries]
    if len(got) != len(set(got)):
        r["error"] = "duplicate objects in pack"
        return r
    missing = want.difference(got)
    extra = set(got).difference(want)
    if missing or extra:
        r["error"] = "object set mismatch: %d missing, %d extra (e.g. %s)" % (
            len(missing), len(extra), sorted(missing or extra)[0])
        return r
    oid_to_off = dict(entries)
    try:
        depths = packparse.chain_depths(parsed["entries"], oid_to_off)
    except packparse.PackError as e:
        r["error"] = "delta structure: %s" % e
        return r
    r["max_depth"] = max(depths.values()) if depths else 0
    r["stats"] = packparse.pack_stats(parsed["entries"], depths)
    if r["max_depth"] > depth_limit:
        r["error"] = "delta chain depth %d exceeds --depth=%d" % (r["max_depth"], depth_limit)
        return r
    # Reconstruct every object with stock cat-file and compare with sealed bytes.
    listing = os.path.join(workdir, "all.txt")
    with open(listing, "wb") as f:
        f.write(b"".join(o.encode() + b"\n" for o, _, _ in manifest))
    mism = {}

    def reader(pipe):
        n = 0
        for oid, typ, size in manifest:
            hdr = pipe.readline(HEADER_MAX).split()
            if len(hdr) != 3 or hdr[0].decode(errors="replace") != oid:
                mism["e"] = "object %s: bad or missing response %r" % (oid, b" ".join(hdr)[:80])
                return n
            if hdr[1].decode() != typ or int(hdr[2]) != size:
                mism["e"] = "object %s: type/size %s/%s, expected %s/%d" % (oid, hdr[1].decode(), hdr[2].decode(), typ, size)
                return n
            data = pipe.read(size)
            if data != expected[oid]:
                mism["e"] = "object %s: reconstructed bytes differ" % oid
                return n
            if pipe.read(1) != b"\n":
                mism["e"] = "object %s: framing error" % oid
                return n
            n += 1
        if pipe.read(1):
            mism["e"] = "trailing data from cat-file"
        return n

    cr = sandbox.run([tools.git, "--git-dir=" + fresh, "cat-file", "--batch"], cwd=workdir, env=env,
                     limits=limits, uid=tools.uid, gid=tools.gid, stdin_path=listing, stdout_pipe_reader=reader)
    if cr.get("reader_error"):
        r["error"] = "reconstruction reader: " + cr["reader_error"]
        return r
    if mism:
        r["error"] = mism["e"]
        return r
    if cr["returncode"] != 0 or cr["timed_out"] or cr.get("reader") != len(manifest):
        r["error"] = sandbox.describe_failure(cr, "cat-file reconstruction") or "incomplete reconstruction"
        return r
    r["ok"] = True
    r["fresh"] = fresh
    return r


def make_batch_drain(expect_sizes):
    """Reader for timed schedules: validates framing/sizes without copying content."""
    def reader(pipe):
        buf = b""
        i = 0
        n = 0
        while i < len(expect_sizes):
            nl = buf.find(b"\n")
            while nl < 0:
                more = pipe.read(1 << 20)
                if not more:
                    raise RuntimeError("short schedule response after %d objects" % n)
                buf += more
                nl = buf.find(b"\n")
            hdr = buf[:nl].split()
            oid, size = expect_sizes[i]
            if len(hdr) != 3 or hdr[0].decode(errors="replace") != oid or int(hdr[2]) != size:
                raise RuntimeError("bad schedule response for %s" % oid)
            need = nl + 1 + size + 1
            while len(buf) < need:
                more = pipe.read(max(1 << 20, need - len(buf)))
                if not more:
                    raise RuntimeError("truncated schedule object %s" % oid)
                buf += more
            if buf[need - 1:need] != b"\n":
                raise RuntimeError("schedule framing error at %s" % oid)
            buf = buf[need:]
            i += 1
            n += 1
        if buf or pipe.read(1):
            raise RuntimeError("trailing schedule data")
        return n
    return reader


def timed_schedule(tools, fresh, schedule_path, sizes, workdir, limits, reps=1, warm=None):
    """Time one read schedule as a single fresh reader process.

    The schedule (its oid list) is fed `reps` times back-to-back to one
    `git cat-file --batch` process, so process startup is paid once and the
    measured wall time is a long, low-variance latency. Returns
    (wall seconds, [wall], error).
    """
    env = sandbox.base_env(workdir, workdir)
    with open(schedule_path, "rb") as f:
        oids = [l.strip() for l in f if l.strip()]
    expect = [(o.decode(), sizes[o.decode()]) for o in oids] * reps
    stream = os.path.join(workdir, "sched-stream.txt")
    with open(stream, "wb") as f:
        for _ in range(reps):
            f.write(b"\n".join(oids) + b"\n")
    if warm:
        warm_files(warm)
    res = sandbox.run([tools.git, "--git-dir=" + fresh, "-c", "core.deltaBaseCacheLimit=96m",
                       "-c", "core.packedGitWindowSize=1g", "-c", "core.packedGitLimit=8g",
                       "cat-file", "--batch"], cwd=workdir, env=env, limits=limits,
                      uid=tools.uid, gid=tools.gid, stdin_path=stream,
                      stdout_pipe_reader=make_batch_drain(expect))
    os.unlink(stream)
    if res.get("reader_error"):
        return res["wall"], [res["wall"]], "read schedule: " + res["reader_error"]
    if res["returncode"] != 0 or res["timed_out"] or res.get("reader") != len(expect):
        return res["wall"], [res["wall"]], sandbox.describe_failure(res, "read schedule") or "incomplete read schedule"
    return res["wall"], [res["wall"]], None


def warm_files(paths):
    """Trusted page-cache warm-up: read files fully (outside any timer)."""
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                warm_files([os.path.join(root, f) for f in files])
            continue
        try:
            with open(p, "rb") as f:
                while f.read(1 << 20):
                    pass
        except OSError:
            pass
