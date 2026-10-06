"""TXT batch files: one immutable file per `batch_size` valid rounds.

Batch N contains insertion-order rounds with seq in [(N-1)*size+1, N*size],
sorted by round id inside the file. Each round includes ALL available canonical
data: all table columns, complete raw JSON, all associated fairness evidence,
verification, and prediction records. NO truncation of any fields.
Existing batch files are never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional


def _batch_path(store, batch_no: int) -> Path:
    size = store.batch_size
    first = (batch_no - 1) * size + 1
    last = batch_no * size
    return store.batches_dir / f"batch_{batch_no:05d}_rounds_{first:07d}-{last:07d}.txt"


def _format_value(val: Any) -> str:
    """Format a value for output, handling None and types."""
    if val is None:
        return "UNAVAILABLE"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val).replace("\t", " ").replace("\n", " ")


def _format_complete_json(obj: Any) -> str:
    """Format object as complete, readable JSON with no truncation."""
    if obj is None:
        return "UNAVAILABLE"
    if isinstance(obj, str):
        # If already JSON string, parse and re-serialize for consistency
        try:
            parsed = json.loads(obj)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            # Plain string, not JSON
            return obj
    # Object, convert to JSON
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def generate_due_batches(store) -> list[Path]:
    size = store.batch_size
    if size <= 0:
        return []
    total = store.round_count()
    complete = total // size
    created: list[Path] = []
    done = {r[0] for r in store.conn.execute("SELECT batch_no FROM batches")}
    for batch_no in range(1, complete + 1):
        if batch_no in done:
            continue
        first_seq = (batch_no - 1) * size + 1
        last_seq = batch_no * size
        
        # Fetch all canonical round data
        rounds_rows = store.conn.execute(
            "SELECT round_id, multiplier, cents, source, origin, first_seen_at, seq, "
            "corroborations, corroborating_sources, raw_json FROM rounds "
            "WHERE seq BETWEEN ? AND ? ORDER BY round_id", (first_seq, last_seq)).fetchall()
        if len(rounds_rows) != size:
            continue
        
        # Build batch with complete data for each round - NO TRUNCATION
        path = _batch_path(store, batch_no)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        lines = [
            f"# Aviator 52358 batch {batch_no} | rounds seq {first_seq}-{last_seq} | {size} valid rounds",
            f"# generated_at_utc {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "",
        ]
        
        for r in rounds_rows:
            round_id = r["round_id"]
            seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["first_seen_at"]))
            corr_src = r["corroborating_sources"] if r["corroborating_sources"] else "[]"
            
            # Round block header
            lines.append("ROUND")
            lines.append(f"  round_id: {round_id}")
            lines.append(f"  multiplier: {r['multiplier']:.2f}")
            lines.append(f"  cents: {r['cents']}")
            lines.append(f"  source: {r['source']}")
            lines.append(f"  origin: {r['origin']}")
            lines.append(f"  first_seen_at: {seen}")
            lines.append(f"  seq: {r['seq']}")
            lines.append(f"  corroborations: {r['corroborations']}")
            lines.append(f"  corroborating_sources: {corr_src}")
            
            # Raw JSON - COMPLETE, NO TRUNCATION
            if r["raw_json"]:
                lines.append("RAW_EVIDENCE:")
                json_content = _format_complete_json(r["raw_json"])
                for json_line in json_content.split("\n"):
                    lines.append(f"  {json_line}")
            else:
                lines.append("RAW_EVIDENCE: UNAVAILABLE")
            
            # Fetch associated fairness evidence (explicitly associated only)
            evidence = store.conn.execute(
                "SELECT kind, value, source, received_at, association FROM fairness_evidence "
                "WHERE round_id = ? AND association = 'explicit' ORDER BY received_at",
                (round_id,)).fetchall()
            
            if evidence:
                lines.append("FAIRNESS_EVIDENCE:")
                for i, ev in enumerate(evidence):
                    ev_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["received_at"]))
                    lines.append(f"  [{i}]")
                    lines.append(f"    kind: {ev['kind']}")
                    lines.append(f"    source: {ev['source']}")
                    lines.append(f"    received_at: {ev_time}")
                    lines.append(f"    association: {ev['association']}")
                    # COMPLETE VALUE - NO TRUNCATION
                    lines.append(f"    value: {_format_complete_json(ev['value'])}")
            else:
                lines.append("FAIRNESS_EVIDENCE: UNAVAILABLE")
            
            # Fairness verification if exists - COMPLETE, NO TRUNCATION
            verify = store.conn.execute(
                "SELECT status, verified, locked, detail FROM fairness_verification WHERE round_id = ?",
                (round_id,)).fetchone()
            
            if verify:
                lines.append("FAIRNESS_VERIFICATION:")
                lines.append(f"  status: {verify['status']}")
                lines.append(f"  verified: {verify['verified']}")
                lines.append(f"  locked: {verify['locked']}")
                if verify["detail"]:
                    lines.append(f"  detail: {_format_complete_json(verify['detail'])}")
                else:
                    lines.append(f"  detail: UNAVAILABLE")
            else:
                lines.append("FAIRNESS_VERIFICATION: UNAVAILABLE")
            
            # Prediction record if exists - COMPLETE, NO TRUNCATION
            pred = store.conn.execute(
                "SELECT model_version, feature_count, output_json, actual_cents, "
                "actual_multiplier, leak_flag FROM predictions WHERE round_id = ?",
                (round_id,)).fetchone()
            
            if pred:
                lines.append("PREDICTION:")
                lines.append(f"  model_version: {pred['model_version']}")
                lines.append(f"  feature_count: {pred['feature_count']}")
                lines.append(f"  leak_flag: {pred['leak_flag']}")
                if pred["actual_cents"] is not None:
                    lines.append(f"  actual_cents: {pred['actual_cents']}")
                    lines.append(f"  actual_multiplier: {pred['actual_multiplier']:.2f}")
                else:
                    lines.append(f"  actual_cents: UNAVAILABLE")
                    lines.append(f"  actual_multiplier: UNAVAILABLE")
                # COMPLETE OUTPUT JSON - NO TRUNCATION
                if pred["output_json"]:
                    lines.append(f"  output_json: {_format_complete_json(pred['output_json'])}")
                else:
                    lines.append(f"  output_json: UNAVAILABLE")
            else:
                lines.append("PREDICTION: UNAVAILABLE")
            
            lines.append("END_ROUND")
            lines.append("")
        
        data = ("\n".join(lines) + "\n").encode("utf-8")
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        store.conn.execute("INSERT OR IGNORE INTO batches(batch_no,first_seq,last_seq,path,sha256,created_at)"
                           " VALUES(?,?,?,?,?,?)", (batch_no, first_seq, last_seq, path.name, sha, time.time()))
        created.append(path)
    return created
