"""Seed/Provably-Fair extraction helpers for the real Aviator page.

The extractor is deliberately conservative: it only labels a value as a seed
when the key/label gives enough semantic context. Raw hex strings are kept as
separate evidence and are not silently treated as server/client seeds.
"""
from __future__ import annotations

import html
import re
from typing import Any

from fairness import looks_like_seed_value

HEX64 = re.compile(r"\b[a-fA-F0-9]{64}\b")
HEX128 = re.compile(r"\b[a-fA-F0-9]{128}\b")
HEX32 = re.compile(r"\b[a-fA-F0-9]{32}\b")
GENERIC_VALUE = re.compile(r"[A-Za-z0-9+/=_\-.:]{8,256}")

SEED_KEY_EXACT = {
    "seedhash", "serverseedhash", "nextserverseedhash", "hashedserverseed",
    "serverseed", "nextserverseed", "revealedserverseed", "serverseedplain",
    "serverseedvalue", "clientseed", "clientseeds", "playerseed", "playerseeds",
    "clientseed1", "clientseed2", "clientseed3", "playerseed1", "playerseed2",
    "playerseed3", "combinedhash", "combinedseed", "fairnesshash", "pfhash",
    "pfseed", "nonce", "roundhash", "gamehash", "serverseedsha256",
    "nextserverseedsha256", "sha256hash", "sha512hash", "seed_hash", "server_seed",
    "client_seed", "client_seeds", "server_seed_hash", "next_server_seed",
    "next_server_seed_hash", "combined_hash",
}

# Short keys are accepted only when the containing object is clearly related to
# provably-fair data, or when the caller explicitly opted in for a known event.
SHORT_SEED_KEYS = {
    "sh": "seed_hash",
    "ssh": "seed_hash",
    "ss": "server_seed",
    "cs": "client_seed",
    "c1": "client_seed",
    "c2": "client_seed",
    "c3": "client_seed",
    "nh": "seed_hash",
    "hash": "seed_hash",
    "seed": "server_seed",
    "nonce": "nonce",
}

NOISE_URL = (
    "dictionary", "translations", "genfiles", "font", "css", "analytics", "gtm",
    "facebook", "tiktok", "favicon", "woff", "png", "jpg", "webp", "svg",
)

LABEL_MAP = (
    ("next server seed", "seed_hash"),
    ("server seed hash", "seed_hash"),
    ("seed hash", "seed_hash"),
    ("hashed server seed", "seed_hash"),
    ("server seed", "server_seed"),
    ("client seed 1", "client_seed"),
    ("client seed 2", "client_seed"),
    ("client seed 3", "client_seed"),
    ("client seed", "client_seed"),
    ("player seed", "client_seed"),
    ("combined hash", "combined_hash"),
    ("nonce", "nonce"),
    ("round hash", "combined_hash"),
    ("game hash", "combined_hash"),
    ("هاش البذرة", "seed_hash"),
    ("بذرة الخادم التالية", "seed_hash"),
    ("بذرة الخادم", "server_seed"),
    ("بذرة العميل", "client_seed"),
)


def normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def noisy_url(url: str) -> bool:
    low = str(url).lower()
    return any(word in low for word in NOISE_URL)


def _as_seed_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if looks_like_seed_value(item):
                out.append(str(item).strip())
            elif isinstance(item, dict):
                for k in ("seed", "clientSeed", "client_seed", "value", "hash"):
                    if looks_like_seed_value(item.get(k)):
                        out.append(str(item.get(k)).strip())
    elif looks_like_seed_value(value):
        out.append(str(value).strip())
    return out


def classify_key(key: Any) -> str | None:
    norm = normalize_key(key)
    if norm in {
        "seedhash", "serverseedhash", "nextserverseedhash", "hashedserverseed",
        "serverseedsha256", "nextserverseedsha256", "sha256hash", "seed_hash",
        "server_seed_hash", "next_server_seed_hash",
    }:
        return "seed_hash"
    if norm in {
        "serverseed", "nextserverseed", "revealedserverseed", "serverseedplain",
        "serverseedvalue", "server_seed",
    }:
        return "server_seed"
    if norm in {
        "clientseed", "playerseed", "clientseed1", "clientseed2", "clientseed3",
        "playerseed1", "playerseed2", "playerseed3", "client_seed",
    }:
        return "client_seed"
    if norm in {"clientseeds", "playerseeds", "client_seeds"}:
        return "client_seeds"
    if norm in {"combinedhash", "combinedseed", "fairnesshash", "pfhash", "pfseed", "roundhash", "gamehash", "combined_hash"}:
        return "combined_hash"
    if norm == "nonce":
        return "nonce"
    if "serverseed" in norm and "hash" in norm:
        return "seed_hash"
    if "seedhash" in norm:
        return "seed_hash"
    if "clientseed" in norm or "playerseed" in norm:
        return "client_seed"
    if norm == "serverseed" or (norm.endswith("serverseed") and "hash" not in norm):
        return "server_seed"
    return None


def extract_seed_fields(value: Any, path: str = "root", short_ok: bool = False) -> dict:
    found = {
        "seed_hash": [],
        "server_seed": [],
        "client_seeds": [],
        "combined_hash": [],
        "nonce": [],
        "hits": [],
    }

    def add(kind: str, raw: Any, at: str, key: str):
        if kind == "client_seed":
            kind = "client_seeds"
        if kind == "nonce":
            text = str(raw).strip() if raw not in (None, "") else ""
            if not text or len(text) > 64:
                return
            values = [text]
        else:
            values = _as_seed_list(raw)
        bucket = found[kind]
        for item in values:
            if item not in bucket:
                bucket.append(item)
                found["hits"].append({"kind": kind, "key": key, "path": at, "value": item})

    def walk(node: Any, at: str, allow_short: bool, fair_context: bool):
        if isinstance(node, dict):
            key_norms = {normalize_key(k) for k in node.keys()}
            direct_fair = bool(
                key_norms & {
                    "seedhash", "serverseed", "clientseed", "clientseeds", "playerseed",
                    "playerseeds", "combinedhash", "fairnesshash", "provablyfair", "nonce",
                    "pfhash", "pfseed", "serverseedhash", "serverseedplain", "roundhash",
                    "gamehash",
                }
            )
            child_fair = fair_context or direct_fair
            for key, child in node.items():
                kind = classify_key(key)
                child_path = f"{at}.{key}"
                if kind:
                    add(kind, child, child_path, str(key))
                elif (allow_short or child_fair) and normalize_key(key) in SHORT_SEED_KEYS:
                    mapped = SHORT_SEED_KEYS[normalize_key(key)]
                    if mapped == "nonce":
                        add(mapped, child, child_path, str(key))
                    elif looks_like_seed_value(child):
                        add(mapped, child, child_path, str(key))
                walk(child, child_path, allow_short or child_fair, child_fair)
        elif isinstance(node, list):
            for idx, child in enumerate(node):
                walk(child, f"{at}[{idx}]", allow_short, fair_context)

    walk(value, path, short_ok, False)
    return found


def extract_hex_from_text(text: str) -> dict:
    raw = text or ""
    return {
        "hex128": HEX128.findall(raw)[:12],
        "hex64": HEX64.findall(raw)[:16],
        "hex32": HEX32.findall(raw)[:16],
    }


def parse_labeled_text(text: str) -> dict:
    """Parse explicit labels such as 'Seed Hash: abc...' from rendered text/HTML."""
    found = {
        "seed_hash": [],
        "server_seed": [],
        "client_seeds": [],
        "combined_hash": [],
        "nonce": [],
    }
    if not text:
        return found
    compact = html.unescape(str(text)).replace("\u00a0", " ")
    compact = re.sub(r"[ \t]+", " ", compact)
    lower = compact.lower()

    def add(kind: str, value: str):
        value = value.strip().strip("`'\"<>()[]{}")
        if kind == "client_seed":
            kind = "client_seeds"
        if not value or len(value) > 256:
            return
        if kind != "nonce" and not looks_like_seed_value(value):
            return
        if value not in found[kind]:
            found[kind].append(value)

    for label, kind in LABEL_MAP:
        needle = label.lower()
        start = 0
        while True:
            pos = lower.find(needle, start)
            if pos < 0:
                break
            after = compact[pos + len(label): pos + len(label) + 350]
            # Prefer the text after ':', '=', or a line break. For HTML snippets
            # this also catches quoted attribute values immediately after labels.
            m = re.search(
                r"(?:[:：=]|\n|\r)\s*([A-Za-z0-9+/=_\-.:]{1,256})",
                after,
            )
            if m:
                add(kind, m.group(1))
            else:
                for candidate in GENERIC_VALUE.findall(after):
                    if candidate.lower() not in {label.lower(), "value", "seed", "hash", "nonce"}:
                        add(kind, candidate)
                        break
            start = pos + len(label)
    return found


def parse_html_attributes(html_text: str) -> dict:
    """Extract values of data-/aria-/meta/input attributes mentioning fairness/seed/hash."""
    found = {
        "seed_hash": [],
        "server_seed": [],
        "client_seeds": [],
        "combined_hash": [],
        "nonce": [],
    }
    if not html_text:
        return found
    raw = html.unescape(html_text)
    # data-seed-hash="...", name="serverSeed" content="...", value="..." etc.
    attr_rx = re.compile(
        r"(?is)(?:data-|aria-)?([a-z0-9_:-]*(?:seed|hash|nonce|fair)[a-z0-9_:-]*)\s*=\s*([\"'])(.*?)\2"
    )
    for match in attr_rx.finditer(raw):
        attr = normalize_key(match.group(1))
        value = match.group(3).strip()
        kind = classify_key(attr)
        if not kind:
            if "serverseed" in attr and "hash" in attr:
                kind = "seed_hash"
            elif "serverseed" in attr:
                kind = "server_seed"
            elif "clientseed" in attr or "playerseed" in attr:
                kind = "client_seed"
            elif "hash" in attr and "seed" in attr:
                kind = "seed_hash"
            elif "nonce" in attr:
                kind = "nonce"
        if kind == "client_seed":
            kind = "client_seeds"
        if kind in found:
            if kind == "nonce":
                if 1 <= len(value) <= 64 and value not in found[kind]:
                    found[kind].append(value)
            elif looks_like_seed_value(value) and value not in found[kind]:
                found[kind].append(value)
    return found


def merge_seed_state(dst: dict, src: dict) -> dict:
    out = dict(dst or {})
    for key in ("seed_hash", "server_seed", "combined_hash", "nonce"):
        incoming = src.get(key)
        if isinstance(incoming, list):
            incoming = incoming[0] if incoming else None
        if incoming and not out.get(key):
            out[key] = incoming
        elif incoming and key == "server_seed" and out.get(key) != incoming:
            # A later revealed server seed supersedes an earlier placeholder.
            out[key] = incoming
    clients = list(out.get("client_seeds") or [])
    extra = src.get("client_seeds") or src.get("client_seed") or []
    if isinstance(extra, str):
        extra = [extra]
    for item in extra:
        if item and item not in clients:
            clients.append(item)
    out["client_seeds"] = clients[:4]
    return out
