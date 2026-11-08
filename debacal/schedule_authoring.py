"""Lossless conversion between canonical recurrence sets and authoring text.

The text describes every nominal active start, including additions and removed
dates. Detached occurrence ranges/content remain separate standard exceptions.
No conversion is adopted unless its complete recurrence set has been certified.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timezone

from .patterns import (DEFAULT_LIMIT, PatternError, compile_sequence, expand_sequence,
                       expression_for_regular, reconstruct_sequence)
from .recurrence import (RecurrenceError, event_zone, key_datetime, normalized_rule,
                         recurrence_key, recurrence_set, schedule_fields)


@dataclass(frozen=True)
class SequenceAuthoring:
    expression: str
    starts: tuple[datetime, ...] | None
    unbounded: bool


def authoring_anchor(event, authoring_zone=None):
    zone = authoring_zone or event_zone(event)
    return (datetime.combine(event.start.date(), time.min, zone) if event.all_day
            else event.start.astimezone(zone))


def authoring_event(event, authoring_zone=None):
    """Rezone the complete prototype, preserving floating all-day dates."""
    anchor = authoring_anchor(event, authoring_zone)
    end = (datetime.combine(event.end.date(), time.min, anchor.tzinfo) if event.all_day
           else event.end.astimezone(anchor.tzinfo))
    return replace(event, start=anchor, end=end, timezone_id=getattr(anchor.tzinfo, 'key', ''))


def simplified_rule(event):
    """Remove only constraints exactly equal to the DTSTART defaults."""
    parts = normalized_rule(event.recurrence_rule, event)
    for name in ('INTERVAL', 'COUNT', 'BYHOUR', 'BYMINUTE', 'BYSECOND', 'BYMONTHDAY', 'BYMONTH'):
        value = parts.get(name, '')
        if value.lstrip('+-').isdigit():
            parts[name] = str(int(value))
    if parts.get('INTERVAL') == '1':
        parts.pop('INTERVAL')
    anchor = authoring_anchor(event)
    defaults = {'BYHOUR': anchor.hour, 'BYMINUTE': anchor.minute, 'BYSECOND': anchor.second}
    if parts.get('FREQ') in ('MONTHLY', 'YEARLY'):
        defaults['BYMONTHDAY'] = anchor.day
    if parts.get('FREQ') == 'YEARLY':
        defaults['BYMONTH'] = anchor.month
    for name, value in defaults.items():
        if parts.get(name) == str(value):
            parts.pop(name)
    return ';'.join(f'{name}={value}' for name, value in sorted(parts.items()))


def _valid_wall(value):
    return value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) == value.replace(tzinfo=None)


def _same_zone(first, second):
    if first == second:
        return True
    # Qt/ICS can spell fixed UTC using either ZoneInfo or datetime.timezone.
    utc_names = {'UTC', 'Etc/UTC', 'Etc/GMT', 'GMT', 'UTC+00:00'}
    return str(first) in utc_names and str(second) in utc_names


def expression_event(event, expression, *, authoring_zone=None):
    """Evaluate text without inheriting hidden dates, exclusions or overrides."""
    prototype = authoring_event(event, authoring_zone)
    anchor = prototype.start
    compiled = compile_sequence(expression, anchor, all_day=event.all_day)
    fields = compiled.fields if compiled else schedule_fields(expand_sequence(expression, anchor), all_day=event.all_day)
    return replace(prototype, recurrence_rule=fields['recurrence_rule'],
                   recurrence_dates=fields['recurrence_dates'], excluded_dates=fields.get('excluded_dates', ()),
                   recurrence_overrides={}, timezone_id=getattr(anchor.tzinfo, 'key', ''))


def _unbounded(event):
    parts = normalized_rule(event.recurrence_rule, event)
    return bool(parts and not {'COUNT', 'UNTIL'} & parts.keys())


def _finite_starts(event, *, authoring_zone=None, limit=DEFAULT_LIMIT):
    anchor = authoring_anchor(event, authoring_zone)
    values = []
    for value in recurrence_set(event):
        if not _valid_wall(value):
            continue
        value = (datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day
                 else value.astimezone(anchor.tzinfo))
        values.append(value)
        if len(values) > limit:
            raise RecurrenceError(f'This schedule exceeds {limit:,} authoring occurrences. Its standard recurrence is preserved.')
    primary = recurrence_key(anchor, event.all_day)
    if primary not in {recurrence_key(value, event.all_day) for value in values}:
        raise RecurrenceError('The primary event is excluded. This schedule cannot be represented by a sequence that keeps the primary event.')
    return (anchor, *[value for value in values if recurrence_key(value, event.all_day) != primary])


def _contains(event, key, rules=None):
    value = key_datetime(key, event)
    previous = (rules if rules is not None else recurrence_set(event)).before(value, inc=True)
    return previous is not None and _valid_wall(previous) and recurrence_key(previous, event.all_day) == recurrence_key(value, event.all_day)


def recurrence_matches(first, second):
    """Exact nominal-set equivalence, with bounded finite exceptions.

    Equal infinite rules can differ only at their finite RDATE/EXDATE keys.
    Finite rules are compared in full within the authoring budget. No finite
    sample is used as proof that two different infinite rules are equivalent.
    """
    if first.all_day != second.all_day:
        return False
    if recurrence_key(first.start, first.all_day) != recurrence_key(second.start, second.all_day):
        return False
    first_unbounded, second_unbounded = _unbounded(first), _unbounded(second)
    if first_unbounded != second_unbounded:
        return False
    if not first_unbounded:
        return ({recurrence_key(value, first.all_day) for value in _finite_starts(first)} ==
                {recurrence_key(value, second.all_day) for value in _finite_starts(second)})
    if not first.all_day and not _same_zone(event_zone(first), event_zone(second)):
        return False
    if simplified_rule(first) != simplified_rule(second):
        return False
    def normalized_keys(event, name):
        return {recurrence_key(key_datetime(key, event), event.all_day) for key in getattr(event, name)}
    keys = ((normalized_keys(first, 'recurrence_dates') ^ normalized_keys(second, 'recurrence_dates'))
            | (normalized_keys(first, 'excluded_dates') ^ normalized_keys(second, 'excluded_dates')))
    if not keys:
        return True
    first_rules, second_rules = recurrence_set(first), recurrence_set(second)
    return all(_contains(first, key, first_rules) == _contains(second, key, second_rules) for key in keys)


def expression_matches(event, expression, *, authoring_zone=None):
    """Whether the whole expression describes the canonical nominal schedule."""
    try:
        candidate = expression_event(event, expression, authoring_zone=authoring_zone)
        return recurrence_matches(event, candidate)
    except (PatternError, RecurrenceError, ValueError, TypeError, OverflowError):
        return False


def _tail_expression(event, *, authoring_zone=None):
    anchor = authoring_anchor(event, authoring_zone)
    if not event.all_day and not _same_zone(anchor.tzinfo, event_zone(event)):
        raise RecurrenceError('This ongoing recurrence uses another timezone. Its standard rule is preserved.')
    parts = normalized_rule(simplified_rule(event), event)
    frequency = parts.get('FREQ')
    interval = int(parts.get('INTERVAL', '1'))
    try:
        tail = expression_for_regular(frequency, interval)
    except PatternError as error:
        raise RecurrenceError(str(error)) from error
    if expression_matches(event, tail, authoring_zone=anchor.tzinfo):
        return tail
    # A single terminal every() can express one calendar-frequency phase, not
    # multiple weekdays, positions within months, or arbitrary BYSETPOS rules.
    allowed = {'FREQ', 'INTERVAL', 'BYHOUR', 'BYMINUTE', 'BYSECOND'}
    allowed |= {'BYDAY'} if frequency == 'WEEKLY' else {'BYMONTHDAY'} if frequency == 'MONTHLY' else {'BYMONTHDAY', 'BYMONTH'} if frequency == 'YEARLY' else set()
    if (parts.keys() - allowed or any(',' in value for value in parts.values())
            or ('BYDAY' in parts and parts['BYDAY'] not in ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'))):
        raise RecurrenceError('This imported recurrence uses additional weekdays or constraints. Its rule is preserved.')
    primary = recurrence_key(anchor, event.all_day)
    if not _contains(event, primary):
        raise RecurrenceError('The primary event is excluded. This schedule cannot be represented by a sequence that keeps the primary event.')
    bare = replace(event, recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
    base_rules, actual_rules = recurrence_set(bare), recurrence_set(event)
    meaningful_keys = {key for key in (*event.recurrence_dates, *event.excluded_dates)
                       if _contains(event, key, actual_rules) != _contains(bare, key, base_rules)}
    special = [key_datetime(key, event) for key in meaningful_keys]
    cutoff = max([event.start, *special], key=lambda value: value.timestamp())
    cursor = None
    # Only visit the finite prefix up to the last exception and one untouched
    # tail member. This never attempts to enumerate an ongoing recurrence.
    for index, value in enumerate(recurrence_set(bare)):
        if index > DEFAULT_LIMIT:
            raise RecurrenceError(f'The edited prefix exceeds {DEFAULT_LIMIT:,} authoring occurrences. Its standard recurrence is preserved.')
        if not _valid_wall(value):
            continue
        # With no exceptions, DTSTART can already be the tail cursor.
        if (not special and value.timestamp() >= cutoff.timestamp()) or value.timestamp() > cutoff.timestamp():
            candidate = (datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day
                         else value.astimezone(anchor.tzinfo))
            # DTSTART may be an off-rule prototype; certify against the rule
            # rather than treating its unconditional set membership as a tail.
            try:
                trial = compile_sequence((reconstruct_sequence([anchor, candidate]) + ' ' + tail).strip(), anchor, all_day=event.all_day)
            except PatternError:
                continue
            if simplified_rule(replace(authoring_event(event, anchor.tzinfo), recurrence_rule=trial.recurrence_rule)) == simplified_rule(event):
                cursor = candidate
                break
    if cursor is None:
        raise RecurrenceError('This ongoing recurrence cannot retain its phase in a custom sequence. Its rule is preserved.')
    values = []
    for value in recurrence_set(event):
        if not _valid_wall(value):
            continue
        local = datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day else value.astimezone(anchor.tzinfo)
        if local.timestamp() > cursor.timestamp():
            break
        values.append(local)
        if len(values) > DEFAULT_LIMIT:
            raise RecurrenceError(f'The edited prefix exceeds {DEFAULT_LIMIT:,} authoring occurrences. Its standard recurrence is preserved.')
    ordered = [anchor, *[value for value in values if recurrence_key(value, event.all_day) != primary]]
    # A prefix may name dates before the prototype. The last instruction must
    # still put the cursor at the first unaffected tail member.
    prefix = reconstruct_sequence(ordered)
    result = (prefix + ' ' + tail).strip()
    if not expression_matches(event, result, authoring_zone=anchor.tzinfo):
        raise RecurrenceError('This recurrence cannot be represented by a complete custom sequence. Its rule is preserved.')
    return result


def reconstruct_authoring(event, *, authoring_zone=None, prefer_literal=True):
    """Return an immutable, certified view before any editor state changes."""
    unbounded = _unbounded(event)
    if (prefer_literal and event.schedule_syntax == 2 and event.schedule_expression
            and expression_matches(event, event.schedule_expression, authoring_zone=authoring_zone)):
        values = None if unbounded else _finite_starts(event, authoring_zone=authoring_zone)
        return SequenceAuthoring(event.schedule_expression, values, unbounded)
    if unbounded:
        expression = _tail_expression(event, authoring_zone=authoring_zone)
        return SequenceAuthoring(expression, None, True)
    values = _finite_starts(event, authoring_zone=authoring_zone)
    expression = reconstruct_sequence(values)
    if not expression_matches(event, expression, authoring_zone=authoring_zone):
        raise RecurrenceError('This schedule cannot be represented losslessly by a custom sequence.')
    return SequenceAuthoring(expression, values, False)
