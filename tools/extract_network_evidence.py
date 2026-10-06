#!/usr/bin/env python3
"""Extract provably-fair evidence from network logs without committing raw files.

Process external game_network.jsonl files to extract fairness evidence,
SFS messages, and WebSocket frames using actual collector formats.
Stream processing with bounded memory and security redaction.

Usage:
    python tools/extract_network_evidence.py <game_network.jsonl> [--output DERIVED.jsonl]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from aviator.extract import extract_fairness
from aviator.netlog import safe_url
from aviator.validation import parse_round_id, RoundValidationError


PROVENANCE_KEYS = {
    "session_id", "collector_run_id", "event_id", "frame_id", "frame_index",
    "packet_index", "packet_offset", "packet_end", "source_file", "source_line",
}

def redact_sensitive(data: Any, depth: int = 0) -> Any:
    """Redact sensitive data without silently truncating evidence structures."""
    
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            key = str(k)
            key_lower = key.lower()
            if key in PROVENANCE_KEYS:
                result[k] = redact_sensitive(v, depth + 1)
            elif any(x in key_lower for x in ['token', 'password', 'cookie', 'auth', 'secret', 'session']):
                result[k] = "REDACTED"
            else:
                result[k] = redact_sensitive(v, depth + 1)
        return result
    elif isinstance(data, list):
        return [redact_sensitive(item, depth + 1) for item in data]
    elif isinstance(data, str):
        # Redact URLs with tokens/credentials
        if data.startswith('http'):
            # Redact query params like ?token=, ?key=, ?auth=
            data = re.sub(r'([?&])(token|key|auth|credential|session|cookie)=[^&\s]+', 
                         r'\1\2=REDACTED', data, flags=re.IGNORECASE)
        return data
    return data


class NetworkExtractor:
    """Extract fairness evidence from network logs (actual collector formats)."""

    def __init__(self, source_path: Path, output_path: Optional[Path] = None, chunk_size: int = 1000):
        self.source_path = Path(source_path)
        import uuid
        self.extraction_run_id = uuid.uuid4().hex
        self.output_path = output_path or self.source_path.parent / f"derived_evidence.{self.extraction_run_id}.jsonl"
        self.chunk_size = chunk_size  # Write in chunks to manage memory
        self.source_file = str(self.source_path)
        self._output_initialized = False
        self.stats = {
            "total_lines": 0,
            "parsed": 0,
            "prepared": 0,
            "written": 0,
            "write_failures": 0,
            "output_complete": False,
            "errors": 0,
            "sfs_decoded_count": 0,
            "sfs_message_count": 0,
            "ws_binary_count": 0,
            "ws_binary_undecoded_count": 0,
            "http_response_count": 0,
            "fairness_evidence_count": 0,
            # Classification breakdown
            "classification_round_result": 0,
            "classification_change_state": 0,
            "classification_init_rounds_info": 0,
            "classification_fairness_evidence": 0,
            "classification_sfs_other": 0,
            "classification_websocket_frame": 0,
            "classification_undecoded_binary": 0,
            "classification_http_fairness": 0,
            "start_time": time.time(),
            "end_time": None,
        }

    def _read_lines(self) -> Iterator[dict]:
        """Read JSONL file line by line (gzipped or plain)."""
        try:
            if str(self.source_path).endswith(".gz"):
                with gzip.open(self.source_path, "rt", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            self.stats["errors"] += 1
            else:
                with open(self.source_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            self.stats["errors"] += 1
        except IOError as e:
            print(f"Error reading {self.source_path}: {e}", file=sys.stderr)
            sys.exit(1)

    def _extract_from_line(self, data: dict, received_at: float) -> list[dict]:
        """Extract evidence from a single network event (actual formats).
        
        PRESERVES COMPLETE DECODED PAYLOADS for research/replay.
        Does NOT reduce to metadata only - includes full params and state.
        """
        records = []
        raw_kind = data.get("kind")
        kind = raw_kind.lower() if isinstance(raw_kind, str) else ""
        source_kind = raw_kind if isinstance(raw_kind, str) else None
        
        # Handle sfs_decoded (actual collector format)
        if kind == "sfs_decoded":
            self.stats["sfs_decoded_count"] += 1
            raw_cmd = data.get("command")
            cmd = raw_cmd.lower() if isinstance(raw_cmd, str) else ""
            params = data.get("params", {})
            url = data.get("url", "")
            
            # ALWAYS create derived record for this SFS event (don't arbitrarily drop it)
            # Classify by command type
            classification = "sfs_other"
            
            # Extract fairness evidence AND preserve complete params
            if isinstance(params, dict):
                fairness_recs = extract_fairness(params, max_nodes=500)
                for fr in fairness_recs:
                    if fr.scan_truncated:
                        records.append({"classification": "fairness_scan_truncated",
                                        "kind": "fairness_scan_truncated", "source": "sfs_decoded",
                                        "path": fr.path, "scan_limit": fr.scan_limit,
                                        "list_truncations": fr.list_truncations})
                        continue
                    if fr.meaningful():
                        self.stats["fairness_evidence_count"] += 1
                        record = {
                            "classification": "fairness_evidence",
                            "kind": "fairness_evidence",
                            "timestamp": received_at,
                            "source": "sfs_decoded",
                            "path": fr.path,
                            "round_id": fr.round_id,
                        }
                        if fr.server_seed:
                            record["server_seed"] = fr.server_seed
                        if fr.player_seeds:
                            record["player_seeds"] = fr.player_seeds
                        if fr.commitment:
                            record["commitment"] = fr.commitment
                        if fr.round_hash:
                            record["round_hash"] = fr.round_hash
                        if fr.crypto_observations:
                            record["crypto_observations"] = fr.crypto_observations
                        records.append(record)
            
            # Classify SFS command and PRESERVE COMPLETE PARAMS
            if "roundchart" in cmd:
                classification = "round_result"
                # Extract round_id and multiplier ONLY from actual payload
                round_id = None
                multiplier = None
                if isinstance(params, dict):
                    round_id = params.get("round_id") or params.get("roundId")
                    max_mult = params.get("max_multiplier") or params.get("maxMultiplier")
                    if max_mult:
                        try:
                            multiplier = float(max_mult)
                        except (ValueError, TypeError):
                            pass
                
                record = {
                    "classification": classification,
                    "kind": "sfs:roundChartInfo",
                    "timestamp": received_at,
                    "cmd": cmd,
                    "source": "sfs_decoded",
                    "complete_params": params,  # PRESERVE COMPLETE PAYLOAD
                    "url": safe_url(url) if url else None,
                }
                if round_id is not None:
                    record["round_id"] = round_id
                if multiplier is not None:
                    record["multiplier"] = multiplier
                records.append(record)
                
            elif "changestate" in cmd:
                classification = "change_state"
                new_state_id = None
                round_id = None
                if isinstance(params, dict):
                    new_state_id = params.get("newStateid") or params.get("newStateId") or params.get("state")
                    round_id = params.get("round_id") or params.get("roundId")
                
                record = {
                    "classification": classification,
                    "kind": "sfs:changeState",
                    "timestamp": received_at,
                    "cmd": cmd,
                    "source": "sfs_decoded",
                    "complete_params": params,  # PRESERVE COMPLETE PAYLOAD
                    "url": safe_url(url) if url else None,
                }
                if new_state_id is not None:
                    record["new_state_id"] = new_state_id
                if round_id is not None:
                    record["round_id"] = round_id
                records.append(record)
                
            elif "init" in cmd and "roundsinfo" in cmd:
                classification = "init_rounds_info"
                roundsinfo = None
                if isinstance(params, dict):
                    roundsinfo = params.get("roundsInfo") or params.get("rounds_info")
                
                record = {
                    "classification": classification,
                    "kind": "sfs:init.roundsInfo",
                    "timestamp": received_at,
                    "cmd": cmd,
                    "source": "sfs_decoded",
                }
                if roundsinfo:
                    record["complete_roundsInfo"] = roundsinfo  # PRESERVE COMPLETE STRUCTURE
                records.append(record)
                
            elif "fairness" in cmd:
                classification = "fairness_evidence"
                round_id = None
                if isinstance(params, dict):
                    round_id = params.get("round_id") or params.get("roundId")
                
                record = {
                    "classification": classification,
                    "kind": "sfs:fairness",
                    "timestamp": received_at,
                    "cmd": cmd,
                    "source": "sfs_decoded",
                    "complete_params": params,  # PRESERVE COMPLETE PAYLOAD
                    "url": safe_url(url) if url else None,
                }
                if round_id is not None:
                    record["round_id"] = round_id
                records.append(record)
                
            else:
                # Other SFS commands - STILL PRESERVE THEM
                classification = "sfs_other"
                record = {
                    "classification": classification,
                    "kind": "sfs:other",
                    "timestamp": received_at,
                    "cmd": cmd,
                    "source": "sfs_decoded",
                    "complete_params": params,  # PRESERVE COMPLETE PAYLOAD
                    "url": safe_url(url) if url else None,
                }
                records.append(record)
        
        # Handle sfs_message (legacy format)
        elif kind == "sfs_message":
            self.stats["sfs_message_count"] += 1
            raw_cmd = data.get("cmd")
            cmd = raw_cmd.lower() if isinstance(raw_cmd, str) else ""
            params = data.get("params", {})
            
            if isinstance(params, dict):
                fairness_recs = extract_fairness(params, max_nodes=500)
                for fr in fairness_recs:
                    if fr.scan_truncated:
                        records.append({"classification": "fairness_scan_truncated",
                                        "kind": "fairness_scan_truncated", "source": "sfs_message",
                                        "path": fr.path, "scan_limit": fr.scan_limit,
                                        "list_truncations": fr.list_truncations})
                        continue
                    if fr.meaningful():
                        self.stats["fairness_evidence_count"] += 1
                        record = {
                            "kind": "fairness_evidence",
                            "timestamp": received_at,
                            "source": "sfs_message",
                            "path": fr.path,
                            "round_id": fr.round_id,
                        }
                        if fr.server_seed:
                            record["server_seed"] = fr.server_seed
                        if fr.player_seeds:
                            record["player_seeds"] = fr.player_seeds
                        if fr.crypto_observations:
                            record["crypto_observations"] = fr.crypto_observations
                        records.append(record)
        
        # Handle WebSocket binary frames
        elif kind in ("ws_binary", "ws_binary_undecoded", "ws_binary_frame"):
            self.stats["ws_binary_undecoded_count" if "undecoded" in kind else "ws_binary_count"] += 1
            classification = "undecoded_binary" if "undecoded" in kind else "websocket_frame"
            record = {
                "classification": classification,
                "kind": "websocket_frame",
                "timestamp": received_at,
                "frame_type": kind,
                "source": "websocket",
                "url": safe_url(data.get("url", "")),
            }
            # Preserve complete frame data if available
            for key in ("frame_data", "payload_b64", "payload_complete", "sha256", "sha1",
                        "byte_length", "length", "frame_index", "frame_id", "session_id",
                        "collector_run_id", "event_id", "decode_status"):
                if key in data:
                    record[key] = data.get(key)
            records.append(record)
        
        elif kind == "sfs_decode_error":
            records.append({"classification": "decode_error", "kind": "sfs_decode_error",
                            "source": "collector", "complete_evidence": data})
        elif kind in ("pre_round_snapshot", "tracker_error", "frame_handler_error",
                      "ws_open", "ws_close", "ws_text", "browser_event", "browser_queue_overflow",
                      "browser_event_unhandled", "browser_hook_error", "browser_drain_error",
                      "collector_error", "fairness_gate_skip",
                      "fairness_button_icon_inventory", "fairness_button_icon_inventory_error",
                      "fairness_menu_exact_click", "fairness_exact_target_probe"):
            records.append({"classification": kind, "kind": kind,
                            "source": "collector", "complete_evidence": data})
        # Handle HTTP responses (may contain fairness or game data)
        elif kind == "http_response":
            self.stats["http_response_count"] += 1
            url = data.get("url", "")
            body = data.get("body", "")
            status = data.get("status")
            headers = data.get("headers")
            
            # Determine classification by checking if body contains actual fairness evidence
            classification = "http_response"  # Default: ordinary HTTP response
            has_fairness_evidence = False
            
            # Try to parse JSON body for fairness evidence AND preserve complete body
            body_data = None
            if body:
                try:
                    if isinstance(body, str):
                        body_data = json.loads(body)
                    else:
                        body_data = body
                    
                    fairness_recs = extract_fairness(body_data, max_nodes=500)
                    for fr in fairness_recs:
                        if fr.scan_truncated:
                            records.append({"classification": "fairness_scan_truncated",
                                            "kind": "fairness_scan_truncated", "source": "http_response",
                                            "path": fr.path, "scan_limit": fr.scan_limit,
                                            "list_truncations": fr.list_truncations})
                            continue
                        if fr.meaningful():
                            has_fairness_evidence = True
                            self.stats["fairness_evidence_count"] += 1
                            record = {
                                "classification": "fairness_evidence",
                                "kind": "fairness_evidence",
                                "timestamp": received_at,
                                "source": "http_response",
                                "path": fr.path,
                                "round_id": fr.round_id,
                                "url": safe_url(url),
                                "server_seed": fr.server_seed,
                                "player_seeds": fr.player_seeds,
                                "crypto_observations": fr.crypto_observations,
                            }
                            records.append(record)
                except (json.JSONDecodeError, TypeError):
                    # Body is not JSON (likely HTML, JS, CSS, image, etc.)
                    pass
            
            # Only classify as http_fairness if actual fairness evidence was found
            if has_fairness_evidence:
                classification = "http_fairness"
            
            # PRESERVE COMPLETE HTTP response record (don't just extract fairness)
            record = {
                "classification": classification,
                "kind": "http_response",
                "timestamp": received_at,
                "source": "http_response",
                "url": safe_url(url),
            }
            if status:
                record["status"] = status
            if body_data:
                record["body"] = body_data  # PRESERVE COMPLETE PARSED BODY
            if headers:
                record["headers"] = redact_sensitive(headers)
            records.append(record)
        
        if not records:
            records.append({"classification": "unhandled_kind", "kind": "unhandled_kind",
                            "source": "collector", "source_kind": source_kind,
                            "complete_evidence": data})
        return records

    def extract(self) -> dict:
        """Process source file and stream evidence output."""
        print(f"Streaming {self.source_path.name}...", file=sys.stderr)
        
        # Ensure output directory exists and isolate this extraction run from any
        # previous contents at the same explicit output path.
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "w", encoding="utf-8"):
            pass
        self._output_initialized = True
        
        # Write in bounded chunks
        chunk = []
        for source_line, line_data in enumerate(self._read_lines(), 1):
            self.stats["total_lines"] += 1
            
            try:
                received_at = line_data.get("received_at")
                timestamp_provenance = "collector_received_at" if received_at is not None else "absent"
                legacy_timestamp = line_data.get("timestamp")
                if isinstance(received_at, str):
                    try:
                        received_at = float(received_at)
                    except (ValueError, TypeError):
                        received_at = None
                        timestamp_provenance = "absent"
                self.stats["parsed"] += 1
                
                # Extract evidence
                records = self._extract_from_line(line_data, received_at)
                
                # Add to chunk
                for rec in records:
                    rec["schema_version"] = 2
                    rec["extraction_run_id"] = self.extraction_run_id
                    rec["source_file"] = self.source_file
                    rec["source_line"] = source_line
                    rec["source_kind"] = line_data.get("kind")
                    rec["event_timestamp"] = received_at
                    rec["timestamp_provenance"] = timestamp_provenance
                    if "timestamp" in line_data:
                        rec["timestamp"] = legacy_timestamp
                    rec["extracted_at"] = time.time()
                    rec["source_record"] = line_data
                    rec_clean = redact_sensitive(rec)
                    chunk.append(rec_clean)
                    self.stats["prepared"] += 1
                    
                    # Count by classification
                    classification = rec.get("classification", "unknown")
                    key = f"classification_{classification}"
                    if key in self.stats:
                        self.stats[key] += 1
                
                # Write chunk if full
                if len(chunk) >= self.chunk_size:
                    try:
                        self._write_chunk(chunk)
                    except OSError as exc:
                        self.stats["write_failures"] += 1
                        raise RuntimeError(f"derived evidence write failed: {type(exc).__name__}: {exc}") from exc
                    chunk = []
            except Exception as e:
                self.stats["errors"] += 1
                chunk.append(redact_sensitive({
                    "schema_version": 2,
                    "extraction_run_id": self.extraction_run_id,
                    "source_file": self.source_file,
                    "source_line": source_line,
                    "source_kind": line_data.get("kind"),
                    "classification": "extractor_error",
                    "kind": "extractor_error",
                    "error": f"{type(e).__name__}: {e}",
                    "source_record": line_data,
                    "extracted_at": time.time(),
                }))
                self.stats["written"] += 1
        
        # Write remaining chunk (CRITICAL: don't lose data at end)
        if chunk:
            try:
                self._write_chunk(chunk)
            except OSError as exc:
                self.stats["write_failures"] += 1
                raise RuntimeError(f"derived evidence write failed: {type(exc).__name__}: {exc}") from exc

        self.stats["output_complete"] = True
        self.stats["end_time"] = time.time()
        return self.stats

    def _write_chunk(self, records: list[dict]) -> None:
        """Write one complete chunk for this extraction run."""
        mode = "a" if self._output_initialized else "w"
        with open(self.output_path, mode, encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            f.flush()
            import os
            os.fsync(f.fileno())
        self.stats["written"] += len(records)

    def report(self) -> str:
        """Generate extraction summary with complete classification breakdown."""
        elapsed = (self.stats["end_time"] or time.time()) - self.stats["start_time"]
        lines = [
            "",
            "=== Network Evidence Extraction Summary ===",
            f"Source: {self.source_path}",
            f"Output: {self.output_path}",
            f"Time: {elapsed:.2f}s",
            "",
            "Input statistics:",
            f"  Total lines parsed: {self.stats['total_lines']:,}",
            f"  Successfully parsed: {self.stats['parsed']:,}",
            f"  Parse errors: {self.stats['errors']}",
            f"  Prepared derived records: {self.stats['prepared']:,}",
            f"  Persisted derived records: {self.stats['written']:,}",
            f"  Write failures: {self.stats['write_failures']}",
            f"  Output complete: {self.stats['output_complete']}",
            "",
            "Source record types found:",
            f"  sfs_decoded: {self.stats['sfs_decoded_count']:,}",
            f"  sfs_message (legacy): {self.stats['sfs_message_count']}",
            f"  ws_binary: {self.stats['ws_binary_count']}",
            f"  ws_binary_undecoded: {self.stats['ws_binary_undecoded_count']}",
            f"  http_response: {self.stats['http_response_count']}",
            "",
            "Derived records by classification:",
            f"  round_result: {self.stats.get('classification_round_result', 0)}",
            f"  change_state: {self.stats.get('classification_change_state', 0)}",
            f"  init_rounds_info: {self.stats.get('classification_init_rounds_info', 0)}",
            f"  fairness_evidence: {self.stats.get('classification_fairness_evidence', 0)}",
            f"  sfs_other: {self.stats.get('classification_sfs_other', 0)}",
            f"  websocket_frame: {self.stats.get('classification_websocket_frame', 0)}",
            f"  undecoded_binary: {self.stats.get('classification_undecoded_binary', 0)}",
            f"  http_fairness: {self.stats.get('classification_http_fairness', 0)}",
            "",
            f"Total derived records written: {self.stats['written']:,}",
            "",
        ]
        
        if self.output_path.exists():
            size_bytes = self.output_path.stat().st_size
            size_kb = size_bytes / 1024
            size_mb = size_bytes / 1024 / 1024
            if size_mb >= 1:
                lines.append(f"Output file size: {size_mb:.2f} MB")
            else:
                lines.append(f"Output file size: {size_kb:.2f} KB")
        else:
            lines.append("Output file: NOT CREATED (no records extracted)")
        
        lines.append("")
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Extract provably-fair evidence from network logs"
    )
    parser.add_argument("source", help="Path to game_network.jsonl (or .jsonl.gz)")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--no-report", action="store_true", help="Suppress summary report")

    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"Error: {source} not found", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    extractor = NetworkExtractor(source, output)
    stats = extractor.extract()

    if not args.no_report:
        print(extractor.report(), file=sys.stderr)

    sys.exit(0 if stats["errors"] == 0 else 1)


if __name__ == "__main__":
    main()
