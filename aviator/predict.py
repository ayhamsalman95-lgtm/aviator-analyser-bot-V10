"""Frozen, leak-free baseline probability models.

The old `LightLSTM` (random, untrained recurrent weights + ridge readout) was
removed: it was never trained/evaluated as a forecaster and must not be shown
as one. What remains are honest probability baselines for P(X >= t):

* theoretical      exact under the 97%-RTP formula: P(X >= t) = 0.97 / t  (t >= 1)
* empirical_all    Beta-binomial posterior on all frozen history, prior centred on theory
* empirical_recent same, last `recent_window` rounds only

Freeze rule: a prediction for round R is computed ONLY on changeState
newStateId=1 for R, from rounds with round_id < R that were already stored
(first_seen_at <= frozen_at). The result of R can never be a feature.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional

MODEL_VERSION = "baseline-v1"
MODELS = ("theoretical", "empirical_all", "empirical_recent")


def theoretical_survival(t: float, rtp: float = 0.97) -> float:
    if t <= 1.0:
        return 1.0
    return min(1.0, rtp / t)


def beta_posterior(k: int, n: int, p0: float, strength: float) -> float:
    a = strength * p0
    b = strength * (1.0 - p0)
    return (k + a) / (n + a + b)


def clip_p(p: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, p))


@dataclass(frozen=True)
class FrozenFeatures:
    target_round_id: int
    frozen_at: float
    round_ids: tuple[int, ...]
    cents: tuple[int, ...]
    max_seq: Optional[int]

    @property
    def count(self) -> int:
        return len(self.cents)

    @property
    def max_round_id(self) -> Optional[int]:
        return self.round_ids[-1] if self.round_ids else None

    def digest(self) -> str:
        h = hashlib.sha256()
        for rid, c in zip(self.round_ids, self.cents):
            h.update(f"{rid}:{c},".encode())
        return h.hexdigest()


def freeze_features(store, target_round_id: int, frozen_at: float) -> FrozenFeatures:
    rows = store.rounds_before(target_round_id, frozen_at)
    ids = tuple(int(r["round_id"]) for r in rows)
    cents = tuple(int(r["cents"]) for r in rows)
    max_seq = max((int(r["seq"]) for r in rows), default=None)
    assert all(i < target_round_id for i in ids), "leak: target or later round in features"
    return FrozenFeatures(target_round_id, frozen_at, ids, cents, max_seq)


def probabilities(cents_history: Iterable[int], thresholds: Iterable[float], rtp: float = 0.97,
                  recent_window: int = 200, prior_strength: float = 20.0) -> dict:
    hist = list(cents_history)
    recent = hist[-recent_window:] if recent_window > 0 else hist
    out = {m: {} for m in MODELS}
    for t in thresholds:
        key = f"{float(t):g}"
        p0 = theoretical_survival(float(t), rtp)
        thr_c = int(round(float(t) * 100))
        k_all = sum(1 for c in hist if c >= thr_c)
        k_rec = sum(1 for c in recent if c >= thr_c)
        out["theoretical"][key] = p0
        out["empirical_all"][key] = beta_posterior(k_all, len(hist), p0, prior_strength)
        out["empirical_recent"][key] = beta_posterior(k_rec, len(recent), p0, prior_strength)
    return {"models": out, "n_all": len(hist), "n_recent": len(recent)}


def make_prediction(store, cfg, target_round_id: int, frozen_at: float, trigger: str) -> Optional[dict]:
    feats = freeze_features(store, target_round_id, frozen_at)
    probs = probabilities(feats.cents, cfg["thresholds"], float(cfg["rtp"]),
                          int(cfg["recent_window"]), float(cfg["prior_strength"]))
    output = {"model_version": MODEL_VERSION, "thresholds": [float(t) for t in cfg["thresholds"]],
              **probs, "note": "probabilities only; rounds are designed to be independent"}
    return {"round_id": target_round_id, "frozen_at": frozen_at, "trigger": trigger,
            "model_version": MODEL_VERSION, "feature_count": feats.count,
            "feature_max_round_id": feats.max_round_id, "feature_max_seq": feats.max_seq,
            "feature_hash": feats.digest(), "output": output}


def leakage_problems(pred: dict, rnd: dict) -> list[str]:
    problems = []
    if int(pred["round_id"]) != int(rnd["round_id"]):
        problems.append("prediction/round id mismatch")
    fmax = pred.get("feature_max_round_id")
    if fmax is not None and int(fmax) >= int(rnd["round_id"]):
        problems.append(f"feature_max_round_id {fmax} >= target {rnd['round_id']}")
    if float(pred["frozen_at"]) >= float(rnd["first_seen_at"]):
        problems.append("prediction frozen at/after the target result was known")
    return problems
