"""Canonical storage: SQLite (WAL) + append-only JSONL mirrors.

Tables
  rounds                 one row per valid completed round (round_id PRIMARY KEY)
  quarantine             rejected / junk / conflicting records (original kept verbatim)
  fairness_evidence      append-only provably-fair evidence with explicit association
  fairness_verification  derived verification status per round (verified rows are locked)
  predictions            frozen pre-round predictions + separately attached outcomes
  manual_entries         /add values -- never mixed with canonical research data
  subscribers, outbox, deliveries   Telegram decoupling (collector never talks to Telegram)
  batches, meta
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .fairness import verify_round
from .jsonl import append_jsonl
from .validation import RoundValidationError, validate_round

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rounds (
    round_id INTEGER PRIMARY KEY,
    multiplier REAL NOT NULL,
    cents INTEGER NOT NULL,
    source TEXT NOT NULL,
    origin TEXT NOT NULL,              -- live | backfill | migration
    seq INTEGER NOT NULL UNIQUE,       -- insertion order (batches use it)
    first_seen_at REAL NOT NULL,       -- unix time the result became known to us
    corroborations INTEGER NOT NULL DEFAULT 0,
    corroborating_sources TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    source TEXT,
    round_id_raw TEXT,
    record_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(record_sha256, reason)
);
CREATE TABLE IF NOT EXISTS fairness_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER,                  -- NULL unless the association is proven
    association TEXT NOT NULL,         -- explicit | unassociated | legacy_unproven
    kind TEXT NOT NULL,                -- server_seed | player_seeds | commitment_sha256 | round_hash_sha512
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    context_round_id INTEGER,          -- diagnostic only, never used for association
    received_at REAL NOT NULL,
    evidence_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_fe_round ON fairness_evidence(round_id, association);
CREATE TABLE IF NOT EXISTS evidence_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER,
    association TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    field_name TEXT,
    value TEXT NOT NULL,
    algorithm TEXT,
    semantic_type TEXT,
    source TEXT NOT NULL,
    context_round_id INTEGER,
    received_at REAL,
    received_monotonic REAL,
    timestamp_provenance TEXT,
    session_id TEXT,
    collector_run_id TEXT,
    event_id TEXT,
    source_file TEXT,
    source_line INTEGER,
    frame_id TEXT,
    frame_index INTEGER,
    packet_index INTEGER,
    packet_offset INTEGER,
    packet_end INTEGER,
    raw_evidence_ref TEXT,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eo_round ON evidence_observations(round_id, association);
CREATE INDEX IF NOT EXISTS ix_eo_frame ON evidence_observations(frame_id, packet_index);
CREATE TABLE IF NOT EXISTS fairness_verification (
    round_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    verified INTEGER NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    result_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    round_id INTEGER PRIMARY KEY,
    frozen_at REAL NOT NULL,
    trigger TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_count INTEGER NOT NULL,
    feature_max_round_id INTEGER,
    feature_max_seq INTEGER,
    feature_hash TEXT NOT NULL,
    output_json TEXT NOT NULL,
    actual_cents INTEGER,
    actual_multiplier REAL,
    resolved_at REAL,
    leak_flag INTEGER NOT NULL DEFAULT 0,
    leak_detail TEXT
);
CREATE TABLE IF NOT EXISTS manual_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    multiplier REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    round_id INTEGER,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    outbox_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,              -- sent | failed | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (outbox_id, chat_id)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_no INTEGER PRIMARY KEY,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path: Path, structured_dir: Path, batches_dir: Path,
                 batch_size: int = 1000, clock=time.time, max_multiplier: float = 1_000_000.0):
        self.db_path = Path(db_path)
        self.structured_dir = Path(structured_dir)
        self.batches_dir = Path(batches_dir)
        self.batch_size = int(batch_size)
        self.clock = clock
        self.max_multiplier = max_multiplier
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        meta_exists = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        previous = self.get_meta("schema_version") if meta_exists else None
        try:
            previous_version = int(previous) if previous is not None else 0
        except (TypeError, ValueError):
            previous_version = 0
        self.conn.executescript(SCHEMA)
        self._migrate_schema(previous_version)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _table_columns(self, table: str) -> set[str]:
        rows = self.conn.execute("PRAGMA table_info(" + table + ")").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_schema(self, previous_version: int) -> None:
        """Apply additive, data-preserving migrations explicitly."""
        if previous_version >= SCHEMA_VERSION:
            return
        if previous_version < 3:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS evidence_observations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER, association TEXT NOT NULL, "
                "evidence_kind TEXT NOT NULL, field_name TEXT, value TEXT NOT NULL, algorithm TEXT, "
                "semantic_type TEXT, source TEXT NOT NULL, context_round_id INTEGER, received_at REAL, "
                "received_monotonic REAL, timestamp_provenance TEXT, session_id TEXT, collector_run_id TEXT, "
                "event_id TEXT, source_file TEXT, source_line INTEGER, frame_id TEXT, frame_index INTEGER, "
                "packet_index INTEGER, packet_offset INTEGER, packet_end INTEGER, raw_evidence_ref TEXT, observed_at REAL NOT NULL)"
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS ix_eo_round ON evidence_observations(round_id, association)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS ix_eo_frame ON evidence_observations(frame_id, packet_index)")
            if "raw_evidence_ref" not in self._table_columns("evidence_observations"):
                self.conn.execute("ALTER TABLE evidence_observations ADD COLUMN raw_evidence_ref TEXT")
            fe_cols = self._table_columns("fairness_evidence")
            if "association" not in fe_cols:
                self.conn.execute("ALTER TABLE fairness_evidence ADD COLUMN association TEXT NOT NULL DEFAULT 'legacy_unproven'")
            if "context_round_id" not in fe_cols:
                self.conn.execute("ALTER TABLE fairness_evidence ADD COLUMN context_round_id INTEGER")
            if "evidence_key" not in fe_cols:
                self.conn.execute("ALTER TABLE fairness_evidence ADD COLUMN evidence_key TEXT")
            rows = self.conn.execute("SELECT id,round_id,association,kind,value,context_round_id,evidence_key,source,received_at FROM fairness_evidence").fetchall()
            for row in rows:
                key = row["evidence_key"]
                if key is None:
                    key = _sha256_json([row["round_id"], row["association"], row["kind"], row["value"], row["context_round_id"] if row["association"] == "unassociated" else None])
                    self.conn.execute("UPDATE fairness_evidence SET evidence_key=? WHERE id=?", (key, row["id"]))
                self.conn.execute(
                    "INSERT INTO evidence_observations(round_id,association,evidence_kind,field_name,value,source,context_round_id,received_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["round_id"], row["association"], row["kind"], row["kind"], row["value"], row["source"], row["context_round_id"], row["received_at"], row["received_at"]),
                )
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fe_evidence_key ON fairness_evidence(evidence_key)")
        self.conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)", (SCHEMA_VERSION, self.clock()))

    @classmethod
    def from_config(cls, cfg) -> "Store":
        return cls(cfg.db_path, cfg.structured_dir, cfg.batches_dir,
                   batch_size=int(cfg["batch_size"]), max_multiplier=float(cfg["max_multiplier"]))

    # ------------------------------------------------------------------ paths
    @property
    def rounds_jsonl(self) -> Path:
        return self.structured_dir / "rounds.jsonl"

    @property
    def predictions_jsonl(self) -> Path:
        return self.structured_dir / "predictions.jsonl"

    @property
    def fairness_jsonl(self) -> Path:
        return self.structured_dir / "fairness_evidence.jsonl"

    @property
    def quarantine_jsonl(self) -> Path:
        return self.structured_dir.parent / "quarantine" / "quarantine.jsonl"

    def close(self) -> None:
        self.conn.close()

    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode").fetchone()[0]

    # ------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------- quarantine
    def quarantine(self, reason: str, record: Any, source: str = "", round_id_raw: Any = None) -> bool:
        rec_json = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        sha = hashlib.sha256(rec_json.encode("utf-8")).hexdigest()
        now = self.clock()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO quarantine(reason,source,round_id_raw,record_sha256,record_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (reason, source, None if round_id_raw is None else str(round_id_raw), sha, rec_json, now))
        if cur.rowcount:
            append_jsonl(self.quarantine_jsonl, {"reason": reason, "source": source,
                                                  "round_id_raw": round_id_raw, "record_sha256": sha,
                                                  "record": record, "quarantined_at": now})
            return True
        return False

    def quarantine_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT reason, COUNT(*) FROM quarantine GROUP BY reason ORDER BY reason")}

    # ----------------------------------------------------------------- rounds
    def insert_round(self, round_id: Any, multiplier: Any, source: str, origin: str = "live",
                     raw: Any = None, notify: bool = True, seen_at: Optional[float] = None,
                     provenance: Optional[dict[str, Any]] = None) -> str:
        """Persist a completed round immediately.

        Returns 'inserted', 'duplicate' or one of 'invalid:<reason>' / 'conflict'.
        """
        try:
            v = validate_round(round_id, multiplier, self.max_multiplier)
        except RoundValidationError as exc:
            self.quarantine(f"invalid_round:{exc.reason}",
                            {"round_id": round_id, "multiplier": multiplier, "source": source, "raw": raw},
                            source=source, round_id_raw=round_id)
            return f"invalid:{exc.reason}"

        now = self.clock() if seen_at is None else seen_at
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute("SELECT cents, source, corroborating_sources FROM rounds "
                                         "WHERE round_id=?", (v.round_id,)).fetchone()
            if existing is not None:
                if existing["cents"] != v.cents:
                    self.conn.execute("COMMIT")
                    self.quarantine("conflicting_result",
                                    {"round_id": v.round_id, "multiplier": v.multiplier, "source": source,
                                     "stored_cents": existing["cents"], "stored_source": existing["source"],
                                     "raw": raw}, source=source, round_id_raw=v.round_id)
                    return "conflict"
                sources = json.loads(existing["corroborating_sources"])
                if source != existing["source"] and source not in sources:
                    sources.append(source)
                    self.conn.execute("UPDATE rounds SET corroborations=corroborations+1, "
                                      "corroborating_sources=? WHERE round_id=?",
                                      (json.dumps(sources), v.round_id))
                self.add_evidence_observation(
                    round_id=v.round_id, association="explicit", evidence_kind="round_result",
                    field_name="maxMultiplier", value={"round_id": v.round_id, "multiplier": v.multiplier},
                    source=source, provenance=provenance,
                )
                self.conn.execute("COMMIT")
                return "duplicate"
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM rounds").fetchone()[0]
            self.conn.execute(
                "INSERT INTO rounds(round_id,multiplier,cents,source,origin,seq,first_seen_at,raw_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (v.round_id, v.multiplier, v.cents, source, origin, seq, now,
                 None if raw is None else json.dumps(raw, ensure_ascii=False, default=str)))
            if notify:
                self._outbox_insert(f"result:{v.round_id}", "round_completed", v.round_id,
                                    {"round_id": v.round_id, "multiplier": v.multiplier, "source": source})
            self.add_evidence_observation(
                round_id=v.round_id, association="explicit", evidence_kind="round_result",
                field_name="maxMultiplier", value={"round_id": v.round_id, "multiplier": v.multiplier},
                source=source, provenance=provenance,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        append_jsonl(self.rounds_jsonl, {"round_id": v.round_id, "multiplier": v.multiplier,
                                          "cents": v.cents, "source": source, "origin": origin,
                                          "seq": seq, "first_seen_at": now})
        # Post-insert derived work. Failures here never lose the stored round.
        self.resolve_prediction(v.round_id)
        self.reverify(v.round_id)
        from .batches import generate_due_batches
        generate_due_batches(self)
        return "inserted"

    def round_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]

    def get_round(self, round_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()

    def last_round(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM rounds ORDER BY round_id DESC LIMIT 1").fetchone()

    def rounds_chronological(self, origins: Iterable[str] | None = None) -> list[sqlite3.Row]:
        if origins:
            origins = list(origins)
            q = ",".join("?" * len(origins))
            return self.conn.execute(f"SELECT * FROM rounds WHERE origin IN ({q}) ORDER BY round_id",
                                     origins).fetchall()
        return self.conn.execute("SELECT * FROM rounds ORDER BY round_id").fetchall()

    def rounds_before(self, target_round_id: int, known_at: float) -> list[sqlite3.Row]:
        """Rounds strictly before the target that were already known at `known_at`."""
        return self.conn.execute(
            "SELECT round_id, cents, seq FROM rounds WHERE round_id < ? AND first_seen_at <= ? "
            "ORDER BY round_id", (target_round_id, known_at)).fetchall()

    # --------------------------------------------------------------- fairness
    def add_fairness_evidence(self, kind: str, value: Any, source: str, round_id: Optional[int],
                              association: str, context_round_id: Optional[int] = None,
                              provenance: Optional[dict[str, Any]] = None) -> bool:
        assert kind in {"server_seed", "player_seeds", "commitment_sha256", "round_hash_sha512"}
        assert association in {"explicit", "unassociated", "legacy_unproven"}
        if association == "explicit" and round_id is None:
            raise ValueError("explicit association requires a round id")
        if association != "explicit":
            round_id_stored = None if association == "unassociated" else round_id
        else:
            round_id_stored = round_id
        value_text = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)
        now = self.clock()
        key = _sha256_json([round_id_stored, association, kind, value_text,
                            context_round_id if association == "unassociated" else None])
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fairness_evidence(round_id,association,kind,value,source,"
            "context_round_id,received_at,evidence_key) VALUES(?,?,?,?,?,?,?,?)",
            (round_id_stored, association, kind, value_text, source, context_round_id, now, key))
        inserted = bool(cur.rowcount)
        if inserted:
            append_jsonl(self.fairness_jsonl, {"round_id": round_id_stored, "association": association,
                                                "kind": kind, "value": value, "source": source,
                                                "context_round_id": context_round_id, "received_at": now})
        # Canonical fairness_evidence remains deduplicated, but every observation
        # retains its independent provenance in evidence_observations.
        self.add_evidence_observation(
            round_id=round_id_stored, association=association, evidence_kind=kind,
            field_name=kind, value=value, source=source, context_round_id=context_round_id,
            provenance=provenance,
        )
        if not inserted:
            return False
        if association == "explicit":
            self.reverify(round_id_stored)
        return True

    def add_evidence_observation(
        self, *, round_id: Optional[int], association: str, evidence_kind: str,
        field_name: Optional[str], value: Any, source: str,
        context_round_id: Optional[int] = None, algorithm: Optional[str] = None,
        semantic_type: Optional[str] = None, provenance: Optional[dict[str, Any]] = None,
    ) -> int:
        p = provenance or {}
        value_text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
        cur = self.conn.execute(
            "INSERT INTO evidence_observations("
            "round_id,association,evidence_kind,field_name,value,algorithm,semantic_type,source,"
            "context_round_id,received_at,received_monotonic,timestamp_provenance,session_id,collector_run_id,event_id,"
            "source_file,source_line,frame_id,frame_index,packet_index,packet_offset,packet_end,raw_evidence_ref,observed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                round_id, association, evidence_kind, field_name, value_text, algorithm, semantic_type, source,
                context_round_id, p.get("received_at"), p.get("received_monotonic"), p.get("timestamp_provenance"),
                p.get("session_id"), p.get("collector_run_id"), p.get("event_id"),
                p.get("source_file"), p.get("source_line"), p.get("frame_id"), p.get("frame_index"),
                p.get("packet_index"), p.get("packet_offset"), p.get("packet_end"), p.get("raw_evidence_ref"), self.clock(),
            ),
        )
        return int(cur.lastrowid)

    def add_crypto_observation(
        self, *, round_id: Optional[int], association: str, field_name: str, value: Any,
        algorithm: Optional[str], semantic_type: str, source: str,
        context_round_id: Optional[int] = None, provenance: Optional[dict[str, Any]] = None,
    ) -> int:
        return self.add_evidence_observation(
            round_id=round_id, association=association, evidence_kind="cryptographic_observation",
            field_name=field_name, value=value, algorithm=algorithm, semantic_type=semantic_type,
            source=source, context_round_id=context_round_id, provenance=provenance,
        )

    def evidence_for_round(self, round_id: int) -> dict[str, list]:
        out = {"server_seed": [], "player_seeds": [], "commitment_sha256": [], "round_hash_sha512": []}
        for r in self.conn.execute("SELECT kind, value FROM fairness_evidence WHERE round_id=? "
                                   "AND association='explicit' ORDER BY id", (round_id,)):
            val = json.loads(r["value"]) if r["kind"] == "player_seeds" else r["value"]
            out[r["kind"]].append(val)
        return out

    def get_verification(self, round_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM fairness_verification WHERE round_id=?",
                                (round_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row["result_json"])
        d["locked"] = bool(row["locked"])
        return d

    def reverify(self, round_id: Optional[int]) -> Optional[dict]:
        if round_id is None:
            return None
        ev = self.evidence_for_round(round_id)
        if not any(ev.values()):
            return None
        prev = self.conn.execute("SELECT status, locked, result_json FROM fairness_verification "
                                 "WHERE round_id=?", (round_id,)).fetchone()
        rnd = self.get_round(round_id)
        result = verify_round(round_id, rnd["cents"] if rnd else None, ev["server_seed"],
                              ev["player_seeds"], ev["commitment_sha256"], ev["round_hash_sha512"])
        now = self.clock()
        if prev is not None and prev["locked"]:
            # A verified round is immutable. Later contradicting evidence is kept
            # (fairness_evidence) and quarantined for review, never overwrites.
            if result.status != "verified":
                self.quarantine("evidence_contradicts_verified_round",
                                {"round_id": round_id, "new_result": result.to_dict()},
                                source="fairness", round_id_raw=round_id)
            return json.loads(prev["result_json"])
        self.conn.execute(
            "INSERT INTO fairness_verification(round_id,status,verified,locked,detail,result_json,updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(round_id) DO UPDATE SET status=excluded.status,"
            " verified=excluded.verified, locked=excluded.locked, detail=excluded.detail,"
            " result_json=excluded.result_json, updated_at=excluded.updated_at",
            (round_id, result.status, int(result.verified), int(result.verified), result.detail,
             json.dumps(result.to_dict()), now))
        if prev is None or prev["status"] != result.status:
            self._outbox_insert(f"fairness:{round_id}:{result.status}", "fairness_update", round_id,
                                {"round_id": round_id, "status": result.status,
                                 "verified": result.verified, "detail": result.detail})
        return result.to_dict()

    def verification_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM fairness_verification GROUP BY status")}

    # ------------------------------------------------------------ predictions
    def insert_prediction(self, row: dict) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO predictions(round_id,frozen_at,trigger,model_version,feature_count,"
            "feature_max_round_id,feature_max_seq,feature_hash,output_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (row["round_id"], row["frozen_at"], row["trigger"], row["model_version"], row["feature_count"],
             row["feature_max_round_id"], row["feature_max_seq"], row["feature_hash"],
             json.dumps(row["output"], sort_keys=True)))
        if not cur.rowcount:
            return False
        append_jsonl(self.predictions_jsonl, {"event": "frozen", **row})
        self._outbox_insert(f"prediction:{row['round_id']}", "prediction_frozen", row["round_id"],
                            {"round_id": row["round_id"], "output": row["output"],
                             "feature_count": row["feature_count"]})
        return True

    def get_prediction(self, round_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM predictions WHERE round_id=?", (round_id,)).fetchone()

    def resolve_prediction(self, round_id: int) -> Optional[dict]:
        pred = self.get_prediction(round_id)
        rnd = self.get_round(round_id)
        if pred is None or rnd is None or pred["resolved_at"] is not None:
            return None
        from .predict import leakage_problems
        problems = leakage_problems(dict(pred), dict(rnd))
        now = self.clock()
        self.conn.execute("UPDATE predictions SET actual_cents=?, actual_multiplier=?, resolved_at=?, "
                          "leak_flag=?, leak_detail=? WHERE round_id=?",
                          (rnd["cents"], rnd["multiplier"], now, int(bool(problems)),
                           "; ".join(problems) or None, round_id))
        rec = {"event": "resolved", "round_id": round_id, "actual_multiplier": rnd["multiplier"],
               "resolved_at": now, "leak_flag": bool(problems), "leak_detail": problems}
        append_jsonl(self.predictions_jsonl, rec)
        return rec

    def resolved_predictions(self, include_leaky: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM predictions WHERE resolved_at IS NOT NULL"
        if not include_leaky:
            q += " AND leak_flag=0"
        return self.conn.execute(q + " ORDER BY round_id").fetchall()

    # ----------------------------------------------------------------- manual
    def add_manual(self, chat_id: int, values: list[float]) -> int:
        now = self.clock()
        for v in values:
            self.conn.execute("INSERT INTO manual_entries(chat_id,multiplier,created_at) VALUES(?,?,?)",
                              (chat_id, v, now))
        return self.manual_count(chat_id)

    def manual_count(self, chat_id: Optional[int] = None) -> int:
        if chat_id is None:
            return self.conn.execute("SELECT COUNT(*) FROM manual_entries").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM manual_entries WHERE chat_id=?",
                                 (chat_id,)).fetchone()[0]

    def manual_values(self, chat_id: int) -> list[float]:
        return [r[0] for r in self.conn.execute(
            "SELECT multiplier FROM manual_entries WHERE chat_id=? ORDER BY id", (chat_id,))]

    def clear_manual(self, chat_id: Optional[int] = None) -> int:
        if chat_id is None:
            cur = self.conn.execute("DELETE FROM manual_entries")
        else:
            cur = self.conn.execute("DELETE FROM manual_entries WHERE chat_id=?", (chat_id,))
        return cur.rowcount

    # ---------------------------------------------------- subscribers/outbox
    def subscribe(self, chat_id: int) -> None:
        now = self.clock()
        self.conn.execute("INSERT INTO subscribers(chat_id,active,created_at,updated_at) VALUES(?,1,?,?) "
                          "ON CONFLICT(chat_id) DO UPDATE SET active=1, updated_at=excluded.updated_at",
                          (chat_id, now, now))

    def unsubscribe(self, chat_id: int) -> None:
        self.conn.execute("UPDATE subscribers SET active=0, updated_at=? WHERE chat_id=?",
                          (self.clock(), chat_id))

    def active_subscribers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM subscribers WHERE active=1 ORDER BY chat_id").fetchall()

    def _outbox_insert(self, event_key: str, kind: str, round_id: Optional[int], payload: dict) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO outbox(event_key,kind,round_id,payload_json,created_at)"
                                " VALUES(?,?,?,?,?)",
                                (event_key, kind, round_id, json.dumps(payload, sort_keys=True), self.clock()))
        return bool(cur.rowcount)

    def pending_outbox(self, since: float, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM outbox WHERE created_at >= ? ORDER BY id LIMIT ?",
                                 (since, limit)).fetchall()

    def delivery(self, outbox_id: int, chat_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM deliveries WHERE outbox_id=? AND chat_id=?",
                                 (outbox_id, chat_id)).fetchone()

    def record_delivery(self, outbox_id: int, chat_id: int, status: str, error: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO deliveries(outbox_id,chat_id,status,attempts,last_error,updated_at) VALUES(?,?,?,1,?,?)"
            " ON CONFLICT(outbox_id,chat_id) DO UPDATE SET status=excluded.status,"
            " attempts=deliveries.attempts+1, last_error=excluded.last_error, updated_at=excluded.updated_at",
            (outbox_id, chat_id, status, error, self.clock()))

    # ---------------------------------------------------------------- status
    def set_status(self, status: str, detail: str = "", **extra: Any) -> None:
        payload = {"status": status, "detail": detail, "updated_at": self.clock(), **extra}
        self.set_meta("collector_status", json.dumps(payload, ensure_ascii=False, default=str))

    def get_status(self) -> dict:
        raw = self.get_meta("collector_status")
        return json.loads(raw) if raw else {"status": "not_started", "detail": ""}
