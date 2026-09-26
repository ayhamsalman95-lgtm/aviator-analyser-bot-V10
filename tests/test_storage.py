import json
import sqlite3
import time
import unittest

from tests.helpers import TempProject
from aviator.jsonl import read_jsonl


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject(batch_size=1000)
        self.s = self.p.store

    def tearDown(self):
        self.p.cleanup()

    def test_wal_mode(self):
        self.assertEqual(self.s.journal_mode().lower(), "wal")

    def test_insert_persists_immediately_and_jsonl_appended(self):
        self.assertEqual(self.s.insert_round(123, 2.5, "sfs:roundChartInfo"), "inserted")
        other = sqlite3.connect(str(self.p.cfg.db_path))  # a second process would see it
        self.assertEqual(other.execute("SELECT cents FROM rounds WHERE round_id=123").fetchone()[0], 250)
        other.close()
        lines = list(read_jsonl(self.s.rounds_jsonl))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["round_id"], 123)

    def test_duplicate_protection_and_corroboration(self):
        self.s.insert_round(1, 1.5, "sfs:roundChartInfo")
        self.assertEqual(self.s.insert_round(1, 1.5, "sfs:roundChartInfo"), "duplicate")
        self.assertEqual(self.s.insert_round("1", "1.50", "sfs:init.roundsInfo", origin="backfill"), "duplicate")
        self.assertEqual(self.s.round_count(), 1)
        self.assertEqual(len(list(read_jsonl(self.s.rounds_jsonl))), 1)
        self.assertEqual(self.s.get_round(1)["corroborations"], 1)

    def test_conflicting_result_is_quarantined_not_overwritten(self):
        self.s.insert_round(7, 3.0, "sfs:roundChartInfo")
        self.assertEqual(self.s.insert_round(7, 4.0, "sfs:roundChartInfo"), "conflict")
        self.assertEqual(self.s.get_round(7)["cents"], 300)
        self.assertIn("conflicting_result", self.s.quarantine_counts())

    def test_strict_validation(self):
        for rid, m, reason in [("9e1da33c668c5970", 2.0, "round_id_not_numeric"), (0, 2.0, "round_id_not_positive"),
                               (5, 0.99, "multiplier_below_1"), (5, float("nan"), "multiplier_not_finite"),
                               (5, 1.234, "multiplier_more_than_2_decimals"), (True, 2.0, "round_id_bool")]:
            self.assertEqual(self.s.insert_round(rid, m, "x"), f"invalid:{reason}")
        self.assertEqual(self.s.round_count(), 0)
        self.assertEqual(sum(self.s.quarantine_counts().values()), 6)

    def test_restart_resume(self):
        self.s.insert_round(10, 1.1, "sfs:roundChartInfo")
        s2 = self.p.reopen()
        self.assertEqual(s2.round_count(), 1)
        self.assertEqual(s2.insert_round(10, 1.1, "sfs:roundChartInfo"), "duplicate")
        self.assertEqual(s2.insert_round(11, 1.2, "sfs:roundChartInfo"), "inserted")
        self.assertEqual(s2.get_round(11)["seq"], 2)
        from aviator.tracker import RoundTracker
        self.assertEqual(RoundTracker(s2, self.p.cfg).last_completed_id, 11)

    def test_manual_entries_never_touch_canonical(self):
        self.s.insert_round(1, 2.0, "sfs:roundChartInfo")
        self.s.add_manual(42, [1.5, 3.0])
        self.assertEqual(self.s.round_count(), 1)
        self.s.insert_round(2, 2.0, "sfs:roundChartInfo")
        self.assertEqual(self.s.manual_count(42), 2)  # old bug: history rebuild wiped /add
        self.s.clear_manual(42)
        self.assertEqual(self.s.round_count(), 2)


class BatchTests(unittest.TestCase):
    def test_batches_fairness_evidence_complete(self):
        """Verify that complete fairness evidence is included in batch, not truncated."""
        p = TempProject(batch_size=50)
        try:
            s = p.store
            
            # Create LONG fairness evidence value (would be truncated at 64 chars in old code)
            long_seed = "x" * 2000  # 2000-char string
            long_value_json = json.dumps({
                "seed": long_seed,
                "nonce": "n" * 500,
                "timestamp": "t" * 300,
                "metadata": "m" * 1500
            })
            
            # Insert fairness evidence BEFORE rounds (so it will be included in batch)
            s.conn.execute(
                "INSERT INTO fairness_evidence(round_id, association, kind, value, source, received_at, evidence_key) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (2000, "explicit", "server_seed", long_value_json, "test", time.time(), "test_seed_2000")
            )
            s.conn.commit()
            
            # NOW insert rounds (which triggers batch generation)
            for i in range(60):
                result = s.insert_round(2000 + i, 1.5 + (i % 10) / 100, "sfs:roundChartInfo")
                self.assertEqual(result, "inserted")
            
            # Get batch file
            from aviator.batches import _batch_path
            batch_file = _batch_path(s, 1)
            self.assertTrue(batch_file.exists(), f"Batch file not found: {batch_file}")
            
            text = batch_file.read_text(encoding="utf-8")
            
            # Verify round 2000 is present
            self.assertIn("round_id: 2000", text, "Round 2000 not found in batch")
            
            # Verify FAIRNESS_EVIDENCE section exists
            self.assertIn("FAIRNESS_EVIDENCE:", text, "FAIRNESS_EVIDENCE section missing")
            
            # Verify server_seed kind is present
            self.assertIn("kind: server_seed", text, "server_seed evidence kind not found")
            
            # CRITICAL: Verify NO TRUNCATION of fairness value
            # The 2000-char seed should be completely present (old code would truncate at 64 chars)
            self.assertIn(long_seed, text, 
                         "Long fairness seed TRUNCATED! Original had 2000 chars, batch should too.")
            
            # Verify the complete JSON structure is present
            self.assertIn('"nonce"', text, "nonce field missing from fairness value")
            self.assertIn("n" * 100, text, "nonce value truncated (should have many n's)")
            
        finally:
            p.cleanup()
    
    def test_batches_fairness_evidence_exact_round_only(self):
        """Verify that fairness evidence is ONLY attached to exact round, not guessed."""
        p = TempProject(batch_size=50)
        try:
            s = p.store
            
            # Insert fairness evidence for round 2000 only
            evidence_2000 = json.dumps({"seed": "seed_2000"})
            s.conn.execute(
                "INSERT INTO fairness_evidence(round_id, association, kind, value, source, received_at, evidence_key) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (2000, "explicit", "server_seed", evidence_2000, "test", time.time(), "key_2000")
            )
            s.conn.commit()
            
            # Insert rounds (which triggers batch generation)
            for i in range(60):
                result = s.insert_round(2000 + i, 1.5 + (i % 10) / 100, "sfs:roundChartInfo")
                self.assertEqual(result, "inserted")
            
            # Get batch file
            from aviator.batches import _batch_path
            batch_file = _batch_path(s, 1)
            self.assertTrue(batch_file.exists())
            
            text = batch_file.read_text(encoding="utf-8")
            
            # Find round 2000 section (should have evidence)
            lines = text.splitlines()
            round_2000_idx = None
            for i, line in enumerate(lines):
                if "round_id: 2000" in line:
                    round_2000_idx = i
                    break
            
            self.assertIsNotNone(round_2000_idx, "Round 2000 not found")
            
            # Extract round 2000 block (between ROUND and END_ROUND)
            round_2000_block = []
            for i in range(round_2000_idx, len(lines)):
                round_2000_block.append(lines[i])
                if lines[i] == "END_ROUND":
                    break
            
            block_text = "\n".join(round_2000_block)
            
            # Round 2000 SHOULD have the evidence
            self.assertIn("server_seed", block_text, "Round 2000 should have server_seed evidence")
            self.assertIn("seed_2000", block_text, "Round 2000 evidence value not found")
            
            # Find round 2001 section (should NOT have evidence)
            round_2001_idx = None
            for i, line in enumerate(lines):
                if "round_id: 2001" in line:
                    round_2001_idx = i
                    break
            
            self.assertIsNotNone(round_2001_idx, "Round 2001 not found")
            
            # Extract round 2001 block
            round_2001_block = []
            for i in range(round_2001_idx, len(lines)):
                round_2001_block.append(lines[i])
                if lines[i] == "END_ROUND":
                    break
            
            block_text_2001 = "\n".join(round_2001_block)
            
            # Round 2001 should NOT have evidence (strict association rule)
            self.assertIn("FAIRNESS_EVIDENCE: UNAVAILABLE", block_text_2001,
                         "Round 2001 should NOT have evidence from round 2000 (exact-round association only)")
            self.assertNotIn("seed_2000", block_text_2001,
                           "Round 2001 should not have round 2000's evidence")
            
        finally:
            p.cleanup()
    
    def test_batches_long_raw_json_not_truncated(self):
        """Verify that LONG raw_json survives completely in batch with NO truncation."""
        p = TempProject(batch_size=50)
        try:
            s = p.store
            
            # Create a LONG raw_json (would be truncated at 200 chars in old code)
            long_raw_json = json.dumps({
                "gameData": {
                    "rounds": [{"id": i, "data": "d" * 100} for i in range(50)],
                    "metadata": "m" * 1500,
                    "logs": "l" * 2000,
                    "trace": "t" * 1000
                },
                "extraInfo": "e" * 3000
            })
            
            # Insert 60 rounds, with first one having LONG raw_json
            for i in range(60):
                if i == 0:
                    # Manually insert first round with raw_json to bypass collector parsing
                    s.conn.execute(
                        "INSERT INTO rounds(round_id, multiplier, cents, source, origin, "
                        "first_seen_at, seq, raw_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (3000, 1.50, 150, "test", "direct", time.time(), 1, long_raw_json)
                    )
                    s.conn.commit()
                else:
                    result = s.insert_round(3000 + i, 1.5 + (i % 10) / 100, "sfs:roundChartInfo")
                    self.assertEqual(result, "inserted")
            
            # Get batch file
            from aviator.batches import _batch_path
            batch_file = _batch_path(s, 1)
            self.assertTrue(batch_file.exists())
            
            text = batch_file.read_text(encoding="utf-8")
            
            # Verify the COMPLETE long raw_json is present
            self.assertIn('"gameData"', text)
            self.assertIn("m" * 200, text,
                         "Long raw_json metadata truncated! Should be complete.")
            self.assertIn("l" * 200, text,
                         "Long raw_json logs truncated! Should be complete.")
            self.assertIn("e" * 200, text,
                         "Long raw_json extraInfo truncated! Should be complete.")
        finally:
            p.cleanup()
    
    def test_batches_every_n_rounds(self):
        p = TempProject(batch_size=100)
        try:
            s = p.store
            for i in range(250):
                s.insert_round(1000 + i, 1.0 + (i % 50) / 10, "sfs:roundChartInfo")
            files = sorted(p.cfg.batches_dir.glob("batch_*.txt"))
            self.assertEqual(len(files), 2)
            
            # New format: ROUND...END_ROUND blocks, so count END_ROUND markers
            text = files[0].read_text(encoding="utf-8")
            round_count = text.count("END_ROUND")  # Count END_ROUND markers
            self.assertEqual(round_count, 100, f"Expected 100 rounds in batch, got {round_count}")
            
            # Verify it contains expected round IDs
            self.assertIn("round_id: 1000", text)
            self.assertIn("END_ROUND", text)
            
            before = files[0].read_bytes()
            from aviator.batches import generate_due_batches
            self.assertEqual(generate_due_batches(s), [])  # idempotent
            self.assertEqual(files[0].read_bytes(), before)  # never rewritten
            for i in range(250, 300):
                s.insert_round(1000 + i, 2.0, "sfs:roundChartInfo")
            self.assertEqual(len(list(p.cfg.batches_dir.glob("batch_*.txt"))), 3)
        finally:
            p.cleanup()

    def test_batches_contain_all_canonical_fields(self):
        """Verify that TXT batches contain ALL canonical fields with NO truncation."""
        p = TempProject(batch_size=50)
        try:
            s = p.store
            
            # Insert 60 rounds (batches generated automatically)
            for i in range(60):
                result = s.insert_round(2000 + i, 1.5 + (i % 10) / 100, "sfs:roundChartInfo")
                self.assertEqual(result, "inserted")
            
            # Get batch file
            from aviator.batches import _batch_path
            batch_file = _batch_path(s, 1)
            self.assertTrue(batch_file.exists(), f"Batch file not found: {batch_file}")
            
            text = batch_file.read_text(encoding="utf-8")
            
            # Verify structure with ROUND/END_ROUND markers
            self.assertIn("ROUND", text)
            self.assertIn("END_ROUND", text)
            self.assertIn("round_id: 2000", text)
            self.assertIn("RAW_EVIDENCE:", text)
            self.assertIn("FAIRNESS_EVIDENCE:", text)
            self.assertIn("FAIRNESS_VERIFICATION:", text)
            self.assertIn("PREDICTION:", text)
            
            # Verify all canonical fields are present for at least one round
            self.assertIn("round_id:", text)
            self.assertIn("multiplier:", text)
            self.assertIn("cents:", text)
            self.assertIn("source:", text)
            self.assertIn("origin:", text)
            self.assertIn("first_seen_at:", text)
            self.assertIn("seq:", text)
            self.assertIn("corroborations:", text)
            self.assertIn("corroborating_sources:", text)
            
            # Verify batch contains 50 rounds (END_ROUND markers)
            round_count = text.count("END_ROUND")
            self.assertEqual(round_count, 50, f"Batch should contain 50 rounds, found {round_count}")
        finally:
            p.cleanup()


if __name__ == "__main__":
    unittest.main()
