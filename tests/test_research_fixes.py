"""Review fixes for the collection modes: per-mode defaults, decode-failure evidence,
HTTP fairness retention in research mode and session-specific mode metadata."""
import asyncio
import hashlib
import json
import unittest
from pathlib import Path

from aviator.collector import Collector
from aviator.config import load_config
from aviator.extract import SPRIBE_AVIATOR_PROFILE, extract_fairness
from aviator.fairness import round_hash, sha256_hex
from aviator.modes import (FORENSIC, HTTP_BODY_INSPECT, HTTP_BODY_OVERSIZED, HTTP_BODY_SKIP,
                           HTTP_INSPECT_MAX_BYTES, MINIMAL, MODE_LOG_PROFILES, RESEARCH,
                           CollectionPolicy, mode_log_defaults)
from aviator.sfs_codec import SfsDecoder
from tests.helpers import ROOT, TempProject
from tests.test_collection_modes import (PLAYER_SEEDS, SERVER_SEED, SPRIBE_EVIDENCE, TRAFFIC, WS,
                                         _decode, _parse, decoded_commands, kinds, make_collector,
                                         make_frame, netlog_records)
from tools.extract_network_evidence import NetworkExtractor


def run(coro):
    return asyncio.run(coro)


def close(col, project):
    col.store.close()
    project.cleanup()


# ======================================================================== 1. defaults
class ModeDefaultsTests(unittest.TestCase):
    def test_profiles_are_the_single_source_of_truth(self):
        self.assertEqual(MODE_LOG_PROFILES[RESEARCH],
                         {"network_log_max_bytes": 5_000_000, "network_log_backups": 3,
                          "log_text_frames": False})
        # forensic keeps the ORIGINAL pre-research defaults
        self.assertEqual(MODE_LOG_PROFILES[FORENSIC],
                         {"network_log_max_bytes": 20_000_000, "network_log_backups": 5,
                          "log_text_frames": True})
        self.assertEqual(MODE_LOG_PROFILES[MINIMAL], MODE_LOG_PROFILES[RESEARCH])
        copy = mode_log_defaults(RESEARCH)
        copy["network_log_backups"] = 99
        self.assertEqual(mode_log_defaults(RESEARCH)["network_log_backups"], 3)  # callers get a copy

    def check_cfg(self, mode, size, backups, text, **overrides):
        p = TempProject(collection_mode=mode, **overrides)
        try:
            self.assertEqual(p.cfg["network_log_max_bytes"], size)
            self.assertEqual(p.cfg["network_log_backups"], backups)
            self.assertIs(p.cfg["log_text_frames"], text)
        finally:
            p.cleanup()

    def test_research_uses_research_defaults(self):
        self.check_cfg(RESEARCH, 5_000_000, 3, False)

    def test_forensic_uses_forensic_defaults_without_manual_overrides(self):
        self.check_cfg(FORENSIC, 20_000_000, 5, True)

    def test_minimal_remains_minimal(self):
        self.check_cfg(MINIMAL, 5_000_000, 3, False)

    def test_shipped_config_follows_the_selected_mode(self):
        shipped = load_config()
        self.assertEqual((shipped.collection_mode, shipped["network_log_max_bytes"],
                          shipped["network_log_backups"], shipped["log_text_frames"]),
                         (RESEARCH, 5_000_000, 3, False))
        forensic = load_config(overrides={"collection_mode": "forensic"})
        self.assertEqual((forensic["network_log_max_bytes"], forensic["network_log_backups"],
                          forensic["log_text_frames"]), (20_000_000, 5, True))
        self.assertEqual(shipped["game_id"], 52358)

    def test_explicit_values_still_win(self):
        self.check_cfg(FORENSIC, 1_234_567, 9, False, network_log_max_bytes=1_234_567,
                       network_log_backups=9, log_text_frames=False)
        self.check_cfg(RESEARCH, 5_000_000, 3, True, log_text_frames=True)
        self.check_cfg(RESEARCH, 5_000_000, 3, False, log_text_frames=None)  # null = mode default
        # an explicit 0 is a value, not "unset"
        self.check_cfg(FORENSIC, 20_000_000, 0, True, network_log_backups=0)

    def test_collector_applies_mode_defaults_to_the_network_log(self):
        for mode, size, backups in ((RESEARCH, 5_000_000, 3), (MINIMAL, 5_000_000, 3),
                                    (FORENSIC, 20_000_000, 5)):
            p = TempProject(collection_mode=mode)
            col = make_collector(p)
            try:
                self.assertEqual((col.netlog.max_bytes, col.netlog.backups), (size, backups), mode)
            finally:
                close(col, p)

    def test_forensic_not_reduced_with_default_config(self):
        """Default forensic config (no overrides) still logs text, noise and raw frames."""
        p = TempProject(collection_mode=FORENSIC)
        col = make_collector(p)
        try:
            col.on_text_frame("pong", WS)
            col.on_binary_frame(make_frame(*TRAFFIC), WS)
            col.on_binary_frame(make_frame(("changeState", {"roundId": 10, "newStateId": 1})), WS)
            ks = kinds(col)
            self.assertIn("ws_text", ks)
            self.assertEqual(ks.count("ws_binary_frame"), 2)
            self.assertEqual(decoded_commands(col),
                             [c for c, _ in TRAFFIC] + ["changeState"])
            self.assertIn("pre_round_snapshot", ks)
            self.assertEqual(col.netlog.records_filtered, 0)
        finally:
            close(col, p)

    def test_forensic_persists_raw_frame_before_decoding(self):
        p = TempProject(collection_mode=FORENSIC)
        col = make_collector(p)

        class Spy:
            seen = None

            def __init__(self, inner, path):
                self.inner, self.path = inner, path

            def decode_frame(self, data):
                text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
                Spy.seen = "ws_binary_frame" in text
                return self.inner.decode_frame(data)
        try:
            col.decoder = Spy(col.decoder, Path(col.netlog.path))
            col.on_binary_frame(make_frame(("heartbeat", {})), WS)
            self.assertTrue(Spy.seen, "forensic must write the raw frame before decode_frame runs")
        finally:
            close(col, p)

    def test_minimal_stays_minimal_even_with_text_flag(self):
        p = TempProject(collection_mode=MINIMAL, log_text_frames=True)
        col = make_collector(p)
        try:
            col.on_text_frame(json.dumps(SPRIBE_EVIDENCE), WS)
            col.on_binary_frame(make_frame(*TRAFFIC), WS)
            col.on_binary_frame(make_frame(("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.0})), WS)
            self.assertEqual(kinds(col), ["sfs_decoded"])
            self.assertEqual(decoded_commands(col), ["roundChartInfo"])
        finally:
            close(col, p)


# ============================================================ 2. decode failure evidence
class RaisingDecoder:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("decoder blew up")

    def decode_frame(self, data):
        raise self.exc


class DecodeFailureTests(unittest.TestCase):
    def only(self, col, kind):
        recs = [r for r in netlog_records(col) if r["kind"] == kind]
        self.assertEqual(len(recs), 1, (kind, kinds(col)))
        return recs[0]

    def test_decoder_success_research(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            col.on_binary_frame(make_frame(("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.5})), WS)
            self.assertEqual(kinds(col), ["ws_binary_frame", "sfs_decoded"])
            self.assertEqual(col.store.get_round(9)["cents"], 250)
        finally:
            close(col, p)

    def test_decoder_exception_preserves_frame_in_research_and_minimal(self):
        for mode in (RESEARCH, MINIMAL):
            p = TempProject(collection_mode=mode)
            col = make_collector(p)
            try:
                col.decoder = RaisingDecoder(ValueError("bad zstd block"))
                data = b"\x80\x01\x02irrelevant-or-not"
                col.on_binary_frame(data, WS)                        # must not raise
                rec = self.only(col, "ws_binary_decode_exception")
                self.assertEqual(rec["payload_b64"], __import__("base64").b64encode(data).decode(), mode)
                self.assertEqual(rec["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(rec["byte_length"], len(data))
                self.assertEqual((rec["error_type"], rec["error"]), ("ValueError", "bad zstd block"))
                self.assertEqual(rec["frame_index"], 1)
                self.assertEqual(rec["frame_id"], f"{col.session_id}:frame:1")
                self.assertEqual(rec["session_id"], col.session_id)
                self.assertTrue(rec["event_id"].startswith(col.session_id))
                self.assertTrue(rec["payload_complete"])
                self.assertNotIn("ws_binary_frame", kinds(col))     # no duplicate/regular frame record
                # frame numbering continues after the failure
                col.decoder = SfsDecoder(decode=_decode, parse=_parse)
                col.on_binary_frame(make_frame(("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.0})), WS)
                if mode == RESEARCH:
                    self.assertEqual(self.only(col, "sfs_decoded")["frame_index"], 2)
            finally:
                close(col, p)

    def test_forensic_exception_keeps_raw_frame_and_propagates_as_before(self):
        p = TempProject(collection_mode=FORENSIC)
        col = make_collector(p)
        try:
            col.decoder = RaisingDecoder()
            data = b"\x80\x01\x02forensic-frame"
            with self.assertRaises(RuntimeError):
                col.on_binary_frame(data, WS)
            rec = self.only(col, "ws_binary_frame")                # raw evidence written before decoding
            self.assertEqual(__import__("base64").b64decode(rec["payload_b64"]), data)
            self.assertNotIn("ws_binary_decode_exception", kinds(col))   # forensic flow unchanged
        finally:
            close(col, p)

    def test_malformed_frame_remains_observable_in_research(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            data = (500).to_bytes(2, "big") + b"{"                 # length prefix larger than the data
            col.on_binary_frame(data, WS)
            rec = self.only(col, "sfs_decode_error")
            self.assertEqual(__import__("base64").b64decode(rec["payload_b64"]), data)
            self.assertEqual(rec["frame_id"], f"{col.session_id}:frame:1")
            self.assertEqual(rec["frame_index"], 1)
            self.assertIn("decode", rec["error_stage"])
            self.assertEqual(decoded_commands(col), [])
            self.assertNotIn("ws_binary_frame", kinds(col))
        finally:
            close(col, p)

    def test_good_packet_followed_by_garbage_keeps_both(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            good = make_frame(("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.5}))
            garbage = (500).to_bytes(2, "big") + b"{"
            col.on_binary_frame(good + garbage, WS)
            ks = kinds(col)
            self.assertIn("ws_binary_frame", ks)
            err = self.only(col, "sfs_decode_error")
            self.assertEqual(err["remaining_bytes"], len(garbage))
            self.assertEqual(col.store.get_round(9)["cents"], 250)
        finally:
            close(col, p)

    def test_unavailable_decoder_preserves_payload(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            col.decoder = SfsDecoder(decode=None, parse=None)
            col.decoder._decode = col.decoder._parse = None
            data = b"\x80\x01\x02raw"
            col.on_binary_frame(data, WS)
            rec = self.only(col, "ws_binary_undecoded")
            self.assertEqual(__import__("base64").b64decode(rec["payload_b64"]), data)
        finally:
            close(col, p)

    def test_policy_bug_fails_open(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            def boom(_packets):
                raise RuntimeError("policy bug")
            col.policy.keep_binary_frame = boom
            col.on_binary_frame(make_frame(*TRAFFIC), WS)
            self.assertEqual(kinds(col).count("ws_binary_frame"), 1)
        finally:
            close(col, p)

    def test_irrelevant_frames_are_still_filtered(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            col.on_binary_frame(make_frame(*TRAFFIC), WS)
            self.assertEqual(kinds(col), [])                        # no failure -> nothing stored
        finally:
            close(col, p)


class LongTextFrameTests(unittest.TestCase):
    def test_long_fairness_text_frame_is_not_dropped_by_the_log_filter(self):
        """ws_text payloads are stored truncated; the log must not re-judge the cut JSON."""
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            frame = json.dumps(dict(SPRIBE_EVIDENCE, padding="x" * 30_000))
            col.on_text_frame(frame, WS)
            col.on_text_frame("pong" + "y" * 30_000, WS)            # long but irrelevant
            recs = [r for r in netlog_records(col) if r["kind"] == "ws_text"]
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["truncated"])
            self.assertEqual(recs[0]["payload_sha256"], hashlib.sha256(frame.encode()).hexdigest())
        finally:
            close(col, p)


# ============================================================== 3. HTTP fairness evidence
class FakeResp:
    def __init__(self, url, body=b"", status=200, ctype="application/json", length=None, fail=False):
        self.url = url
        self.status = status
        self.headers = {"content-type": ctype}
        if length is not None:
            self.headers["content-length"] = str(length)
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.fail = fail
        self.body_calls = 0

    async def body(self):
        self.body_calls += 1
        if self.fail:
            raise RuntimeError("body unavailable")
        return self._body


ORDINARY = {"config": {"theme": "dark", "currency": "USD"}, "hash": "a" * 64, "roundTimer": 12}
UNKNOWN_HASH_EVIDENCE = {"roundId": 778, "serverSeed": SERVER_SEED, "playerSeeds": PLAYER_SEEDS,
                         "serverSeedSHA256": "a" * 64, "roundHashSHA512": "b" * 128}


class HttpFairnessTests(unittest.TestCase):
    def http_records(self, col):
        return [r for r in netlog_records(col) if r["kind"] == "http_response"]

    def test_plan_for_response_types(self):
        research = CollectionPolicy(RESEARCH)
        self.assertEqual(research.http_body_plan("application/json; charset=utf-8", None, 200), HTTP_BODY_INSPECT)
        self.assertEqual(research.http_body_plan("text/plain", "10", 200), HTTP_BODY_INSPECT)
        for ctype in ("image/png", "text/html", "application/javascript", "video/mp4", None):
            self.assertEqual(research.http_body_plan(ctype, "10", 200), HTTP_BODY_SKIP, ctype)
        for status in (204, 301, 304, 404, 500, "x"):
            self.assertEqual(research.http_body_plan("application/json", "10", status), HTTP_BODY_SKIP, status)
        self.assertEqual(research.http_body_plan("application/json", HTTP_INSPECT_MAX_BYTES + 1, 200),
                         HTTP_BODY_OVERSIZED)
        # forensic follows log_http_bodies, minimal never reads bodies
        for mode in (FORENSIC, MINIMAL):
            self.assertEqual(CollectionPolicy(mode).http_body_plan("application/json", "10", 200), HTTP_BODY_SKIP)

    def test_ordinary_http_is_filtered(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            for resp in (FakeResp("https://x.invalid/api/config", ORDINARY),
                         FakeResp("https://x.invalid/a.png", b"\x89PNG", ctype="image/png"),
                         FakeResp("https://x.invalid/page", b"<html>seed</html>", ctype="text/html"),
                         FakeResp("https://x.invalid/missing", ORDINARY, status=404),
                         FakeResp("https://x.invalid/ok", {"fairness": True, "hash": "c" * 64})):
                run(col.on_http_response(resp))
            self.assertEqual(self.http_records(col), [])
            self.assertEqual(netlog_records(col), [])               # and nothing else was written
            self.assertFalse(col.cfg["log_http_bodies"])            # body logging was NOT enabled
        finally:
            close(col, p)

    def test_non_inspectable_responses_are_not_even_read(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            skipped = [FakeResp("https://x.invalid/a.png", b"x", ctype="image/png"),
                       FakeResp("https://x.invalid/app.js", b"x", ctype="application/javascript"),
                       FakeResp("https://x.invalid/404", ORDINARY, status=404)]
            for resp in skipped:
                run(col.on_http_response(resp))
            self.assertEqual([r.body_calls for r in skipped], [0, 0, 0])
        finally:
            close(col, p)

    def test_valid_spribe_evidence_survives_research_filter(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            run(col.on_http_response(FakeResp("https://x.invalid/api/fairness?token=SECRET", SPRIBE_EVIDENCE)))
            recs = self.http_records(col)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["status"], 200)
            self.assertNotIn("SECRET", rec["url"])                  # URL redaction unchanged
            self.assertEqual(rec["body_encoding"], "utf-8")
            body = json.loads(rec["body"])
            self.assertEqual(body, SPRIBE_EVIDENCE)
            fr = extract_fairness(body, profile=SPRIBE_AVIATOR_PROFILE)[0]
            self.assertEqual(fr.round_id, 777)
            self.assertEqual(fr.commitment, sha256_hex(SERVER_SEED))
            self.assertEqual(fr.round_hash, round_hash(SERVER_SEED, PLAYER_SEEDS))
            self.assertFalse(col.cfg["log_http_bodies"])
        finally:
            close(col, p)

    def test_each_named_fairness_field_is_retained(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            bodies = [{"roundId": 1, "serverSeed": SERVER_SEED},
                      {"roundId": 2, "revealedServerSeed": SERVER_SEED},
                      {"roundId": 3, "playerSeeds": PLAYER_SEEDS},
                      {"roundId": 4, "clientSeeds": PLAYER_SEEDS},
                      {"roundId": 5, "serverSeedSHA256": "d" * 64},
                      {"roundId": 6, "roundHashSHA512": "e" * 128},
                      {"data": {"fairness": {"state": "opaque"}}},
                      {"rounds": [{"roundId": 7, "serverSeed": SERVER_SEED}]}]
            for i, body in enumerate(bodies):
                run(col.on_http_response(FakeResp(f"https://x.invalid/{i}", body)))
            self.assertEqual(len(self.http_records(col)), len(bodies))
        finally:
            close(col, p)

    def test_unknown_hashes_are_retained_but_never_promoted(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            run(col.on_http_response(FakeResp("https://x.invalid/f", UNKNOWN_HASH_EVIDENCE)))
            rec = self.http_records(col)[0]
            body = json.loads(rec["body"])
            fr = extract_fairness(body, profile=SPRIBE_AVIATOR_PROFILE)[0]
            self.assertIsNone(fr.commitment)
            self.assertIsNone(fr.round_hash)
            self.assertEqual({o["semantic_type"] for o in fr.crypto_observations}, {"unknown"})
            # the offline extractor stays conservative as well
            out = Path(col.netlog.path).with_name("derived.jsonl")
            NetworkExtractor(Path(col.netlog.path), out).extract()
            derived = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
            fair = [d for d in derived if d.get("classification") == "fairness_evidence"]
            self.assertTrue(fair)
            self.assertEqual({o["semantic_type"] for d in fair for o in d["crypto_observations"]}, {"unknown"})
        finally:
            close(col, p)

    def test_oversized_and_unreadable_json_bodies_are_recorded_not_silently_lost(self):
        p = TempProject(collection_mode=RESEARCH)
        col = make_collector(p)
        try:
            big = FakeResp("https://x.invalid/big", ORDINARY, length=HTTP_INSPECT_MAX_BYTES + 1)
            run(col.on_http_response(big))
            self.assertEqual(big.body_calls, 0)
            broken = FakeResp("https://x.invalid/broken", ORDINARY, fail=True)
            run(col.on_http_response(broken))
            recs = [r for r in netlog_records(col) if r["kind"] == "http_body_uninspected"]
            self.assertEqual([r["reason"] for r in recs], ["oversized", "body_read_failed"])
            self.assertIn("body unavailable", recs[1]["error"])
            self.assertEqual(self.http_records(col), [])
        finally:
            close(col, p)

    def test_minimal_never_reads_or_stores_http(self):
        p = TempProject(collection_mode=MINIMAL)
        col = make_collector(p)
        try:
            resp = FakeResp("https://x.invalid/f", SPRIBE_EVIDENCE)
            run(col.on_http_response(resp))
            self.assertEqual(resp.body_calls, 0)
            self.assertEqual(netlog_records(col), [])
        finally:
            close(col, p)

    def test_forensic_http_behaviour_unchanged(self):
        # default: no bodies, every response recorded
        p = TempProject(collection_mode=FORENSIC)
        col = make_collector(p)
        try:
            a, b = FakeResp("https://x.invalid/a", ORDINARY), FakeResp("https://x.invalid/b", SPRIBE_EVIDENCE)
            run(col.on_http_response(a))
            run(col.on_http_response(b))
            recs = self.http_records(col)
            self.assertEqual(len(recs), 2)
            self.assertEqual((a.body_calls, b.body_calls), (0, 0))
            self.assertTrue(all("body" not in r for r in recs))
        finally:
            close(col, p)
        # log_http_bodies=True still stores every body, ordinary ones included
        p = TempProject(collection_mode=FORENSIC, log_http_bodies=True)
        col = make_collector(p)
        try:
            run(col.on_http_response(FakeResp("https://x.invalid/a", ORDINARY)))
            rec = self.http_records(col)[0]
            self.assertEqual(json.loads(rec["body"]), ORDINARY)
        finally:
            close(col, p)

    def test_research_with_http_bodies_enabled_still_drops_ordinary(self):
        p = TempProject(collection_mode=RESEARCH, log_http_bodies=True)
        col = make_collector(p)
        try:
            run(col.on_http_response(FakeResp("https://x.invalid/a", ORDINARY)))
            run(col.on_http_response(FakeResp("https://x.invalid/b", SPRIBE_EVIDENCE)))
            self.assertEqual(len(self.http_records(col)), 1)
        finally:
            close(col, p)


# ========================================================== 4. session-specific metadata
class SessionMetadataTests(unittest.TestCase):
    def test_store_open_never_writes_a_global_mode(self):
        p = TempProject(collection_mode=FORENSIC)
        try:
            self.assertIsNone(p.store.get_meta("collection_mode"))
            other = load_config(path=p.dir / "none.json", root=p.dir, overrides={"collection_mode": "minimal"})
            from aviator.db import Store
            second = Store.from_config(other)                       # e.g. a script or the bot
            self.assertEqual(second.collection_mode, MINIMAL)       # affects only its own writes
            self.assertIsNone(second.get_meta("collection_mode"))
            self.assertEqual(second.collection_sessions(), [])
            second.close()
        finally:
            p.cleanup()

    def test_each_collector_session_records_its_own_mode(self):
        p = TempProject(collection_mode=RESEARCH)
        a = make_collector(p)
        try:
            a.store.insert_round(1, 2.0, "sfs:roundChartInfo", notify=False)
            a.store.insert_round(2, 3.0, "sfs:roundChartInfo", notify=False)
            p.clock.tick(60)
            cfg_b = load_config(path=p.dir / "none.json", root=p.dir, overrides={"collection_mode": "forensic"})
            b = Collector(cfg_b)
            b.store.clock = p.clock
            try:
                sessions = b.store.collection_sessions()
                self.assertEqual([s["mode"] for s in sessions], [RESEARCH, FORENSIC])
                self.assertEqual([s["session_id"] for s in sessions], [a.session_id, b.session_id])
                self.assertEqual([s["start_round_seq"] for s in sessions], [0, 2])
                # the earlier (research) session is not rewritten by the later forensic one
                self.assertEqual(a.store.collection_sessions()[0]["mode"], RESEARCH)
            finally:
                b.store.close()
        finally:
            close(a, p)

    def test_recording_is_append_only(self):
        p = TempProject(collection_mode=RESEARCH)
        try:
            s = p.store
            self.assertTrue(s.record_collection_session("sess-1", RESEARCH))
            p.clock.tick(5)
            self.assertFalse(s.record_collection_session("sess-1", FORENSIC))   # no overwrite
            self.assertEqual([x["mode"] for x in s.collection_sessions()], [RESEARCH])
            self.assertEqual(s.collection_sessions()[0]["started_at"], 1_700_000_000.0)
            with self.assertRaises(ValueError):
                s.record_collection_session("sess-2", "bogus")
            # survives reopening
            self.assertEqual([x["session_id"] for x in p.reopen().collection_sessions()], ["sess-1"])
        finally:
            p.cleanup()

    def test_schema_version_and_tables_unchanged(self):
        from aviator.db import SCHEMA_VERSION
        p = TempProject()
        try:
            self.assertEqual(SCHEMA_VERSION, 3)
            self.assertEqual(p.store.get_meta("schema_version"), "3")
            tables = {r[0] for r in p.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ("rounds", "fairness_evidence", "fairness_verification", "predictions",
                         "evidence_observations", "quarantine", "outbox", "deliveries", "meta"):
                self.assertIn(name, tables)
        finally:
            p.cleanup()


# ======================================================== 5. cross-mode regression matrix
class RegressionMatrixTests(unittest.TestCase):
    def feed(self, col):
        col.on_binary_frame(make_frame(*TRAFFIC), WS)
        col.on_binary_frame(make_frame(("changeState", {"roundId": 10, "newStateId": 1})), WS)
        col.on_binary_frame(make_frame(("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.5})), WS)
        col.on_binary_frame(make_frame(("serverSeedResponse", SPRIBE_EVIDENCE)), WS)
        col.on_text_frame("pong", WS)
        col.on_text_frame(json.dumps(SPRIBE_EVIDENCE), WS)
        run(col.on_http_response(FakeResp("https://x.invalid/ordinary", ORDINARY)))
        run(col.on_http_response(FakeResp("https://x.invalid/fair", SPRIBE_EVIDENCE)))

    def summary(self, mode, **overrides):
        p = TempProject(collection_mode=mode, **overrides)
        col = make_collector(p)
        try:
            self.feed(col)
            recs = netlog_records(col)
            return {
                "commands": decoded_commands(col),
                "frames": kinds(col).count("ws_binary_frame"),
                "text": kinds(col).count("ws_text"),
                "http": [("body" in r) for r in recs if r["kind"] == "http_response"],
                "snapshot": "pre_round_snapshot" in kinds(col),
                "round": col.store.get_round(9)["cents"],
                "fairness_rows": col.store.conn.execute("SELECT COUNT(*) FROM fairness_evidence").fetchone()[0],
            }
        finally:
            close(col, p)

    def test_research(self):
        s = self.summary(RESEARCH)
        self.assertEqual(s["commands"], ["changeState", "roundChartInfo", "serverSeedResponse"])
        self.assertEqual(s["frames"], 3)                     # noise-only frame skipped
        self.assertEqual(s["text"], 1)                       # only the fairness-bearing text frame
        self.assertEqual(s["http"], [True])                  # ordinary filtered, fairness kept with body
        self.assertFalse(s["snapshot"])
        self.assertEqual(s["round"], 250)
        self.assertGreater(s["fairness_rows"], 0)

    def test_forensic(self):
        s = self.summary(FORENSIC)
        self.assertEqual(s["commands"], [c for c, _ in TRAFFIC] +
                         ["changeState", "roundChartInfo", "serverSeedResponse"])
        self.assertEqual(s["frames"], 4)
        self.assertEqual(s["text"], 2)                       # text frames enabled by forensic defaults
        self.assertEqual(s["http"], [False, False])          # every response, no bodies (as before)
        self.assertTrue(s["snapshot"])
        self.assertEqual(s["round"], 250)

    def test_minimal(self):
        s = self.summary(MINIMAL)
        self.assertEqual(s["commands"], ["roundChartInfo", "serverSeedResponse"])
        self.assertEqual(s["frames"], 0)
        self.assertEqual(s["text"], 0)
        self.assertEqual(s["http"], [])
        self.assertFalse(s["snapshot"])
        self.assertEqual(s["round"], 250)
        self.assertGreater(s["fairness_rows"], 0)            # explicitly associated evidence only


if __name__ == "__main__":
    unittest.main()
