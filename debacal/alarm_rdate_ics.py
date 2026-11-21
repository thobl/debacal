"""Wire representation of nominal reminders for finite, exact UTC date sets.

A relative VALARM on a UTC master applies the same elapsed duration to every
date. Keep that ordinary master and detach just the dates whose local calendar
arithmetic differs. The original zones can be recovered by replaying the
finite set and checking the actual standard components.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from . import ics_fallback as ics
from .alarm_fold_ics import needs_projection, _segments, _utc_bounds
from .alarm_ics import _alarm_lines, _trigger_identity, direct_parts, read_alarms
from .alarms import alarm_semantic, signed_duration
from .event_duration import end_for, occurrence_end_zone
from .ics_range_policy import MAX_GENERATED_RANGES
from .timezones import event_timezone, resolve_timezone, timezone_identifier


ZONE = 'X-DEBACAL-RDATE-ALARM-ZONE'
GENERATED = 'X-DEBACAL-RDATE-ALARM-GENERATED'
UTC = timezone.utc


def applies(event):
    if (not event.recurrence_dates or event.recurrence_rule or event.range_policy or event.duration_spec
            or event.schedule_expression and event.schedule_syntax == 2 or event.all_day or event.floating_time
            or not event.alarms):
        return False
    if (not event.timezone_id and event.start.tzinfo != UTC
            and timezone_identifier(event.start.tzinfo) not in {'UTC', 'Etc/UTC'}):
        return False
    start = event.start.astimezone(event_timezone(event))
    end = event.end if event.end_timezone_id else event.end.astimezone(start.tzinfo)
    ambiguous = any(bound.fold and bound.replace(fold=0).timestamp() != bound.timestamp() for bound in (start, end))
    return ambiguous and any(needs_projection(a, {'START', 'END'}) for a in event.alarms)


def utc_range(event):
    return replace(event, start=event.start.astimezone(UTC), end=event.end.astimezone(UTC), timezone_id='')


def _alarm_stamp(event):
    return sha256(repr(alarm_semantic(event.alarms)).encode()).hexdigest()


def zone_lines(event):
    lines = []
    stamp = _alarm_stamp(event)
    for role, bound, identifier in (
            ('START', event.start, event.timezone_id or 'UTC'),
            ('END', event.end, event.end_timezone_id or event.timezone_id)):
        local = bound.astimezone(resolve_timezone(identifier, event.timezone_definitions))
        lines.append(f'{ZONE};RELATED={role};TZID={ics.parameter(identifier)};X-ALARMS={stamp};'
                     f'X-UTC={bound.astimezone(UTC):%Y%m%dT%H%M%SZ}:{local:%Y%m%dT%H%M%S}')
    return lines


def candidate(master, component):
    anchors = component.get(ZONE, ())
    if len(anchors) != 2 or not master.alarms:
        return None
    utc_bounds = _utc_bounds(component['_alarm_source'])
    point = 'DTEND' not in component and 'DURATION' not in component
    if 'START' not in utc_bounds or 'END' not in utc_bounds and not point:
        return None
    stamp, bounds = _alarm_stamp(master), {}
    for params, wall in anchors:
        role = params.get('RELATED')
        if role not in {'START', 'END'} or role in bounds or params.get('X-ALARMS') != stamp:
            return None
        bound = master.start if role == 'START' else master.end
        identifier = params.get('TZID')
        if not identifier:
            return None
        try:
            local = bound.astimezone(resolve_timezone(identifier, master.timezone_definitions))
        except (ValueError, TypeError, OverflowError):
            return None
        if (params.get('X-UTC') != bound.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
                or wall != local.strftime('%Y%m%dT%H%M%S')):
            return None
        bounds[role] = (local, identifier)
    start, identifier = bounds['START']
    event = replace(master, start=start, end=bounds['END'][0], timezone_id=identifier)
    return event if applies(event) else None


def relative_child_alarms(alarms, component_key):
    """Keep relative alarm data and remap intra-child UID references."""
    relative = [a for a in alarms if (a.trigger or '').upper().startswith(('P', '+P', '-P'))]
    uids = {a.uid: 'urn:uuid:' + str(uuid5(NAMESPACE_URL, f'{component_key}/alarm/{a.uid}'))
            for a in relative if a.uid}
    result, ordinals = [], Counter()
    for alarm in relative:
        if not alarm.raw_ics:
            # UID insertion must have the same wire spelling before and after
            # an authored alarm acquires its retained template on first read.
            alarm = replace(alarm, raw_ics=ics.folded(_alarm_lines(alarm)).decode())
        if uids:
            lines = ['BEGIN:VALARM']
            for part in direct_parts(_alarm_lines(alarm)):
                if len(part) == 1 and part[0].split(':', 1)[0].split(';', 1)[0].upper() == 'RELATED-TO':
                    try:
                        _, _, value = ics.content_line(part[0])
                    except ValueError:
                        value = ''  # Preserve an unrecognized producer property verbatim.
                    if value and ics.text(value) in uids:
                        part = [part[0].removesuffix(value) + ics.escaped(uids[ics.text(value)])]
                lines.extend(part)
            alarm = replace(alarm, raw_ics=ics.folded([*lines, 'END:VALARM']).decode())
        if alarm.uid:
            uid = uids[alarm.uid]
        else:
            identity = (alarm.action, alarm.related, _trigger_identity(alarm.trigger))
            seed = json.dumps((component_key, identity, ordinals[identity]))
            uid = 'urn:uuid:' + str(uuid5(NAMESPACE_URL, seed))
            ordinals[identity] += 1
        result.append(replace(alarm, key=uid, uid=uid))
    return tuple(result)


def _utc_seconds(spec):
    days, seconds = signed_duration(spec)
    return days * 86400 + seconds


def _same_utc_schedule(alarm, start, end):
    bound = end if alarm.related == 'END' else start
    first, count, interval = next(_segments(alarm, start, end))
    return (first == bound.astimezone(UTC) + timedelta(seconds=_utc_seconds(alarm.trigger))
            and count == alarm.repeat
            and (not count or _utc_seconds(interval) == _utc_seconds(alarm.repeat_interval)))


def exceptions(event, manual):
    """Yield corrected children without expanding an RRULE or scanning time."""
    from .recurrence import occurrence_id, recurrence_key
    zone = event_timezone(event)
    primary = recurrence_key(event.start)
    alarms = [a for a in event.alarms if needs_projection(a, {'START', 'END'})]
    omitted = set(event.excluded_dates) | set(manual)
    budget = MAX_GENERATED_RANGES
    for key in sorted(({primary} | set(event.recurrence_dates)) - omitted):
        start = datetime.fromisoformat(key).astimezone(zone)
        end = occurrence_end_zone(event, end_for(event, start))
        differs = False
        for alarm in alarms:
            cost = 1
            if alarm.repeat and signed_duration(alarm.repeat_interval)[0]:
                cost += alarm.repeat
            budget -= cost
            if budget < 0:
                raise ValueError(f'Converting explicit-date reminders exceeds {MAX_GENERATED_RANGES:,} nominal evaluations. '
                                 'Shorten the date set or use elapsed reminders.')
            if not _same_utc_schedule(alarm, start, end):
                differs = True
        if differs:
            yield key, replace(event, id=occurrence_id(event.id, key), start=start, end=end,
                               recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                               series_id=event.id, recurrence_id=key, range_policy='', schedule_expression='',
                               expression_anchor='', schedule_syntax=1, raw_ics='',
                               alarms=relative_child_alarms(event.alarms, event.ical_uid + '/' + key))


def verified_children(event, overrides, sources, render):
    manual = {key for key in overrides if not sources.get(key)}
    verified = set()
    try:
        for key, child in exceptions(event, manual):
            if key not in overrides:
                return None  # A missing required child reinstates the standard UTC master.
            if matches_child(sources[key], render(child, event.ical_uid, key)):
                verified.add(key)
    except (ValueError, OverflowError):
        return None
    return verified


def matches_child(lines, expected):
    """Any extra property or nested data keeps the component as a manual edit."""
    if not lines:
        return False
    ignored = {'DTSTAMP', 'X-DEBACAL-ID', 'X-DEBACAL-CALENDAR'}
    def content(block):
        properties = sorted(tuple(part) for part in direct_parts(block)
                            if part[0].partition(':')[0].upper() not in ignored and part[0] != 'BEGIN:VALARM')
        return properties, alarm_semantic(read_alarms('\n'.join(block), component_key=''))
    return content(lines) == content(expected)
