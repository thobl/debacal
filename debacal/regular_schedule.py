"""One standard recurrence model for editor controls and sequence authoring."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from calendar import monthrange

from dateutil.rrule import rrulestr

from .recurrence import RecurrenceError


REGULAR_KINDS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
MAX_REPEAT_COUNT = 100000
_DAY_NAMES = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')


def shifted_reference_start(start, kind, *, overflow='', range_span=1, preferred_day=None):
    """An initial fitted start can precede the independent reference date."""
    if overflow != 'shift' or kind not in ('MONTHLY', 'YEARLY') or range_span == 1:
        return None
    latest = monthrange(start.year, start.month)[1] - range_span + 1
    if latest < 1:
        return None
    candidate = start.replace(day=min(preferred_day or start.day, latest))
    return candidate if candidate.timestamp() < start.timestamp() else None


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError('The recurrence needs a timezone-aware start.')
    return value


def _parts(kind, interval=1, weekdays=(), *, clamp=False, start=None,
           preferred_day=None, selectors=(), week_start='MO', overflow='', range_span=1):
    kind = str(kind).upper()
    if kind not in REGULAR_KINDS:
        raise RecurrenceError('Choose a daily, weekly, monthly or yearly recurrence.')
    if type(interval) is not int or not 1 <= interval <= 999:
        raise RecurrenceError('The recurrence interval must be between 1 and 999.')
    days = tuple(sorted(set(weekdays)))
    if any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise RecurrenceError('Choose weekdays from Monday through Sunday.')
    result = f'FREQ={kind};INTERVAL={interval}'
    if kind == 'WEEKLY' and days:
        result += ';BYDAY=' + ','.join(_DAY_NAMES[day] for day in days)
    week_start = str(week_start).upper()
    if week_start not in _DAY_NAMES or (kind != 'WEEKLY' and week_start != 'MO'):
        raise RecurrenceError('Choose a valid first weekday for a weekly recurrence.')
    if kind == 'WEEKLY' and week_start != 'MO':
        result += ';WKST=' + week_start
    if overflow:
        if overflow not in ('skip', 'shift', 'cut') or kind not in ('MONTHLY', 'YEARLY') or selectors:
            raise RecurrenceError('Choose Skip, Shift or Cut for monthly/yearly calendar-day ranges.')
        if type(range_span) is not int or not 1 <= range_span <= 31:
            raise RecurrenceError('A calendar range must occupy between 1 and 31 days.')
        _aware(start)
        day = preferred_day or start.day
        if overflow == 'shift' and range_span > 28:
            from .range_policies import shift_range_fits
            if not shift_range_fits(start, kind, interval, span=range_span):
                raise RecurrenceError('This whole range cannot fit in every selected month. Choose Skip or Cut.')
        if day + range_span - 1 > 31:
            # An intentionally cross-month template keeps its full duration.
            range_span = 1
        if overflow == 'skip' and range_span > 1:
            result += f';BYMONTHDAY={day},{day + range_span - 1};BYSETPOS=-2'
        elif overflow == 'shift' and range_span > 1:
            result += f';BYMONTHDAY={day},-{range_span};BYSETPOS=1'
        elif overflow == 'shift':
            result += ';BYMONTHDAY=' + ','.join(str(value) for value in range(min(28, day), day + 1))
            result += ';BYSETPOS=-1'
        elif preferred_day is not None:
            result += f';BYMONTHDAY={day}'
        if kind == 'YEARLY' and (overflow != 'skip' or range_span > 1 or preferred_day is not None):
            result += f';BYMONTH={start.month}'
        return result
    if selectors:
        from .patterns import PatternError, _weekday_selectors
        try:
            names = _weekday_selectors(selectors, {'MONTHLY': 'm', 'YEARLY': 'y'}.get(kind))
        except PatternError as error:
            raise RecurrenceError(str(error)) from error
        if clamp or preferred_day is not None:
            raise RecurrenceError('Choose weekday selection or a calendar day, not both.')
        result += ';BYDAY=' + ','.join(names)
        if kind == 'YEARLY':
            _aware(start)
            result += ';BYMONTH=' + str(start.month)
    if preferred_day is not None and (kind not in ('MONTHLY', 'YEARLY')
                                      or type(preferred_day) is not int or not 1 <= preferred_day <= 31):
        raise RecurrenceError('Choose a monthly/yearly day from 1 through 31.')
    if clamp:
        if kind not in ('MONTHLY', 'YEARLY'):
            raise RecurrenceError('Clamping is available only for monthly and yearly recurrences.')
        _aware(start)
        day = preferred_day or start.day
        result += ';BYMONTHDAY=' + ','.join(str(value) for value in range(min(28, day), day + 1))
        result += ';BYSETPOS=-1'
        if kind == 'YEARLY':
            result += ';BYMONTH=' + str(start.month)
    elif preferred_day is not None:
        result += ';BYMONTHDAY=' + str(preferred_day)
        if kind == 'YEARLY':
            _aware(start)
            result += ';BYMONTH=' + str(start.month)
    return result


def count_to_until(start: datetime, kind: str, interval: int, count: int, weekdays=(), *, clamp=False,
                   preferred_day=None, selectors=(), week_start='MO', overflow='', range_span=1) -> date:
    """Inclusive last date of a COUNT rule, including DTSTART when it matches.

    Dateutil's calendar arithmetic deliberately skips nonexistent month days:
    a monthly event on the 31st does not turn into a February event.
    """
    _aware(start)
    if type(count) is not int or not 1 <= count <= MAX_REPEAT_COUNT:
        raise RecurrenceError(f'Choose between 1 and {MAX_REPEAT_COUNT:,} occurrences.')
    try:
        rule = _parts(kind, interval, weekdays, clamp=clamp, start=start,
                      preferred_day=preferred_day, selectors=selectors, week_start=week_start,
                      overflow=overflow, range_span=range_span)
        prelude = shifted_reference_start(start, kind, overflow=overflow, range_span=range_span, preferred_day=preferred_day)
        if prelude is not None and count == 1:
            return prelude.date()
        if str(kind).upper() == 'DAILY':
            return start.date() + timedelta(days=interval * (count - 1))
        if str(kind).upper() == 'WEEKLY' and (not weekdays or set(weekdays) == {start.weekday()}):
            return start.date() + timedelta(days=7 * interval * (count - 1))
        native_count = count - int(prelude is not None)
        values = rrulestr(rule + f';COUNT={native_count}', dtstart=start)
        last = None
        generated = 0
        for generated, last in enumerate(values, 1):
            pass
        if last is None or generated < native_count:
            raise RecurrenceError('No recurrence fits within the supported calendar years.')
        return last.date()
    except (OverflowError, ValueError, TypeError) as exc:
        if isinstance(exc, RecurrenceError):
            raise
        raise RecurrenceError('The recurrence extends beyond the supported calendar years.') from exc


def until_to_count(start: datetime, kind: str, interval: int, until: date, weekdays=(),
                   *, limit: int = MAX_REPEAT_COUNT, clamp=False,
                   preferred_day=None, selectors=(), week_start='MO', overflow='', range_span=1) -> int:
    """Count rule occurrences through an inclusive calendar date, with a cap."""
    _aware(start)
    if isinstance(until, datetime) or not isinstance(until, date):
        raise RecurrenceError('Choose a valid final recurrence date.')
    if until < start.date():
        fitted = shifted_reference_start(start, kind, overflow=overflow, range_span=range_span, preferred_day=preferred_day)
        if fitted is None or until < fitted.date():
            raise RecurrenceError('The final recurrence date must not precede the start.')
    if type(limit) is not int or not 1 <= limit <= MAX_REPEAT_COUNT:
        raise RecurrenceError('Invalid recurrence count limit.')
    count = 0
    try:
        rule = _parts(kind, interval, weekdays, clamp=clamp, start=start,
                      preferred_day=preferred_day, selectors=selectors, week_start=week_start,
                      overflow=overflow, range_span=range_span)
        prelude = shifted_reference_start(start, kind, overflow=overflow, range_span=range_span, preferred_day=preferred_day)
        if prelude is not None and prelude.date() <= until:
            count = 1
        if str(kind).upper() == 'DAILY':
            count = (until - start.date()).days // interval + 1
        elif str(kind).upper() == 'WEEKLY' and (not weekdays or set(weekdays) == {start.weekday()}):
            count = (until - start.date()).days // (7 * interval) + 1
        else:
            for value in rrulestr(rule, dtstart=start):
                if value.date() > until:
                    break
                count += 1
                if count > limit:
                    raise RecurrenceError(f'This schedule exceeds {limit:,} occurrences.')
        if count > limit:
            raise RecurrenceError(f'This schedule exceeds {limit:,} occurrences.')
    except (OverflowError, ValueError, TypeError) as exc:
        if isinstance(exc, RecurrenceError):
            raise
        raise RecurrenceError('The recurrence extends beyond the supported calendar years.') from exc
    if count == 0:
        raise RecurrenceError('No recurrence occurs on or before the chosen final date.')
    return count


@dataclass(frozen=True, slots=True)
class RegularSchedule:
    kind: str = 'WEEKLY'
    interval: int = 1
    count: int | None = 6
    until: date | None = None
    weekdays: tuple[int, ...] = ()
    ending: str = 'count'
    clamp: bool = False
    preferred_day: int | None = None
    selectors: tuple[str, ...] = ()
    week_start: str = 'MO'
    overflow: str = ''
    range_span: int = 1

    def rule(self, start: datetime, *, all_day=False) -> str:
        _aware(start)
        result = _parts(self.kind, self.interval, self.weekdays, clamp=self.clamp, start=start,
                        preferred_day=self.preferred_day, selectors=self.selectors, week_start=self.week_start,
                        overflow=self.overflow, range_span=self.range_span)
        if self.ending == 'never' or (self.count is None and self.until is None):
            return result
        if self.ending == 'until':
            fitted = shifted_reference_start(start, self.kind, overflow=self.overflow,
                                             range_span=self.range_span, preferred_day=self.preferred_day)
            if self.until is None or self.until < (fitted or start).date():
                raise RecurrenceError('The final recurrence date must not precede the start.')
            if all_day:
                return result + ';UNTIL=' + self.until.strftime('%Y%m%d')
            boundary = datetime.combine(self.until, time.max, start.tzinfo).astimezone(timezone.utc)
            return result + ';UNTIL=' + boundary.strftime('%Y%m%dT%H%M%SZ')
        if self.ending != 'count':
            raise RecurrenceError('Choose a count, final date or unbounded recurrence.')
        if type(self.count) is not int or not 1 <= self.count <= MAX_REPEAT_COUNT:
            raise RecurrenceError(f'Choose between 1 and {MAX_REPEAT_COUNT:,} occurrences.')
        return result + f';COUNT={self.count}'

    @classmethod
    def from_rule(cls, rule: str, start: datetime, *, all_day=False):
        """Return regular controls only when they can retain all rule clauses."""
        try:
            parts = dict(part.strip().upper().split('=', 1) for part in rule.removeprefix('RRULE:').split(';'))
            kind = parts.get('FREQ')
            if (kind in ('MONTHLY', 'YEARLY') and parts.get('BYSETPOS') == '-1'
                    and 'BYMONTHDAY' not in parts and 'BYDAY' not in parts and start.day <= 28):
                # A canonicalized one-candidate position retains the implicit
                # DTSTART day. On days <=28 this is the same clamped rule.
                parts['BYMONTHDAY'] = str(start.day)
                if kind == 'YEARLY':
                    parts.setdefault('BYMONTH', str(start.month))
            allowed = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'WKST'}
            preferred_day, selectors = None, ()
            overflow, range_span = '', 1
            week_start = parts.get('WKST', 'MO')
            if kind == 'WEEKLY':
                allowed.add('BYDAY')
            elif kind in ('MONTHLY', 'YEARLY') and 'BYDAY' in parts:
                from .patterns import _weekday_selectors
                selectors = _weekday_selectors(parts['BYDAY'].split(','), 'm' if kind == 'MONTHLY' else 'y')
                allowed.add('BYDAY')
                if kind == 'YEARLY':
                    if parts.get('BYMONTH') != str(start.month):
                        return None
                    allowed.add('BYMONTH')
            clamp = False
            if kind in ('MONTHLY', 'YEARLY') and 'BYMONTHDAY' in parts:
                raw_days = parts['BYMONTHDAY'].split(',')
                if not all(value.lstrip('-').isdigit() and 1 <= abs(int(value)) <= 31 for value in raw_days):
                    return None
                candidate_days = tuple(sorted(set(int(value) for value in raw_days)))
                positive = [value for value in candidate_days if value > 0]
                negative = [value for value in candidate_days if value < 0]
                if parts.get('BYSETPOS') == '-2' and len(positive) == 2 and not negative:
                    preferred_day = min(positive)
                    range_span = max(positive) - preferred_day + 1
                    overflow = 'skip'
                    allowed.add('BYSETPOS')
                elif parts.get('BYSETPOS') == '1' and len(positive) == len(negative) == 1:
                    preferred_day, range_span = positive[0], -negative[0]
                    overflow = 'shift'
                    allowed.add('BYSETPOS')
                else:
                    if negative:
                        return None
                    preferred_day = max(candidate_days)
                if selectors:
                    return None
                if overflow:
                    pass
                elif parts.get('BYSETPOS') == '-1':
                    if candidate_days != tuple(range(min(28, preferred_day), preferred_day + 1)):
                        return None
                    clamp = True
                elif len(candidate_days) != 1:
                    return None
                allowed.add('BYMONTHDAY')
                if preferred_day == start.day:
                    preferred_day = None
            if clamp:
                expected = dict(part.split('=', 1) for part in _parts(kind, clamp=True, start=start,
                                                                     preferred_day=preferred_day).split(';'))
                clamp_keys = {'BYMONTHDAY', 'BYSETPOS'} | ({'BYMONTH'} if kind == 'YEARLY' else set())
                if any(parts.get(key) != expected[key] for key in clamp_keys):
                    return None
                allowed |= clamp_keys
            elif kind == 'YEARLY' and 'BYMONTHDAY' in parts:
                if parts.get('BYMONTH', str(start.month)) != str(start.month):
                    return None
                allowed.add('BYMONTH')
            if kind not in REGULAR_KINDS or parts.keys() - allowed:
                return None
            days = tuple(sorted({_DAY_NAMES.index(day) for day in parts.get('BYDAY', '').split(',') if day})) if kind == 'WEEKLY' else ()
            interval = int(parts.get('INTERVAL', 1))
            rule_options = dict(clamp=clamp, preferred_day=preferred_day, selectors=selectors, week_start=week_start,
                                overflow=overflow, range_span=range_span)
            base_rule = _parts(kind, interval, days, start=start, **rule_options)
            if (days or selectors or preferred_day is not None) and rrulestr(base_rule, dtstart=start).after(start, inc=True) != start:
                # DTSTART is still a set member in the calendar model. A rule
                # beginning on a different weekday has an extra prototype and
                # cannot truthfully use controls whose count includes it.
                return None
            if 'COUNT' in parts and 'UNTIL' in parts:
                return None
            if 'COUNT' in parts:
                count = int(parts['COUNT'])
                until = count_to_until(start, kind, interval, count, days, **rule_options)
                return cls(kind, interval, count, until, days, 'count', clamp, preferred_day, selectors, week_start, overflow, range_span)
            if 'UNTIL' in parts:
                value = parts['UNTIL']
                if len(value) == 8:
                    until = datetime.strptime(value, '%Y%m%d').date()
                else:
                    stamp = datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S')
                    stamp = stamp.replace(tzinfo=timezone.utc if value.endswith('Z') else start.tzinfo)
                    local = stamp.astimezone(start.tzinfo)
                    until = local.date()
                    # An exact cutoff before this date's recurrence clock
                    # excludes the entire candidate. Rounding it to a date
                    # first would add a phantom occurrence after a split.
                    candidate = datetime.combine(until, start.timetz()).replace(microsecond=0)
                    if candidate.timestamp() > stamp.timestamp():
                        until -= timedelta(days=1)
                count = until_to_count(start, kind, interval, until, days, **rule_options)
                last = count_to_until(start, kind, interval, count, days, **rule_options)
                return cls(kind, interval, count, last, days, 'until', clamp, preferred_day, selectors, week_start, overflow, range_span)
            return cls(kind, interval, None, None, days, 'never', clamp, preferred_day, selectors, week_start, overflow, range_span)
        except (ValueError, OverflowError, TypeError):
            return None


def retain_relative_intent(expression, start, regular):
    """Retain calendar instruction intent when choosing regular controls.

    Two dates can happen to look like a day rule while expressing +1m.
    Regular controls may extend a program only when their repeated unit is
    equivalent, including every month in its calendar cycle.
    """
    if regular is None:
        return None
    from .patterns import relative_sequence_steps
    steps = relative_sequence_steps(expression)
    if steps is None:
        return regular
    # Initial zero offsets explicitly include the reference; they add no
    # repeated interval. Interior zero offsets can reset a clamp preference.
    while steps and steps[0][0] == 0:
        steps = steps[1:]
    if not steps or any(amount == 0 for amount, _unit, _clamp in steps):
        return None
    normalized = tuple((amount * 7, 'd', clamp) if unit == 'w' else (amount, unit, clamp)
                       for amount, unit, clamp in steps)
    if len(set(normalized)) != 1:
        return None
    amount, unit, clamp = normalized[0]
    if clamp != regular.clamp:
        return None
    if unit == 'd':
        period = regular.interval * (7 if regular.kind == 'WEEKLY' else 1)
        return regular if regular.kind in ('DAILY', 'WEEKLY') and period == amount else None
    if unit == 'm':
        period = regular.interval * (12 if regular.kind == 'YEARLY' else 1)
        return regular if regular.kind in ('MONTHLY', 'YEARLY') and period == amount else None
    if unit == 'y':
        if regular.kind == 'MONTHLY' and regular.interval % 12:
            return None
        period = regular.interval // 12 if regular.kind == 'MONTHLY' and regular.interval % 12 == 0 else regular.interval
        return regular if regular.kind in ('MONTHLY', 'YEARLY') and period == amount else None
    return None
