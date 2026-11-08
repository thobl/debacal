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
from .patterns import (DEFAULT_LIMIT, PatternError, compile_sequence, expand_sequence,
                       reconstruct_sequence, sequence_tail, shift_schedule_expression)
from .recurrence import (
    RecurrenceError, base_schedule_starts, event_zone, expand_event,
    key_datetime, occurrence, recurrence_key, recurrence_set, schedule_fields,
    updated_series,
)
from .schedule_authoring import expression_event, expression_matches, reconstruct_authoring


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
            authored = (bool(source.schedule_expression) or bool(source.recurrence_dates and not source.recurrence_rule)
                        or bool(regular and (source.excluded_dates or source.recurrence_dates)))
            if authored:
                try:
                    view = reconstruct_authoring(source, authoring_zone=zone)
                    draft._take_authoring(view, source)
                except (PatternError, RecurrenceError, ValueError, OverflowError):
                    # Unsupported or malformed annotations are display-only.
                    # Canonical fields remain untouched until an explicit edit.
                    draft.kind = 'advanced' if source.recurring else 'once'
            elif regular is not None:
                draft._take_regular(regular)
            elif source.recurrence_rule:
                draft.kind = 'advanced'
            draft.advanced_rule = source.recurrence_rule
        draft.initial_expression = draft.expression
        draft._loaded_expression_mode = draft._expression_mode
        draft._loaded_config = draft._config()
        return draft

    def _take_authoring(self, view, master, *, force_custom=False):
        """Install a certified view; exception handling has already succeeded."""
        self.kind, self.expression, self._expression_mode = 'custom', view.expression, True
        self._materialized = list(view.starts) if view.starts is not None else None
        anchor = self.author_anchor(master.start, all_day=master.all_day)
        evaluated = expression_event(master, view.expression, authoring_zone=anchor.tzinfo)
        regular = (RegularSchedule.from_rule(evaluated.recurrence_rule, anchor, all_day=master.all_day)
                   if not evaluated.recurrence_dates and not evaluated.excluded_dates else None)
        if regular is not None and not force_custom:
            self._take_regular(regular)
        return regular

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
        staged = replace(self)
        if staged._edited_master is not None:
            # Editing the rule after a graphical exception must retain that
            # exception, including newly chosen occurrence-specific ranges.
            staged.source = staged._edited_master
            staged._primary_reference_start = staged._edited_reference_start
            staged._primary_reference_end = staged._edited_reference_end
        if staged.source is not None and staged._expression_mode and staged.kind in REGULAR_KINDS:
            # An auto-detected regular view describes the complete active base.
            # EXDATE/RDATE left over from its former canonical representation
            # must not start affecting a newly chosen interval/count again.
            source = staged.source
            canonical = replace(source, recurrence_rule=regular.rule(source.start, all_day=source.all_day),
                                recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
            rules = recurrence_set(canonical)
            overrides = {}
            for key, payload in source.recurrence_overrides.items():
                if key in source.excluded_dates:
                    continue
                when = key_datetime(key, source)
                previous = rules.before(when, inc=True)
                if previous is not None and recurrence_key(previous, source.all_day) == recurrence_key(when, source.all_day):
                    overrides[key] = payload
            staged.source = replace(source, recurrence_dates=(), excluded_dates=(), recurrence_overrides=overrides)
        staged._take_regular(regular)
        staged.expression = ''
        staged._expression_mode = False
        staged._materialized = None
        staged._edited_fields = None
        staged._edited_master = None
        self.__dict__.update(staged.__dict__)

    def use_sequence(self, master, *, reference_start=None, reference_end=None):
        """Change the authoring view while retaining canonical exceptions."""
        view = reconstruct_authoring(master, authoring_zone=self.authoring_zone)
        staged = replace(self)
        staged._take_authoring(view, master, force_custom=True)
        if view.starts is not None and not master.recurrence_overrides:
            zone = staged.author_anchor(master.start, all_day=master.all_day).tzinfo
            master = replace(master, **schedule_fields(view.starts, all_day=master.all_day),
                             timezone_id=getattr(zone, 'key', ''))
        staged._store_graphical(master, reference_start=reference_start, reference_end=reference_end)
        self.__dict__.update(staged.__dict__)

    def set_expression(self, text, start, *, all_day=False, detect=True, _canonical=False):
        staged = replace(self)
        if staged._edited_master is not None and not _canonical:
            staged.source = staged._edited_master
            staged._primary_reference_start = staged._edited_reference_start
            staged._primary_reference_end = staged._edited_reference_end
        anchor = staged.author_anchor(start if _canonical else staged._source_range(start, all_day=all_day)[0], all_day=all_day)
        compiled = compile_sequence(text, anchor, all_day=all_day)
        values = compiled.prefix_starts if compiled else expand_sequence(text, anchor)
        if all_day and any(value.time() != time.min for value in values):
            raise RecurrenceError('Time entries require a timed event.')
        if compiled:
            regular = (RegularSchedule.from_rule(compiled.recurrence_rule, anchor, all_day=all_day)
                       if detect and not compiled.recurrence_dates and not compiled.excluded_dates else None)
        else:
            regular = detect_regular_schedule(values, all_day=all_day) if detect else None
        staged.kind = 'custom'
        if regular:
            staged._take_regular(regular)
        staged.expression = text
        staged._expression_mode = True
        staged._materialized = None
        staged._edited_fields = None
        staged._edited_master = None
        source = staged.source
        if source is not None:
            source_anchor = staged.author_anchor(source.start, all_day=all_day)
            same_primary = recurrence_key(anchor, all_day) == recurrence_key(source_anchor, all_day)
            if same_primary and all_day == source.all_day and expression_matches(source, text, authoring_zone=anchor.tzinfo):
                # Re-reading text, including whitespace edits, cannot erase
                # occurrence-specific ranges/content or canonical identity.
                staged._store_graphical(source,
                    reference_start=staged._primary_reference_start or start,
                    reference_end=staged._primary_reference_end or source.end)
            elif source.recurrence_overrides:
                functional = compiled.fields if compiled else schedule_fields(values, all_day=all_day)
                duration = source.end.astimezone(anchor.tzinfo).replace(tzinfo=None) - source.start.astimezone(anchor.tzinfo).replace(tzinfo=None)
                candidate = replace(source, start=anchor, end=anchor + duration, all_day=all_day,
                    recurrence_rule=functional['recurrence_rule'], recurrence_dates=functional['recurrence_dates'],
                    excluded_dates=functional.get('excluded_dates', ()), recurrence_overrides={},
                    timezone_id=getattr(anchor.tzinfo, 'key', ''))
                rules = recurrence_set(candidate)
                shift = anchor.replace(tzinfo=None) - source_anchor.replace(tzinfo=None)
                overrides = {}
                for key, payload in source.recurrence_overrides.items():
                    nominal = key_datetime(key, source)
                    nominal = (datetime.combine(nominal.date(), time.min, anchor.tzinfo) if all_day
                               else nominal.astimezone(anchor.tzinfo))
                    moved = (nominal.replace(tzinfo=None) + shift).replace(tzinfo=anchor.tzinfo, fold=nominal.fold)
                    member = rules.before(moved, inc=True)
                    moved_key = recurrence_key(moved, all_day)
                    if member is None or recurrence_key(member, all_day) != moved_key:
                        continue
                    override = Event.from_dict(payload)
                    def shifted(value):
                        local = datetime.combine(value.date(), time.min, anchor.tzinfo) if all_day else value.astimezone(anchor.tzinfo)
                        return (local.replace(tzinfo=None) + shift).replace(tzinfo=anchor.tzinfo, fold=local.fold)
                    overrides[moved_key] = replace(override, start=shifted(override.start), end=shifted(override.end),
                                                   recurrence_id=moved_key, all_day=all_day).to_dict()
                candidate = replace(candidate, recurrence_overrides=overrides)
                staged._store_graphical(candidate, reference_start=start,
                    reference_end=start + ((staged._primary_reference_end - staged._primary_reference_start)
                                           if staged._primary_reference_start and staged._primary_reference_end else duration))
        self.__dict__.update(staged.__dict__)
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

    def validation_starts(self, start, *, all_day=False):
        """Validate the finite authored prefix without enumerating a tail."""
        if self.expression and sequence_tail(self.expression):
            fields = self.fields(start, all_day=all_day)
            compiled = compile_sequence(fields['schedule_expression'], self.schedule_anchor(start, all_day=all_day), all_day=all_day)
            return list(compiled.prefix_starts)
        return self.starts(start, all_day=all_day)

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
                if (same_anchor and source.schedule_syntax == 2 and self.expression != source.schedule_expression
                        and expression_matches(source, self.expression, authoring_zone=anchor.tzinfo)):
                    values.update(schedule_expression=self.expression, expression_anchor=source.start.date().isoformat())
                return values
            if self.expression and sequence_tail(self.expression):
                # Only the certified editor view may drive authoring moves.
                # A remote client may have left stale proprietary metadata on
                # the source while changing its canonical weekly/daily rule.
                if not expression_matches(source, self.expression, authoring_zone=anchor.tzinfo):
                    raise RecurrenceError('The custom definition no longer matches its canonical schedule.')
                certified = replace(source, schedule_expression=self.expression,
                                    expression_anchor=source.start.date().isoformat(), schedule_syntax=2)
                duration = ((source.end.replace(tzinfo=None) - source.start.replace(tzinfo=None)) if all_day
                            else source.end.astimezone(anchor.tzinfo).replace(tzinfo=None)
                                 - source.start.astimezone(anchor.tzinfo).replace(tzinfo=None))
                edited = replace(certified, start=anchor, end=anchor + duration)
                projected = updated_series(certified, certified, edited)
                return {name: deepcopy(getattr(projected, name)) for name in _SCHEDULE_FIELDS}
            starts = self.starts(start, all_day=all_day)
            previous = source.start.replace(tzinfo=anchor.tzinfo) if all_day else source.start.astimezone(anchor.tzinfo)
            shift = anchor.replace(tzinfo=None) - previous.replace(tzinfo=None)
            functional = schedule_fields(starts, all_day=all_day)
            values.update(functional)
            if source.schedule_expression:
                # The old date-only authoring language is display-only on load.
                # An authoring edit migrates its canonical dates to today's
                # language rather than writing another legacy expression.
                legacy = source.schedule_syntax != 2 or not expression_matches(source, source.schedule_expression,
                                                                                 authoring_zone=anchor.tzinfo)
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
            compiled = compile_sequence(self.expression, anchor, all_day=all_day)
            if compiled:
                values.update(compiled.fields)
                values.update(schedule_expression=self.expression, expression_anchor=anchor.date().isoformat())
                return values
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
        if all_day:
            # All-day dates float: a picker in another display zone must not
            # turn the source's midnight into an earlier/later calendar day.
            canonical_start = datetime.combine(canonical_start.date(), time.min, event_zone(master))
            canonical_end = datetime.combine(canonical_end.date(), time.min, event_zone(master))
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
        """Adopt one canonical edit only after constructing its complete view.

        A failed conversion cannot leak a partially detected daily/weekly mode
        into an unrelated imported rule. Graphical edits to unsupported rules
        remain valid canonical edits, with a preserved, non-text authoring view.
        """
        staged = replace(self)
        try:
            view = reconstruct_authoring(master, authoring_zone=self.authoring_zone)
            staged._take_authoring(view, master)
            if view.starts is not None and not master.recurrence_overrides:
                zone = staged.author_anchor(master.start, all_day=master.all_day).tzinfo
                master = replace(master, **schedule_fields(view.starts, all_day=master.all_day),
                                 timezone_id=getattr(zone, 'key', ''))
            staged._store_graphical(master, reference_start=reference_start, reference_end=reference_end)
        except (RecurrenceError, PatternError, ValueError, OverflowError):
            staged.kind, staged.expression, staged._expression_mode = 'advanced', '', False
            staged.advanced_rule = master.recurrence_rule
            staged._materialized = None
            master = replace(master, schedule_expression='', expression_anchor='', schedule_syntax=2)
            staged._store_graphical(master, reference_start=reference_start, reference_end=reference_end)
        self.__dict__.update(staged.__dict__)

    def _store_graphical(self, master, *, reference_start=None, reference_end=None):
        fields = {name: deepcopy(getattr(master, name)) for name in _SCHEDULE_FIELDS}
        anchor = self.author_anchor(master.start, all_day=master.all_day)
        fields.update(schedule_expression=self.expression,
                      expression_anchor=anchor.date().isoformat() if self._expression_mode else '', schedule_syntax=2)
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
