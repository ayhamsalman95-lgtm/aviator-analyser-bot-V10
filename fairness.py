"""Provably-fair helpers for Spribe Aviator and HMAC-style crash games.

These functions verify a round AFTER the server seed is revealed.
They cannot recover a hidden server seed from its hash.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Optional


HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha512_hex(text: str) -> str:
    return hashlib.sha512(text.encode("utf-8")).hexdigest()


def _floor2(value: float) -> float:
    return float(int(value * 100.0) / 100.0)


def crash_from_digest_hex(digest_hex: str, rtp: float = 0.97) -> float:
    """Map a hex digest to a crash multiplier (52-bit / 3% house edge)."""
    if not digest_hex:
        return 1.0
    hex13 = digest_hex[:13]
    try:
        h = int(hex13, 16)
    except ValueError:
        return 1.0
    e = 2 ** 52
    if h >= e:
        return 1.0
    raw = (rtp * e) / (e - h)
    return max(1.0, _floor2(raw))


def spribe_combined(server_seed: str, client_seeds: list[str], nonce: Optional[str] = None) -> str:
    parts = [server_seed] + [c for c in client_seeds if c]
    if nonce not in (None, ""):
        parts.append(str(nonce))
    return "".join(parts)


def spribe_crash(server_seed: str, client_seeds: list[str], nonce: Optional[str] = None) -> tuple[str, float]:
    combined = spribe_combined(server_seed, client_seeds, nonce)
    digest = sha512_hex(combined)
    return digest, crash_from_digest_hex(digest)


def hmac_crash(server_seed: str, client_seed: str, nonce: Optional[str] = None) -> tuple[str, float]:
    message = client_seed if nonce in (None, "") else f"{client_seed}:{nonce}"
    digest = hmac.new(server_seed.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest, crash_from_digest_hex(digest)


@dataclass
class VerifyResult:
    ok: bool
    scheme: str
    expected_multiplier: Optional[float]
    hash_match: Optional[bool]
    combined_hash: Optional[str] = None
    detail: str = ""
    tried: list[str] = field(default_factory=list)


def hash_matches(server_seed: str, seed_hash: str) -> Optional[bool]:
    if not server_seed or not seed_hash:
        return None
    want = re.sub(r"[^0-9a-fA-F]", "", seed_hash).lower()
    got256 = sha256_hex(server_seed).lower()
    got512 = sha512_hex(server_seed).lower()
    if want == got256 or want == got256[: len(want)]:
        return True
    if want == got512 or want == got512[: len(want)]:
        return True
    return False


def verify_round(
    multiplier: Optional[float],
    server_seed: str = "",
    client_seeds: Optional[list[str]] = None,
    seed_hash: str = "",
    nonce: Optional[str] = None,
) -> VerifyResult:
    clients = [c for c in (client_seeds or []) if c]
    tried: list[str] = []

    hmatch = hash_matches(server_seed, seed_hash)
    if server_seed and seed_hash:
        tried.append("sha256(server_seed) vs seed_hash")

    if not server_seed:
        return VerifyResult(
            ok=False,
            scheme="none",
            expected_multiplier=None,
            hash_match=hmatch,
            detail="بذرة الخادم لم تُكشف بعد. لا يمكن التحقق قبل الكشف.",
            tried=tried,
        )

    schemes: list[tuple[str, str, float]] = []
    if len(clients) >= 1:
        digest, crash = spribe_crash(server_seed, clients[:3], None)
        schemes.append(("spribe-sha512-concat", digest, crash))
        digest, crash = spribe_crash(server_seed, clients[:3], nonce)
        schemes.append(("spribe-sha512-concat-nonce", digest, crash))
        digest, crash = hmac_crash(server_seed, clients[0], nonce)
        schemes.append(("hmac-sha256-client-nonce", digest, crash))
        joined = "|".join([server_seed] + clients[:3] + ([str(nonce)] if nonce else []))
        digest = sha512_hex(joined)
        schemes.append(("spribe-sha512-pipes", digest, crash_from_digest_hex(digest)))

    best: Optional[tuple[str, str, float]] = None
    if multiplier is not None and schemes:
        for name, digest, crash in schemes:
            tried.append(f"{name} -> {crash:.2f}x")
            if abs(crash - float(multiplier)) < 0.015:
                best = (name, digest, crash)
                break
        if best is None:
            best = schemes[0]
    elif schemes:
        best = schemes[0]
        tried.append(best[0])

    if best:
        name, digest, crash = best
        ok = True
        if multiplier is not None and abs(crash - float(multiplier)) >= 0.015:
            ok = False
        if hmatch is False:
            ok = False
        detail = (
            "النتيجة تطابق البذور."
            if ok and (multiplier is None or abs(crash - float(multiplier)) < 0.015)
            else "البذور لا تنتج نفس المضاعف المعروض — تحقق من النسخ أو المخطط."
        )
        if hmatch is False:
            detail = "هاش بذرة الخادم لا يطابق البذرة المكشوفة."
        return VerifyResult(
            ok=ok and (hmatch is not False),
            scheme=name,
            expected_multiplier=crash,
            hash_match=hmatch,
            combined_hash=digest,
            detail=detail,
            tried=tried,
        )

    if hmatch is True:
        return VerifyResult(
            ok=True,
            scheme="server-hash-only",
            expected_multiplier=None,
            hash_match=True,
            detail="هاش بذرة الخادم مطابق. بذور العميل غير مكتملة لإعادة حساب المضاعف.",
            tried=tried,
        )

    return VerifyResult(
        ok=False,
        scheme="none",
        expected_multiplier=None,
        hash_match=hmatch,
        detail="لا توجد بذور كافية للتحقق.",
        tried=tried,
    )


def looks_like_seed_value(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return False
    if not isinstance(value, str):
        return False
    s = value.strip()
    if len(s) < 8 or len(s) > 256:
        return False
    if " " in s or "\n" in s:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-.:]+", s):
        return False
    if re.fullmatch(r"https?://.*", s, re.I):
        return False
    return True
