"""A fresh year overview of ranges, with full events resolved only on demand.

This is the displayed view model, not a cache of previously visited windows.
It borrows immutable prepared source records from the maintained range index.
Painting needs at most 366 counters per calendar, rather than a full Event and
URL-encoded identity for every occurrence. Editing still uses canonical Store
reads and its normal conflict checks.
"""
from __future__ import annotations

from collections.abc import Sequence
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta, timezone
from heapq import nsmallest

from .event_range_index import OCCURRENCE_LIMIT
from .daily_presence import _fixed_utc, regular_day_starts, merged_days
from .models import copy_event
from .recurrence import RecurrenceError, RecurrenceRangeError, overlaps, split_occurrence_id


def viewer_day(value, zone):
    try:
        return value.astimezone(zone).date()
    except OverflowError:
        # astimezone normally passes through UTC. At year 1 that intermediate
        # can underflow even if the target local clock is representable.
        target_offset = zone.utcoffset(value.replace(tzinfo=zone))
        return (value.replace(tzinfo=None) + (target_offset - value.utcoffset())).date()


def day_interval(start, end, all_day, first_ordinal, day_count, zone):
    """The viewer's civil days touched by a half-open range, including points."""
    if all_day:
        first, last = start.date().toordinal(), end.date().toordinal()
        if first == last:
            last += 1
    else:
        first = viewer_day(start, zone).toordinal()
        if start.timestamp() == end.timestamp():
            last = first + 1
        else:
            try:
                previous = (end.astimezone(timezone.utc) - timedelta(microseconds=1)).astimezone(zone)
            except OverflowError:
                # UTC conversion can cross year 1 even when both local
                # endpoints are legal. Keep that supported civil boundary.
                last = viewer_day(end - timedelta(microseconds=1), zone).toordinal() + 1
            else:
                last = previous.date().toordinal() + 1
    first, last = max(0, first - first_ordinal), min(day_count, last - first_ordinal)
    return (first, last) if first < last else None


def _matches(event, query):
    return not query or query in ' '.join((event.title, event.description, event.location)).casefold()


class YearProjection(Sequence):
    """Compact current-window presence and bounded, lazy detail access.

    Source snapshots outlive subsequent writes safely: an index update replaces
    a record rather than mutating it. No SQLite access, string parsing, retained
    query pages or shared recurrence iterators are used to draw this overview.
    """
    def __init__(self, start, end, events, records, sources, *, zone, issues=None,
                 query='', available=None):
        self.start, self.end, self.zone = start, end, zone
        self.first_day = start.date()
        self._first_ordinal = self.first_day.toordinal()
        self._day_count = (end.date() - self.first_day).days + bool(end.time() != time.min)
        self._explicit = tuple(events)
        self._records = tuple(records)
        self._sources = dict(sources)
        self._query = query.strip().casefold()
        self._available = set() if available is None else set(available)
        self.failures = []
        self.known_events = ()
        self.summaries = []
        self._count = 0
        self._build(issues, available is not None)

    def _build(self, issues, prevalidated):
        grouped = {}
        for event in self._explicit:
            grouped.setdefault(event.series_id or event.id, []).append(event)
        rules = {record.master.id: record for record in self._records}
        self._by_source = grouped
        self._rules = rules
        counts, known, start_days, instant_days = {}, [], set(), set()
        daily_spans = []
        start_ordinals = set()
        direct_utc_days = all(_fixed_utc(zone) for zone in (self.zone, self.start.tzinfo, self.end.tzinfo))
        try:
            utc_bounds = self.start.astimezone(timezone.utc), self.end.astimezone(timezone.utc)
        except OverflowError:
            utc_bounds = None  # Preserve the general supported-year boundary path.
        # Identical UTC clocks can share civil-day conversion work within this
        # fresh model. Nothing from this map survives _build or a later query.
        viewer_geometry = {}
        epoch_ordinal = date(1970, 1, 1).toordinal()
        for identity in grouped.keys() | rules.keys():
            if prevalidated and identity not in self._available:
                continue
            record = self._sources[identity]
            master = record.master
            master_match = _matches(master, self._query)
            # Keep source-local endpoints until its complete range passes the
            # budget/decoder checks. Sparse sources must not allocate and merge
            # a whole year's counters just to touch one or two civil days.
            differences = {}
            native_keys = set()
            native_count = 0
            daily_span = None
            source_days = set()
            source_instants = set()
            count = 0
            try:
                if record.error is not None:
                    raise record.error
                if identity in rules and (master_match or not prevalidated):
                    daily_span = (regular_day_starts(record.prepared, *utc_bounds, timezone.utc)
                                  if utc_bounds is not None else None)
                    if daily_span is not None:
                        native_count = len(daily_span)
                        if native_count > OCCURRENCE_LIMIT:
                            raise RecurrenceRangeError(f'This range exceeds {OCCURRENCE_LIMIT:,} occurrences. Choose a smaller date range.')
                        if master.start.date().toordinal() in daily_span:
                            native_keys.update(event.recurrence_id for event in record.prepared.explicit_occurrences)
                        if master_match and daily_span:
                            if not direct_utc_days:
                                clock = (master.start.hour * 60 + master.start.minute) * 60 + master.start.second
                                duration = record.prepared.duration
                                for ordinal in daily_span:
                                    geometry_key = ordinal, clock, duration
                                    geometry = viewer_geometry.get(geometry_key)
                                    if geometry is None:
                                        actual = datetime.combine(date.fromordinal(ordinal), time.min, timezone.utc) + timedelta(seconds=clock)
                                        geometry = (viewer_day(actual, self.zone), day_interval(
                                            actual, actual + duration, False, self._first_ordinal,
                                            self._day_count, self.zone))
                                        viewer_geometry[geometry_key] = geometry
                                    day, interval = geometry
                                    source_days.add(day)
                                    source_instants.add(ordinal - epoch_ordinal)
                                    if interval:
                                        first, last = interval
                                        differences[first] = differences.get(first, 0) + 1
                                        differences[last] = differences.get(last, 0) - 1
                            elif daily_span.step == 1:
                                first, last = daily_span.start - self._first_ordinal, daily_span.stop - self._first_ordinal
                                differences[first] = differences.get(first, 0) + 1
                                differences[last] = differences.get(last, 0) - 1
                            else:
                                for ordinal in daily_span:
                                    first = ordinal - self._first_ordinal
                                    differences[first] = differences.get(first, 0) + 1
                                    differences[first + 1] = differences.get(first + 1, 0) - 1
                            count += native_count
                    else:
                        for key, start, end, _spec in record.prepared.iter_rule_ranges(
                                self.start, self.end, floating_zone=self.zone if master.floating_time else None):
                            native_keys.add(key)
                            native_count += 1
                            if master_match:
                                source_days.add(start.date() if master.all_day else viewer_day(start, self.zone))
                                source_instants.add(int(start.timestamp() // 86400))
                                interval = day_interval(start, end, master.all_day, self._first_ordinal,
                                                        self._day_count, self.zone)
                                if interval:
                                    first, last = interval
                                    differences[first] = differences.get(first, 0) + 1
                                    differences[last] = differences.get(last, 0) - 1
                                count += 1
                explicit = grouped.get(identity, ())
                if not prevalidated and master.recurring and native_count + sum(
                        event.recurrence_id not in native_keys for event in explicit) > OCCURRENCE_LIMIT:
                    raise RecurrenceRangeError(f'This range exceeds {OCCURRENCE_LIMIT:,} occurrences. Choose a smaller date range.')
                source_known = []
                for event in explicit:
                    if not (master_match or _matches(event, self._query)):
                        continue
                    source_known.append(event)
                    if event.recurrence_id and event.recurrence_id in native_keys:
                        continue
                    source_days.add(event.start.date() if event.all_day else viewer_day(event.start, self.zone))
                    source_instants.add(int(event.start.timestamp() // 86400))
                    interval = day_interval(event.start, event.end, event.all_day, self._first_ordinal,
                                            self._day_count, self.zone)
                    if interval:
                        first, last = interval
                        differences[first] = differences.get(first, 0) + 1
                        differences[last] = differences.get(last, 0) - 1
                    count += 1
            except RecurrenceRangeError as error:
                if issues is None:
                    raise
                issues.append((master, error))
                continue
            except (ValueError, TypeError, OverflowError, KeyError):
                self.failures.append((master, RecurrenceError('Unsupported or invalid recurrence rule.')))
                continue
            self._available.add(identity)
            if master_match and daily_span and direct_utc_days:
                if daily_span.step == 1:
                    daily_spans.append(daily_span)
                else:
                    start_ordinals.update(daily_span)
            start_days.update(source_days)
            instant_days.update(source_instants)
            known.extend(source_known)
            self._count += count
            target = counts.get(master.calendar_id)
            if target is None:
                target = counts[master.calendar_id] = [0] * (self._day_count + 1)
            for day, value in differences.items():
                target[day] += value
        summaries = [{} for _ in range(self._day_count)]
        for calendar_id, differences in counts.items():
            active = 0
            for day in range(self._day_count):
                active += differences[day]
                if active:
                    summaries[day][calendar_id] = (active, '')
        self.known_events = tuple(known)
        self._known = {event.id: event for event in known}
        self.summaries = summaries
        start_ordinals.update(merged_days(daily_spans))
        for ordinal in start_ordinals:
            start_days.add(date.fromordinal(ordinal))
            instant_days.add(ordinal - epoch_ordinal)
        self._start_days = tuple(sorted(start_days))
        self._instant_days = tuple(sorted(instant_days))

    def __len__(self):
        return self._count

    def __iter__(self):
        return iter(self.for_sources(self._available))

    def __getitem__(self, index):
        return self.for_sources(self._available)[index]

    def filtered(self, query):
        query = query.strip().casefold()
        if query == self._query:
            return self
        return type(self)(self.start, self.end, self._explicit, self._records, self._sources,
                          zone=self.zone, query=query, available=self._available, issues=[])

    def without_calendars(self, identifiers):
        """Restrict current-view details as well as markers to active sources."""
        identifiers = set(identifiers)
        available = {identity for identity in self._available
                     if self._sources[identity].master.calendar_id not in identifiers}
        if available == self._available:
            return self
        return type(self)(self.start, self.end, self._explicit, self._records, self._sources,
                          zone=self.zone, query=self._query, available=available, issues=[])

    def _allows(self, event):
        return overlaps(event, self.start, self.end) and (
            _matches(event, self._query) or _matches(self._sources[event.series_id or event.id].master, self._query))

    def event_by_id(self, identity):
        parts = split_occurrence_id(identity)
        source_id = parts[0] if parts else identity
        if source_id not in self._available:
            return None
        record = self._sources[source_id]
        if not parts:
            value = self._known.get(identity)
        else:
            key = parts[1]
            value = (record.prepared.occurrence_at(key, floating_zone=self.zone if record.master.floating_time else None)
                     if record.prepared is not None else None)
            if value is None:
                value = record.occurrences.get(identity)
                if value is not None and value.floating_time:
                    value = copy_event(value, {'start': value.start.replace(tzinfo=self.zone),
                                               'end': value.end.replace(tzinfo=self.zone)})
        return value if value is not None and self._allows(value) else None

    def selected_events(self, identifiers):
        return [value for identity in identifiers if (value := self.event_by_id(identity)) is not None]

    def _events_in_range(self, source_ids, start, end, *, limit=None):
        values = {event.id: event for identity in source_ids for event in self._by_source.get(identity, ())
                  if overlaps(event, start, end) and self._allows(event)}
        order = lambda event: (event.start.timestamp(), event.end.timestamp(), event.id)
        if limit is not None:
            values = {event.id: event for event in nsmallest(limit, values.values(), key=order)}
        for identity in source_ids:
            record = self._rules.get(identity)
            if record is None or not _matches(record.master, self._query):
                continue  # Nonmatching masters can contribute only indexed overrides.
            values.update((event.id, event) for event in record.prepared.query_rule(
                start, end, floating_zone=self.zone if record.master.floating_time else None,
                **({'limit': limit, 'truncate': True} if limit is not None else {})))
        values = (event for event in values.values() if self._allows(event))
        return nsmallest(limit, values, key=order) if limit is not None else sorted(values, key=order)

    def for_sources(self, identifiers):
        return self._events_in_range(set(identifiers) & self._available, self.start, self.end)

    def events_on_day(self, day, *, limit=100):
        if limit <= 0 or not self.first_day <= day or day.toordinal() >= self._first_ordinal + self._day_count:
            return []
        start = datetime.combine(day, time.min, self.zone)
        try:
            end = datetime.combine(day + timedelta(days=1), time.min, self.zone)
        except OverflowError:
            end = datetime.max.replace(tzinfo=self.zone)
        return self._events_in_range(self._available, start, end, limit=limit)

    def adjacent_start_day(self, day, direction):
        """Nearest start day inside this displayed model, without expansion."""
        index = bisect_right(self._start_days, day) if direction > 0 else bisect_left(self._start_days, day) - 1
        return self._start_days[index] if 0 <= index < len(self._start_days) else None

    def events_starting_on(self, day):
        """Resolve one start-day bucket for keyboard navigation, not a year."""
        start = datetime.combine(day, time.min, self.zone)
        end = (datetime.combine(day + timedelta(days=1), time.min, self.zone)
               if day.toordinal() < datetime.max.toordinal() else datetime.max.replace(tzinfo=self.zone))
        return [event for event in self._events_in_range(self._available, start, end)
                if (event.start.date() if event.all_day else viewer_day(event.start, self.zone)) == day]

    def adjacent_event(self, identity, direction):
        """Previous/next in canonical start/end/ID order, wrapping at the ends."""
        if not self._instant_days:
            return None
        current = self.event_by_id(identity) if identity else None
        if current is not None:
            day = int(current.start.timestamp() // 86400)
            values = self._events_in_instant_day(day)
            index = next((position for position, value in enumerate(values) if value.id == identity), None)
            if index is not None and 0 <= index + direction < len(values):
                return values[index + direction]
            position = (bisect_right(self._instant_days, day) if direction > 0 else
                        bisect_left(self._instant_days, day) - 1)
            day = self._instant_days[position] if 0 <= position < len(self._instant_days) else None
        else:
            day = None
        if day is None:
            day = self._instant_days[0 if direction > 0 else -1]
        values = self._events_in_instant_day(day)
        return values[0 if direction > 0 else -1] if values else None

    def _events_in_instant_day(self, day):
        # Canonical ordering is by instant, whereas DATE entries overlap by
        # civil dates. Include adjacent civil days before selecting one UTC
        # bucket; legal offsets are strictly smaller than a day.
        try:
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            start = epoch + timedelta(days=day - 1)
            end = epoch + timedelta(days=day + 2)
        except OverflowError:
            values = self.for_sources(self._available)  # Only supported-year endpoints.
        else:
            values = self._events_in_range(self._available, start, end)
        return [event for event in values if int(event.start.timestamp() // 86400) == day]
