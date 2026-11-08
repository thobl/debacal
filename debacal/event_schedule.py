"""Recurrence authoring independent of event-editor widgets.

Canonical calendar fields remain the source of truth for imported schedules.
Authoring text is retained until a schedule edit deliberately replaces it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from dateutil.rrule import rrulestr

from .models import Event
from .patterns import DEFAULT_LIMIT, PatternError, expand_sequence, reconstruct_sequence, shift_schedule_expression
from .recurrence import (
    RecurrenceError, base_schedule_starts, event_zone, expand_event,
    key_datetime, occurrence, recurrence_key, schedule_fields,
    updated_series,
)


REGULAR_KINDS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
MAX_REPEAT_COUNT = 100000
_DAY_NAMES = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
_SCHEDULE_FIELDS = ('recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides',
                    'schedule_expression', 'expression_anchor', 'schedule_syntax', 'timezone_id')


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError('The recurrence needs a timezone-aware start.')
    return value


def _checked(value):
    _aware(value)
    if value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise RecurrenceError('This occurrence falls in a daylight-saving clock change. Choose another time.')
    return value


def _same(values, other, all_day=False):
    key = (lambda value: value.date()) if all_day else (lambda value: value.astimezone(timezone.utc))
    return {key(value) for value in values} == {key(value) for value in other}


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
                    until = stamp.astimezone(start.tzinfo).date()
                count = until_to_count(start, kind, interval, until, days)
                return cls(kind, interval, count, until, days, 'until')
            return cls(kind, interval, None, None, days, 'never')
        except (ValueError, OverflowError, TypeError):
            return None


def detect_regular_schedule(starts: Iterable[datetime], *, all_day=False) -> RegularSchedule | None:
    """Classify only exact finite calendar-unit schedules, never clock offsets."""
    values = list(starts)
    if len(values) < 2:
        return None
    fields = schedule_fields(values, all_day=all_day)
    if not fields['recurrence_rule']:
        return None
    return RegularSchedule.from_rule(fields['recurrence_rule'], values[0], all_day=all_day)


@dataclass
class ScheduleDraft:
    """Small mutable authoring draft with explicit schedule-changing methods.

    Source values are copied exactly for ordinary edits. A loaded expression
    shifts its existing concrete dates when the primary moves; newly typed text
    instead evaluates from the newly chosen primary timestamp.
    """
    source: Event | None = None
    authoring_zone: object = None
    kind: str = 'once'
    interval: int = 1
    count: int | None = 6
    until: date | None = None
    ending: str = 'count'
    weekdays: tuple[int, ...] = ()
    expression: str = ''
    advanced_rule: str = ''
    initial_expression: str = ''
    _loaded_config: tuple = field(default=(), repr=False)
    _expression_mode: bool = field(default=False, repr=False)
    _loaded_expression_mode: bool = field(default=False, repr=False)
    _materialized: list[datetime] | None = field(default=None, repr=False)
    _edited_fields: dict | None = field(default=None, repr=False)
    _edited_master: Event | None = field(default=None, repr=False)
    _edited_reference_start: datetime | None = field(default=None, repr=False)
    _edited_reference_end: datetime | None = field(default=None, repr=False)
    _primary_reference_start: datetime | None = field(default=None, repr=False)
    _primary_reference_end: datetime | None = field(default=None, repr=False)

    @classmethod
    def from_event(cls, source: Event | None = None, *, authoring_zone=None):
        draft = cls(source=source, authoring_zone=authoring_zone)
        if source is not None:
            zone = authoring_zone or event_zone(source)
            start = (datetime.combine(source.start.date(), time.min, zone) if source.all_day
                     else source.start.astimezone(zone))
            regular = RegularSchedule.from_rule(source.recurrence_rule, start, all_day=source.all_day) if source.recurrence_rule else None
            if source.schedule_expression or (source.recurrence_dates and not source.recurrence_rule):
                draft.kind = 'custom'
                draft.expression = source.schedule_expression
                try:
                    values = [datetime.combine(value.date(), time.min, zone) if source.all_day else value.astimezone(zone)
                              for value in base_schedule_starts(source, limit=DEFAULT_LIMIT)]
                    draft._materialized = values
                    faithful = source.schedule_syntax == 2 and bool(source.schedule_expression)
                    if faithful:
                        try:
                            faithful = _same(expand_sequence(source.schedule_expression, values[0]), values, source.all_day)
                        except PatternError:
                            faithful = False
                    if not faithful:
                        draft.expression = reconstruct_sequence(values)
                    if regular is None:
                        regular = detect_regular_schedule(values, all_day=source.all_day)
                except (ValueError, OverflowError):
                    # Precise legacy clocks may not fit HH:MM. Preserve their
                    # canonical values; display text never destroys them.
                    pass
                draft._expression_mode = True
            elif source.recurrence_rule:
                draft.kind = 'advanced'
            if regular is not None:
                draft._take_regular(regular)
            draft.advanced_rule = source.recurrence_rule
        draft.initial_expression = draft.expression
        draft._loaded_expression_mode = draft._expression_mode
        draft._loaded_config = draft._config()
        return draft

    def _config(self):
        return (self.kind, self.interval, self.count, self.until, self.ending, tuple(self.weekdays),
                self.expression, self.advanced_rule, self._expression_mode)

    @property
    def configuration_changed(self):
        return self._edited_fields is not None or self._config() != self._loaded_config

    @property
    def dirty(self):
        return self.configuration_changed

    def _take_regular(self, regular):
        self.kind, self.interval = regular.kind, regular.interval
        self.count, self.until, self.weekdays, self.ending = regular.count, regular.until, regular.weekdays, regular.ending

    def set_regular(self, kind, interval=1, count=6, until=None, weekdays=(), ending='count'):
        regular = RegularSchedule(str(kind).upper(), interval, count, until, tuple(weekdays), ending)
        _parts(regular.kind, regular.interval, regular.weekdays)
        if self._edited_master is not None:
            # Editing the rule after a graphical exception must retain that
            # exception, including newly chosen occurrence-specific ranges.
            self.source = self._edited_master
            self._primary_reference_start = self._edited_reference_start
            self._primary_reference_end = self._edited_reference_end
        self._take_regular(regular)
        self.expression = ''
        self._expression_mode = False
        self._materialized = None
        self._edited_fields = None
        self._edited_master = None

    def use_sequence(self, master, *, reference_start=None, reference_end=None):
        """Change the authoring view while retaining canonical exceptions."""
        # Refuse an unbounded/oversized conversion before touching the draft.
        base_schedule_starts(master, limit=DEFAULT_LIMIT)
        self._adopt_graphical(master, reference_start=reference_start, reference_end=reference_end)
        self.kind = 'custom'

    def set_expression(self, text, start, *, all_day=False, detect=True, _canonical=False):
        if self._edited_master is not None and not _canonical:
            self.source = self._edited_master
            self._primary_reference_start = self._edited_reference_start
            self._primary_reference_end = self._edited_reference_end
        anchor = self.author_anchor(start if _canonical else self._source_range(start, all_day=all_day)[0], all_day=all_day)
        values = expand_sequence(text, anchor)
        if all_day and any(value.time() != time.min for value in values):
            raise RecurrenceError('Time entries require a timed event.')
        regular = detect_regular_schedule(values, all_day=all_day) if detect else None
        self.kind = 'custom'
        if regular:
            self._take_regular(regular)
        self.expression = text
        self._expression_mode = True
        self._materialized = None
        self._edited_fields = None
        self._edited_master = None
        return regular

    def set_starts(self, starts, *, all_day=False):
        values = list(starts)
        if not values:
            raise RecurrenceError('The primary event must remain in the schedule.')
        text = reconstruct_sequence(values)
        return self.set_expression(text, values[0], all_day=all_day, _canonical=True)

    def bind_primary_reference(self, start, end):
        """Bind the displayed prototype to its source's nominal recurrence root.

        Following edits may start from a moved/resized detached occurrence.
        Its displayed range is a reference for user changes, not a new DTSTART.
        """
        self._primary_reference_start, self._primary_reference_end = start, end

    def _source_range(self, start, end=None, *, all_day=False):
        if self.source is None or self._primary_reference_start is None:
            return start, end
        return self._range_from_reference(self.source, start, end, self._primary_reference_start,
                                          self._primary_reference_end, all_day=all_day)

    def author_anchor(self, start, *, all_day=False):
        _aware(start)
        zone = self.authoring_zone or (event_zone(self.source) if self.source is not None else start.tzinfo)
        if all_day:
            return datetime.combine(start.date(), time.min, zone)
        return start.astimezone(zone)

    def schedule_anchor(self, start, *, all_day=False):
        """Nominal primary for recurrence controls, independent of UI exceptions."""
        if self._edited_master is not None:
            return self._project_edited(start, all_day=all_day).start
        return self.author_anchor(self._source_range(start, all_day=all_day)[0], all_day=all_day)

    def starts(self, start, *, all_day=False, limit=DEFAULT_LIMIT):
        if self._edited_master is not None:
            effective = self._project_edited(start, all_day=all_day)
            values = base_schedule_starts(effective, limit=limit)
            return [value for value in values if recurrence_key(value, all_day) not in effective.excluded_dates
                    or recurrence_key(value, all_day) == recurrence_key(effective.start, all_day)]
        anchor = self.author_anchor(self._source_range(start, all_day=all_day)[0], all_day=all_day)
        if self.source is not None and not self.configuration_changed and self._loaded_expression_mode:
            values = self._materialized if self._materialized is not None else base_schedule_starts(self.source, limit=limit)
            values = [value.replace(tzinfo=anchor.tzinfo) if all_day else value.astimezone(anchor.tzinfo) for value in values]
            previous = self.source.start.replace(tzinfo=anchor.tzinfo) if all_day else self.source.start.astimezone(anchor.tzinfo)
            shift = anchor.replace(tzinfo=None) - previous.replace(tzinfo=None)
            values = [(value.replace(tzinfo=None) + shift).replace(tzinfo=anchor.tzinfo, fold=value.fold) for value in values]
            if not all_day:
                values = [_checked(value) for value in values]
            return values
        if self.kind == 'once':
            return [anchor]
        if self._expression_mode or self.kind == 'custom':
            return expand_sequence(self.expression, anchor, limit=limit)
        rule = self.advanced_rule if self.kind == 'advanced' else self.regular().rule(anchor, all_day=all_day)
        parts = dict(part.split('=', 1) for part in rule.split(';') if '=' in part)
        if not {'COUNT', 'UNTIL'} & parts.keys():
            raise RecurrenceError('An unbounded recurrence cannot be expanded into a finite custom sequence.')
        values = []
        for value in rrulestr(rule, dtstart=anchor):
            values.append(value)
            if len(values) > limit:
                raise RecurrenceError(f'This custom sequence exceeds {limit:,} occurrences.')
        return values

    def regular(self):
        return RegularSchedule(self.kind, self.interval, self.count, self.until, tuple(self.weekdays), self.ending)

    def fields(self, start, end=None, *, all_day=False, preserve_source=True):
        """Return Event recurrence/authoring fields, preserving untouched data."""
        if self._edited_master is not None:
            effective = self._project_edited(start, end=end, all_day=all_day)
            return {name: deepcopy(getattr(effective, name)) for name in _SCHEDULE_FIELDS}
        anchor = self.author_anchor(self._source_range(start, end, all_day=all_day)[0], all_day=all_day)
        if self._edited_fields is not None:
            return deepcopy(self._edited_fields)
        source = self.source
        if preserve_source and source is not None and not self.configuration_changed and all_day == source.all_day:
            values = {name: deepcopy(getattr(source, name)) for name in _SCHEDULE_FIELDS}
            same_anchor = anchor.date() == source.start.date() if all_day else anchor == source.start
            if not self._loaded_expression_mode or same_anchor:
                return values
            starts = self.starts(start, all_day=all_day)
            previous = source.start.replace(tzinfo=anchor.tzinfo) if all_day else source.start.astimezone(anchor.tzinfo)
            shift = anchor.replace(tzinfo=None) - previous.replace(tzinfo=None)
            functional = schedule_fields(starts, all_day=all_day)
            values.update(functional)
            if source.schedule_expression:
                # The old date-only authoring language is display-only on load.
                # An authoring edit migrates its canonical dates to today's
                # language rather than writing another legacy expression.
                legacy = source.schedule_syntax != 2
                expression, expression_anchor = shift_schedule_expression(
                    self.expression if legacy else source.schedule_expression,
                    previous.date().isoformat() if legacy else source.expression_anchor or previous.date().isoformat(),
                    shift, starts, syntax=2 if legacy else source.schedule_syntax)
                values.update(schedule_expression=expression, expression_anchor=expression_anchor,
                              schedule_syntax=2)
            return values
        values = dict(recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                      schedule_expression='', expression_anchor='', schedule_syntax=2,
                      timezone_id=getattr(anchor.tzinfo, 'key', ''))
        if self.kind == 'once':
            return values
        if self._expression_mode or self.kind == 'custom':
            starts = self.starts(start, all_day=all_day)
            if all_day and any(value.time() != time.min for value in starts):
                raise RecurrenceError('Time entries require a timed event.')
            values.update(schedule_fields(starts, all_day=all_day))
            values.update(schedule_expression=self.expression, expression_anchor=anchor.date().isoformat())
            return values
        values['recurrence_rule'] = self.advanced_rule if self.kind == 'advanced' else self.regular().rule(anchor, all_day=all_day)
        # Rule edits preserve additional dates, exclusions and detached events.
        if source is not None:
            for name in ('recurrence_dates', 'excluded_dates', 'recurrence_overrides'):
                values[name] = deepcopy(getattr(source, name))
        return values

    def _project_edited(self, start, *, end=None, all_day=False):
        master = self._edited_master
        reference_start = self._edited_reference_start or master.start
        reference_end = self._edited_reference_end or master.end
        canonical_start, canonical_end = self._range_from_reference(master, start, end, reference_start,
                                                                     reference_end, all_day=all_day)
        if (canonical_start == master.start and canonical_end == master.end
                and all_day == master.all_day):
            return master
        edited = replace(master, start=canonical_start, end=canonical_end, all_day=all_day)
        return updated_series(master, master, edited)

    def _range_from_reference(self, master, start, end, reference_start, reference_end, *, all_day=False):
        zone = self.author_anchor(start, all_day=all_day).tzinfo

        def wall(value):
            return value.replace(tzinfo=None) if all_day else value.astimezone(zone).replace(tzinfo=None)

        shift = wall(start) - wall(reference_start)
        canonical_start = (wall(master.start) + shift).replace(tzinfo=zone, fold=master.start.fold)
        duration = wall(master.end) - wall(master.start)
        if end is not None:
            chosen_duration = wall(end) - wall(start)
            if chosen_duration != wall(reference_end) - wall(reference_start):
                duration = chosen_duration
        canonical_end = canonical_start + duration
        return canonical_start, canonical_end

    def primary_range(self, start, end, *, all_day=False):
        """Canonical range for a graphical draft's possibly detached UI primary."""
        if self._edited_master is None:
            if self.configuration_changed:
                return self._source_range(start, end, all_day=all_day)
            return start, end
        effective = self._project_edited(start, end=end, all_day=all_day)
        return effective.start, effective.end

    def _adopt_graphical(self, master, *, reference_start=None, reference_end=None):
        """Materialize small finite schedules; retain rules for large/unbounded ones."""
        try:
            values = base_schedule_starts(master, limit=DEFAULT_LIMIT)
            zone = self.authoring_zone or event_zone(master)
            values = [datetime.combine(value.date(), time.min, zone) if master.all_day else value.astimezone(zone)
                      for value in values]
            # A materialized custom sequence represents the active base starts.
            # Detached exceptions instead retain their nominal canonical starts
            # so editing their range never changes a RECURRENCE-ID.
            if not master.recurrence_overrides:
                values = [value for value in values if recurrence_key(value, master.all_day) not in master.excluded_dates]
            primary_start = (datetime.combine(master.start.date(), time.min, zone) if master.all_day
                             else master.start.astimezone(zone))
            if not values or values[0] != primary_start:
                values = [primary_start, *[value for value in values if value != primary_start]]
            self.set_starts(values, all_day=master.all_day)
            # Do not ask starts() to recover these values: a final removal can
            # produce exactly the initial blank expression, which deliberately
            # still refers to the untouched source until this edit is adopted.
            fields = dict(schedule_fields(values, all_day=master.all_day),
                          excluded_dates=master.excluded_dates,
                          recurrence_overrides=deepcopy(master.recurrence_overrides),
                          schedule_expression=self.expression,
                          expression_anchor=primary_start.date().isoformat(), schedule_syntax=2,
                          timezone_id=getattr(zone, 'key', ''))
            if master.recurrence_overrides:
                fields.update({name: deepcopy(getattr(master, name)) for name in
                               ('recurrence_rule', 'recurrence_dates', 'timezone_id')})
                # A range exception leaves the author's nominal sequence intact.
                if master.schedule_expression and master.schedule_syntax == 2:
                    try:
                        if _same(expand_sequence(master.schedule_expression, primary_start), values, master.all_day):
                            self.expression = fields['schedule_expression'] = master.schedule_expression
                    except PatternError:
                        pass
                if master.recurrence_rule and not master.recurrence_dates:
                    regular = RegularSchedule.from_rule(master.recurrence_rule, primary_start, all_day=master.all_day)
                    if regular is not None:
                        self._take_regular(regular)
        except (RecurrenceError, PatternError):
            fields = {name: deepcopy(getattr(master, name)) for name in _SCHEDULE_FIELDS}
            self.kind = 'advanced' if master.recurrence_rule else 'custom'
            self.advanced_rule = master.recurrence_rule
            self._expression_mode = False
            self.expression = ''
            fields.update(schedule_expression='', expression_anchor='', schedule_syntax=2)
        self._edited_fields = fields
        self._edited_master = replace(master, **fields)
        self._edited_reference_start = reference_start or master.start
        self._edited_reference_end = reference_end or master.end

    def toggle_date(self, master: Event, day: date, *, display_zone=None,
                    reference_start=None, reference_end=None):
        """Toggle a date without expanding unbounded history into a local list."""
        zone = self.authoring_zone or event_zone(master)
        displayed = event_zone(master) if master.all_day else display_zone or zone
        day_start = datetime.combine(day, time.min, displayed)
        day_end = datetime.combine(day + timedelta(days=1), time.min, displayed)
        instances = expand_event(master, day_start, day_end, limit=1000)
        active = [item for item in instances if (item.start.date() if master.all_day else
                                                item.start.astimezone(displayed).date()) == day]
        exclusions = set(master.excluded_dates)
        rdates = set(master.recurrence_dates)
        overrides = deepcopy(master.recurrence_overrides)
        primary = recurrence_key(master.start, master.all_day)
        if active:
            for item in active:
                key = item.recurrence_id or recurrence_key(item.start, master.all_day)
                if key == primary:
                    continue
                exclusions.add(key)
                overrides.pop(key, None)
        else:
            restored = {key for key in exclusions if (key_datetime(key, master).date() if master.all_day else
                                                       key_datetime(key, master).astimezone(displayed).date()) == day}
            if restored:
                exclusions -= restored
                # Finite text authoring may have omitted an excluded nominal
                # start. Retain the identity as an explicit date on restoration.
                rdates.update(restored)
            elif day != (master.start.date() if master.all_day else master.start.astimezone(displayed).date()):
                start = datetime.combine(day, master.start.astimezone(zone).timetz())
                if not master.all_day:
                    start += timedelta(days=(day - start.astimezone(displayed).date()).days)
                if not master.all_day:
                    _checked(start)
                rdates.add(recurrence_key(start, master.all_day))
        changed = replace(master, recurrence_dates=tuple(rdates), excluded_dates=tuple(exclusions), recurrence_overrides=overrides)
        if changed == master:
            return master
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master

    def remove_occurrence(self, master: Event, instance: Event, *, reference_start=None, reference_end=None):
        key = instance.recurrence_id or recurrence_key(instance.start, master.all_day)
        if key == recurrence_key(master.start, master.all_day):
            return master
        changed = replace(master, excluded_dates=tuple(set(master.excluded_dates) | {key}),
                          recurrence_overrides={name: value for name, value in master.recurrence_overrides.items() if name != key})
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master

    def add_occurrence(self, master: Event, start: datetime, end: datetime | None = None,
                       *, reference_start=None, reference_end=None):
        if not master.all_day:
            _checked(start)
        key = recurrence_key(start, master.all_day)
        overrides = deepcopy(master.recurrence_overrides)
        if end is not None:
            _checked(end)
            if end.timestamp() <= start.timestamp():
                raise RecurrenceError('The end must be after the start.')
            default = occurrence(master, start, key)
            if end != default.end:
                overrides[key] = replace(default, start=start, end=end).to_dict()
        changed = replace(master, recurrence_dates=tuple(set(master.recurrence_dates) | {key}),
                          excluded_dates=tuple(value for value in master.excluded_dates if value != key),
                          recurrence_overrides=overrides)
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master

    def edit_occurrence(self, master: Event, instance: Event, start: datetime, end: datetime,
                        *, reference_start=None, reference_end=None):
        """Move/resize an occurrence while keeping its original RECURRENCE-ID.

        A detached exception changes its displayed range, not its place in the
        standard recurrence. In particular this does not create an EXDATE plus
        a new RDATE, or replace an existing exception's nominal identity.
        """
        _checked(start)
        _checked(end)
        if end.timestamp() <= start.timestamp():
            raise RecurrenceError('The end must be after the start.')
        key = instance.recurrence_id or recurrence_key(instance.start, master.all_day)
        if key == recurrence_key(master.start, master.all_day):
            raise RecurrenceError('Change the primary event using its start and end fields.')
        if start == instance.start and end == instance.end:
            return master
        original = occurrence(master, key_datetime(key, master), key)
        overrides = deepcopy(master.recurrence_overrides)
        overrides[key] = replace(original, start=start, end=end).to_dict()
        changed = replace(master, recurrence_overrides=overrides,
                          excluded_dates=tuple(value for value in master.excluded_dates if value != key))
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master


# Explicit aliases make the ending-control use clear at call sites.
recurrence_end_for_count = count_to_until
recurrence_count_until = until_to_count
