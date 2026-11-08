"""Pure partitioning and copying of complete calendar UID resources.

Following-occurrence operations split at the original RECURRENCE-ID, even when
the selected detached occurrence moved to another date. Neither side flattens
an unbounded rule. Storage assigns a new identity to the tail when needed.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from itertools import islice
import re
from uuid import uuid4

from dateutil.rrule import rrulestr

from .models import Event
from .event_duration import end_for
from .recurrence import (
    RecurrenceError, ScheduleReplacement, canonical_rule, event_zone,
    key_datetime, occurrence_id, recurrence_key, recurrence_set,
    rules_equivalent, updated_series, _valid_wall_time,
)


MAX_SPLIT_SCAN = 10000
_PHASE_SAMPLE = 64


def _instant(value):
    return value.astimezone(timezone.utc)


def _rule_parts(master):
    return dict(piece.split('=', 1) for piece in canonical_rule(master.recurrence_rule, master).split(';') if '=' in piece)


def _rule_text(parts):
    return ';'.join(key + '=' + value for key, value in parts.items())


def _rule_iterator(master):
    text = canonical_rule(master.recurrence_rule, master)
    if master.all_day:
        def until(match):
            last = datetime.strptime(match[1], '%Y%m%d').date()
            boundary = datetime.combine(last, time.max, event_zone(master))
            return 'UNTIL=' + boundary.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        text = re.sub(r'UNTIL=(\d{8})(?=;|$)', until, text)
    return iter(rrulestr(text, dtstart=master.start.astimezone(event_zone(master))))


def _end_at(master, start):
    return end_for(master, start)


def _has_visible_occurrence(master):
    excluded = set(master.excluded_dates)
    if any(key not in excluded for key in master.recurrence_overrides):
        return True
    for index, start in enumerate(recurrence_set(master)):
        if index >= MAX_SPLIT_SCAN:
            raise RecurrenceError('Too many excluded dates to split safely. Edit the whole series or choose a nearer occurrence.')
        if recurrence_key(start, master.all_day) not in excluded:
            return True
    return False


def _refresh_authoring(part, *, preferred_expression=None):
    """Give every partition a certified, complete editable expression."""
    from .schedule_authoring import authoring_anchor, expression_matches, reconstruct_authoring, simplified_rule
    normalized = replace(part, recurrence_rule=simplified_rule(part),
                         schedule_expression='', expression_anchor='', schedule_syntax=2)
    anchor = authoring_anchor(normalized)
    if preferred_expression is not None and expression_matches(normalized, preferred_expression):
        expression = preferred_expression
    else:
        # A clipped month suffix or complex imported rule may need a canonical
        # document. It is still fully representable; losing a short relative
        # spelling is not a reason to reject the requested split.
        expression = reconstruct_authoring(normalized).expression
    return replace(normalized, schedule_expression=expression,
                   expression_anchor=anchor.date().isoformat(), schedule_syntax=2)


def partition_series(master: Event, reference: Event) -> tuple[Event | None, Event | None]:
    """Return the visible head and tail around an occurrence's nominal start.

    Both results temporarily retain the source ID/UID. COUNT is split using rule
    instances before exclusions. Off-rule RDATE pivots remain RDATEs, while the
    tail DTSTART follows the first future rule instance to preserve its phase.
    At most 10,000 historical rule instances are inspected. Unsupported or
    unverifiable phase changes raise a useful error before any mutation.
    """
    if master.series_id:
        raise RecurrenceError('Choose a stored series master before splitting occurrences.')
    if reference.series_id and reference.series_id != master.id:
        raise RecurrenceError('This occurrence belongs to another series.')
    if reference.recurrence_id:
        pivot = key_datetime(reference.recurrence_id, master)
        pivot_key = recurrence_key(pivot, master.all_day)
    elif reference.id == master.id:
        pivot = master.start.astimezone(event_zone(master))
        pivot_key = recurrence_key(pivot, master.all_day)
    else:
        raise RecurrenceError('Choose an occurrence from this series.')
    if not master.recurring:
        # Following/all scopes on an ordinary event are the same operation.
        # A read-only snapshot must not synthesize authoring metadata.
        return None, master
    if pivot_key in master.excluded_dates:
        raise RecurrenceError('This occurrence is excluded from the series. Refresh before editing it.')
    from .coincident_occurrences import logical_occurrence_start
    pivot = logical_occurrence_start(master, pivot_key)
    cutoff = _instant(pivot)
    relative_parts = None
    if master.schedule_syntax == 2 and master.schedule_expression:
        from .patterns import PatternError, split_relative_sequence
        from .schedule_authoring import authoring_anchor, expression_matches
        if expression_matches(master, master.schedule_expression):
            try:
                relative_parts = split_relative_sequence(master.schedule_expression, authoring_anchor(master), pivot)
            except PatternError:
                # A suffix can retain dates while losing a preferred calendar
                # day encoded by the original anchor. Its canonical document
                # remains exact even when no sliced relative spelling exists.
                relative_parts = None
    parts = _rule_parts(master) if master.recurrence_rule else {}
    consumed, first_future, future_sample = 0, None, []
    if parts:
        recurrence_set(master)  # Apply the same validation as normal display.
        if 'COUNT' in parts and 'UNTIL' in parts:
            raise RecurrenceError('A rule with both COUNT and UNTIL cannot be split safely. Edit its whole-series rule first.')
        iterator = _rule_iterator(master)
        for generated in iterator:
            if _instant(generated) >= cutoff:
                first_future = generated
                future_sample = [generated, *islice(iterator, _PHASE_SAMPLE - 1)]
                break
            consumed += 1
            if consumed >= MAX_SPLIT_SCAN:
                raise RecurrenceError('This split is more than 10,000 rule occurrences from the beginning. Edit the whole series or split it nearer the start.')
    base = {recurrence_key(master.start, master.all_day): master.start.astimezone(event_zone(master))}
    base.update((key, key_datetime(key, master)) for key in master.recurrence_dates)
    if (pivot_key not in base and pivot_key not in master.recurrence_overrides
            and (first_future is None or recurrence_key(first_future, master.all_day) != pivot_key)):
        raise RecurrenceError('The selected date is no longer in this series. Refresh before editing it.')

    before_parts, after_parts = dict(parts), dict(parts)
    if consumed:
        if 'COUNT' in parts:
            before_parts['COUNT'] = str(consumed)
        else:
            if master.all_day:
                bound = (pivot.astimezone(event_zone(master)).date() - timedelta(days=1)).strftime('%Y%m%d')
            else:
                before_instant = _instant(pivot)
                before_instant = (before_instant.replace(microsecond=0) if before_instant.microsecond
                                  else before_instant - timedelta(seconds=1))
                bound = before_instant.strftime('%Y%m%dT%H%M%SZ')
            before_parts['UNTIL'] = min(parts.get('UNTIL', bound), bound)
    else:
        before_parts = {}
    if first_future is None:
        after_parts = {}
    elif 'COUNT' in after_parts:
        after_parts['COUNT'] = str(int(after_parts['COUNT']) - consumed)

    def belongs(key, after):
        return (_instant(logical_occurrence_start(master, key)) >= cutoff) == after

    def build(after):
        rule_parts = after_parts if after else before_parts
        local_base = {key: value for key, value in base.items() if belongs(key, after)}
        overrides = {key: value for key, value in master.recurrence_overrides.items() if belongs(key, after)}
        exclusions = tuple(key for key in master.excluded_dates if belongs(key, after))
        if after and rule_parts:
            start = first_future
        elif not after and rule_parts:
            start = master.start.astimezone(event_zone(master))
        else:
            options = list(local_base.values()) + [key_datetime(key, master) for key in overrides]
            if not options:
                return None
            initial = recurrence_key(master.start, master.all_day)
            start = (master.start.astimezone(event_zone(master)) if not after and initial in local_base
                     else min(options, key=_instant))
        end = _end_at(master, start)
        if after and not master.all_day and not all(_valid_wall_time(value) for value in (start, end)):
            raise RecurrenceError(
                'This occurrence crosses a daylight-saving clock gap. '
                'Split at another occurrence to preserve the series time and duration.'
            )
        # DTSTART itself belongs to the set even when it did not match RRULE.
        # Keep that original anchor as RDATE if the rule's tail starts elsewhere.
        rdates = tuple(key for key in local_base if key != recurrence_key(start, master.all_day))
        part = replace(master, start=start, end=end, recurrence_rule=_rule_text(rule_parts),
                       recurrence_dates=rdates, excluded_dates=exclusions, recurrence_overrides=overrides,
                       series_id='', recurrence_id='')
        if not _has_visible_occurrence(part):
            return None
        if after and rule_parts and _instant(start) != _instant(master.start):
            if any(name in rule_parts for name in ('BYWEEKNO', 'BYYEARDAY')):
                raise RecurrenceError('This week-number or year-day rule needs a whole-series edit; its following-occurrence phase cannot be split safely yet.')
            actual = list(islice(_rule_iterator(part), _PHASE_SAMPLE))
            if [_instant(value) for value in actual] != [_instant(value) for value in future_sample]:
                raise RecurrenceError('This rule changes phase when split. Edit the whole series or use an explicit finite schedule.')
        preferred = relative_parts[1 if after else 0] if relative_parts is not None else None
        return _refresh_authoring(part, preferred_expression=preferred)

    return build(False), build(True)


def updated_future(master: Event, reference: Event, edited: Event,
                   replace_schedule: bool | ScheduleReplacement = False) -> tuple[Event | None, Event | None]:
    """Project the same following-occurrence edit for storage and editor preview."""
    before, after = partition_series(master, reference)
    if after is None:
        return before, None
    changes = {}
    if not replace_schedule:
        if rules_equivalent(edited.recurrence_rule, master.recurrence_rule, master):
            changes['recurrence_rule'] = after.recurrence_rule
        for name in ('recurrence_dates', 'excluded_dates', 'recurrence_overrides'):
            if getattr(edited, name) == getattr(master, name):
                changes[name] = getattr(after, name)
        if edited.schedule_expression == master.schedule_expression and edited.schedule_syntax == master.schedule_syntax:
            changes.update(schedule_expression=after.schedule_expression,
                           expression_anchor=after.expression_anchor, schedule_syntax=after.schedule_syntax)
    rebased = replace(edited, **changes)
    return before, updated_series(after, reference, rebased, replace_schedule)


def _event_template_groups(block):
    """Group direct properties and whole nested component families by key."""
    order, groups = [], {}
    position = 1
    while position < len(block) - 1:
        line = block[position]
        left, separator, value = line.partition(':')
        if left.upper() == 'BEGIN' and separator:
            key = 'COMPONENT:' + value.upper()
            group, depth = [], 0
            while position < len(block) - 1:
                current = block[position]
                marker = current.partition(':')[0].upper()
                if marker == 'BEGIN':
                    depth += 1
                group.append(current)
                position += 1
                if marker == 'END':
                    depth -= 1
                if not depth:
                    break
        else:
            key = 'PROPERTY:' + left.split(';', 1)[0].upper()
            group = [line]
            position += 1
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].extend(group)
    return order, groups


def _single_occurrence_raw(master: Event, reference: Event) -> str:
    if not master.raw_ics:
        return ''
    if not isinstance(master.raw_ics, (str, bytes)):
        raise RecurrenceError('The source calendar metadata is malformed and cannot be copied safely. Repair or re-import this event first.')
    from .ics_resources import _blocks, _decoded, _key, _uid_of
    from .ics_fallback import components, folded, prop
    try:
        header, blocks = _blocks(master.raw_ics)
        base, detached = [], []
        for block in blocks:
            if block[0] != 'BEGIN:VEVENT' or _uid_of(block) != master.ical_uid:
                continue
            component = components(folded(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
            if 'RECURRENCE-ID' not in component:
                base.append(block)
            elif (reference.recurrence_id in master.recurrence_overrides
                  and prop(component, 'STATUS').upper() != 'CANCELLED'):
                key = _key(_decoded(component, 'RECURRENCE-ID', event_zone(master))[0], event_zone(master))
                if key == reference.recurrence_id:
                    detached.append(block)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RecurrenceError('The source calendar metadata is malformed and cannot be copied safely. Repair or re-import this event first.') from exc
    if len(base) != 1 or len(detached) > 1:
        raise RecurrenceError('The source event metadata is ambiguous and cannot be copied safely. Re-import this event first.')
    order, inherited = _event_template_groups(base[0])
    specific_order, specific = _event_template_groups(detached[0]) if detached else ([], {})
    order += [key for key in specific_order if key not in inherited]
    merged = inherited | specific
    removed = {'PROPERTY:' + name for name in (
        'RECURRENCE-ID', 'RRULE', 'RDATE', 'EXDATE',
        'X-DEBACAL-PATTERN', 'X-DEBACAL-PATTERN-ANCHOR', 'X-DEBACAL-PATTERN-VERSION',
    )}
    template = ['BEGIN:VEVENT', *(line for key in order if key not in removed for line in merged[key]), 'END:VEVENT']
    retained = [block for block in blocks if block[0] != 'BEGIN:VEVENT']
    return folded(['BEGIN:VCALENDAR', *header, *(line for block in retained for line in block),
                   *template, 'END:VCALENDAR']).decode()


def detach_event(master: Event, reference: Event) -> Event:
    """Capture one actual occurrence as an ordinary event with effective metadata.

    Identity remains that of the reference until the paste assigns a new ID/UID.
    A detached exception replaces whole property/component groups it supplies;
    absent groups, including alarms, are inherited from the parent master.
    """
    if reference.calendar_id != master.calendar_id or (
        reference.series_id and reference.series_id != master.id
    ) or (not reference.series_id and reference.id != master.id):
        raise RecurrenceError('This occurrence belongs to another event.')
    if reference.recurrence_id in master.excluded_dates:
        raise RecurrenceError('This occurrence is excluded from the series. Refresh before copying it.')
    raw = _single_occurrence_raw(master, reference) if master.recurring else master.raw_ics
    return replace(reference, ical_uid=master.ical_uid, series_id='', recurrence_id='',
                   recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                   schedule_expression='', expression_anchor='', schedule_syntax=1, raw_ics=raw)


def _reidentified_raw(event: Event, new_uid: str) -> str:
    """Retain source metadata while giving every owned VEVENT its new UID.

    Cancelled exceptions may be emitted verbatim by the serializer, so their
    UID must also change here. Nested alarm/component identifiers are separate
    identities and remain untouched. Unrelated event resources are not copied.
    """
    if not event.raw_ics:
        return ''
    if not isinstance(event.raw_ics, (str, bytes)):
        raise RecurrenceError('The source calendar metadata is malformed and cannot be copied safely. Repair or re-import this event first.')
    from .ics_resources import _blocks, _uid_of
    from .ics_fallback import escaped, folded
    try:
        header, blocks = _blocks(event.raw_ics)
    except (ValueError, TypeError) as exc:
        raise RecurrenceError('The source calendar metadata is malformed and cannot be copied safely. Repair or re-import this event first.') from exc
    result, matched = [], False
    for block in blocks:
        if block[0] != 'BEGIN:VEVENT':
            result.append(block)
            continue
        if _uid_of(block) != event.ical_uid:
            continue
        matched = True
        rewritten, depth = [], 0
        for line in block:
            left, separator, value = line.partition(':')
            marker = left.upper()
            if marker == 'BEGIN':
                depth += 1
            if depth == 1 and separator and left.split(';', 1)[0].upper() == 'UID':
                line = left + ':' + escaped(new_uid)
            rewritten.append(line)
            if marker == 'END':
                depth -= 1
        result.append(rewritten)
    if not matched:
        raise RecurrenceError('The source calendar metadata belongs to another event and cannot be copied safely. Re-import this event first.')
    return folded(['BEGIN:VCALENDAR', *header, *(line for block in result for line in block), 'END:VCALENDAR']).decode()


def reidentify_series(event: Event, new_id: str, new_uid: str) -> Event:
    """Give a resource and every detached exception one consistent new identity."""
    if not new_id or not new_uid:
        raise ValueError('A copied series needs a new resource ID and UID.')
    overrides = {}
    for key, payload in event.recurrence_overrides.items():
        item = Event.from_dict(payload)
        overrides[key] = replace(item, id=occurrence_id(new_id, key), series_id=new_id,
                                 recurrence_id=key, ical_uid=new_uid, calendar_id=event.calendar_id,
                                 raw_ics='', schedule_expression='', expression_anchor='', schedule_syntax=1).to_dict()
    return replace(event, id=new_id, ical_uid=new_uid, series_id='', recurrence_id='',
                   raw_ics=_reidentified_raw(event, new_uid), recurrence_overrides=overrides)


def clone_event(event: Event, *, new_id: str | None = None, new_uid: str | None = None,
                calendar_id: str | None = None, group_id: str | None = None) -> Event:
    """Copy an ordinary event/instance or a complete master without source links."""
    source = replace(event, calendar_id=calendar_id or event.calendar_id,
                     group_id=group_id, read_only=False)
    if event.series_id:
        source = replace(source, series_id='', recurrence_id='', recurrence_rule='', recurrence_dates=(),
                         excluded_dates=(), recurrence_overrides={}, schedule_expression='', expression_anchor='', schedule_syntax=1)
    identifier = new_id or str(uuid4())
    copied = reidentify_series(source, identifier, new_uid or identifier)
    overrides = {key: replace(Event.from_dict(payload), read_only=False, group_id=group_id).to_dict()
                 for key, payload in copied.recurrence_overrides.items()}
    return replace(copied, recurrence_overrides=overrides)


clone_series = clone_event
