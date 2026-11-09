"""Calendar-boundary range policies without expanding exceptions in storage.

Skip and Shift can be expressed by standard RRULE clauses. Cut keeps that
rule's starts and truncates overflowing native ranges at the month boundary.
Its finite standard exceptions are derived only when exporting to iCalendar.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from math import gcd
from typing import Iterator

from .models import Event
from .event_duration import add_elapsed, elapsed_duration, end_for, parse_duration_spec


RANGE_POLICIES = ('skip', 'shift', 'cut')


def static_range_policy_fits(prototype: Event, kind: str, interval: int = 1, *,
                             policy: str, span: int, preferred_day: int | None = None,
                             count: int | None = None, until: date | None = None) -> bool:
    """Whether a native Skip/Shift rule can safely retain this duration.

    Plain imported rules are unrestricted. This preflight is only for authored
    full-month-range policies, whose static start constraints cannot repair an
    exact-hour end that crosses a month boundary during a clock change.
    """
    if policy not in ('skip', 'shift') or span <= 1:
        return True
    from .recurrence import event_zone
    if prototype.floating_time:
        # A floating clock has a neutral wall-time duration, so display-zone
        # daylight changes must not alter the month-boundary preflight.
        return _static_range_policy_fits(prototype.start.replace(tzinfo=timezone.utc).isoformat(),
            prototype.end.replace(tzinfo=timezone.utc).isoformat(), timezone.utc, prototype.all_day,
            prototype.duration_spec, str(kind).upper(), interval, policy, span, preferred_day, count, until)
    return _static_range_policy_fits(prototype.start.isoformat(), prototype.end.isoformat(),
        event_zone(prototype), prototype.all_day, prototype.duration_spec, str(kind).upper(), interval,
        policy, span, preferred_day, count, until)


@lru_cache(maxsize=512)
def _static_range_policy_fits(start_text, end_text, zone, all_day, duration_spec, kind, interval,
                              policy, span, preferred_day, count, until):
    if kind not in ('MONTHLY', 'YEARLY') or type(interval) is not int or not 1 <= interval <= 999:
        return False
    start, end = (datetime.fromisoformat(value).astimezone(zone) for value in (start_text, end_text))
    preferred = start.day if preferred_day is None else preferred_day
    if not 1 <= preferred <= 31 or not 1 <= span <= 31 or preferred + span - 1 > 31:
        return False
    if occupied_span(start, end, all_day) > span:
        return False
    months = interval * (12 if kind == 'YEARLY' else 1)
    initial = start.year * 12 + start.month - 1
    cycle = 4800 // gcd(months, 4800)
    # A valid selected date repeats at least once per Gregorian phase cycle.
    # Finite bounds near year 9999 still inspect their terminal endpoint.
    bounds_safe = (count is None and until is None or until is not None and until.year < 9999
                   or count is not None and (initial + months * cycle * max(0, count - 1)) // 12 < 9999)
    if bounds_safe and (all_day or isinstance(zone, timezone) or
                        not getattr(zone, 'identifier', '') and str(zone) in {'UTC', 'Etc/UTC', 'Etc/GMT', 'GMT'}):
        return True
    nominal_days = nominal_seconds = 0
    if duration_spec:
        nominal_days, nominal_seconds = parse_duration_spec(duration_spec)
        if bounds_safe and not nominal_seconds and start.time() == time.min:
            return True  # Nominal midnight ends retain calendar-boundary intent.
    # Any legal two UTC offsets differ by less than 48 hours. Sufficient wall
    # slack therefore proves exact ranges safe without enumerating centuries.
    # Nominal day normalization and its elapsed clock part get separate slack.
    clock = start.hour * 3600 + start.minute * 60 + start.second + start.microsecond / 1_000_000
    remaining = []
    for last in range(28, 32):
        if policy == 'skip' and preferred + span - 1 > last:
            continue
        day = preferred if policy == 'skip' else min(preferred, last - span + 1)
        if day < 1:
            return False
        remaining.append((last - day + 1) * 86400 - clock)
    seconds = elapsed_duration(start, end).total_seconds()
    conservative = (nominal_days * 86400 + nominal_seconds + 96 * 3600 if duration_spec else
                    seconds + 48 * 3600)
    if bounds_safe and remaining and conservative <= min(remaining):
        return True
    prototype = Event('range-check', 'range-check', 'Range check', start, end, all_day=all_day,
                      timezone_id=getattr(zone, 'key', ''), duration_spec=duration_spec)
    maximum = (9999 * 12 + 11 - initial) // months + 1
    emitted = 0
    clock_time = start.timetz().replace(fold=0)
    nominal_delta = timedelta(days=nominal_days)
    from .recurrence import normalize_recurrence_time
    for period in range(maximum):
        year, position = divmod(initial + period * months, 12)
        month = position + 1
        if until is not None and date(year, month, 1) > until:
            return True
        last = monthrange(year, month)[1]
        if policy == 'skip' and preferred + span - 1 > last:
            continue
        day = preferred if policy == 'skip' else min(preferred, last - span + 1)
        if day < 1:
            return False
        nominal = datetime.combine(date(year, month, day), clock_time)
        if period == 0 and nominal.timestamp() < start.timestamp():
            continue
        if until is not None and nominal.date() > until:
            return True
        if year == 9999 and month == 12:
            try:
                actual = nominal if all_day else normalize_recurrence_time(nominal)
                final = _last_occupied(actual, end_for(prototype, actual), all_day)
            except (ValueError, OverflowError):
                if count is None and until is None:
                    continue  # Unbounded rules stop at the representable horizon.
                return False
            if (final.year, final.month) != (year, month):
                return False
        else:
            boundary = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
            if all_day:
                pass  # The static occupied-day constraint proves these ranges.
            elif duration_spec:
                # Most starts are ordinary clocks. Only a forward gap needs
                # a round trip before nominal days advance from its real time.
                actual = nominal
                if nominal.replace(fold=1).utcoffset() > nominal.utcoffset():
                    actual = normalize_recurrence_time(nominal)
                    if (actual.year, actual.month) != (year, month):
                        return False
                cursor = actual + nominal_delta
                available = ((boundary - cursor).total_seconds()
                             - (boundary.utcoffset() - cursor.utcoffset()).total_seconds())
                if nominal_seconds > available:
                    return False
            else:
                wall_remaining = (last - day + 1) * 86400 - clock
                actual_remaining = wall_remaining - (boundary.utcoffset() - nominal.utcoffset()).total_seconds()
                if actual_remaining <= 0 or seconds > actual_remaining:
                    return False
        emitted += 1
        if count is not None and emitted >= count:
            return True
    return count is None or emitted >= count


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


def _last_occupied(start: datetime, end: datetime, all_day: bool) -> datetime:
    if end.timestamp() <= start.timestamp():
        return start
    local_end = end if all_day else end.astimezone(start.tzinfo)
    return (local_end - timedelta(microseconds=1) if all_day else
            add_elapsed(local_end, -timedelta(microseconds=1)))


def occupied_span(start: datetime, end: datetime, all_day: bool = False) -> int:
    """Number of occupied calendar days, respecting the exclusive end.

    An event ending at midnight does not occupy the following date. All-day
    ranges have the same exclusive-end convention as timed ranges.
    """
    last = _last_occupied(start, end, all_day)
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
    last = _last_occupied(start, end, all_day)
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
    """Apply the master's exact or nominal iCalendar duration to this start."""
    return end_for(master, start)


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
    if not master.all_day and not master.floating_time:
        boundary = normalize_recurrence_time(boundary)
    return boundary if end.timestamp() > boundary.timestamp() else end


def cut_range_for_key(master: Event, key: str) -> tuple[datetime, datetime] | None:
    """A verified changed native range, excluding explicit additions/overrides."""
    from .recurrence import (recurrence_set, key_datetime, normalize_recurrence_time,
                             recurrence_key)
    if (not cut_policy_active(master) or key in master.excluded_dates
            or key in master.recurrence_dates or key in master.recurrence_overrides):
        return None
    start = key_datetime(key, master)
    if not master.all_day and not master.floating_time:
        start = normalize_recurrence_time(start)
    try:
        end = occurrence_end(master, start)
        if end.timestamp() == uncut_occurrence_end(master, start).timestamp():
            return None
        rules = recurrence_set(master, include_dates=False)
        previous = rules.before(start, inc=True)
        if previous is None or recurrence_key(previous, master.all_day, floating_time=master.floating_time) != recurrence_key(start, master.all_day, floating_time=master.floating_time):
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
    from .recurrence import (recurrence_set, normalize_recurrence_stream,
                             recurrence_key)
    skip = set(master.excluded_dates) | set(master.recurrence_dates) | set(master.recurrence_overrides)
    # Export intentionally streams the complete four-digit calendar horizon.
    # At most 119,988 monthly periods exist; views retain the smaller default
    # budget, and the ICS range adapter separately caps exported exceptions.
    rules = recurrence_set(master, include_dates=False, work_limit=120000)
    for start in normalize_recurrence_stream(rules, all_day=master.all_day or master.floating_time):
        key = recurrence_key(start, master.all_day, floating_time=master.floating_time)
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
