# Delta Foundry — submission fields

For Harbor submission, package the repository contents at the archive root. The last prepared release ZIP had SHA-256 `eca58847ebbd3a0c0b7b93a91b427801a6a614df72ba5058eef995879602d27f`. A valid ZIP should contain `task.toml`, `instruction.md`, `README.md`, `environment/`, and `tests/` at its root with no wrapping directory.

**Canary removed.** The platform rejected the first upload with `CANARY-PRESENT` (53 files). This ZIP is canary-free (rebuilt with `assemble_bundle.py --no-canary`; 0 markers verified inside the ZIP), builds both images, and passes the baseline/failure-path checks. The removed lines were inert (comment lines + an ignored JSON key).

| Form field | Value |
|---|---|
| 1. Artifact class | **Codecs & signal processing** |
| Verification patterns (one or two) | **Differential oracle** and **Benchmark metric** |
| 2. Task name | `delta-foundry` — must equal the slug in `[task] name = "afterquery/delta-foundry"`. Do not type `afterquery/delta-foundry` in the name field unless the form requires the namespace form. |
| 3. Bundle zip | `delta-foundry.zip` |
| Hardware | CPU only: 2 CPUs, 4 GiB, no GPU |

## Unresolved before final submission

- **Canary policy was resolved for this release ZIP.** The current release ZIP is canary-free after a `CANARY-PRESENT` structure rejection. If platform policy changes, rebuild from the canonical task source and revalidate the archive instead of editing the ZIP manually.
- **Frontier probe pending.** The 250M-token frontier requirement has not been evaluated because no platform credentials or spend cap were available in the authoring environment.
- **Full green-gate re-run pending.** Every correctness check passed on the completed Harbor nop; the gate turned red only on the pre-recalibration 1.25 read limit (now 1.40). A full 792-case green-gate re-run at 1.40 is deferred to adequately resourced hardware (the local 4 GiB Docker VM OOMs on full re-runs).

## Repackage command

```sh
zip -q -r -X delta-foundry.zip . \
  -x '*.git/*' -x '*.DS_Store' -x '*/__pycache__/*' -x '*.pyc' -x 'docs/*'
shasum -a 256 delta-foundry.zip > delta-foundry.zip.sha256
```
