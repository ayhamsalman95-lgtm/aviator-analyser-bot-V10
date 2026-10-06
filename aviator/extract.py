"""Evidence-aware fairness parsing.

Only explicitly named fields are interpreted. Ambiguous cryptographic fields are
preserved as observations instead of being forced into verifier semantics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .fairness import REQUIRED_PLAYER_SEEDS, round_hash, sha256_hex
from .validation import RoundValidationError, parse_round_id


def norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


ROUND_ID_KEYS = {"roundid"}
SERVER_SEED_KEYS = {"serverseed", "revealedserverseed"}
# Hash-like fields are observations until their protocol semantics are explicitly
# established. They must not be promoted to verifier inputs merely from naming.
COMMITMENT_KEYS: set[str] = set()
PLAYER_SEEDS_KEYS = {"playerseeds", "clientseeds", "playersseeds"}
ROUND_HASH_KEYS: set[str] = set()
OBSERVED_CRYPTO_KEYS = {
    "seedsha256",
    "serverseedsha256",
    "serverseedhash",
    "hashedserverseed",
    "nextserverseedsha256",
    "combinedhash",
    "combinedseedhash",
    "hashsha512",
    "sha512hash",
    "roundhashsha512",
}
SEED_ITEM_KEYS = ("seed", "clientSeed", "playerSeed", "value")

SEED_RE = re.compile(r"^[A-Za-z0-9_\-]{4,128}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
HEX128 = re.compile(r"^[0-9a-fA-F]{128}$")


@dataclass(frozen=True)
class FairnessProfile:
    """Protocol-specific reading of fairness fields.

    A profile never classifies a hash by its name alone. ``commitment_keys`` and
    ``round_hash_keys`` list fields that *may* carry a commitment / round hash; such a
    field is promoted only when the digest relationship is proven inside the same
    object (SHA-256(serverSeed) or SHA-512(serverSeed + first three seeds)). Every
    other hash stays an unclassified observation.
    """
    name: str
    game_id: Optional[int] = None
    accepted_fields: frozenset = frozenset()
    commitment_keys: frozenset = frozenset()
    round_hash_keys: frozenset = frozenset()


GENERIC_PROFILE = FairnessProfile(name="generic")

# Spribe Aviator, game_id 52358. Field names are compared after ``norm``.
SPRIBE_AVIATOR_PROFILE = FairnessProfile(
    name="spribe_aviator",
    game_id=52358,
    accepted_fields=frozenset(norm(k) for k in (
        "serverSeed", "revealedServerSeed", "playerSeeds", "clientSeeds",
        "serverSeedSHA256", "roundHashSHA512")),
    commitment_keys=frozenset({norm("serverSeedSHA256")}),
    round_hash_keys=frozenset({norm("roundHashSHA512")}),
)

_PROFILES_BY_GAME = {SPRIBE_AVIATOR_PROFILE.game_id: SPRIBE_AVIATOR_PROFILE}


def profile_for_game(game_id: Any) -> FairnessProfile:
    try:
        return _PROFILES_BY_GAME.get(int(game_id), GENERIC_PROFILE)
    except (TypeError, ValueError):
        return GENERIC_PROFILE


@dataclass
class FairnessRecord:
    path: str
    round_id: Optional[int] = None
    server_seed: Optional[str] = None
    player_seeds: list[str] = field(default_factory=list)
    commitment: Optional[str] = None
    round_hash: Optional[str] = None
    crypto_observations: list[dict[str, Any]] = field(default_factory=list)
    is_next_commitment: bool = False
    scan_truncated: bool = False
    scan_limit: Optional[int] = None
    list_truncations: int = 0
    profile: str = "generic"

    def meaningful(self) -> bool:
        return bool(self.server_seed or self.player_seeds or self.commitment or
                    self.round_hash or self.crypto_observations or self.scan_truncated)


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
            out.append(s)
    return out


def _confirm_with_profile(rec: FairnessRecord, profile: FairnessProfile) -> None:
    """Promote profile-listed hash observations whose digest relationship is proven.

    Unproven hashes are left untouched as ``semantic_type == "unknown"`` observations.
    """
    if not (profile.commitment_keys or profile.round_hash_keys) or not rec.server_seed:
        return
    for obs in rec.crypto_observations:
        key = norm(obs["field_name"])
        value = obs["value"]
        if key in profile.commitment_keys and obs.get("algorithm") == "SHA-256":
            if value == sha256_hex(rec.server_seed):
                obs["semantic_type"] = "commitment_sha256"
                obs["confirmation"] = "digest_matches_server_seed"
                rec.commitment = value
        elif key in profile.round_hash_keys and obs.get("algorithm") == "SHA-512":
            if len(rec.player_seeds) >= REQUIRED_PLAYER_SEEDS:
                seeds = rec.player_seeds[:REQUIRED_PLAYER_SEEDS]
                if value == round_hash(rec.server_seed, seeds):
                    obs["semantic_type"] = "round_hash_sha512"
                    obs["confirmation"] = "digest_matches_seeds"
                    rec.round_hash = value


def _record_from_dict(d: dict, path: str, profile: FairnessProfile = GENERIC_PROFILE) -> FairnessRecord:
    rec = FairnessRecord(path=path, profile=profile.name)
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
            text = v.strip() if isinstance(v, str) else None
            if text and HEX128.match(text):
                rec.round_hash = text.lower()
        elif nk in OBSERVED_CRYPTO_KEYS:
            if isinstance(v, str) and (HEX64.match(v.strip()) or HEX128.match(v.strip())):
                text = v.strip().lower()
                algorithm = None
                if nk in {"seedsha256", "serverseedsha256", "nextserverseedsha256"}:
                    algorithm = "SHA-256"
                elif nk in {"hashsha512", "sha512hash", "roundhashsha512"}:
                    algorithm = "SHA-512"
                rec.crypto_observations.append({
                    "field_name": str(k),
                    "value": text,
                    "algorithm": algorithm,
                    "value_length": len(text),
                    "semantic_type": "unknown",
                })
    _confirm_with_profile(rec, profile)
    return rec


def extract_fairness(data: Any, max_nodes: int = 500,
                     profile: FairnessProfile = GENERIC_PROFILE) -> list[FairnessRecord]:
    """Return one record per object that directly contains named evidence fields."""
    out: list[FairnessRecord] = []
    stack: list[tuple[Any, str]] = [(data, "$")]
    seen: set[int] = set()
    nodes = 0
    list_truncations = 0
    while stack and nodes < max_nodes:
        node, path = stack.pop()
        nodes += 1
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            rec = _record_from_dict(node, path, profile)
            if rec.meaningful():
                out.append(rec)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    stack.append((v, f"{path}.{k}"))
        elif isinstance(node, list):
            if len(node) > 500:
                list_truncations += 1
            for i, v in enumerate(node[:500]):
                if isinstance(v, (dict, list)):
                    stack.append((v, f"{path}[{i}]"))
    scan_truncated = bool(stack) or list_truncations > 0
    if scan_truncated:
        out.append(FairnessRecord(path="$.__scan__", scan_truncated=True,
                                  scan_limit=max_nodes, list_truncations=list_truncations))
    out.sort(key=lambda r: r.path)
    return out
