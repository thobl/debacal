"""iCalendar duration semantics shared by storage, authoring, and views.

Timed DTEND ranges retain elapsed time. DURATION day/week parts advance calendar
dates in the event's zone before its hour/minute/second parts advance instants.
All-day ranges always retain floating calendar dates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import re

from .timezones import event_timezone


_DURATION = re.compile(r'\+?P(?:(?P<weeks>\d+)W|(?:(?P<days>\d+)D)?'
                       r'(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?)')


@lru_cache(maxsize=256)
def parse_duration_spec(spec: str) -> tuple[int, int]:
    """Return nominal days and elapsed seconds from a nonnegative RFC duration."""
    if not isinstance(spec, str):
        raise ValueError('An event duration must be text.')
    match = _DURATION.fullmatch(spec)
    if match is None or not any(value is not None for value in match.groupdict().values()):
        raise ValueError('Use a nonnegative iCalendar duration such as P1D or PT1H.')
    if 'T' in spec and not any(match[key] is not None for key in ('hours', 'minutes', 'seconds')):
        raise ValueError('A duration time part needs hours, minutes, or seconds.')
    values = {key: int(value or 0) for key, value in match.groupdict().items()}
    days = values['weeks'] * 7 + values['days']
    seconds = values['hours'] * 3600 + values['minutes'] * 60 + values['seconds']
    try:
        timedelta(days=days, seconds=seconds)
    except OverflowError as exc:
        raise ValueError('The event duration exceeds the supported calendar range.') from exc
    return days, seconds


@lru_cache(maxsize=256)
def normalized_spec(spec: str) -> str:
    """Retain only a canonical nominal-day specification; clock-only is exact."""
    if spec == '':
        return ''
    days, seconds = parse_duration_spec(spec)
    if not days:
        return ''
    text = f'P{days}D'
    if seconds:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        text += 'T' + (f'{hours}H' if hours else '') + (f'{minutes}M' if minutes else '') + (f'{seconds}S' if seconds else '')
    return text


def elapsed_duration(start: datetime, end: datetime) -> timedelta:
    """Compare instants even when ZoneInfo equality ignores repeated-hour folds."""
    try:
        return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    except OverflowError:
        # A valid local year-1 endpoint can precede Python's first UTC date.
        return timedelta(seconds=end.timestamp() - start.timestamp())


def add_elapsed(start: datetime, duration: timedelta) -> datetime:
    if duration == timedelta(0):
        return start
    try:
        return (start.astimezone(timezone.utc) + duration).astimezone(start.tzinfo)
    except OverflowError:
        candidate = (start.replace(tzinfo=None) + duration).replace(tzinfo=start.tzinfo, fold=start.fold)
        if candidate.utcoffset() == start.utcoffset():
            return candidate
        raise


def add_duration(start: datetime, spec: str) -> datetime:
    """Apply nominal days first, then elapsed time, as required by RFC 5545."""
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError('Duration arithmetic needs a timezone-aware start.')
    days, seconds = parse_duration_spec(spec)
    cursor = start
    if days:
        cursor = (start.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=start.tzinfo)
        try:
            cursor = cursor.astimezone(timezone.utc).astimezone(start.tzinfo)
        except OverflowError:
            if cursor.utcoffset() != start.utcoffset():
                raise
    return add_elapsed(cursor, timedelta(seconds=seconds))


def end_for(event, start: datetime) -> datetime:
    """Project an event's semantic duration at another starting instant/date."""
    if event.all_day:
        return start + timedelta(days=max(1, (event.end.date() - event.start.date()).days))
    if event.floating_time:
        # Floating endpoints describe wall clocks, independent of the viewer's
        # UTC offset and DST transitions.
        return start + (event.end.replace(tzinfo=None) - event.start.replace(tzinfo=None))
    if event.duration_spec:
        zone = event_timezone(event)
        return add_duration(start.astimezone(zone), event.duration_spec).astimezone(start.tzinfo)
    return add_elapsed(start, elapsed_duration(event.start, event.end))


def same_duration(first, second) -> bool:
    if first.all_day != second.all_day:
        return False
    if first.all_day:
        return (max(1, (first.end.date() - first.start.date()).days)
                == max(1, (second.end.date() - second.start.date()).days))
    if first.floating_time and second.floating_time:
        return (first.end.replace(tzinfo=None) - first.start.replace(tzinfo=None)
                == second.end.replace(tzinfo=None) - second.start.replace(tzinfo=None))
    if first.duration_spec or second.duration_spec:
        return first.duration_spec == second.duration_spec
    return elapsed_duration(first.start, first.end) == elapsed_duration(second.start, second.end)


def retained_duration_spec(raw: str) -> str:
    """Recover old stored nominal intent from the retained master, excluding alarms."""
    if not raw or 'DURATION' not in raw.upper():
        return ''
    # Count and byte bounds keep a few unusually large retained resources from
    # pinning their complete calendars in a compatibility lookup cache.
    return _cached_retained_spec(raw) if len(raw) <= 65_536 else _read_retained_spec(raw)


@lru_cache(maxsize=128)
def _cached_retained_spec(raw):
    return _read_retained_spec(raw)


def _read_retained_spec(raw):
    from .ics_fallback import components, prop
    try:
        for component in components(raw):
            if 'RECURRENCE-ID' not in component and 'DURATION' in component and 'DTEND' not in component:
                return normalized_spec(prop(component, 'DURATION'))
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    return ''
