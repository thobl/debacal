"""One standard recurrence model for editor controls and sequence authoring."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from calendar import monthrange
from math import gcd

from dateutil.rrule import rrulestr

from .recurrence import RecurrenceError


REGULAR_KINDS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
MAX_REPEAT_COUNT = 100000
_DAY_NAMES = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError('The recurrence needs a timezone-aware start.')
    return value


def _parts(kind, interval=1, weekdays=()):
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
    return result


def count_to_until(start: datetime, kind: str, interval: int, count: int, weekdays=()) -> date:
    """Inclusive last date of a COUNT rule, including DTSTART when it matches.

    Dateutil's calendar arithmetic deliberately skips nonexistent month days:
    a monthly event on the 31st does not turn into a February event.
    """
    _aware(start)
    if type(count) is not int or not 1 <= count <= MAX_REPEAT_COUNT:
        raise RecurrenceError(f'Choose between 1 and {MAX_REPEAT_COUNT:,} occurrences.')
    try:
        rule = _parts(kind, interval, weekdays)
        if str(kind).upper() == 'DAILY':
            return start.date() + timedelta(days=interval * (count - 1))
        if str(kind).upper() == 'WEEKLY' and (not weekdays or set(weekdays) == {start.weekday()}):
            return start.date() + timedelta(days=7 * interval * (count - 1))
        values = rrulestr(rule + f';COUNT={count}', dtstart=start)
        last = None
        generated = 0
        for generated, last in enumerate(values, 1):
            pass
        if last is None or generated < count:
            raise RecurrenceError('No recurrence fits within the supported calendar years.')
        return last.date()
    except (OverflowError, ValueError, TypeError) as exc:
        if isinstance(exc, RecurrenceError):
            raise
        raise RecurrenceError('The recurrence extends beyond the supported calendar years.') from exc


def until_to_count(start: datetime, kind: str, interval: int, until: date, weekdays=(),
                   *, limit: int = MAX_REPEAT_COUNT) -> int:
    """Count rule occurrences through an inclusive calendar date, with a cap."""
    _aware(start)
    if isinstance(until, datetime) or not isinstance(until, date):
        raise RecurrenceError('Choose a valid final recurrence date.')
    if until < start.date():
        raise RecurrenceError('The final recurrence date must not precede the start.')
    if type(limit) is not int or not 1 <= limit <= MAX_REPEAT_COUNT:
        raise RecurrenceError('Invalid recurrence count limit.')
    count = 0
    try:
        rule = _parts(kind, interval, weekdays)
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

    def rule(self, start: datetime, *, all_day=False) -> str:
        _aware(start)
        result = _parts(self.kind, self.interval, self.weekdays)
        if self.ending == 'never' or (self.count is None and self.until is None):
            return result
        if self.ending == 'until':
            if self.until is None or self.until < start.date():
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
            allowed = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'WKST'}
            if kind == 'WEEKLY':
                allowed.add('BYDAY')
            if kind not in REGULAR_KINDS or parts.keys() - allowed or parts.get('WKST', 'MO') != 'MO':
                return None
            days = tuple(_DAY_NAMES.index(day) for day in parts.get('BYDAY', '').split(',') if day)
            interval = int(parts.get('INTERVAL', 1))
            _parts(kind, interval, days)
            if days and start.weekday() not in days:
                # DTSTART is still a set member in the calendar model. A rule
                # beginning on a different weekday has an extra prototype and
                # cannot truthfully use controls whose count includes it.
                return None
            if 'COUNT' in parts and 'UNTIL' in parts:
                return None
            if 'COUNT' in parts:
                count = int(parts['COUNT'])
                until = count_to_until(start, kind, interval, count, days)
                return cls(kind, interval, count, until, days, 'count')
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
                count = until_to_count(start, kind, interval, until, days)
                last = count_to_until(start, kind, interval, count, days)
                return cls(kind, interval, count, last, days, 'until')
            return cls(kind, interval, None, None, days, 'never')
        except (ValueError, OverflowError, TypeError):
            return None


def retain_relative_intent(expression, start, regular):
    """Do not classify a clipped month program from a short point sample.

    Two dates can happen to look like a 28-day rule while expressing +1m.
    Regular controls may extend a program only when their repeated unit is
    equivalent, including every month in its calendar cycle.
    """
    if regular is None:
        return None
    from .patterns import relative_sequence_steps
    steps = relative_sequence_steps(expression)
    if steps is None:
        return regular
    normalized = tuple((amount * 7, 'd') if unit == 'w' else (amount, unit)
                       for amount, unit in steps)
    if len(set(normalized)) != 1:
        return None
    amount, unit = normalized[0]
    if unit == 'd':
        period = regular.interval * (7 if regular.kind == 'WEEKLY' else 1)
        return regular if regular.kind in ('DAILY', 'WEEKLY') and period == amount else None
    if unit == 'm':
        months = {(start.month - 1 + index * amount) % 12 + 1 for index in range(12 // gcd(amount, 12))}
        if any(start.day > monthrange(2001, month)[1] for month in months):
            return None
        period = regular.interval * (12 if regular.kind == 'YEARLY' else 1)
        return regular if regular.kind in ('MONTHLY', 'YEARLY') and period == amount else None
    if unit == 'y':
        if (start.month, start.day) == (2, 29):
            return None
        if regular.kind == 'MONTHLY' and regular.interval % 12:
            return None
        period = regular.interval // 12 if regular.kind == 'MONTHLY' and regular.interval % 12 == 0 else regular.interval
        return regular if regular.kind in ('MONTHLY', 'YEARLY') and period == amount else None
    return None  # Legacy clock-offset programs remain in Custom.
