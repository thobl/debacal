"""Direct VALARM components, retained extensions, and explicit legacy hydration."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from .alarms import Alarm, signed_duration, utc_stamp
from . import ics_fallback as ics
from .models import Event


_FIELDS = {'ACTION': ('action',), 'TRIGGER': ('trigger', 'related'), 'REPEAT': ('repeat',),
           'DURATION': ('repeat_interval',), 'UID': ('uid',), 'DESCRIPTION': ('description',),
           'ACKNOWLEDGED': ('acknowledged',)}


def direct_parts(lines):
    """Yield direct property lines or whole nested components of one block."""
    i = 1
    while i < len(lines) - 1:
        start = i
        i += 1
        if lines[start].upper().startswith('BEGIN:'):
            depth = 1
            while depth and i < len(lines):
                line = lines[i].upper()
                if line.startswith('BEGIN:'):
                    depth += 1
                elif line.startswith('END:'):
                    depth -= 1
                i += 1
            if depth:
                raise ValueError('Unclosed iCalendar component.')
        yield lines[start:i]


def without_alarms(lines):
    if 'BEGIN:VALARM' not in lines:  # _blocks has already normalized component markers.
        return lines
    return [lines[0], *(line for part in direct_parts(lines) if part[0].upper() != 'BEGIN:VALARM'
                        for line in part), lines[-1]]


def _repeat_count(value):
    if not re.fullmatch(r'[+-]?[0-9]+', value):
        raise ValueError('Invalid alarm repeat count.')
    count = int(value)
    if not 0 <= count <= 2**31 - 1:
        raise ValueError('Invalid alarm repeat count.')
    return count


def _parse_alarm(lines):
    props, issue = {}, None
    for part in direct_parts(lines):
        if len(part) > 1:
            continue
        try:
            name, params, value = ics.content_line(part[0])
        except ValueError:
            issue = 'Malformed alarm property.'
            continue
        if name in props and name in _FIELDS:
            issue = 'An alarm property occurs more than once.'
        props.setdefault(name, (params, value))

    def raw(name, default=None):
        return props.get(name, ({}, default))[1]

    action = raw('ACTION', '').upper()
    trigger = raw('TRIGGER')
    params = props.get('TRIGGER', ({}, None))[0]
    related = params.get('RELATED', 'START').upper()
    repeat = 0
    interval = raw('DURATION')
    acknowledged = None
    if action not in {'DISPLAY', 'AUDIO', 'EMAIL', 'PROCEDURE'}:
        issue = 'Unknown or missing alarm action.'
    if 'PROXIMITY' in props:
        issue = 'Location-based alarms do not have a local time trigger.'
    try:
        if not trigger:
            raise ValueError('Missing alarm trigger.')
        kind = params.get('VALUE', 'DURATION')
        if kind == 'DATE-TIME':
            if 'RELATED' in params or 'TZID' in params:
                raise ValueError('An absolute alarm trigger must be UTC with no RELATED parameter.')
            utc_stamp(trigger)
        elif kind == 'DURATION' and related in {'START', 'END'} and 'TZID' not in params:
            signed_duration(trigger)
        else:
            raise ValueError('Unsupported alarm trigger parameters.')
    except (ValueError, OverflowError):
        issue = 'Invalid or missing alarm trigger.'
    if 'REPEAT' in props or interval is not None:
        try:
            repeat = _repeat_count(raw('REPEAT', ''))
            days, seconds = signed_duration(interval or '')
            if days < 0 or seconds < 0 or not (days or seconds):
                raise ValueError
        except (ValueError, OverflowError):
            issue = 'Alarm repeats require a nonnegative count and a positive duration.'
    if 'ACKNOWLEDGED' in props:
        try:
            acknowledged = utc_stamp(raw('ACKNOWLEDGED'))
        except ValueError:
            issue = 'Invalid alarm acknowledgement time.'
    uid = ics.text(raw('UID', '')) or None
    return Alarm('', action, trigger, related if related in {'START', 'END'} else 'START', repeat,
                 interval, uid, ics.text(raw('DESCRIPTION', '')), acknowledged, ics.folded(lines).decode(), issue)


def _trigger_identity(value):
    try:
        return ('duration', *signed_duration(value))
    except (ValueError, OverflowError):
        return ('wire', value)


def read_alarms(vevent: str, *, component_key: str) -> tuple[Alarm, ...]:
    ordinals = Counter()
    result = []
    for part in direct_parts(ics.unfolded(vevent)):
        if part[0].upper() != 'BEGIN:VALARM':
            continue
        alarm = _parse_alarm(part)
        key = alarm.uid
        if key is None:
            identity = (component_key, alarm.action, alarm.related, _trigger_identity(alarm.trigger))
            ordinal = ordinals[identity]
            ordinals[identity] += 1
            key = 'alarm-' + sha256(json.dumps((*identity, ordinal)).encode()).hexdigest()
        result.append(replace(alarm, key=key))
    return tuple(result)


def _field_line(alarm, name, params):
    params = dict(params)
    if name == 'TRIGGER':
        value = alarm.trigger
        params.pop('RELATED', None)
        params.pop('VALUE', None)
        params.pop('TZID', None)
        if value and value.upper().startswith(('P', '-P', '+P')):
            if alarm.related != 'START':
                params['RELATED'] = alarm.related
        elif value:
            params['VALUE'] = 'DATE-TIME'
    elif name == 'DESCRIPTION':
        value = ics.escaped(alarm.description) if alarm.description or alarm.action == 'DISPLAY' else None
    elif name == 'UID':
        value = ics.escaped(alarm.uid) if alarm.uid else None
    elif name == 'ACKNOWLEDGED':
        value = (alarm.acknowledged.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                 if alarm.acknowledged else None)
    elif name == 'REPEAT':
        value = str(alarm.repeat) if alarm.repeat or alarm.repeat_interval is not None else None
    else:
        value = getattr(alarm, _FIELDS[name][0])
    if value is None:
        return None
    if '\r' in value or '\n' in value:
        raise ValueError('An alarm property must occupy one logical line.')
    return name + ''.join(';' + key + '=' + ics.parameter(item) for key, item in params.items()) + ':' + value


def _alarm_lines(alarm):
    lines = ics.unfolded(alarm.raw_ics) if alarm.raw_ics else []
    # Keep producer spellings, parameters, duplicated/malformed lines and
    # unknown nested data. Only rewrite a property whose structured intent changed.
    changed = set(_FIELDS)
    if lines:
        original = _parse_alarm(lines)
        changed = {name for name, attrs in _FIELDS.items()
                   if any(getattr(original, attr) != getattr(alarm, attr) for attr in attrs)}
        if not changed:
            return lines
    result, seen = ['BEGIN:VALARM'], set()
    for part in direct_parts(lines):
        try:
            name, params, _ = ics.content_line(part[0])
        except ValueError:
            name, params = '', {}
        if len(part) > 1 or name not in changed:
            result.extend(part)
        elif name not in seen:
            seen.add(name)
            line = _field_line(alarm, name, params)
            if line is not None:
                result.append(line)
    for name in _FIELDS:
        if name in changed and name not in seen:
            line = _field_line(alarm, name, {})
            if line is not None:
                result.append(line)
    return [*result, 'END:VALARM']


def write_alarms(vevent: str, alarms: tuple[Alarm, ...]) -> str:
    lines = ics.unfolded(vevent)
    result = [lines[0]]
    for part in direct_parts(lines):
        if part[0].upper() != 'BEGIN:VALARM':
            result.extend(part)
    for alarm in alarms:
        result.extend(_alarm_lines(alarm))
    return ics.folded([*result, lines[-1]]).decode()


def semantic_properties(alarm):
    def normalized(line):
        try:
            name, params, value = ics.content_line(line)
        except ValueError:
            return ('', (), line)
        if name == 'TRIGGER':
            if params.get('RELATED', '').upper() == 'START':
                params.pop('RELATED')
            elif 'RELATED' in params:
                params['RELATED'] = params['RELATED'].upper()
            if params.get('VALUE') == 'DURATION':
                params.pop('VALUE')
        if name == 'ACTION':
            value = value.upper()
        elif name in {'UID', 'DESCRIPTION'}:
            value = ics.text(value)
        elif name == 'REPEAT':
            try:
                value = str(_repeat_count(value))
            except ValueError:
                pass  # Invalid spellings remain distinct on the wire.
        elif name in {'TRIGGER', 'DURATION'}:
            value = _trigger_identity(value)
        return name, tuple(sorted(params.items())), value

    return tuple(sorted(tuple(normalized(line) for line in part) for part in direct_parts(_alarm_lines(alarm))))


def same_transport(left, right):
    """Preserve imported envelope changes outside separately compared VALARMs."""
    if left is None or right is None:
        return left is right
    previous, current = left.get('raw_ics', ''), right.get('raw_ics', '')
    if previous != current:
        if not previous or not current:
            return False
        from .ics_resources import _blocks
        def envelope(raw):
            header, blocks = _blocks(raw)
            for block in blocks:
                if block[0] == 'BEGIN:VEVENT':
                    properties = (part for part in direct_parts(block) if part[0].upper() != 'BEGIN:VALARM')
                    block[1:-1] = [line for part in properties for line in part]
            return header, blocks
        try:
            if envelope(previous) != envelope(current):
                return False
        except ValueError:
            return False
    children = left.get('recurrence_overrides', {})
    others = right.get('recurrence_overrides', {})
    return children.keys() == others.keys() and all(same_transport(child, others[key]) for key, child in children.items())


def normalize_alarm_context(event, values):
    """Compare the effective calculation rules independently of bound presentation."""
    from .alarm_fold_ics import needs_projection
    from .ics_resources import _timezone_signature
    from .timezones import event_timezone, timezone_identifier
    result = {}
    for alarm in event.alarms or ():
        if event.all_day or event.floating_time or not needs_projection(alarm, {'START', 'END'}):
            continue
        role = alarm.related
        if role in result:
            continue
        # Generated DURATION ends derive from their occurrence's DTSTART.
        separate_end = role == 'END' and event.end_timezone_id and not (event.recurring and event.duration_spec)
        zone = event.end.tzinfo if separate_end else event_timezone(event)
        identifier = timezone_identifier(zone) or str(zone)
        definition = event.timezone_definitions.get(identifier)
        result[role] = (_timezone_signature(ics.unfolded(definition)) if definition else
                        'UTC' if identifier == 'Etc/UTC' else identifier)
    if result:
        values['alarm_calculation_zones'] = result
    if (event.duration_spec or event.end_timezone_id == 'UTC' and event.recurrence_dates
            and not event.recurrence_rule and not event.schedule_expression):
        # DURATION owns projected ends. In an exact-date set, a UTC end
        # can become implicit when a repeated-hour primary exports as UTC.
        values.pop('end_timezone_id', None)


def _retained_alarms(event, raw, wanted, *, standalone_key=''):
    from . import alarm_fold_ics
    from .ics_resources import _blocks, _decoded, _key, _uid_of
    from .timezones import event_timezone

    family = {}
    zone = event_timezone(event)
    events = [lines for lines in _blocks(raw)[1] if lines[0] == 'BEGIN:VEVENT' and _uid_of(lines) == event.ical_uid]
    for lines in events:
        props = ics.components('\n'.join(['BEGIN:VCALENDAR', *lines, 'END:VCALENDAR']))[0]
        props['_timezone_definitions'] = event.timezone_definitions
        key = standalone_key if len(events) == 1 else ''
        if 'RECURRENCE-ID' in props:
            key = _key(_decoded(props, 'RECURRENCE-ID', zone)[0], zone, event.floating_time)
        if key in wanted:
            component_key = event.ical_uid + '/' + key
            alarms = read_alarms('\n'.join(lines), component_key=component_key)
            restored_fields = {}
            bounds = Event.from_dict(event.recurrence_overrides[key]) if key in event.recurrence_overrides else event
            # Pre-alarm snapshots retained only the primary TZID. Recover an
            # independently zoned DTEND only while it still describes this end.
            if (not bounds.all_day and not bounds.floating_time and not bounds.end_timezone_id
                    and len(props.get('DTEND', ())) == 1):
                params, value = props['DTEND'][0]
                if 'TZID' in params or value.upper().endswith('Z'):
                    try:
                        retained_end, = _decoded(props, 'DTEND', zone)
                    except (ValueError, KeyError, OverflowError):
                        retained_end = None
                    if isinstance(retained_end, datetime) and retained_end.timestamp() == bounds.end.timestamp():
                        bounds = replace(bounds, end=retained_end)
                        restored_fields['end'] = bounds.end
            if alarm_fold_ics.ORIGINAL in props and alarms:
                props['_alarm_source'] = lines
                start, end, identifier, restored = alarm_fold_ics.restore_alarms(
                    props, component_key, bounds.start, bounds.end, bounds.timezone_id, alarms)
                if restored is not alarms:
                    alarms = restored
                    restored_fields = dict(start=start, end=end, timezone_id=identifier)
            family[key] = alarms, restored_fields
    return family


def hydrate_alarms(event: Event) -> Event:
    """Resolve one canonical resource family on demand. Preserve explicit edits."""
    missing = {key for key, payload in event.recurrence_overrides.items() if payload.get('alarms') is None}
    if (event.alarms is not None and not missing) or event._query_projection:
        return event
    wanted = missing | {''} if event.alarms is None else missing
    family = _retained_alarms(event, event.raw_ics, wanted) if event.raw_ics else {}
    alarms, restored_fields = family.get('', ((), {})) if event.alarms is None else (event.alarms, {})
    overrides = event.recurrence_overrides.copy()
    for key in missing:
        payload = overrides[key]
        if key not in family and payload.get('raw_ics'):
            family.update(_retained_alarms(event, payload['raw_ics'], {key}, standalone_key=key))
        # Old locally created exceptions may have no detached wire template.
        alarms_for_child, child_fields = family.get(key, (alarms, {}))
        if child_fields:
            overrides[key] = replace(Event.from_dict(payload), alarms=alarms_for_child, **child_fields).to_dict()
        else:
            overrides[key] = dict(payload, alarms=[alarm.to_dict() for alarm in alarms_for_child])
    return replace(event, alarms=alarms, recurrence_overrides=overrides, **restored_fields)
