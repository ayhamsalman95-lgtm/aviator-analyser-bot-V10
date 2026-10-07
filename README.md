# Aviator Telegram Analyzer V12

Production-ready provably-fair evidence collection and analysis system for Spribe Aviator game #52358.

## Features

- **SQLite Storage** with WAL mode for atomic, recoverable data storage
- **Automatic Batch Generation** of 1,000-round CSV files with all canonical fields
- **Fairness Tracking** with complete seed evidence and verification
- **Prediction Freezing** at game state changes to prevent lookahead bias
- **Telegram Notifications** for fairness updates and statistics
- **Network Evidence Extraction** tool to process game_network.jsonl files
- **Data Migration** from V8-V11 runtime files
- **Security** with sensitive data redaction and token isolation

## Quick Start

### Prerequisites
- Python 3.8 or higher
- Windows, Linux, or macOS

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# (Optional) Install development/test dependencies
pip install -r requirements-dev.txt

# Check environment
python scripts/check_env.py
```

### Running Tests

```bash
# Run all tests
python -m unittest discover tests -v

# Expected: 88 tests, 87 pass, 1 skip
```

### Configuration

Edit `config.json` to customize:
- Collection mode (`minimal`, `research`, `forensic`; see Collection Modes)
- Game ID and URLs
- Data directory locations
- Batch size (default: 1000 rounds)
- Prediction thresholds
- Fairness autoclick behavior

## Collection Modes

Set `"collection_mode"` in `config.json` (`minimal`, `research` or `forensic`; default `research`). An invalid value stops startup instead of silently collecting the wrong amount of data.

| Mode | Intended use | What is persisted |
|------|--------------|-------------------|
| `minimal` | Rounds only | Completed rounds (round_id, multiplier, timestamp, source). Fairness data only when explicitly tied to a round. No provenance tables, no raw frames. |
| `research` (**recommended**) | Statistical analysis of rounds (e.g. 5000 Aviator rounds) | `roundChartInfo` results, `changeState` round boundaries, `init` backfill, `serverSeedResponse`, fairness-bearing frames, connection open/close markers, and provenance (round_id, source, frame reference). |
| `forensic` | Investigations only | Everything, as before: raw binary frames, all decoded packets, text frames (per `log_text_frames`), pre-round snapshots. |

**Research mode does not store** heartbeats, ping/pong, bet and cash-out feeds, other game events, UI events, or text frames that carry no fairness evidence. `log_text_frames` does not widen research mode. Errors, queue overflows, log rotation and undecodable frames are persisted in every mode so data loss stays visible.

### Per-mode logging defaults

`network_log_max_bytes`, `network_log_backups` and `log_text_frames` are `null` in `config.json`, which means "use the selected mode's default". The profiles live in one place (`MODE_LOG_PROFILES` in `aviator/modes.py`):

| Mode | `network_log_max_bytes` | `network_log_backups` | `log_text_frames` |
|------|-------------------------|-----------------------|-------------------|
| `minimal` | 5,000,000 | 3 | false |
| `research` | 5,000,000 | 3 | false |
| `forensic` | 20,000,000 | 5 | true |

Switching to `forensic` therefore needs no other config edits. A number or boolean written in `config.json` still overrides the mode default. In research mode `log_text_frames` does not widen collection; text frames are stored only when they carry fairness evidence. Forensic mode still writes each raw binary frame before decoding it.

### Decode failures and HTTP evidence in research mode

- Irrelevant frames are dropped only **after** a successful decode. If the decoder raises, research and minimal mode store a `ws_binary_decode_exception` record with the raw frame, its hash, `frame_id` and `frame_index`. Partial decodes, malformed frames and an unavailable decoder are already recorded as `sfs_decode_error` / `ws_binary_undecoded` with the raw payload.
- Ordinary HTTP traffic is ignored. Successful JSON/plain-text responses are inspected without being stored (`log_http_bodies` stays off), and a response is persisted, body included, only when it contains fairness fields (`serverSeed`, `revealedServerSeed`, `playerSeeds`, `clientSeeds`, `serverSeedSHA256`, `roundHashSHA512`, other known hash fields, or a `fairness` object). Hash fields stay unclassified observations unless their digest relationship is proven. A JSON body that cannot be inspected (over 2 MB or unreadable) leaves an `http_body_uninspected` record.

### Collection mode metadata

The database does not keep a global "current mode", because the bot, scripts and tests can open it too. Each collector session instead writes an append-only `meta` row `collection_session:<session_id>` with its mode, start time and the highest `rounds.seq` at that moment (`Store.collection_sessions()` lists them). Rounds with a larger `seq` were collected during or after that session. The schema version and tables are unchanged.

Existing databases stay compatible: no tables are removed, and the `evidence_observations` provenance table is simply left unpopulated in `minimal` mode.

Use `research` day to day. Switch to `forensic` only while investigating a specific problem, because it writes far more data.

### Fairness evidence (Spribe Aviator, game 52358)

For game 52358 the collector reads `serverSeed`, `revealedServerSeed`, `playerSeeds`, `clientSeeds`, `serverSeedSHA256` and `roundHashSHA512`. Hash fields are never classified by name alone: `serverSeedSHA256` is recorded as a commitment only when it equals SHA-256 of the `serverSeed` in the same object, and `roundHashSHA512` as a round hash only when it equals SHA-512 of the server seed plus the first three player seeds. Every other hash stays an unclassified observation. The verification gate is unchanged, so no round is reported as verified on the strength of field names.

Fairness verification needs evidence that the client can actually see. If the game never sends seeds to the browser, rounds are stored but cannot be verified.

### Telegram

Telegram output is one line per completed round: `ROUND <round_id> -> <multiplier>x source=<source>`. It never includes hashes, debug data, raw evidence or exception text. Prediction and fairness pushes are sent only in `forensic` mode.

### What the collector is not

The collector does **not** predict future rounds. It records completed rounds and the evidence around them so they can be analysed afterwards. Nothing here gives an edge over the game.

### Environment Variables

```bash
# Set Telegram bot token (REQUIRED for Telegram features)
export TELEGRAM_BOT_TOKEN="your:token:here"

# Optional: Set legacy data directory for migration
export LEGACY_DATA_DIR="/path/to/legacy"
```

### Usage

#### Start Telegram Bot
```bash
python -m aviator.telegram
```
Or on Windows:
```batch
run_bot.bat
```

#### Start Game Collector
```bash
python collector.py
```
Or on Windows:
```batch
run_collector.bat
```

#### Migrate Legacy Data (V8-V11)
```bash
python scripts/migrate_legacy.py
```
Or on Windows:
```batch
migrate.bat
```

#### Extract Network Evidence
```bash
python tools/extract_network_evidence.py game_network.jsonl
```
Or on Windows:
```batch
extract_network_evidence.bat C:\path\to\game_network.jsonl
```

#### Generate Report
```bash
python scripts/report.py
```
Or on Windows:
```batch
report.bat
```

## Architecture

### Canonical Storage
All game data is stored in SQLite under `data/rounds.db`:
- **rounds** table: Game round results with fairness evidence
- **pending_evidence** table: Fairness evidence awaiting verification
- **verified** table: Cryptographically verified fairness records

### Batch Format
Every 1,000 rounds, a TXT batch file is generated with:
- All 8 canonical fields (round_id, multiplier, cents, source, origin, etc.)
- Complete raw JSON evidence (no truncation)
- Complete fairness evidence where available
- Complete prediction output where available

Example: `data/batches/batch_1.txt`

### Data Migration
Legacy V8-V11 runtime files are migrated to SQLite with full evidence preservation:
- Original files copied to `data/legacy_evidence/` with SHA-256 manifest
- Canonical rounds imported and deduplicated
- Junk records (hex IDs, invalid data) quarantined
- Process is fully idempotent

## Testing

Complete test suite with 88 tests covering:
- Fairness extraction and verification
- Round state machine
- Prediction freezing and leakage detection
- Data migration and repair
- Batch generation and integrity
- SFS protocol codec
- Telegram integration
- Storage and concurrency
- Network evidence extraction
- Security and dependency management

Run tests:
```bash
python -m unittest discover tests -v
```

## Security

- **No tokens in code**: Telegram token must be set via `TELEGRAM_BOT_TOKEN` environment variable
- **No credentials in Git**: `.gitignore` excludes sensitive files
- **Sensitive data redaction**: Passwords, phone numbers, and other PII are redacted before storage
- **Atomic transactions**: SQLite WAL mode ensures no partial writes
- **Evidence immutability**: Verified fairness records cannot be changed

## Documentation

- `IMPLEMENTATION_AUDIT.md` - Complete audit of all 40 bugs fixed
- `V12_CORRECTIONS_APPLIED.md` - Detailed corrections and test coverage
- `HTTP_FAIRNESS_CLASSIFICATION_FIX.md` - HTTP response classification fix
- `TEST_RESULTS.txt` - Full test results
- `README_AR.md` - Arabic documentation

## Limitations

- sfs2x-py is optional (gracefully skipped if not installed)
- Network collector requires Chrome and account login
- Telegram features require bot token and network access
- SQLite limits to ~2.1 billion rounds (practical limit not reached)

## Version

V12 - Complete production-ready implementation

All 38 originally identified bugs fixed:
- ✅ Data truncation eliminated
- ✅ Payload preservation complete
- ✅ HTTP response classification corrected
- ✅ Fairness evidence exact-round association
- ✅ Zero data loss

## License

See LICENSE file for terms.

## Support

For issues or questions, review the documentation files or check the test cases for usage examples.
