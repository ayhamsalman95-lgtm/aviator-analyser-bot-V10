"""Local-only launch URL validation and secret-safe diagnostics."""
from __future__ import annotations

import math
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_LAUNCH_DELAY_SECONDS = 20.0
MAX_LAUNCH_DELAY_SECONDS = 300.0
ALLOWED_LAUNCH_HOST = "launch.spribegaming.com"
_SECRET_KEYS = re.compile(
    r"(?i)^(token|access_token|refresh_token|authorization|auth|password|pass|secret|signature|sig|"
    r"cookie|session|sid|sessionid|jwt|key|apikey|api_key|user|user_id|userid|return_url)$"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|authorization|auth|password|pass|secret|signature|sig|"
    r"cookie|session|sid|sessionid|jwt|key|apikey|api_key|user|user_id|userid|return_url)=)[^&#\s]*"
)
_LAUNCH_URL = re.compile(r"https?://launch\.spribegaming\.com/[^\s\"'<>]+")


def validate_launch_url(raw_url: str) -> str:
    """Validate the direct Spribe launch URL without logging or rewriting its secret query."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("AVIATOR_GAME_URL must contain a direct Spribe launch URL")
    value = raw_url.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except Exception as exc:
        raise ValueError("AVIATOR_GAME_URL is malformed") from exc
    if parsed.scheme.lower() != "https" or host != ALLOWED_LAUNCH_HOST:
        raise ValueError("AVIATOR_GAME_URL must use HTTPS on launch.spribegaming.com")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("AVIATOR_GAME_URL must not contain URL credentials or a fragment")
    return value


def launch_delay_seconds(value: str | float | int | None = None) -> float:
    """Read the post-operator-game delay; default to 20 seconds and reject unsafe values."""
    raw = os.environ.get("AVIATOR_LAUNCH_DELAY_SECONDS") if value is None else value
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LAUNCH_DELAY_SECONDS
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AVIATOR_LAUNCH_DELAY_SECONDS must be a number from 0 to 300") from exc
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_LAUNCH_DELAY_SECONDS:
        raise ValueError("AVIATOR_LAUNCH_DELAY_SECONDS must be a number from 0 to 300")
    return seconds


def redact_sensitive_text(value: object) -> str:
    """Redact credential-bearing query values from exception and diagnostic text."""
    text = str(value)
    text = _QUERY_SECRET.sub(r"\1[redacted]", text)

    def clean_launch(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            query = [
                (key, "[redacted]" if _SECRET_KEYS.match(key) else val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        except Exception:
            return raw.split("?", 1)[0]

    return _LAUNCH_URL.sub(clean_launch, text)
