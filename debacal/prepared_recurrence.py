"""Immutable, write-time preparation for arbitrary recurrence-window queries.

This is an index representation, not a query-result cache: constructing one
does all parsing and finite-exception work before any view asks for a window.
Queries never retain their results or advance a shared recurrence iterator.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from copy import copy
from datetime import date, datetime, time, timedelta, timezone
from math import gcd

from dateutil.rrule import DAILY, HOURLY, MINUTELY, MONTHLY, WEEKLY, YEARLY, _iterinfo, weekday

from .event_duration import add_elapsed, elapsed_duration, parse_duration_spec
from .models import Event
from .recurrence import (
    RecurrenceRangeError, _floating_master, _recurrence_set,
    _static_periods, event_zone, key_datetime, normalize_recurrence_stream,
    normalize_recurrence_time, occurrence, occurrence_id, overlaps, recurrence_key,
)
from .recurrence_budget import BoundedRule


def _effective_arguments(rule):
    """Make DTSTART-derived defaults explicit before changing its period.

    dateutil.replace preserves the spelling of the original rule. Its omitted
    BYMONTHDAY/BYHOUR defaults would otherwise be re-derived from the new start.
    The supported dateutil 2.x engine already exposes these normalized fields.
    """
    return {
        'bymonth': rule._bymonth,
        'bymonthday': (*rule._bymonthday, *rule._bynmonthday),
        'byweekday': (*(rule._byweekday or ()),
                      *(weekday(day, number) for day, number in rule._bynweekday or ())),
        'byhour': rule._byhour, 'byminute': rule._byminute, 'bysecond': rule._bysecond,
    }


class _StaticSeek:
    """Predecoded phase and rank arithmetic for hourly/daily/weekly rules."""
    def __init__(self, fields, start):
        self.start = start
        self.frequency = fields['FREQ']
        self.interval = int(fields.get('INTERVAL', 1))
        self.count = int(fields['COUNT']) if 'COUNT' in fields else None
        names = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
        selected_days = fields.get('BYDAY', '').split(',') if fields.get('BYDAY') else None
        days = {names.index(day) for day in selected_days} if selected_days else set(range(7))
        hours = sorted({int(value) for value in fields.get('BYHOUR', str(start.hour)).split(',')})
        minutes = sorted({int(value) for value in fields.get('BYMINUTE', str(start.minute)).split(',')})
        seconds = sorted({int(value) for value in fields.get('BYSECOND', str(start.second)).split(',')})
        clock_hours = [start.hour] if self.frequency == 'HOURLY' else hours
        size = len(clock_hours) * len(minutes) * len(seconds)
        before = (sum(hour < start.hour for hour in clock_hours) * len(minutes) * len(seconds)
                  + (sum(minute < start.minute for minute in minutes) * len(seconds)
                     + (sum(second < start.second for second in seconds) if start.minute in minutes else 0)
                     if start.hour in clock_hours else 0))

        def positions(length):
            if 'BYSETPOS' not in fields:
                return length, None
            values = {value - 1 if value > 0 else length + value
                      for value in map(int, fields['BYSETPOS'].split(','))}
            values = {value for value in values if 0 <= value < length}
            return len(values), values

        if self.frequency == 'WEEKLY':
            self.multiplicity, _ = positions(size * (len(days) if selected_days else 1))
            self.pattern, self.partial = (True,), 0
        else:
            self.multiplicity, selected = positions(size)
            self.partial = before if selected is None else sum(value < before for value in selected)
            cycle = 168 if self.frequency == 'HOURLY' else 7
            allowed_hours = set(hours) if 'BYHOUR' in fields else set(range(24))
            pattern = []
            for index in range(cycle // gcd(cycle, self.interval)):
                phase = ((start.weekday() * 24 + start.hour + index * self.interval) % 168
                         if self.frequency == 'HOURLY' else (start.weekday() + index * self.interval) % 7)
                pattern.append(phase // 24 in days and phase % 24 in allowed_hours
                               if self.frequency == 'HOURLY' else phase in days)
            self.pattern = tuple(pattern)
        self.prefix = [0]
        for allowed in self.pattern:
            self.prefix.append(self.prefix[-1] + allowed)
        self.unit = timedelta(hours=1) if self.frequency == 'HOURLY' else timedelta(
            days=7 if self.frequency == 'WEEKLY' else 1)

    def seek(self, after):
        if not self.multiplicity or not self.prefix[-1]:
            return None
        elapsed = after.astimezone(self.start.tzinfo).replace(tzinfo=None) - self.start.replace(tzinfo=None)
        skipped = max(0, elapsed // (self.unit * self.interval) - 1)
        if not skipped:
            return self.start, self.count
        count = self.count
        if count is not None:
            rounds, remainder = divmod(skipped, len(self.pattern))
            full = rounds * self.prefix[-1] + self.prefix[remainder]
            consumed = (full * self.multiplicity - self.partial * self.pattern[0]
                        + self.partial * self.pattern[remainder])
            count -= consumed
            if count <= 0:
                return None
        return self.start + self.unit * (self.interval * skipped), count

    def upper(self):
        if self.count is None:
            return None
        if not self.multiplicity or not self.prefix[-1]:
            return self.start
        # A conservative bound needs no expansion or inverse-rank search.
        periods = (self.count + 1) * len(self.pattern)
        try:
            return self.start + self.unit * (self.interval * periods)
        except OverflowError:
            return None


class _CalendarSeek:
    """Calendar-period seeking with an eager Gregorian rank table for COUNT.

    Gregorian selectors repeat every 400 years. MONTHLY has at most 4,800
    phases and YEARLY at most 400, independent of a series' age or count.
    Prefix counts select the remaining COUNT in constant time, without
    enumerating historical instances or retaining a previous query's work.
    """
    def __init__(self, rule):
        self.rule, self.start = rule, rule._dtstart
        self.frequency = rule._freq
        self.monthly = rule._freq == MONTHLY
        self.interval = rule._interval
        self.count = rule._count
        self.cyclic = not rule._byeaster
        if self.frequency in (DAILY, WEEKLY):
            self.week_offset = (self.start.weekday() - rule._wkst) % 7 if self.frequency == WEEKLY else 0
            self.origin = self.start.date().toordinal() - self.week_offset
            self.step = self.interval * (7 if self.frequency == WEEKLY else 1)
            self.periods = 146097 // gcd(146097, self.step)
            if not self.cyclic:
                self.periods = (date.max.toordinal() - self.origin) // self.step + 1
        else:
            self.origin = self.start.year * 12 + self.start.month - 1 if self.monthly else self.start.year
            self.periods = (4800 if self.monthly else 400) // gcd(4800 if self.monthly else 400, self.interval)
            if not self.cyclic:
                self.periods = ((9999 * 12 + 11 - self.origin) // self.interval + 1 if self.monthly else
                                (9999 - self.origin) // self.interval + 1)
        self.times = tuple(rule._timeset)
        self.prefix = None
        self.partial = 0
        self.complete_cycle = self.cyclic
        if self.count is not None:
            self.prefix = array('Q', [0])
            info = _iterinfo(rule)
            for period in range(self.periods):
                year, month, day = self._phase(period)
                days = self._days(info, year, month, day)
                size = len(days) * len(self.times)
                self.prefix.append(self.prefix[-1] + len(self._positions(size)))
                if period == 0:
                    origin = (datetime(year, month, day, self.start.hour, self.start.minute, self.start.second)
                              + timedelta(days=self.week_offset)) if self.frequency in (DAILY, WEEKLY) else self.start.replace(year=year)
                    before_days = sum(value < origin.date().toordinal() for value in days)
                    before_clocks = sum(value.replace(tzinfo=None) < origin.timetz().replace(tzinfo=None)
                                        for value in self.times)
                    before = before_days * len(self.times) + (
                        before_clocks if origin.date().toordinal() in days else 0)
                    self.partial = sum(value < before for value in self._positions(size))
                if self.prefix[-1] - self.partial >= self.count:
                    # Most finite schedules end long before a Gregorian cycle.
                    # This prefix already proves all subsequent periods empty.
                    self.complete_cycle = self.cyclic and period + 1 == self.periods
                    break

    def _phase(self, period):
        if self.frequency in (DAILY, WEEKLY):
            if not self.cyclic:
                value = date.fromordinal(max(1, self.origin + period * self.step))
                return value.year, value.month, value.day
            base = date(2000, 1, 1).toordinal()
            value = date.fromordinal(base + (self.origin + period * self.step - base) % 146097)
            return value.year, value.month, value.day
        unit = self.origin + period * self.interval
        if self.monthly:
            if not self.cyclic:
                year, month = divmod(unit, 12)
                return year, month + 1, 1
            year, month = divmod(unit % 4800, 12)
            return 2000 + year, month + 1, 1
        return (2000 + unit % 400 if self.cyclic else unit), self.start.month, 1

    def _days(self, info, year, month, day, *, frequency=None):
        """Use the engine's calendar masks to count nominal selected days."""
        rule = self.rule
        info.rebuild(year, month)
        dayset, begin, end = ({DAILY: info.ddayset, WEEKLY: info.wdayset,
                              MONTHLY: info.mdayset, YEARLY: info.ydayset}[
                                  self.frequency if frequency is None else frequency](year, month, day))
        selected = []
        for index in dayset[begin:end]:
            if ((rule._bymonth and info.mmask[index] not in rule._bymonth)
                    or (rule._byweekno and not info.wnomask[index])
                    or (rule._byweekday and info.wdaymask[index] not in rule._byweekday)
                    or (info.nwdaymask and not info.nwdaymask[index])
                    or (rule._byeaster and not info.eastermask[index])
                    or ((rule._bymonthday or rule._bynmonthday)
                        and info.mdaymask[index] not in rule._bymonthday
                        and info.nmdaymask[index] not in rule._bynmonthday)
                    or (rule._byyearday and ((index < info.yearlen and index + 1 not in rule._byyearday
                                             and -info.yearlen + index not in rule._byyearday)
                                            or (index >= info.yearlen and index + 1 - info.yearlen not in rule._byyearday
                                                and -info.nextyearlen + index - info.yearlen not in rule._byyearday)))):
                continue
            selected.append(info.yearordinal + index)
        return selected

    def _positions(self, size):
        if not self.rule._bysetpos:
            return range(size)
        return sorted({value - 1 if value > 0 else size + value
                       for value in self.rule._bysetpos if 0 < value <= size or -size <= value < 0})

    def _period_start(self, period):
        if self.frequency in (DAILY, WEEKLY):
            return datetime.combine(date.fromordinal(self.origin + self.step * period), time.min,
                                    self.start.tzinfo)
        unit = self.origin + self.interval * period
        if self.monthly:
            year, month = divmod(unit, 12)
            return datetime(year, month + 1, 1, tzinfo=self.start.tzinfo)
        return datetime(unit, 1, 1, tzinfo=self.start.tzinfo)

    def seek(self, after):
        boundary = after.astimezone(self.start.tzinfo)
        if self.frequency in (DAILY, WEEKLY):
            skipped = max(0, (boundary.date().toordinal() - self.origin) // self.step - 1)
        else:
            unit = boundary.year * 12 + boundary.month - 1 if self.monthly else boundary.year
            skipped = max(0, (unit - self.origin) // self.interval - 1)
        if not skipped:
            return self.start, self.count
        count = self.count
        if count is not None:
            if not self.complete_cycle and skipped >= len(self.prefix) - 1:
                return None
            rounds, remainder = divmod(skipped, self.periods) if self.complete_cycle else (0, skipped)
            consumed = rounds * self.prefix[-1] + self.prefix[remainder] - self.partial
            count -= consumed
            if count <= 0 or not self.prefix[-1]:
                return None
        return self._period_start(skipped), count

    def upper(self):
        if self.count is None:
            return None
        if not self.prefix[-1]:
            return self.start
        rank = self.count - 1 + self.partial
        rounds, remainder = divmod(rank, self.prefix[-1]) if self.complete_cycle else (0, rank)
        period = rounds * self.periods + bisect_right(self.prefix, remainder) - 1
        try:
            return self._period_start(period + 1)
        except (ValueError, OverflowError):
            return None


class _HourlySeek:
    """Eager ranks of calendar-filtered hours, counted a day at a time.

    A date contributes at most 24 hour positions regardless of how many
    minute/second candidates each position emits. Prefix cardinalities make
    an arbitrary suffix query independent of the age of a counted series.
    """
    def __init__(self, rule):
        self.rule, self.start = rule, rule._dtstart
        self.count, self.interval = rule._count, rule._interval
        self.origin = self.start.date().toordinal() * 24 + self.start.hour
        clocks = sorted((minute, second) for minute in rule._byminute for second in rule._bysecond)
        if rule._bysetpos:
            positions = sorted({value - 1 if value > 0 else len(clocks) + value
                                for value in rule._bysetpos if 0 < value <= len(clocks) or -len(clocks) <= value < 0})
            clocks = [clocks[index] for index in positions]
        self.multiplicity = len(clocks)
        self.partial = sum(value < (self.start.minute, self.start.second) for value in clocks)
        self.days, self.prefix = array('I'), array('Q', [0])
        if not self.multiplicity:
            return
        info = _iterinfo(rule)
        year, month = self.start.year, self.start.month
        while year <= 9999:
            if not rule._bymonth or month in rule._bymonth:
                selected = _CalendarSeek._days(self, info, year, month, 1, frequency=MONTHLY)
                for ordinal in selected:
                    if ordinal < self.start.date().toordinal() or ordinal > date.max.toordinal():
                        continue
                    hours = self._hours(ordinal)
                    count = len(hours) * self.multiplicity
                    if ordinal == self.start.date().toordinal() and self.start.hour in hours:
                        count -= self.partial
                    if count:
                        self.days.append(ordinal)
                        self.prefix.append(self.prefix[-1] + count)
                    if self.prefix[-1] >= self.count:
                        return
            if rule._until is not None and (year, month) >= (rule._until.year, rule._until.month):
                return
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    def _hours(self, ordinal):
        first = max(0, -((self.origin - ordinal * 24) // self.interval))
        last = (ordinal * 24 + 23 - self.origin) // self.interval
        hours = ((self.origin + index * self.interval) % 24 for index in range(first, last + 1))
        return tuple(hour for hour in hours if self.rule._byhour is None or hour in self.rule._byhour)

    def seek(self, after):
        elapsed = after.astimezone(self.start.tzinfo).replace(tzinfo=None) - self.start.replace(tzinfo=None)
        skipped = max(0, elapsed // timedelta(hours=self.interval) - 1)
        if not skipped:
            return self.start, self.count
        start = self.start + timedelta(hours=skipped * self.interval)
        ordinal = start.date().toordinal()
        index = bisect_left(self.days, ordinal)
        consumed = self.prefix[index]
        if index < len(self.days) and self.days[index] == ordinal:
            hours = self._hours(ordinal)
            consumed += sum(hour < start.hour for hour in hours) * self.multiplicity
            if ordinal == self.start.date().toordinal() and self.start.hour in hours:
                consumed -= self.partial
            if start.hour in hours:
                consumed += self.partial
        count = self.count - consumed
        return (start, count) if count > 0 else None

    def upper(self):
        if not self.days:
            return self.start
        try:
            return datetime.combine(date.fromordinal(self.days[-1] + 1), time.min, self.start.tzinfo)
        except (ValueError, OverflowError):
            return None


class PreparedSeries:
    """Decoded finite intervals and a prepared rule for one immutable master."""
    def __init__(self, master: Event):
        self.master = master
        self.floating = master.all_day or master.floating_time
        self.generation_master = _floating_master(master) if self.floating else master
        self.zone = event_zone(self.generation_master)
        self.event_zone = event_zone(master)
        self.nominal_duration = parse_duration_spec(master.duration_spec) if master.duration_spec else None
        self.duration = (timedelta(days=max(1, (master.end.date() - master.start.date()).days)) if master.all_day else
                         master.end.replace(tzinfo=None) - master.start.replace(tzinfo=None) if master.floating_time else
                         elapsed_duration(master.start, master.end))
        self.excluded = frozenset(master.excluded_dates)
        self.override_keys = frozenset(master.recurrence_overrides)
        self.added_keys = frozenset(master.recurrence_dates)
        self.rule, self.seek = None, None
        self._finite_values = None
        self._finite_starts = ()
        self.calendar_periods = False
        self.effective = None
        if master.recurrence_rule:
            # This runs canonicalization, validation, parsing and exclusions
            # once. Queries use the rule directly and test excluded keys by set.
            rules = _recurrence_set(self.generation_master, include_dates=False)
            if rules._rrule:
                bounded = rules._rrule[0]
                self.rule = bounded.rule
                self.calendar_periods = bounded.calendar_periods
                self.effective = _effective_arguments(self.rule)
                fields = dict(piece.split('=', 1) for piece in str(self.rule).split('RRULE:')[-1].strip().split(';'))
                if self.rule._count is not None and (self.rule._count <= 256 or self.rule._freq == MINUTELY):
                    # Small finite schedules are represented by their starts
                    # immediately. Don't build a 400-year rank table for a
                    # monthly event with only a dozen occurrences.
                    try:
                        # Supported MINUTELY rules have a validated finite
                        # count <=10,000 and no calendar filters, so traversal
                        # is bounded directly by their count.
                        values = self.rule if self.rule._freq == MINUTELY else bounded
                        self._finite_values = tuple(normalize_recurrence_stream(values, all_day=self.floating))
                        self._finite_starts = tuple(value.timestamp() for value in self._finite_values)
                    except RecurrenceRangeError:
                        # A small COUNT can still have very sparse selectors.
                        # Build its rank structure instead of scanning again.
                        pass
                if self._finite_values is None:
                    if _static_periods(fields):
                        self.seek = _StaticSeek(fields, self.rule._dtstart)
                    elif self.rule._freq in (DAILY, WEEKLY, MONTHLY, YEARLY):
                        self.seek = _CalendarSeek(self.rule)
                    elif self.rule._freq == HOURLY and self.rule._count is None:
                        # Calendar filters do not alter the hourly period phase.
                        self.seek = _StaticSeek({'FREQ': fields['FREQ'], 'INTERVAL': str(self.rule._interval)},
                                                self.rule._dtstart)
                    elif self.rule._freq == HOURLY:
                        self.seek = _HourlySeek(self.rule)
        self.explicit_occurrences = self._explicit()
        self.cut = master.range_policy == 'cut' and self.rule is not None and self.rule._freq in (MONTHLY, YEARLY)
        self._explicit_starts = tuple(item.start.timestamp() for item in self.explicit_occurrences)
        maximum = float('-inf')
        ends = []
        for item in self.explicit_occurrences:
            maximum = max(maximum, item.end.timestamp())
            ends.append(maximum)
        self._explicit_ends = tuple(ends)
        self.rule_bounds = self._bounds()

    def _explicit(self):
        master = self.master
        initial = self.generation_master.start.astimezone(self.zone)
        if initial.fold and self.rule is not None:
            # DTSTART's nominal fold is owned by the rule when it matches.
            anchor = self.rule.before(initial, inc=True)
            if anchor is not None and anchor.replace(tzinfo=None) == initial.replace(tzinfo=None):
                initial = anchor
        starts = {recurrence_key(initial, master.all_day, floating_time=master.floating_time): initial}
        starts.update((key, key_datetime(key, self.generation_master)) for key in master.recurrence_dates)
        result = {}
        for key, value in starts.items():
            if key in self.excluded or key in self.override_keys:
                continue
            actual = (datetime.combine(value.date(), time.min, event_zone(master)) if master.all_day else
                      value.replace(tzinfo=event_zone(master)) if master.floating_time else value)
            item = occurrence(master, actual, key)
            result[item.id] = item
        for key in self.override_keys - self.excluded:
            item = occurrence(master, key_datetime(key, master), key)
            result[item.id] = item
        return tuple(sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id)))

    def _bounds(self):
        if self.rule is None:
            return None
        begin = self.rule._dtstart
        finite_end = self._finite_values[-1] if self._finite_values else None
        bounds = [value for value in (self.rule._until, finite_end,
                                      self.seek.upper() if self.seek else None) if value is not None]
        upper = min(bounds, key=lambda value: value.timestamp()) if bounds else None
        if upper is not None:
            try:
                days, seconds = self.nominal_duration or (0, 0)
                duration = (timedelta(days=days + 2, seconds=seconds) if self.master.duration_spec else
                            self.generation_master.end - self.generation_master.start if self.floating else
                            elapsed_duration(self.master.start, self.master.end))
                # An inclusive UNTIL may contain a point event exactly there.
                upper = add_elapsed(upper, duration + timedelta(microseconds=1))
            except OverflowError:
                upper = None
        return begin, upper

    def _lower(self, start, zone=None):
        try:
            if self.floating:
                wall = ((start.astimezone(zone or self.event_zone) if self.master.floating_time else start)
                        .replace(tzinfo=timezone.utc, fold=0))
                return wall - (self.generation_master.end - self.generation_master.start)
            if self.master.duration_spec:
                days, seconds = self.nominal_duration
                return start.astimezone(self.zone) - timedelta(days=days + 2, seconds=seconds)
            return add_elapsed(start.astimezone(self.zone), -elapsed_duration(self.master.start, self.master.end))
        except OverflowError:
            return None

    def _regular(self, lower, window_end=None):
        if self.rule is None:
            return iter(())
        if self._finite_values is not None:
            first = 0 if lower is None else bisect_left(self._finite_starts, lower.timestamp())
            return (self._finite_values[index] for index in range(first, len(self._finite_values)))
        rule = self.rule
        if lower is not None and self.seek is not None:
            sought = self.seek.seek(lower)
            if sought is None:
                return iter(())
            start, count = sought
            if start != rule._dtstart:
                rule = rule.replace(dtstart=start, count=count, cache=False, **self.effective)
        bounded = BoundedRule(rule, RecurrenceRangeError, calendar_periods=self.calendar_periods,
                              window_end=window_end if self.calendar_periods else None)
        values = normalize_recurrence_stream(bounded, all_day=self.floating)
        if lower is None:
            return values
        instant = lower.timestamp()
        return (value for value in values if value.timestamp() >= instant)

    def query_rule(self, start, end, *, limit=10000, truncate=False):
        if truncate and limit <= 0 or self.rule is None:
            return []
        master = self.master
        if master.floating_time:
            from .timeutil import local_zone
            zone = local_zone()
        else:
            zone = self.event_zone
        lower = self._lower(start, zone)
        boundary = ((end.astimezone(zone) if master.floating_time else end)
                    .replace(tzinfo=timezone.utc, fold=0)) if self.floating else end
        result = {}
        for value in self._regular(lower, boundary.astimezone(self.zone)):
            if value.timestamp() >= boundary.timestamp():
                break
            key = recurrence_key(value, master.all_day, floating_time=master.floating_time)
            if key in self.excluded or key in self.override_keys:
                continue
            actual = (datetime.combine(value.date(), time.min, zone) if master.all_day else
                      value.replace(tzinfo=zone) if master.floating_time else value)
            item = self._project(actual, key)
            if overlaps(item, start, end):
                result[item.id] = item
                if truncate and len(result) >= limit:
                    break
                if not truncate and len(result) > limit:
                    raise RecurrenceRangeError(f'This range exceeds {limit:,} occurrences. Choose a smaller date range.')
        return sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))

    def occurrence_at(self, key):
        """Return one native rule identity without looking back its duration.

        Store indexes explicit additions and detached payloads separately.
        Membership is a question about one generated start, not all ranges
        overlapping that instant; long event durations must not expand history.
        """
        if self.rule is None or key in self.excluded or key in self.override_keys:
            return None
        master = self.master
        try:
            if master.all_day:
                target = datetime.combine(date.fromisoformat(key), time.min, timezone.utc)
            elif master.floating_time:
                target = datetime.fromisoformat(key).replace(tzinfo=timezone.utc, fold=0)
            else:
                target = datetime.fromisoformat(key)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=self.event_zone)
                target = target.astimezone(self.zone)
            if recurrence_key(target, master.all_day, floating_time=master.floating_time) != key:
                return None
        except (ValueError, OverflowError, TypeError):
            return None
        # The window guard inspects calendar periods, so the target's own day
        # is sufficient even at year 9999, without overflowing target + 1 sec.
        instant = target.timestamp()
        for value in self._regular(target, target):
            candidate = value.timestamp()
            if candidate > instant:
                return None
            if candidate == instant and recurrence_key(value, master.all_day,
                                                       floating_time=master.floating_time) == key:
                if master.all_day:
                    actual = datetime.combine(value.date(), time.min, self.event_zone)
                elif master.floating_time:
                    from .timeutil import local_zone
                    actual = value.replace(tzinfo=local_zone())
                else:
                    actual = value
                return self._project(actual, key)
        return None

    def _project(self, start, key):
        # The master was validated at the write boundary. A projection changes
        # only identity and range; don't sort/copy its entire recurrence again.
        item = copy(self.master)
        zone = start.tzinfo if self.master.floating_time else self.event_zone
        if self.floating:
            end = start + self.duration
        elif self.nominal_duration:
            days, seconds = self.nominal_duration
            cursor = start.astimezone(self.event_zone)
            if days:
                cursor = (cursor.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=self.event_zone)
                cursor = normalize_recurrence_time(cursor)
            end = add_elapsed(cursor, timedelta(seconds=seconds)).astimezone(start.tzinfo)
        else:
            end = add_elapsed(start, self.duration)
        duration_spec = self.master.duration_spec
        if self.cut and key not in self.added_keys:
            local = start if self.master.all_day else start.astimezone(zone)
            if local.year < 9999 or local.month < 12:
                boundary = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1,
                                    tzinfo=zone)
                if not self.floating:
                    boundary = normalize_recurrence_time(boundary)
                if end.timestamp() > boundary.timestamp():
                    end = boundary
                    duration_spec = ''
        fields = {'id': occurrence_id(self.master.id, key), 'start': start, 'end': end,
                  'series_id': self.master.id, 'recurrence_id': key, 'recurrence_rule': '',
                  'recurrence_dates': (), 'excluded_dates': (), 'recurrence_overrides': {}, 'raw_ics': '',
                  'range_policy': '', 'duration_spec': duration_spec}
        for name, value in fields.items():
            object.__setattr__(item, name, value)
        return item

    def finite_rule_occurrences(self, maximum):
        """Unroll a short bounded rule eagerly; never traverse an infinite one."""
        if self.rule is None:
            return ()
        if self.rule._count is None and self.rule._until is None:
            return None
        if isinstance(self.seek, (_CalendarSeek, _HourlySeek)) and self.seek.prefix is not None and not self.seek.prefix[-1]:
            return ()
        result = []
        for value in self._regular(None):
            key = recurrence_key(value, self.master.all_day, floating_time=self.master.floating_time)
            if key in self.excluded or key in self.override_keys:
                continue
            actual = (datetime.combine(value.date(), time.min, self.event_zone) if self.master.all_day else
                      value.replace(tzinfo=self.event_zone) if self.master.floating_time else value)
            result.append(self._project(actual, key))
            if len(result) > maximum:
                return None
        return tuple(result)

    def finite_occurrences(self, maximum):
        regular = self.finite_rule_occurrences(maximum)
        if regular is None:
            return None
        result = {item.id: item for item in (*self.explicit_occurrences, *regular)}
        return tuple(sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))) if len(result) <= maximum else None

    def query(self, start, end, *, limit=10000, truncate=False):
        if not self.master.recurring:
            return [self.master] if overlaps(self.master, start, end) else []
        if self.master.all_day or self.master.floating_time:
            # Civil-date/floating semantics are indexed independently by Store.
            explicit = [item for item in self.explicit_occurrences if overlaps(item, start, end)]
        else:
            first = bisect_left(self._explicit_ends, start.timestamp())
            last = bisect_left(self._explicit_starts, end.timestamp())
            explicit = [item for item in self.explicit_occurrences[first:last] if overlaps(item, start, end)]
        regular = self.query_rule(start, end, limit=limit, truncate=truncate)
        result = {item.id: item for item in (*explicit, *regular)}
        if not truncate and len(result) > limit:
            raise RecurrenceRangeError(f'This range exceeds {limit:,} occurrences. Choose a smaller date range.')
        values = sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))
        return values[:limit] if truncate else values
