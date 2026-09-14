#!/bin/sh
# Incremental build of /app/build/git with the verifier's exact recipe.
exec python3 /app/ci/build.py "$@"
