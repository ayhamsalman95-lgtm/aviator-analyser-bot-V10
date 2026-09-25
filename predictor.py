import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class Prediction:
    combined: float
    heuristic: float
    sequence: float
    agreement: float


class LightLSTM:
    """Small NumPy LSTM feature extractor + trained linear readout.

    The recurrent weights are deterministic and the output head is fitted by
    ridge regression. This keeps the install small and makes the bot practical
    on modest PCs. It is not a reverse-engineering method for provably-fair RNG.
    """

    def __init__(self, hidden: int = 12, ridge: float = 1e-2, seed: int = 42):
        self.hidden = hidden
        self.ridge = ridge
        rng = np.random.default_rng(seed)
        # Concatenate x and h. Four gates: input, forget, output, candidate.
        self.W = rng.normal(0, 0.18, size=(1 + hidden, 4 * hidden))
        self.b = np.zeros(4 * hidden, dtype=float)
        self.beta = None

    @staticmethod
    def _sigmoid(x):
        x = np.clip(x, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-x))

    def _features(self, seq: np.ndarray) -> np.ndarray:
        h = np.zeros(self.hidden, dtype=float)
        c = np.zeros(self.hidden, dtype=float)
        states = []
        for x in seq.reshape(-1):
            z = np.concatenate(([float(x)], h)) @ self.W + self.b
            i = self._sigmoid(z[:self.hidden])
            f = self._sigmoid(z[self.hidden:2*self.hidden])
            o = self._sigmoid(z[2*self.hidden:3*self.hidden])
            g = np.tanh(z[3*self.hidden:4*self.hidden])
            c = f * c + i * g
            h = o * np.tanh(c)
            states.append(h)
        return np.concatenate((h, [1.0]))

    def fit(self, X: np.ndarray, y: np.ndarray):
        F = np.vstack([self._features(s) for s in X])
        I = np.eye(F.shape[1])
        self.beta = np.linalg.solve(F.T @ F + self.ridge * I, F.T @ y)
        return self

    def predict(self, seq: np.ndarray) -> float:
        if self.beta is None:
            raise RuntimeError("model not fitted")
        f = self._features(seq)
        return float(f @ self.beta)


def _log_values(values):
    return np.log(np.maximum(np.asarray(values, dtype=float), 1.0))


def heuristic_predict(history, window=10):
    x = _log_values(history[-window:])
    weights = np.arange(1, len(x) + 1, dtype=float)
    weighted = np.average(x, weights=weights)
    med = float(np.median(x))
    recent = float(np.mean(x[-3:]))
    # Robust blend: weighted level + median + recent level.
    score = 0.55 * weighted + 0.30 * med + 0.15 * recent
    return float(np.clip(math.exp(score), 1.01, 50.0))


def sequence_predict(history, window=10) -> Optional[float]:
    if len(history) < window + 8:
        return None
    logs = _log_values(history)
    mu = float(np.mean(logs))
    sd = float(np.std(logs))
    sd = max(sd, 0.15)
    z = (logs - mu) / sd

    X = []
    y = []
    for i in range(window, len(z)):
        X.append(z[i-window:i])
        y.append(z[i])
    if len(X) < 8:
        return None

    model = LightLSTM(hidden=12, ridge=0.03)
    model.fit(np.asarray(X, dtype=float), np.asarray(y, dtype=float))
    pred_z = model.predict(z[-window:])
    pred = math.exp(pred_z * sd + mu)
    return float(np.clip(pred, 1.01, 50.0))


def combined_predict(history, window=10) -> Prediction:
    h = heuristic_predict(history, window)
    s = sequence_predict(history, window)
    if s is None:
        return Prediction(combined=h, heuristic=h, sequence=h, agreement=0.0)

    gap = abs(math.log(max(h, 1.0)) - math.log(max(s, 1.0)))
    agreement = 100.0 * math.exp(-gap)
    w = agreement / 100.0
    combined = w * ((h + s) / 2.0) + (1.0 - w) * h
    return Prediction(
        combined=float(np.clip(combined, 1.01, 50.0)),
        heuristic=h,
        sequence=s,
        agreement=float(min(100.0, agreement)),
    )
