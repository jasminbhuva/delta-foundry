#!/usr/bin/env python3
"""Final guard: both reward files exist, carry every key with finite numbers,
and reward.txt is exactly the gate of reward.json. Exit 1 otherwise."""

import json
import math
import sys

KEYS = ["compatibility_passed", "correctness_passed", "error", "geo_mean_size_ratio", "large_cases_passed",
        "max_index_time_ratio", "max_pack_size_ratio", "max_pack_time_ratio", "max_read_time_ratio",
        "num_passed_tests", "small_cases_passed", "total_tests"]


def main():
    d = sys.argv[1]
    try:
        rw = json.load(open(d + "/reward.json"))
        txt = open(d + "/reward.txt").read().strip()
    except (OSError, ValueError) as e:
        print("reward_guard: unreadable rewards: %s" % e)
        return 1
    for k in KEYS:
        v = rw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            print("reward_guard: bad or missing key %s=%r" % (k, v))
            return 1
    # The shipped/platform run always has the full 792 denominator. The authoring
    # security harness runs a small subset via DF_WORKLOADS; there the denominator
    # is that subset's size (still a fixed positive integer, checked below).
    import os
    if not os.environ.get("DF_WORKLOADS"):
        if rw["total_tests"] != 792:
            print("reward_guard: denominator changed")
            return 1
    elif not (isinstance(rw["total_tests"], (int, float)) and rw["total_tests"] > 0):
        print("reward_guard: bad denominator")
        return 1
    gate = rw["correctness_passed"] == 1
    if txt != ("1" if gate else "0"):
        print("reward_guard: reward.txt disagrees with gate")
        return 1
    if not gate and rw["geo_mean_size_ratio"] != 0.0:
        print("reward_guard: objective present with failed gate")
        return 1
    print("reward_guard: ok (gate=%d)" % gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
