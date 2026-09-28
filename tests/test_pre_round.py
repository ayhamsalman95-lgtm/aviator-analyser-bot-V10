import unittest

from aviator.pre_round import ObservableEvent, PreRoundBuffer


class PreRoundBufferTests(unittest.TestCase):
    def test_snapshot_is_cutoff_safe(self):
        b = PreRoundBuffer(max_events=10, max_age_s=10)
        b.add(ObservableEvent(100.0, "ws", "a", 10, None, 1, 0, 100, 20, 0, 20, None, {"x": 1}))
        b.add(ObservableEvent(100.5, "ws", "changeState", 10, 1, 2, 0, 110, 30, 0, 30, 500.0, {}))
        b.add(ObservableEvent(101.0, "ws", "roundChartInfo", 10, 3, 3, 0, 120, 40, 0, 40, 500.0, {"maxMultiplier": 2.0}))
        snap = b.snapshot(10, 100.5, cutoff_state=1)
        self.assertEqual(snap["event_count"], 2)
        self.assertEqual(snap["command_sequence"], ["a", "changeState"])
        self.assertNotIn("roundChartInfo", snap["command_sequence"])

    def test_unassociated_events_are_allowed_but_other_rounds_are_excluded(self):
        b = PreRoundBuffer(max_events=10, max_age_s=10)
        b.add(ObservableEvent(100.0, "ws", "heartbeat", None, None, 1, 0, 10, 10, 0, 10, None, {}))
        b.add(ObservableEvent(100.1, "ws", "x", 99, None, 2, 0, 10, 10, 0, 10, None, {}))
        b.add(ObservableEvent(100.2, "ws", "y", 100, None, 3, 0, 10, 10, 0, 10, None, {}))
        snap = b.snapshot(100, 100.2)
        self.assertEqual(snap["command_sequence"], ["heartbeat", "y"])

    def test_age_and_capacity_bounds(self):
        b = PreRoundBuffer(max_events=2, max_age_s=1)
        for i in range(4):
            b.add(ObservableEvent(float(i), "ws", str(i), 1, None, i, 0, 1, 1, 0, 1, None, {}))
        self.assertEqual(len(b.events_for_round(1, 3.0)), 2)

    def test_clear_removes_old_session_events(self):
        b = PreRoundBuffer()
        b.add(ObservableEvent(100.0, "ws", "old", 1, None, 1, 0, 1, 1, 0, 1, None, {}))
        b.clear()
        self.assertEqual(b.events_for_round(1, 100.0), [])

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            PreRoundBuffer(0, 10)
        with self.assertRaises(ValueError):
            PreRoundBuffer(10, 0)


if __name__ == "__main__":
    unittest.main()
