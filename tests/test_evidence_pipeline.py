import base64
import hashlib
import json
import unittest
from pathlib import Path

from aviator.collector import Collector, INJECT_JS
from aviator.extract import extract_fairness
from tools.extract_network_evidence import NetworkExtractor
from aviator.netlog import RotatingJsonlLog
from aviator.sfs_codec import SfsDecoder
from tests.helpers import TempProject


def fake_codec():
    def decode(buf):
        n = buf[0]
        if n == 0 or len(buf) < 1 + n:
            raise ValueError("truncated packet")
        return {"c": buf[1:1 + n].decode()}, 1 + n

    def parse(obj):
        return obj["c"], {"roundId": 1}

    return decode, parse


class EvidencePipelineTests(unittest.TestCase):
    def test_consumed_rejects_bool_and_out_of_bounds(self):
        for value in (True, False, 99):
            decoder = SfsDecoder(
                decode=lambda _buf, value=value: ({"c": "x"}, value),
                parse=lambda obj: ("x", obj),
            )
            result = decoder.decode_frame(b"abc")
            self.assertEqual(result.packets, 0)
            self.assertIn("consumed", result.error)

    def test_packet_limit_preserves_remainder_metadata(self):
        d, p = fake_codec()
        result = SfsDecoder(d, p, max_packets=1).decode_frame(
            b"\x01a\x01b"
        )
        self.assertEqual(result.packets, 1)
        self.assertTrue(result.packet_limit_reached)
        self.assertEqual(result.leftover, 2)
        self.assertEqual(
            result.remaining_sha256,
            hashlib.sha256(b"\x01b").hexdigest(),
        )
        self.assertEqual(
            base64.b64decode(result.remaining_b64),
            b"\x01b",
        )

    def test_raw_binary_frame_round_trip(self):
        p = TempProject()
        try:
            col = Collector.__new__(Collector)
            col.cfg, col.store = p.cfg, p.store
            col.netlog = RotatingJsonlLog(p.dir / "net.jsonl")
            col.tracker = __import__("aviator.tracker", fromlist=["RoundTracker"]).RoundTracker(p.store, p.cfg)
            col.decoder = SfsDecoder(decode=None, parse=None)
            col.decoder._decode = None
            col.decoder._parse = None
            col._frame_index = 0
            col._last_frame_received_at = None
            col._last_snapshot_round = None
            col.pre_round = __import__("aviator.pre_round", fromlist=["PreRoundBuffer"]).PreRoundBuffer(
                max_events=int(p.cfg["pre_round_max_events"]), max_age_s=float(p.cfg["pre_round_window_s"])
            )
            col._event_index = 0
            col.session_id = "test-session"
            col.collector_run_id = "test-run"
            payload = b"\x80\x01\x02real-binary-evidence"
            col.on_binary_frame(payload, "wss://example.invalid/BlueBox/websocket")
            row = json.loads((p.dir / "net.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(base64.b64decode(row["payload_b64"]), payload)
            self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(row["byte_length"], len(payload))
            self.assertTrue(row["payload_complete"])
        finally:
            p.cleanup()

    def test_seedsha256_is_observation_not_round_hash(self):
        records = extract_fairness({
            "roundId": 123,
            "seedSHA256": "a" * 64,
        })
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].round_hash)
        self.assertEqual(records[0].crypto_observations[0]["semantic_type"], "unknown")

    def test_browser_snapshot_weakset_is_per_snapshot(self):
        self.assertIn("const snap = (v, d = 0, seen = new WeakSet())", INJECT_JS)
        self.assertNotIn("const seen = new WeakSet();", INJECT_JS)

    def test_browser_queue_overflow_contains_sequence_gap_metadata(self):
        self.assertIn("first_dropped_sequence", INJECT_JS)
        self.assertIn("last_dropped_sequence", INJECT_JS)
        self.assertIn("raw_browser_event_queue_not_recoverable", INJECT_JS)

    def test_rotation_is_observable(self):
        p = TempProject()
        try:
            log = RotatingJsonlLog(p.dir / "network.jsonl", max_bytes=80, backups=1)
            log.write({"kind": "first", "payload": "x" * 40})
            log.write({"kind": "second", "payload": "y" * 40})
            self.assertGreaterEqual(log.rotation_events, 1)
            lines = (p.dir / "network.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(json.loads(x).get("kind") == "network_log_rotation" for x in lines))
        finally:
            p.cleanup()


    def test_rotation_does_not_invent_record_drop_count(self):
        p = TempProject()
        try:
            log = RotatingJsonlLog(p.dir / "network.jsonl", max_bytes=80, backups=1)
            log.write({"kind": "first", "payload": "x" * 40})
            log.write({"kind": "second", "payload": "y" * 40})
            log.write({"kind": "third", "payload": "z" * 40})
            self.assertEqual(log.records_dropped, 0)
            rotation_rows = []
            for path in p.dir.glob("network.jsonl*"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("kind") == "network_log_rotation":
                        rotation_rows.append(row)
            self.assertTrue(rotation_rows)
            self.assertIn("old_file_size", rotation_rows[0])
            self.assertIn("evidence_loss", rotation_rows[0])
        finally:
            p.cleanup()

    def test_collector_to_jsonl_to_extractor_integration(self):
        p = TempProject()
        try:
            col = Collector.__new__(Collector)
            col.cfg, col.store = p.cfg, p.store
            col.netlog = RotatingJsonlLog(p.dir / "game_network.jsonl")
            col.tracker = __import__("aviator.tracker", fromlist=["RoundTracker"]).RoundTracker(p.store, p.cfg)
            col.decoder = SfsDecoder(
                decode=lambda _buf: ({"c": "init"}, 1),
                parse=lambda obj: ("init", {"roundsInfo": []}),
            )
            col._frame_index = 0
            col._last_frame_received_at = None
            col._last_snapshot_round = None
            col.pre_round = __import__("aviator.pre_round", fromlist=["PreRoundBuffer"]).PreRoundBuffer(
                max_events=int(p.cfg["pre_round_max_events"]), max_age_s=float(p.cfg["pre_round_window_s"])
            )
            col._event_index = 0
            col.session_id = "integration-session"
            col.collector_run_id = "integration-run"
            col.on_binary_frame(b"\x00", "wss://example.invalid/BlueBox/websocket")
            output = p.dir / "derived.jsonl"
            stats = NetworkExtractor(p.dir / "game_network.jsonl", output).extract()
            self.assertGreaterEqual(stats["written"], 2)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["source_file"] == str(p.dir / "game_network.jsonl") for row in rows))
            self.assertTrue(all(row["extraction_run_id"] for row in rows))
            self.assertTrue(any(row["source_kind"] == "ws_binary_frame" for row in rows))
        finally:
            p.cleanup()

    def test_extraction_run_isolated_and_timestamp_provenance(self):
        p = TempProject()
        try:
            source = p.dir / "game_network.jsonl"
            output = p.dir / "derived.jsonl"
            source.write_text(
                json.dumps({"kind": "ws_text", "received_at": 123.5, "timestamp": 99.0, "payload": "x"}) + "\n",
                encoding="utf-8",
            )
            first = NetworkExtractor(source, output)
            first_stats = first.extract()
            first_rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(first_stats["output_complete"])
            self.assertEqual(len(first_rows), 1)
            self.assertEqual(first_rows[0]["event_timestamp"], 123.5)
            self.assertEqual(first_rows[0]["timestamp_provenance"], "collector_received_at")
            self.assertEqual(first_rows[0]["timestamp"], 99.0)
            first_id = first_rows[0]["extraction_run_id"]

            second = NetworkExtractor(source, output)
            second.extract()
            second_rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(second_rows), 1)
            self.assertNotEqual(second_rows[0]["extraction_run_id"], first_id)
        finally:
            p.cleanup()

    def test_null_command_and_plain_init_are_preserved(self):
        p = TempProject()
        try:
            source = p.dir / "network.jsonl"
            output = p.dir / "derived.jsonl"
            source.write_text(
                "\n".join([
                    json.dumps({"kind": "sfs_decoded", "command": None, "params": {"x": 1}, "received_at": 1.0}),
                    json.dumps({"kind": "sfs_decoded", "command": "init", "params": {"foo": "bar"}, "received_at": 2.0}),
                ]) + "\n",
                encoding="utf-8",
            )
            NetworkExtractor(source, output).extract()
            rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
            null_rows = [r for r in rows if r.get("command") is None]
            init_rows = [r for r in rows if r.get("classification") == "init"]
            self.assertTrue(null_rows)
            self.assertTrue(init_rows)
            self.assertEqual(init_rows[0]["complete_params"], {"foo": "bar"})
        finally:
            p.cleanup()

    def test_http_fairness_classification_counted_once(self):
        p = TempProject()
        try:
            source = p.dir / "http.jsonl"
            output = p.dir / "derived.jsonl"
            source.write_text(json.dumps({
                "kind": "http_response",
                "received_at": 10.0,
                "body": json.dumps({"seedSHA256": "a" * 64}),
                "url": "https://example.invalid/fairness",
                "status": 200,
            }) + "\n", encoding="utf-8")
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines()]
            http_rows = [r for r in rows if r.get("classification") == "http_fairness"]
            self.assertEqual(len(http_rows), 1)
            self.assertEqual(stats["classification_http_fairness"], 1)
        finally:
            p.cleanup()

    def test_evidence_observation_preserves_provenance_and_duplicates(self):
        p = TempProject()
        try:
            provenance = {
                "received_at": 12.5,
                "received_monotonic": 4.25,
                "timestamp_provenance": "collector_received_at",
                "session_id": "s",
                "collector_run_id": "r",
                "event_id": "e",
                "source_file": "game_network.jsonl",
                "source_line": 7,
                "frame_id": "f",
                "frame_index": 3,
                "packet_index": 2,
                "packet_offset": 10,
                "packet_end": 20,
            }
            first = p.store.add_crypto_observation(
                round_id=123, association="explicit", field_name="seedSHA256",
                value="a" * 64, algorithm="SHA-256", semantic_type="unknown",
                source="sfs", provenance=provenance,
            )
            second = p.store.add_crypto_observation(
                round_id=123, association="explicit", field_name="seedSHA256",
                value="a" * 64, algorithm="SHA-256", semantic_type="unknown",
                source="browser", provenance={**provenance, "event_id": "e2"},
            )
            self.assertNotEqual(first, second)
            rows = p.store.conn.execute(
                "SELECT source,event_id,frame_id,packet_index,timestamp_provenance FROM evidence_observations ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["event_id"], "e")
            self.assertEqual(rows[1]["event_id"], "e2")
        finally:
            p.cleanup()

    def test_db_has_evidence_observations(self):
        p = TempProject()
        try:
            row = p.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_observations'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            p.cleanup()


if __name__ == "__main__":
    unittest.main()
