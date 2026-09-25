"""Persistent round + live-state store used by collector and the Telegram bot."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
HISTORY = BASE / CONFIG.get("history_file", "history.json")
ROUNDS = BASE / CONFIG.get("rounds_file", "rounds.json")
LIVE = BASE / "live_state.json"
STATUS = BASE / "collector_status.json"
SEEDS_LOG = BASE / "seeds.jsonl"
DIAGNOSTICS_LOG = BASE / "diagnostics.jsonl"
MAX_HISTORY = int(CONFIG.get("max_history", 10000))


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history() -> list[float]:
    raw = _read_json(HISTORY, [])
    out = []
    for x in raw:
        try:
            v = float(x)
            if v >= 1.0:
                out.append(v)
        except (TypeError, ValueError):
            pass
    return out[-MAX_HISTORY:]


def save_history(values: list[float]) -> None:
    _write_json(HISTORY, [round(v, 4) for v in values[-MAX_HISTORY:]])


def load_rounds() -> list[dict]:
    raw = _read_json(ROUNDS, [])
    if not isinstance(raw, list):
        return []
    return raw[-MAX_HISTORY:]


def save_rounds(rows: list[dict]) -> None:
    _write_json(ROUNDS, rows[-MAX_HISTORY:])


def upsert_round(record: dict) -> dict:
    rows = load_rounds()
    rid = str(record.get("id") or "")
    idx = next((i for i, r in enumerate(rows) if str(r.get("id")) == rid and rid), None)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    record = dict(record)
    record["updated_at"] = now
    if idx is None:
        record.setdefault("captured_at", now)
        rows.append(record)
    else:
        merged = dict(rows[idx])
        for key, value in record.items():
            if key == "client_seeds":
                seeds = list(merged.get("client_seeds") or [])
                for item in value or []:
                    if item and item not in seeds:
                        seeds.append(item)
                merged["client_seeds"] = seeds[:4]
            elif value not in (None, "", [], {}):
                merged[key] = value
        merged["updated_at"] = now
        rows[idx] = merged
        record = merged
    save_rounds(rows)
    multiplier = record.get("multiplier")
    if isinstance(multiplier, (int, float)) and multiplier >= 1.0:
        history = load_history()
        last_id = None
        if rows:
            # Rebuild history from rounds when possible so we don't duplicate.
            rebuilt = []
            for row in rows:
                m = row.get("multiplier")
                try:
                    mv = float(m)
                except (TypeError, ValueError):
                    continue
                if mv >= 1.0:
                    rebuilt.append(mv)
            if rebuilt:
                history = rebuilt
            elif not history or abs(history[-1] - float(multiplier)) > 1e-6:
                history.append(float(multiplier))
        save_history(history)
    return record


def load_live() -> dict:
    return _read_json(LIVE, {"phase": "waiting", "detail": "لم تبدأ الجولة بعد."})


def save_live(state: dict) -> None:
    state = dict(state)
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(LIVE, state)


def set_status(status: str, detail: str = "", extra: dict | None = None) -> None:
    data = {
        "status": status,
        "detail": detail,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": CONFIG.get("game_url"),
        "rounds": len(load_rounds()),
        "history": len(load_history()),
    }
    if extra:
        data.update(extra)
    _write_json(STATUS, data)


def append_seed_log(record: dict) -> None:
    try:
        with SEEDS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def append_diagnostic(record: dict) -> None:
    try:
        with DIAGNOSTICS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def last_round() -> dict | None:
    rows = load_rounds()
    return rows[-1] if rows else None


# AVIATOR_V10_TELEGRAM_DEDUPE
# Persistent de-duplication for Telegram alerts. This is intentionally
# separate from round storage so repeated send paths cannot create duplicate
# messages for the same round/message body.
_TELEGRAM_SENT_FILE = BASE / "telegram_sent.json"


def _load_telegram_sent() -> set[str]:
    try:
        if not _TELEGRAM_SENT_FILE.exists():
            return set()
        import json as _json
        data = _json.loads(_TELEGRAM_SENT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            values = data.get("keys") or []
            return {str(x) for x in values}
    except Exception:
        pass
    return set()


def telegram_alert_already_sent(key: str) -> bool:
    return str(key) in _load_telegram_sent()


def mark_telegram_alert_sent(key: str) -> None:
    key = str(key)
    try:
        values = _load_telegram_sent()
        values.add(key)
        # Keep this bounded; old alerts are not useful forever.
        values = set(sorted(values)[-5000:])
        import json as _json
        _TELEGRAM_SENT_FILE.write_text(
            _json.dumps(sorted(values), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        try:
            print(f"[TELEGRAM] dedupe-store warning: {exc}", flush=True)
        except Exception:
            pass
