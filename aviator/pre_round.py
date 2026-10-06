"""Pre-round capture primitives for Aviator research."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Optional


@dataclass(frozen=True)
class ObservableEvent:
    timestamp: float
    source: str
    command: Optional[str]
    round_id: Optional[int]
    state_id: Optional[int]
    frame_index: Optional[int]
    packet_index: Optional[int]
    frame_size: Optional[int]
    packet_size: Optional[int]
    packet_offset: Optional[int]
    packet_end: Optional[int]
    inter_arrival_ms: Optional[float]
    payload: Any = None


class PreRoundBuffer:
    """Bounded event buffer with explicit overflow accounting."""

    def __init__(self, max_events: int = 512, max_age_s: float = 10.0):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        self.max_events = int(max_events)
        self.max_age_s = float(max_age_s)
        self._events: Deque[ObservableEvent] = deque()
        self.dropped_total = 0

    def clear(self) -> None:
        self._events.clear()
        self.dropped_total = 0

    def add(self, event: ObservableEvent) -> Optional[ObservableEvent]:
        dropped = None
        if len(self._events) >= self.max_events:
            dropped = self._events.popleft()
            self.dropped_total += 1
        self._events.append(event)
        return dropped

    def events_for_round(self, round_id: int, cutoff_timestamp: float) -> list[ObservableEvent]:
        lower = cutoff_timestamp - self.max_age_s
        return [
            e for e in self._events
            if lower <= e.timestamp <= cutoff_timestamp and (
                e.round_id is None or e.round_id == round_id
            )
        ]

    def snapshot(self, round_id: int, cutoff_timestamp: float, cutoff_state: int = 1) -> dict:
        events = self.events_for_round(round_id, cutoff_timestamp)
        packets = [e for e in events if e.frame_index is not None]
        commands = [e.command for e in events if e.command]
        sizes = [e.frame_size for e in events if e.frame_size is not None]
        packet_sizes = [e.packet_size for e in events if e.packet_size is not None]
        timings = [e.inter_arrival_ms for e in events if e.inter_arrival_ms is not None]
        return {
            "round_id": int(round_id),
            "cutoff_state": int(cutoff_state),
            "cutoff_timestamp": float(cutoff_timestamp),
            "event_count": len(events),
            "packet_event_count": len(packets),
            "command_sequence": commands,
            "frame_sizes": sizes,
            "packet_sizes": packet_sizes,
            "inter_arrival_ms": timings,
            "events": [self._event_dict(e) for e in events],
            "buffer_limit": self.max_events,
            "buffer_dropped_total": self.dropped_total,
            "truncated": bool(self.dropped_total),
            "truncation_reason": "pre_round_buffer_limit" if self.dropped_total else None,
            "recoverability": "raw_network_evidence" if self.dropped_total else "complete_within_window",
        }

    @staticmethod
    def _event_dict(event: ObservableEvent) -> dict:
        return {
            "timestamp": event.timestamp,
            "source": event.source,
            "command": event.command,
            "round_id": event.round_id,
            "state_id": event.state_id,
            "frame_index": event.frame_index,
            "packet_index": event.packet_index,
            "frame_size": event.frame_size,
            "packet_offset": event.packet_offset,
            "packet_end": event.packet_end,
            "packet_size": event.packet_size,
            "inter_arrival_ms": event.inter_arrival_ms,
            "payload": event.payload,
        }
