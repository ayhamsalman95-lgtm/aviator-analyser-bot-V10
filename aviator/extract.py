"""Strict provably-fair parsing.

Rules (replacing the old heuristic seeds.py):
* Only explicitly named keys are read. Bare `hash`, `seed`, `sh`, `cs`, ... are ignored.
* No free-text / raw-hex scanning. No DOM scraping. No nonce (Aviator does not use one).
* A value is associated with a round ONLY when a `roundId` key sits in the SAME
  object as the fairness fields. A nested `fairness` object uses its own
  `roundId`; it never inherits the parent message's (current) round id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .validation import RoundValidationError, parse_round_id


def norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


ROUND_ID_KEYS = {"roundid"}
SERVER_SEED_KEYS = {"serverseed", "revealedserverseed"}
COMMITMENT_KEYS = {"serverseedsha256", "serverseedhash", "hashedserverseed", "nextserverseedsha256"}
PLAYER_SEEDS_KEYS = {"playerseeds", "clientseeds", "playersseeds"}
ROUND_HASH_KEYS = {"combinedhash", "combinedseedhash", "hashsha512", "sha512hash", "roundhashsha512", "seedsha256"}
SEED_ITEM_KEYS = ("seed", "clientSeed", "playerSeed", "value")

SEED_RE = re.compile(r"^[A-Za-z0-9_\-]{4,128}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")


@dataclass
class FairnessRecord:
    path: str
    round_id: Optional[int] = None
    server_seed: Optional[str] = None
    player_seeds: list[str] = field(default_factory=list)
    commitment: Optional[str] = None
    round_hash: Optional[str] = None
    is_next_commitment: bool = False

    def meaningful(self) -> bool:
        return bool(self.server_seed or self.player_seeds or self.commitment or self.round_hash)


def _seed(value: Any) -> Optional[str]:
    if isinstance(value, str) and SEED_RE.match(value.strip()):
        return value.strip()
    return None


def _player_seeds(value: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in value:
        s = None
        if isinstance(item, str):
            s = _seed(item)
        elif isinstance(item, dict):
            for k in SEED_ITEM_KEYS:
                if k in item:
                    s = _seed(item[k])
                    if s:
                        break
        if s:
            out.append(s)  # order preserved: panel order matters for SHA-512
    return out


def _record_from_dict(d: dict, path: str) -> FairnessRecord:
    rec = FairnessRecord(path=path)
    for k, v in d.items():
        nk = norm(k)
        if nk in ROUND_ID_KEYS:
            try:
                rec.round_id = parse_round_id(v)
            except RoundValidationError:
                rec.round_id = None
        elif nk in SERVER_SEED_KEYS:
            rec.server_seed = _seed(v)
        elif nk in COMMITMENT_KEYS:
            if isinstance(v, str) and HEX64.match(v.strip()):
                rec.commitment = v.strip().lower()
                rec.is_next_commitment = nk.startswith("next")
        elif nk in PLAYER_SEEDS_KEYS:
            rec.player_seeds = _player_seeds(v)
        elif nk in ROUND_HASH_KEYS:
            if isinstance(v, str) and HEX128.match(v.strip()):
                rec.round_hash = v.strip().lower()
    return rec


def extract_fairness(data: Any, max_nodes: int = 2000) -> list[FairnessRecord]:
    """Return one record per object that directly contains fairness fields."""
    out: list[FairnessRecord] = []
    stack: list[tuple[Any, str]] = [(data, "$")]
    seen: set[int] = set()
    nodes = 0
    while stack and nodes < max_nodes:
        node, path = stack.pop()
        nodes += 1
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            rec = _record_from_dict(node, path)
            if rec.meaningful():
                out.append(rec)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    stack.append((v, f"{path}.{k}"))
        elif isinstance(node, list):
            for i, v in enumerate(node[:500]):
                if isinstance(v, (dict, list)):
                    stack.append((v, f"{path}[{i}]"))
    out.sort(key=lambda r: r.path)
    return out
