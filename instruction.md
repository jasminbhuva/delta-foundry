# Smaller Git packs with bounded read cost

Git v2.55.0 (commit `e9019fcafe0040228b8631c30f97ae1adb61bcdc`) is at `/app/git`. Improve its pack encoder so `git pack-objects` writes smaller packs for evolving source code, diverging branches, generated text, binary assets and repacks, while every object still reconstructs exactly with stock Git and packing, indexing and reading stay within 1.40x of stock Git. Hosting and backup operators would use this to cut storage and transfer costs without changing any Git client.

There is no target score: smaller verified output is better. Log hypotheses/results, re-profile after gains and investigate remaining costs. Use focused tests; broaden for an unresolved risk.

## What you change

Edit only these files under `/app/git`: `builtin/pack-objects.c`, `pack-objects.c`, `pack-objects.h`, `diff-delta.c`, `delta.h`, `pack-write.c`, `pack.h`, `csum-file.c`, `csum-file.h`, `path-walk.c`, `path-walk.h`, `git-zlib.c`, `git-zlib.h`. Other files, new files, Makefiles and build outputs are ignored, and no new libraries are allowed. The verifier copies these files over a pristine Git tree and builds `git` with a fixed recipe (gcc 14, zlib 1.3.1). `/app/ci/build.sh` runs the same recipe incrementally and writes `/app/build/git`. Full rules are in `/app/ci/SOURCE_RULES.md`.

## What the verifier runs

In a separate offline container, for each case it runs

`git --git-dir=<read-only repo> pack-objects --stdout -q <profile> < <object list>`

as an unprivileged user with an empty scratch directory, 120 s and 2 GiB address space per process, and at most 512 MiB of output. The list is the output of `git rev-list --objects --all`. The result must be one self-contained pack that holds exactly those objects, once each: no thin packs or external bases, no delta chain deeper than `--depth`, and no ofs-deltas unless `--delta-base-offset` is given. Object order, bases, delta instructions and compressed bytes may all change. The 24 compression cases use `--window=50 --depth=50 --threads=1 --compression=9 --delta-base-offset`. Window, threads, compression and reuse flags are effort hints you may reinterpret; the container has 2 CPUs.

Stock `git index-pack --strict` indexes each output in a fresh repository, and stock `git cat-file` reconstructs every object for byte comparison with the expected data.

## Scoring

Gate (`correctness_passed`): all 792 cases must pass. That is 768 small correctness cases plus 24 compression cases, four each of source edits, diverging branches, generated text, binary evolution, mixed loose/packed repacking and poorly compressible controls. For each compression case the verifier alternates six baseline/candidate runs, taking median bytes and minimum times. Each compression case must also satisfy all of these limits:

- candidate packing time at most 1.40x the baseline's;
- stock indexing time at most 1.40x;
- each of three stock read-schedule times at most 1.40x;
- `.pack` plus `.idx` bytes at most 1.02x.

Any failure makes the reward 0.

Objective: `geo_mean_size_ratio = exp(mean over the 24 cases of log(B_i / C_i))`, where `B_i` and `C_i` are the median baseline and candidate `.pack` plus `.idx` bytes. The untouched tree scores 1.0, and higher is better. Measurement details, every limit and the reward fields are in `/app/ci/SCORING.md`.

## Self-check

Run `/app/ci/build.sh && /app/ci/check.sh --fast` (48 small + 2 compression cases) or `--full` (192 small + 6 compression cases, one per family). Both use the verifier's oracle, gate, limits, profile and pairing on public cases built from different repositories and seeds, so they track the real score but do not equal it. `/app/ci/profile.sh` shows phase times and pack structure, and `/app/ci/upstream-tests.sh` runs Git's focused pack tests, optionally with sanitizers.

Only the final `/app/git` is scored. Leave it building and passing the self-check.
