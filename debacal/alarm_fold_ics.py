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
        if len(part) == 1 and part[0].upper().startswith(('DTSTART', 'DTEND')):
            name, params, value = ics.content_line(part[0])
            if not value.upper().endswith('Z') or 'TZID' in params or params.get('VALUE', 'DATE-TIME') != 'DATE-TIME':
                continue
            if name == 'DTSTART':
                result.add('START')
            elif name == 'DTEND':
                result.add('END')
    return result


def _implicit_anchor(lines, *, point=False):
    """Stamp the standard properties that supply an implicit end."""
    props = [part[0] for part in direct_parts(lines) if len(part) == 1
             and part[0].split(':', 1)[0].split(';', 1)[0].upper() in {'DTSTART', 'DTEND', 'DURATION'}]
    if ((point and len(props) == 1 and props[0].upper().startswith('DTSTART'))
            or (not point and len(props) == 2 and any(line.upper().startswith('DURATION:') for line in props))):
        return sha256('\n'.join(sorted(props)).encode()).hexdigest()
    return ''


def needs_projection(alarm, utc_bounds):
    if alarm.issue or alarm.related not in utc_bounds or not alarm.trigger:
        return False
    if not alarm.trigger.upper().startswith(('P', '+P', '-P')):
        return False
    specs = (alarm.trigger, alarm.repeat_interval if alarm.repeat else None)
    try:
        return any(signed_duration(spec)[0] for spec in specs if spec and ('D' in spec.upper() or 'W' in spec.upper()))
    except ValueError:
        return False  # Raw malformed alarm data remains authoritative.


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


def _point_end(alarm):
    return alarm.related == 'END' and not alarm.issue and (alarm.trigger or '').upper().startswith(('P', '+P', '-P'))


def _wire_parts(alarm, token, start, end, *, point_end=False):
    bound = (end if alarm.related == 'END' else start).astimezone(timezone.utc)
    parts = []
    for index, (first, repeat, interval) in enumerate(_segments(alarm, start, end)):
        uid = alarm.uid if index == 0 and alarm.uid else 'urn:uuid:' + str(uuid5(NAMESPACE_URL, f'{token}/{index}'))
        parts.append(replace(alarm, uid=uid, related='START' if point_end else alarm.related,
                             trigger=_elapsed(int((first - bound).total_seconds())),
                             repeat=repeat, repeat_interval=interval))
    return parts


def project_alarms(event, lines, component_key):
    alarms = event.alarms
    if event.floating_time or event.all_day or alarms is None or not len(alarms):
        return alarms, []
    # Detached points can use their own elapsed offsets. A finite UTC master
    # supplies the common UTC schedule while its generated exceptions correct
    # any date that requires a different offset in the original end zone.
    zoned = bool(event.timezone_id or event.end_timezone_id)
    point_anchor = ''
    if (not event.recurrence_rule and (not event.recurring or not zoned)
            and event.start.timestamp() == event.end.timestamp() and any(_point_end(a) for a in alarms)):
        point_anchor = _implicit_anchor(lines, point=True)
    if not (point_anchor or zoned):
        return alarms, []
    bounds = _utc_bounds(lines)
    duration_anchor = _implicit_anchor(lines) if not event.recurring and event.end_timezone_id else ''
    if duration_anchor:
        bounds.add('END')
    if not point_anchor and not any(needs_projection(alarm, bounds) for alarm in alarms):
        return alarms, []
    zone = event_timezone(event)
    start = event.start.astimezone(zone)
    end = event.end if event.end_timezone_id else event.end.astimezone(zone)
    annotations, anchored = [], set()
    wire, budget = [], MAX_GENERATED_RANGES
    ordinals = Counter()
    for alarm in alarms:
        identifier = event.timezone_id
        if alarm.related == 'END' and event.end_timezone_id:
            identifier = event.end_timezone_id
        is_utc = identifier in {'UTC', 'Etc/UTC'} and identifier not in event.timezone_definitions
        implicit = alarm.related == 'END' and duration_anchor
        point_end = bool(point_anchor and _point_end(alarm))
        if not point_end and (not identifier or is_utc and not implicit or not needs_projection(alarm, bounds)):
            wire.append(alarm)
            continue
        if alarm.related == 'END' and event.recurrence_rule and event.end_timezone_id:
            # A rule with a separately zoned, repeated-hour end needs exact UTC
            # instants, but a single elapsed offset cannot describe its local
            # day arithmetic across the series. Preserve the canonical event.
            raise ics.CalendarValueError(f'Cannot export event {event.ical_uid!r}: its recurring END reminder uses an '
                             'independently zoned repeated-hour end. Use an elapsed-hour reminder '
                             '(such as -PT24H) or adjust the endpoint.')
        if alarm.related not in anchored:
            bound = end if alarm.related == 'END' else start
            stamp = bound.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            source = f';X-DURATION={duration_anchor}' if implicit else ''
            if point_end:
                source = ';X-POINT=' + point_anchor
            local_time = f';TZID={ics.parameter(identifier)}:{bound:%Y%m%dT%H%M%S}' if identifier else ':' + stamp
            annotations.append(f'{ANCHOR};RELATED={alarm.related};X-UTC={stamp}{source};VALUE=DATE-TIME{local_time}')
            anchored.add(alarm.related)
        identity = (alarm.uid,) if alarm.uid else (alarm.action, alarm.related, _trigger_identity(alarm.trigger))
        ordinal = ordinals[identity]
        ordinals[identity] += 1
        token = sha256(json.dumps((component_key, identity, ordinal)).encode()).hexdigest()
        budget = _remaining_budget(alarm, budget)
        try:
            parts = _wire_parts(alarm, token, start, end, point_end=point_end)
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
    if not anchors or not originals or not alarms:
        return start, end, timezone_id, alarms
    utc_bounds = _utc_bounds(component['_alarm_source'])
    contexts = {}
    point_end = False
    counts = Counter(params.get('RELATED', 'START') for params, _wall in anchors)
    for params, wall in anchors:
        role = params.get('RELATED', 'START')
        point = 'X-POINT' in params
        if point:
            # Only a bare DTSTART justifies restoring END from a START alarm.
            if (role != 'END' or not params['X-POINT'] or 'X-DURATION' in params or 'RRULE' in component
                    or params['X-POINT'] != _implicit_anchor(component['_alarm_source'], point=True)
                    or 'RDATE' in component and (params.get('TZID') or 'START' not in utc_bounds)
                    or any('/' in raw for _, raw in component.get('RDATE', ()))):
                continue
        elif 'X-DURATION' in params:
            if (role != 'END' or not params['X-DURATION']
                    or params['X-DURATION'] != _implicit_anchor(component['_alarm_source'])
                    or any(name in component for name in ('RRULE', 'RDATE', 'EXDATE'))):
                continue
        elif role not in utc_bounds:
            continue
        if counts[role] != 1 or params.get('VALUE', 'DATE-TIME') != 'DATE-TIME':
            continue
        bound = end if role == 'END' else start
        try:
            identifier = params.get('TZID')
            utc_point = point and not identifier
            zone = timezone.utc if utc_point else resolve_timezone(identifier, component.get('_timezone_definitions', {}))
            local = bound.astimezone(zone)
        except (ValueError, TypeError, OverflowError):
            continue
        if local.strftime('%Y%m%dT%H%M%SZ' if utc_point else '%Y%m%dT%H%M%S') != wall:
            continue
        if 'X-UTC' in params:
            if bound.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ') != params['X-UTC']:
                continue
        elif 'RELATED' in params or not local.fold:
            # The first format attested only the late-fold start wall clock.
            continue
        contexts[role] = (local, identifier)
        if point:
            point_end = True
    by_uid = defaultdict(list)
    for index, alarm in enumerate(alarms):
        by_uid[alarm.uid].append(index)
    restored_groups, claimed, budget = [], set(), MAX_GENERATED_RANGES
    for params, payload in originals:
        token = params.get('GROUP', '')
        if not re.fullmatch('[0-9a-f]{64}', token):
            continue
        try:
            lines = ics.unfolded(b64decode(payload, validate=True))
            if not lines or lines[0] != 'BEGIN:VALARM' or lines[-1] != 'END:VALARM':
                continue
            original, = read_alarms('\n'.join(['BEGIN:VEVENT', *lines, 'END:VEVENT']), component_key=component_key)
            use_start = point_end and _point_end(original)
            if not use_start and not needs_projection(original, contexts):
                continue
            budget = _remaining_budget(original, budget)
            local, _identifier = contexts[original.related]
            expected = _wire_parts(original, token, local, local, point_end=use_start)
        except (ValueError, TypeError, OverflowError):
            continue  # Unrecognized or stale extensions leave the standard properties authoritative.
        members = []
        for part in expected:
            indices = by_uid.get(part.uid, ())
            if len(indices) != 1 or indices[0] in claimed:
                break
            index = indices[0]
            if semantic_properties(part) != semantic_properties(alarms[index]):
                break
            members.append(index)
        if len(members) == len(expected):
            restored_groups.append((original, members))
            claimed.update(members)
    # Rebinding an endpoint is justified only by an intact group on that
    # endpoint. A new nominal alarm using the same UTC bound needs UTC.
    blocked = {alarm.related for index, alarm in enumerate(alarms)
               if index not in claimed and needs_projection(alarm, contexts)}
    replacements, owned = {}, set()
    for original, members in restored_groups:
        if original.related not in blocked:
            replacements.update(dict.fromkeys(members))
            replacements[members[0]] = original
            owned.add(original.related)
    if not replacements:
        return start, end, timezone_id, alarms
    if 'START' in owned:
        start, timezone_id = contexts['START']
    # The finite UTC master's role conversion does not own either original
    # zone. Hydration may already have both from the verified date set.
    if 'END' in owned and contexts['END'][1]:
        end, _identifier = contexts['END']
    result = [replacements.get(index, alarm) for index, alarm in enumerate(alarms)]
    # Re-key recovered UID-less duplicates in their original component context.
    lines = ['BEGIN:VEVENT', *(line for alarm in result if alarm is not None for line in _alarm_lines(alarm)), 'END:VEVENT']
    return start, end, timezone_id, read_alarms('\n'.join(lines), component_key=component_key)
