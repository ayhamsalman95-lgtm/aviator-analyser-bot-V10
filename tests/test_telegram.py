import asyncio
import unittest

from tests.helpers import TempProject
from aviator import commands
from aviator.notify import Notifier, escape_md_v2


class Forbidden(Exception):
    pass


class FakeSender:
    def __init__(self, fail=(), forbidden=()):
        self.sent = []
        self.fail = set(fail)
        self.forbidden = set(forbidden)

    async def __call__(self, chat_id, text):
        if chat_id in self.forbidden:
            raise Forbidden("Forbidden: bot was blocked by the user")
        if chat_id in self.fail:
            raise TimeoutError("network down")
        self.sent.append((chat_id, text))


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject()
        self.s = self.p.store

    def tearDown(self):
        self.p.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_escape(self):
        self.assertEqual(escape_md_v2("1.5x (≥2) _a_ [b]!"), r"1\.5x \(≥2\) \_a\_ \[b\]\!")

    def test_failure_isolation_and_dedup(self):
        for c in (1, 2, 3):
            self.s.subscribe(c)
        self.p.clock.tick()
        self.s.insert_round(100, 2.0, "sfs:roundChartInfo")
        sender = FakeSender(fail={2}, forbidden={3})
        n = Notifier(self.s, sender, self.p.cfg, clock=self.p.clock)
        c = self.run_async(n.deliver_pending())
        self.assertEqual(c, {"sent": 1, "failed": 1, "skipped": 1})
        self.assertEqual([x[0] for x in sender.sent], [1])
        self.assertEqual([r["chat_id"] for r in self.s.active_subscribers()], [1, 2])  # 3 blocked -> removed
        # collection is unaffected by Telegram failures
        self.assertEqual(self.s.round_count(), 1)
        # retry later: chat 1 not re-sent (dedup), chat 2 retried after backoff
        sender.fail.clear()
        self.p.clock.tick(120)
        self.run_async(n.deliver_pending())
        self.assertEqual(sorted(x[0] for x in sender.sent), [1, 2])
        self.run_async(n.deliver_pending())
        self.assertEqual(len(sender.sent), 2)

    def test_fairness_updates_dedup_by_round_id_and_status(self):
        self.s.subscribe(1)
        self.p.clock.tick()
        self.s.insert_round(200, 2.0, "sfs:roundChartInfo", notify=False)
        self.s.add_fairness_evidence("server_seed", "Different1234567", "t", 200, "explicit")
        self.s.add_fairness_evidence("player_seeds", ["aaaa1111", "bbbb2222", "cccc3333"], "t", 200, "explicit")
        self.s.add_fairness_evidence("server_seed", "Different1234567", "t2", 200, "explicit")  # same value
        kinds = [r["event_key"] for r in self.s.conn.execute("SELECT event_key FROM outbox")]
        self.assertEqual(kinds, ["fairness:200:incomplete", "fairness:200:mismatch"])
        sender = FakeSender()
        self.run_async(Notifier(self.s, sender, self.p.cfg, clock=self.p.clock).deliver_pending())
        self.assertEqual(len(sender.sent), 1)   # 'incomplete' is silent, 'mismatch' delivered once

    def test_stale_prediction_not_sent(self):
        self.s.subscribe(1)
        self.p.clock.tick()
        self.s._outbox_insert("prediction:300", "prediction_frozen", 300,
                              {"round_id": 300, "output": {"models": {}, "thresholds": []}})
        self.s.insert_round(300, 1.5, "sfs:roundChartInfo", notify=False)
        sender = FakeSender()
        self.run_async(Notifier(self.s, sender, self.p.cfg, clock=self.p.clock).deliver_pending())
        self.assertEqual(sender.sent, [])

    def test_new_subscriber_gets_no_backlog(self):
        self.s.insert_round(400, 1.5, "sfs:roundChartInfo")
        self.p.clock.tick()
        self.s.subscribe(9)
        sender = FakeSender()
        self.run_async(Notifier(self.s, sender, self.p.cfg, clock=self.p.clock).deliver_pending())
        self.assertEqual(sender.sent, [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject()
        self.s = self.p.store
        self.s.insert_round(1, 2.0, "sfs:roundChartInfo")

    def tearDown(self):
        self.p.cleanup()

    def test_add_isolated(self):
        out = commands.cmd_add(self.s, self.p.cfg, 5, ["1.5", "2,4x", "abc", "0.5"])
        self.assertIn("2", out)
        self.assertEqual(self.s.manual_values(5), [1.5, 2.4])
        self.assertEqual(self.s.round_count(), 1)

    def test_clear_requires_confirm_and_never_touches_canonical(self):
        commands.cmd_add(self.s, self.p.cfg, 5, ["1.5"])
        commands.cmd_clear(self.s, self.p.cfg, 5, [])
        self.assertEqual(self.s.manual_count(5), 1)
        commands.cmd_clear(self.s, self.p.cfg, 5, ["confirm"])
        self.assertEqual(self.s.manual_count(5), 0)
        self.assertEqual(self.s.round_count(), 1)
        self.assertTrue(self.s.rounds_jsonl.exists())

    def test_clear_all_admin_only(self):
        commands.cmd_add(self.s, self.p.cfg, 5, ["1.5"])
        out = commands.cmd_clear(self.s, self.p.cfg, 6, ["confirm", "all"])
        self.assertEqual(self.s.manual_count(), 1)
        self.assertIn("admin", out)

    def test_other_commands_run(self):
        for name, fn in commands.COMMANDS.items():
            self.assertIsInstance(fn(self.s, self.p.cfg, 5, []), str, name)


if __name__ == "__main__":
    unittest.main()
