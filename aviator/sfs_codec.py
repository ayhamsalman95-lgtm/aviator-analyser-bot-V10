"""SmartFoxServer 2X binary frame decoding.

Real decoding is delegated to the `sfs2x-py` package (import name `sfs2x`),
API: decode_s2c_packet(bytes) -> (dict, consumed:int), parse_s2c_command(dict) -> (cmd, params).
No fallback decoder is provided on purpose: if the package is missing, binary
frames are logged as opaque bytes (length + sha1 + short hex preview) and are
NEVER pushed through text/JSON/hex heuristics.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:  # pragma: no cover - depends on the local environment
    import sfs2x as _sfs2x
    from sfs2x import decode_s2c_packet as _decode, parse_s2c_command as _parse
    SFS2X_AVAILABLE = True
    SFS2X_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # ImportError or a broken install
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
    # Per-packet transport boundaries are retained for timing/size research.
    # Existing callers can continue using ``commands`` unchanged.
    packet_spans: list[dict[str, int]] = field(default_factory=list)
    packets: int = 0
    consumed: int = 0
    total: int = 0
    error: Optional[str] = None
    decoder_available: bool = True

    @property
    def leftover(self) -> int:
        return self.total - self.consumed


def binary_summary(data: bytes, preview: int = 24) -> dict:
    return {"length": len(data), "sha1": hashlib.sha1(data).hexdigest(), "preview_hex": data[:preview].hex()}


class SfsDecoder:
    """Decode EVERY packet in a WebSocket frame using the consumed length."""

    def __init__(self, decode: Optional[Callable] = None, parse: Optional[Callable] = None,
                 max_packets: int = 256):
        self._decode = decode or _decode
        self._parse = parse or _parse
        self.max_packets = max_packets

    @property
    def available(self) -> bool:
        return self._decode is not None and self._parse is not None

    def decode_frame(self, data: bytes) -> FrameResult:
        res = FrameResult(total=len(data), decoder_available=self.available)
        if not self.available:
            res.error = "decoder_unavailable"
            return res
        offset = 0
        while offset < len(data) and res.packets < self.max_packets:
            try:
                obj, consumed = self._decode(data[offset:])
            except Exception as exc:
                res.error = f"{type(exc).__name__}: {exc} (at offset {offset})"
                break
            if not isinstance(consumed, int) or consumed <= 0:
                res.error = f"decoder returned invalid consumed={consumed!r} at offset {offset}"
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
                cmd, params = None, None
            res.commands.append((cmd, params if params is not None else obj))
        res.consumed = offset
        return res


def unwrap_browser_event(event: dict) -> Optional[tuple[str, Any]]:
    """Map a Chrome-side SmartFox dispatchEvent snapshot to (cmd, params).

    SFS2X JS dispatches extension responses as type 'extensionResponse' with
    `cmd` and `params`. Other event types (connection, login, ...) are ignored.
    """
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
