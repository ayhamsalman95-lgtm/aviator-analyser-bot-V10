# V12 implementation audit

Scope: Spribe Aviator, game 52358. Environment used: offline Linux sandbox, Python 3.12.
GitHub write access: **not available** (the connector is read-only), so the work
is delivered as a ZIP plus `APPLY_TO_GITHUB.md` (branch commands).

## Confirmed bugs -> resolution
| # | Bug | Resolution | Test |
|---|---|---|---|
| 1 | `serverSeedResponse` empty path `NameError` (`d.keys()`) | rewritten; logs `params.keys()` safely | `test_empty_server_seed_response_no_crash` |
| 2 | `init.roundsInfo` ignored | `RoundTracker._on_init` backfill, origin=backfill, unknown items quarantined | `test_full_replay` |
| 3-6 | seed evidence bound to `last_round_id` / previous round | association only via same-object `roundId`; otherwise `unassociated` (never used) | `test_full_replay`, `test_unassociated_never_used`, `test_next_commitment_with_round_id_is_not_bound` |
| 7-8 | `changeState` vs `roundChartInfo` state clash | current round driven only by `changeState` (monotonic); results never touch it | `test_full_replay` (current id stays 5000004) |
| 9-10 | bot misses betting phase (1.2 s polling) | prediction frozen inside the collector on `newStateId=1`, persisted + outbox | `test_predictions_frozen_and_resolved` |
| 11-12 | `/add` wiped by history rebuild | `manual_entries` table, no derived history | `test_manual_entries_never_touch_canonical`, `test_add_isolated` |
| 13-14 | `/clear` hardcodes `rounds.json` | clears manual entries only, needs `confirm`, `all` admin-only | `test_clear_*` |
| 15-16 | fragile fairness dedup | structured outbox keys + (event, chat) delivery rows | `test_fairness_updates_dedup_by_round_id_and_status` |
| 17-18 | no reconnect | supervisor with backoff + watchdog + per-packet error isolation | code review only (needs a browser) |
| 19-20 | fairness click log spam | off by default; event-driven, rate-limited, no control dumps | code review only |
| 21-22 | `merge_seed_state` keeps first wrong hash | append-only evidence; conflicts -> `conflict`; verified rows locked | `test_wrong_early_value_can_be_corrected`, `test_verified_is_immutable` |
| 23-24 | `short_ok` generic hash/seed mapping | explicit key whitelist only | `test_generic_hash_and_seed_ignored` |
| 25-26 | loose 32/128 hex extraction | removed | `test_generic_hash_and_seed_ignored` |
| 27-28 | one SFS packet per frame | loop on `consumed` | `test_every_packet_in_frame_is_decoded` |
| 29-30 | undecoded binary parsed as text | opaque summary only | `test_undecoded_binary_never_parsed_as_text` |
| 31-32 | `install_v11.bat` -> missing script | new `install.bat`; old name redirects | `test_installer_does_not_reference_missing_script` |
| 33-34 | seed count mismatch (3 vs 4) | exactly 3 player seeds required for verification | `test_two_seeds_incomplete` |
| 35-36 | untrained `LightLSTM` shown as model | removed; honest baselines only | `test_no_lstm_in_output` |
| 37-38 | runtime data tracked in repo | `.gitignore` + migration preserves evidence under `data/legacy_evidence` | `test_gitignore_excludes_sensitive_and_runtime` |
| 39-40 | HTTP responses all classified as fairness | only classify as http_fairness when response body contains actual fairness evidence (not HTML/JS/CSS) | `test_http_response_not_fairness_unless_contains_fairness` |

## What was verified by execution here
* Unit + replay tests (see TEST_RESULTS.txt).
* CLI smoke run: migrate -> 2,100 simulated live rounds through the tracker -> repair -> report
  (2 TXT batches created, 2,100 predictions frozen, 0 leak flags, integrity ok, WAL on).
* Fairness formula checked against a published worked example (hash prefix `f0fbffc79944c` -> 16.53x).

## What could NOT be verified here (and why)
* **Live collection** (Chrome, login, the 1xlite site): the sandbox has no internet and no browser session.
* **sfs2x-py on real traffic**: the package cannot be installed offline. Its API names/shapes
  (`decode_s2c_packet -> (dict, consumed)`, `parse_s2c_command -> (cmd, params)`) were matched to the
  published docs; `test_real_sfs2x_api_and_multi_packet_roundtrip` runs automatically once it is installed.
  If the Aviator session is AES-encrypted, Python decoding will fail and the Chrome SmartFox hook remains the source.
* **Telegram API**: exercised with a fake sender; real sending needs a token + network.
* **Your real rounds.json (272 KB)**: could not be copied into the sandbox. The migration was tested on a
  fixture built from the exact junk-record shape seen in the repo (`source="websocket"`, hex ids, keys a/c/p).
  Run `migrate.bat` locally; the summary is written to `data/migration_summary.json`.
* **`init.roundsInfo` field names**: assumed to mirror `roundChartInfo` (`roundId`, `maxMultiplier`);
  anything else is quarantined, not guessed. Check the quarantine after the first live run.
* **Hash -> multiplier mapping** is not officially published by Spribe; verification fails safe if it differs.
