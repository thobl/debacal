"""Read-only iCalendar export of the exact union represented by a selection."""

from __future__ import annotations

import json
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .ics import export_ics
from .models import Event
from .selection import capture_selection, is_whole_root
from .series_operations import reidentify_series


def _partial_identity(event: Event, source_id: str, scope: str, boundary: str) -> str:
    identity = json.dumps(['debacal-selection-export-v1', event.calendar_id, source_id,
                           event.ical_uid, scope, boundary], ensure_ascii=False, separators=(',', ':'))
    return str(uuid5(NAMESPACE_URL, identity))


def selection_events(store, refs: Iterable[Event], scope: str = 'single') -> tuple[Event, ...]:
    """Snapshot selected single/all/following events without editing their source.

    Complete resources retain their UID. An isolated occurrence or following
    partition gets a deterministic independent UID, so importing multiple chosen
    occurrences cannot merge them back into their original repeating resource.
    An explicitly selected whole-series root always represents that whole series.
    """
    refs = tuple(refs)
    if not refs:
        raise ValueError('Select an event before exporting.')
    captured = capture_selection(store, refs, scope)
    whole = {(event.calendar_id, event.id) for event in refs if is_whole_root(event)}
    singles = {event.id: event for event in refs if event.series_id}
    future = {}
    for event in singles.values():
        family = event.calendar_id, event.series_id
        future[family] = min(future.get(family, event.recurrence_id), event.recurrence_id)
    result = []
    for snapshot in captured.masters:
        source_id = boundary = ''
        if scope == 'single':
            reference = singles.get(snapshot.id)
            if reference is not None:
                source_id, boundary = reference.series_id, reference.recurrence_id
        elif scope == 'future' and (snapshot.calendar_id, snapshot.id) not in whole:
            boundary = future.get((snapshot.calendar_id, snapshot.id), '')
            if boundary:
                source_id = snapshot.id
        if source_id:
            identity = _partial_identity(snapshot, source_id, scope, boundary)
            snapshot = reidentify_series(snapshot, identity, identity)
        result.append(snapshot)
    if len({event.ical_uid for event in result}) != len(result):
        raise ValueError('These selected resources share an iCalendar UID. Export them separately to keep both events.')
    return tuple(result)


def export_selection(store, refs: Iterable[Event], scope: str = 'single') -> bytes:
    """Serialize a reviewed selection as interoperable iCalendar bytes."""
    return export_ics(selection_events(store, refs, scope))
