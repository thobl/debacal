"""Small, best-effort descriptions of resources the event parser rejected.

This deliberately reads lexical properties rather than trying to import the
event again. Metadata is bounded and can be displayed without loading its raw
calendar data or trusting the timezone definition that caused the failure.
"""
from __future__ import annotations

from datetime import datetime
import re


DIAGNOSTIC_PREFIX_LIMIT = 262_144
SOURCE_PREVIEW_LIMIT = 262_144


def _parts(value, separator):
    quoted, start = False, 0
    for index, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            yield value[start:index]
            start = index + 1
    yield value[start:]


def _property(line):
    parts = iter(_parts(line, ':'))
    left = next(parts)
    # Preserve any colons in the value after finding the parameter boundary.
    if len(left) == len(line):
        return '', {}, ''
    value = line[len(left) + 1:]
    name, *parameters = _parts(left, ';')
    parsed = {}
    for parameter in parameters:
        key, separator, content = parameter.partition('=')
        if separator:
            content = content.strip('"')
            content = re.sub(r"\^([nN^'])", lambda match: {'n': '\n', 'N': '\n', '^': '^', "'": '"'}[match[1]], content)
            parsed[key.upper()] = content
    return name.upper(), parsed, value


def _text(value, limit=240):
    value = re.sub(r'\\([nN,;\\])', lambda match: '\n' if match[1] in 'nN' else match[1], value)
    return ' '.join(value.split())[:limit]


def resource_diagnostics(raw: str | bytes) -> dict:
    """Describe a master/series even when its calendar structure is damaged."""
    clipped = raw[:DIAGNOSTIC_PREFIX_LIMIT]
    text = clipped.decode('utf-8-sig', errors='replace') if isinstance(clipped, bytes) else clipped
    text = re.sub(r'(?:\r\n|\r|\n)[ \t]', '', text).replace('\r\n', '\n').replace('\r', '\n')
    stack, events, current, definitions = [], [], None, []
    for line in text.split('\n'):
        name, parameters, value = _property(line)
        if name == 'BEGIN':
            component = value.upper()
            stack.append(component)
            if component == 'VEVENT' and current is None:
                current = {'title': '', 'uid': '', 'start': '', 'end': '', 'all_day': False,
                           'timezones': [], 'detached': False}
            continue
        if name == 'END':
            component = value.upper()
            if component == 'VEVENT' and current is not None:
                events.append(current)
                current = None
                if len(events) >= 64:
                    break
            if component in stack:
                del stack[len(stack) - 1 - stack[::-1].index(component):]
            continue
        if stack and stack[-1] == 'VTIMEZONE' and name == 'TZID':
            identifier = _text(value, 180)
            if identifier and identifier not in definitions and len(definitions) < 8:
                definitions.append(identifier)
        # A VALARM may have its own UID/SUMMARY, which must not identify the event.
        if current is None or not stack or stack[-1] != 'VEVENT':
            continue
        if name == 'SUMMARY' and not current['title']:
            current['title'] = _text(value)
        elif name == 'UID' and not current['uid']:
            current['uid'] = _text(value, 300)
        elif name in {'DTSTART', 'DTEND'} and not current['start' if name == 'DTSTART' else 'end']:
            current['start' if name == 'DTSTART' else 'end'] = _text(value, 100)
            if name == 'DTSTART':
                current['all_day'] = parameters.get('VALUE', '').upper() == 'DATE' or len(value) == 8
        elif name == 'RECURRENCE-ID':
            current['detached'] = True
        identifier = _text(parameters.get('TZID', ''), 180)
        if identifier and identifier not in current['timezones'] and len(current['timezones']) < 8:
            current['timezones'].append(identifier)
    if current is not None and len(events) < 64:
        events.append(current)
    masters = [event for event in events if not event['detached']]
    selected = masters or events
    primary = selected[0] if selected else {}
    timezones = list(dict.fromkeys(zone for event in selected for zone in event['timezones']))[:8] or definitions
    return {'version': 1, 'title': primary.get('title', ''), 'uid': primary.get('uid', ''),
            'start': primary.get('start', ''), 'end': primary.get('end', ''),
            'all_day': primary.get('all_day', False), 'timezones': timezones,
            'events': selected[:16], 'truncated': len(raw) > DIAGNOSTIC_PREFIX_LIMIT}


def resource_when(diagnostics, preferences):
    value = diagnostics.get('start', '')
    try:
        pattern = '%Y%m%d' if diagnostics.get('all_day') else '%Y%m%dT%H%M%S' if len(value.rstrip('Z')) == 15 else '%Y%m%dT%H%M'
        start = datetime.strptime(value.rstrip('Z'), pattern)
    except (ValueError, TypeError):
        return value
    caption = preferences.format_date(start.date())
    caption += ' · All day' if diagnostics.get('all_day') else ' · ' + preferences.format_time(start)
    if value.endswith('Z'):
        caption += ' UTC'
    return caption


def resource_details(item, calendar_name, preferences, *, retained=True):
    metadata = item.get('diagnostics', {})
    lines = [calendar_name]
    if when := resource_when(metadata, preferences):
        lines.append(when)
    if zones := metadata.get('timezones'):
        lines.append('Timezone: ' + ', '.join(zones))
    lines.extend(['', item['reason'], '',
                  'The original calendar resource is retained. Other events can continue syncing.' if retained else
                  'This resource has not been imported. You can inspect or save its original calendar data.'])
    if metadata.get('truncated'):
        lines.append('Only the beginning was inspected for this description. Save the original to inspect the complete resource.')
    if 'timezone' in item['reason'].lower():
        lines.append('This may be valid calendar data that Debacal cannot interpret. The timezone definition may need compatibility support or repair.')
    lines.extend(['', 'Resource ID: ' + item['resource_id']])
    if uid := metadata.get('uid'):
        lines.append('Event UID: ' + uid)
    extra = metadata.get('events', [])[1:]
    if extra:
        lines.extend(['', 'Other event components in this resource:'])
        lines.extend((event.get('title') or 'Untitled event') + (' · ' + event['start'] if event.get('start') else '')
                     for event in extra)
    return '\n'.join(lines)
