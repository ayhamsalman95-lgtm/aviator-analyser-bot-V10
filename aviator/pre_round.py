"""Pre-round capture primitives for Aviator research.

This module is deliberately independent of prediction logic. It records only
observable transport/decoder metadata and creates immutable snapshots at a
caller-defined cutoff. It never reads credentials, cookies, tokens, or hidden
browser state.
"""
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
    """Bounded event buffer used to build leakage-safe pre-round snapshots."""

    def __init__(self, max_events: int = 512, max_age_s: float = 10.0):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        self.max_events = int(max_events)
        self.max_age_s = float(max_age_s)
        self._events: Deque[ObservableEvent] = deque(maxlen=self.max_events)

    def clear(self) -> None:
        """Drop buffered observations when a browser session is restarted."""
        self._events.clear()

    def add(self, event: ObservableEvent) -> None:
        # JS and Python sources can arrive slightly out of timestamp order.
        # Keep the bounded deque and enforce the time window when reading.
        self._events.append(event)

    def events_for_round(self, round_id: int, cutoff_timestamp: float) -> list[ObservableEvent]:
        """Return only events inside the configured pre-round time window."""
        lower = cutoff_timestamp - self.max_age_s
        return [
            e for e in self._events
            if lower <= e.timestamp <= cutoff_timestamp and (
                e.round_id is None or e.round_id == round_id
            )
        ]

    def snapshot(
        self,
        round_id: int,
        cutoff_timestamp: float,
        cutoff_state: int = 1,
    ) -> dict:
        events = self.events_for_round(round_id, cutoff_timestamp)
        packets = [e for e in events if e.frame_index is not None]
        commands = [e.command for e in events if e.command]
        sizes = [e.frame_size for e in events if e.frame_size is not None]
        packet_sizes = [e.packet_size for e in events if e.packet_size is not None]
        timings = [
            e.inter_arrival_ms for e in events
            if e.inter_arrival_ms is not None
        ]
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
            "packet_size": event.packet_size,
            "packet_offset": event.packet_offset,
            "packet_end": event.packet_end,
            "inter_arrival_ms": event.inter_arrival_ms,
            "payload": event.payload,
        }
