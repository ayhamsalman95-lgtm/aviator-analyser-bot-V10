"""Strict completed-round validation.

A completed round is valid only when it has a positive integer round id and a
finite multiplier >= 1.00 with at most two decimals (Aviator results are cents).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

_INT_RE = re.compile(r"^[0-9]{1,18}$")


@dataclass(frozen=True)
class ValidRound:
    round_id: int
    multiplier: float
    cents: int


class RoundValidationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_round_id(value: Any) -> int:
    if isinstance(value, bool):
        raise RoundValidationError("round_id_bool")
    if isinstance(value, int):
        rid = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise RoundValidationError("round_id_not_integer")
        rid = int(value)
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        rid = int(value.strip())
    else:
        raise RoundValidationError("round_id_not_numeric")
    if rid <= 0:
        raise RoundValidationError("round_id_not_positive")
    return rid


def parse_multiplier(value: Any, max_multiplier: float = 1_000_000.0) -> tuple[float, int]:
    if isinstance(value, bool) or value is None:
        raise RoundValidationError("multiplier_missing")
    if isinstance(value, str):
        text = value.strip().lower().rstrip("x").replace(",", ".")
        try:
            value = float(text)
        except ValueError as exc:
            raise RoundValidationError("multiplier_not_numeric") from exc
    if not isinstance(value, (int, float)):
        raise RoundValidationError("multiplier_not_numeric")
    x = float(value)
    if not math.isfinite(x):
        raise RoundValidationError("multiplier_not_finite")
    if x < 1.0:
        raise RoundValidationError("multiplier_below_1")
    if x > max_multiplier:
        raise RoundValidationError("multiplier_above_max")
    cents = int(round(x * 100))
    if abs(cents - x * 100) > 1e-6 * max(1.0, x):
        raise RoundValidationError("multiplier_more_than_2_decimals")
    return cents / 100.0, cents


def validate_round(round_id: Any, multiplier: Any, max_multiplier: float = 1_000_000.0) -> ValidRound:
    rid = parse_round_id(round_id)
    mult, cents = parse_multiplier(multiplier, max_multiplier)
    return ValidRound(rid, mult, cents)
