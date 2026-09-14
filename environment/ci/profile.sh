#!/bin/sh
# Per-phase timings, pack structure diagnostics and optional CPU profiles on a public case.
#   /app/ci/profile.sh [--case ID] [--engine candidate|baseline] [--perf] [--callgrind]
exec python3 /app/ci/profile.py "$@"
