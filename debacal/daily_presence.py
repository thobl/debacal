"""Arithmetic presence for proven UTC daily rules; general rules keep their iterator."""
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil.rrule import DAILY


_DAY_US = 86400 * 1_000_000
_MAX_ORDINAL = date.max.toordinal()


def _fixed_utc(zone):
    return ((isinstance(zone, timezone) and zone.utcoffset(None) == timedelta()) or
            (isinstance(zone, ZoneInfo) and zone.key in {'UTC', 'Etc/UTC', 'Etc/GMT', 'GMT'}))


def _clock_us(value):
    return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond


def daily_day_span(prepared, start, end, viewer_zone):
    """Return a half-open ordinal range, or None when arithmetic is inapplicable.

    This deliberately handles only fixed-UTC timed DAILY/INTERVAL=1 rules whose
    duration occupies their start civil day. It reads the already decoded rule.
    An empty range is a supported schedule with no native starts in the window.
    """
    master, rule = prepared.master, prepared.rule
    if (rule is None or rule._freq != DAILY or rule._interval != 1 or master.all_day or master.floating_time
            or master.range_policy or master.duration_spec or master.recurrence_dates
            or master.excluded_dates or master.recurrence_overrides or master.start.microsecond
            or master.end.microsecond or not all(_fixed_utc(zone) for zone in (
                viewer_zone, start.tzinfo, end.tzinfo, master.start.tzinfo, master.end.tzinfo, rule._dtstart.tzinfo))
            or rule._dtstart != master.start):
        return None
    if any(getattr(rule, name, None) for name in ('_bymonth', '_byweekno', '_byyearday', '_bymonthday',
                                                 '_bynmonthday', '_byweekday', '_bynweekday', '_byeaster', '_bysetpos')):
        return None
    if (set(rule._byhour) != {master.start.hour} or set(rule._byminute) != {master.start.minute}
            or set(rule._bysecond) != {master.start.second}):
        return None
    duration = prepared.duration
    clock = _clock_us(master.start)
    if not timedelta() <= duration <= timedelta(microseconds=_DAY_US - clock):
        return None
    duration_us = duration // timedelta(microseconds=1)
    first = start.date().toordinal()
    # Positive ranges overlap only if their exclusive endpoint is after lower;
    # points instead include equality at lower.
    before_window = (clock + duration_us <= _clock_us(start) if duration_us
                     else clock < _clock_us(start))
    if before_window:
        first += 1
    stop = end.date().toordinal() + (clock < _clock_us(end))
    anchor = master.start.date().toordinal()
    first = max(first, anchor)
    stop = min(stop, _MAX_ORDINAL + 1)
    if rule._count is not None:
        stop = min(stop, anchor + rule._count)
    if rule._until is not None:
        until = rule._until.astimezone(timezone.utc)
        stop = min(stop, until.date().toordinal() + (clock <= _clock_us(until)))
    return range(first, max(first, stop))


def merged_days(spans):
    """Yield a union of ordinal spans without revisiting duplicate source days."""
    first = stop = None
    for span in sorted((value for value in spans if value), key=lambda value: value.start):
        if stop is None:
            first, stop = span.start, span.stop
        elif span.start <= stop:
            stop = max(stop, span.stop)
        else:
            yield from range(first, stop)
            first, stop = span.start, span.stop
    if stop is not None:
        yield from range(first, stop)
