"""Source overlay collection and the fixed Git build recipe.

Only regular files on the published allowlist are taken from the submitted
tree. They are read without following symlinks, size-limited, and written
into a pristine build tree; nothing else from the submission (Makefiles,
scripts, objects, config) is ever used or executed.
"""

import errno
import os
import stat
import subprocess

from . import sandbox

ALLOWLIST = [
    "builtin/pack-objects.c",
    "pack-objects.c",
    "pack-objects.h",
    "diff-delta.c",
    "delta.h",
    "pack-write.c",
    "pack.h",
    "csum-file.c",
    "csum-file.h",
    "path-walk.c",
    "path-walk.h",
    "git-zlib.c",
    "git-zlib.h",
]
PER_FILE_LIMIT = 4 << 20
AGGREGATE_LIMIT = 16 << 20
SCAN_FILE_LIMIT = 200000

MAKE_ARGS = [
    "prefix=/usr/local",
    "NO_RUST=1", "NO_OPENSSL=1", "NO_CURL=1", "NO_EXPAT=1", "NO_TCLTK=1",
    "NO_GETTEXT=1", "NO_PYTHON=1", "NO_INSTALL_HARDLINKS=1",
    "CFLAGS=-g -O2 -Wall",
]


def fingerprint(root):
    """SHA-256 over the allowlisted files under root (missing files marked)."""
    import hashlib
    h = hashlib.sha256()
    for rel in ALLOWLIST:
        h.update(rel.encode() + b"\0")
        try:
            with open(os.path.join(root, rel), "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()
MAKE_TARGET = "git"
BUILD_JOBS = 2
BUILD_TIMEOUT = 900


class CollectError(Exception):
    pass


def _read_nofollow(root, rel, limit):
    """Open root/rel without following any symlink; return bytes of a regular file."""
    parts = rel.split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise CollectError("%s: directory component %r is a symlink or not a directory" % (rel, comp))
                if e.errno == errno.ENOENT:
                    raise CollectError("%s: missing (deleting allowlisted files is not supported)" % rel)
                raise
            os.close(fd)
            fd = nfd
        try:
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise CollectError("%s: is a symlink" % rel)
            if e.errno == errno.ENOENT:
                raise CollectError("%s: missing (deleting allowlisted files is not supported)" % rel)
            raise CollectError("%s: cannot open: %s" % (rel, e))
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode):
                raise CollectError("%s: not a regular file" % rel)
            if st.st_size > limit:
                raise CollectError("%s: %d bytes exceeds the %d byte per-file limit" % (rel, st.st_size, limit))
            chunks = []
            total = 0
            while True:
                b = os.read(ffd, 1 << 20)
                if not b:
                    break
                total += len(b)
                if total > limit:
                    raise CollectError("%s: grew beyond the per-file limit while reading" % rel)
                chunks.append(b)
            return b"".join(chunks)
        finally:
            os.close(ffd)
    finally:
        os.close(fd)


def collect(submitted_root, pristine_root):
    """Return (overlay {rel: bytes}, report dict). Raises CollectError."""
    if os.path.islink(submitted_root) or not os.path.isdir(submitted_root):
        raise CollectError("%s is not a directory" % submitted_root)
    overlay = {}
    total = 0
    for rel in ALLOWLIST:
        data = _read_nofollow(submitted_root, rel, PER_FILE_LIMIT)
        total += len(data)
        if total > AGGREGATE_LIMIT:
            raise CollectError("allowlisted sources exceed the %d byte aggregate limit" % AGGREGATE_LIMIT)
        with open(os.path.join(pristine_root, rel), "rb") as f:
            if f.read() != data:
                overlay[rel] = data
    report = {"changed_allowlisted": sorted(overlay), "allowlisted_bytes": total,
              "ignored_changes": ignored_changes(submitted_root, pristine_root)}
    return overlay, report


def ignored_changes(submitted_root, pristine_root, limit=50):
    """List (bounded) files outside the allowlist that differ from pristine."""
    allowed = set(ALLOWLIST)
    out = []
    seen = 0
    for root, dirs, files in os.walk(submitted_root, followlinks=False):
        rel_root = os.path.relpath(root, submitted_root)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            seen += 1
            if seen > SCAN_FILE_LIMIT or len(out) >= limit:
                return out + ["(scan truncated)"]
            rel = os.path.join(rel_root, name) if rel_root else name
            if rel in allowed:
                continue
            p = os.path.join(root, name)
            q = os.path.join(pristine_root, rel)
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    out.append(rel + " (not a regular file)")
                    continue
                if not os.path.isfile(q):
                    out.append(rel + " (new file, not built)")
                    continue
                if st.st_size != os.path.getsize(q):
                    out.append(rel)
                    continue
                with open(p, "rb") as a, open(q, "rb") as b:
                    if a.read(1 << 22) != b.read(1 << 22):
                        out.append(rel)
            except OSError:
                out.append(rel + " (unreadable)")
    return out


def apply_overlay(build_root, pristine_root, overlay, owner=None):
    """Make every allowlisted file in build_root equal pristine+overlay.

    Unchanged files are left untouched so make's timestamps stay valid.
    """
    written = []
    for rel in ALLOWLIST:
        want = overlay.get(rel)
        if want is None:
            with open(os.path.join(pristine_root, rel), "rb") as f:
                want = f.read()
        dst = os.path.join(build_root, rel)
        cur = None
        if os.path.isfile(dst) and not os.path.islink(dst):
            with open(dst, "rb") as f:
                cur = f.read()
        if cur == want:
            continue
        if os.path.lexists(dst):
            os.unlink(dst)
        with open(dst, "wb") as f:
            f.write(want)
        if owner is not None:
            os.chown(dst, owner[0], owner[1])
        written.append(rel)
    return written


def make(build_root, *, uid=None, gid=None, home=None, log_path=None, timeout=BUILD_TIMEOUT):
    """Run the fixed recipe. Returns (ok, wall_seconds, log_tail)."""
    env = sandbox.base_env(home or build_root, home or build_root)
    lim = sandbox.Limits(wall=timeout, address_space=4 << 30, file_size=1 << 30,
                         nofile=1024, nproc=256, stderr_cap=1 << 20)
    argv = ["make", "-j%d" % BUILD_JOBS] + MAKE_ARGS + [MAKE_TARGET]
    res = sandbox.run(argv, cwd=build_root, env=env, limits=lim, uid=uid, gid=gid,
                      stdout_path=log_path or os.devnull)
    tail = res.get("stderr", b"")[-4000:].decode("utf-8", "replace")
    ok = res["returncode"] == 0 and not res["timed_out"] and os.path.isfile(os.path.join(build_root, "git"))
    if res["timed_out"]:
        tail = "build exceeded %d s\n" % timeout + tail
    return ok, res["wall"], tail


def install(tree, *, timeout=BUILD_TIMEOUT):
    """`make install` the built tree to its compiled prefix (/usr/local) so the
    dashed helper programs and templates resolve for commands that spawn them
    (e.g. `git fetch <bundle>` during case materialization). Runtime encoder and
    oracle use builtins and do not depend on this."""
    env = sandbox.base_env(tree, tree)
    lim = sandbox.Limits(wall=timeout, address_space=4 << 30, file_size=1 << 30, nofile=1024, nproc=256,
                         stderr_cap=1 << 20)
    res = sandbox.run(["make"] + MAKE_ARGS + ["install"], cwd=tree, env=env, limits=lim,
                      stdout_path=os.path.join(tree, ".install.log"))
    if res["returncode"] != 0 or res["timed_out"]:
        raise RuntimeError("make install failed: " + res.get("stderr", b"")[-2000:].decode("utf-8", "replace"))


def recipe_text():
    return "make -j%d %s %s" % (BUILD_JOBS, " ".join("'%s'" % a if " " in a else a for a in MAKE_ARGS), MAKE_TARGET)
