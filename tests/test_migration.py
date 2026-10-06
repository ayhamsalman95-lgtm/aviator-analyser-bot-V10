import hashlib
import json
import shutil
import sqlite3
import unittest

from tests.helpers import FIXTURES, TempProject
from scripts import migrate_legacy
from scripts import repair as repair_mod
from aviator.jsonl import read_jsonl
from aviator.db import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject()
        self.legacy = self.p.dir / "legacy"
        self.legacy.mkdir()
        shutil.copy(FIXTURES / "legacy_rounds_sample.json", self.legacy / "rounds.json")
        (self.legacy / "subscribers.json").write_text("[111, 222]")
        (self.legacy / "config.json").write_text(json.dumps({"bot_token": "123:SECRET", "window": 10}))
        self.orig_sha = hashlib.sha256((self.legacy / "rounds.json").read_bytes()).hexdigest()

    def tearDown(self):
        self.p.cleanup()

    def run_migration(self):
        return migrate_legacy.migrate(self.p.store, self.legacy, self.p.cfg.data_dir / "legacy_evidence")

    def test_migration_counts(self):
        s = self.run_migration()
        self.assertEqual(s["rounds_seen"], 15)
        self.assertEqual(s["inserted"], 6)                     # 6 valid canonical rounds
        self.assertEqual(s["duplicates"], 1)
        self.assertEqual(s["quarantined"]["non_canonical_source:websocket"], 5)
        self.assertEqual(s["quarantined"]["conflict"], 1)
        self.assertEqual(s["quarantined"]["invalid:round_id_not_numeric"], 1)
        self.assertEqual(s["quarantined"]["invalid:multiplier_below_1"], 1)
        self.assertEqual(s["subscribers"], 2)
        self.assertEqual(self.p.store.round_count(), 6)
        self.assertEqual({r["origin"] for r in self.p.store.rounds_chronological()}, {"migration"})

    def test_evidence_preserved_and_originals_untouched(self):
        s = self.run_migration()
        self.assertEqual(hashlib.sha256((self.legacy / "rounds.json").read_bytes()).hexdigest(), self.orig_sha)
        man = s["preserved"]["files"]
        self.assertEqual(man["rounds.json"]["sha256_original"], self.orig_sha)
        copies = list((self.p.cfg.data_dir / "legacy_evidence").glob("*/rounds.json"))
        self.assertEqual(len(copies), 1)
        cfg_copy = next((self.p.cfg.data_dir / "legacy_evidence").glob("*/config.json")).read_text()
        self.assertNotIn("SECRET", cfg_copy)
        # quarantined junk keeps the full original record
        rec = json.loads(self.p.store.conn.execute(
            "SELECT record_json FROM quarantine WHERE round_id_raw='9e1da33c668c5970'").fetchone()[0])
        self.assertEqual(rec["raw_keys"], ["a", "c", "p"])

    def test_legacy_seeds_never_verified(self):
        self.run_migration()
        rows = self.p.store.conn.execute("SELECT association FROM fairness_evidence").fetchall()
        self.assertTrue(rows)
        self.assertEqual({r[0] for r in rows}, {"legacy_unproven"})
        self.assertEqual(self.p.store.verification_counts(), {})

    def test_idempotent(self):
        self.run_migration()
        q1 = self.p.store.conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        s2 = self.run_migration()
        self.assertEqual(s2["inserted"], 0)
        self.assertEqual(self.p.store.round_count(), 6)
        self.assertEqual(self.p.store.conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0], q1)
        self.assertEqual(len(list(read_jsonl(self.p.store.rounds_jsonl))), 6)

    def test_schema_migration_preserves_legacy_fairness_evidence(self):
        db_path = self.p.dir / "legacy.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta(key,value) VALUES('schema_version','2');
            CREATE TABLE fairness_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT NOT NULL,
                received_at REAL NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO fairness_evidence(round_id,kind,value,source,received_at) VALUES(?,?,?,?,?)",
            (123, "server_seed", "legacy-value", "legacy", 100.0),
        )
        conn.commit()
        conn.close()

        store = Store(
            db_path,
            self.p.cfg.structured_dir,
            self.p.cfg.batches_dir,
            batch_size=int(self.p.cfg["batch_size"]),
            clock=self.p.clock,
        )
        try:
            row = store.conn.execute(
                "SELECT round_id,kind,value,association FROM fairness_evidence"
            ).fetchone()
            self.assertEqual(dict(row), {
                "round_id": 123,
                "kind": "server_seed",
                "value": "legacy-value",
                "association": "legacy_unproven",
            })
            obs = store.conn.execute(
                "SELECT evidence_kind,value,association FROM evidence_observations"
            ).fetchall()
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["value"], "legacy-value")
            self.assertEqual(obs[0]["association"], "legacy_unproven")
            self.assertEqual(store.get_meta("schema_version"), "3")
            self.assertIsNotNone(
                store.conn.execute(
                    "SELECT version FROM schema_migrations WHERE version=3"
                ).fetchone()
            )
        finally:
            store.close()

    def test_repair_appends_missing_jsonl_only(self):
        self.run_migration()
        lines = self.p.store.rounds_jsonl.read_text().splitlines()
        self.p.store.rounds_jsonl.write_text("\n".join(lines[:3]) + "\n")  # simulate lost tail
        rep = repair_mod.repair(self.p.store)
        self.assertEqual(rep["jsonl_appended"], 3)
        self.assertEqual(rep["integrity"], "ok")
        self.assertEqual(len(list(read_jsonl(self.p.store.rounds_jsonl))), 6)


if __name__ == "__main__":
    unittest.main()
