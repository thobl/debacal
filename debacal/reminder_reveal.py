"""Resolve one explicit reminder activation from its original canonical snapshot."""
from contextlib import closing
from datetime import timezone

from .event_range_index import _local_floating
from .recurrence import occurrence_id, recurrence_key, RecurrenceRangeError
from .reminder_compilation import DAY_US, MAX_US, canonical_timing, instant, micros
from .reminder_rule_seek import seek_slice
from .reminder_sources import ReminderSources
from .store import resolve_indexed_occurrence
from .timeutil import local_zone


def _first_rule(series, lower, upper, zone):
    """Seek a single retained rule member in the inclusive nominal range."""
    master = series.master
    lower = (lower + 999_999) // 1_000_000 * 1_000_000
    while lower <= upper:
        try:
            value = next(series._regular(instant(lower), instant(upper)), None)
        except RecurrenceRangeError:
            checkpoint = None
            while True:
                value, checkpoint = seek_slice(series, lower, upper, checkpoint)
                if checkpoint is None:
                    break
        if value is None or micros(value) > upper:
            return None
        key = recurrence_key(value, master.all_day, floating_time=master.floating_time)
        if key not in series.excluded and key not in series.override_keys:
            actual = value.replace(tzinfo=zone) if series.floating else value
            return micros(value), series._project(actual, key)
        lower = micros(value) + 1_000_000
    return None


def absolute_occurrence(record, trigger):
    """Earliest actual start with an end after the trigger, otherwise the latest start."""
    zone = local_zone()
    after = before = None
    def consider(event):
        nonlocal after, before
        start, end = event.start, event.end
        if event.all_day or event.floating_time:
            start, end = start.replace(tzinfo=zone), end.replace(tzinfo=zone)
        order = micros(start), event.id
        if micros(end) > micros(trigger):
            if after is None or order < after[0]:
                after = order, event
        elif before is None or order > before[0]:
            before = order, event

    for event in record.occurrences.values():
        consider(_local_floating(event))

    series = record.prepared
    if series and any(key[2] for key, _ in record.entries) and series.rule is not None:
        begin, end = series.rule_bounds
        lo, hi = micros(begin), micros(end) if end else MAX_US
        lower = series._lower(trigger.astimezone(zone), zone)
        cursor = max(lo, micros(lower) if lower else lo)
        if series.nominal_duration and (isinstance(series.zone, timezone)
                                        or getattr(series.zone, 'key', '') in ('UTC', 'Etc/UTC', 'GMT')):
            days, seconds = series.nominal_duration
            cursor = max(lo, micros(trigger) - days * DAY_US - seconds * 1_000_000)
        while cursor <= hi:
            result = _first_rule(series, cursor, hi, zone)
            if result is None:
                break
            stamp, event = result
            consider(event)
            if after is not None and micros(event.start) >= after[0][0]:
                break
            cursor = stamp + 1_000_000
        if after is None:
            # Find the last native start logarithmically, including late EXDATEs.
            stop = min(hi, cursor - 1)
            while lo <= stop:
                mid = (lo + stop) // 2
                result = _first_rule(series, mid, stop, zone)
                if result is None:
                    stop = mid - 1
                else:
                    stamp, event = result
                    consider(event)
                    lo = stamp + 1_000_000
    return (after or before or (None, None))[1]


def reminder_target(database, due, context):
    if not context.current_partition(database):
        return None
    with closing(ReminderSources(database)) as reader:
        if reader.identity_evidence()[0] != context.main_file:
            return None
        with reader.for_incarnation(context.incarnation):
            changes = reader.selected((due.key.source_id,))
            if not changes:
                return None
            change = changes[0]
            master, record = change.event, change.indexed
            if (master is None or record is None or change.version != due.source_revision
                    or master.calendar_id != due.calendar_id):
                return None
            record = canonical_timing(change)
            key = due.key.occurrence_key
            if key is None:
                return master, absolute_occurrence(record, due.key.trigger_utc), True
            if not master.recurring:
                return master, master, False
            event = resolve_indexed_occurrence(record, occurrence_id(master.id, key), key)
            return (master, event, False) if event is not None else None
