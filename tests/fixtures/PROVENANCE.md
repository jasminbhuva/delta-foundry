# Sealed corpus provenance

Sealed compression and correctness cases are built from these upstream repositories, pinned by commit, disjoint from the public set. Only files covered by the listed licenses are included. Histories are linearised (first-parent) with synthetic committer identities; upstream contents, dates and subjects preserved. License texts ship beside each bundle.

| excerpt | repository | commit | license | included paths |
|---|---|---|---|---|
| zlib | https://github.com/madler/zlib | e3dc0a85b7032e98380dec011bc8f2c2ee0d8fca | zlib | *.c, *.h, docs, examples/, test/ (excl. contrib) |
| lz4 | https://github.com/lz4/lz4 | 0774d05537f9762f838f7ab541b7765f1a729cb5 | BSD-2 (lib/LICENSE) | lib/ |
| xxhash | https://github.com/Cyan4973/xxHash | c0b5ea995d66691734b1a79ad89e73a0d2fd5a53 | BSD-2 | xxhash.h, xxhash.c, xxh3.h, xxh_x86dispatch.* , doc/ |
| stb | https://github.com/nothings/stb | 2c980bb59875b0d32144a71867fbdebb2f77cd20 | MIT/public domain | stb_*.h |
| miniz | https://github.com/richgel999/miniz | 77d0dce8627735138c51770d1799a1ef48f2117d | MIT | all |
| libyaml | https://github.com/yaml/libyaml | 90a56d4500aa1a1798514c5cb55c3ad4cb095f94 | MIT | src/, include/, tests/ |
| tmux | https://github.com/tmux/tmux | e880cf63e0a9fe095d7c5d313761520fb1a8653c | ISC (COPYING) | *.c, *.h (excl. compat/) |
| libuv | https://github.com/libuv/libuv | 096a02d14cb9d3de6d55a29ec02c4dfcb7643c7f | MIT + extra | src/, include/ |
