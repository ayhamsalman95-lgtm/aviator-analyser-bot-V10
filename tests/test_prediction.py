import json
import math
import random
import unittest

from tests.helpers import TempProject
from aviator import evaluate, predict, stats
from aviator.jsonl import read_jsonl
from aviator.tracker import RoundTracker


class PredictionCutoffTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject(min_history=1)
        self.s = self.p.store
        self.t = RoundTracker(self.s, self.p.cfg, clock=self.p.clock)
        for i in range(1, 51):
            self.p.clock.tick()
            self.s.insert_round(i, 1.0 + (i % 7), "sfs:roundChartInfo")

    def tearDown(self):
        self.p.cleanup()

    def test_frozen_features_exclude_target_and_future(self):
        self.p.clock.tick()
        self.t.handle("changeState", {"roundId": 60, "newStateId": 1})
        row = self.s.get_prediction(60)
        self.assertEqual(row["feature_count"], 50)
        self.assertEqual(row["feature_max_round_id"], 50)
        # a backfilled older round inserted AFTER the freeze is not part of the frozen set
        self.p.clock.tick()
        self.s.insert_round(55, 9.0, "sfs:init.roundsInfo", origin="backfill", notify=False)
        feats = predict.freeze_features(self.s, 60, row["frozen_at"])
        self.assertEqual(feats.count, 50)
        self.assertEqual(feats.digest(), row["feature_hash"])

    def test_prediction_recorded_before_result_then_resolved_separately(self):
        self.p.clock.tick()
        self.t.handle("changeState", {"roundId": 51, "newStateId": 1})
        row = self.s.get_prediction(51)
        self.assertIsNone(row["actual_cents"])
        self.p.clock.tick()
        self.t.handle("roundChartInfo", {"roundId": 51, "maxMultiplier": 3.33})
        row = self.s.get_prediction(51)
        self.assertEqual(row["actual_cents"], 333)
        self.assertEqual(row["leak_flag"], 0)
        events = [e["event"] for e in read_jsonl(self.s.predictions_jsonl)]
        self.assertEqual(events, ["frozen", "resolved"])

    def test_leakage_detector(self):
        pred = {"round_id": 5, "frozen_at": 100.0, "feature_max_round_id": 5}
        self.assertTrue(predict.leakage_problems(pred, {"round_id": 5, "first_seen_at": 200.0}))
        pred = {"round_id": 5, "frozen_at": 300.0, "feature_max_round_id": 4}
        self.assertTrue(predict.leakage_problems(pred, {"round_id": 5, "first_seen_at": 200.0}))
        pred = {"round_id": 5, "frozen_at": 100.0, "feature_max_round_id": 4}
        self.assertEqual(predict.leakage_problems(pred, {"round_id": 5, "first_seen_at": 200.0}), [])

    def test_leaky_prediction_flagged_and_excluded(self):
        # simulate a prediction row frozen after the result was known
        self.s.conn.execute("INSERT INTO predictions(round_id,frozen_at,trigger,model_version,feature_count,"
                            "feature_max_round_id,feature_max_seq,feature_hash,output_json) "
                            "VALUES(30,?,?,?,?,?,?,?,?)",
                            (self.p.clock() + 999, "test", "x", 1, 29, 29, "h", json.dumps({"models": {}})))
        self.s.resolve_prediction(30)
        self.assertEqual(self.s.get_prediction(30)["leak_flag"], 1)
        self.assertEqual(len(self.s.resolved_predictions()), 0)

    def test_no_lstm_in_output(self):
        self.p.clock.tick()
        self.t.handle("changeState", {"roundId": 70, "newStateId": 1})
        out = json.loads(self.s.get_prediction(70)["output_json"])
        self.assertEqual(set(out["models"]), {"theoretical", "empirical_all", "empirical_recent"})
        import aviator.predict as mod
        self.assertFalse(hasattr(mod, "LightLSTM"))


class EvaluationTests(unittest.TestCase):
    def test_walk_forward_is_chronological(self):
        seen = []
        evaluate.walk_forward([100 + i for i in range(50)], [2.0], min_train=10,
                              _observer=lambda i, n: seen.append((i, n)))
        self.assertTrue(all(i == n for i, n in seen))  # at step i the model saw exactly rounds[:i]
        self.assertEqual(seen[0], (10, 10))

    def test_metrics_known_values(self):
        self.assertAlmostEqual(evaluate.brier([0.5, 0.5], [1, 0]), 0.25)
        self.assertAlmostEqual(evaluate.log_loss([0.5, 0.5], [1, 0]), math.log(2))
        cal = evaluate.calibration([0.05, 0.05, 0.95], [0, 1, 1])
        self.assertEqual(cal[0]["n"], 2)
        self.assertAlmostEqual(cal[0]["observed"], 0.5)

    def test_theoretical_is_well_calibrated_on_simulated_rounds(self):
        rng = random.Random(7)
        E = 2 ** 52
        cents = [max(100, (97 * E) // (E - rng.randrange(E))) for _ in range(20000)]
        res = evaluate.walk_forward(cents, [2.0], min_train=100)
        m = res["metrics"]["2"]
        self.assertAlmostEqual(m["base_rate"], 0.485, delta=0.02)
        theo = m["models"]["theoretical"]["brier"]
        self.assertAlmostEqual(theo, 0.485 * 0.515, delta=0.01)


class StatsTests(unittest.TestCase):
    def test_streaks(self):
        s = stats.streaks([1.0, 1.1, 3.0, 1.2, 1.3, 1.4, 2.5], 2.0)
        self.assertEqual(s["longest_below"], 3)
        self.assertEqual(s["longest_above"], 1)
        self.assertEqual(s["current"], {"type": "above", "length": 1})

    def test_runs_test_detects_alternation(self):
        r = stats.runs_test([1.0, 3.0] * 100, 2.0)
        self.assertEqual(r["runs"], 200)
        self.assertLess(r["p_value"], 1e-6)

    def test_runs_test_random_not_rejected(self):
        rng = random.Random(1)
        r = stats.runs_test([rng.choice([1.5, 2.5]) for _ in range(2000)], 2.0)
        self.assertGreater(r["p_value"], 0.001)

    def test_autocorrelation_and_transitions(self):
        rng = random.Random(3)
        vals = [1 + rng.expovariate(1) for _ in range(3000)]
        ac = stats.autocorrelation(vals, 10)
        self.assertLess(abs(ac["lags"][1]), 0.1)
        tr = stats.transitions(vals)
        self.assertEqual(tr["n_transitions"], 2999)
        self.assertIsNotNone(tr["p_value"])


if __name__ == "__main__":
    unittest.main()
