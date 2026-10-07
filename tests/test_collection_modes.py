"""Collection modes (minimal / research / forensic), Spribe profile and Telegram format."""
import asyncio
import json
import unittest
from pathlib import Path

from aviator.collector import Collector
from aviator.config import load_config
from aviator.extract import (GENERIC_PROFILE, SPRIBE_AVIATOR_PROFILE, extract_fairness,
                             profile_for_game)
from aviator.fairness import cents_from_hash, round_hash, sha256_hex
from aviator.modes import (COLLECTION_MODES, DEFAULT_MODE, FORENSIC, MINIMAL, NOISE_COMMANDS,
                           RESEARCH, CollectionPolicy, resolve_mode)
from aviator.netlog import RotatingJsonlLog
from aviator.notify import Notifier, format_event, format_result
from aviator.sfs_codec import SfsDecoder
from aviator.tracker import IGNORED_COMMANDS
from tests.helpers import ROOT, TempProject

SERVER_SEED = "ServerSeedABCD1234"
PLAYER_SEEDS = ["clientAaaa111", "clientBbbb222", "clientCccc333"]
SPRIBE_EVIDENCE = {
    "roundId": 777,
    "serverSeed": SERVER_SEED,
    "playerSeeds": PLAYER_SEEDS,
    "serverSeedSHA256": sha256_hex(SERVER_SEED),
    "roundHashSHA512": round_hash(SERVER_SEED, PLAYER_SEEDS),
}


# ---- tiny test codec: frame = sequence of <2-byte len><json {"c": cmd, "p": params}> --
def _decode(buf):
    n = int.from_bytes(buf[:2], "big")
    return json.loads(buf[2:2 + n].decode("utf-8")), 2 + n


def _parse(obj):
    return obj["c"], obj["p"]


def make_frame(*packets):
    out = b""
    for cmd, params in packets:
        blob = json.dumps({"c": cmd, "p": params}).encode("utf-8")
        out += len(blob).to_bytes(2, "big") + blob
    return out


def make_collector(project):
    col = Collector(project.cfg)
    col.decoder = SfsDecoder(decode=_decode, parse=_parse)
    return col


def netlog_records(col):
    path = Path(col.netlog.path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def kinds(col):
    return [r["kind"] for r in netlog_records(col)]


def decoded_commands(col):
    return [r["command"] for r in netlog_records(col) if r["kind"] == "sfs_decoded"]


WS = "wss://example.invalid/BlueBox/websocket"
TRAFFIC = [
    ("heartbeat", {}),
    ("pingResponse", {}),
    ("updateCurrentBets", {"bets": [{"id": 1}]}),
    ("onlineplayers", {"n": 5}),
    ("chatMessage", {"text": "hi"}),
]


class ModeSelectionTests(unittest.TestCase):
    def test_default_is_research(self):
        self.assertEqual(DEFAULT_MODE, RESEARCH)
        self.assertEqual(resolve_mode(None), RESEARCH)
        p = TempProject()
        try:
            self.assertEqual(p.cfg.collection_mode, RESEARCH)
            self.assertEqual(p.store.collection_mode, RESEARCH)
        finally:
            p.cleanup()

    def test_all_modes_selectable_and_normalised(self):
        self.assertEqual(COLLECTION_MODES, (MINIMAL, RESEARCH, FORENSIC))
        for raw, expected in (("minimal", MINIMAL), (" Research ", RESEARCH), ("FORENSIC", FORENSIC)):
            p = TempProject(collection_mode=raw)
            try:
                self.assertEqual(p.cfg.collection_mode, expected)
                self.assertEqual(p.store.collection_mode, expected)
                # no global metadata: the mode is recorded per collector session instead
                self.assertIsNone(p.store.get_meta("collection_mode"))
                self.assertEqual(CollectionPolicy(p.cfg.collection_mode).mode, expected)
            finally:
                p.cleanup()

    def test_invalid_mode_fails_loudly(self):
        for bad in ("", "full", "Forensics", 3):
            with self.assertRaises(ValueError):
                resolve_mode(bad)
        with self.assertRaises(ValueError):
            TempProject(collection_mode="verbose")

    def test_shipped_config_json(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["collection_mode"], "research")
        self.assertEqual(cfg["game_id"], 52358)
        # null = "follow the collection mode" (tests/test_research_fixes.py checks the values)
        for key in ("network_log_max_bytes", "network_log_backups", "log_text_frames"):
            self.assertIn(key, cfg)
            self.assertIsNone(cfg[key], key)
        self.assertIn("thresholds", cfg)  # existing settings are kept
        self.assertEqual(load_config().collection_mode, RESEARCH)


class PolicyFilterTests(unittest.TestCase):
    def test_noise_covers_tracker_ignored_commands(self):
        self.assertTrue(IGNORED_COMMANDS <= NOISE_COMMANDS)
        for name in ("heartbeat", "ping", "pong"):
            self.assertIn(name, NOISE_COMMANDS)

    def test_minimal_filtering(self):
        pol = CollectionPolicy(MINIMAL, SPRIBE_AVIATOR_PROFILE)
        self.assertTrue(pol.keep_sfs_packet("roundChartInfo", {"roundId": 1, "maxMultiplier": 2.0}))
        self.assertFalse(pol.keep_sfs_packet("changeState", {"roundId": 1, "newStateId": 1}))
        self.assertFalse(pol.keep_sfs_packet("init", {"roundsInfo": []}))
        for cmd, params in TRAFFIC:
            self.assertFalse(pol.keep_sfs_packet(cmd, params), cmd)
        # fairness only when explicitly tied to a round
        self.assertTrue(pol.keep_sfs_packet("serverSeedResponse", SPRIBE_EVIDENCE))
        self.assertFalse(pol.keep_sfs_packet("serverSeedResponse", {"serverSeed": SERVER_SEED}))
        self.assertFalse(pol.keep_text_frame(json.dumps(SPRIBE_EVIDENCE), True))
        self.assertFalse(pol.keep_binary_frame([("roundChartInfo", {"roundId": 1})]))
        self.assertFalse(pol.keep_browser_event(("roundChartInfo", {"roundId": 1})))
        self.assertFalse(pol.store_provenance)
        self.assertFalse(pol.keep_record({"kind": "ws_open"}))
        self.assertFalse(pol.keep_record({"kind": "sfs_change_state"}))
        self.assertTrue(pol.keep_record({"kind": "sfs_decoded", "command": "roundChartInfo", "params": {}}))
        self.assertTrue(pol.keep_record({"kind": "tracker_error"}))  # integrity is never filtered

    def test_research_filtering(self):
        pol = CollectionPolicy(RESEARCH, SPRIBE_AVIATOR_PROFILE)
        for cmd in ("roundChartInfo", "changeState", "init", "serverSeedResponse"):
            self.assertTrue(pol.keep_sfs_packet(cmd, {"roundId": 1}), cmd)
        for cmd, params in TRAFFIC:
            self.assertFalse(pol.keep_sfs_packet(cmd, params), cmd)
        # any command carrying fairness evidence is kept
        self.assertTrue(pol.keep_sfs_packet("fairnessInfo", SPRIBE_EVIDENCE))
        self.assertTrue(pol.keep_sfs_packet("fairnessInfo", {"deep": {"serverSeed": SERVER_SEED}}))
        self.assertTrue(pol.keep_binary_frame([("heartbeat", {}), ("roundChartInfo", {"roundId": 1})]))
        self.assertFalse(pol.keep_binary_frame(TRAFFIC))
        self.assertTrue(pol.keep_browser_event(("changeState", {"roundId": 1})))
        self.assertFalse(pol.keep_browser_event(("heartbeat", {})))
        self.assertFalse(pol.keep_browser_event(None))
        self.assertTrue(pol.store_provenance)

    def test_research_text_and_http_frames(self):
        pol = CollectionPolicy(RESEARCH, SPRIBE_AVIATOR_PROFILE)
        self.assertFalse(pol.keep_text_frame("pong", False))
        self.assertFalse(pol.keep_text_frame("pong", True))            # log_text_frames cannot widen research
        self.assertFalse(pol.keep_text_frame('{"type":"ui","seedling":1}', True))
        self.assertTrue(pol.keep_text_frame(json.dumps(SPRIBE_EVIDENCE), False))
        self.assertFalse(pol.keep_http_response({"kind": "http_response", "status": 200}))
        self.assertTrue(pol.keep_http_response({"kind": "http_response", "body": json.dumps(SPRIBE_EVIDENCE)}))

    def test_research_record_kinds(self):
        pol = CollectionPolicy(RESEARCH, SPRIBE_AVIATOR_PROFILE)
        for kind in ("ws_open", "ws_close", "sfs_change_state", "sfs_init_backfill",
                     "tracker_error", "network_log_rotation", "browser_queue_overflow",
                     "sfs_decode_error", "ws_binary_undecoded", "some_new_error"):
            self.assertTrue(pol.keep_record({"kind": kind}), kind)
        for kind in ("pre_round_snapshot", "pre_round_buffer_overflow_x", "browser_event_unhandled",
                     "fairness_menu_not_found", "unknown_kind"):
            self.assertFalse(pol.keep_record({"kind": kind}), kind)
        self.assertFalse(pol.keep_record({"kind": "sfs_decoded", "command": "heartbeat", "params": {}}))
        self.assertFalse(pol.keep_record({"no": "kind"}))

    def test_forensic_preserves_everything(self):
        pol = CollectionPolicy(FORENSIC)
        for cmd, params in TRAFFIC:
            self.assertTrue(pol.keep_sfs_packet(cmd, params), cmd)
        self.assertTrue(pol.keep_binary_frame(TRAFFIC))
        self.assertTrue(pol.keep_binary_frame([]))
        self.assertTrue(pol.keep_browser_event(None))
        self.assertTrue(pol.keep_text_frame("pong", True))
        self.assertFalse(pol.keep_text_frame("pong", False))           # honours log_text_frames as before
        self.assertTrue(pol.keep_http_response({"kind": "http_response"}))
        for kind in ("pre_round_snapshot", "ws_open", "anything", "browser_event_unhandled"):
            self.assertTrue(pol.keep_record({"kind": kind}), kind)

    def test_netlog_filter_counts_and_fails_open(self):
        p = TempProject()
        try:
            log = RotatingJsonlLog(p.dir / "n.jsonl", record_filter=lambda r: r.get("keep", False))
            self.assertTrue(log.write({"kind": "x", "keep": False}))   # skipping is not a failure
            self.assertTrue(log.write({"kind": "x", "keep": True}))
            self.assertEqual((log.records_observed, log.records_persisted, log.records_filtered), (2, 1, 1))
            self.assertEqual(log.write_failures, 0)

            def boom(_record):
                raise RuntimeError("filter bug")
            bad = RotatingJsonlLog(p.dir / "m.jsonl", record_filter=boom)
            self.assertTrue(bad.write({"kind": "x"}))
            self.assertEqual(bad.records_persisted, 1)                 # fail open: evidence is kept
        finally:
            p.cleanup()


class CollectorModeTests(unittest.TestCase):
    def feed(self, col):
        """Round 10 betting, a flying/crash cycle, noise, and its result."""
        col.on_binary_frame(make_frame(*TRAFFIC), WS)
        col.on_binary_frame(make_frame(("changeState", {"roundId": 10, "newStateId": 1})), WS)
        col.on_binary_frame(make_frame(("updateCurrentBets", {"bets": []}),
                                       ("roundChartInfo", {"roundId": 9, "maxMultiplier": 2.5})), WS)
        col.on_text_frame("pong", WS)

    def test_research_mode_persists_only_relevant_traffic(self):
        p = TempProject(collection_mode="research")
        col = make_collector(p)
        try:
            self.feed(col)
            col.on_binary_frame(make_frame(("fairnessInfo", SPRIBE_EVIDENCE)), WS)
            self.assertEqual(decoded_commands(col), ["changeState", "roundChartInfo", "fairnessInfo"])
            self.assertEqual(kinds(col).count("ws_binary_frame"), 3)     # the traffic-only frame is skipped
            self.assertNotIn("ws_text", kinds(col))
            for noisy in ("heartbeat", "pingResponse", "updateCurrentBets", "onlineplayers", "chatMessage"):
                self.assertNotIn(noisy, json.dumps(netlog_records(col)))
            self.assertNotIn("pre_round_snapshot", kinds(col))
            # collection itself is unaffected
            self.assertEqual(col.store.get_round(9)["multiplier"], 2.5)
            self.assertEqual(col.tracker.current_round_id, 10)
            self.assertGreater(col.store.conn.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0], 0)
            self.assertIsNotNone(col.store.get_round(9)["raw_json"])
        finally:
            col.store.close()
            p.cleanup()

    def test_research_mode_text_frame_with_fairness_is_kept(self):
        p = TempProject(collection_mode="research", log_text_frames=False)
        col = make_collector(p)
        try:
            col.on_text_frame(json.dumps(SPRIBE_EVIDENCE), WS)
            col.on_text_frame("pong", WS)
            self.assertEqual(kinds(col), ["ws_text"])
        finally:
            col.store.close()
            p.cleanup()

    def test_research_mode_browser_events(self):
        p = TempProject(collection_mode="research")
        col = make_collector(p)
        try:
            col.on_browser_events([
                {"event_type": "x", "data": {"cmd": "heartbeat", "params": {}}},
                {"event_type": "x", "data": {"cmd": "roundChartInfo", "params": {"roundId": 4, "maxMultiplier": 1.2}}},
            ])
            self.assertEqual(kinds(col).count("browser_event"), 1)
            self.assertEqual(decoded_commands(col), [])
            self.assertEqual(col.store.get_round(4)["cents"], 120)
        finally:
            col.store.close()
            p.cleanup()

    def test_minimal_mode_stores_rounds_only(self):
        p = TempProject(collection_mode="minimal")
        col = make_collector(p)
        try:
            self.feed(col)
            col.on_binary_frame(make_frame(("serverSeedResponse", SPRIBE_EVIDENCE)), WS)
            col.on_binary_frame(make_frame(("serverSeedResponse", {"serverSeed": "OrphanSeed999"})), WS)
            self.assertEqual(decoded_commands(col), ["roundChartInfo", "serverSeedResponse"])
            self.assertNotIn("ws_binary_frame", kinds(col))
            self.assertNotIn("ws_text", kinds(col))
            row = col.store.get_round(9)
            self.assertEqual((row["multiplier"], row["source"]), (2.5, "sfs:roundChartInfo"))
            self.assertIsNone(row["raw_json"])
            # no provenance tables, explicit fairness only
            self.assertEqual(col.store.conn.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0], 0)
            ev = col.store.conn.execute("SELECT association, kind FROM fairness_evidence").fetchall()
            self.assertTrue(ev)
            self.assertEqual({r["association"] for r in ev}, {"explicit"})
        finally:
            col.store.close()
            p.cleanup()

    def test_forensic_mode_keeps_current_behaviour(self):
        p = TempProject(collection_mode="forensic", log_text_frames=True)
        col = make_collector(p)
        try:
            self.feed(col)
            self.assertEqual(kinds(col).count("ws_binary_frame"), 3)
            self.assertEqual(decoded_commands(col),
                             [c for c, _ in TRAFFIC] + ["changeState", "updateCurrentBets", "roundChartInfo"])
            self.assertIn("ws_text", kinds(col))
            self.assertIn("pre_round_snapshot", kinds(col))
            # frame evidence is written before its decoded packets
            ks = kinds(col)
            self.assertLess(ks.index("ws_binary_frame"), ks.index("sfs_decoded"))
            self.assertEqual(col.netlog.records_filtered, 0)
            self.assertGreater(col.store.conn.execute("SELECT COUNT(*) FROM evidence_observations").fetchone()[0], 0)
        finally:
            col.store.close()
            p.cleanup()

    def test_forensic_text_frames_still_follow_config_flag(self):
        p = TempProject(collection_mode="forensic", log_text_frames=False)
        col = make_collector(p)
        try:
            col.on_text_frame("pong", WS)
            self.assertEqual(kinds(col), [])
        finally:
            col.store.close()
            p.cleanup()


class SpribeProfileTests(unittest.TestCase):
    def only_record(self, data, profile=SPRIBE_AVIATOR_PROFILE):
        recs = [r for r in extract_fairness(data, profile=profile) if not r.scan_truncated]
        self.assertEqual(len(recs), 1)
        return recs[0]

    def test_profile_selection(self):
        self.assertIs(profile_for_game(52358), SPRIBE_AVIATOR_PROFILE)
        self.assertIs(profile_for_game("52358"), SPRIBE_AVIATOR_PROFILE)
        self.assertIs(profile_for_game(1), GENERIC_PROFILE)
        self.assertIs(profile_for_game(None), GENERIC_PROFILE)
        self.assertEqual(SPRIBE_AVIATOR_PROFILE.game_id, 52358)
        self.assertTrue(SPRIBE_AVIATOR_PROFILE.commitment_keys <= SPRIBE_AVIATOR_PROFILE.accepted_fields)
        self.assertTrue(SPRIBE_AVIATOR_PROFILE.round_hash_keys <= SPRIBE_AVIATOR_PROFILE.accepted_fields)

    def test_spribe_fields_extracted_and_digest_confirmed(self):
        rec = self.only_record(SPRIBE_EVIDENCE)
        self.assertEqual(rec.profile, "spribe_aviator")
        self.assertEqual(rec.round_id, 777)
        self.assertEqual(rec.server_seed, SERVER_SEED)
        self.assertEqual(rec.player_seeds, PLAYER_SEEDS)
        self.assertEqual(rec.commitment, sha256_hex(SERVER_SEED))
        self.assertEqual(rec.round_hash, round_hash(SERVER_SEED, PLAYER_SEEDS))
        types = {o["field_name"]: o["semantic_type"] for o in rec.crypto_observations}
        self.assertEqual(types, {"serverSeedSHA256": "commitment_sha256",
                                 "roundHashSHA512": "round_hash_sha512"})

    def test_alternate_field_names(self):
        rec = self.only_record({"roundId": 5, "revealedServerSeed": SERVER_SEED, "clientSeeds": PLAYER_SEEDS})
        self.assertEqual((rec.server_seed, rec.player_seeds), (SERVER_SEED, PLAYER_SEEDS))
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)

    def test_unknown_hashes_remain_observations(self):
        evidence = dict(SPRIBE_EVIDENCE, serverSeedSHA256="a" * 64, roundHashSHA512="b" * 128)
        rec = self.only_record(evidence)
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)
        self.assertEqual({o["semantic_type"] for o in rec.crypto_observations}, {"unknown"})
        self.assertEqual(len(rec.crypto_observations), 2)

    def test_hashes_without_seeds_are_not_confirmed(self):
        rec = self.only_record({"roundId": 5, "serverSeedSHA256": sha256_hex(SERVER_SEED),
                                "roundHashSHA512": "c" * 128})
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)
        self.assertEqual({o["semantic_type"] for o in rec.crypto_observations}, {"unknown"})

    def test_round_hash_needs_three_player_seeds(self):
        two = PLAYER_SEEDS[:2]
        rec = self.only_record({"roundId": 5, "serverSeed": SERVER_SEED, "playerSeeds": two,
                                "roundHashSHA512": round_hash(SERVER_SEED, two)})
        self.assertIsNone(rec.round_hash)

    def test_profile_does_not_globally_classify_hashes(self):
        digest = sha256_hex(SERVER_SEED)
        data = {"roundId": 5, "serverSeed": SERVER_SEED, "hashedServerSeed": digest,
                "combinedHash": "d" * 128, "hashSha512": "e" * 128}
        rec = self.only_record(data)
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)
        self.assertEqual({o["semantic_type"] for o in rec.crypto_observations}, {"unknown"})
        self.assertEqual(len(rec.crypto_observations), 3)

    def test_generic_profile_never_promotes(self):
        rec = self.only_record(SPRIBE_EVIDENCE, profile=GENERIC_PROFILE)
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)
        self.assertEqual({o["semantic_type"] for o in rec.crypto_observations}, {"unknown"})
        self.assertEqual(extract_fairness(SPRIBE_EVIDENCE)[0].profile, "generic")

    def test_tracker_stores_confirmed_and_unknown_separately_without_verifying(self):
        p = TempProject()
        try:
            from aviator.tracker import RoundTracker
            tr = RoundTracker(p.store, p.cfg)
            self.assertIs(tr.profile, SPRIBE_AVIATOR_PROFILE)
            digest = round_hash(SERVER_SEED, PLAYER_SEEDS)
            p.store.insert_round(777, cents_from_hash(digest) / 100, "sfs:roundChartInfo", notify=False)
            tr.handle("serverSeedResponse", dict(SPRIBE_EVIDENCE, nextServerSeedSHA256="f" * 64))
            ev = p.store.evidence_for_round(777)
            self.assertEqual(ev["commitment_sha256"], [sha256_hex(SERVER_SEED)])
            self.assertEqual(ev["round_hash_sha512"], [digest])
            obs = {r["field_name"]: r["semantic_type"] for r in p.store.conn.execute(
                "SELECT field_name, semantic_type FROM evidence_observations "
                "WHERE evidence_kind='cryptographic_observation'")}
            self.assertEqual(obs["nextServerSeedSHA256"], "unknown")
            self.assertEqual(obs["serverSeedSHA256"], "commitment_sha256")
            # the verification safety gate is unchanged: no false "verified"
            v = p.store.get_verification(777)
            self.assertEqual((v["status"], v["verified"]), ("not_verifiable", False))
        finally:
            p.cleanup()


class TelegramFormatTests(unittest.TestCase):
    PAYLOAD = {"round_id": 123456, "multiplier": 2.5, "source": "sfs:roundChartInfo"}

    def test_round_message_format(self):
        self.assertEqual(format_result(self.PAYLOAD), "ROUND 123456 -> 2.50x source=sfs:roundChartInfo")
        self.assertEqual(format_event("round_completed", self.PAYLOAD),
                         "ROUND 123456 -> 2.50x source=sfs:roundChartInfo")
        self.assertEqual(format_result({"round_id": 1, "multiplier": 1, "source": "backfill"}),
                         "ROUND 1 -> 1.00x source=backfill")

    def test_message_never_carries_hashes_or_debug(self):
        text = format_result(dict(self.PAYLOAD, source="a" * 64 + " Traceback (most recent call last)",
                                  server_seed=SERVER_SEED, hash="f" * 64, error="boom"))
        self.assertEqual(text, "ROUND 123456 -> 2.50x source=unknown")
        self.assertNotIn("Traceback", text)
        self.assertNotIn(SERVER_SEED, text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_only_round_messages_outside_forensic(self):
        prediction = {"round_id": 5, "output": {"models": {}, "thresholds": []}}
        fairness = {"round_id": 5, "status": "verified", "detail": "multiplier reproduced"}
        for mode in (MINIMAL, RESEARCH):
            self.assertIsNone(format_event("prediction_frozen", prediction, None, mode))
            self.assertIsNone(format_event("fairness_update", fairness, None, mode))
            self.assertIsNotNone(format_event("round_completed", self.PAYLOAD, None, mode))
        self.assertIsNotNone(format_event("prediction_frozen", prediction, None, FORENSIC))
        self.assertIsNotNone(format_event("fairness_update", fairness, None, FORENSIC))

    def test_notifier_sends_single_line_round_message(self):
        for mode, expect_extra in ((RESEARCH, 0), (FORENSIC, 1)):
            p = TempProject(collection_mode=mode)
            try:
                p.store.subscribe(1)
                p.clock.tick()
                p.store.insert_round(300, 3.14, "sfs:roundChartInfo", notify=True)
                p.store._outbox_insert("prediction:301", "prediction_frozen", 301,
                                       {"round_id": 301, "output": {"models": {}, "thresholds": []}})
                sent = []

                async def send(chat_id, text):
                    sent.append(text)
                asyncio.run(Notifier(p.store, send, p.cfg, clock=p.clock).deliver_pending())
                self.assertEqual(sent[0], "ROUND 300 -> 3.14x source=sfs:roundChartInfo")
                self.assertEqual(len(sent), 1 + expect_extra, mode)
            finally:
                p.cleanup()


if __name__ == "__main__":
    unittest.main()
