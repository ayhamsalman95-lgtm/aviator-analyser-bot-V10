"""Collection modes and the evidence-filtering policy.

Three modes decide how much the collector persists:

``minimal``
    Completed rounds (round_id, multiplier, timestamp, source). Fairness data only
    when it is explicitly tied to a round. No provenance tables, no raw frames.
``research`` (default, recommended)
    Everything needed for statistical analysis of rounds plus conservative fairness
    evidence: roundChartInfo, changeState (round boundaries), init backfill,
    fairness-bearing frames and their provenance. Unrelated WebSocket traffic
    (heartbeats, pings, bet feeds, UI events, plain text frames) is not stored.
``forensic``
    The original behaviour: full evidence collection, nothing filtered.

All filtering decisions live in :class:`CollectionPolicy`; callers never compare
command names or record kinds themselves. Integrity signals (errors, overflows,
log rotation, undecodable frames) are persisted in every mode so that data loss
stays observable.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from .extract import GENERIC_PROFILE, FairnessProfile, extract_fairness, norm

MINIMAL = "minimal"
RESEARCH = "research"
FORENSIC = "forensic"
COLLECTION_MODES = (MINIMAL, RESEARCH, FORENSIC)
DEFAULT_MODE = RESEARCH


# Per-mode logging profile. These are the single source of truth for the settings that
# differ between modes; config.json leaves them null so the mode decides, and an explicit
# value in config.json (or an override) still wins. Forensic keeps the original defaults.
_RESEARCH_LOG_PROFILE = {"network_log_max_bytes": 5_000_000, "network_log_backups": 3,
                         "log_text_frames": False}
MODE_LOG_PROFILES: dict[str, dict[str, Any]] = {
    MINIMAL: dict(_RESEARCH_LOG_PROFILE),
    RESEARCH: dict(_RESEARCH_LOG_PROFILE),
    FORENSIC: {"network_log_max_bytes": 20_000_000, "network_log_backups": 5,
               "log_text_frames": True},
}
MODE_AWARE_KEYS = tuple(MODE_LOG_PROFILES[RESEARCH])


def mode_log_defaults(mode: Any) -> dict[str, Any]:
    """Logging defaults for ``mode`` (a copy; safe to mutate)."""
    return dict(MODE_LOG_PROFILES[resolve_mode(mode)])


def resolve_mode(value: Any) -> str:
    """Normalise a configured mode. ``None`` -> default; anything unknown raises."""
    if value is None:
        return DEFAULT_MODE
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in COLLECTION_MODES:
            return mode
    raise ValueError(f"invalid collection_mode {value!r}; expected one of {', '.join(COLLECTION_MODES)}")


# --- SFS command classes (compared after ``extract.norm``) -------------------
ROUND_RESULT_COMMANDS = frozenset({"roundchartinfo"})
LIFECYCLE_COMMANDS = frozenset({"changestate"})
REQUIRED_SFS_COMMANDS = frozenset({"init", "serverseedresponse"})
# Mirrors tracker.IGNORED_COMMANDS plus liveness traffic. Never persisted outside forensic.
NOISE_COMMANDS = frozenset({
    "updatecurrentbets", "updatecurrentcashouts", "updatecurrentcashout", "currentbetsinfo",
    "onlineplayers", "x", "pingresponse", "betsinfo",
    "heartbeat", "ping", "pong", "pongresponse",
})

# --- netlog record kinds -----------------------------------------------------
# Integrity / loss signals: kept in every mode.
INTEGRITY_KINDS = frozenset({
    "tracker_error", "frame_handler_error", "collector_error", "network_log_rotation",
    "browser_hook_error", "browser_queue_overflow", "browser_drain_error",
    "sfs_decode_error", "ws_binary_undecoded", "fairness_scan_truncated",
    "sfs_round_result_invalid", "ws_binary_decode_exception", "http_body_uninspected",
})
INTEGRITY_SUFFIXES = ("_error", "_failure", "_overflow", "_exception")
# Small records that mark connection gaps and round boundaries: research + forensic.
LIFECYCLE_KINDS = frozenset({
    "ws_open", "ws_close", "sfs_change_state", "sfs_change_state_no_round",
    "sfs_init_backfill", "sfs_init_without_roundsInfo", "sfs_fairness_response_empty",
})
# Records the collector has already judged on the COMPLETE frame/event (ws_text is stored
# truncated, so re-judging its payload here would wrongly drop long fairness frames).
PREDECIDED_KINDS = frozenset({"ws_binary_frame", "browser_event", "ws_text"})

_TEXT_PREFILTER = ("seed", "sha256", "sha512", "fairness", "provablyfair", "provably_fair")
_MAX_TEXT_SCAN_CHARS = 1_000_000

# HTTP bodies are inspected (never stored) in research mode to find fairness evidence.
HTTP_INSPECT_MAX_BYTES = 2_000_000
_HTTP_INSPECT_TYPES = ("json", "text/plain")
HTTP_BODY_INSPECT = "inspect"      # read the body and run the fairness detector
HTTP_BODY_OVERSIZED = "oversized"  # inspectable type but too large: record that it was not inspected
HTTP_BODY_SKIP = "skip"            # ordinary traffic: do not even read the body
# Keys that mark a fairness *structure* whose inner field names may be unknown to us.
_FAIRNESS_STRUCTURE_MARKERS = ("fairness", "provablyfair")


def _has_fairness_structure(data: Any, max_nodes: int = 500) -> bool:
    """True if a dict key names a fairness structure holding a non-empty object/list."""
    stack = [data]
    nodes = 0
    while stack and nodes < max_nodes:
        node = stack.pop()
        nodes += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    if value and any(m in norm(key) for m in _FAIRNESS_STRUCTURE_MARKERS):
                        return True
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(v for v in node[:500] if isinstance(v, (dict, list)))
    return False


class CollectionPolicy:
    def __init__(self, mode: str = DEFAULT_MODE, profile: Optional[FairnessProfile] = None):
        self.mode = resolve_mode(mode)
        self.profile = profile or GENERIC_PROFILE

    # -------------------------------------------------------------- properties
    @property
    def minimal(self) -> bool:
        return self.mode == MINIMAL

    @property
    def research(self) -> bool:
        return self.mode == RESEARCH

    @property
    def forensic(self) -> bool:
        return self.mode == FORENSIC

    @property
    def store_provenance(self) -> bool:
        """Provenance tables (evidence_observations) are populated in research/forensic only."""
        return self.mode != MINIMAL

    # --------------------------------------------------------------- evidence
    def has_fairness_evidence(self, data: Any, *, explicit_only: bool = False) -> bool:
        """True when ``data`` carries Spribe/profile fairness fields.

        ``explicit_only`` additionally requires core evidence (seeds or confirmed
        hashes) tied to a round id inside the same object. Without it, a named fairness
        structure (e.g. a ``fairness`` object) also counts, so evidence whose inner field
        names are not yet understood is still preserved as an observation.
        """
        if not isinstance(data, (dict, list)):
            return False
        for rec in extract_fairness(data, profile=self.profile):
            if rec.scan_truncated:
                continue
            core = bool(rec.server_seed or rec.player_seeds or rec.commitment or rec.round_hash)
            if explicit_only:
                if core and rec.round_id is not None and not rec.is_next_commitment:
                    return True
            elif core or rec.crypto_observations:
                return True
        return False if explicit_only else _has_fairness_structure(data)

    # ------------------------------------------------------------ SFS packets
    def keep_sfs_packet(self, command: Any, params: Any) -> bool:
        if self.forensic:
            return True
        name = norm(command) if isinstance(command, str) else ""
        if not name or name in NOISE_COMMANDS:
            return False
        if name in ROUND_RESULT_COMMANDS:
            return True
        if self.minimal:
            return self.has_fairness_evidence(params, explicit_only=True)
        if name in LIFECYCLE_COMMANDS or name in REQUIRED_SFS_COMMANDS:
            return True
        return self.has_fairness_evidence(params)

    def keep_binary_frame(self, packets: Iterable[tuple[Any, Any]]) -> bool:
        """Persist the raw frame only when it carries at least one relevant packet."""
        if self.forensic:
            return True
        if self.minimal:
            return False
        return any(self.keep_sfs_packet(cmd, params) for cmd, params in packets)

    def keep_browser_event(self, unwrapped: Optional[tuple[Any, Any]]) -> bool:
        if self.forensic:
            return True
        if self.minimal or unwrapped is None:
            return False
        return self.keep_sfs_packet(*unwrapped)

    # --------------------------------------------------------- text and HTTP
    def keep_text_frame(self, text: str, log_text_frames: bool = False) -> bool:
        """Text frames: forensic honours ``log_text_frames``; research keeps only
        frames that carry fairness evidence; minimal keeps none."""
        if self.forensic:
            return bool(log_text_frames)
        if self.minimal or not isinstance(text, str) or len(text) > _MAX_TEXT_SCAN_CHARS:
            return False
        lowered = text.lower()
        if not any(token in lowered for token in _TEXT_PREFILTER):
            return False
        return self._json_has_evidence(text)

    def http_body_plan(self, content_type: Any, content_length: Any, status: Any = 200) -> str:
        """Decide whether a response body must be read to look for fairness evidence.

        Only research mode inspects bodies on its own (forensic follows ``log_http_bodies``,
        minimal never reads them), and only for successful JSON/plain-text responses. This
        never enables general body logging: bodies that are not fairness-bearing are dropped.
        """
        if not self.research:
            return HTTP_BODY_SKIP
        try:
            ok = 200 <= int(status) < 300 and int(status) != 204
        except (TypeError, ValueError):
            ok = False
        kind = str(content_type or "").lower()
        if not ok or not any(t in kind for t in _HTTP_INSPECT_TYPES):
            return HTTP_BODY_SKIP
        try:
            if content_length is not None and int(content_length) > HTTP_INSPECT_MAX_BYTES:
                return HTTP_BODY_OVERSIZED
        except (TypeError, ValueError):
            pass
        return HTTP_BODY_INSPECT

    def keep_http_response(self, record: dict) -> bool:
        if self.forensic:
            return True
        if self.minimal:
            return False
        body = record.get("body")
        if not isinstance(body, str) or record.get("body_encoding") not in (None, "utf-8"):
            return False
        if len(body) > HTTP_INSPECT_MAX_BYTES:
            return False  # not inspectable; the collector records http_body_uninspected
        return self._json_has_evidence(body)

    def _json_has_evidence(self, text: str) -> bool:
        try:
            data = json.loads(text)
        except ValueError:
            return False
        return self.has_fairness_evidence(data)

    # ----------------------------------------------------------------- records
    def keep_record(self, record: dict) -> bool:
        """Single decision point used by the network log for every record."""
        if self.forensic:
            return True
        kind = record.get("kind")
        if not isinstance(kind, str):
            return False
        if kind in INTEGRITY_KINDS or kind.endswith(INTEGRITY_SUFFIXES):
            return True
        if kind == "sfs_decoded":
            return self.keep_sfs_packet(record.get("command"), record.get("params"))
        if self.minimal:
            return False
        if kind in LIFECYCLE_KINDS or kind in PREDECIDED_KINDS:
            return True
        if kind == "http_response":
            return self.keep_http_response(record)
        return False
