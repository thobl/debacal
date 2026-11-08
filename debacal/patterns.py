"""Finite instructions and standard recurrence tails anchored at one fixed event.

The anchor is always the first result. Each instruction moves a cursor and adds
an occurrence; duplicate instants are retained only once. Relative instructions
advance local wall time; day/week/month/year steps preserve the preceding clock.
Date instructions without a clock use the original anchor's clock. Omitted
date parts choose the next matching instant;
fully specified dates are exact, including dates before the cursor or anchor.

Ambiguous relative times retain the preceding fold; absolute dates retain the
anchor's fold, and time-only instructions retain the preceding fold. Nonexistent
DST times are rejected rather than normalized. All comparisons use timestamps.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Iterable


DEFAULT_LIMIT = 500
MAX_SEQUENCE_EVENTS = 10000
MAX_PATTERN_LENGTH = 8192
MAX_NESTING = 8
_INTEGER = re.compile(r'[0-9]+')
_CLOCK = re.compile(r'([0-9]{1,2}):([0-9]{2})')
_TAIL = re.compile(r'(?<![^\s,])every\(\s*([0-9]+)\s*([dwmy]?)\s*\)\s*$', re.I)
_FREQUENCIES = {'d': 'DAILY', 'w': 'WEEKLY', 'm': 'MONTHLY', 'y': 'YEARLY'}


class PatternError(ValueError):
    """Invalid syntax, an impossible local time, or an unrepresentable schedule."""


@dataclass(frozen=True)
class SequenceTail:
    prefix: str
    interval: int
    unit: str

    @property
    def frequency(self):
        return _FREQUENCIES[self.unit]


@dataclass(frozen=True)
class CompiledSequence:
    """A finite prefix followed by an interoperable, unbounded calendar rule."""
    prefix_starts: tuple[datetime, ...]
    tail_start: datetime
    recurrence_rule: str
    recurrence_dates: tuple[str, ...]
    excluded_dates: tuple[str, ...]

    @property
    def fields(self):
        return dict(recurrence_rule=self.recurrence_rule, recurrence_dates=self.recurrence_dates,
                    excluded_dates=self.excluded_dates)


def sequence_tail(expression: str) -> SequenceTail | None:
    """Recognize a terminal every(Nunit), without expanding an endless list.

    This is a standard calendar recurrence, distinct from relative +m/+y
    instructions: missing month days and leap dates are skipped, not clipped.
    """
    if not isinstance(expression, str):
        raise PatternError('The sequence must be text.')
    if len(expression) > MAX_PATTERN_LENGTH:
        raise PatternError(f'A sequence may contain at most {MAX_PATTERN_LENGTH} characters.')
    if 'every' not in expression.lower():
        return None
    match = _TAIL.search(expression)
    if match is None or 'every' in expression[:match.start()].lower():
        raise PatternError('Use every(1d), every(2w), every(1m) or every(1y) as the final instruction.')
    if len(match[1]) > 3 or not 1 <= int(match[1]) <= 999:
        raise PatternError('The indefinite repeat interval must be between 1 and 999.')
    prefix = expression[:match.start()].rstrip()
    if prefix.endswith(','):
        prefix = prefix[:-1].rstrip()
    return SequenceTail(prefix, int(match[1]), match[2].lower() or 'w')


def expression_for_regular(kind, interval=1):
    """A simple unbounded recurrence has a concise custom authoring form."""
    unit = next((unit for unit, frequency in _FREQUENCIES.items() if frequency == str(kind).upper()), None)
    if unit is None or type(interval) is not int or not 1 <= interval <= 999:
        raise PatternError('Choose a daily, weekly, monthly or yearly interval between 1 and 999.')
    return f'every({interval}{unit})'


@dataclass(frozen=True)
class _Relative:
    amount: int
    unit: str


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
        raise PatternError('The fixed event needs a timezone-aware date and time.')


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
    def __init__(self, expression, limit):
        if not isinstance(expression, str):
            raise PatternError('The sequence must be text.')
        if len(expression) > MAX_PATTERN_LENGTH:
            raise PatternError(f'A sequence may contain at most {MAX_PATTERN_LENGTH} characters.')
        self.expression = expression
        self.position = 0
        self.budget = limit - 1

    def error(self, message):
        raise PatternError(f'{message} (position {self.position + 1}).')

    def spaces(self):
        start = self.position
        while self.position < len(self.expression) and self.expression[self.position].isspace():
            self.position += 1
        return self.position > start

    def bounded(self, size):
        if size > self.budget:
            raise PatternError(f'This sequence exceeds the limit of {self.budget + 1} events, including the fixed event.')

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
                self.error('A relative step needs a positive number, for example +2 or +3d')
            if len(match[0]) > 9:
                self.error('The relative step exceeds the supported date range')
            amount = int(match[0])
            if amount == 0:
                self.error('A relative step must move forward; +0 is not allowed')
            self.position = match.end()
            unit_start = self.position
            while self.position < len(self.expression) and self.expression[self.position].isalpha():
                self.position += 1
            unit = self.expression[unit_start:self.position].lower()
            if unit not in ('', 'min', 'h', 'd', 'w', 'm', 'y'):
                self.error('Use min, h, d, w, m or y for minutes, hours, days, weeks, months or years')
            return _Relative(amount, unit or 'w')
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
        # A day always has a dot. A trailing dot after month/year is accepted;
        # the concise day.month form is also unambiguous in this grammar.
        match = re.compile(r'([0-9]{1,2})\.').match(self.expression, self.position)
        if not match:
            self.error('Expected +N, HH:MM, or a date such as 13., 3.2., or 3.2.2027')
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
                        self.position += 1
                elif self.position < len(self.expression) and self.expression[self.position].isdigit():
                    self.error('Use four digits for a fully specified year')
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


def _relative(step, previous):
    try:
        if step.unit in ('min', 'h'):
            value = previous + timedelta(minutes=step.amount * (60 if step.unit == 'h' else 1))
        elif step.unit in ('d', 'w'):
            value = previous + timedelta(days=step.amount * (7 if step.unit == 'w' else 1))
        else:
            months = step.amount * (12 if step.unit == 'y' else 1)
            index = previous.year * 12 + previous.month - 1 + months
            year, month = divmod(index, 12)
            if not 1 <= year <= 9999:
                raise OverflowError
            month += 1
            value = previous.replace(year=year, month=month,
                                     day=min(previous.day, monthrange(year, month)[1]))
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
    """Return the fixed initial event followed by the sequence's unique instants.

    The instruction budget includes the initial event and every expanded step,
    even a repeated explicit date that is later deduplicated. Full dates are
    exact; partial dates, clock-only and relative steps always advance.
    """
    _check_limit(limit)
    _aware(anchor)
    _validate_wall(anchor)
    if sequence_tail(expression) is not None:
        raise PatternError('An indefinite sequence has no finite list of events. Browse its occurrences in the preview.')
    steps = _Parser(expression, limit).sequence()
    return _execute_steps(steps, anchor)[0]


def _execute_steps(steps, anchor):
    values, seen = [anchor], {_instant(anchor)}
    previous = anchor
    for step in steps:
        previous = _relative(step, previous) if isinstance(step, _Relative) else _absolute(step, previous, anchor)
        stamp = _instant(previous)
        if stamp not in seen:
            values.append(previous)
            seen.add(stamp)
    return values, previous


def compile_sequence(expression: str, anchor: datetime, *, all_day=False,
                     limit: int = DEFAULT_LIMIT) -> CompiledSequence | None:
    """Compile an indefinite tail into one RRULE plus finite date exceptions.

    The original primary remains DTSTART. Prefixes must share that DTSTART's
    recurrence phase; otherwise one standard rule cannot represent them and we
    refuse the conversion rather than shifting or truncating any occurrences.
    Only the bounded period preceding the tail is visited.
    """
    tail = sequence_tail(expression)
    if tail is None:
        return None
    from dateutil.rrule import rrulestr
    _check_limit(limit)
    _aware(anchor)
    _validate_wall(anchor)
    values, start = _execute_steps(_Parser(tail.prefix, limit).sequence(), anchor)
    if all_day and any(value.time() != time.min for value in values):
        raise PatternError('Time entries require a timed event.')
    if start.microsecond or anchor.microsecond:
        raise PatternError('An indefinite recurrence requires whole-second times.')
    if _instant(start) < _instant(anchor):
        raise PatternError('The indefinite tail must start at or after the primary event.')
    if tail.unit == 'd':
        phase = (start.date() - anchor.date()).days
    elif tail.unit == 'w':
        first_week = anchor.date() - timedelta(days=anchor.weekday())
        tail_week = start.date() - timedelta(days=start.weekday())
        phase = (tail_week - first_week).days // 7
    elif tail.unit == 'm':
        phase = (start.year - anchor.year) * 12 + start.month - anchor.month
    else:
        phase = start.year - anchor.year
    if phase % tail.interval:
        raise PatternError('This indefinite tail does not align with the primary event for that interval. '
                           'Choose an aligned start or a different interval.')
    parts = [f'FREQ={tail.frequency}', f'INTERVAL={tail.interval}']
    if tail.unit == 'w' and start.weekday() != anchor.weekday():
        parts.append('BYDAY=' + ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')[start.weekday()])
    if tail.unit in ('m', 'y') and start.day != anchor.day:
        parts.append('BYMONTHDAY=' + str(start.day))
    if tail.unit == 'y' and start.month != anchor.month:
        parts.append('BYMONTH=' + str(start.month))
    for key, actual, original in (('BYHOUR', start.hour, anchor.hour), ('BYMINUTE', start.minute, anchor.minute),
                                  ('BYSECOND', start.second, anchor.second)):
        if actual != original:
            parts.append(f'{key}={actual}')
    rule = ';'.join(parts)
    key = (lambda value: value.date().isoformat()) if all_day else (lambda value: _instant(value).isoformat())
    desired = {key(value) for value in values}
    generated = set()
    found = False
    for index, value in enumerate(rrulestr(rule, dtstart=anchor)):
        if index >= MAX_SEQUENCE_EVENTS:
            raise PatternError('This indefinite tail needs more than 10,000 earlier recurrence exceptions.')
        if not _valid_wall(value):
            continue
        if _instant(value) >= _instant(start):
            found = _instant(value) == _instant(start)
            break
        generated.add(key(value))
    if not found:
        raise PatternError('This indefinite tail cannot retain its exact time in a standard calendar rule.')
    # Tail DTSTART is generated by the rule; the primary is always a set member.
    rdates = desired - generated - {key(anchor), key(start)}
    exclusions = generated - desired
    return CompiledSequence(tuple(values), start, rule, tuple(sorted(rdates)), tuple(sorted(exclusions)))


def expand_pattern(expression: str, anchor: date, limit: int = DEFAULT_LIMIT) -> list[date]:
    """Date-only compatibility wrapper; new syntax uses a UTC midnight anchor."""
    if not isinstance(anchor, date) or isinstance(anchor, datetime):
        raise PatternError('A calendar date is required for date-only expansion.')
    return [value.date() for value in expand_sequence(expression, datetime.combine(anchor, time.min, timezone.utc), limit)]


def _unique_starts(starts: Iterable[datetime]):
    values, seen = [], set()
    for value in starts:
        _aware(value)
        _validate_wall(value)
        if _instant(value) not in seen:
            values.append(value)
            seen.add(_instant(value))
    if not values:
        raise PatternError('At least the fixed event is required.')
    try:
        return [value.astimezone(values[0].tzinfo) for value in values]
    except (ValueError, OverflowError) as exc:
        raise PatternError('The timezone conversion extends beyond the supported years 1–9999.') from exc


def _absolute_text(value, anchor):
    day = f'{value.day}.{value.month}.{value.year:04d}.'
    if value.timetz().replace(tzinfo=None) == anchor.timetz().replace(tzinfo=None):
        return day
    if value.second or value.microsecond:
        raise PatternError('This schedule has different sub-minute clocks that HH:MM instructions cannot represent losslessly.')
    return value.strftime('%H:%M-') + day


def _serialize(starts, relative):
    values = _unique_starts(starts)
    atoms = []
    for previous, value in zip(values, values[1:]):
        days = (value.date() - previous.date()).days
        same_clock = value.timetz().replace(tzinfo=None) == previous.timetz().replace(tzinfo=None)
        if relative and days > 0 and same_clock:
            candidate = '+' + str(days // 7) if days % 7 == 0 else f'+{days}d'
            try:
                matches = _instant(expand_sequence(candidate, previous)[-1]) == _instant(value)
            except PatternError:
                matches = False
            atoms.append(candidate if matches else _absolute_text(value, values[0]))
        else:
            atoms.append(_absolute_text(value, values[0]))
    compact = []
    for atom in atoms:
        if compact and compact[-1][0] == atom:
            compact[-1] = (atom, compact[-1][1] + 1)
        else:
            compact.append((atom, 1))
    expression = ' '.join(f'{count}*{atom}' if count > 1 else atom for atom, count in compact)
    reproduced = expand_sequence(expression, values[0], limit=max(DEFAULT_LIMIT, len(values)))
    if [_instant(value) for value in reproduced] != [_instant(value) for value in values]:
        raise PatternError('This schedule contains ambiguous local times that the sequence cannot represent losslessly.')
    return expression


def serialize_sequence(starts: Iterable[datetime]) -> str:
    """Lossless full-date instructions excluding the supplied first fixed event."""
    return _serialize(starts, False)


def shift_unbounded_expression(expression: str, previous: datetime, current: datetime) -> tuple[str, str]:
    """Move a finite prefix and retain its terminal calendar recurrence."""
    tail = sequence_tail(expression)
    if tail is None:
        raise PatternError('An indefinite sequence is required.')
    old = compile_sequence(expression, previous)
    zone = previous.tzinfo
    current = current.astimezone(zone)
    delta = current.replace(tzinfo=None) - previous.replace(tzinfo=None)
    def shifted(value):
        return _validate_wall((value.replace(tzinfo=None) + delta).replace(tzinfo=zone, fold=value.fold))
    expected = tuple(shifted(value) for value in old.prefix_starts)
    cursor = shifted(old.tail_start)
    try:
        candidate = compile_sequence(expression, current)
        if ({_instant(value) for value in candidate.prefix_starts} == {_instant(value) for value in expected}
                and _instant(candidate.tail_start) == _instant(cursor)):
            return expression, current.date().isoformat()
    except PatternError:
        pass
    prefix = serialize_sequence(expected)
    if _instant(expected[-1]) != _instant(cursor):
        prefix = (prefix + ' ' + _absolute_text(cursor, current)).strip()
    result = (prefix + ' ' + expression_for_regular(tail.frequency, tail.interval)).strip()
    compiled = compile_sequence(result, current)
    if ({_instant(value) for value in compiled.prefix_starts} != {_instant(value) for value in expected}
            or _instant(compiled.tail_start) != _instant(cursor)):
        raise PatternError('The moved indefinite sequence cannot retain its exact dates.')
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
    """Certify dates and explicit clocks against the original fixed event.

    Minute/hour offsets remain readable but are not generated: changing one
    occurrence's clock should not turn later clocks into relative offsets.
    """
    proposals = []
    same_clock = value.timetz().replace(tzinfo=None) == previous.timetz().replace(tzinfo=None)
    days = (value.date() - previous.date()).days
    months = (value.year - previous.year) * 12 + value.month - previous.month
    years = value.year - previous.year
    if same_clock and days > 0:
        if days % 7 == 0:
            proposals.append((f'+{days // 7}', _Relative(days // 7, 'w'), 0))
        proposals.append((f'+{days}d', _Relative(days, 'd'), 0))
        if months > 0:
            proposals.append((f'+{months}m', _Relative(months, 'm'), 0))
        if years > 0:
            proposals.append((f'+{years}y', _Relative(years, 'y'), 0))
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
            (prefix + f'{value.day}.{value.month}', _Absolute(value.day, value.month, hour=hour, minute=minute), 0),
            (prefix + f'{value.day}.{value.month}.{value.year:04d}',
             _Absolute(value.day, value.month, value.year, hour, minute), 1),
        ))
    result = {}
    for text, instruction, full_year in proposals:
        try:
            actual = (_relative(instruction, previous) if isinstance(instruction, _Relative)
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


def simplify_sequence(starts: Iterable[datetime], *, prefer_dates=True) -> str:
    """Choose a short, verified sequence for clicked dates and precise clocks.

    The fixed first event and instruction order are retained. Optimization is
    bounded to 500 instants; larger legacy sets use the full-date serializer.
    Every atom is certified against the original anchor, and the final result
    must reproduce every timestamp before it is returned. Unrepeated dates
    prefer explicit calendar entries; ``prefer_dates=False`` also allows short
    isolated offsets when describing the phase of an ongoing regular tail.
    """
    values = _unique_starts(starts)
    if len(values) == 1:
        return ''
    if len(values) > DEFAULT_LIMIT:
        return serialize_sequence(values)
    indexes, forms, masks = {}, [], []
    for previous, value in zip(values, values[1:]):
        candidates = _transition_atoms(previous, value, values[0])
        if not candidates:
            return serialize_sequence(values)
        mask = 0
        for text, full_year in candidates.items():
            if text not in indexes:
                indexes[text] = len(forms)
                forms.append(_ShortForm(text, full_year, offsets=int(text.startswith('+'))))
            mask |= 1 << indexes[text]
        masks.append(mask)
    expression = _simplify_masks(masks, forms, prefer_dates=prefer_dates)
    try:
        expanded = expand_sequence(expression, values[0])
    except PatternError:
        return serialize_sequence(values)
    if [_instant(value) for value in expanded] != [_instant(value) for value in values]:
        return serialize_sequence(values)
    return expression


def reconstruct_sequence(starts: Iterable[datetime], *, prefer_dates=True) -> str:
    """Equivalent short instructions for legacy sources and current date sets."""
    return simplify_sequence(starts, prefer_dates=prefer_dates)


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

    Relative-only literals keep their exact formatting when re-evaluation from
    the moved first event produces the same instants. Absolute instructions are
    rewritten as full dates and clocks. Legacy syntax delegates to its preserved
    implementation; callers must supply syntax=1 for legacy authoring text.
    """
    if not shift or not expression:
        return expression, anchor
    delta = timedelta(days=shift) if isinstance(shift, int) else shift
    raw = list(starts)
    values = [value if isinstance(value, datetime) else datetime.combine(value, time.min, timezone.utc) for value in raw]
    values = _unique_starts(values)
    if syntax == 1:
        from . import legacy_patterns
        days = (values[0].date() - (values[0] - delta).date()).days
        try:
            return legacy_patterns.shift_schedule_expression(expression, anchor, days, [value.date() for value in values])
        except legacy_patterns.PatternError as exc:
            raise PatternError(str(exc)) from exc
    if syntax != 2:
        raise PatternError('Unsupported custom sequence version.')
    steps = _Parser(expression, MAX_SEQUENCE_EVENTS).sequence()
    if all(isinstance(step, _Relative) for step in steps):
        try:
            expanded = expand_sequence(expression, values[0], limit=MAX_SEQUENCE_EVENTS)
            matches = {_instant(value) for value in expanded} == {_instant(value) for value in values}
        except PatternError:
            matches = False
        if matches:
            return expression, values[0].date().isoformat()
    serialized = serialize_sequence(values)
    if not serialized:
        serialized = _absolute_text(values[0], values[0])
    return serialized, values[0].date().isoformat()


def describe_pattern(expression: str, anchor: date, limit: int = DEFAULT_LIMIT) -> str:
    days = expand_pattern(expression, anchor, limit)
    return (f'1 event · {days[0]:%d.%m.%Y}' if len(days) == 1 else
            f'{len(days)} events · {days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}')
