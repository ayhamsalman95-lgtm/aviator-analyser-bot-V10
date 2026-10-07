"""Aviator (Spribe, game 52358) research collector + Telegram notifier.

Package layout
--------------
config      configuration + secrets (TELEGRAM_BOT_TOKEN from the environment)
db          SQLite (WAL) canonical storage + append-only JSONL mirrors
validation  strict completed-round validation
fairness    Spribe provably-fair verification (SHA-256 commitment, SHA-512 round hash)
extract     strict, explicit-key fairness parsing (no generic hash/seed guessing)
sfs_codec   SmartFoxServer 2X binary decoding via the `sfs2x-py` package
tracker     authoritative round state machine (changeState / roundChartInfo / init)
predict     frozen, leak-free baseline probability models
evaluate    chronological walk-forward evaluation (log loss, Brier, calibration)
stats       streaks, transitions, autocorrelation, runs test
reports     report generation
batches     TXT batch files every N valid rounds
netlog      rotating, redacted network log
notify      Telegram outbox delivery (library independent, failure isolated)
commands    Telegram command logic (library independent)
"""

__version__ = "12.0.0"
GAME_ID = 52358

# Install the optional browser Network API diagnostic before collector.py imports
# Playwright. It is isolated and failure-safe, so normal collection is unchanged.
try:
    from .browser_network_probe import install as _install_browser_network_probe
    _install_browser_network_probe()
except Exception:
    pass
