"""Calendar-boundary range policies without expanding exceptions in storage.

Skip and Shift can be expressed by standard RRULE clauses. Cut keeps that
rule's starts and truncates overflowing native ranges at the month boundary.
Its finite standard exceptions are derived only when exporting to iCalendar.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, time, timedelta
from functools import lru_cache
from math import gcd
from typing import Iterator

from .models import Event


RANGE_POLICIES = ('skip', 'shift', 'cut')


@lru_cache(maxsize=512)
def _minimum_month_length(year_modulo: int, month: int, kind: str, interval: int) -> int:
    months = interval * (12 if kind == 'YEARLY' else 1)
    initial = year_modulo * 12 + month - 1
    minimum = 31
    for step in range(4800 // gcd(months, 4800)):
        year, position = divmod((initial + step * months) % 4800, 12)
        minimum = min(minimum, monthrange(year + 400, position + 1)[1])
        if minimum == 28:
            break
    return minimum


def shift_range_fits(start: datetime, kind: str, interval: int = 1, span: int = 1) -> bool:
    """Whether moving a whole range earlier can fit every selected month."""
    kind = str(kind).upper()
    if (kind not in ('MONTHLY', 'YEARLY') or type(interval) is not int
            or not 1 <= interval <= 999 or type(span) is not int or not 1 <= span <= 31):
        return False
    return span <= _minimum_month_length(start.year % 400, start.month, kind, interval)


def occupied_span(start: datetime, end: datetime, all_day: bool = False) -> int:
    """Number of occupied calendar days, respecting the exclusive end.

    An event ending at midnight does not occupy the following date. All-day
    ranges have the same exclusive-end convention as timed ranges.
    """
    local_end = end if all_day else end.astimezone(start.tzinfo)
    last = local_end - timedelta(microseconds=1) if end.timestamp() > start.timestamp() else start
    return max(1, (last.date() - start.date()).days + 1)


def range_policy_options(start: datetime, end: datetime, kind: str, interval: int = 1, *,
                         all_day: bool = False, preferred_day: int | None = None,
                         range_policy: str = '', span: int | None = None) -> tuple[bool, bool]:
    """Return whether overflow is possible and whether Cut is meaningful.

    Existing Cut intent survives a split whose full-duration reference now
    crosses a short month's boundary. New intentional cross-month ranges keep
    their ordinary duration and do not acquire these month-contained options.
    """
    kind = str(kind).upper()
    if (kind not in ('MONTHLY', 'YEARLY') or type(interval) is not int
            or not 1 <= interval <= 999):
        return False, False
    if start.tzinfo is None or end.tzinfo is None:
        return False, False
    local_end = end if all_day else end.astimezone(start.tzinfo)
    last = local_end - timedelta(microseconds=1) if end.timestamp() > start.timestamp() else start
    same_month = (start.year, start.month) == (last.year, last.month)
    month_contained = same_month or range_policy == 'cut' or span is not None
    # Intentional cross-month templates still offer the original start-only
    # Skip/Shift choice when their start day overflows. Their duration remains.
    days = ((occupied_span(start, end, all_day) if span is None else span)
            if month_contained else 1)
    preferred = start.day if preferred_day is None else preferred_day
    if type(days) is not int or type(preferred) is not int or not 1 <= days <= 31 or not 1 <= preferred <= 31:
        return False, False
    final_day = preferred + days - 1
    if final_day > 31:
        return False, False
    allow_cut = month_contained and days > 1
    if final_day <= 28:
        return False, allow_cut
    return (final_day > _minimum_month_length(start.year % 400, start.month, kind, interval), allow_cut)


def _frequency(master: Event) -> str:
    return next((part.partition('=')[2].strip().upper() for part in
                 master.recurrence_rule.upper().removeprefix('RRULE:').split(';')
                 if part.partition('=')[0].strip().upper() == 'FREQ'), '')


def cut_policy_active(master: Event) -> bool:
    return master.range_policy == 'cut' and _frequency(master) in ('MONTHLY', 'YEARLY')


def uncut_occurrence_end(master: Event, start: datetime) -> datetime:
    """Apply the reference's wall duration, including fold-only/point ranges."""
    from .recurrence import event_zone, normalize_recurrence_time
    zone = event_zone(master)
    wall_duration = (master.end.astimezone(zone).replace(tzinfo=None)
                     - master.start.astimezone(zone).replace(tzinfo=None))
    if wall_duration <= timedelta(0):
        end = datetime.fromtimestamp(start.timestamp() + master.end.timestamp() - master.start.timestamp(), zone)
    else:
        end = start + wall_duration
    return end if master.all_day else normalize_recurrence_time(end)


def occurrence_end(master: Event, start: datetime) -> datetime:
    """End of a native rule instance; explicit occurrence ranges bypass this."""
    from .recurrence import event_zone, normalize_recurrence_time
    end = uncut_occurrence_end(master, start)
    if not cut_policy_active(master):
        return end
    zone = event_zone(master)
    local_start = (datetime.combine(start.date(), time.min, zone) if master.all_day
                   else start.astimezone(zone))
    if local_start.month == 12:
        if local_start.year == 9999:
            return end
        boundary = datetime(local_start.year + 1, 1, 1, tzinfo=zone)
    else:
        boundary = datetime(local_start.year, local_start.month + 1, 1, tzinfo=zone)
    if not master.all_day:
        boundary = normalize_recurrence_time(boundary)
    return boundary if end.timestamp() > boundary.timestamp() else end


def cut_range_for_key(master: Event, key: str) -> tuple[datetime, datetime] | None:
    """A verified changed native range, excluding explicit additions/overrides."""
    from .recurrence import (_recurrence_set, key_datetime, normalize_recurrence_time,
                             recurrence_key)
    if (not cut_policy_active(master) or key in master.excluded_dates
            or key in master.recurrence_dates or key in master.recurrence_overrides):
        return None
    start = key_datetime(key, master)
    if not master.all_day:
        start = normalize_recurrence_time(start)
    try:
        end = occurrence_end(master, start)
        if end.timestamp() == uncut_occurrence_end(master, start).timestamp():
            return None
        rules = _recurrence_set(master, include_dates=False)
        previous = rules.before(start, inc=True)
        if previous is None or recurrence_key(previous, master.all_day) != recurrence_key(start, master.all_day):
            return None
    except (ValueError, OverflowError):
        return None
    return start, end


def iter_cut_ranges(master: Event) -> Iterator[tuple[str, datetime, datetime]]:
    """Yield only changed ends for standard exception export through year 9999.

    The compact master stays small in SQLite and local views. This iterator
    visits native rule starts once, never constructing thousands of Events or
    repeatedly seeking from DTSTART. Explicit exceptions and RDATE additions
    keep their chosen ranges and are exported by the ordinary path instead.
    """
    if not cut_policy_active(master):
        return
    from .recurrence import (_recurrence_set, normalize_recurrence_stream,
                             recurrence_key)
    skip = set(master.excluded_dates) | set(master.recurrence_dates) | set(master.recurrence_overrides)
    rules = _recurrence_set(master, include_dates=False)
    for start in normalize_recurrence_stream(rules, all_day=master.all_day):
        key = recurrence_key(start, master.all_day)
        if key in skip:
            continue
        try:
            end = occurrence_end(master, start)
            original = uncut_occurrence_end(master, start)
        except (OverflowError, ValueError):
            # The standard's four-digit years cannot encode an end beyond
            # 9999. There is no representable exception for that boundary.
            continue
        if end.timestamp() < original.timestamp():
            yield key, start, end
