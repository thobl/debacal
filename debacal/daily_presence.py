"""Arithmetic civil-day starts for strict fixed-UTC regular schedules."""
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil.rrule import DAILY, WEEKLY


_DAY_US = 86400 * 1_000_000
_MAX_ORDINAL = date.max.toordinal()


def _fixed_utc(zone):
    return ((isinstance(zone, timezone) and zone.utcoffset(None) == timedelta()) or
            (isinstance(zone, ZoneInfo) and zone.key in {'UTC', 'Etc/UTC', 'Etc/GMT', 'GMT'}))


def _clock_us(value):
    return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond


def regular_day_starts(prepared, start, end, viewer_zone):
    """Return a possibly stepped ordinal range, or None for other schedules.

    Only fixed-UTC timed DAILY and one-weekday WEEKLY rules qualify. Their
    duration must occupy the start civil day; additional selectors/exceptions
    keep the ordinary prepared iterator. This reads the decoded rule, never its
    text. An empty range means a supported schedule has no starts in the window.
    """
    master, rule = prepared.master, prepared.rule
    if (rule is None or rule._freq not in {DAILY, WEEKLY} or rule._interval < 1 or master.all_day or master.floating_time
            or master.range_policy or master.duration_spec or master.recurrence_dates
            or master.excluded_dates or master.recurrence_overrides or master.start.microsecond
            or master.end.microsecond or not all(_fixed_utc(zone) for zone in (
                viewer_zone, start.tzinfo, end.tzinfo, master.start.tzinfo, master.end.tzinfo, rule._dtstart.tzinfo))
            or rule._dtstart != master.start):
        return None
    if any(getattr(rule, name, None) for name in ('_bymonth', '_byweekno', '_byyearday', '_bymonthday',
                                                 '_bynmonthday', '_bynweekday', '_byeaster', '_bysetpos')):
        return None
    if ((rule._freq == DAILY and rule._byweekday)
            or (rule._freq == WEEKLY and set(rule._byweekday or ()) != {master.start.weekday()})):
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
    step = rule._interval * (7 if rule._freq == WEEKLY else 1)
    first = anchor + max(0, (first - anchor + step - 1) // step) * step
    stop = min(stop, _MAX_ORDINAL + 1)
    if rule._count is not None:
        stop = min(stop, anchor + rule._count * step)
    if rule._until is not None:
        until = rule._until.astimezone(timezone.utc)
        stop = min(stop, until.date().toordinal() + (clock <= _clock_us(until)))
    starts = range(first, max(first, stop), step)
    if clock + duration_us == _DAY_US and _MAX_ORDINAL in starts:
        # The canonical iterator reports an endpoint beyond supported year
        # 9999. Arithmetic must not turn that invalid range into a valid day.
        return None
    return starts


def daily_day_span(prepared, start, end, viewer_zone):
    """The contiguous DAILY/1 specialization of regular_day_starts."""
    rule = prepared.rule
    if rule is None or rule._freq != DAILY or rule._interval != 1:
        return None
    return regular_day_starts(prepared, start, end, viewer_zone)


def merged_days(spans):
    """Yield a union of contiguous spans without revisiting source days."""
    first = stop = None
    for span in sorted((value for value in spans if value), key=lambda value: value.start):
        if span.step != 1:
            raise ValueError('Only contiguous day spans can be merged.')
        if stop is None:
            first, stop = span.start, span.stop
        elif span.start <= stop:
            stop = max(stop, span.stop)
        else:
            yield from range(first, stop)
            first, stop = span.start, span.stop
    if stop is not None:
        yield from range(first, stop)
