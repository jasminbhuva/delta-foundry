#!/bin/bash
# Delta Foundry verifier entry point. Runs as root in the separate verifier image.
# Complete failure rewards are written before anything else, so every exit path
# (including SIGKILL of the supervisor) leaves both reward files with all keys.
set -u
OUT=/logs/verifier
mkdir -p "$OUT"
chmod 0755 "$OUT" 2>/dev/null
for f in reward.json reward.txt error.txt details.json; do
  rm -rf "$OUT/$f"
done
FAIL='{"compatibility_passed": 0, "correctness_passed": 0, "error": 1, "geo_mean_size_ratio": 0.0, "large_cases_passed": 0, "max_index_time_ratio": 0.0, "max_pack_size_ratio": 0.0, "max_pack_time_ratio": 0.0, "max_read_time_ratio": 0.0, "num_passed_tests": 0, "small_cases_passed": 0, "total_tests": 792}'
printf '%s\n' "$FAIL" > "$OUT/reward.json.init" && mv -f "$OUT/reward.json.init" "$OUT/reward.json"
printf '0\n' > "$OUT/reward.txt.init" && mv -f "$OUT/reward.txt.init" "$OUT/reward.txt"
printf 'verifier did not finish\n' > "$OUT/error.txt"

# Internal deadline (supervisor stops starting work at DF_BUDGET_SEC) and a hard
# kill below the 3600 s platform verifier timeout, leaving time to finalize.
export DF_BUDGET_SEC="${DF_BUDGET_SEC:-3300}"
timeout -s KILL 3450 python3 -I /opt/df/verifier/supervisor.py > "$OUT/supervisor.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "supervisor exited with status $rc" >> "$OUT/supervisor.log"
fi
# Consistency guard: reward.txt must be the gate of reward.json.
python3 -I /opt/df/verifier/reward_guard.py "$OUT" >> "$OUT/supervisor.log" 2>&1 || {
  printf '%s\n' "$FAIL" > "$OUT/reward.json"
  printf '0\n' > "$OUT/reward.txt"
}
exit 0
