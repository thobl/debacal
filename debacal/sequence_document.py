"""A complete, editable scheduling document when a short program is unsuitable.

The compact sequence language describes a set of actual starts. That set alone
cannot identify two coincident detached occurrences, nor express every imported
iCalendar rule. ``schedule{...}`` instead names the canonical rule, additions,
exclusions and detached ranges directly. The enclosing event supplies the
reference range; existing detached content stays attached to its nominal key.

No recurrence expansion is required, so conversion is total even for ongoing
rules and very large finite sets. Parsing never evaluates Python or code.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time
import json
from zoneinfo import ZoneInfo

from .models import Event
from .recurrence import RecurrenceError, event_zone, key_datetime, occurrence, recurrence_key


PREFIX = 'schedule'
_FIELDS = {'rule', 'add', 'omit', 'move', 'range_policy'}
_RANGE_FIELDS = {'start', 'end', 'all_day', 'zone'}


def is_sequence_document(text: str) -> bool:
    """Recognize the reserved document form, including a malformed body."""
    return isinstance(text, str) and text.lstrip().startswith(PREFIX)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecurrenceError(f'The schedule contains duplicate field {key!r}.')
        result[key] = value
    return result


def parse_document(text: str) -> dict:
    """Read strict, scheduling-only JSON without hidden defaults from old text."""
    text = text.strip()
    if not text.startswith(PREFIX):
        raise RecurrenceError('A scheduling document must start with schedule{…}.')
    try:
        value = json.loads(text[len(PREFIX):], object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               RecurrenceError(f'{value} is not a scheduling value.')))
    except (json.JSONDecodeError, TypeError) as error:
        raise RecurrenceError(f'Invalid scheduling document: {error}') from error
    if not isinstance(value, dict) or value.keys() - _FIELDS:
        raise RecurrenceError('A schedule may contain only rule, add, omit and move fields.')
    if 'rule' in value and not isinstance(value['rule'], str):
        raise RecurrenceError('The schedule rule must be text.')
    if value.get('range_policy', '') not in ('', 'cut'):
        raise RecurrenceError('The schedule range policy must be cut or empty.')
    for field in ('add', 'omit'):
        items = value.get(field, [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise RecurrenceError(f'The schedule {field} field must be a list of dates or datetimes.')
    moves = value.get('move', {})
    if not isinstance(moves, dict):
        raise RecurrenceError('The schedule move field must map nominal dates to event ranges.')
    for key, item in moves.items():
        if (not isinstance(key, str) or not isinstance(item, dict)
                or item.keys() - _RANGE_FIELDS or not {'start', 'end'} <= item.keys()):
            raise RecurrenceError('Each moved occurrence needs a nominal date and a start/end range.')
        if not all(isinstance(item[name], str) for name in ('start', 'end')):
            raise RecurrenceError('Moved occurrence start and end values must be dates or datetimes.')
        if 'all_day' in item and type(item['all_day']) is not bool:
            raise RecurrenceError('A moved occurrence all_day value must be true or false.')
        if 'zone' in item and not isinstance(item['zone'], str):
            raise RecurrenceError('A moved occurrence zone must be a timezone name.')
    return value


def _range_document(item: Event) -> dict:
    def value_at(value):
        # Model values imported from other software can carry an all-day flag
        # together with non-midnight clocks. Keep every accepted range exactly;
        # the ordinary DATE-only case remains pleasant to read and edit.
        return (value.date().isoformat() if item.all_day and value.time() == time.min and value.fold == 0
                else value.isoformat())
    value = {'start': value_at(item.start), 'end': value_at(item.end),
             'all_day': item.all_day}
    if item.timezone_id:
        value['zone'] = item.timezone_id
    return value


def document_expression(event: Event) -> str:
    """Serialize canonical scheduling fields without enumerating any rule.

    Omitted clauses mean empty clauses. Every nominal detached identity is
    written separately, even when several of its actual ranges coincide.
    """
    document = {}
    if event.range_policy:
        document['range_policy'] = event.range_policy
    if event.recurrence_rule:
        document['rule'] = event.recurrence_rule
    if event.recurrence_dates:
        document['add'] = list(event.recurrence_dates)
    if event.excluded_dates:
        document['omit'] = list(event.excluded_dates)
    if event.recurrence_overrides:
        document['move'] = {key: _range_document(Event.from_dict(payload))
                            for key, payload in sorted(event.recurrence_overrides.items())}
    return PREFIX + json.dumps(document, ensure_ascii=False, separators=(', ', ': '))


def _datetime(value, *, zone):
    try:
        parsed = datetime.fromisoformat(value)
        if len(value) == 10:
            parsed = datetime.combine(parsed.date(), time.min, zone)
        elif parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(zone)
    except (ValueError, OverflowError) as error:
        raise RecurrenceError(f'Invalid scheduling date or datetime: {value!r}.') from error


def document_event(source: Event, text: str) -> Event:
    """Apply an explicit document, preserving detached payloads by nominal ID.

    Deleting a move clause restores its nominal occurrence; deleting a range
    itself uses omit. A newly written move inherits content from the prototype.
    Existing full exception payloads are copied and only range fields changed.
    """
    document = parse_document(text)
    # Event performs the same key canonicalization as ICS import and storage.
    bare = replace(source, recurrence_rule=document.get('rule', ''),
                   recurrence_dates=tuple(document.get('add', ())),
                   excluded_dates=tuple(document.get('omit', ())),
                   recurrence_overrides={}, range_policy=document.get('range_policy', ''))
    moves = {}
    for raw_key, values in document.get('move', {}).items():
        canonical = (datetime.fromisoformat(raw_key).date().isoformat() if len(raw_key) == 10
                     else recurrence_key(key_datetime(raw_key, bare), bare.all_day))
        if canonical in moves:
            raise RecurrenceError('Two moved occurrence keys name the same nominal date.')
        original = source.recurrence_overrides.get(canonical)
        item = (Event.from_dict(original) if original is not None
                else occurrence(bare, key_datetime(canonical, bare), canonical))
        try:
            zone = ZoneInfo(values['zone']) if values.get('zone') else event_zone(item)
        except (ValueError, KeyError) as error:
            raise RecurrenceError(f'Invalid occurrence timezone: {values.get("zone")!r}.') from error
        all_day = values.get('all_day', item.all_day)
        start, end = (_datetime(values[name], zone=zone) for name in ('start', 'end'))
        changed = replace(item, start=start, end=end, all_day=all_day,
                          timezone_id=values.get('zone', item.timezone_id))
        payload = deepcopy(original) if original is not None else item.to_dict()
        # Preserve all content and identity exactly when reading the unchanged
        # document. Range edits deliberately update only these four fields.
        if (changed.start.isoformat(), changed.end.isoformat(), changed.all_day, changed.timezone_id) != (
                item.start.isoformat(), item.end.isoformat(), item.all_day, item.timezone_id):
            payload.update(start=changed.start.isoformat(), end=changed.end.isoformat(),
                           all_day=changed.all_day, timezone_id=changed.timezone_id)
        moves[canonical] = payload
    return replace(bare, recurrence_overrides=moves)


def document_matches(event: Event, text: str) -> bool:
    """Prove equality directly from the finite canonical representation."""
    try:
        other = document_event(event, text)
    except (RecurrenceError, ValueError, TypeError, OverflowError):
        return False
    same_rule = other.recurrence_rule == event.recurrence_rule
    if not same_rule:
        # ICS writers may reorder clauses or spell an implicit INTERVAL=1.
        # These are exact symbolic identities, requiring no date expansion.
        from .schedule_authoring import simplified_rule
        try:
            same_rule = simplified_rule(other) == simplified_rule(event)
        except (RecurrenceError, ValueError, TypeError, OverflowError):
            return False
    return same_rule and all(getattr(other, field) == getattr(event, field) for field in (
        'recurrence_dates', 'excluded_dates', 'recurrence_overrides', 'range_policy'))
