"""Deterministic Git history construction through `git fast-import`.

Callers describe commits as full snapshots ({path: bytes}); the writer emits
only the per-commit changes, deduplicates blob payloads and assigns marks.
Everything is byte-for-byte deterministic for a given sequence of calls.
"""

import hashlib
import os
import subprocess

EPOCH = 1700000000


def _check_path(path):
    if not path or "\n" in path or path.startswith('"') or path.startswith("/"):
        raise ValueError("unsupported path %r" % path)
    parts = path.split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        raise ValueError("unsupported path %r" % path)


class FastImportWriter:
    def __init__(self, out):
        self.out = out
        self.mark = 0
        self._blob_marks = {}
        self.ref_tips = {}      # ref -> mark of last commit
        self.ref_snap = {}      # ref -> {path: (mode, key)}
        self.commit_snap = {}   # commit mark -> {path: (mode, key)}

    def _next(self):
        self.mark += 1
        return self.mark

    def blob(self, data):
        key = hashlib.sha256(data).digest()
        m = self._blob_marks.get(key)
        if m is None:
            m = self._next()
            self.out.write(b"blob\nmark :%d\ndata %d\n" % (m, len(data)))
            self.out.write(data)
            self.out.write(b"\n")
            self._blob_marks[key] = m
        return key, m

    def commit(self, ref, snapshot, when, message, parents=None,
               author=b"Delta Foundry Generator <generator@example.invalid>",
               modes=None):
        """Write a commit whose tree is exactly `snapshot`.

        parents: list of commit marks; None means "continue `ref`".
        Returns the new commit mark.
        """
        if parents is None:
            parents = [self.ref_tips[ref]] if ref in self.ref_tips else []
        base = self.commit_snap[parents[0]] if parents else {}
        new = {}
        for path in sorted(snapshot):
            _check_path(path)
            mode = (modes or {}).get(path, b"100644")
            key, m = self.blob(snapshot[path])
            new[path] = (mode, key, m)
        m = self._next()
        msg = message if isinstance(message, bytes) else message.encode()
        if not msg.endswith(b"\n"):
            msg += b"\n"
        ts = b"%d +0000" % (EPOCH + int(when))
        self.out.write(b"commit %s\nmark :%d\n" % (ref.encode(), m))
        self.out.write(b"author %s %s\ncommitter %s %s\n" % (author, ts, author, ts))
        self.out.write(b"data %d\n%s" % (len(msg), msg))
        if parents:
            self.out.write(b"from :%d\n" % parents[0])
            for p in parents[1:]:
                self.out.write(b"merge :%d\n" % p)
        else:
            self.out.write(b"deleteall\n")
        for path in sorted(base):
            if path not in new:
                self.out.write(b"D %s\n" % path.encode())
        for path, (mode, key, bm) in new.items():
            old = base.get(path)
            if old is None or old[0] != mode or old[1] != key:
                self.out.write(b"M %s :%d %s\n" % (mode, bm, path.encode()))
        self.out.write(b"\n")
        snap = {p: (v[0], v[1]) for p, v in new.items()}
        self.commit_snap[m] = snap
        self.ref_tips[ref] = m
        self.ref_snap[ref] = snap
        return m

    def tag(self, name, target_mark, when, message,
            tagger=b"Delta Foundry Generator <generator@example.invalid>"):
        msg = message if isinstance(message, bytes) else message.encode()
        if not msg.endswith(b"\n"):
            msg += b"\n"
        ts = b"%d +0000" % (EPOCH + int(when))
        m = self._next()
        self.out.write(b"tag %s\nmark :%d\nfrom :%d\ntagger %s %s\ndata %d\n%s\n"
                       % (name.encode(), m, target_mark, tagger, ts, len(msg), msg))
        return m

    def reset(self, ref, target_mark):
        self.out.write(b"reset %s\nfrom :%d\n\n" % (ref.encode(), target_mark))
        self.ref_tips[ref] = target_mark

    def done(self):
        self.out.write(b"done\n")


def git_env():
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Delta Foundry Generator",
        "GIT_AUTHOR_EMAIL": "generator@example.invalid",
        "GIT_COMMITTER_NAME": "Delta Foundry Generator",
        "GIT_COMMITTER_EMAIL": "generator@example.invalid",
        "TZ": "UTC",
    }
    return env


def run_git(git, args, cwd=None, stdin=None, check=True):
    p = subprocess.run([git] + args, cwd=cwd, input=stdin, env=git_env(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s"
                           % (" ".join(args[:4]), p.returncode,
                              p.stderr.decode(errors="replace")[-2000:]))
    return p.stdout


def init_bare(git, path, object_format):
    os.makedirs(path, exist_ok=False)
    run_git(git, ["init", "-q", "--bare", "--template=",
                  "--object-format=" + object_format, "--ref-format=files", path])
    for d in ("objects/info", "objects/pack", "refs/heads", "refs/tags", "info"):
        os.makedirs(os.path.join(path, d), exist_ok=True)


def fast_import(git, repo, stream_path):
    with open(stream_path, "rb") as f:
        p = subprocess.run([git, "--git-dir=" + repo, "-c", "fastimport.unpackLimit=0",
                            "fast-import", "--quiet",
                            "--done", "--depth=10", "--active-branches=64"],
                           stdin=f, env=git_env(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("fast-import failed: " + p.stderr.decode(errors="replace")[-2000:])
