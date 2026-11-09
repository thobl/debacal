"""Prepared calendar sources and maintained indexes for arbitrary date windows.

This is a representation of the current data, built at load/write time. It
never records query windows or their results. Explicit dates and detached
occurrences have individual interval entries; only long rules use a series
envelope. DATE, floating wall time and fixed instants use separate coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time
from itertools import islice
from heapq import nsmallest
from typing import Any, Iterable

from .interval_index import Interval, IntervalIndex
from .models import Event


FINITE_RULE_LIMIT = 256
OCCURRENCE_LIMIT = 10_000
_EPOCH = datetime(1970, 1, 1)


def _kind(event):
    return 'date' if event.all_day else 'wall' if event.floating_time else 'instant'


def _wall(value):
    return (value.replace(tzinfo=None) - _EPOCH).total_seconds()


def _coordinates(event):
    if event.all_day:
        return float(event.start.date().toordinal()), float(event.end.date().toordinal())
    if event.floating_time:
        return _wall(event.start), _wall(event.end)
    return event.start.timestamp(), event.end.timestamp()


def _bounds(start, end, kind):
    if kind == 'date':
        return (float(start.date().toordinal()),
                float(end.date().toordinal() + (end.time() != time.min)))
    if kind == 'wall':
        # A legal UTC offset is strictly between -24h and +24h. This fixed
        # envelope also covers repeated/skipped local hours, where converting
        # just the two query endpoints to wall time can reverse their order.
        # Exact instant overlap is checked after rebinding the current zone.
        return start.timestamp() - 86400, end.timestamp() + 86400
    return start.timestamp(), end.timestamp()


def _local_floating(event):
    if event.floating_time:
        from .timeutil import local_zone
        zone = local_zone()
        if event.start.tzinfo != zone or event.end.tzinfo != zone:
            return replace(event, start=event.start.replace(tzinfo=zone), end=event.end.replace(tzinfo=zone))
    return event


@dataclass(slots=True)
class IndexedSource:
    master: Event
    prepared: Any = None
    error: Exception | None = None
    occurrences: dict[str, Event] = field(default_factory=dict)
    entries: list[tuple[tuple[str, str, bool], tuple[float, float, str]]] = field(default_factory=list)


class EventRangeIndex:
    """All sources, with AVL interval trees partitioned by calendar and time type."""

    def __init__(self):
        self.sources: dict[str, IndexedSource] = {}
        self.trees: dict[tuple[str, str, bool], IntervalIndex] = {}

    def remove(self, identity: str) -> None:
        previous = self.sources.pop(identity, None)
        if previous is not None:
            for tree_key, key in previous.entries:
                self._remove_entry(tree_key, key)

    def restore(self, identity: str, record: IndexedSource | None) -> None:
        """Restore a transaction's prepared source without decoding it again."""
        self.remove(identity)
        if record is not None:
            self.sources[identity] = record
            for tree_key, (start, end, identifier) in record.entries:
                value = record if tree_key[2] else record.occurrences[identifier]
                self.trees.setdefault(tree_key, IntervalIndex()).add(Interval(start, end, identifier, value))

    def _remove_entry(self, tree_key, key):
        tree = self.trees[tree_key]
        tree.remove(key)
        if tree._root is None:
            del self.trees[tree_key]

    def _add(self, record, kind, start, end, identity, value, *, rule=False):
        tree_key = record.master.calendar_id, kind, rule
        tree = self.trees.setdefault(tree_key, IntervalIndex())
        entry = Interval(start, end, identity, value)
        tree.add(entry)
        record.entries.append((tree_key, entry.key))

    def _add_event(self, record, event):
        lower, upper = _coordinates(event)
        self._add(record, _kind(event), lower, upper, event.id, event)
        record.occurrences[event.id] = event

    def upsert(self, event: Event) -> None:
        from .prepared_recurrence import PreparedSeries
        from .recurrence import RecurrenceError

        self.remove(event.id)
        record = IndexedSource(event)
        self.sources[event.id] = record
        if not event.recurring:
            self._add_event(record, event)
            return
        try:
            prepared = record.prepared = PreparedSeries(event)
            finite = prepared.finite_rule_occurrences(FINITE_RULE_LIMIT)
            entries = {item.id: item for item in prepared.explicit_occurrences}
            if finite is not None:
                entries.update((item.id, item) for item in finite)
            for item in entries.values():
                self._add_event(record, item)
            if finite is None and prepared.rule_bounds is not None:
                lower, upper = prepared.rule_bounds
                kind = _kind(event)
                if kind == 'date':
                    begin = float(lower.date().toordinal())
                    finish = float(upper.date().toordinal() + bool(upper.time() != time.min)) if upper else float('inf')
                elif kind == 'wall':
                    begin, finish = _wall(lower), _wall(upper) if upper else float('inf')
                else:
                    begin, finish = lower.timestamp(), upper.timestamp() if upper else float('inf')
                self._add(record, kind, begin, finish, event.id, record, rule=True)
        except (ValueError, TypeError, OverflowError, KeyError) as error:
            # Retain malformed resources and isolate them in the same way as
            # projection errors. They must not prevent the workspace opening.
            for tree_key, key in record.entries:
                self._remove_entry(tree_key, key)
            record.entries.clear()
            record.occurrences.clear()
            record.error = error if isinstance(error, RecurrenceError) else RecurrenceError('Unsupported or invalid recurrence rule.')
            self._add(record, 'instant', float('-inf'), float('inf'), event.id, record, rule=True)

    def select(self, start: datetime, end: datetime, calendars: Iterable[str] | None = None,
               *, ordinary_limit: int | None = None):
        identifiers = set(calendars) if calendars is not None else None
        events, records = [], {}
        keys = (tuple(self.trees) if identifiers is None else
                ((calendar, kind, rule) for calendar in identifiers
                 for kind in ('instant', 'date', 'wall') for rule in (False, True)))
        for calendar, kind, rule in keys:
            tree = self.trees.get((calendar, kind, rule))
            if tree is None:
                continue
            lower, upper = _bounds(start, end, kind)
            values = tree.ordered_overlapping(lower, upper) if ordinary_limit is not None and not rule else tree.overlapping(lower, upper)
            if kind == 'wall' and not rule:
                from .recurrence import overlaps
                values = (_local_floating(item) for item in values)
                values = (item for item in values if overlaps(item, start, end))
                # Wall order differs from instant order across clock changes.
                if ordinary_limit is not None:
                    values = nsmallest(ordinary_limit, values, key=lambda item: self.order(item, start.tzinfo))
            elif ordinary_limit is not None and not rule:
                values = islice(values, ordinary_limit)
            if rule:
                for record in values:
                    records[record.master.id] = record
            else:
                events.extend(values)
        return events, list(records.values())

    @staticmethod
    def order(event, zone):
        if event.all_day:
            return (datetime.combine(event.start.date(), time.min, zone).timestamp(),
                    datetime.combine(event.end.date(), time.min, zone).timestamp(), event.id)
        return event.start.timestamp(), event.end.timestamp(), event.id

    def query(self, start: datetime, end: datetime, calendars: Iterable[str] | None = None,
              *, limit: int | None = None, issues: list | None = None):
        from .recurrence import RecurrenceError, RecurrenceRangeError

        events, rules = self.select(start, end, calendars, ordinary_limit=limit)
        ordinary, grouped, failures = [], {}, []
        for event in events:
            if event.series_id:
                grouped.setdefault(event.series_id, {})[event.id] = event
            else:
                ordinary.append(event)
        unavailable = set()
        for record in rules:
            master = record.master
            try:
                if record.error is not None:
                    raise record.error
                values = record.prepared.query_rule(start, end, limit=limit if limit is not None else OCCURRENCE_LIMIT,
                                                    truncate=limit is not None)
                grouped.setdefault(master.id, {}).update((item.id, item) for item in values)
            except RecurrenceRangeError as error:
                if issues is None:
                    raise
                issues.append((master, error))
                unavailable.add(master.id)
            except (ValueError, TypeError, OverflowError, KeyError):
                failures.append((master, RecurrenceError('Unsupported or invalid recurrence rule.')))
                unavailable.add(master.id)
        for identity, members in grouped.items():
            if identity in unavailable:
                continue
            if limit is None and len(members) > OCCURRENCE_LIMIT:
                error = RecurrenceRangeError(f'This range exceeds {OCCURRENCE_LIMIT:,} occurrences. Choose a smaller date range.')
                if issues is None:
                    raise error
                issues.append((self.sources[identity].master, error))
                continue
            ordinary.extend(members.values())
        key = (lambda item: self.order(item, start.tzinfo)) if limit is not None else (
            lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))
        result = sorted(ordinary, key=key)
        return (result[:limit] if limit is not None else result), failures
