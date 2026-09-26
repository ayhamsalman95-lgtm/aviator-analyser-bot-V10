"""Configuration loading.

Secrets are NEVER read from config.json. The Telegram token comes only from the
TELEGRAM_BOT_TOKEN environment variable (optionally loaded from a local,
git-ignored `.env` file).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "game_id": 52358,
    "game_url": "https://1xlite-130003.top/ar/casino-search?game=52358",
    "site_home_url": "https://1xlite-130003.top/ar/",
    "aviator_frame_host": "aviator-next.spribegaming.com",
    "data_dir": "data",
    "logs_dir": "logs",
    "reports_dir": "reports",
    "chrome_profile_dir": "chrome_profile",
    "batch_size": 1000,
    "thresholds": [1.5, 2.0, 3.0, 5.0, 10.0],
    "min_history": 30,
    "recent_window": 200,
    "prior_strength": 20.0,
    "rtp": 0.97,
    "max_multiplier": 1_000_000.0,
    "fairness_autoclick": False,
    "fairness_autoclick_min_interval_s": 30.0,
    "watchdog_no_event_s": 180.0,
    "login_wait_s": 180.0,
    "reconnect_backoff_s": [5, 10, 20, 40, 80, 120],
    "network_log_max_bytes": 20_000_000,
    "network_log_backups": 5,
    "log_http_bodies": False,
    "log_text_frames": True,
    "admin_chat_ids": [],
    "telegram_poll_interval_s": 1.0,
    "telegram_max_attempts": 5,
    "telegram_event_max_age_s": 900,
    "bot_starts_collector": False,
}

PLACEHOLDER_TOKENS = {"", "YOUR_TELEGRAM_BOT_TOKEN", "PUT_NEW_BOT_TOKEN_HERE"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Never overrides real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    values: dict[str, Any]
    root: Path = field(default_factory=lambda: PROJECT_ROOT)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def path(self, key: str) -> Path:
        p = Path(self.values[key])
        return p if p.is_absolute() else self.root / p

    @property
    def data_dir(self) -> Path:
        return self.path("data_dir")

    @property
    def logs_dir(self) -> Path:
        return self.path("logs_dir")

    @property
    def reports_dir(self) -> Path:
        return self.path("reports_dir")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aviator.sqlite3"

    @property
    def structured_dir(self) -> Path:
        return self.data_dir / "structured"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    def telegram_token(self) -> str | None:
        _load_dotenv(self.root / ".env")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        return None if token in PLACEHOLDER_TOKENS else token


def load_config(path: str | Path | None = None, root: Path | None = None,
                overrides: dict[str, Any] | None = None) -> Config:
    root = root or PROJECT_ROOT
    cfg_path = Path(path) if path else root / "config.json"
    values = dict(DEFAULTS)
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config.json must contain a JSON object")
        # A token in config.json is ignored on purpose (secrets via env only).
        raw.pop("bot_token", None)
        values.update(raw)
    if overrides:
        values.update(overrides)
    return Config(values=values, root=root)
