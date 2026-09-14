#!/bin/sh
# Focused upstream regression tests for the pack encoder, built from the
# allowlisted sources in /app/git over the pristine tree (same rules as
# /app/ci/build.sh), in a separate tree so /app/build stays untouched.
#
#   /app/ci/upstream-tests.sh              optimized build, focused pack tests
#   /app/ci/upstream-tests.sh --sanitize   same tests with SANITIZE=address,undefined
#   /app/ci/upstream-tests.sh t5316-pack-delta-depth.sh ...   choose tests
#
# These tests are not scored by the verifier; they are for your own checking.
set -eu
MODE=opt
if [ "${1:-}" = "--sanitize" ]; then MODE=asan; shift; fi
TREE=/app/build-tests-$MODE
TESTS="$*"
[ -n "$TESTS" ] || TESTS="t5300-pack-object.sh t5301-sliding-window.sh t5302-pack-index.sh
t5303-pack-corruption-resilience.sh t5306-pack-nobase.sh t5308-pack-detect-duplicates.sh
t5309-pack-delta-cycles.sh t5313-pack-bounds-checks.sh t5314-pack-cycle-detection.sh
t5315-pack-objects-compression.sh t5316-pack-delta-depth.sh t5321-pack-large-objects.sh
t5331-pack-objects-stdin.sh t1050-large.sh"
if [ ! -d "$TREE" ]; then
  cp -a /opt/delta-foundry/pristine-src "$TREE"
  chmod -R u+w "$TREE"
fi
python3 - "$TREE" <<'EOF'
import sys
sys.path.insert(0, "/app/ci")
from dfcore import build
overlay, report = build.collect("/app/git", "/opt/delta-foundry/pristine-src")
print("allowlisted changes: %s" % (", ".join(report["changed_allowlisted"]) or "none"))
build.apply_overlay(sys.argv[1], "/opt/delta-foundry/pristine-src", overlay)
EOF
ARGS="prefix=/usr/local NO_RUST=1 NO_OPENSSL=1 NO_CURL=1 NO_EXPAT=1 NO_TCLTK=1 NO_GETTEXT=1 NO_PYTHON=1"
if [ "$MODE" = asan ]; then ARGS="$ARGS SANITIZE=address,undefined"; fi
cd "$TREE"
make -j2 $ARGS CFLAGS="-g -O2 -Wall" all >"$TREE/.build.log" 2>&1 || { tail -40 "$TREE/.build.log"; exit 1; }
cd t
status=0
for t in $TESTS; do
  if GIT_TEST_OPTS="--verbose-log -x" make -s $ARGS "$t" >/dev/null 2>&1; then
    echo "PASS $t"
  else
    echo "FAIL $t (see $TREE/t/test-results/${t%.sh}.out)"
    status=1
  fi
done
exit $status
