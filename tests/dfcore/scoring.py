"""Aggregation, the single gate predicate and atomic reward emission."""

import json
import math
import os

TOTAL_TESTS = 792

# Numeric error codes (Harbor 0.22.0 only accepts numeric reward values).
E_NONE = 0
E_INTERNAL = 1
E_COLLECT = 2
E_BUILD = 3
E_SMALL_CASE = 4
E_LARGE_CASE = 5
E_RESOURCE = 6
E_TIMEOUT = 7
E_SANDBOX = 8
E_SETUP = 9
ERROR_NAMES = {
    E_NONE: "none", E_INTERNAL: "verifier internal error", E_COLLECT: "source collection rejected",
    E_BUILD: "candidate build failed", E_SMALL_CASE: "small correctness case failed",
    E_LARGE_CASE: "compression case failed correctness", E_RESOURCE: "performance or size limit exceeded",
    E_TIMEOUT: "verifier deadline reached", E_SANDBOX: "sandbox violation", E_SETUP: "verifier setup failed",
}

# Timing-ratio limits carry a noise margin: sub-second stock-tool wall times are
# not reproducible to 1.25 on shared, virtualized hardware (a baseline-vs-baseline
# nop reaches ~1.34 on the smallest read schedule under host contention). The
# deterministic guarantees against pathological output are the exact delta-depth
# bound (<= --depth, computed from pack structure) and the 1.02 size limit; the
# timing limits are a secondary guard set at 1.40 so the untouched baseline
# reproduces within tolerance. See SCORING.md.
PACK_TIME_LIMIT = 1.40
INDEX_TIME_LIMIT = 1.40
READ_TIME_LIMIT = 1.40
SIZE_LIMIT = 1.02


def failure_rewards(total_tests=TOTAL_TESTS, code=E_INTERNAL):
    return {
        "correctness_passed": 0,
        "compatibility_passed": 0,
        "geo_mean_size_ratio": 0.0,
        "num_passed_tests": 0,
        "total_tests": total_tests,
        "max_pack_time_ratio": 0.0,
        "max_index_time_ratio": 0.0,
        "max_read_time_ratio": 0.0,
        "max_pack_size_ratio": 0.0,
        "small_cases_passed": 0,
        "large_cases_passed": 0,
        "error": code,
    }


def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of nothing")
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def finite(x, default=0.0):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def case_ratios(base_runs, cand_runs):
    """Per-case ratios from complete run lists (all repeats kept).

    Bytes use the median (they capture nondeterministic packing size). Time
    ratios use the minimum over the runs: wall time under host contention can
    only rise, so the minimum is the least-contended, most reproducible signal,
    and it is symmetric between the two engines. A candidate that is genuinely
    slower shows it in the minimum too.
    """
    b_bytes = median([r["bytes"] for r in base_runs])
    c_bytes = median([r["bytes"] for r in cand_runs])
    out = {
        "baseline_bytes": b_bytes, "candidate_bytes": c_bytes,
        "size_gain": b_bytes / c_bytes,                  # B_i / C_i (objective term)
        "size_ratio": c_bytes / b_bytes,                 # C_i / B_i (limit <= 1.02)
        "pack_time_ratio": min(r["t_pack"] for r in cand_runs) / min(r["t_pack"] for r in base_runs),
        "index_time_ratio": min(r["t_index"] for r in cand_runs) / min(r["t_index"] for r in base_runs),
        "read_time_ratio": {},
        "aggregation": {"bytes": "median", "time": "min"},
    }
    for k in base_runs[0]["t_read"]:
        out["read_time_ratio"][k] = (min(r["t_read"][k] for r in cand_runs) /
                                     min(r["t_read"][k] for r in base_runs))
    return out


def within_limits(r):
    return (r["pack_time_ratio"] <= PACK_TIME_LIMIT and r["index_time_ratio"] <= INDEX_TIME_LIMIT
            and all(v <= READ_TIME_LIMIT for v in r["read_time_ratio"].values())
            and r["size_ratio"] <= SIZE_LIMIT)


def aggregate(small_ok, n_small_expected, large, n_large_expected, fatal_code=E_NONE):
    """Compute the reward dict from complete per-case evidence.

    small_ok: list of booleans for completed small comparisons.
    large: list of dicts {"ok": bool, "ratios": case_ratios(...) or None}.
    The gate (correctness_passed) is the one predicate for both files.
    """
    total = n_small_expected + n_large_expected
    rw = failure_rewards(total, fatal_code or E_NONE)
    n_small = sum(1 for x in small_ok if x)
    n_large = sum(1 for c in large if c["ok"])
    rw["small_cases_passed"] = n_small
    rw["large_cases_passed"] = n_large
    rw["num_passed_tests"] = n_small + n_large
    measured = [c["ratios"] for c in large if c.get("ratios")]
    if measured:
        rw["max_pack_time_ratio"] = finite(max(r["pack_time_ratio"] for r in measured))
        rw["max_index_time_ratio"] = finite(max(r["index_time_ratio"] for r in measured))
        rw["max_read_time_ratio"] = finite(max(max(r["read_time_ratio"].values()) for r in measured))
        rw["max_pack_size_ratio"] = finite(max(r["size_ratio"] for r in measured))
    semantic_ok = (fatal_code == E_NONE and len(small_ok) == n_small_expected and n_small == n_small_expected
                   and len(large) == n_large_expected and n_large == n_large_expected)
    limits_ok = semantic_ok and len(measured) == n_large_expected and all(within_limits(r) for r in measured)
    rw["compatibility_passed"] = 1 if semantic_ok else 0
    gate = semantic_ok and limits_ok
    if gate:
        g = math.exp(sum(math.log(r["size_gain"]) for r in measured) / len(measured))
        rw["geo_mean_size_ratio"] = finite(g)
        rw["correctness_passed"] = 1
        rw["error"] = E_NONE
    else:
        rw["correctness_passed"] = 0
        rw["geo_mean_size_ratio"] = 0.0
        if fatal_code != E_NONE:
            rw["error"] = fatal_code
        elif not semantic_ok:
            rw["error"] = E_SMALL_CASE if n_small != n_small_expected or len(small_ok) != n_small_expected else E_LARGE_CASE
        else:
            rw["error"] = E_RESOURCE
    return rw


def gate_passes(rw):
    return rw.get("correctness_passed") == 1


def _atomic_write(path, data):
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    if os.path.lexists(path) and (os.path.isdir(path) and not os.path.islink(path)):
        import shutil
        shutil.rmtree(path)
    with open(tmp, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_rewards(out_dir, rw, message=None, details=None):
    """Validate and atomically write reward.json and reward.txt from one predicate."""
    clean = {}
    for k, v in failure_rewards(rw.get("total_tests", TOTAL_TESTS)).items():
        val = rw.get(k, v)
        clean[k] = int(val) if isinstance(v, int) and not isinstance(v, bool) else finite(val)
    txt = "1\n" if gate_passes(clean) else "0\n"
    if details is not None:
        _atomic_write(os.path.join(out_dir, "details.json"),
                      json.dumps(details, indent=1, sort_keys=True, default=str))
    _atomic_write(os.path.join(out_dir, "error.txt"),
                  (message or ERROR_NAMES.get(clean["error"], "")) + "\n")
    _atomic_write(os.path.join(out_dir, "reward.json"), json.dumps(clean, sort_keys=True) + "\n")
    _atomic_write(os.path.join(out_dir, "reward.txt"), txt)
    return clean
