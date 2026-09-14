# Delta Foundry scoring

This file is the complete, agent-readable scoring contract. The sealed verifier (`tests/test.sh` in a separate, offline container) and the public self-check (`/app/ci/check.sh`) implement it with the same code.

## Cases

The sealed set has 792 cases, a fixed denominator:

- **768 small correctness cases.** These are small repositories (a few to a few hundred objects) built from licensed upstream excerpts and seeded generators. They cover empty and tiny objects, identical content under many paths, deep single-file histories with shallow `--depth` bounds (0-8), objects of 200 KB-2 MB, copy regions around 64 KiB boundaries, binary data with NUL bytes, tags of commits/tags, executable and symlink entries, octopus and ordinary merges, renames, incompressible data, and inputs whose older history is already packed (delta reuse). Each case has its own profile: `--window` 1-50, `--depth` 0-50, `--threads` 1-2, `--compression` 1-9, with or without `--delta-base-offset`, sometimes `--no-reuse-delta`, `--no-reuse-object` or `--name-hash-version=2`. About half use SHA-256 repositories.
- **24 compression cases**, four per family, each 900-5,000 objects and 8-160 MB of object data:
  1. `source-edits`: real C project histories, including a directory rename partway through.
  2. `diverging-branches`: a real mainline plus 8-12 topic branches with seeded edits (inserted, deleted and moved blocks, identifier renames, file moves and copies), merges and tags.
  3. `generated-text`: JSON catalogs, lockfiles, generated C tables, growing logs, Markdown, minified JavaScript and CSV, with changing repeated regions.
  4. `binary-evolution`: raw images, fixed-size database pages with inserts, pseudo-executables whose edits shift code and absolute pointer tables, float arrays and a container of deflated members.
  5. `mixed-repack`: real histories where older history is already in one or two packs written by stock Git (`--window=10`, depth 10-50), and newer objects are loose. Delta reuse is allowed.
  6. `controls`: random data, tiny files, pre-compressed data and duplicates.

  All use `--window=50 --depth=50 --threads=1 --compression=9 --delta-base-offset`. Half of the compression cases are SHA-256 repositories.

Public cases use the same generators and semantics with different repositories (lua, cJSON, jq, linenoise) and seeds. The sealed cases use other repositories (zlib, lz4, xxHash, stb, miniz, libyaml, tmux, libuv) that are not in the image. Case definitions for the public set are in `/app/ci/public-workloads.json`, and the inputs are under `/opt/delta-foundry/public-cases/<id>/`.

## Correctness of one output

For every pack your build produces, in every small case and in every one of the six timed runs of every compression case:

1. The encoder must exit 0 within its limits and leave no processes, SysV IPC objects or files outside its scratch directory.
2. The output is parsed by a bounded trusted parser. Checks: `PACK` signature, version 2/3, object count equal to the case's object count, every entry header, each zlib stream inflating to exactly its declared size, no bytes between the last entry and the trailer, and a trailer checksum matching the file. Truncated, corrupt or trailing-garbage output fails.
3. Pristine `git index-pack --strict --no-rev-index --threads=2` runs in a fresh repository with no access to the input objects. There is no `--fix-thin`, so thin packs and external bases fail, and so do duplicates, cycles and bad checksums.
4. The trusted index must list exactly the expected object IDs, once each. Delta depth is computed from the pack structure and must be at most `--depth`. Without `--delta-base-offset`, ofs-delta entries are rejected.
5. Pristine `git cat-file --batch` reconstructs every object. Type, size and all bytes must equal the expected objects, which were read from the trusted input before your encoder ran.

Any failure fails the case, and any failed case fails the gate.

## Timing and size measurement (compression cases)

- **Engines.** The baseline engine is pristine Git v2.55.0 built with the same recipe. Both engines run in the same sandbox with the same profile, the same input and fresh scratch directories.
- **Order.** Six pairs alternate: baseline then candidate, then candidate then baseline, and so on. All runs are kept; bytes are aggregated by median and times by minimum (see below). There is no reseeding.
- **Packing time.** Wall time on the verifier's monotonic clock, from just before your process is spawned until it has exited. This includes object reading, all search, delta creation, compression, writing and any threads. The input repository files are read once beforehand (page-cache warm-up) outside the timer, identically for both engines.
- **Indexing time.** Each output is indexed by pristine `git index-pack --strict` R times in fresh processes. The summed wall time is that output's indexing time. R is fixed per case in the workload file (`index_reps`), identical for both engines, and chosen so the baseline total is long enough to dominate timer noise.
- **Read time.** There are three disclosed schedules per case: `uniform` (random blobs, with replacement), `tip` (every tree and blob of each branch tip) and `ancestor` (blobs of the oldest quarter of history that are not in any tip). Each schedule runs through pristine `git cat-file --batch` with `core.deltaBaseCacheLimit=96m`, R fresh reader processes in a row (`read_reps`, fixed per case), with the pack and index read into the page cache before each process. Responses are drained and their framing and sizes checked. These are whole-schedule latencies, not per-object percentiles.
- **Bytes.** Logical length of the `.pack` plus the trusted `.idx` (v2) written by stock `index-pack`. Reverse indexes are disabled for both engines, and no sidecar files are accepted or counted.

For case i, over the six runs of each engine:

- `B_i`, `C_i`: **median** baseline and candidate bytes;
- `pack_time_ratio = min(candidate pack time) / min(baseline pack time)`, and likewise `index_time_ratio` and one `read_time_ratio` per schedule. Time ratios use the **minimum** of the six runs, not the median: wall time under host contention can only rise, so the minimum is the least-contended and most reproducible measurement, it is symmetric between the two engines, and a candidate that is genuinely slower still shows it in the minimum. All six runs are recorded either way;
- `size_ratio = C_i / B_i`.

A compression case passes its limits when:

- `pack_time_ratio <= 1.40`;
- `index_time_ratio <= 1.40`;
- every `read_time_ratio <= 1.40`;
- `size_ratio <= 1.02`.

The three timing limits carry a deliberate noise margin (1.40, not 1.25): sub-second stock-tool wall times do not reproduce to 1.25 on shared virtualized hardware, where a baseline-vs-baseline run reaches about 1.34 on the smallest read schedule under contention. The deterministic guarantees against pathological output are the exact delta-depth bound and the 1.02 size limit; the timing limits are a secondary guard, and the untouched baseline reproduces within them. On dedicated target hardware they may be tightened.

A fast but oversized result fails, and so does a small result with slow reads.

## Gate and objective

`correctness_passed = 1` if and only if all of these hold:

- the build succeeded;
- all 768 small cases passed;
- all 24 compression cases passed every correctness check in all 12 runs;
- all 24 cases are within their limits.

The objective counts only when the gate passes:

```
geo_mean_size_ratio = exp( (1/24) * sum_i log(B_i / C_i) )
```

Direction: higher is better. Untouched baseline: 1.0, with 2% relative tolerance. For example, 1.10 means packs about 9.1% smaller in geometric mean. With a failed gate the objective is 0.

## Reward files

`/logs/verifier/reward.json` always contains every key below. Complete failure values are written before anything else runs, and the final values are written atomically from one predicate. `reward.txt` is `1` exactly when `correctness_passed` is 1, otherwise `0`.

| key | meaning |
|---|---|
| `correctness_passed` | gate, 0/1 |
| `compatibility_passed` | 1 when every correctness comparison (build, 768 small, 24 x 12 outputs) passed, before the limits are applied |
| `geo_mean_size_ratio` | objective; 0 when the gate fails |
| `num_passed_tests` / `total_tests` | completed passing cases / 792 (fixed) |
| `small_cases_passed`, `large_cases_passed` | the two parts of the pass count |
| `max_pack_time_ratio`, `max_index_time_ratio`, `max_read_time_ratio`, `max_pack_size_ratio` | worst case of each limit, 0 when not measured |
| `error` | numeric code: 0 none, 1 verifier internal, 2 source collection rejected, 3 build failed, 4 small case failed, 5 compression case failed, 6 limit exceeded, 7 verifier deadline, 8 sandbox violation, 9 setup |

`error.txt` and `details.json` give the message and all raw per-run measurements. If small cases fail, the compression cases are still run once each, untimed, for the pass count, and the limit fields stay 0. After 8 small failures the remaining small cases are skipped and count as not passed.

## Other enforced limits

Build: 900 s, 4 GiB address space. Per encoder, index or read process: 120 s wall, 2 GiB (encoder) or 4 GiB (stock tools) address space, 512 MiB per written file, 256 open files, 64 processes/threads per user. Scratch: 4 GiB and 100,000 files. The whole verifier stops starting new work after 3,300 s and then reports error 7.

## Public self-check

`/app/ci/check.sh --fast` runs 48 small and 2 compression cases (source edits, binary evolution). `--full` runs 192 small and 6 compression cases, one per family. The objective is averaged over the compression cases actually run. The oracle, limits, pairing, profile and sandbox are the verifier's. It refuses to run when `/app/build/git` was not built from the current `/app/git` sources. Public results are a sample: they share the families and generators but not the repositories, seeds or case count, so the sealed score can differ in either direction.
