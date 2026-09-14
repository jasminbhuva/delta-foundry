# Delta Foundry source and build rules

## What you may change

Only these files under `/app/git` are collected (paths relative to `/app/git`):

```
builtin/pack-objects.c   pack-objects.c   pack-objects.h
diff-delta.c             delta.h
pack-write.c             pack.h
csum-file.c              csum-file.h
path-walk.c              path-walk.h
git-zlib.c               git-zlib.h
```

This covers object enumeration and ordering, delta candidate search, the delta encoder, delta-chain layout, reuse of existing deltas, pack writing and the zlib wrappers. Put helper code (similarity indexes, cost models, parsers, a different deflate implementation, threading) inside these files. New translation units are not supported: the Makefile is fixed, so new `.c` files are never compiled. No new external libraries.

Collection rules, applied identically by `/app/ci/build.sh` and the verifier:

- Each allowlisted path must be a regular file, read without following symlinks (no symlinked file or directory component, no device/FIFO/socket).
- Per-file limit 4 MiB, total limit 16 MiB across the 13 files.
- Deleting an allowlisted file is not supported: a missing file is a collection error.
- Any collection error fails the correctness gate (error code 2).
- Files outside the allowlist are ignored: the pristine Git v2.55.0 version is built instead. `build.sh` lists such changes as warnings. Makefiles, scripts, hooks, config, object files and binaries from `/app/git` or `/app/build` are never used by the verifier.

## Build recipe

The verifier copies your allowlisted files over a pristine, already-built Git v2.55.0 tree (commit `e9019fcafe0040228b8631c30f97ae1adb61bcdc`) and runs, as an unprivileged user with no network:

```
make -j2 prefix=/usr/local NO_RUST=1 NO_OPENSSL=1 NO_CURL=1 NO_EXPAT=1 NO_TCLTK=1 \
     NO_GETTEXT=1 NO_PYTHON=1 NO_INSTALL_HARDLINKS=1 'CFLAGS=-g -O2 -Wall' git
```

Toolchain: Debian 13 (trixie) gcc 14.2.0, GNU make, system zlib 1.3.1. No `-march` flags; the same recipe builds the baseline. Build limits: 900 s wall, 4 GiB address space. A failed build fails the gate (error code 3). `/app/ci/build.sh` runs this exact recipe incrementally in `/app/build`.

## Interface the verifier calls

For every case the verifier runs your binary as

```
git --git-dir=<read-only input repository> -c safe.directory=* -c core.fsync=none \
    pack-objects --stdout -q <profile flags>  < <object list>  > <pack file>
```

The object list is exactly what `git rev-list --objects --all` prints for the input repository: one object name per line, optionally followed by a space and a path (the name-hash hint). The output must be one complete, self-contained pack on standard output containing exactly the listed objects, each once.

Profile flags: compression cases use `--window=50 --depth=50 --threads=1 --compression=9 --delta-base-offset`. Small cases vary `--window` (1-50), `--depth` (0-50), `--threads` (1-2), `--compression` (1-9), and sometimes omit `--delta-base-offset` or add `--no-reuse-delta`, `--no-reuse-object` or `--name-hash-version=2`.

- Enforced: every delta chain is at most `--depth` deltas long (`--depth=0` means no deltas), and without `--delta-base-offset` the pack must not contain ofs-delta entries.
- Not enforced: `--window`, `--threads`, `--compression`, reuse flags and the name-hash version are search and effort parameters. How you interpret them is up to you, but the time limits in SCORING.md still apply.

## Runtime sandbox

Your binary runs as a dedicated unprivileged user with an empty `HOME` and `TMPDIR` (its only writable place), no network, only standard input/output/error open, and a scrubbed environment (no global or system Git config). Limits per process: 120 s wall, 2 GiB address space, 512 MiB per written file (including the pack on stdout), 256 open files, 64 processes/threads for the user. Scratch usage must stay under 4 GiB and 100,000 files. The process must not leave running processes, SysV IPC objects or files outside its scratch directory. Scratch is deleted after every run, so nothing carries over between runs or cases. Any violation fails the gate (error code 5 or 4).
