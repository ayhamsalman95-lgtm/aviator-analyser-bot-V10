import hashlib
import json
import shutil
import unittest

from tests.helpers import FIXTURES, TempProject
from scripts import migrate_legacy
from scripts import repair as repair_mod
from aviator.jsonl import read_jsonl


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
