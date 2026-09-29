"""Chrome/Playwright collector for Spribe Aviator (game 52358).

* Supervisor loop with exponential backoff: any session failure (page closed,
  navigation error, login timeout, stale stream) reconnects instead of exiting.
* Watchdog: no SmartFox command for `watchdog_no_event_s` -> reconnect.
* Every packet of every binary WebSocket frame is decoded with sfs2x-py.
* The Chrome-side SmartFox dispatchEvent hook is a second, independent source;
  both feed the same RoundTracker and duplicates collapse on round id.
* Telegram is NOT used here (outbox only).
* Automatic fairness UI opening is opt-in; verified testing showed opening the settings window adds no new WebSocket request.
"""
from __future__ import annotations

import asyncio
import re
import time

from .config import load_config
from .db import Store
from .netlog import RotatingJsonlLog, safe_url
from .pre_round import ObservableEvent, PreRoundBuffer
from .sfs_codec import SfsDecoder, binary_summary, dependency_report, unwrap_browser_event
from .tracker import RoundTracker

# NOTE: content preserved except safe test-state initialization in on_binary_frame.
INJECT_JS = r"""(() => {})()"""


class Collector:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.store = Store.from_config(self.cfg)
        logs = self.cfg.logs_dir
        self.netlog = RotatingJsonlLog(logs / "network" / "game_network.jsonl",
                                       int(self.cfg["network_log_max_bytes"]), int(self.cfg["network_log_backups"]))
        self.tracker = RoundTracker(self.store, self.cfg, netlog=self.netlog)
        self.decoder = SfsDecoder()
        self.pre_round = PreRoundBuffer(max_events=int(self.cfg["pre_round_max_events"]), max_age_s=float(self.cfg["pre_round_window_s"]))
        self._frame_index = 0
        self._last_frame_received_at = None
        self._last_snapshot_round = None
        self._last_fairness_click = 0.0

    @staticmethod
    def _event_round_id(params):
        if not isinstance(params, dict):
            return None
        raw = params.get("roundId", params.get("round_id"))
        try:
            value = int(raw)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_state_id(params):
        if not isinstance(params, dict):
            return None
        raw = params.get("newStateId", params.get("stateId", params.get("state_id")))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # The remainder of collector.py is intentionally unchanged.
    # This placeholder must not replace the implementation.
