"""Chronological walk-forward evaluation (no look-ahead).

For each index i >= min_train the models see ONLY rounds[:i] (ordered by round id),
predict P(X_i >= t), and are scored against X_i. Counts are updated after scoring.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Iterable, Sequence

from .predict import MODELS, beta_posterior, clip_p, theoretical_survival


def log_loss(ps: Sequence[float], ys: Sequence[int]) -> float:
    if not ps:
        return float("nan")
    return -sum(y * math.log(clip_p(p)) + (1 - y) * math.log(1 - clip_p(p)) for p, y in zip(ps, ys)) / len(ps)


def brier(ps: Sequence[float], ys: Sequence[int]) -> float:
    if not ps:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def calibration(ps: Sequence[float], ys: Sequence[int], bins: int = 10) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not idx:
            continue
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                    "mean_pred": sum(ps[i] for i in idx) / len(idx),
                    "observed": sum(ys[i] for i in idx) / len(idx)})
    return out


def walk_forward(cents: Sequence[int], thresholds: Iterable[float], rtp: float = 0.97,
                 recent_window: int = 200, prior_strength: float = 20.0, min_train: int = 100,
                 _observer=None) -> dict:
    thresholds = [float(t) for t in thresholds]
    thr_c = {t: int(round(t * 100)) for t in thresholds}
    k_all = {t: 0 for t in thresholds}
    recent: deque = deque(maxlen=recent_window if recent_window > 0 else None)
    k_rec = {t: 0 for t in thresholds}
    preds = {m: {t: [] for t in thresholds} for m in MODELS}
    ys = {t: [] for t in thresholds}
    seen = 0
    for i, c in enumerate(cents):
        if i >= min_train:
            if _observer is not None:
                _observer(i, seen)  # test hook: number of rounds the model has seen at step i
            for t in thresholds:
                p0 = theoretical_survival(t, rtp)
                preds["theoretical"][t].append(p0)
                preds["empirical_all"][t].append(beta_posterior(k_all[t], i, p0, prior_strength))
                preds["empirical_recent"][t].append(beta_posterior(k_rec[t], len(recent), p0, prior_strength))
                ys[t].append(1 if c >= thr_c[t] else 0)
        # update AFTER scoring
        if recent.maxlen is not None and len(recent) == recent.maxlen:
            old = recent[0]
            for t in thresholds:
                if old >= thr_c[t]:
                    k_rec[t] -= 1
        recent.append(c)
        seen += 1
        for t in thresholds:
            if c >= thr_c[t]:
                k_all[t] += 1
                k_rec[t] += 1
    return summarize(preds, ys, thresholds, n_total=len(cents), min_train=min_train)


def summarize(preds: dict, ys: dict, thresholds: list[float], **extra) -> dict:
    res = {"thresholds": thresholds, **extra, "metrics": {}}
    for t in thresholds:
        key = f"{t:g}"
        y = ys[t]
        res["metrics"][key] = {"n": len(y), "base_rate": (sum(y) / len(y)) if y else None, "models": {}}
        for m in preds:
            p = preds[m][t]
            res["metrics"][key]["models"][m] = {"log_loss": log_loss(p, y), "brier": brier(p, y),
                                                "calibration": calibration(p, y)}
    return res


def evaluate_logged(store, thresholds: Iterable[float]) -> dict:
    """Score predictions that were frozen live (leaky ones are excluded)."""
    import json
    thresholds = [float(t) for t in thresholds]
    rows = store.resolved_predictions(include_leaky=False)
    preds: dict = {}
    ys = {t: [] for t in thresholds}
    for r in rows:
        out = json.loads(r["output_json"])
        for t in thresholds:
            key = f"{t:g}"
            if all(key in out["models"].get(m, {}) for m in out["models"]):
                ys[t].append(1 if r["actual_cents"] >= int(round(t * 100)) else 0)
                for m, table in out["models"].items():
                    preds.setdefault(m, {tt: [] for tt in thresholds})[t].append(table[key])
    leaky = store.conn.execute("SELECT COUNT(*) FROM predictions WHERE leak_flag=1").fetchone()[0]
    return summarize(preds, ys, thresholds, n_resolved=len(rows), n_leaky_excluded=leaky)
