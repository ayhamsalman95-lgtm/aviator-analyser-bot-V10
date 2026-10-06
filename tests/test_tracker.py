import json
import unittest

from tests.helpers import TempProject, load_replay
from aviator.tracker import RoundTracker


class ReplayTests(unittest.TestCase):
    """Deterministic replay of an SFS command stream through the state machine."""

    def setUp(self):
        self.p = TempProject()
        self.s = self.p.store
        self.t = RoundTracker(self.s, self.p.cfg, clock=self.p.clock)

    def tearDown(self):
        self.p.cleanup()

    def run_replay(self):
        results = []
        for ev in load_replay():
            self.p.clock.tick(1)
            results.append(self.t.handle(ev["cmd"], ev["params"]))
        return results

    def test_full_replay(self):
        r = self.run_replay()
        self.assertEqual(r[0], "init:2/0/2")                 # backfill: 2 valid, 2 quarantined
        self.assertEqual(r[1], "prediction_frozen")          # cutoff at newStateId=1
        self.assertEqual(r[2], "fairness_empty")             # serverSeedSHA256 is preserved as neutral crypto evidence, not fairness evidence
        self.assertEqual(r[4], "ignored")                    # cashout multipliers never results
        self.assertEqual(r[7], "inserted")                   # roundChartInfo for 5000003
        self.assertEqual(r[8], "duplicate")
        self.assertEqual(self.s.round_count(), 4)
        # ...but the round-less commitment is NOT attached to any round
        row = self.s.conn.execute("SELECT round_id, association, context_round_id FROM fairness_evidence "
                                  "WHERE source LIKE '%serverSeedResponse%'").fetchone()
        self.assertIsNone(row["round_id"])
        self.assertEqual(row["association"], "unassociated")
        # previous-round result arriving after next round's changeState did not wipe state
        self.assertEqual(self.t.current_round_id, 5000004)
        # fairness.roundId (5000003) used, not the message's top-level roundId (5000004)
        v = self.s.get_verification(5000003)
        self.assertEqual(v["status"], "not_verifiable", v)
        self.assertFalse(v["verified"], v)
        self.assertIsNone(self.s.get_verification(5000004))
        # backfill rounds are tagged
        self.assertEqual(self.s.get_round(5000001)["origin"], "backfill")
        self.assertEqual(self.s.get_round(5000001)["source"], "sfs:init.roundsInfo")

    def test_predictions_frozen_and_resolved(self):
        self.run_replay()
        p3 = self.s.get_prediction(5000003)
        self.assertIsNotNone(p3)
        self.assertEqual(p3["feature_count"], 2)             # only the two backfilled rounds
        self.assertLess(p3["feature_max_round_id"], 5000003)
        self.assertEqual(p3["actual_cents"], self.s.get_round(5000003)["cents"])
        self.assertEqual(p3["leak_flag"], 0)
        p4 = self.s.get_prediction(5000004)
        self.assertEqual(p4["feature_count"], 2)             # 5000003 result unknown at 5000004's cutoff
        self.assertEqual(p4["leak_flag"], 0)
        self.assertEqual(p4["actual_cents"], 100)

    def test_no_prediction_on_other_states_or_known_result(self):
        self.s.insert_round(10, 2.0, "sfs:roundChartInfo")
        self.assertEqual(self.t.handle("changeState", {"roundId": 11, "newStateId": 2}), "state:2")
        self.assertIsNone(self.s.get_prediction(11))
        self.assertEqual(self.t.handle("changeState", {"roundId": 10, "newStateId": 1}), "stale")
        t2 = RoundTracker(self.s, self.p.cfg, clock=self.p.clock)
        self.assertEqual(t2.handle("changeState", {"roundId": 10, "newStateId": 1}),
                         "prediction_skipped_result_known")

    def test_empty_server_seed_response_no_crash(self):
        self.assertEqual(self.t.handle("serverSeedResponse", {}), "fairness_empty")
        self.assertEqual(self.t.handle("serverSeedResponse", None), "fairness_empty")
        self.assertEqual(self.t.handle("serverSeedResponse", {"foo": 1}), "fairness_empty")

    def test_invalid_round_chart_quarantined(self):
        self.assertEqual(self.t.handle("roundChartInfo", {"roundId": 5}), "invalid")
        self.assertEqual(self.t.handle("roundChartInfo", {"roundId": "x1", "maxMultiplier": 2}), "invalid:round_id_not_numeric")
        self.assertEqual(self.s.round_count(), 0)

    def test_generic_crash_payloads_never_saved(self):
        for cmd, params in [("x", {"crashPoint": 3.0, "roundId": 1}),
                            ("updateCurrentBets", {"multiplier": 2.0, "roundId": 1}),
                            ("OnCrash", {"f": 2.0, "l": 5})]:
            self.t.handle(cmd, params)
        self.assertEqual(self.s.round_count(), 0)

    def test_next_commitment_with_round_id_is_not_bound(self):
        self.t.handle("serverSeedResponse", {"roundId": 77, "nextServerSeedSHA256": "c" * 64})
        self.assertIsNone(self.s.get_verification(77))


if __name__ == "__main__":
    unittest.main()
