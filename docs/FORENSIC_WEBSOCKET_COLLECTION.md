# Forensic WebSocket Evidence Collection (Spribe Aviator)

## Purpose

Collect the complete browser-visible WebSocket evidence for the configured Aviator session, preserve original frames for later decoding, and keep round/fairness conclusions separate from raw evidence.

## Configuration

`config.json` uses `"collection_mode": "forensic"`. In this mode the collector does not apply the research-mode relevance filter to network records. Restart the collector after changing the mode; an already-running process keeps its in-memory policy.

For the two-tab direct-launch workflow, set `AVIATOR_GAME_URL` locally to a fresh HTTPS launch URL on `launch.spribegaming.com`. Never put that credential-bearing URL in `config.json`, source control, logs, or shared diagnostics. The collector opens the configured operator site, opens Aviator in its operator tab, waits 20 seconds, then creates a separate capture tab. `AVIATOR_LAUNCH_DELAY_SECONDS` optionally changes the delay (allowed range 0–300 seconds; default 20).

Example shape only (not a real URL or credential):

`AVIATOR_GAME_URL='https://launch.spribegaming.com/aviator?...' python collector.py`

The second tab's request/response/frame/WebSocket listeners are attached before navigating it to the direct URL. The primary forensic WebSocket stream is scoped to this second tab. If `AVIATOR_GAME_URL` is absent, the collector retains a backward-compatible mode that captures the operator game tab instead.

Default rotation settings are currently 20,000,000 bytes per network log file with 5 backups. This is a bounded rolling log, not an unlimited archive. Each rotation writes a `network_log_rotation` record; when an old backup is discarded, the record marks `evidence_loss: true`.

## Evidence layout

- `logs/network/game_network.jsonl`: active network evidence stream.
- `logs/network/game_network.jsonl.1` through `.5`: rotated network evidence files.
- `logs/network/fairness_ui.jsonl`: visible UI snapshots from the fairness settings extractor, when that tool is run.
- `data/`: structured round/tracker database files as configured by the application.
- `reports/`: generated analysis reports; reports are derived results and do not replace raw evidence.

Keep raw logs immutable after a collection run. Copy them to a separate archive before analysis, and analyze copies whenever possible.

## Record families

| Record kind | Purpose |
| --- | --- |
| `ws_open`, `ws_close` | Connection lifecycle and gaps |
| `ws_binary_frame` | Complete incoming binary frame, Base64 payload, frame ID/index, timestamp, size and summary |
| `ws_text` | Incoming text frame when enabled by the collection mode |
| `ws_frame_sent`, `ws_text_frame_sent` | Outgoing frames observed by the browser collector |
| `sfs_decoded` | Decoded SmartFox command and parameters, linked to frame/packet provenance |
| `ws_binary_undecoded`, `ws_binary_decode_exception`, `sfs_decode_error` | Evidence retained when decoding is unavailable or incomplete |
| `frame_handler_error`, `tracker_error`, `network_log_rotation` | Collection errors, integrity signals and log-rotation/loss indicators |
| `pre_round_snapshot`, round records | Round context and tracker-derived evidence, where available |
| `fairness_ui_evidence` | Visible values captured from the user's own Provably Fair Settings UI |

Record names are a guide; use the actual `kind` value in each JSONL record as the source of truth.

## Correlation rules

1. Preserve the original frame record and its `frame_id`/event provenance.
2. Link decoded packets to their parent frame through `frame_id`, and use `packet_index`, `packet_offset`, and `packet_end` where present.
3. Link a fairness observation to a round only when a round ID or an explicit, timestamped evidence path supports the link. Otherwise label the association as approximate or unresolved.
4. Treat UI-observed Client Seed and hash values as account-settings evidence. Do not label other players' seeds as the user's Client Seed.
5. A timestamp is the collector's observation time unless the record explicitly documents a source timestamp. It may bound when a value changed without proving the exact instant of change.
6. Never infer that a hash change means the Client Seed changed; compare each displayed field independently.

## Sensitive data and safe handling

Forensic payloads may contain session-specific or account-related data even when URL query parameters are redacted. Keep `logs/`, `data/`, browser profiles, cookies, tokens and live payloads local and private. Do not commit collected logs or session tokens to GitHub or paste them into public issues. Share only a sanitized excerpt if a review is needed.

## Run checklist

1. Confirm that the configured operator account is already authenticated, and provide a fresh local-only direct Spribe launch URL when using the two-tab workflow.
2. Start the collector. Verify the operator tab opens first, then the configured delay elapses, then the separate direct-Spribe capture tab opens and reports observed WebSocket events.
3. Confirm that `ws_open` and subsequent frame records appear in `logs/network/game_network.jsonl`.
4. Confirm that both incoming and outgoing events are represented when they occur, and that undecodable frames/errors are retained.
5. Watch disk usage and rotation markers during long captures. Copy rotated logs to a separate archive before the bounded backup window overwrites them.
6. Stop the run cleanly and preserve all JSONL files together with the matching database and run metadata.
7. Analyze the copies and produce a separate round-to-evidence table. Never edit raw evidence to make it easier to read.

## Limitations

This captures data exposed to the browser collector and whatever the current browser instrumentation can observe. It cannot guarantee access to server-internal data or fields that are never transmitted to the client. Base64 preserves bytes; it does not make an unknown binary protocol readable by itself.
