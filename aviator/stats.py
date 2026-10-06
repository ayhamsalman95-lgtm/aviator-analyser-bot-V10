"""Descriptive statistics and independence tests (pure Python)."""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence

BUCKETS = [(1.00, 1.50), (1.50, 2.00), (2.00, 3.00), (3.00, 5.00), (5.00, 10.00), (10.00, float("inf"))]


def bucket_of(x: float) -> int:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= x < hi:
            return i
    return len(BUCKETS) - 1


def bucket_label(i: int) -> str:
    lo, hi = BUCKETS[i]
    return f">={lo:g}" if hi == float("inf") else f"{lo:g}-{hi:g}"


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_sf(x: float, k: int) -> float:
    try:
        from scipy.stats import chi2  # optional
        return float(chi2.sf(x, k))
    except Exception:
        if k <= 0:
            return float("nan")
        # Wilson-Hilferty approximation
        z = ((x / k) ** (1 / 3) - (1 - 2 / (9 * k))) / math.sqrt(2 / (9 * k))
        return normal_sf(z)


def streaks(values: Sequence[float], threshold: float) -> dict:
    below_runs: list[int] = []
    above_runs: list[int] = []
    run = 0
    cur = None
    for v in values:
        state = v >= threshold
        if state == cur:
            run += 1
        else:
            if cur is not None:
                (above_runs if cur else below_runs).append(run)
            cur, run = state, 1
    if cur is not None:
        (above_runs if cur else below_runs).append(run)
    current = {"type": ("above" if cur else "below") if cur is not None else None, "length": run}
    return {"threshold": threshold, "longest_below": max(below_runs, default=0),
            "longest_above": max(above_runs, default=0), "current": current,
            "below_run_hist": dict(sorted(Counter(below_runs).items())),
            "above_run_hist": dict(sorted(Counter(above_runs).items()))}


def transitions(values: Sequence[float]) -> dict:
    k = len(BUCKETS)
    m = [[0] * k for _ in range(k)]
    for a, b in zip(values, values[1:]):
        m[bucket_of(a)][bucket_of(b)] += 1
    n = sum(map(sum, m))
    rows = [sum(r) for r in m]
    cols = [sum(m[i][j] for i in range(k)) for j in range(k)]
    chi = 0.0
    used_r = [i for i in range(k) if rows[i]]
    used_c = [j for j in range(k) if cols[j]]
    for i in used_r:
        for j in used_c:
            e = rows[i] * cols[j] / n
            chi += (m[i][j] - e) ** 2 / e
    dof = max(0, (len(used_r) - 1) * (len(used_c) - 1))
    probs = [[(m[i][j] / rows[i]) if rows[i] else None for j in range(k)] for i in range(k)]
    return {"labels": [bucket_label(i) for i in range(k)], "counts": m, "row_probs": probs,
            "n_transitions": n, "chi2": chi, "dof": dof, "p_value": chi2_sf(chi, dof) if dof else None}


def autocorrelation(values: Sequence[float], max_lag: int = 20) -> dict:
    xs = [math.log(v) for v in values if v > 0]
    n = len(xs)
    if n < 3:
        return {"n": n, "lags": {}, "band_95": None}
    mu = sum(xs) / n
    var = sum((x - mu) ** 2 for x in xs)
    lags = {}
    for lag in range(1, min(max_lag, n - 1) + 1):
        cov = sum((xs[i] - mu) * (xs[i + lag] - mu) for i in range(n - lag))
        lags[lag] = cov / var if var else 0.0
    band = 1.96 / math.sqrt(n)
    # Ljung-Box Q
    q = n * (n + 2) * sum(r * r / (n - k) for k, r in lags.items())
    return {"n": n, "series": "log(multiplier)", "lags": lags, "band_95": band,
            "outside_band": [k for k, r in lags.items() if abs(r) > band],
            "ljung_box_q": q, "ljung_box_p": chi2_sf(q, len(lags))}


def runs_test(values: Sequence[float], threshold: float) -> dict:
    seq = [v >= threshold for v in values]
    n1 = sum(seq)
    n2 = len(seq) - n1
    if n1 == 0 or n2 == 0:
        return {"threshold": threshold, "n": len(seq), "runs": None, "z": None, "p_value": None}
    runs = 1 + sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    n = n1 + n2
    mu = 2 * n1 * n2 / n + 1
    var = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1))
    z = (runs - mu) / math.sqrt(var) if var > 0 else 0.0
    return {"threshold": threshold, "n": n, "n_above": n1, "n_below": n2, "runs": runs,
            "expected_runs": mu, "z": z, "p_value": 2 * normal_sf(abs(z))}


def threshold_table(values: Sequence[float], thresholds: Sequence[float], rtp: float = 0.97) -> list[dict]:
    n = len(values)
    out = []
    for t in thresholds:
        k = sum(1 for v in values if v >= t)
        p0 = min(1.0, rtp / t) if t > 1 else 1.0
        se = math.sqrt(p0 * (1 - p0) / n) if n and 0 < p0 < 1 else None
        z = ((k / n) - p0) / se if se else None
        out.append({"threshold": t, "n": n, "count": k, "observed": (k / n) if n else None,
                    "theoretical": p0, "z_vs_theory": z,
                    "p_value": 2 * normal_sf(abs(z)) if z is not None else None})
    return out
