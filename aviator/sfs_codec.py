"""SmartFoxServer 2X binary frame decoding.

The real decoder is delegated to sfs2x-py. This module never guesses the
meaning of opaque binary data and records strict packet-consumption metadata so
the collector can preserve the original frame when decoding is incomplete.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    import sfs2x as _sfs2x
    from sfs2x import decode_s2c_packet as _decode, parse_s2c_command as _parse
    SFS2X_AVAILABLE = True
    SFS2X_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:
    _sfs2x = None
    _decode = None
    _parse = None
    SFS2X_AVAILABLE = False
    SFS2X_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def dependency_report() -> dict:
    info = {"package": "sfs2x-py", "import_name": "sfs2x", "available": SFS2X_AVAILABLE,
            "error": SFS2X_IMPORT_ERROR}
    if SFS2X_AVAILABLE:
        info["has_decode_s2c_packet"] = callable(getattr(_sfs2x, "decode_s2c_packet", None))
        info["has_parse_s2c_command"] = callable(getattr(_sfs2x, "parse_s2c_command", None))
        info["version"] = getattr(_sfs2x, "__version__", None)
    return info


@dataclass
class FrameResult:
    commands: list[tuple[Optional[str], Any]] = field(default_factory=list)
    packet_spans: list[dict[str, int]] = field(default_factory=list)
    packets: int = 0
    consumed: int = 0
    total: int = 0
    error: Optional[str] = None
    error_stage: Optional[str] = None
    decode_status: str = "success"
    decoder_available: bool = True
    packet_limit_reached: bool = False
    remaining_sha256: Optional[str] = None
    remaining_b64: Optional[str] = None

    @property
    def leftover(self) -> int:
        return self.total - self.consumed


def binary_summary(data: bytes, preview: int = 24) -> dict:
    return {
        "length": len(data),
        "byte_length": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "preview_hex": data[:preview].hex(),
    }


class SfsDecoder:
    """Decode packets using the decoder-reported consumed length."""

    def __init__(self, decode: Optional[Callable] = None, parse: Optional[Callable] = None,
                 max_packets: int = 256):
        self._decode = decode or _decode
        self._parse = parse or _parse
        self.max_packets = int(max_packets)

    @property
    def available(self) -> bool:
        return self._decode is not None and self._parse is not None

    def _set_remainder(self, res: FrameResult, data: bytes, offset: int) -> None:
        remainder = data[offset:]
        if remainder:
            res.remaining_sha256 = hashlib.sha256(remainder).hexdigest()
            res.remaining_b64 = base64.b64encode(remainder).decode("ascii")

    def decode_frame(self, data: bytes) -> FrameResult:
        res = FrameResult(total=len(data), decoder_available=self.available)
        if not self.available:
            res.error = "decoder_unavailable"
            res.error_stage = "availability"
            res.decode_status = "unavailable"
            return res

        offset = 0
        while offset < len(data):
            if res.packets >= self.max_packets:
                res.packet_limit_reached = True
                res.error = f"packet limit reached ({self.max_packets}) at offset {offset}"
                res.error_stage = "packet_limit"
                res.decode_status = "packet_limit"
                break

            remaining = len(data) - offset
            try:
                obj, consumed = self._decode(data[offset:])
            except Exception as exc:
                res.error = f"{type(exc).__name__}: {exc} (at offset {offset})"
                res.error_stage = "decode"
                res.decode_status = "decompression_error" if "compress" in type(exc).__name__.lower() else "exception"
                break

            if isinstance(consumed, bool) or not isinstance(consumed, int):
                res.error = f"decoder returned invalid consumed={consumed!r} at offset {offset}"
                res.error_stage = "consumption"
                res.decode_status = "malformed"
                break
            if consumed <= 0:
                res.error = f"decoder returned non-positive consumed={consumed!r} at offset {offset}"
                res.error_stage = "consumption"
                res.decode_status = "malformed"
                break
            if consumed > remaining:
                res.error = f"decoder consumed {consumed} bytes but only {remaining} remain at offset {offset}"
                res.error_stage = "consumption"
                res.decode_status = "incomplete"
                break

            start = offset
            offset += consumed
            res.packets += 1
            res.packet_spans.append({
                "index": res.packets - 1,
                "offset": start,
                "end": offset,
                "length": consumed,
            })
            try:
                cmd, params = self._parse(obj)
            except Exception as exc:
                res.error = f"parse {type(exc).__name__}: {exc}"
                res.error_stage = "parse"
                res.decode_status = "packet_error"
                cmd, params = None, obj
            res.commands.append((cmd, params if params is not None else obj))
            if res.error_stage == "parse":
                break

        res.consumed = offset
        if res.leftover:
            self._set_remainder(res, data, offset)
            if res.error is None:
                res.error = f"unconsumed trailing bytes: {res.leftover}"
                res.error_stage = "trailing"
                res.decode_status = "incomplete"
        return res


def unwrap_browser_event(event: dict) -> Optional[tuple[str, Any]]:
    """Map a Chrome-side SmartFox dispatchEvent snapshot to (cmd, params)."""
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("cmd"), str):
        return data["cmd"], data.get("params") if data.get("params") is not None else {}
    inner = data.get("params")
    if isinstance(inner, dict) and isinstance(inner.get("cmd"), str):
        return inner["cmd"], inner.get("params") if inner.get("params") is not None else {}
    return None
