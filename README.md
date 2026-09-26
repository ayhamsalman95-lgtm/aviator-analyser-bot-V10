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
- Game ID and URLs
- Data directory locations
- Batch size (default: 1000 rounds)
- Prediction thresholds
- Fairness autoclick behavior

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
