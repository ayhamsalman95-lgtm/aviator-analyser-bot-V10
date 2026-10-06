"""Rotating JSONL evidence log with explicit loss observability."""
from __future__ import annotations

import json
import os
import re
import sys
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


PROVENANCE_KEYS = {
    "session_id", "collector_run_id", "event_id", "frame_id", "frame_index",
    "packet_index", "packet_offset", "packet_end", "source_file", "source_line",
}

def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key in PROVENANCE_KEYS:
                out[k] = redact(v)
            else:
                out[k] = "[redacted]" if SECRET_JSON_KEYS.search(key) else redact(v)
        return out
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
        self.write_failures = 0
        self.rotation_events = 0
        self.records_observed = 0
        self.records_persisted = 0
        self.records_failed = 0
        self.records_dropped = 0
        self.last_error: str | None = None

    def _rotate(self) -> dict:
        dropped_backup = None
        dropped_backup_size = None
        old_file_size = self.path.stat().st_size if self.path.exists() else None
        if self.backups > 0:
            dropped = self.path.with_name(f"{self.path.name}.{self.backups}")
            if dropped.exists():
                dropped_backup = str(dropped)
                dropped_backup_size = dropped.stat().st_size
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            dst = self.path.with_name(f"{self.path.name}.{i + 1}")
            if src.exists():
                os.replace(src, dst)
        if self.backups > 0:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink()
        self.rotation_events += 1
        return {
            "old_file": str(self.path),
            "old_file_size": old_file_size,
            "archived_file": str(self.path.with_name(f"{self.path.name}.1")) if self.backups > 0 else None,
            "active_file": str(self.path),
            "max_bytes": self.max_bytes,
            "backups": self.backups,
            "dropped_backup": dropped_backup,
            "dropped_backup_size": dropped_backup_size,
            "evidence_loss": bool(dropped_backup),
            "reason": "size_limit",
            "rotation_index": self.rotation_events,
        }

    def _write_line(self, line: str) -> bool:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            self.write_failures += 1
            self.records_failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[EVIDENCE-WRITE-FAILURE] {self.path}: {self.last_error}", file=sys.stderr, flush=True)
            return False

    def write(self, record: dict) -> bool:
        self.records_observed += 1
        try:
            rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), **redact(record)}
            line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            rotation = None
            if self.path.exists() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                rotation = self._rotate()
            if rotation is not None:
                event = {"kind": "network_log_rotation", "timestamp": time.time(),
                         "timestamp_provenance": "log_time_only",
                         "session_id": record.get("session_id"),
                         "collector_run_id": record.get("collector_run_id"), **rotation}
                rotation_line = json.dumps(
                    {"t": time.strftime("%Y-%m-%d %H:%M:%S"), **event},
                    ensure_ascii=False, default=str) + "\n"
                if not self._write_line(rotation_line):
                    self.records_failed += 1
                    return False
                self.records_persisted += 1
            ok = self._write_line(line)
            if ok:
                self.records_persisted += 1
            else:
                self.records_failed += 1
            return ok
        except Exception as exc:
            self.write_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[EVIDENCE-WRITE-FAILURE] {self.path}: {self.last_error}", file=sys.stderr, flush=True)
            return False
