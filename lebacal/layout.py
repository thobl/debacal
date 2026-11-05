"""Interval partitioning for calendar event columns, independent of Qt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import heapq
from typing import Iterable, Protocol


class CalendarInterval(Protocol):
    id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Placement:
    event_id: str
    column: int
    columns: int


def layout_events(events: Iterable[CalendarInterval]) -> list[Placement]:
    """Place intervals into the fewest columns in each connected overlap group.

    Touching endpoints do not overlap. Zero-duration events occupy fifteen
    minutes for visual placement only; their stored instants remain unchanged.
    Events must have distinct IDs and nonnegative duration. Output follows
    chronological order, with an ID tie-break for deterministic keyboard hints.
    """
    ordered = sorted(events, key=lambda event: (event.start.timestamp(), event.end.timestamp(), event.id))
    if len({event.id for event in ordered}) != len(ordered):
        raise ValueError("Event IDs must be unique.")
    if any(event.end.timestamp() < event.start.timestamp() for event in ordered):
        raise ValueError("Events must not end before they start.")
    result: list[Placement] = []
    component: list[tuple[str, int]] = []
    active: list[tuple[float, int]] = []
    available: list[int] = []
    column_count = 0

    def flush() -> None:
        result.extend(Placement(event_id, column, column_count)
                      for event_id, column in component)

    for event in ordered:
        while active and active[0][0] <= event.start.timestamp():
            _, column = heapq.heappop(active)
            heapq.heappush(available, column)
        if not active and component:
            flush()
            component = []
            available = []
            column_count = 0
        if available:
            column = heapq.heappop(available)
        else:
            column = column_count
            column_count += 1
        visual_end = event.end.timestamp()
        if visual_end == event.start.timestamp():
            visual_end += 15 * 60
        heapq.heappush(active, (visual_end, column))
        component.append((event.id, column))
    if component:
        flush()
    return result
