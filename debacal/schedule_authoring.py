"""Total conversion between canonical schedules and editable authoring text.

Short programs describe actual starts, retaining standard detached identities
and their content separately. A scheduling document names canonical fields
directly whenever a set of starts cannot retain the necessary information.
No conversion is adopted unless its complete scheduling state is certified.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone

from .models import Event
from .patterns import (DEFAULT_LIMIT, PatternError, compile_sequence,
                       expression_for_regular, reconstruct_sequence)
from .recurrence import (RecurrenceError, event_zone, key_datetime, normalized_rule,
                         normalize_recurrence_stream,
                         recurrence_key, recurrence_set)
from .regular_schedule import RegularSchedule
from .sequence_document import (document_event, document_expression, document_matches,
                                is_sequence_document)


@dataclass(frozen=True)
class SequenceAuthoring:
    expression: str
    starts: tuple[datetime, ...] | None
    unbounded: bool
    structured: bool = False


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
    if is_sequence_document(expression):
        return document_event(event, expression)
    prototype = authoring_event(event, authoring_zone)
    anchor = prototype.start
    compiled = compile_sequence(expression, anchor, all_day=event.all_day)
    fields = compiled.fields
    return replace(prototype, recurrence_rule=fields['recurrence_rule'],
                   recurrence_dates=fields['recurrence_dates'], excluded_dates=fields.get('excluded_dates', ()),
                   recurrence_overrides={}, timezone_id=getattr(anchor.tzinfo, 'key', ''))


def _unbounded(event):
    parts = normalized_rule(event.recurrence_rule, event)
    return bool(parts and not {'COUNT', 'UNTIL'} & parts.keys())


def _document_unbounded(event):
    """Classify even a malformed imported rule without blocking its document."""
    try:
        return _unbounded(event)
    except (RecurrenceError, ValueError, TypeError, OverflowError):
        fields = {part.partition('=')[0].strip().upper()
                  for part in event.recurrence_rule.split(';')}
        return bool(event.recurrence_rule and not {'COUNT', 'UNTIL'} & fields)


def _finite_starts(event, *, authoring_zone=None, limit=DEFAULT_LIMIT):
    anchor = authoring_anchor(event, authoring_zone)
    values = []
    for value in normalize_recurrence_stream(recurrence_set(event), all_day=event.all_day):
        value = (datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day
                 else value.astimezone(anchor.tzinfo))
        values.append(value)
        if len(values) > limit:
            raise RecurrenceError(f'This schedule exceeds {limit:,} authoring occurrences. Its standard recurrence is preserved.')
    values = sorted({recurrence_key(value, event.all_day): value for value in values}.values(),
                    key=lambda value: value.timestamp())
    primary = recurrence_key(anchor, event.all_day)
    # The reference controls interpretation, but need not itself be an event.
    # Keep it first when present; excluded/moved references need no invented
    # occurrence merely to make the editor's date field meaningful.
    return tuple([value for value in values if recurrence_key(value, event.all_day) == primary]
                 + [value for value in values if recurrence_key(value, event.all_day) != primary])


def _contains(event, key, rules=None):
    value = key_datetime(key, event)
    previous = (rules if rules is not None else recurrence_set(event)).before(value, inc=True)
    return previous is not None and recurrence_key(previous, event.all_day) == recurrence_key(value, event.all_day)


def displayed_schedule(event):
    """Project actual starts into a temporary identity-free recurrence set.

    This is exclusively an authoring/proof view. Persisting it would replace
    detached exceptions with unrelated RDATEs and lose their identities/content.
    Duplicate starts require the canonical scheduling document, so reject an
    identity-free projection instead of silently collapsing occurrences.
    """
    if not event.recurrence_overrides:
        return event
    excluded = set(event.excluded_dates)
    actual = []
    nominal = set()
    for key, payload in event.recurrence_overrides.items():
        if key in excluded:
            continue
        item = Event.from_dict(payload)
        if item.all_day != event.all_day:
            raise RecurrenceError('Occurrences with different all-day settings keep their standard exceptions; a single sequence cannot describe them.')
        if not _valid_wall(item.start):
            raise RecurrenceError('An occurrence uses a nonexistent local time. Its standard exception is preserved.')
        nominal.add(key)
        actual.append(recurrence_key(item.start, event.all_day))
    if not actual:
        return replace(event, recurrence_overrides={})
    base = replace(event, excluded_dates=tuple(excluded | nominal), recurrence_overrides={})
    rules = recurrence_set(base)
    if len(actual) != len(set(actual)) or any(_contains(base, key, rules) for key in actual):
        raise RecurrenceError('Several occurrences start at the same time. Their separate standard exceptions are preserved; a single sequence cannot describe them.')
    # Exclusion keys suppress both RRULE and RDATE values. Restore an actual
    # start after suppressing all detached nominal slots, including swaps.
    return replace(base, recurrence_dates=tuple(set(event.recurrence_dates) | set(actual)),
                   excluded_dates=tuple((excluded | nominal) - set(actual)))


def displayed_starts(event, *, authoring_zone=None, limit=DEFAULT_LIMIT):
    """Return the complete finite actual-start view, primary first."""
    if _unbounded(event):
        raise RecurrenceError('An unbounded recurrence cannot be expanded into a finite custom sequence.')
    return _finite_starts(displayed_schedule(event), authoring_zone=authoring_zone, limit=limit)


def canonical_starts(event, *, authoring_zone=None, limit=DEFAULT_LIMIT):
    """Finite actual starts retaining coincident detached identities.

    This is a visual/validation projection, not a compact-language proof view.
    It therefore keeps duplicates and mixed all-day exception ranges intact.
    """
    if _unbounded(event):
        raise RecurrenceError('An unbounded recurrence cannot be expanded into a finite custom sequence.')
    detached = set(event.recurrence_overrides)
    base = replace(event, recurrence_overrides={}, excluded_dates=tuple(set(event.excluded_dates) | detached))
    values = list(_finite_starts(base, authoring_zone=authoring_zone, limit=limit))
    for key, payload in event.recurrence_overrides.items():
        if key not in event.excluded_dates:
            item = Event.from_dict(payload)
            values.append(item.start.astimezone(authoring_zone or event_zone(event)))
            if len(values) > limit:
                raise RecurrenceError(f'This schedule exceeds {limit:,} authoring occurrences.')
    return tuple(sorted(values, key=lambda value: value.timestamp()))


def retain_displayed_overrides(candidate, source, *, shift=None):
    """Map retained actual starts back to their standard nominal identities.

    ``candidate`` is a newly evaluated expression without exceptions. An old
    exception survives when its actual start still occurs in that expression.
    Set operations translate that displayed slot back to its RECURRENCE-ID,
    leaving its complete payload available to other calendar clients.
    """
    shift = timedelta(0) if shift is None else shift
    zone = event_zone(candidate)

    def moved(value):
        local = (datetime.combine(value.date(), time.min, zone) if candidate.all_day
                 else value.astimezone(zone))
        return (local.replace(tzinfo=None) + shift).replace(tzinfo=zone, fold=local.fold)

    rules = recurrence_set(candidate)
    overrides, actual, nominal = {}, set(), set()
    for key, payload in source.recurrence_overrides.items():
        if key in source.excluded_dates:
            continue
        item = Event.from_dict(payload)
        start, end = moved(item.start), moved(item.end)
        if not _valid_wall(start) or not _valid_wall(end):
            raise RecurrenceError('Moving this occurrence would use a nonexistent local time. Choose another time.')
        actual_key = recurrence_key(start, candidate.all_day)
        if not _contains(candidate, actual_key, rules):
            continue
        nominal_key = recurrence_key(moved(key_datetime(key, source)), candidate.all_day)
        overrides[nominal_key] = replace(item, start=start, end=end, all_day=candidate.all_day,
                                         recurrence_id=nominal_key).to_dict()
        actual.add(actual_key)
        nominal.add(nominal_key)
    # A requested additional occurrence at a retained nominal slot would be
    # overwritten by that exception. Refuse this ambiguous conversion.
    if any(_contains(candidate, key, rules) for key in nominal - actual):
        raise RecurrenceError('A new occurrence uses the original slot of a moved occurrence. Choose another time or change that occurrence first.')
    bare = replace(candidate, recurrence_dates=(), excluded_dates=())
    bare_rules = recurrence_set(bare)
    additions = {key for key in nominal if not _contains(bare, key, bare_rules)}
    removals = {key for key in actual if _contains(bare, key, bare_rules)}
    result = replace(candidate, recurrence_dates=tuple((set(candidate.recurrence_dates) - actual) | additions),
                     excluded_dates=tuple((set(candidate.excluded_dates) | removals) - nominal),
                     recurrence_overrides=overrides)
    if not recurrence_matches(displayed_schedule(result), candidate):
        raise RecurrenceError('This sequence edit cannot preserve the occurrence exceptions without changing its dates.')
    return result


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
    same_zone = first.all_day or _same_zone(event_zone(first), event_zone(second))
    if first_unbounded and not same_zone:
        return False
    same_rule = same_zone and simplified_rule(first) == simplified_rule(second)
    if same_zone and not same_rule and not first_unbounded:
        # The controls, text and exact split boundary use one regular model.
        # Equal phases with the same finite rule count are equivalent even
        # when one uses COUNT and the other an exact inclusive UNTIL.
        regulars = [RegularSchedule.from_rule(value.recurrence_rule, authoring_anchor(value),
                                             all_day=value.all_day) for value in (first, second)]
        if all(value is not None and value.count is not None for value in regulars):
            def signature(regular):
                days = regular.weekdays or ((first.start.weekday(),) if regular.kind == 'WEEKLY' else ())
                return regular.kind, regular.interval, regular.count, days, regular.clamp
            same_rule = signature(regulars[0]) == signature(regulars[1])
    if not same_rule:
        if first_unbounded:
            return False
        return ({recurrence_key(value, first.all_day) for value in _finite_starts(first)} ==
                {recurrence_key(value, second.all_day) for value in _finite_starts(second)})
    def normalized_keys(event, name):
        return {recurrence_key(key_datetime(key, event), event.all_day) for key in getattr(event, name)}
    keys = ((normalized_keys(first, 'recurrence_dates') ^ normalized_keys(second, 'recurrence_dates'))
            | (normalized_keys(first, 'excluded_dates') ^ normalized_keys(second, 'excluded_dates')))
    if not keys:
        return True
    first_rules, second_rules = recurrence_set(first), recurrence_set(second)
    return all(_contains(first, key, first_rules) == _contains(second, key, second_rules) for key in keys)


def expression_matches(event, expression, *, authoring_zone=None):
    """Whether the whole expression describes the displayed actual starts.

    Use ``recurrence_matches`` for nominal RRULE/RDATE/EXDATE comparisons.
    Durations/content are preserved separately, never inferred from this text.
    """
    if is_sequence_document(expression):
        return document_matches(event, expression)
    try:
        candidate = expression_event(event, expression, authoring_zone=authoring_zone)
        return recurrence_matches(displayed_schedule(event), candidate)
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
        regular = RegularSchedule.from_rule(event.recurrence_rule, anchor, all_day=event.all_day)
        tail = expression_for_regular(frequency, interval, clamp=regular.clamp if regular else False)
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
    bare = replace(event, recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
    base_rules, actual_rules = recurrence_set(bare), recurrence_set(event)
    meaningful_keys = {key for key in (*event.recurrence_dates, *event.excluded_dates)
                       if _contains(event, key, actual_rules) != _contains(bare, key, base_rules)}
    special = [key_datetime(key, event) for key in meaningful_keys]
    cutoff = max([event.start, *special], key=lambda value: value.timestamp())
    cursor = None
    # Only visit the finite prefix up to the last exception and one untouched
    # tail member. This never attempts to enumerate an ongoing recurrence.
    for index, value in enumerate(normalize_recurrence_stream(recurrence_set(bare), all_day=event.all_day)):
        if index > DEFAULT_LIMIT:
            raise RecurrenceError(f'The edited prefix exceeds {DEFAULT_LIMIT:,} authoring occurrences. Its standard recurrence is preserved.')
        # With no exceptions, DTSTART can already be the tail cursor.
        if (not special and value.timestamp() >= cutoff.timestamp()) or value.timestamp() > cutoff.timestamp():
            candidate = (datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day
                         else value.astimezone(anchor.tzinfo))
            # DTSTART may be an off-rule prototype; certify against the rule
            # rather than treating its unconditional set membership as a tail.
            try:
                trial = compile_sequence((reconstruct_sequence([candidate], anchor=anchor, prefer_dates=False) + ' ' + tail).strip(), anchor, all_day=event.all_day)
            except PatternError:
                continue
            if simplified_rule(replace(authoring_event(event, anchor.tzinfo), recurrence_rule=trial.recurrence_rule)) == simplified_rule(event):
                cursor = candidate
                break
    if cursor is None:
        raise RecurrenceError('This ongoing recurrence cannot retain its phase in a custom sequence. Its rule is preserved.')
    values = []
    for value in normalize_recurrence_stream(recurrence_set(event), all_day=event.all_day):
        local = datetime.combine(value.date(), time.min, anchor.tzinfo) if event.all_day else value.astimezone(anchor.tzinfo)
        if local.timestamp() > cursor.timestamp():
            break
        values.append(local)
        if len(values) > DEFAULT_LIMIT:
            raise RecurrenceError(f'The edited prefix exceeds {DEFAULT_LIMIT:,} authoring occurrences. Its standard recurrence is preserved.')
    ordered = ([value for value in values if recurrence_key(value, event.all_day) == primary]
               + [value for value in values if recurrence_key(value, event.all_day) != primary])
    # A prefix may name dates before the prototype. The last instruction must
    # still put the cursor at the first unaffected tail member.
    # A jump into an ongoing regular tail describes that tail's phase, rather
    # than an arbitrary list of dates, so retain compact offset instructions.
    prefix = reconstruct_sequence(ordered, anchor=anchor, prefer_dates=False)
    result = (prefix + ' ' + tail).strip()
    if not expression_matches(event, result, authoring_zone=anchor.tzinfo):
        raise RecurrenceError('This recurrence cannot be represented by a complete custom sequence. Its rule is preserved.')
    return result


def _compact_authoring(event, *, authoring_zone=None, prefer_literal=True):
    """Try a certified short program; callers retain a total document fallback."""
    displayed = displayed_schedule(event)
    unbounded = _unbounded(displayed)
    if (prefer_literal and event.schedule_syntax == 2 and event.schedule_expression
            and expression_matches(event, event.schedule_expression, authoring_zone=authoring_zone)):
        values = None
        if not unbounded:
            try:
                values = _finite_starts(displayed, authoring_zone=authoring_zone)
            except RecurrenceError:
                from .patterns import sequence_tail
                # A certified bounded tail stays symbolic beyond the finite
                # visual authoring budget, just like its standard RRULE.
                if (not is_sequence_document(event.schedule_expression)
                        and sequence_tail(event.schedule_expression) is None):
                    raise
        return SequenceAuthoring(event.schedule_expression, values, unbounded,
                                 is_sequence_document(event.schedule_expression))
    if unbounded:
        expression = _tail_expression(displayed, authoring_zone=authoring_zone)
        return SequenceAuthoring(expression, None, True)
    try:
        values = _finite_starts(displayed, authoring_zone=authoring_zone)
    except RecurrenceError:
        # Large simple rules need not turn into thousands of date tokens.
        # Certify their regular text symbolically against the canonical rule.
        regular = RegularSchedule.from_rule(displayed.recurrence_rule, authoring_anchor(displayed),
                                            all_day=displayed.all_day)
        if regular is None or regular.count is None:
            raise
        expression = expression_for_regular(regular.kind, regular.interval, count=regular.count, clamp=regular.clamp)
        if not expression_matches(event, expression, authoring_zone=authoring_zone):
            raise
        return SequenceAuthoring(expression, None, False)
    expression = reconstruct_sequence(values, anchor=authoring_anchor(event, authoring_zone))
    regular = RegularSchedule.from_rule(displayed.recurrence_rule, authoring_anchor(displayed),
                                        all_day=displayed.all_day)
    if regular is not None and regular.count is not None and regular.count > 1:
        bounded = expression_for_regular(regular.kind, regular.interval, count=regular.count, clamp=regular.clamp)
        evaluated = expression_event(event, expression, authoring_zone=authoring_zone)
        inferred = RegularSchedule.from_rule(evaluated.recurrence_rule, authoring_anchor(evaluated),
                                             all_day=evaluated.all_day)
        # Keep monthly/yearly calendar intent, including explicit clamping,
        # rather than reclassifying a short sample as a daily stride.
        if ((inferred is None or (inferred.kind, inferred.interval, inferred.clamp)
                != (regular.kind, regular.interval, regular.clamp))
                and expression_matches(event, bounded, authoring_zone=authoring_zone)):
            expression = bounded
    if not expression_matches(event, expression, authoring_zone=authoring_zone):
        raise RecurrenceError('This schedule cannot be represented losslessly by a custom sequence.')
    return SequenceAuthoring(expression, values, False)


def reconstruct_authoring(event, *, authoring_zone=None, prefer_literal=True):
    """Return a lossless expression for every canonical scheduling state.

    Short programs are adopted only after exact recurrence-set proof. Whenever
    an imported rule or distinct detached identities are outside that language,
    the finite canonical fields provide a direct, editable scheduling document.
    No occurrence sampling, work budget or missing reference can remove the
    user's ability to read and edit the series definition.
    """
    if (prefer_literal and event.schedule_syntax == 2
            and is_sequence_document(event.schedule_expression)
            and document_matches(event, event.schedule_expression)):
        return SequenceAuthoring(event.schedule_expression, None, _document_unbounded(event), True)
    try:
        return _compact_authoring(event, authoring_zone=authoring_zone, prefer_literal=prefer_literal)
    except (PatternError, RecurrenceError, ValueError, TypeError, OverflowError):
        expression = document_expression(event)
        # Serialized scheduling fields are their own constructive inverse. The
        # check catches implementation mistakes rather than rejecting a class
        # of calendar or asking the user to discard standard event identities.
        if not document_matches(event, expression):
            raise RecurrenceError('The scheduling document could not preserve its canonical fields.')
        return SequenceAuthoring(expression, None, _document_unbounded(event), True)
