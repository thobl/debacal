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
from .event_duration import add_duration, add_elapsed, elapsed_duration, end_for, same_duration
from .coincident_occurrences import apply_occurrence_counts, multiplicities
from .patterns import (DEFAULT_LIMIT, compile_sequence, expand_sequence,
                       is_relative_sequence, reconstruct_sequence, sequence_tail)
from .recurrence import (
    RecurrenceError, base_schedule_starts, canonicalize_authored_primary, event_zone, expand_event,
    key_datetime, occurrence, recurrence_key, recurrence_set, schedule_fields,
    updated_series,
)
from .regular_schedule import (
    MAX_REPEAT_COUNT, REGULAR_KINDS, RegularSchedule, _aware, _parts,
    count_to_until, retain_relative_intent, until_to_count,
)
from .schedule_authoring import (canonical_starts, displayed_schedule, expression_event, expression_matches,
                                 reconstruct_authoring, retain_displayed_overrides)
from .sequence_document import document_expression, is_sequence_document, parse_document


_SCHEDULE_FIELDS = ('recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides', 'range_policy',
                    'schedule_expression', 'expression_anchor', 'schedule_syntax', 'timezone_id', 'duration_spec')


def _checked(value):
    _aware(value)
    if value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise RecurrenceError('This occurrence falls in a daylight-saving clock change. Choose another time.')
    return value


def _range_end(start, reference_start, reference_end, *, all_day=False, duration_spec='', duration_zone=None):
    """Move a range using floating days, nominal duration, or exact elapsed time."""
    if all_day:
        return start + (reference_end.replace(tzinfo=None) - reference_start.replace(tzinfo=None))
    if duration_spec:
        zone = duration_zone or reference_start.tzinfo
        return add_duration(start.astimezone(zone), duration_spec).astimezone(start.tzinfo)
    return add_elapsed(start, elapsed_duration(reference_start, reference_end))


def detect_regular_schedule(starts: Iterable[datetime], *, all_day=False) -> RegularSchedule | None:
    """Classify only exact finite calendar-unit schedules, never clock offsets."""
    values = list(starts)
    if len(values) < 2:
        return None
    fields = schedule_fields(values, all_day=all_day)
    if not fields['recurrence_rule']:
        return None
    return RegularSchedule.from_rule(fields['recurrence_rule'], values[0], all_day=all_day)


def _range_regular(tail, anchor):
    """Controls count fitted leading dates as well as native rule dates."""
    if tail is None or not tail.overflow or tail.prefix:
        return None
    ending = 'count' if tail.count is not None else 'until' if tail.until is not None else 'never'
    options = dict(overflow=tail.overflow, range_span=tail.range_span, preferred_day=tail.preferred_day)
    count, until = tail.count, tail.until
    if count is not None:
        until = count_to_until(anchor, tail.frequency, tail.interval, count, **options)
    elif until is not None:
        count = until_to_count(anchor, tail.frequency, tail.interval, until, **options)
    return RegularSchedule(tail.frequency, tail.interval, count, until, ending=ending,
                           preferred_day=tail.preferred_day, overflow=tail.overflow, range_span=tail.range_span)


def _editable_regular(regular, anchor):
    """Use the compact text when basic controls cannot retain a rule clause."""
    if regular is None:
        return None
    if (getattr(regular, 'selectors', ())
            or getattr(regular, 'week_start', 'MO') != 'MO'
            or getattr(regular, 'preferred_day', None) not in (None, anchor.day)):
        return None
    return regular


@dataclass
class ScheduleDraft:
    """Small mutable authoring draft with explicit schedule-changing methods.

    Source values are copied exactly for ordinary edits. Relative programs
    reevaluate from a moved primary; explicit date sets retain their translated
    dates. Newly typed text always evaluates from the chosen primary timestamp.
    """
    source: Event | None = None
    authoring_zone: object = None
    kind: str = 'once'
    interval: int = 1
    count: int | None = 6
    until: date | None = None
    ending: str = 'count'
    weekdays: tuple[int, ...] = ()
    clamp: bool = False
    overflow: str = ''
    range_span: int = 1
    expression: str = '+0d'
    initial_expression: str = ''
    authoring_error: str = ''
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
            view = reconstruct_authoring(source, authoring_zone=zone)
            draft._take_authoring(view, source)
            if (not source.recurrence_rule and not source.recurrence_dates
                    and not source.excluded_dates and not source.recurrence_overrides
                    and view.expression == '+0d'):
                draft.kind = 'once'
        draft.initial_expression = draft.expression
        draft._loaded_expression_mode = draft._expression_mode
        draft._loaded_config = draft._config()
        return draft

    def _take_authoring(self, view, master, *, force_custom=False):
        """Install a certified view; exception handling has already succeeded."""
        self.kind, self.expression, self._expression_mode = 'custom', view.expression, True
        self.authoring_error = ''
        self._materialized = list(view.starts) if view.starts is not None else None
        tail = sequence_tail(view.expression) if not view.structured else None
        self.overflow = tail.overflow if tail else ''
        self.range_span = tail.range_span if tail else 1
        anchor = self.author_anchor(master.start, all_day=master.all_day)
        evaluated = expression_event(master, view.expression, authoring_zone=anchor.tzinfo)
        regular = (RegularSchedule.from_rule(evaluated.recurrence_rule, anchor, all_day=master.all_day)
                   if not view.structured and not evaluated.recurrence_dates and not evaluated.excluded_dates else None)
        regular = _range_regular(tail, anchor) or regular
        if not view.structured:
            regular = retain_relative_intent(view.expression, anchor, regular)
        regular = _editable_regular(regular, anchor)
        if regular is not None:
            tail = sequence_tail(view.expression)
            if tail and tail.overflow:
                regular = replace(regular, overflow=tail.overflow, range_span=tail.range_span)
        if regular is not None and not force_custom:
            self._take_regular(regular)
        return regular

    def _config(self):
        return (self.kind, self.interval, self.count, self.until, self.ending, tuple(self.weekdays),
                self.clamp, self.overflow, self.range_span, self.expression, self._expression_mode)

    @property
    def configuration_changed(self):
        return self._edited_fields is not None or self._config() != self._loaded_config

    @property
    def dirty(self):
        return self.configuration_changed

    def _take_regular(self, regular):
        self.kind, self.interval = regular.kind, regular.interval
        self.count, self.until, self.weekdays, self.ending = regular.count, regular.until, regular.weekdays, regular.ending
        self.clamp = regular.clamp
        self.overflow, self.range_span = regular.overflow, regular.range_span

    def set_regular(self, kind, interval=1, count=6, until=None, weekdays=(), ending='count', clamp=False,
                    range_policy=None, range_span=None):
        kind = str(kind).upper()
        if range_span is None:
            range_span = self.range_span if range_policy is not None and kind == self.kind and self.overflow else 1
        regular = RegularSchedule(kind, interval, count, until, tuple(weekdays), ending,
                                  bool(clamp and kind in ('MONTHLY', 'YEARLY')),
                                  overflow=(range_policy or '') if kind in ('MONTHLY', 'YEARLY') else '',
                                  range_span=range_span if kind in ('MONTHLY', 'YEARLY') else 1)
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
        from .patterns import expression_for_regular
        staged.expression = expression_for_regular(regular.kind, regular.interval,
            count=regular.count if regular.ending == 'count' else None,
            until=regular.until if regular.ending == 'until' else None, clamp=regular.clamp,
            weekdays=regular.weekdays, overflow=regular.overflow, range_span=regular.range_span)
        staged.authoring_error = ''
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
        # Explicit conversion adopts the certified program while keeping its
        # actual starts and exception identities. A simple calendar recurrence
        # uses bounded every(), so monthly skipping stays a standard rule;
        # unsupported finite BYxxx rules become an editable exact date set.
        # A certified view changes authoring mode, not the underlying calendar
        # or nominal identities. Preserve even redundant standard properties.
        staged._store_graphical(master, reference_start=reference_start, reference_end=reference_end)
        self.__dict__.update(staged.__dict__)

    def set_expression(self, text, start, *, all_day=False, detect=True, _canonical=False, prototype=None):
        staged = replace(self)
        if staged._edited_master is not None and not _canonical:
            staged.source = staged._edited_master
            staged._primary_reference_start = staged._edited_reference_start
            staged._primary_reference_end = staged._edited_reference_end
        anchor = staged.author_anchor(start if _canonical else staged._source_range(start, all_day=all_day)[0], all_day=all_day)
        structured = is_sequence_document(text)
        compiled = None if structured else compile_sequence(text, anchor, all_day=all_day)
        if compiled is not None and all_day and any(value.time() != time.min for value in compiled.prefix_starts):
            raise RecurrenceError('Time entries require a timed event.')
        regular = (RegularSchedule.from_rule(compiled.recurrence_rule, anchor, all_day=all_day)
                   if compiled is not None and detect and not compiled.recurrence_dates
                   and not compiled.excluded_dates and not compiled.duplicate_starts else None)
        tail = sequence_tail(text) if not structured else None
        if detect:
            regular = _range_regular(tail, anchor) or regular
        if not structured:
            regular = retain_relative_intent(text, anchor, regular)
        regular = _editable_regular(regular, anchor)
        staged.kind = 'custom'
        if compiled is not None:
            tail = sequence_tail(text)
            staged.overflow = tail.overflow if tail else ''
            staged.range_span = tail.range_span if tail else 1
            if regular is not None and tail and tail.overflow:
                regular = replace(regular, overflow=tail.overflow, range_span=tail.range_span)
        if regular:
            staged._take_regular(regular)
        staged.expression = text
        staged.authoring_error = ''
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
            elif structured or source.recurrence_overrides:
                if prototype is not None:
                    _, canonical_end = staged._source_range(start, prototype.end,
                        all_day=all_day, duration_spec=prototype.duration_spec)
                else:
                    canonical_end = end_for(source, anchor)
                reference = replace(source, start=anchor, end=canonical_end, all_day=all_day,
                                    duration_spec=prototype.duration_spec if prototype else source.duration_spec,
                                    timezone_id=getattr(anchor.tzinfo, 'key', ''))
                reference = staged._document_prototype(reference, text) if structured else reference
                candidate = expression_event(reference, text, authoring_zone=anchor.tzinfo)
                if not structured:
                    shift = anchor.replace(tzinfo=None) - source_anchor.replace(tzinfo=None)
                    candidate = retain_displayed_overrides(candidate, source, shift=shift)
                staged._store_graphical(candidate, reference_start=start,
                    reference_end=end_for(candidate, start) if structured else end_for(replace(source,
                        start=staged._primary_reference_start or source.start,
                        end=staged._primary_reference_end or source.end), start))
        elif structured:
            # There is no existing payload to inherit for a new event. The
            # real dialog range replaces this temporary prototype on saving.
            reference = prototype or Event('draft', 'draft', 'Draft', anchor,
                anchor + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day=all_day)
            reference = staged._document_prototype(reference, text)
            candidate = expression_event(reference, text)
            staged._store_graphical(candidate, reference_start=start, reference_end=end_for(candidate, start))
        self.__dict__.update(staged.__dict__)
        return regular

    @staticmethod
    def _document_prototype(prototype, text):
        """A newly authored nominal duration changes the whole reference range."""
        spec = parse_document(text).get('duration_spec', '')
        if spec and not prototype.all_day:
            zone = event_zone(prototype)
            end = add_duration(prototype.start.astimezone(zone), spec).astimezone(prototype.end.tzinfo)
            return replace(prototype, end=end, duration_spec=spec)
        return replace(prototype, duration_spec=spec)

    def document_duration(self, start):
        """Range state to adopt after successfully entering a complete document.

        Return nothing for compact programs: those do not edit duration. Keeping
        this with the staged master lets the UI commit text and range together.
        """
        master = self._edited_master
        if master is not None and is_sequence_document(self.expression):
            return end_for(master, start), master.duration_spec, event_zone(master)
        return None

    def commit_reference_range(self, start, end, *, all_day=False, duration_spec=''):
        """Commit controls back to a complete document as one canonical edit.

        A document owns duration as well as recurrence fields. Leaving its text
        stale after a control edit would replay the previous duration when the
        sequence field is revisited. Pending text is handled by the caller;
        compact programs do not themselves specify a primary duration.
        """
        if not is_sequence_document(self.expression):
            return False
        staged = replace(self)
        if staged._edited_master is None:
            if staged.source is None:
                return False
            staged._edited_master = staged.source
            staged._edited_reference_start = staged._primary_reference_start or staged.source.start
            staged._edited_reference_end = staged._primary_reference_end or staged.source.end
        master = staged._project_edited(start, end=end, all_day=all_day, duration_spec=duration_spec)
        if master is staged._edited_master:
            return False
        staged.expression = document_expression(master)
        staged._store_graphical(master, reference_start=start, reference_end=end)
        self.__dict__.update(staged.__dict__)
        return True

    def set_starts(self, starts, *, all_day=False, anchor=None):
        values = list(starts)
        if anchor is None:
            current = self._edited_master or self.source
            anchor = current.start if current else (values[0] if values else self._primary_reference_start)
        if anchor is None:
            raise RecurrenceError('Choose a reference date before creating an empty sequence.')
        text = reconstruct_sequence(values, anchor=anchor, all_day=all_day)
        return self.set_expression(text, anchor, all_day=all_day, _canonical=True)

    def bind_primary_reference(self, start, end):
        """Bind the displayed prototype to its source's nominal recurrence root.

        Following edits may start from a moved/resized detached occurrence.
        Its displayed range is a reference for user changes, not a new DTSTART.
        """
        self._primary_reference_start, self._primary_reference_end = start, end

    def _source_range(self, start, end=None, *, all_day=False, duration_spec=None):
        if self.source is None or self._primary_reference_start is None:
            return start, end
        return self._range_from_reference(self.source, start, end, self._primary_reference_start,
                                          self._primary_reference_end, all_day=all_day, duration_spec=duration_spec)

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
            return list(canonical_starts(effective, authoring_zone=self.authoring_zone, limit=limit))
        anchor = self.author_anchor(self._source_range(start, all_day=all_day)[0], all_day=all_day)
        if self.source is not None and not self.configuration_changed and self._loaded_expression_mode:
            if is_sequence_document(self.expression):
                fields = self.fields(start, all_day=all_day)
                return list(canonical_starts(replace(self.source, **fields), authoring_zone=self.authoring_zone, limit=limit))
            if is_relative_sequence(self.expression):
                return expand_sequence(self.expression, anchor, limit=limit, all_day=all_day)
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
        if self._expression_mode or self.kind == 'custom' or self.overflow:
            if is_sequence_document(self.expression):
                prototype = self.source or Event('draft', 'draft', 'Draft', anchor,
                    anchor + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day=all_day)
                return list(canonical_starts(expression_event(prototype, self.expression),
                                            authoring_zone=self.authoring_zone, limit=limit))
            return expand_sequence(self.expression, anchor, limit=limit, all_day=all_day)
        rule = self.regular().rule(anchor, all_day=all_day)
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
        if is_sequence_document(self.expression):
            # The document parser validates its explicit ranges directly.
            # Imported ongoing rules need no expansion just to accept a dialog.
            self.fields(start, all_day=all_day)
            return [self.schedule_anchor(start, all_day=all_day)]
        if self.expression and sequence_tail(self.expression):
            fields = self.fields(start, all_day=all_day)
            if is_sequence_document(fields['schedule_expression']):
                return [self.schedule_anchor(start, all_day=all_day)]
            compiled = compile_sequence(fields['schedule_expression'], self.schedule_anchor(start, all_day=all_day), all_day=all_day)
            values = [*compiled.prefix_starts, compiled.tail_start]
            return list({recurrence_key(value, all_day): value for value in values}.values())
        return self.starts(start, all_day=all_day)

    def regular(self):
        return RegularSchedule(self.kind, self.interval, self.count, self.until, tuple(self.weekdays), self.ending,
                               self.clamp, overflow=self.overflow, range_span=self.range_span)

    @property
    def range_policy(self):
        if self.overflow:
            return self.overflow
        tail = sequence_tail(self.expression) if self.expression and not is_sequence_document(self.expression) else None
        if tail and tail.overflow:
            return tail.overflow
        if self.source is not None:
            if self.source.range_policy == 'cut':
                return 'cut'
            from .range_policies import occupied_span, range_policy_options
            kind = self.kind if self.kind in REGULAR_KINDS else tail.frequency if tail else None
            if kind in ('MONTHLY', 'YEARLY') and occupied_span(self.source.start, self.source.end, all_day=self.source.all_day) > 1:
                _, fits_one_month = range_policy_options(self.source.start, self.source.end, kind,
                    self.interval, all_day=self.source.all_day)
                if fits_one_month:
                    return None  # Imported constant-duration ranges are not silently reinterpreted.
        if self.clamp or (tail and tail.clamp):
            return 'shift'
        if not is_sequence_document(self.expression):
            from .patterns import calendar_step_policy
            choice = calendar_step_policy(self.expression)
            if choice is not None:
                return choice
        if self.source is None and self.kind in ('MONTHLY', 'YEARLY'):
            return 'skip'
        if self.source is not None and self.kind in ('MONTHLY', 'YEARLY'):
            from .range_policies import occupied_span
            if occupied_span(self.source.start, self.source.end, all_day=self.source.all_day) == 1:
                return 'skip'
        return None

    def set_range_policy(self, policy, start, end, *, all_day=False):
        from .range_policies import occupied_span, range_policy_options
        from .patterns import expression_for_regular
        if policy not in ('skip', 'shift', 'cut'):
            raise RecurrenceError('Choose Skip, Shift or Cut.')
        start, end = self._source_range(start, end, all_day=all_day)
        tail = sequence_tail(self.expression) if self.expression and not is_sequence_document(self.expression) else None
        kind = self.kind if self.kind in ('MONTHLY', 'YEARLY') else tail.frequency if tail else None
        if kind not in ('MONTHLY', 'YEARLY'):
            if tail is None and policy in ('skip', 'shift') and not is_sequence_document(self.expression):
                if occupied_span(start, end, all_day=all_day) != 1:
                    raise RecurrenceError('Choose a regular calendar repeat to fit a multi-day range.')
                from .patterns import with_calendar_clamp
                self.set_expression(with_calendar_clamp(self.expression, policy == 'shift'), start, all_day=all_day)
                return
            raise RecurrenceError('Choose a monthly or yearly repeat before changing its range policy.')
        interval = self.interval if self.kind in REGULAR_KINDS else tail.interval
        actual_span = occupied_span(start, end, all_day=all_day)
        known_intent = actual_span if self.range_span > 1 and start.day + actual_span - 1 <= 31 else None
        _, allow_cut = range_policy_options(start, end, kind, interval, all_day=all_day,
            preferred_day=tail.preferred_day if tail else None, range_policy='cut' if policy == 'cut' else '',
            span=known_intent)
        if policy == 'cut' and not allow_cut:
            raise RecurrenceError('Cut applies to multi-day ranges contained in one calendar month.')
        span = occupied_span(start, end, all_day=all_day) if allow_cut else 1
        if self.kind in REGULAR_KINDS:
            self.set_regular(kind, interval, self.count, self.until, self.weekdays, self.ending,
                             range_policy=policy, range_span=span)
        else:
            expression = expression_for_regular(tail.frequency, tail.interval, count=tail.count, until=tail.until,
                weekdays=tail.weekdays, preferred_day=tail.preferred_day, week_start=tail.week_start,
                overflow=policy, range_span=span)
            self.set_expression((tail.prefix + ' ' + expression).strip(), start, all_day=all_day)

    def refresh_range_policy(self, start, end, *, all_day=False):
        if self.overflow and self.kind in ('MONTHLY', 'YEARLY'):
            start, end = self._source_range(start, end, all_day=all_day)
            from .range_policies import occupied_span, range_policy_options
            span = occupied_span(start, end, all_day=all_day)
            known_intent = span if self.range_span > 1 and start.day + span - 1 <= 31 else None
            _, allow_cut = range_policy_options(start, end, self.kind, self.interval, all_day=all_day,
                range_policy='cut' if self.overflow == 'cut' else '', span=known_intent)
            self.range_span = span if allow_cut else 1
            from .patterns import expression_for_regular
            self.expression = expression_for_regular(self.kind, self.interval,
                count=self.count if self.ending == 'count' else None,
                until=self.until if self.ending == 'until' else None,
                overflow=self.overflow, range_span=self.range_span)

    def fields(self, start, end=None, *, all_day=False, preserve_source=True, prototype=None):
        """Return Event recurrence/authoring fields, preserving untouched data."""
        chosen_spec = prototype.duration_spec if prototype is not None else None
        if self._edited_master is not None:
            effective = self._project_edited(start, end=end, all_day=all_day, duration_spec=chosen_spec)
            return {name: deepcopy(getattr(effective, name)) for name in _SCHEDULE_FIELDS}
        anchor = self.author_anchor(self._source_range(start, end, all_day=all_day, duration_spec=chosen_spec)[0], all_day=all_day)
        if self._edited_fields is not None:
            return deepcopy(self._edited_fields)
        source = self.source
        if preserve_source and source is not None and not self.configuration_changed and all_day == source.all_day:
            values = {name: deepcopy(getattr(source, name)) for name in _SCHEDULE_FIELDS}
            same_anchor = anchor.date() == source.start.date() if all_day else anchor == source.start
            if same_anchor:
                if source.schedule_expression:
                    # The editor may explain the same starts in its display
                    # timezone. Persist authoring text in the event's own zone,
                    # keeping a valid original literal exactly on a no-op.
                    expression = (source.schedule_expression if source.schedule_syntax == 2
                                  and expression_matches(source, source.schedule_expression)
                                  else reconstruct_authoring(source).expression)
                    values.update(schedule_expression=expression,
                                  expression_anchor=source.start.date().isoformat(), schedule_syntax=2)
                return values
            # One transport path handles text fields, graphical moves, and
            # detached reference ranges. Current relative instructions survive
            # reanchoring; canonical documents move their absolute fields.
            expression = reconstruct_authoring(source).expression
            certified = replace(source, schedule_expression=expression,
                                expression_anchor=source.start.date().isoformat(), schedule_syntax=2)
            projection_anchor = (datetime.combine(anchor.date(), time.min, event_zone(source))
                                 if all_day else anchor)
            edited = replace(certified, start=projection_anchor,
                end=_range_end(projection_anchor, source.start, source.end, all_day=all_day,
                    duration_spec=source.duration_spec if chosen_spec is None else chosen_spec,
                    duration_zone=event_zone(source)),
                duration_spec=source.duration_spec if chosen_spec is None else chosen_spec)
            projected = updated_series(certified, certified, edited)
            return {name: deepcopy(getattr(projected, name)) for name in _SCHEDULE_FIELDS}
        values = dict(recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={}, range_policy='',
                      schedule_expression='+0d', expression_anchor=anchor.date().isoformat(), schedule_syntax=2,
                      timezone_id=getattr(anchor.tzinfo, 'key', ''),
                      duration_spec=chosen_spec if chosen_spec is not None else source.duration_spec if source else '')
        if self.kind == 'once':
            values.update(schedule_expression='', expression_anchor='', schedule_syntax=1)
            return values
        if self._expression_mode or self.kind == 'custom' or self.overflow:
            prototype = source or prototype or Event('draft', 'draft', 'Draft', anchor,
                end or anchor + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day=all_day)
            prototype = replace(prototype, start=anchor,
                                end=end or end_for(prototype, anchor),
                                duration_spec=prototype.duration_spec if chosen_spec is None else chosen_spec,
                                all_day=all_day, timezone_id=getattr(anchor.tzinfo, 'key', ''))
            evaluated = expression_event(prototype, self.expression, authoring_zone=anchor.tzinfo)
            values.update({name: deepcopy(getattr(evaluated, name)) for name in (
                'recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides', 'range_policy', 'duration_spec')})
            values.update(schedule_expression=self.expression, expression_anchor=anchor.date().isoformat())
            return values
        values['recurrence_rule'] = self.regular().rule(anchor, all_day=all_day)
        values['range_policy'] = 'cut' if self.overflow == 'cut' else ''
        # Rule edits preserve additional dates, exclusions and detached events.
        if source is not None:
            for name in ('recurrence_dates', 'excluded_dates', 'recurrence_overrides'):
                values[name] = deepcopy(getattr(source, name))
        prototype = source or Event('draft', 'draft', 'Draft', anchor,
            end or anchor + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day=all_day)
        candidate = replace(prototype, start=anchor,
                            end=end or end_for(prototype, anchor),
                            all_day=all_day, **values)
        values['schedule_expression'] = reconstruct_authoring(candidate, authoring_zone=anchor.tzinfo,
                                                             prefer_literal=False).expression
        return values

    def _project_edited(self, start, *, end=None, all_day=False, duration_spec=None):
        master = self._edited_master
        reference_start = self._edited_reference_start or master.start
        reference_end = self._edited_reference_end or master.end
        canonical_start, canonical_end = self._range_from_reference(master, start, end, reference_start,
            reference_end, all_day=all_day, duration_spec=duration_spec)
        if all_day:
            # All-day dates float: a picker in another display zone must not
            # turn the source's midnight into an earlier/later calendar day.
            canonical_start = datetime.combine(canonical_start.date(), time.min, event_zone(master))
            canonical_end = datetime.combine(canonical_end.date(), time.min, event_zone(master))
        zone = event_zone(master)
        wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
        phase_edit = (not all_day and end is not None and wall(reference_start) == wall(master.start)
            and ((wall(start) == wall(reference_start) and start.timestamp() != reference_start.timestamp())
                 or (wall(end) == wall(reference_end) and end.timestamp() != reference_end.timestamp())))
        if (canonical_start.timestamp() == master.start.timestamp()
                and canonical_end.timestamp() == master.end.timestamp()
                and all_day == master.all_day and not phase_edit
                and (duration_spec is None or duration_spec == master.duration_spec)):
            return master
        edited = replace(master, start=canonical_start, end=canonical_end, all_day=all_day,
                         duration_spec=master.duration_spec if duration_spec is None else duration_spec)
        projected = updated_series(master, master, edited)
        return canonicalize_authored_primary(projected, primary_range=(start, end)) if phase_edit else projected

    def _range_from_reference(self, master, start, end, reference_start, reference_end, *, all_day=False, duration_spec=None):
        zone = self.author_anchor(start, all_day=all_day).tzinfo

        def wall(value):
            return value.replace(tzinfo=None) if all_day else value.astimezone(zone).replace(tzinfo=None)

        shift = wall(start) - wall(reference_start)
        canonical_start = (wall(master.start) + shift).replace(tzinfo=zone, fold=master.start.fold)
        if master.start.timestamp() == reference_start.timestamp() and not all_day:
            canonical_start = start.astimezone(zone)
            if end is not None and master.end.timestamp() == reference_end.timestamp():
                # This form controls the canonical primary itself. Both
                # endpoint instants are authoritative, including fold-only
                # changes that leave every written clock unchanged.
                return canonical_start, end.astimezone(zone)
        duration_source = master
        if end is not None:
            previous = replace(master, start=reference_start, end=reference_end)
            chosen = replace(master, start=start, end=end, all_day=all_day,
                             duration_spec=master.duration_spec if duration_spec is None else duration_spec)
            if not same_duration(previous, chosen):
                duration_source = chosen
        canonical_end = end_for(duration_source, canonical_start)
        return canonical_start, canonical_end

    def primary_range(self, start, end, *, all_day=False, duration_spec=None):
        """Canonical range for a graphical draft's possibly detached UI primary."""
        if self._edited_master is None:
            if self.configuration_changed:
                return self._source_range(start, end, all_day=all_day, duration_spec=duration_spec)
            return start, end
        effective = self._project_edited(start, end=end, all_day=all_day, duration_spec=duration_spec)
        return effective.start, effective.end

    def _adopt_graphical(self, master, *, reference_start=None, reference_end=None):
        """Adopt one canonical edit only after constructing its complete view.

        Actual starts may have a short program or need the canonical document.
        Neither representation changes nominal occurrence identities or causes
        exceptions to disappear when regular controls happen to be detectable.
        """
        staged = replace(self)
        view = reconstruct_authoring(master, authoring_zone=self.authoring_zone)
        staged._take_authoring(view, master)
        if (not master.recurrence_overrides and not view.structured and view.starts is not None):
            # A graphical list that becomes a regular series should export as
            # RRULE. Every nominal start already equals its displayed start,
            # so this exact compilation cannot change detached identities.
            evaluated = expression_event(master, view.expression, authoring_zone=self.authoring_zone)
            if evaluated.recurrence_rule:
                master = replace(master, recurrence_rule=evaluated.recurrence_rule,
                                 recurrence_dates=evaluated.recurrence_dates,
                                 excluded_dates=evaluated.excluded_dates,
                                 timezone_id=evaluated.timezone_id)
        staged._store_graphical(master, reference_start=reference_start, reference_end=reference_end)
        self.__dict__.update(staged.__dict__)

    def _store_graphical(self, master, *, reference_start=None, reference_end=None):
        fields = {name: deepcopy(getattr(master, name)) for name in _SCHEDULE_FIELDS}
        anchor = self.author_anchor(master.start, all_day=master.all_day)
        expression = (self.expression if expression_matches(master, self.expression)
                      else reconstruct_authoring(master).expression)
        fields.update(schedule_expression=expression,
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
        if active:
            for item in active:
                key = item.recurrence_id or recurrence_key(item.start, master.all_day)
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
            else:
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
        changed = replace(master, excluded_dates=tuple(set(master.excluded_dates) | {key}),
                          recurrence_overrides={name: value for name, value in master.recurrence_overrides.items() if name != key})
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master

    def add_occurrence(self, master: Event, start: datetime, end: datetime | None = None,
                       *, reference_start=None, reference_end=None, duration_spec=None):
        if not master.all_day:
            _checked(start)
        existing = expand_event(master, start - timedelta(seconds=1), start + timedelta(seconds=1), limit=1000)
        if any(recurrence_key(item.start, master.all_day) == recurrence_key(start, master.all_day) for item in existing):
            counts = multiplicities(master)
            counts[recurrence_key(start, master.all_day)] += 1
            extras = [key_datetime(key, master) for key, count in counts.items() for _ in range(count)]
            changed = apply_occurrence_counts(displayed_schedule(master), master, extras)
            new_key = next(key for key in changed.recurrence_overrides
                           if key not in master.recurrence_overrides
                           and recurrence_key(Event.from_dict(changed.recurrence_overrides[key]).start, master.all_day)
                           == recurrence_key(start, master.all_day))
            if end is not None:
                if not master.all_day:
                    _checked(end)
                if end.timestamp() < start.timestamp() or master.all_day and end.timestamp() == start.timestamp():
                    raise RecurrenceError('The end must not be before the start.')
                payloads = deepcopy(changed.recurrence_overrides)
                item = Event.from_dict(payloads[new_key])
                payloads[new_key] = replace(item, start=start, end=end,
                    duration_spec=item.duration_spec if duration_spec is None else duration_spec).to_dict()
                changed = replace(changed, recurrence_overrides=payloads)
            self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
            return self._edited_master
        key = recurrence_key(start, master.all_day)
        overrides = deepcopy(master.recurrence_overrides)
        if end is not None:
            if not master.all_day:
                _checked(end)
            if end.timestamp() < start.timestamp() or master.all_day and end.timestamp() == start.timestamp():
                raise RecurrenceError('The end must not be before the start.')
            default = occurrence(master, start, key)
            item = replace(default, start=start, end=end,
                           duration_spec=default.duration_spec if duration_spec is None else duration_spec)
            if end.timestamp() != default.end.timestamp() or item.duration_spec != default.duration_spec:
                overrides[key] = item.to_dict()
        changed = replace(master, recurrence_dates=tuple(set(master.recurrence_dates) | {key}),
                          excluded_dates=tuple(value for value in master.excluded_dates if value != key),
                          recurrence_overrides=overrides)
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master

    def edit_occurrence(self, master: Event, instance: Event, start: datetime, end: datetime,
                        *, reference_start=None, reference_end=None, duration_spec=None):
        """Move/resize an occurrence while keeping its original RECURRENCE-ID.

        A detached exception changes its displayed range, not its place in the
        standard recurrence. In particular this does not create an EXDATE plus
        a new RDATE, or replace an existing exception's nominal identity.
        """
        if not master.all_day:
            _checked(start)
            _checked(end)
        if end.timestamp() < start.timestamp() or master.all_day and end.timestamp() == start.timestamp():
            raise RecurrenceError('The end must not be before the start.')
        key = instance.recurrence_id or recurrence_key(instance.start, master.all_day)
        spec = instance.duration_spec if duration_spec is None else duration_spec
        if (start.timestamp() == instance.start.timestamp() and end.timestamp() == instance.end.timestamp()
                and spec == instance.duration_spec):
            return master
        original = occurrence(master, key_datetime(key, master), key)
        overrides = deepcopy(master.recurrence_overrides)
        overrides[key] = replace(original, start=start, end=end, duration_spec=spec).to_dict()
        changed = replace(master, recurrence_overrides=overrides,
                          excluded_dates=tuple(value for value in master.excluded_dates if value != key))
        self._adopt_graphical(changed, reference_start=reference_start, reference_end=reference_end)
        return self._edited_master


# Explicit aliases make the ending-control use clear at call sites.
recurrence_end_for_count = count_to_until
recurrence_count_until = until_to_count
