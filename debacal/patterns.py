"""Explicit occurrence programs relative to a reference date and time.

The reference supplies the initial cursor and default clock, but contributes no
occurrence by itself. ``+0d`` emits it. Every finite instruction emits its new
cursor, including another occurrence at an already emitted instant. A final ``every(...)`` includes its
cursor and follows standard calendar recurrence semantics. Month/year offsets
skip invalid dates; the explicit ``!`` suffix clamps them while retaining their
preferred day across consecutive month/year steps.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import gcd
import re
from typing import Iterable


DEFAULT_LIMIT = 500
MAX_SEQUENCE_EVENTS = 10000
MAX_PATTERN_LENGTH = 8192
MAX_NESTING = 8
_INTEGER = re.compile(r'[0-9]+')
_CLOCK = re.compile(r'([0-9]{1,2}):([0-9]{2})')
_TAIL = re.compile(r'(?<![^\s,])every\(\s*([0-9]+)\s*([dwmy])(!?)\s*'
                   r'((?:,\s*[^(),]+)*)\)\s*$', re.I)
_FREQUENCIES = {'d': 'DAILY', 'w': 'WEEKLY', 'm': 'MONTHLY', 'y': 'YEARLY'}
_WEEKDAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')


class PatternError(ValueError):
    """Invalid syntax, an impossible local time, or an unrepresentable schedule."""


@dataclass(frozen=True)
class SequenceTail:
    prefix: str
    interval: int
    unit: str
    count: int | None = None
    until: date | None = None
    clamp: bool = False
    weekdays: tuple[str, ...] = ()
    preferred_day: int | None = None
    week_start: str = 'MO'
    overflow: str = ''
    range_span: int = 1

    @property
    def frequency(self):
        return _FREQUENCIES[self.unit]

    @property
    def unbounded(self):
        return self.count is None and self.until is None


@dataclass(frozen=True)
class CompiledSequence:
    """A finite prefix followed by an interoperable calendar recurrence."""
    prefix_starts: tuple[datetime, ...]
    tail_start: datetime
    recurrence_rule: str
    recurrence_dates: tuple[str, ...]
    excluded_dates: tuple[str, ...]
    unbounded: bool = False
    duplicate_starts: tuple[datetime, ...] = ()
    range_policy: str = ''
    tail_prelude: tuple[datetime, ...] = ()

    @property
    def fields(self):
        if self.duplicate_starts:
            raise PatternError('Coincident occurrences need an event reference. Use the event sequence compiler to retain their separate identities.')
        fields = dict(recurrence_rule=self.recurrence_rule, recurrence_dates=self.recurrence_dates,
                      excluded_dates=self.excluded_dates)
        if self.range_policy:
            fields['range_policy'] = self.range_policy
        return fields


def _weekday_selectors(values, unit):
    """Normalize calendar weekdays without accepting ambiguous ordinal weeks."""
    result = []
    for value in values:
        if type(value) is int and 0 <= value <= 6:
            value = _WEEKDAYS[value]
        value = str(value).upper()
        match = re.fullmatch(r'([+-]?[1-5])?(MO|TU|WE|TH|FR|SA|SU)', value)
        if match is None or (match[1] is not None and unit not in ('m', 'y')):
            raise PatternError('Use weekday names MO, TU, WE, TH, FR, SA, SU; '
                               'monthly/yearly selectors may use an ordinal such as -1FR.')
        ordinal = str(int(match[1])) if match[1] is not None else ''
        result.append(ordinal + match[2])
    if result and unit not in ('w', 'm', 'y'):
        raise PatternError('Weekday selection is available for weeks, months and years.')
    return tuple(sorted(set(result), key=lambda value: (_WEEKDAYS.index(value[-2:]),
                                                        int(value[:-2] or '0'))))


def sequence_tail(expression: str) -> SequenceTail | None:
    """Recognize terminal every(Nunit[, on=MO+WE][, count=N|until=D.M.YYYY]).

    Invalid month days and leap dates are skipped unless the unit has an
    explicit clamp suffix, as in every(1m!, count=12).
    """
    if not isinstance(expression, str):
        raise PatternError('The sequence must be text.')
    if len(expression) > MAX_PATTERN_LENGTH:
        raise PatternError(f'A sequence may contain at most {MAX_PATTERN_LENGTH} characters.')
    if 'every' not in expression.lower():
        return None
    match = _TAIL.search(expression)
    if match is None or 'every' in expression[:match.start()].lower():
        raise PatternError('Use every(1d), every(2w), every(1m) or every(1y) as the final instruction; '
                           'optionally add count=N or until=D.M.YYYY inside its parentheses.')
    if len(match[1]) > 3 or not 1 <= int(match[1]) <= 999:
        raise PatternError('The repeat interval must be between 1 and 999.')
    prefix = expression[:match.start()].rstrip()
    if prefix.endswith(','):
        prefix = prefix[:-1].rstrip()
    count = until = preferred_day = None
    weekdays, week_start = (), 'MO'
    if match[3] and match[2].lower() not in ('m', 'y'):
        raise PatternError('Clamping is available only for month and year intervals.')
    options = {}
    for clause in match[4].split(',')[1:]:
        name, separator, value = clause.strip().partition('=')
        name, value = name.strip().lower(), value.strip()
        if not separator or name not in ('on', 'day', 'week_start', 'count', 'until', 'overflow', 'span') or name in options:
            raise PatternError('Use each recurrence option at most once: on, day, week_start, count, until, overflow, span.')
        options[name] = value
    if 'count' in options and 'until' in options:
        raise PatternError('Choose either a repeat count or a final date, not both.')
    unit = match[2].lower()
    if 'count' in options:
        if not re.fullmatch(r'[0-9]{1,6}', options['count']) or not 1 <= int(options['count']) <= 100000:
            raise PatternError('The repeat count must be between 1 and 100,000, including the tail start.')
        count = int(options['count'])
    if 'until' in options:
        bound = re.fullmatch(r'([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})', options['until'])
        try:
            if bound is None:
                raise ValueError
            until = date(int(bound[3]), int(bound[2]), int(bound[1]))
        except ValueError as exc:
            raise PatternError('Use a complete final date, for example until=13.11.2026.') from exc
    if 'on' in options:
        weekdays = _weekday_selectors([value.strip() for value in options['on'].split('+')], unit)
    if 'day' in options:
        if unit not in ('m', 'y') or not re.fullmatch(r'[0-9]{1,2}', options['day']) or not 1 <= int(options['day']) <= 31:
            raise PatternError('Use day=1 through day=31 for monthly or yearly repeats.')
        if weekdays:
            raise PatternError('Choose weekday selection or a calendar day, not both.')
        preferred_day = int(options['day'])
    if weekdays and match[3]:
        raise PatternError('Weekday selection cannot be combined with calendar-day clamping.')
    if 'week_start' in options:
        week_start = options['week_start'].upper()
        if unit != 'w' or week_start not in _WEEKDAYS:
            raise PatternError('Use week_start=MO through week_start=SU for weekly repeats.')
    overflow, span = options.get('overflow', '').lower(), options.get('span', '1')
    if overflow and (overflow not in ('skip', 'shift', 'cut') or unit not in ('m', 'y') or weekdays):
        raise PatternError('Use overflow=skip, shift or cut for monthly/yearly calendar-day ranges.')
    if not re.fullmatch(r'[0-9]{1,2}', span) or not 1 <= int(span) <= 31 or ('span' in options and not overflow):
        raise PatternError('Use span=1 through span=31 with an overflow policy.')
    return SequenceTail(prefix, int(match[1]), unit, count, until, bool(match[3]),
                        weekdays, preferred_day, week_start, overflow, int(span))


def expression_for_regular(kind, interval=1, *, count=None, until=None, clamp=False,
                           weekdays=(), preferred_day=None, week_start='MO', overflow='', range_span=1):
    """One spelling for a regular recurrence and its optional inclusive bound."""
    unit = next((unit for unit, frequency in _FREQUENCIES.items() if frequency == str(kind).upper()), None)
    if unit is None or type(interval) is not int or not 1 <= interval <= 999:
        raise PatternError('Choose a daily, weekly, monthly or yearly interval between 1 and 999.')
    if count is not None and until is not None:
        raise PatternError('Choose either a repeat count or a final date, not both.')
    if clamp and unit not in ('m', 'y'):
        raise PatternError('Clamping is available only for month and year intervals.')
    selectors = _weekday_selectors(weekdays, unit)
    if preferred_day is not None and (unit not in ('m', 'y') or type(preferred_day) is not int
                                      or not 1 <= preferred_day <= 31 or selectors):
        raise PatternError('Choose a monthly/yearly day from 1 through 31, without weekday selection.')
    if selectors and clamp:
        raise PatternError('Weekday selection cannot be combined with calendar-day clamping.')
    week_start = str(week_start).upper()
    if week_start not in _WEEKDAYS or (unit != 'w' and week_start != 'MO'):
        raise PatternError('Choose a valid first weekday for a weekly repeat.')
    if range_span == 1 and overflow in ('skip', 'shift'):
        clamp, overflow = overflow == 'shift', ''
    options = []
    if selectors:
        options.append('on=' + '+'.join(selectors))
    if preferred_day is not None:
        options.append(f'day={preferred_day}')
    if week_start != 'MO':
        options.append('week_start=' + week_start)
    if overflow:
        if overflow not in ('skip', 'shift', 'cut') or unit not in ('m', 'y') or selectors:
            raise PatternError('Choose Skip, Shift or Cut for a monthly/yearly calendar-day range.')
        if type(range_span) is not int or not 1 <= range_span <= 31:
            raise PatternError('The range span must be between 1 and 31 calendar days.')
        options.append('overflow=' + overflow)
        if range_span != 1:
            options.append(f'span={range_span}')
        clamp = False  # The policy replaces the older single-day ! spelling.
    unit += '!' if clamp else ''
    if count is not None:
        if type(count) is not int or not 1 <= count <= 100000:
            raise PatternError('The repeat count must be between 1 and 100,000, including the tail start.')
        options.append(f'count={count}')
    if until is not None:
        if not isinstance(until, date) or isinstance(until, datetime):
            raise PatternError('Choose a valid final recurrence date.')
        options.append(f'until={until.day}.{until.month}.{until.year:04d}')
    return f'every({interval}{unit}' + (', ' + ', '.join(options) if options else '') + ')'


def _calendar_invalid(start, months, preferred_day):
    if preferred_day <= 28:
        return False
    initial = start.year * 12 + start.month - 1
    for period in range(1, 4800 // gcd(months, 4800) + 1):
        year, month = divmod((initial + period * months) % 4800, 12)
        if preferred_day > monthrange(year + 400, month + 1)[1]:
            return True
    return False


def clamp_relevant(start, kind, interval=1, *, preferred_day=None):
    """Whether a regular interval ever encounters an invalid calendar day."""
    try:
        _aware(start)
        unit = {'MONTHLY': 'm', 'YEARLY': 'y', 'm': 'm', 'y': 'y'}.get(kind)
        if unit is None or type(interval) is not int or not 1 <= interval <= 999:
            return False
        day = start.day if preferred_day is None else preferred_day
        if type(day) is not int or not 1 <= day <= 31:
            return False
        return _calendar_invalid(start, interval * (12 if unit == 'y' else 1), day)
    except (PatternError, ValueError, OverflowError):
        return False


def with_calendar_clamp(expression, enabled):
    """Toggle m/y clamp suffixes without changing other text or formatting."""
    tail = sequence_tail(expression)
    _Parser(tail.prefix if tail else expression, MAX_SEQUENCE_EVENTS, allow_empty=tail is not None).sequence()
    protected = _TAIL.search(expression).start() if tail is not None and tail.weekdays else len(expression)
    return (re.sub(r'(?<=[0-9])([mMyY])!?',
                   lambda match: match[1] + ('!' if enabled else ''), expression[:protected])
            + expression[protected:])


def calendar_step_policy(expression):
    """One choice only when all finite calendar steps share the same policy."""
    try:
        tail = sequence_tail(expression)
        steps = _Parser(tail.prefix if tail else expression, MAX_SEQUENCE_EVENTS,
                        allow_empty=tail is not None).sequence()
        choices = {step.clamp for step in steps if isinstance(step, _Relative) and step.unit in ('m', 'y')}
        return ('shift' if True in choices else 'skip') if len(choices) == 1 else None
    except PatternError:
        return None


def calendar_clamp_options(expression, anchor):
    """Return (relevant, enabled) for the calendar-clamp editor control."""
    try:
        _aware(anchor)
        tail = sequence_tail(expression)
        steps = _Parser(tail.prefix if tail else expression, MAX_SEQUENCE_EVENTS,
                        allow_empty=tail is not None).sequence()
        cursor, preferred_day = anchor, anchor.day
        relevant, enabled = False, False
        for step in steps:
            if isinstance(step, _Relative):
                if step.unit in ('m', 'y') and step.amount:
                    months = step.amount * (12 if step.unit == 'y' else 1)
                    year, month = divmod(cursor.year * 12 + cursor.month - 1 + months, 12)
                    matters = 1 <= year <= 9999 and preferred_day > monthrange(year, month + 1)[1]
                    relevant |= matters
                    enabled |= matters and step.clamp
                cursor = _relative(step, cursor, preferred_day)
                if step.unit in ('d', 'w'):
                    preferred_day = cursor.day
            else:
                cursor = _absolute(step, cursor, anchor)
                preferred_day = cursor.day
        if tail is not None and tail.unit in ('m', 'y') and not tail.weekdays:
            matters = _calendar_invalid(cursor, tail.interval * (12 if tail.unit == 'y' else 1),
                                        tail.preferred_day or (preferred_day if tail.clamp else cursor.day))
            relevant |= matters
            enabled |= matters and tail.clamp
        return bool(relevant), bool(enabled)
    except (PatternError, ValueError, OverflowError, TypeError, AttributeError):
        return False, False


@dataclass(frozen=True)
class _Relative:
    amount: int
    unit: str
    clamp: bool = False


@dataclass(frozen=True)
class _Absolute:
    day: int | None = None
    month: int | None = None
    year: int | None = None
    hour: int | None = None
    minute: int | None = None


def _check_limit(limit):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEQUENCE_EVENTS:
        raise PatternError(f'The event limit must be an integer between 1 and {MAX_SEQUENCE_EVENTS}.')


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PatternError('The reference needs a timezone-aware date and time.')


def _instant(value):
    """Exact comparison key, including microseconds near the year boundaries."""
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise PatternError('The timezone conversion extends beyond the supported years 1–9999.') from exc


def _valid_wall(value):
    try:
        return (value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None)
                == value.replace(tzinfo=None))
    except (ValueError, OverflowError) as exc:
        raise PatternError('The timezone conversion extends beyond the supported years 1–9999.') from exc


def _validate_wall(value):
    if not _valid_wall(value):
        raise PatternError(f'The local time {value:%d.%m.%Y %H:%M} does not exist because the clocks change. Choose another time.')
    return value


class _Parser:
    """Expand a bounded instruction plan before executing any date operations."""
    def __init__(self, expression, limit, *, allow_empty=False):
        if not isinstance(expression, str):
            raise PatternError('The sequence must be text.')
        if len(expression) > MAX_PATTERN_LENGTH:
            raise PatternError(f'A sequence may contain at most {MAX_PATTERN_LENGTH} characters.')
        if not expression.strip() and not allow_empty:
            raise PatternError('Use none for an empty sequence, or +0d to include the reference event.')
        self.expression = '' if expression.strip().lower() == 'none' else expression
        self.position = 0
        self.budget = limit

    def error(self, message):
        raise PatternError(f'{message} (position {self.position + 1}).')

    def spaces(self):
        start = self.position
        while self.position < len(self.expression) and self.expression[self.position].isspace():
            self.position += 1
        return self.position > start

    def bounded(self, size):
        if size > self.budget:
            raise PatternError(f'This sequence exceeds the limit of {self.budget} instructions.')

    def sequence(self, depth=0):
        if depth > MAX_NESTING:
            self.error(f'Sequences may nest at most {MAX_NESTING} levels')
        self.spaces()
        steps = []
        while self.position < len(self.expression):
            char = self.expression[self.position]
            if char == ')':
                if depth == 0:
                    self.error('Unexpected closing parenthesis')
                if not steps:
                    self.error('An empty (...) group is not allowed')
                self.position += 1
                return steps
            if char == ',':
                self.error('An instruction is missing before the comma')
            unit = self.instruction(depth)
            self.bounded(len(steps) + len(unit))
            steps.extend(unit)
            separated = self.spaces()
            if self.position == len(self.expression):
                break
            char = self.expression[self.position]
            if char == ')':
                continue
            if char == ',':
                self.position += 1
                self.spaces()
                if self.position == len(self.expression) or self.expression[self.position] in ',)':
                    self.error('An instruction is missing after the comma')
            elif not separated:
                self.error('Separate instructions with a space or comma')
        if depth:
            self.error('Missing closing parenthesis')
        return steps

    def instruction(self, depth):
        count = 1
        match = _INTEGER.match(self.expression, self.position)
        if match:
            after = match.end()
            while after < len(self.expression) and self.expression[after].isspace():
                after += 1
            if after < len(self.expression) and self.expression[after] == '*':
                digits = match[0]
                if len(digits) > 6 or int(digits) < 1:
                    self.error('A multiplier must be a positive integer within the event limit')
                count = int(digits)
                self.bounded(count)
                self.position = after + 1
                self.spaces()
        if self.position == len(self.expression):
            self.error('A multiplier needs an instruction or (...) group')
        if self.expression[self.position] == '(':
            self.position += 1
            unit = self.sequence(depth + 1)
        else:
            unit = [self.atom()]
        self.bounded(len(unit) * count)
        return unit * count

    def atom(self):
        if self.expression[self.position] == '+':
            self.position += 1
            match = _INTEGER.match(self.expression, self.position)
            if not match:
                self.error('A relative step needs a number and unit, for example +2w or +3d')
            if len(match[0]) > 9:
                self.error('The relative step exceeds the supported date range')
            amount = int(match[0])
            self.position = match.end()
            unit_start = self.position
            while self.position < len(self.expression) and self.expression[self.position].isalpha():
                self.position += 1
            unit = self.expression[unit_start:self.position].lower()
            if unit not in ('d', 'w', 'm', 'y'):
                self.error('Use an explicit d, w, m or y unit for days, weeks, months or years')
            clamp = self.position < len(self.expression) and self.expression[self.position] == '!'
            if clamp:
                self.position += 1
                if unit not in ('m', 'y'):
                    self.error('Clamping is available only for month and year steps')
            return _Relative(amount, unit, clamp)
        clock = _CLOCK.match(self.expression, self.position)
        hour = minute = None
        if clock:
            hour, minute = int(clock[1]), int(clock[2])
            if hour > 23 or minute > 59:
                self.error('Use a 24-hour time from 00:00 through 23:59')
            self.position = clock.end()
            after = self.position
            while after < len(self.expression) and self.expression[after].isspace():
                after += 1
            if after == len(self.expression) or self.expression[after] != '-':
                return _Absolute(hour=hour, minute=minute)
            self.position = after + 1
            self.spaces()
        # Partial dates end in a dot; a complete year never does.
        match = re.compile(r'([0-9]{1,2})\.').match(self.expression, self.position)
        if not match:
            self.error('Expected +Nunit, HH:MM, or a date such as 13., 3.2., or 3.2.2027')
        day = int(match[1])
        self.position = match.end()
        month = year = None
        match = re.compile(r'[0-9]{1,2}').match(self.expression, self.position)
        if match:
            month = int(match[0])
            self.position = match.end()
            if self.position < len(self.expression) and self.expression[self.position] == '.':
                self.position += 1
                match = re.compile(r'[0-9]{4}').match(self.expression, self.position)
                if match:
                    year = int(match[0])
                    self.position = match.end()
                    if self.position < len(self.expression) and self.expression[self.position] == '.':
                        self.error('A complete year must not have a trailing dot')
                elif self.position < len(self.expression) and self.expression[self.position].isdigit():
                    self.error('Use four digits for a fully specified year')
            else:
                self.error('A partial day.month date needs a final dot, for example 3.2.')
        if not 1 <= day <= 31:
            self.error('The day must be between 1 and 31')
        if month is not None:
            if not 1 <= month <= 12:
                self.error('The month must be between 1 and 12')
            try:
                date(year if year is not None else 2000, month, day)
            except ValueError:
                self.error('This calendar date does not exist')
        return _Absolute(day, month, year, hour, minute)


def relative_sequence_steps(expression):
    """Return the expanded ``(amount, unit, clamp)`` relative program, or None.

    Multipliers and groups are transparent. An empty program, absolute date or
    clock, regular every(...) tail, and invalid text are not relative programs.
    """
    try:
        if sequence_tail(expression) is not None:
            return None
        steps = _Parser(expression, MAX_SEQUENCE_EVENTS).sequence()
    except PatternError:
        return None
    if not steps or not all(isinstance(step, _Relative) for step in steps):
        return None
    return tuple((step.amount, step.unit, step.clamp) for step in steps)


def is_relative_sequence(expression):
    """Whether a valid nonempty finite program contains only relative steps."""
    return relative_sequence_steps(expression) is not None


def _relative_program_text(steps):
    """Compress known relative nodes without inferring different date units."""
    if not steps:
        return 'none'
    texts = ['+' + str(step.amount) + step.unit + ('!' if step.clamp else '') for step in steps]
    if len(set(texts)) == 1:
        return texts[0] if len(texts) == 1 else f'{len(texts)}*{texts[0]}'
    if len(texts) <= DEFAULT_LIMIT:
        forms, indexes, masks = [], {}, []
        for text in texts:
            if text not in indexes:
                indexes[text] = len(forms)
                forms.append(_ShortForm(text))
            masks.append(1 << indexes[text])
        return _simplify_masks(masks, forms, prefer_dates=False)
    # Long programs avoid the block optimizer. A primitive whole-program
    # period and adjacent runs can still retain compact instruction intent.
    borders = [0] * len(texts)
    for index in range(1, len(texts)):
        border = borders[index - 1]
        while border and texts[index] != texts[border]:
            border = borders[border - 1]
        if texts[index] == texts[border]:
            border += 1
        borders[index] = border
    period = len(texts) - borders[-1]
    if period < len(texts) and len(texts) % period == 0:
        body = _relative_program_text(steps[:period])
        return f'{len(texts) // period}*({body})'
    runs = []
    for text in texts:
        if runs and runs[-1][0] == text:
            runs[-1] = text, runs[-1][1] + 1
        else:
            runs.append((text, 1))
    return ' '.join(f'{count}*{text}' if count > 1 else text for text, count in runs)


def split_relative_sequence(expression, anchor, boundary):
    """Partition relative instruction intent at a nominal occurrence start.

    Return ``(None, original_text)`` before the first occurrence, otherwise two
    finite programs. ``+0d`` means a singleton. Non-relative programs return None;
    a relative program with an unknown boundary raises rather than guessing.
    Re-parsing proves identical relative nodes, so future reanchoring retains
    the original units/calendar behavior rather than a day-gap inference.
    A split that would lose a clamped preferred day returns None so the caller
    can retain its canonical dates instead.
    """
    if not is_relative_sequence(expression):
        return None
    _aware(anchor)
    _validate_wall(anchor)
    _aware(boundary)
    steps = _Parser(expression, MAX_SEQUENCE_EVENTS).sequence()
    values, _cursor = _execute_steps(steps, anchor)
    if len(values) != len(steps):
        return None
    target = _instant(boundary)
    index = next((index for index, value in enumerate(values) if _instant(value) == target), None)
    if index is None:
        raise PatternError('The selected occurrence does not match a relative instruction boundary.')
    if index == 0 and _instant(values[0]) == _instant(anchor):
        return None, expression
    before_steps, after_steps = steps[:index], [_Relative(0, 'd'), *steps[index + 1:]]
    before, after = _relative_program_text(before_steps), _relative_program_text(after_steps)
    for text, expected_steps, primary, expected_values in (
        (before, before_steps, anchor, values[:index]),
        (after, after_steps, values[index], values[index:]),
    ):
        actual_steps = _Parser(text, MAX_SEQUENCE_EVENTS).sequence()
        if actual_steps != expected_steps:
            raise PatternError('The split cannot preserve the original relative instructions.')
        actual, _cursor = _execute_steps(actual_steps, primary)
        if [_instant(value) for value in actual] != [_instant(value) for value in expected_values]:
            return None
    return (before if index else None), after


def _relative(step, previous, preferred_day=None):
    try:
        if step.amount == 0:
            return previous
        if step.unit in ('d', 'w'):
            value = previous + timedelta(days=step.amount * (7 if step.unit == 'w' else 1))
        else:
            preferred_day = previous.day if preferred_day is None else preferred_day
            months = step.amount * (12 if step.unit == 'y' else 1)
            index = previous.year * 12 + previous.month - 1 + months
            while True:
                year, month = divmod(index, 12)
                if not 1 <= year <= 9999:
                    raise OverflowError
                month += 1
                maximum = monthrange(year, month)[1]
                if preferred_day <= maximum or step.clamp:
                    value = previous.replace(year=year, month=month, day=min(preferred_day, maximum))
                    break
                index += months
        value = value.replace(fold=previous.fold)
        return _validate_wall(value)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, PatternError):
            raise
        raise PatternError('The relative step extends beyond the supported years 1–9999.') from exc


def _after(candidate, previous):
    # A nonexistent nominal clock earlier than the cursor is already past;
    # otherwise the next matching time falls in a gap and needs user correction.
    if not _valid_wall(candidate):
        if candidate.replace(tzinfo=None) <= previous.replace(tzinfo=None):
            return False
        _validate_wall(candidate)
    return _instant(candidate) > _instant(previous)


def _absolute(step, previous, anchor):
    clock = (time(step.hour, step.minute, fold=previous.fold if step.day is None else anchor.fold)
             if step.hour is not None else anchor.timetz().replace(tzinfo=None))

    def on(day):
        return datetime.combine(day, clock, anchor.tzinfo)

    try:
        if step.day is None:
            candidate = on(previous.date())
            if _after(candidate, previous):
                return candidate
            return _validate_wall(on(previous.date() + timedelta(days=1)))
        if step.year is not None:
            return _validate_wall(on(date(step.year, step.month, step.day)))
        if step.month is not None:
            for year in range(previous.year, 10000):
                try:
                    candidate_day = date(year, step.month, step.day)
                except ValueError:  # A valid 29 February skips non-leap years.
                    continue
                if candidate_day < previous.date():
                    continue
                candidate = on(candidate_day)
                if _after(candidate, previous):
                    return candidate
        else:
            index = previous.year * 12 + previous.month - 1
            while index < 10000 * 12:
                year, month = divmod(index, 12)
                try:
                    candidate_day = date(year, month + 1, step.day)
                except ValueError:  # Day 31 skips months that do not have it.
                    index += 1
                    continue
                if candidate_day >= previous.date():
                    candidate = on(candidate_day)
                    if _after(candidate, previous):
                        return candidate
                index += 1
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, PatternError):
            raise
        raise PatternError('The date extends beyond the supported years 1–9999.') from exc
    raise PatternError('No next matching date exists within the supported years 1–9999.')


def expand_sequence(expression: str, anchor: datetime, limit: int = DEFAULT_LIMIT) -> list[datetime]:
    """Return emitted occurrences, retaining coincident starts in instruction order.

    The instruction budget includes every expanded step, even a repeated date
    even at a repeated start. Full dates are exact; partial dates and clocks
    advance. Zero relative offsets explicitly emit the existing cursor.
    """
    _check_limit(limit)
    _aware(anchor)
    _validate_wall(anchor)
    tail = sequence_tail(expression)
    if tail is not None:
        if tail.unbounded:
            raise PatternError('An indefinite sequence has no finite list of events. Browse its occurrences in the preview.')
        compiled = compile_sequence(expression, anchor, limit=limit)
        from dateutil.rrule import rrulestr
        from .recurrence import normalize_recurrence_time
        rule = compiled.recurrence_rule
        # Timed expansion uses aware local clocks. Stored all-day rules still
        # use DATE UNTIL, but this entry point expands a timed prototype.
        result = list(compiled.prefix_starts)
        result.extend(compiled.tail_prelude)
        shared = bool(result and _instant(result[-1]) == _instant(compiled.tail_start))
        for value in rrulestr(rule, dtstart=anchor) if rule else ():
            value = normalize_recurrence_time(value)
            if _instant(value) < _instant(compiled.tail_start):
                continue
            if _instant(value).isoformat() in compiled.excluded_dates:
                continue
            stamp = _instant(value)
            if shared and stamp == _instant(compiled.tail_start):
                continue
            result.append(value)
            if len(result) > limit:
                raise PatternError(f'This sequence exceeds the limit of {limit} events.')
        return result
    steps = _Parser(expression, limit).sequence()
    return _execute_steps(steps, anchor)[0]


def _execute_state(steps, anchor):
    values = []
    previous = anchor
    preferred_day = anchor.day
    for step in steps:
        if isinstance(step, _Relative):
            previous = _relative(step, previous, preferred_day)
            if step.unit in ('d', 'w'):
                preferred_day = previous.day
        else:
            previous = _absolute(step, previous, anchor)
            preferred_day = previous.day
        values.append(previous)
    return values, previous, preferred_day


def _execute_steps(steps, anchor):
    values, previous, _preferred_day = _execute_state(steps, anchor)
    return values, previous


def duplicate_occurrences(values):
    """Occurrences beyond the first at each instant, in emission order."""
    seen, duplicates = set(), []
    for value in values:
        key = _instant(value)
        if key in seen:
            duplicates.append(value)
        seen.add(key)
    return tuple(duplicates)


def tail_duplicate_occurrences(values, start, rule, anchor):
    """Finite extra copies emitted alongside a standard terminal rule.

    Only the last prefix emission is shared with the tail's cursor. Other
    prefix emissions remain occurrences even when the rule later reaches them.
    """
    from dateutil.rrule import rrulestr
    def date_until(match):
        last = datetime.strptime(match[1], '%Y%m%d').date()
        boundary = datetime.combine(last, time.max, anchor.tzinfo).astimezone(timezone.utc)
        return 'UNTIL=' + f'{boundary.year:04d}' + boundary.strftime('%m%dT%H%M%SZ')
    rule = re.sub(r'UNTIL=([0-9]{8})(?=;|$)', date_until, rule)
    generated = rrulestr(rule, dtstart=anchor)
    seen, extras = set(), []
    cursor_key = _instant(start)
    shared = bool(values and _instant(values[-1]) == cursor_key)
    for value in values:
        stamp = _instant(value)
        in_tail = False
        if stamp >= cursor_key:
            previous = generated.before(value, inc=True)
            in_tail = previous is not None and _instant(previous) == stamp
        if stamp in seen or (in_tail and not (shared and stamp == cursor_key)):
            extras.append(value)
        seen.add(stamp)
    return tuple(extras)


def _check_tail_count_range(tail, start, preferred_day=None):
    """Prove a finite calendar count fits without expanding its occurrences."""
    if tail.count is None:
        return
    try:
        if tail.unit == 'w' and tail.weekdays:
            days = sorted(_WEEKDAYS.index(value) for value in tail.weekdays)
            origin = _WEEKDAYS.index(tail.week_start)
            offsets = sorted((day - origin) % 7 for day in days)
            offset = (start.weekday() - origin) % 7
            remaining = [day for day in offsets if day >= offset]
            index = tail.count - 1
            first_week = start.date() - timedelta(days=offset)
            if index < len(remaining):
                first_week + timedelta(days=remaining[index])
            else:
                periods, day = divmod(index - len(remaining), len(offsets))
                first_week + timedelta(days=(periods + 1) * 7 * tail.interval + offsets[day])
            return
        if tail.unit in ('d', 'w'):
            start.date() + timedelta(days=tail.interval * (tail.count - 1) * (7 if tail.unit == 'w' else 1))
            return
        if tail.weekdays:
            # At most 4,800 monthly or 400 yearly periods cover a complete
            # Gregorian cycle. Count positions symbolically, not 100k events.
            months = tail.interval * (12 if tail.unit == 'y' else 1)
            cycle = 4800 // gcd(months, 4800)
            initial = start.year * 12 + start.month - 1
            candidates = []
            for period in range(cycle):
                year, month = divmod((initial + period * months) % 4800, 12)
                for day in _selected_month_days(year + 400, month + 1, tail.weekdays):
                    candidates.append((period, day))
            initial_days = _selected_month_days(start.year, start.month, tail.weekdays)
            skipped = sum(day < start.day for day in initial_days)
            rounds, index = divmod(tail.count - 1 + skipped, len(candidates))
            final_period, _day = candidates[index]
            year, _month = divmod(initial + (rounds * cycle + final_period) * months, 12)
            if not 1 <= year <= 9999:
                raise OverflowError
            return
        # Month/day validity repeats after 400 Gregorian years. Compute the
        # final period using at most one calendar cycle, not the tail's count.
        months = tail.interval * (12 if tail.unit == 'y' else 1)
        cycle = 4800 // gcd(months, 4800)
        initial = start.year * 12 + start.month - 1
        valid = []
        for period in range(cycle):
            year, month = divmod((initial + period * months) % 4800, 12)
            required_day = (preferred_day or start.day) + (tail.range_span - 1 if tail.overflow == 'skip' else 0)
            if tail.clamp or tail.overflow in ('shift', 'cut') or required_day <= monthrange(year + 400, month + 1)[1]:
                valid.append(period)
        repetitions, remainder = divmod(tail.count - 1, len(valid))
        final_period = repetitions * cycle + valid[remainder]
        year, _month = divmod(initial + final_period * months, 12)
        if not 1 <= year <= 9999:
            raise OverflowError
    except (OverflowError, ValueError) as exc:
        raise PatternError('The repeat count extends beyond the supported years 1–9999.') from exc


def _selected_month_days(year, month, selectors):
    values = set()
    last = monthrange(year, month)[1]
    for selector in selectors:
        weekday = _WEEKDAYS.index(selector[-2:])
        matching = [day for day in range(1, last + 1) if date(year, month, day).weekday() == weekday]
        ordinal = int(selector[:-2]) if selector[:-2] else None
        if ordinal is None:
            values.update(matching)
        elif -len(matching) <= ordinal <= len(matching):
            values.add(matching[ordinal - 1] if ordinal > 0 else matching[ordinal])
    return sorted(values)


def compile_sequence(expression: str, anchor: datetime, *, all_day=False,
                     limit: int = DEFAULT_LIMIT) -> CompiledSequence:
    """Compile an occurrence program to one RRULE plus finite date exceptions.

    The reference remains DTSTART. Regular tails must share that DTSTART's
    recurrence phase; otherwise one standard rule cannot represent them and we
    refuse the conversion rather than shifting or truncating any occurrences.
    Only the bounded period preceding the tail is visited, including for large
    finite tails. COUNT counts tail occurrences including its cursor; UNTIL is
    an inclusive local calendar date and does not restrict the finite prefix.
    """
    tail = sequence_tail(expression)
    from dateutil.rrule import rrulestr
    from .recurrence import normalize_recurrence_time
    _check_limit(limit)
    _aware(anchor)
    _validate_wall(anchor)
    steps = _Parser(tail.prefix if tail else expression, limit, allow_empty=tail is not None).sequence()
    values, start, preferred_day = _execute_state(steps, anchor)
    key = (lambda value: value.date().isoformat()) if all_day else (lambda value: _instant(value).isoformat())
    if all_day and any(value.time() != time.min for value in values):
        raise PatternError('Time entries require a timed event.')
    if tail is None:
        duplicates = duplicate_occurrences(values)
        # A finite uniform program is a bounded standard rule, including its
        # skip/clamp behavior. Verify the projection before replacing dates;
        # graphically edited irregular sets still retain exact RDATEs.
        if (not anchor.microsecond and len(steps) > 1 and steps[0] == _Relative(0, 'd')
                and isinstance(steps[1], _Relative) and 1 <= steps[1].amount <= 999
                and all(step == steps[1] for step in steps[1:])):
            step = steps[1]
            regular_text = expression_for_regular(_FREQUENCIES[step.unit], step.amount,
                                                  count=len(values), clamp=step.clamp)
            regular = compile_sequence(regular_text, anchor, all_day=all_day, limit=limit)
            actual = [normalize_recurrence_time(value) for value in rrulestr(regular.recurrence_rule, dtstart=anchor)]
            if [_instant(value) for value in actual] == [_instant(value) for value in values]:
                return CompiledSequence(tuple(values), start, regular.recurrence_rule,
                                        regular.recurrence_dates, regular.excluded_dates)
        if not duplicates and values and _instant(values[0]) == _instant(anchor):
            # Exact sets entered as dates or clocks deserve the same standard
            # storage as equivalent relative programs. The detector assumes
            # its first value is DTSTART, so never apply it to an omitted
            # reference: those retain explicit dates and EXDATE(reference).
            from .recurrence import schedule_fields
            inferred = schedule_fields(values, all_day=all_day)
            if inferred['recurrence_rule']:
                actual = [normalize_recurrence_time(value)
                          for value in rrulestr(inferred['recurrence_rule'], dtstart=anchor)]
                if [_instant(value) for value in actual] == [_instant(value) for value in values]:
                    return CompiledSequence(tuple(values), start, inferred['recurrence_rule'],
                                            tuple(inferred.get('recurrence_dates', ())), ())
        desired = {key(value) for value in values}
        return CompiledSequence(tuple(values), start, '', tuple(sorted(desired - {key(anchor)})),
                                () if key(anchor) in desired else (key(anchor),), duplicate_starts=duplicates)
    if tail.preferred_day is not None:
        preferred_day = tail.preferred_day
    elif not tail.clamp:
        preferred_day = start.day
    if start.microsecond or anchor.microsecond:
        raise PatternError('A regular recurrence requires whole-second times.')
    if _instant(start) < _instant(anchor):
        raise PatternError('The regular tail must start at or after the reference.')
    if tail.until is not None and tail.until < start.date():
        from .regular_schedule import shifted_reference_start
        fitted = shifted_reference_start(start, tail.frequency, overflow=tail.overflow,
            range_span=tail.range_span, preferred_day=preferred_day)
        if fitted is None or tail.until < fitted.date():
            raise PatternError('The final recurrence date must not precede the tail start.')
    if tail.unit == 'd':
        phase = (start.date() - anchor.date()).days
    elif tail.unit == 'w':
        week_start = _WEEKDAYS.index(tail.week_start)
        first_week = anchor.date() - timedelta(days=(anchor.weekday() - week_start) % 7)
        tail_week = start.date() - timedelta(days=(start.weekday() - week_start) % 7)
        phase = (tail_week - first_week).days // 7
    elif tail.unit == 'm':
        phase = (start.year - anchor.year) * 12 + start.month - anchor.month
    else:
        phase = start.year - anchor.year
    if phase % tail.interval:
        raise PatternError('This regular tail does not align with the reference for that interval. '
                           'Choose an aligned start or a different interval.')
    parts = [f'FREQ={tail.frequency}', f'INTERVAL={tail.interval}']
    if tail.weekdays:
        parts.append('BYDAY=' + ','.join(tail.weekdays))
    elif tail.unit == 'w' and start.weekday() != anchor.weekday():
        parts.append('BYDAY=' + ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')[start.weekday()])
    if tail.unit == 'w' and tail.week_start != 'MO':
        parts.append('WKST=' + tail.week_start)
    if tail.unit in ('m', 'y') and not tail.weekdays:
        if tail.overflow:
            from .regular_schedule import _parts
            policy_parts = dict(part.split('=', 1) for part in _parts(tail.frequency, tail.interval,
                start=start, preferred_day=preferred_day, overflow=tail.overflow, range_span=tail.range_span).split(';'))
            parts.extend(name + '=' + value for name, value in policy_parts.items() if name not in ('FREQ', 'INTERVAL'))
        elif tail.clamp and preferred_day > 28:
            parts.extend(('BYMONTHDAY=' + ','.join(str(day) for day in range(28, preferred_day + 1)), 'BYSETPOS=-1'))
        elif preferred_day != anchor.day:
            parts.append('BYMONTHDAY=' + str(preferred_day))
    if tail.unit == 'y' and not tail.overflow and (tail.clamp or tail.weekdays or preferred_day != anchor.day or start.month != anchor.month):
        parts.append('BYMONTH=' + str(start.month))
    for field, actual, original in (('BYHOUR', start.hour, anchor.hour), ('BYMINUTE', start.minute, anchor.minute),
                                  ('BYSECOND', start.second, anchor.second)):
        if actual != original:
            parts.append(f'{field}={actual}')
    rule = ';'.join(parts)
    from .regular_schedule import shifted_reference_start
    prelude_start = shifted_reference_start(start, tail.frequency, overflow=tail.overflow,
        range_span=tail.range_span, preferred_day=preferred_day)
    prelude = ((prelude_start,) if prelude_start is not None and _instant(prelude_start) < _instant(anchor) else ())
    if prelude_start is not None:
        start = prelude_start
    desired = {key(value) for value in values}
    generated = set()
    prefix_count = 0
    found = False
    for index, value in enumerate(rrulestr(rule, dtstart=anchor)):
        if index >= MAX_SEQUENCE_EVENTS:
            raise PatternError('This regular tail needs more than 10,000 earlier recurrence exceptions.')
        value = normalize_recurrence_time(value)
        if _instant(value) >= _instant(start):
            found = _instant(value) == _instant(start) or bool(tail.weekdays or tail.preferred_day is not None or prelude)
            if found:
                start = value
            prefix_count = index
            break
        generated.add(key(value))
    if not found:
        raise PatternError('This regular tail cannot retain its exact time in a standard calendar rule.')
    if tail.until is not None and tail.until < (prelude[0] if prelude else start).date():
        raise PatternError('No selected weekday or calendar day occurs on or before the final date.')
    _check_tail_count_range(tail, start, preferred_day)
    if tail.count is not None:
        native_count = prefix_count + tail.count - len(prelude)
        rule = rule + f';COUNT={native_count}' if native_count else ''
    elif tail.until is not None:
        if all_day:
            rule += f';UNTIL={tail.until.year:04d}{tail.until.month:02d}{tail.until.day:02d}'
        else:
            boundary = _instant(datetime.combine(tail.until, time.max, anchor.tzinfo))
            rule += f';UNTIL={boundary.year:04d}' + boundary.strftime('%m%dT%H%M%SZ')
    # The reference is a set member only if explicitly emitted or in the tail.
    rdates = desired - generated - {key(anchor), key(start)}
    exclusions = generated - desired
    if _instant(anchor) < _instant(start) and key(anchor) not in desired:
        exclusions.add(key(anchor))
    rdates.update(key(value) for value in prelude)
    if prelude and key(anchor) not in desired:
        exclusions.add(key(anchor))
    tail_start = prelude[0] if prelude else start
    return CompiledSequence(tuple(values), tail_start, rule, tuple(sorted(rdates)), tuple(sorted(exclusions)), tail.unbounded,
                            duplicate_starts=tail_duplicate_occurrences(values, start, rule, anchor) if rule else duplicate_occurrences(values),
                            range_policy='cut' if tail.overflow == 'cut' else '', tail_prelude=prelude)


def expand_pattern(expression: str, anchor: date, limit: int = DEFAULT_LIMIT) -> list[date]:
    """Expand date-only instructions using a UTC midnight reference."""
    if not isinstance(anchor, date) or isinstance(anchor, datetime):
        raise PatternError('A calendar date is required for date-only expansion.')
    return [value.date() for value in expand_sequence(expression, datetime.combine(anchor, time.min, timezone.utc), limit)]


def _occurrence_starts(starts: Iterable[datetime]):
    values = []
    for value in starts:
        _aware(value)
        _validate_wall(value)
        values.append(value)
    if not values:
        return []
    try:
        return [value.astimezone(values[0].tzinfo) for value in values]
    except (ValueError, OverflowError) as exc:
        raise PatternError('The timezone conversion extends beyond the supported years 1–9999.') from exc


def _absolute_text(value, anchor):
    day = f'{value.day}.{value.month}.{value.year:04d}'
    if value.timetz().replace(tzinfo=None) == anchor.timetz().replace(tzinfo=None):
        return day
    if value.second or value.microsecond:
        raise PatternError('This schedule has different sub-minute clocks that HH:MM instructions cannot represent losslessly.')
    return value.strftime('%H:%M-') + day


def _serialize(starts, relative, anchor=None):
    values = _occurrence_starts(starts)
    anchor = _normalizer_anchor(values, anchor)
    values = [value.astimezone(anchor.tzinfo) for value in values]
    if not values:
        return 'none'
    atoms = []
    for previous, value in zip([anchor, *values[:-1]], values):
        if _instant(value) == _instant(previous):
            atoms.append('+0d')
            continue
        days = (value.date() - previous.date()).days
        same_clock = value.timetz().replace(tzinfo=None) == previous.timetz().replace(tzinfo=None)
        if relative and days > 0 and same_clock:
            candidate = f'+{days // 7}w' if days % 7 == 0 else f'+{days}d'
            try:
                matches = _instant(expand_sequence(candidate, previous)[-1]) == _instant(value)
            except PatternError:
                matches = False
            atoms.append(candidate if matches else _absolute_text(value, anchor))
        else:
            atoms.append(_absolute_text(value, anchor))
    compact = []
    for atom in atoms:
        if compact and compact[-1][0] == atom:
            compact[-1] = (atom, compact[-1][1] + 1)
        else:
            compact.append((atom, 1))
    expression = ' '.join(f'{count}*{atom}' if count > 1 else atom for atom, count in compact)
    reproduced = expand_sequence(expression, anchor, limit=max(DEFAULT_LIMIT, len(values)))
    if [_instant(value) for value in reproduced] != [_instant(value) for value in values]:
        raise PatternError('This schedule contains ambiguous local times that the sequence cannot represent losslessly.')
    return expression


def _normalizer_anchor(values, anchor):
    if anchor is None:
        if not values:
            raise PatternError('An empty occurrence set requires a reference date and time.')
        anchor = values[0]
    _aware(anchor)
    _validate_wall(anchor)
    return anchor


def serialize_sequence(starts: Iterable[datetime], *, anchor=None) -> str:
    """Lossless full-date instructions, with explicit reference inclusion."""
    return _serialize(starts, False, anchor)


def shift_unbounded_expression(expression: str, previous: datetime, current: datetime) -> tuple[str, str]:
    """Move a regular tail and prefix, retaining count or shifting its end date.

    The name is retained for callers of the original unbounded-only helper;
    bounded every(...) uses exactly the same movement semantics.
    """
    tail = sequence_tail(expression)
    if tail is None:
        raise PatternError('A regular every(...) tail is required.')
    old = compile_sequence(expression, previous)
    zone = previous.tzinfo
    current = current.astimezone(zone)
    delta = current.replace(tzinfo=None) - previous.replace(tzinfo=None)
    def shifted(value):
        return _validate_wall((value.replace(tzinfo=None) + delta).replace(tzinfo=zone, fold=value.fold))
    expected = tuple(shifted(value) for value in old.prefix_starts)
    cursor = shifted(old.tail_start)
    weekday_shift = (current.date() - previous.date()).days % 7
    weekdays = tuple(value[:-2] + _WEEKDAYS[(_WEEKDAYS.index(value[-2:]) + weekday_shift) % 7]
                     for value in tail.weekdays)
    week_start = (_WEEKDAYS[(_WEEKDAYS.index(tail.week_start) + weekday_shift) % 7]
                  if tail.unit == 'w' and tail.weekdays else tail.week_start)

    def tail_text(until):
        return expression_for_regular(tail.frequency, tail.interval, count=tail.count,
            until=until, clamp=tail.clamp, weekdays=weekdays,
            preferred_day=tail.preferred_day, week_start=week_start,
            overflow=tail.overflow, range_span=tail.range_span)

    def check_shifted_selectors(compiled):
        # An ordinal pattern can cross into another month: the day after the
        # last Friday is not necessarily the last Saturday. Do not silently
        # move the selected occurrence onto another date in that case.
        if tail.unit in ('m', 'y') and tail.weekdays and compiled.tail_start.date() != cursor.date():
            raise PatternError('This move cannot retain the selected weekday pattern at the chosen date.')
    until = None
    if tail.until is not None:
        try:
            boundary = datetime.combine(tail.until, previous.time().replace(tzinfo=None)) + delta
            until = boundary.date()
        except (ValueError, OverflowError) as exc:
            raise PatternError('The moved final recurrence date extends beyond the supported years 1–9999.') from exc
    if not tail.prefix.strip() or is_relative_sequence(tail.prefix) or tail.prefix.strip().lower() == 'none':
        # Relative instructions describe intent, not a fixed timedelta applied
        # to their previous materialized dates. Retain that intent when a new
        # anchor crosses a month boundary or changes invalid-date skipping.
        if until == tail.until and weekdays == tail.weekdays and week_start == tail.week_start:
            result = expression
        else:
            result = (tail.prefix + ' ' + tail_text(until)).strip()
        check_shifted_selectors(compile_sequence(result, current))
        return result, current.date().isoformat()
    try:
        candidate = compile_sequence(expression, current)
        if ({_instant(value) for value in candidate.prefix_starts} == {_instant(value) for value in expected}
                and _instant(candidate.tail_start) == _instant(cursor) and until == tail.until
                and weekdays == tail.weekdays and week_start == tail.week_start):
            return expression, current.date().isoformat()
    except PatternError:
        pass
    prefix = reconstruct_sequence(expected, anchor=current, prefer_dates=False)
    if prefix == 'none':
        prefix = ''
    if not expected or _instant(expected[-1]) != _instant(cursor):
        if _instant(cursor) == _instant(current):
            cursor_text = '+0d'
        else:
            cursor_text = _absolute_text(cursor, current)
        prefix = (prefix + ' ' + cursor_text).strip()
    result = (prefix + ' ' + tail_text(until)).strip()
    compiled = compile_sequence(result, current)
    check_shifted_selectors(compiled)
    if ({_instant(value) for value in compiled.prefix_starts} != {_instant(value) for value in expected}
            or _instant(compiled.tail_start) != _instant(cursor)):
        raise PatternError('The moved regular sequence cannot retain its exact dates.')
    return result, current.date().isoformat()


@dataclass(frozen=True)
class _ShortForm:
    text: str
    years: int = 0
    depth: int = 0
    groups: int = 0
    offsets: int = 0

    @property
    def score(self):
        # Unrepeated offsets obscure an irregular set's actual dates. Repeated
        # strides/motifs are meaningful patterns and carry no such penalty.
        return self.offsets, len(self.text), self.years, self.depth, self.groups, self.text


def _transition_atoms(previous, value, anchor):
    """Certify dates and explicit clocks against the independent reference.

    Clock changes use explicit HH:MM instructions so changing one occurrence
    does not make later clocks depend on a relative minute/hour offset.
    """
    if _instant(previous) == _instant(value):
        return {'+0d': 0}
    proposals = []
    same_clock = value.timetz().replace(tzinfo=None) == previous.timetz().replace(tzinfo=None)
    days = (value.date() - previous.date()).days
    months = (value.year - previous.year) * 12 + value.month - previous.month
    years = value.year - previous.year
    if same_clock and days > 0:
        if days % 7 == 0:
            proposals.append((f'+{days // 7}w', _Relative(days // 7, 'w'), 0))
        proposals.append((f'+{days}d', _Relative(days, 'd'), 0))
        if months > 0:
            proposals.append((f'+{months}m', _Relative(months, 'm'), 0))
            proposals.append((f'+{months}m!', _Relative(months, 'm', True), 0))
            if months > 1:
                proposals.append(('+1m', _Relative(1, 'm'), 0))
        if years > 0:
            proposals.append((f'+{years}y', _Relative(years, 'y'), 0))
            proposals.append((f'+{years}y!', _Relative(years, 'y', True), 0))
            if years > 1:
                proposals.append(('+1y', _Relative(1, 'y'), 0))
    clock = f'{value.hour}:{value.minute:02d}'
    if not value.second and not value.microsecond:
        proposals.append((clock, _Absolute(hour=value.hour, minute=value.minute), 0))
    prefixes = []
    if value.timetz().replace(tzinfo=None) == anchor.timetz().replace(tzinfo=None):
        prefixes.append(('', None, None))
    elif not value.second and not value.microsecond:
        prefixes.append((clock + '-', value.hour, value.minute))
    for prefix, hour, minute in prefixes:
        proposals.extend((
            (prefix + f'{value.day}.', _Absolute(value.day, hour=hour, minute=minute), 0),
            (prefix + f'{value.day}.{value.month}.', _Absolute(value.day, value.month, hour=hour, minute=minute), 0),
            (prefix + f'{value.day}.{value.month}.{value.year:04d}',
             _Absolute(value.day, value.month, value.year, hour, minute), 1),
        ))
    result = {}
    for text, instruction, full_year in proposals:
        try:
            actual = (_relative(instruction, previous, anchor.day if instruction.clamp else None) if isinstance(instruction, _Relative)
                      else _absolute(instruction, previous, anchor))
        except PatternError:
            continue
        if _instant(actual) == _instant(value):
            result[text] = full_year
    return result


def _simplify_masks(masks, forms, *, prefer_dates=True):
    """Bounded lattice DP: concatenation, shared atoms, and periodic blocks.

    Short blocks are searched directly. Longer primitive periods are proposed
    by stable-common and relative-preferred signatures. Memoized intersections
    let a repeated block choose a common atom that was not locally shortest.
    A deterministic work budget bounds preview latency on low-entropy inputs.
    """
    atom_cache, cache = {}, {}
    work = [2_000_000]
    cheap = sum(1 << index for index, form in enumerate(forms) if len(form.text) <= 4)
    frequencies = [0] * len(forms)
    for mask in masks:
        bits = mask
        while bits:
            bit = bits & -bits
            frequencies[bit.bit_length() - 1] += 1
            bits -= bit

    def atom(mask, patterned=False):
        key = mask, patterned
        if key not in atom_cache:
            bits, choices = mask, []
            while bits:
                bit = bits & -bits
                form = forms[bit.bit_length() - 1]
                choices.append(form if not patterned else _ShortForm(form.text, form.years))
                bits -= bit
            atom_cache[key] = min(choices, key=lambda form: form.score)
        return atom_cache[key]

    def signature(mask, prefer_relative):
        bits, choices = mask, []
        while bits:
            bit = bits & -bits
            index = bit.bit_length() - 1
            form = forms[index]
            choices.append((index, form))
            bits -= bit
        if prefer_relative:
            return min(choices, key=lambda pair: (not pair[1].text.startswith('+'), pair[1].score))[0]
        return min(choices, key=lambda pair: (-frequencies[pair[0]], pair[1].score))[0]

    def consider(prefix, suffix, best):
        text = prefix.text + (' ' + suffix.text if suffix.text else '')
        candidate = _ShortForm(text, prefix.years + suffix.years, max(prefix.depth, suffix.depth),
                               prefix.groups + suffix.groups, prefix.offsets + suffix.offsets)
        return candidate if candidate.score < best.score else best

    def encode(sequence, level, patterned=False):
        key = sequence, level, patterned
        if key in cache:
            return cache[key]
        count = len(sequence)
        best = [_ShortForm('')] * (count + 1)
        signatures = [[signature(mask, relative) for mask in sequence] for relative in (False, True)]
        cheap_runs = [0] * count
        for start in range(count):
            shared = cheap
            for stop in range(start, count):
                shared &= sequence[stop]
                if not shared:
                    break
                cheap_runs[start] = stop - start + 1
        for start in range(count - 1, -1, -1):
            first = atom(sequence[start], patterned)
            tail = best[start + 1]
            selected = _ShortForm(first.text + (' ' + tail.text if tail.text else ''),
                                  first.years + tail.years, tail.depth, tail.groups,
                                  first.offsets + tail.offsets)
            shared = sequence[start]
            for stop in range(start + 1, count):
                shared &= sequence[stop]
                if not shared:
                    break
                common = atom(shared, True)
                repeated = _ShortForm(f'{stop - start + 1}*{common.text}', common.years)
                selected = consider(repeated, best[stop + 1], selected)
            if level < MAX_NESTING and work[0] > 0:
                # Fair per-suffix work prevents a dense periodic tail from
                # consuming the budget before the fixed anchor is considered.
                start_work = 1024
                maximum = (count - start) // 2
                periods = set(range(2, min(32, maximum) + 1))
                # KMP borders propose long primitive repeated blocks without
                # searching every possible grammar or every interval split.
                for full in signatures:
                    values = full[start:]
                    borders = [0] * len(values)
                    for offset in range(1, len(values)):
                        border = borders[offset - 1]
                        while border and values[offset] != values[border]:
                            border = borders[border - 1]
                        if values[offset] == values[border]:
                            border += 1
                        borders[offset] = border
                        size = offset + 1
                        if size % 2 == 0:
                            period = size // 2
                            if 32 < period <= maximum and size - border == period:
                                periods.add(period)
                for period in sorted(periods):
                    if start_work <= 0:
                        break
                    if cheap_runs[start] >= period * 2:
                        continue  # A cheap shared atom is strictly simpler.
                    common = list(sequence[start:start + period])
                    repetitions = 1
                    while start + (repetitions + 1) * period <= count and work[0] > 0 and start_work >= period:
                        offset = start + repetitions * period
                        valid = True
                        for index in range(period):
                            work[0] -= 1
                            start_work -= 1
                            common[index] &= sequence[offset + index]
                            if not common[index]:
                                valid = False
                                break
                        if not valid:
                            break
                        repetitions += 1
                        body = encode(tuple(common), level + 1, True)
                        grouped = _ShortForm(f'{repetitions}*({body.text})', body.years,
                                             body.depth + 1, body.groups + 1)
                        selected = consider(grouped, best[start + repetitions * period], selected)
            best[start] = selected
        result = best[0]
        if len(cache) < 4096:
            cache[key] = result
        return result

    return encode(tuple(masks), 0, not prefer_dates).text


def _linear_sequence(values, *, anchor, prefer_dates=True):
    """Bounded per-transition work for sets too large for block optimization.

    Adjacent transitions sharing one verified instruction form a repeat run;
    other transitions choose the same concise-date preference as the full
    optimizer. This deliberately searches no nested or alternating motifs.
    """
    candidates = [_transition_atoms(previous, value, anchor)
                  for previous, value in zip([anchor, *values[:-1]], values)]
    if any(not choices for choices in candidates):
        return serialize_sequence(values, anchor=anchor)
    result, index = [], 0
    while index < len(candidates):
        shared = set(candidates[index])
        end = index + 1
        while end < len(candidates):
            common = shared & candidates[end].keys()
            if not common:
                break
            shared = common
            end += 1
        if end - index > 1:
            atom = min(shared, key=lambda text: (len(text), candidates[index][text], text))
            result.append(f'{end - index}*{atom}')
        else:
            atom = min(shared, key=lambda text: (int(prefer_dates and text.startswith('+')),
                                                len(text), candidates[index][text], text))
            result.append(atom)
        index = end
    expression = ' '.join(result)
    reproduced = expand_sequence(expression, anchor, limit=max(DEFAULT_LIMIT, len(values)))
    if [_instant(value) for value in reproduced] != [_instant(value) for value in values]:
        return serialize_sequence(values, anchor=anchor)
    return expression


def simplify_sequence(starts: Iterable[datetime], *, anchor=None, prefer_dates=True) -> str:
    """Choose a short, verified sequence for clicked dates and precise clocks.

    The independent reference and supplied occurrence order are retained. Optimization is
    bounded to 500 instants; larger sets use a linear run/concise-atom pass.
    Every atom is certified against the original anchor, and the final result
    must reproduce every timestamp before it is returned. Unrepeated dates
    prefer explicit calendar entries; ``prefer_dates=False`` also allows short
    isolated offsets when describing the phase of an ongoing regular tail.
    """
    values = _occurrence_starts(starts)
    anchor = _normalizer_anchor(values, anchor)
    values = [value.astimezone(anchor.tzinfo) for value in values]
    if not values:
        return 'none'
    if len(values) > DEFAULT_LIMIT:
        return _linear_sequence(values, anchor=anchor, prefer_dates=prefer_dates)
    indexes, forms, masks = {}, [], []
    for previous, value in zip([anchor, *values[:-1]], values):
        candidates = _transition_atoms(previous, value, anchor)
        if not candidates:
            return serialize_sequence(values, anchor=anchor)
        mask = 0
        for text, full_year in candidates.items():
            if text not in indexes:
                indexes[text] = len(forms)
                forms.append(_ShortForm(text, full_year, offsets=int(text.startswith('+'))))
            mask |= 1 << indexes[text]
        masks.append(mask)
    expression = _simplify_masks(masks, forms, prefer_dates=prefer_dates)
    try:
        expanded = expand_sequence(expression, anchor)
    except PatternError:
        return serialize_sequence(values, anchor=anchor)
    if [_instant(value) for value in expanded] != [_instant(value) for value in values]:
        return serialize_sequence(values, anchor=anchor)
    return expression


def reconstruct_sequence(starts: Iterable[datetime], *, anchor=None, prefer_dates=True) -> str:
    """Equivalent short instructions for a finite occurrence set."""
    return simplify_sequence(starts, anchor=anchor, prefer_dates=prefer_dates)


def reconstruct_pattern(dates: list[date]) -> str | None:
    """Build new-syntax instructions after the first date, preserving that anchor."""
    if not dates or any(not isinstance(day, date) or isinstance(day, datetime) for day in dates):
        return None
    try:
        return reconstruct_sequence(datetime.combine(day, time.min, timezone.utc) for day in dates)
    except PatternError:
        return None


def shift_expression_anchor(anchor: str, days: int) -> str:
    if not anchor:
        return ''
    try:
        return (date.fromisoformat(anchor) + timedelta(days=days)).isoformat()
    except (ValueError, OverflowError) as exc:
        raise PatternError('The custom schedule anchor cannot move outside the supported date range.') from exc


def shift_schedule_expression(expression: str, anchor: str, shift: timedelta | int,
                              starts: Iterable[datetime | date], *, syntax: int = 2) -> tuple[str, str]:
    """Keep a moved schedule's text equivalent to its actual occurrence instants.

    Literals keep their exact formatting when re-evaluation from the moved
    first event produces the same instants. Other instructions are reconstructed
    in the normal concise form. Only the current occurrence grammar is accepted.
    """
    if not shift or not expression:
        return expression, anchor
    delta = timedelta(days=shift) if isinstance(shift, int) else shift
    raw = list(starts)
    values = [value if isinstance(value, datetime) else datetime.combine(value, time.min, timezone.utc) for value in raw]
    values = _occurrence_starts(values)
    if syntax != 2:
        raise PatternError('Unsupported custom sequence version.')
    try:
        expanded = expand_sequence(expression, values[0], limit=MAX_SEQUENCE_EVENTS)
        matches = {_instant(value) for value in expanded} == {_instant(value) for value in values}
    except PatternError:
        matches = False
    if matches:
        return expression, values[0].date().isoformat()
    serialized = reconstruct_sequence(values)
    return serialized, values[0].date().isoformat()


def describe_pattern(expression: str, anchor: date, limit: int = DEFAULT_LIMIT) -> str:
    days = expand_pattern(expression, anchor, limit)
    return ('No occurrences' if not days else f'1 event · {days[0]:%d.%m.%Y}' if len(days) == 1 else
            f'{len(days)} events · {days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}')
