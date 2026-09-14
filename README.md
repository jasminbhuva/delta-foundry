# Delta Foundry

Delta Foundry is a Harbor benchmark task for improving Git's packfile encoder. The task asks an agent to modify a pinned Git v2.55.0 source tree so `git pack-objects` writes smaller self-contained packs while every object still reconstructs exactly with stock Git and packing, indexing, and read costs remain within documented limits.

This repository contains the task bundle only: the agent environment, task instruction, public self-check assets, and sealed verifier. Large authoring experiments, trial logs, and scratch artifacts are intentionally excluded.

## Project Summary

| Field | Value |
|---|---|
| Task name | `afterquery/delta-foundry` |
| Artifact class | Codecs and signal processing |
| Paradigm | Performance |
| Verification | Differential oracle and benchmark metric |
| Baseline | Git v2.55.0, commit `e9019fcafe0040228b8631c30f97ae1adb61bcdc` |
| Main objective | Increase `geo_mean_size_ratio` above the untouched baseline of `1.0` |
| Correctness gate | All 792 verifier cases must pass |
| Hardware profile | CPU only, 2 CPUs, 4 GiB memory |

## Why It Matters

Git hosting, archival, and backup systems store many related versions of source code and binary assets. Smaller packs reduce storage and transfer costs, but the output must remain readable by unmodified Git clients. A useful solution must improve compression without breaking object reconstruction, delta depth constraints, indexing time, or historical read performance.

The interesting work lives in the interactions between similarity search, base selection, delta-chain layout, delta reuse, copy/insert encoding, and zlib behavior. A locally smaller delta can still make the global pack worse or slow down stock Git reads, so progress has to be measured across real workload families.

## Repository Layout

```text
.
├── instruction.md
├── task.toml
├── environment/
│   ├── Dockerfile
│   ├── ci/
│   └── public/
├── tests/
│   ├── Dockerfile
│   ├── fixtures/
│   ├── dfcore/
│   └── test.sh
└── docs/
    ├── SUBMISSION.md
    └── VALIDATION.md
```

`environment/` is visible to the evaluated agent and includes the pinned Git source archive, public excerpts, build scripts, and fast self-check tooling. `tests/` contains the offline verifier, sealed fixtures, and scoring code. The verifier runs in a separate no-network container.

## Scoring

The verifier rebuilds the candidate from an allowlist of Git pack-related source files, then compares candidate output against stock Git. The gate passes only when all correctness cases pass and every compression case stays within the size and timing limits.

The main metric is:

```text
geo_mean_size_ratio = exp(mean(log(baseline_bytes / candidate_bytes)))
```

The untouched baseline scores `1.0`; higher is better. Full scoring details are in `environment/ci/SCORING.md`.

## Local Self-Check

Inside the agent container:

```sh
/app/ci/build.sh
/app/ci/check.sh --fast
```

The fast check runs a public subset with the same oracle, limits, and measurement logic as the sealed verifier. The full public check is:

```sh
/app/ci/check.sh --full
```

`/app/ci/profile.sh` reports pack structure and phase timing, and `/app/ci/upstream-tests.sh` runs focused Git pack tests.

## Validation Status

Local validation confirmed that the bundle builds, the public self-check passes, and the full Harbor nop run completed all 792 correctness cases. Timing limits were calibrated to `1.40x` after a baseline-vs-baseline read-time false positive under local host contention. See `docs/VALIDATION.md` for details.

Frontier/platform validation is still pending. This repository should be treated as a prepared benchmark bundle, not as an accepted platform result.

## Provenance

Git v2.55.0 is pinned to commit `e9019fcafe0040228b8631c30f97ae1adb61bcdc`. The release tarball SHA-256 is:

```text
457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357
```

The public and sealed corpora are built from licensed upstream excerpts plus deterministic generated histories. License files are included beside the corresponding bundles.
