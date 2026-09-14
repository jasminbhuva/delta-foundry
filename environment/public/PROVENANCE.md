# Public corpus provenance

The public self-check cases are built from these upstream repositories, pinned by commit. Only files covered by the listed licenses are included. Histories are linearised (first-parent) with synthetic committer identities; upstream file contents, commit dates and subject lines are preserved. Each excerpt's license text is shipped beside its bundle in `excerpts/<name>.LICENSE`.

| excerpt | repository | commit | license | included paths |
|---|---|---|---|---|
| lua | https://github.com/lua/lua | 7579fc9d7ed90240487251dfb69168f8e64e9294 | MIT (lua.h header) | *.c, *.h, *.lua, makefile, manual/, testes/ |
| cjson | https://github.com/DaveGamble/cJSON | fb16e5cf358798aabb049655975cde8427101056 | MIT | all |
| jq | https://github.com/jqlang/jq | 9d241e277204b83c4a7ddc7d733e5c72f99ef500 | BSD-2 (COPYING) | src/, tests/ (excl. src/decNumber) |
| linenoise | https://github.com/antirez/linenoise | a473823d74b93eab2ba83480df16ed37617493f2 | BSD-2 | all |

The pinned Git v2.55.0 source tarball is at `git-2.55.0.tar.xz` (SHA-256 `457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357`).
