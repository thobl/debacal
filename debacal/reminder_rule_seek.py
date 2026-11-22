"""Resumable nominal rank lookup when the prepared iterator exceeds its view budget."""
from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

from dateutil.rrule import DAILY, HOURLY, MONTHLY, WEEKLY, YEARLY, _iterinfo

from .prepared_recurrence import _CalendarSeek
from .reminder_compilation import DAY_US, MAX_US, instant, micros


def _following(start, rule):
    stride = rule._interval
    if rule._freq == YEARLY:
        return datetime(start.year + stride, 1, 1)
    if rule._freq == MONTHLY:
        year, month = divmod(start.year * 12 + start.month - 1 + stride, 12)
        return datetime(year, month + 1, 1)
    if rule._freq == WEEKLY:
        days = 7 * stride - (start.weekday() - rule._wkst) % 7
        return datetime.combine(start.date() + timedelta(days=days), time.min)
    if rule._freq == DAILY:
        return datetime.combine(start.date() + timedelta(days=stride), time.min)
    return start.replace(minute=0, second=0) + timedelta(hours=stride)


def _rank(days, clocks, bound):
    """First Cartesian (day,clock) position at/after this naive civil clock."""
    ordinal = bound.toordinal()
    day = bisect_left(days, ordinal)
    position = day * len(clocks)
    if day < len(days) and days[day] == ordinal:
        position += bisect_left(clocks, bound.time(), key=lambda t: t.replace(tzinfo=None))
    return position


def seek_slice(series, lower, upper, checkpoint):
    """Return (earliest start, continuation). A completed empty probe covers [lower, upper]."""
    rule, zone = series.rule, series.zone
    fixed = isinstance(zone, timezone) or getattr(zone, 'key', '') in ('UTC', 'Etc/UTC', 'GMT')
    # UTC = nominal wall coordinate - originating offset, including a gap.
    # With variable offsets no distant endpoint comparison proves monotonicity.
    offset = int(instant(lower).astimezone(zone).utcoffset() // timedelta(microseconds=1)) if fixed else None
    low_wall = instant(lower + (offset if fixed else -DAY_US)).replace(tzinfo=None)
    if checkpoint is None:
        sought = series.seek.seek(instant(lower if fixed else lower - 2*DAY_US))
        if sought is None:
            return None, None
        start, count = sought
        start, position, best = start.replace(tzinfo=None), 0, None
    else:
        text, count, position, best = checkpoint
        start = datetime.fromisoformat(text)
    masks = SimpleNamespace(rule=rule, frequency=rule._freq)
    info = _iterinfo(rule)
    until = micros(rule._until) if rule._until else MAX_US
    budget = 256
    while budget and (count is None or count > 0):
        high = min(upper, until, best if best is not None else MAX_US)
        high_wall = instant(high + (offset if fixed else DAY_US)).replace(tzinfo=None)
        if start > high_wall:
            break
        y, m, d = start.year, start.month, start.day
        frequency = DAILY if rule._freq == HOURLY else rule._freq
        days = _CalendarSeek._days(masks, info, y, m, d, frequency=frequency)
        clocks = rule._timeset
        if rule._freq == HOURLY:
            if rule._byhour and start.hour not in rule._byhour:
                days = []
            clocks = info.htimeset(start.hour, start.minute, start.second)
        positions = _CalendarSeek._positions(masks, len(days)*len(clocks))
        first = bisect_left(positions, _rank(days, clocks, start))
        end = len(positions) if count is None else min(len(positions), first+count)
        position = max(position, first, bisect_left(positions, _rank(days, clocks, low_wall)))
        while position < end and budget:
            day_index, clock_index = divmod(positions[position], len(clocks))
            wall = datetime.combine(date.fromordinal(days[day_index]), clocks[clock_index]).replace(tzinfo=None)
            if wall > high_wall:
                return (instant(best).astimezone(zone) if best is not None else None), None
            candidate = micros(wall.replace(tzinfo=zone, fold=0))
            if lower <= candidate <= high:
                best = high = candidate
                if fixed:
                    return instant(best).astimezone(zone), None
                high_wall = instant(high+DAY_US).replace(tzinfo=None)
            position += 1
            budget -= 1
        if position < end:
            break
        if count is not None:
            count -= end-first
        try:
            start = _following(start, rule)
        except (ValueError, OverflowError):
            break
        position = 0
        budget = max(0, budget-1)
    if budget == 0:
        return None, [start.isoformat(), count, position, best]
    return (instant(best).astimezone(zone) if best is not None else None), None
