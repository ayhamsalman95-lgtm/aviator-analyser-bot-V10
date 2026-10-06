"""Migrate V8-V11 runtime files into canonical SQLite + JSONL storage.

Canonical rule:  source == "sfs:roundChartInfo"  AND  numeric round id  AND  valid multiplier.
Everything else (e.g. source="websocket" junk with hex ids) is quarantined verbatim.
Originals are copied to data/legacy_evidence/<stamp>/ with a SHA-256 manifest and
are never modified or deleted. Re-running is idempotent.

Usage:  python scripts/migrate_legacy.py [--legacy-dir PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from aviator.config import load_config
from aviator.db import Store
from aviator.extract import extract_fairness  # noqa: F401  (kept for parity/documentation)

LEGACY_FILES = ["rounds.json", "history.json", "seeds.jsonl", "live_state.json", "collector_status.json",
                "telegram_sent.json", "subscribers.json", "diagnostics.jsonl", "telegram_errors.log",
                "game_network.jsonl", "config.json"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preserve(legacy_dir: Path, dest_root: Path) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = dest_root / stamp
    manifest = {"created_at": stamp, "source_dir": str(legacy_dir), "files": {}}
    for name in LEGACY_FILES:
        src = legacy_dir / name
        if not src.exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        if name == "config.json":
            raw = json.loads(src.read_text(encoding="utf-8"))
            if "bot_token" in raw:
                raw["bot_token"] = "[removed during migration]"
            target.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            shutil.copy2(src, target)
        manifest["files"][name] = {"sha256_original": sha256_file(src), "bytes": src.stat().st_size}
    if manifest["files"]:
        (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def classify(rec) -> str | None:
    """Return None if canonical, else the quarantine reason."""
    if not isinstance(rec, dict):
        return "not_an_object"
    if rec.get("source") != "sfs:roundChartInfo":
        return f"non_canonical_source:{rec.get('source')}"
    return None


def migrate(store: Store, legacy_dir: Path, evidence_root: Path) -> dict:
    summary = {"legacy_dir": str(legacy_dir), "rounds_seen": 0, "inserted": 0, "duplicates": 0,
               "quarantined": {}, "legacy_seed_evidence": 0, "subscribers": 0}
    summary["preserved"] = preserve(legacy_dir, evidence_root)
    rounds_path = legacy_dir / "rounds.json"
    records = []
    if rounds_path.exists():
        try:
            records = json.loads(rounds_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            summary["rounds_error"] = f"rounds.json unreadable: {exc}"
            records = []
    if not isinstance(records, list):
        records = []
    for rec in records:
        summary["rounds_seen"] += 1
        reason = classify(rec)
        if reason:
            store.quarantine(f"migration:{reason}", rec, source=str(rec.get("source") if isinstance(rec, dict)
                                                                    else ""),
                             round_id_raw=rec.get("id") if isinstance(rec, dict) else None)
            summary["quarantined"][reason] = summary["quarantined"].get(reason, 0) + 1
            continue
        status = store.insert_round(rec.get("id"), rec.get("multiplier"), source="sfs:roundChartInfo",
                                    origin="migration", raw={"legacy_record": rec}, notify=False,
                                    seen_at=_legacy_time(rec))
        if status == "inserted":
            summary["inserted"] += 1
            rid = int(rec["id"])
            # Legacy seed fields were produced by the old heuristic binder -> never trusted.
            for kind, key in (("server_seed", "server_seed"), ("commitment_sha256", "seed_hash")):
                if rec.get(key):
                    summary["legacy_seed_evidence"] += store.add_fairness_evidence(
                        kind, rec[key], "migration:legacy", rid, "legacy_unproven")
            if rec.get("client_seeds"):
                summary["legacy_seed_evidence"] += store.add_fairness_evidence(
                    "player_seeds", list(rec["client_seeds"]), "migration:legacy", rid, "legacy_unproven")
        elif status == "duplicate":
            summary["duplicates"] += 1
        else:
            summary["quarantined"][status] = summary["quarantined"].get(status, 0) + 1
    subs = legacy_dir / "subscribers.json"
    if subs.exists():
        try:
            for chat_id in json.loads(subs.read_text(encoding="utf-8")):
                store.subscribe(int(chat_id))
                summary["subscribers"] += 1
        except Exception as exc:
            summary["subscribers_error"] = str(exc)
    hist = legacy_dir / "history.json"
    if hist.exists():
        try:
            summary["legacy_history_len_not_imported"] = len(json.loads(hist.read_text(encoding="utf-8")))
        except Exception:
            pass
    return summary


def _legacy_time(rec: dict):
    ts = rec.get("captured_at") or rec.get("updated_at")
    if not ts:
        return None
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-dir", default=None)
    args = ap.parse_args(argv)
    cfg = load_config()
    legacy_dir = Path(args.legacy_dir) if args.legacy_dir else (
        cfg.root / "legacy" if (cfg.root / "legacy" / "rounds.json").exists() else cfg.root)
    store = Store.from_config(cfg)
    summary = migrate(store, legacy_dir, cfg.data_dir / "legacy_evidence")
    out = cfg.data_dir / "migration_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "preserved"}, ensure_ascii=False, indent=2))
    print(f"summary: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
