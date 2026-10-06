"""Spribe Aviator provably-fair verification.

Confirmed pipeline (Spribe public description + independent reproductions):

* Before the round the operator publishes SHA-256(server_seed)   -> "commitment"
* Round hash  = SHA-512(server_seed + seed1 + seed2 + seed3)      (plain concat,
  exactly the first three player seeds, in panel order, no separator, NO nonce)
* Multiplier  = max(1.00, floor(97 * 2^52 / (2^52 - h)) / 100),
  h = int(first 13 hex chars of the round hash)

The hash steps are exact. Spribe has not published the hash->multiplier mapping;
the formula above is the widely reproduced 97%-RTP reconstruction. Therefore:

``verified`` is True ONLY when, for an explicitly associated round:
  - exactly three player seeds and a revealed server seed are present,
  - the recomputed multiplier equals the recorded canonical multiplier (cents),
  - the commitment (if one is associated) equals SHA-256(server_seed),
  - the panel round hash (if one is associated) equals the recomputed SHA-512.
Anything else is "incomplete", "mismatch" or "conflict" -- never verified.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

FORMULA = "sha512(server+s1+s2+s3); m=max(100,floor(97*2^52/(2^52-h)))/100"
E52 = 2 ** 52
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX128 = re.compile(r"^[0-9a-f]{128}$")
REQUIRED_PLAYER_SEEDS = 3


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha512_hex(text: str) -> str:
    return hashlib.sha512(text.encode("utf-8")).hexdigest()


def round_hash(server_seed: str, player_seeds: list[str]) -> str:
    return sha512_hex(server_seed + "".join(player_seeds))


def cents_from_hash(digest_hex: str, rtp_percent: int = 97) -> int:
    h = int(digest_hex[:13], 16)
    cents = (rtp_percent * E52) // (E52 - h)
    return max(100, cents)


def multiplier_from_seeds(server_seed: str, player_seeds: list[str]) -> tuple[str, int]:
    digest = round_hash(server_seed, player_seeds)
    return digest, cents_from_hash(digest)


@dataclass
class Verification:
    round_id: int
    status: str                      # verified | mismatch | incomplete | conflict | not_verifiable
    verified: bool
    detail: str
    server_seed: Optional[str] = None
    player_seeds: list[str] = field(default_factory=list)
    commitment: Optional[str] = None
    panel_hash: Optional[str] = None
    computed_hash: Optional[str] = None
    computed_cents: Optional[int] = None
    recorded_cents: Optional[int] = None
    commitment_match: Optional[bool] = None
    panel_hash_match: Optional[bool] = None
    verification_basis: str = "cryptographic_semantics_unestablished"
    formula: str = FORMULA

    def to_dict(self) -> dict:
        return asdict(self)


def verify_round(
    round_id: int,
    recorded_cents: Optional[int],
    server_seeds: list[str],
    player_seed_sets: list[list[str]],
    commitments: list[str],
    panel_hashes: list[str],
    *,
    evidence_semantics_confirmed: bool = False,
) -> Verification:
    """Verify one round only when the evidence semantics are explicitly established.

    The default is conservative: field names or digest lengths alone are not enough
    to establish a commitment, round-hash, or multiplier-verification relationship.
    """
    if not evidence_semantics_confirmed:
        return Verification(
            round_id=round_id,
            status="not_verifiable",
            verified=False,
            detail="cryptographic evidence semantics are not established; observations preserved without verification",
            recorded_cents=recorded_cents,
        )
    server_set = sorted(set(server_seeds))
    seed_sets = []
    for s in player_seed_sets:
        if s not in seed_sets:
            seed_sets.append(s)
    commit_set = sorted({c.lower() for c in commitments})
    panel_set = sorted({p.lower() for p in panel_hashes})

    base = Verification(round_id=round_id, status="incomplete", verified=False, detail="",
                        recorded_cents=recorded_cents)
    conflicts = []
    if len(server_set) > 1:
        conflicts.append("server_seed")
    if len(seed_sets) > 1:
        conflicts.append("player_seeds")
    if len(commit_set) > 1:
        conflicts.append("commitment")
    if len(panel_set) > 1:
        conflicts.append("panel_hash")
    if conflicts:
        base.status = "conflict"
        base.detail = "conflicting evidence for: " + ",".join(conflicts)
        return base

    base.server_seed = server_set[0] if server_set else None
    base.player_seeds = list(seed_sets[0]) if seed_sets else []
    base.commitment = commit_set[0] if commit_set else None
    base.panel_hash = panel_set[0] if panel_set else None

    if base.server_seed and base.commitment:
        base.commitment_match = sha256_hex(base.server_seed) == base.commitment

    missing = []
    if recorded_cents is None:
        missing.append("recorded_result")
    if not base.server_seed:
        missing.append("server_seed")
    if len(base.player_seeds) != REQUIRED_PLAYER_SEEDS:
        missing.append(f"player_seeds(have {len(base.player_seeds)}, need 3)")
    if missing:
        base.status = "incomplete"
        base.detail = "missing: " + ", ".join(missing)
        if base.commitment_match is False:
            base.status = "mismatch"
            base.detail = "commitment != sha256(server_seed); " + base.detail
        return base

    digest, cents = multiplier_from_seeds(base.server_seed, base.player_seeds)
    base.computed_hash = digest
    base.computed_cents = cents
    if base.panel_hash:
        base.panel_hash_match = base.panel_hash == digest

    problems = []
    if cents != recorded_cents:
        problems.append(f"computed {cents/100:.2f}x != recorded {recorded_cents/100:.2f}x")
    if base.commitment_match is False:
        problems.append("commitment != sha256(server_seed)")
    if base.panel_hash_match is False:
        problems.append("panel round hash != sha512(seeds)")
    if problems:
        base.status = "mismatch"
        base.detail = "; ".join(problems)
        return base

    base.status = "verified"
    base.verified = True
    checks = ["multiplier reproduced"]
    if base.commitment_match:
        checks.append("commitment matched")
    if base.panel_hash_match:
        checks.append("round hash matched")
    base.detail = ", ".join(checks)
    return base
