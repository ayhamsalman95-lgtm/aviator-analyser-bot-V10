"""Rotating, redacted JSONL network log."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_QUERY_KEYS = re.compile(
    r"(?i)^(token|access_token|refresh_token|authorization|auth|password|pass|secret|signature|sig|"
    r"cookie|session|sid|sessionid|jwt|key|apikey|api_key)$")
SECRET_JSON_KEYS = re.compile(
    r"(?i)(password|passwd|token|authorization|cookie|set-cookie|secret|session|jwt|apikey|api_key)")


def safe_url(raw_url: str) -> str:
    try:
        p = urlsplit(str(raw_url))
        q = [(k, "[redacted]" if SECRET_QUERY_KEYS.match(k) else v)
             for k, v in parse_qsl(p.query, keep_blank_values=True)]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))
    except Exception:
        return str(raw_url).split("?", 1)[0]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if SECRET_JSON_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"_binary_len": len(value)}
    return value


class RotatingJsonlLog:
    def __init__(self, path: Path, max_bytes: int = 20_000_000, backups: int = 5):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate(self) -> None:
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            dst = self.path.with_name(f"{self.path.name}.{i + 1}")
            if src.exists():
                os.replace(src, dst)
        if self.backups > 0:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink()

    def write(self, record: dict) -> None:
        try:
            rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), **redact(record)}
            line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            if self.path.exists() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                self._rotate()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass  # logging must never break collection
