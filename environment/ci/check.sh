#!/bin/sh
# Offline self-check: same oracle, gate, limits and profile as the verifier on public cases.
#   /app/ci/check.sh --fast   48 small + 2 compression cases
#   /app/ci/check.sh --full   192 small + 6 compression cases
exec python3 /app/ci/check.py "$@"
