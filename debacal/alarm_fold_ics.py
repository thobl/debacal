"""Represent nominal alarms when a zoned event bound must be written in UTC.

A TZID wall clock identifies the first instance of a repeated hour. To retain
the second instant, the event codec uses UTC. Other clients then need elapsed
alarm offsets; the original nominal intent is recoverable while the equivalent
standard alarms still describe precisely the same firings and metadata.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timezone
from hashlib import sha256
import json
import re
from uuid import NAMESPACE_URL, uuid5

from . import ics_fallback as ics
from .alarm_ics import _alarm_lines, _trigger_identity, direct_parts, read_alarms, semantic_properties
from .alarms import _advance, alarm_trigger, signed_duration
from .ics_range_policy import MAX_GENERATED_RANGES
from .timezones import event_timezone, resolve_timezone


ANCHOR = 'X-DEBACAL-ALARM-ANCHOR'
ORIGINAL = 'X-DEBACAL-ORIGINAL-ALARM'


def _utc_bounds(lines):
    result = set()
    for part in direct_parts(lines):
        if len(part) == 1 and part[0].upper().endswith('Z'):
            name = part[0].split(':', 1)[0].split(';', 1)[0].upper()
            if name == 'DTSTART':
                result.add('START')
            elif name == 'DTEND':
                result.add('END')
    return result


def _needs_projection(alarm, utc_bounds):
    if alarm.issue or alarm.related not in utc_bounds or not alarm.trigger:
        return False
    if not alarm.trigger.upper().startswith(('P', '+P', '-P')):
        return False
    return any('D' in spec.upper() or 'W' in spec.upper() for spec in (alarm.trigger, alarm.repeat_interval or ''))


def _elapsed(seconds):
    return ('-' if seconds < 0 else '') + 'PT' + str(abs(seconds)) + 'S'


def _segments(alarm, start, end):
    first = alarm_trigger(alarm, start, end)
    if not alarm.repeat or not signed_duration(alarm.repeat_interval or '')[0]:
        yield first, alarm.repeat, alarm.repeat_interval
        return
    days, seconds = signed_duration(alarm.repeat_interval)
    zone = (end if alarm.related == 'END' else start).tzinfo
    previous, count, spacing = first, 0, None
    for _ in range(alarm.repeat):
        following = _advance(previous.astimezone(zone), days, seconds)
        gap = int((following - previous).total_seconds())
        if spacing is None or gap == spacing:
            count += 1
            spacing = gap
        else:
            yield first, count, _elapsed(spacing)
            first, count, spacing = following, 0, None
        previous = following
    yield first, count, _elapsed(spacing) if spacing is not None else None


def _remaining_budget(alarm, budget):
    if alarm.repeat and signed_duration(alarm.repeat_interval or '')[0]:
        budget -= alarm.repeat
        if budget < 0:
            raise ValueError(f'Converting zoned reminders on UTC event times exceeds {MAX_GENERATED_RANGES:,} '
                             'nominal repetitions. Shorten the repeat count or use an elapsed interval.')
    return budget


def _wire_parts(alarm, token, start, end):
    bound = (end if alarm.related == 'END' else start).astimezone(timezone.utc)
    parts = []
    for index, (first, repeat, interval) in enumerate(_segments(alarm, start, end)):
        uid = alarm.uid if index == 0 and alarm.uid else 'urn:uuid:' + str(uuid5(NAMESPACE_URL, f'{token}/{index}'))
        parts.append(replace(alarm, uid=uid, trigger=_elapsed(int((first - bound).total_seconds())),
                             repeat=repeat, repeat_interval=interval))
    return parts


def project_alarms(event, lines, component_key):
    alarms = event.alarms
    if not event.timezone_id or event.floating_time or event.all_day or alarms is None or not len(alarms):
        return alarms, []
    bounds = _utc_bounds(lines)
    if not any(_needs_projection(alarm, bounds) for alarm in alarms):
        return alarms, []
    zone = event_timezone(event)
    start, end = event.start.astimezone(zone), event.end.astimezone(zone)
    annotations = [f'{ANCHOR};VALUE=DATE-TIME;TZID={ics.parameter(event.timezone_id)}:{start:%Y%m%dT%H%M%S}']
    wire, budget = [], MAX_GENERATED_RANGES
    ordinals = Counter()
    for alarm in alarms:
        if not _needs_projection(alarm, bounds):
            wire.append(alarm)
            continue
        identity = (alarm.uid,) if alarm.uid else (alarm.action, alarm.related, _trigger_identity(alarm.trigger))
        ordinal = ordinals[identity]
        ordinals[identity] += 1
        token = sha256(json.dumps((component_key, identity, ordinal)).encode()).hexdigest()
        budget = _remaining_budget(alarm, budget)
        try:
            parts = _wire_parts(alarm, token, start, end)
        except OverflowError as exc:
            raise ValueError('The zoned reminder exceeds the supported calendar range. Shorten its offset or repeats.') from exc
        original = b64encode(ics.folded(_alarm_lines(alarm))).decode('ascii')
        annotations.append(f'{ORIGINAL};GROUP={token};VALUE=BINARY;ENCODING=BASE64:{original}')
        wire.extend(parts)
    return tuple(wire), annotations


def restore_alarms(component, component_key, start, end, timezone_id, alarms):
    """Restore groups only when replaying the original explains all wire parts."""
    anchors = component.get(ANCHOR, ())
    originals = component.get(ORIGINAL, ())
    if len(anchors) != 1 or not originals or not alarms:
        return start, end, timezone_id, alarms
    params, wall = anchors[0]
    identifier = params.get('TZID')
    try:
        zone = resolve_timezone(identifier, component.get('_timezone_definitions', {}))
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    except (ValueError, TypeError, OverflowError):
        return start, end, timezone_id, alarms
    if local_start.strftime('%Y%m%dT%H%M%S') != wall:
        return start, end, timezone_id, alarms
    bounds = _utc_bounds(component['_alarm_source'])
    by_uid = defaultdict(list)
    for index, alarm in enumerate(alarms):
        by_uid[alarm.uid].append(index)
    replacements, budget = {}, MAX_GENERATED_RANGES
    for params, payload in originals:
        token = params.get('GROUP', '')
        if not re.fullmatch('[0-9a-f]{64}', token):
            continue
        try:
            lines = ics.unfolded(b64decode(payload, validate=True))
            if not lines or lines[0] != 'BEGIN:VALARM' or lines[-1] != 'END:VALARM':
                continue
            original, = read_alarms('\n'.join(['BEGIN:VEVENT', *lines, 'END:VEVENT']), component_key=component_key)
            if not _needs_projection(original, bounds):
                continue
            budget = _remaining_budget(original, budget)
            expected = _wire_parts(original, token, local_start, local_end)
        except (ValueError, TypeError, OverflowError):
            continue  # Unrecognized or stale extensions leave the standard properties authoritative.
        members = []
        for part in expected:
            indices = by_uid.get(part.uid, ())
            if len(indices) != 1 or indices[0] in replacements:
                break
            index = indices[0]
            if semantic_properties(part) != semantic_properties(alarms[index]):
                break
            members.append(index)
        if len(members) == len(expected):
            replacements.update(dict.fromkeys(members))
            replacements[members[0]] = original
    if not replacements or any(_needs_projection(alarm, bounds) for index, alarm in enumerate(alarms)
                               if index not in replacements):
        # A newly supplied nominal alarm on this UTC bound needs UTC semantics.
        return start, end, timezone_id, alarms
    result = [replacements.get(index, alarm) for index, alarm in enumerate(alarms)]
    # Re-key recovered UID-less duplicates in their original component context.
    lines = ['BEGIN:VEVENT', *(line for alarm in result if alarm is not None for line in _alarm_lines(alarm)), 'END:VEVENT']
    return local_start, local_end, identifier, read_alarms('\n'.join(lines), component_key=component_key)
