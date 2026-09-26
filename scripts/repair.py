"""Integrity check + repair (never deletes evidence).

* SQLite integrity_check
* rounds.jsonl reconciliation: rounds present in SQLite but missing from the JSONL
  are APPENDED (marked repair=true); nothing is rewritten.
* missing TXT batches are generated
* fairness verification recomputed for rounds with explicit evidence (locked rows kept)
* frozen predictions whose round exists are resolved
"""
from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401

from aviator.batches import generate_due_batches
from aviator.config import load_config
from aviator.db import Store
from aviator.jsonl import append_jsonl, read_jsonl


def repair(store: Store) -> dict:
    rep = {"integrity": store.conn.execute("PRAGMA integrity_check").fetchone()[0],
           "journal_mode": store.journal_mode()}
    in_jsonl = {int(r["round_id"]) for r in read_jsonl(store.rounds_jsonl) if "round_id" in r}
    appended = 0
    for r in store.rounds_chronological():
        if int(r["round_id"]) not in in_jsonl:
            append_jsonl(store.rounds_jsonl, {"round_id": r["round_id"], "multiplier": r["multiplier"],
                                               "cents": r["cents"], "source": r["source"], "origin": r["origin"],
                                               "seq": r["seq"], "first_seen_at": r["first_seen_at"],
                                               "repair": True})
            appended += 1
    rep["jsonl_appended"] = appended
    rep["batches_created"] = [p.name for p in generate_due_batches(store)]
    ids = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT round_id FROM fairness_evidence WHERE association='explicit'")]
    for rid in ids:
        store.reverify(rid)
    rep["reverified"] = len(ids)
    pending = [r[0] for r in store.conn.execute("SELECT round_id FROM predictions WHERE resolved_at IS NULL")]
    rep["predictions_resolved"] = sum(1 for rid in pending if store.resolve_prediction(rid))
    return rep


def main() -> int:
    store = Store.from_config(load_config())
    print(json.dumps(repair(store), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
