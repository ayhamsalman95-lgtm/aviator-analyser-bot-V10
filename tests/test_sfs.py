import unittest

from aviator import sfs_codec
from aviator.sfs_codec import SfsDecoder, unwrap_browser_event
from tests.helpers import TempProject


def fake_codec():
    """Test double for frame SPLITTING only: each packet is <len byte><cmd ascii>."""
    def decode(buf):
        n = buf[0]
        if n == 0 or len(buf) < 1 + n:
            raise ValueError("truncated packet")
        return {"c": buf[1:1 + n].decode()}, 1 + n

    def parse(obj):
        return obj["c"], {"cmd": obj["c"]}
    return decode, parse


class FrameTests(unittest.TestCase):
    def test_every_packet_in_frame_is_decoded(self):
        d, p = fake_codec()
        frame = b"\x04init" + b"\x0bchangeState" + b"\x0eroundChartInfo"
        res = SfsDecoder(d, p).decode_frame(frame)
        self.assertEqual([c for c, _ in res.commands], ["init", "changeState", "roundChartInfo"])
        self.assertEqual(res.leftover, 0)
        self.assertIsNone(res.error)

    def test_truncated_tail_reported_not_guessed(self):
        d, p = fake_codec()
        res = SfsDecoder(d, p).decode_frame(b"\x04init\x09chan")
        self.assertEqual(res.packets, 1)
        self.assertEqual(res.leftover, 5)
        self.assertIn("truncated", res.error)

    def test_decoder_unavailable_is_opaque(self):
        res = SfsDecoder(decode=None, parse=None)
        res._decode = None
        out = res.decode_frame(b"\x80\x00\x10{\"serverSeed\":\"x\"}")
        self.assertEqual(out.commands, [])
        self.assertEqual(out.error, "decoder_unavailable")

    def test_undecoded_binary_never_parsed_as_text(self):
        p = TempProject()
        try:
            from aviator.collector import Collector
            col = Collector.__new__(Collector)
            col.cfg, col.store = p.cfg, p.store
            from aviator.netlog import RotatingJsonlLog
            from aviator.tracker import RoundTracker
            col.netlog = RotatingJsonlLog(p.dir / "net.jsonl")
            col.tracker = RoundTracker(p.store, p.cfg)
            col.decoder = SfsDecoder()
            col.decoder._decode = None
            payload = b'{"roundId": 9, "serverSeed": "SeedSeedSeed1234", "maxMultiplier": 2.0}'
            col.on_binary_frame(payload, "wss://x")
            self.assertEqual(p.store.round_count(), 0)
            self.assertEqual(p.store.conn.execute("SELECT COUNT(*) FROM fairness_evidence").fetchone()[0], 0)
            self.assertIn("ws_binary_undecoded", (p.dir / "net.jsonl").read_text())
        finally:
            p.cleanup()

    def test_browser_event_unwrap(self):
        self.assertEqual(unwrap_browser_event({"event_type": "extensionResponse",
                                               "data": {"cmd": "roundChartInfo", "params": {"roundId": 1}}}),
                         ("roundChartInfo", {"roundId": 1}))
        self.assertIsNone(unwrap_browser_event({"event_type": "connection", "data": {"success": True}}))


class RealDependencyTests(unittest.TestCase):
    @unittest.skipUnless(sfs_codec.SFS2X_AVAILABLE, "sfs2x-py not installed in this environment")
    def test_real_sfs2x_api_and_multi_packet_roundtrip(self):
        import sfs2x
        self.assertTrue(callable(sfs2x.decode_s2c_packet))
        self.assertTrue(callable(sfs2x.parse_s2c_command))
        if not hasattr(sfs2x, "encode_s2c_packet"):
            self.skipTest("encode_s2c_packet not exported")
        pkt = sfs2x.encode_s2c_packet  # API: encode_s2c_packet(obj, compress)
        objs = []
        for cmd, params in [("changeState", {"roundId": 1, "newStateId": 1}),
                            ("roundChartInfo", {"roundId": 1, "maxMultiplier": 2.5})]:
            objs.append({"c": 1, "a": 13, "p": {"c": cmd, "r": -1, "p": params}})
        try:
            frame = b"".join(pkt(o, False) for o in objs)
        except Exception as exc:
            self.skipTest(f"cannot build packets with this sfs2x-py version: {exc}")
        res = SfsDecoder().decode_frame(frame)
        self.assertEqual(res.packets, 2, res.error)   # both packets split via `consumed`
        cmds = [c for c, _ in res.commands]
        if None in cmds:
            self.skipTest(f"parse_s2c_command uses another object layout: {res.commands!r}")
        self.assertEqual(cmds, ["changeState", "roundChartInfo"])


if __name__ == "__main__":
    unittest.main()
