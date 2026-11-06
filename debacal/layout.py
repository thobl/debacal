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


def layout_events(events: Iterable[CalendarInterval], *, window_start: datetime | None = None,
                  window_end: datetime | None = None) -> list[Placement]:
    """Place intervals into the fewest columns in each connected overlap group.

    Touching endpoints do not overlap. Zero-duration events occupy fifteen
    minutes for visual placement only; their stored instants remain unchanged.
    Events must have distinct IDs and nonnegative duration. Output follows
    chronological order, with an ID tie-break for deterministic keyboard hints.

    Optional window bounds clip intervals for a day view without constructing
    full event objects. Input validation still applies to the original events.
    """
    values = [(event.start.timestamp(), event.end.timestamp(), event.id) for event in events]
    if len({value[2] for value in values}) != len(values):
        raise ValueError("Event IDs must be unique.")
    if any(end < start for start, end, _identifier in values):
        raise ValueError("Events must not end before they start.")
    lower = window_start.timestamp() if window_start is not None else float('-inf')
    upper = window_end.timestamp() if window_end is not None else float('inf')
    ordered = sorted((max(start, lower), min(end, upper), identifier) for start, end, identifier in values
                     if (lower <= start < upper if start == end else start < upper and end > lower))
    result: list[Placement] = []
    component: list[tuple[str, int]] = []
    active: list[tuple[float, int]] = []
    available: list[int] = []
    column_count = 0

    def flush() -> None:
        result.extend(Placement(event_id, column, column_count)
                      for event_id, column in component)

    for start, end, identifier in ordered:
        while active and active[0][0] <= start:
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
        visual_end = end
        if visual_end == start:
            visual_end += 15 * 60
        heapq.heappush(active, (visual_end, column))
        component.append((identifier, column))
    if component:
        flush()
    return result
