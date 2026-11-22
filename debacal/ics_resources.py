"""Lossless-resource calendar codec: one persisted master per iCalendar UID.

Recurrences expand only for display, never during import or synchronization.
Structured fields are authoritative when exporting; raw resources retain
unknown event properties, alarms, timezone definitions and detached metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
from uuid import NAMESPACE_URL, uuid5
import re

from . import alarm_fold_ics, alarm_rdate_ics
from .alarm_ics import read_alarms, without_alarms, write_alarms
from .models import Event
from .alarm_series import materialize_alarms
from .event_duration import add_duration, end_for, normalized_spec, parse_duration_spec
from .timezones import TimezoneError, event_timezone, resolve_timezone
from . import ics_fallback as fallback
from . import ics_range_policy as range_codec


@dataclass(frozen=True)
class ResourceIssue:
    resource_id: str
    reason: str


@dataclass(frozen=True)
class RejectedResource:
    uid: str
    raw_ics: str
    resource_id: str
    reason: str


@dataclass(frozen=True)
class ParseReport:
    events: tuple[Event, ...]
    issues: tuple[ResourceIssue, ...] = ()
    rejected: tuple[RejectedResource, ...] = ()


class PeriodError(fallback.CalendarValueError):
    """A safe, property-specific explanation for a malformed RDATE period."""


class ConflictingTimezoneDefinitions(ValueError):
    def __init__(self, identifier):
        self.identifier = identifier
        super().__init__(f'Incompatible timezone definitions for {identifier!r}. Export the affected events separately.')


@dataclass(frozen=True)
class _RecurrencePeriod:
    start: datetime
    end: datetime
    duration_spec: str
    floating_time: bool
    timezone_id: str


def reject_duplicate_identities(report: ParseReport, *, existing_events=()) -> ParseReport:
    """Quarantine resources claiming one local ID instead of overwriting a peer.

    Other clients can copy native X-properties while assigning a fresh UID.
    Those resources must not silently collapse when written to SQLite. Repeated
    versions of the same UID remain valid and can use the last-source rule.
    Existing events are only checked for ownership; they are never rejected or
    mutated when incoming data claims their identity for another UID.
    """
    identities = {}
    for event in report.events:
        identities.setdefault(event.id, set()).add((event.calendar_id, event.ical_uid))
    for event in existing_events:
        if event.id in identities:
            identities[event.id].add((event.calendar_id, event.ical_uid))
    duplicates = {identifier for identifier, uids in identities.items() if len(uids) > 1}
    if not duplicates:
        return report
    events, issues, rejected = [], list(report.issues), list(report.rejected)
    reason = 'Different calendar resources share a Debacal identity. Remove duplicated X-DEBACAL-ID metadata from the source.'
    for event in report.events:
        if event.id not in duplicates:
            events.append(event)
            continue
        resource_id = hashlib.sha256(event.ical_uid.encode()).hexdigest()[:12]
        issues.append(ResourceIssue(resource_id, reason))
        rejected.append(RejectedResource(event.ical_uid, event.raw_ics, resource_id, reason))
    return ParseReport(tuple(events), tuple(issues), tuple(rejected))


def _safe_reason(exc):
    from .recurrence import RecurrenceError
    message = str(exc).lower()
    if isinstance(exc, TimezoneError):
        return str(exc)
    if isinstance(exc, fallback.CalendarValueError):
        return str(exc)
    if "schedule syntax version" in message:
        return "Unsupported custom schedule version."
    if "until" in message:
        return "Invalid recurrence end boundary (UNTIL)."
    if isinstance(exc, RecurrenceError) or "recurrence" in message or "rrule" in message:
        return "Unsupported or invalid recurrence rule."
    if "timezone" in message:
        return "Event time values have missing or invalid timezone information."
    if "end" in message and "before" in message:
        return "Event ends before its start."
    if "uid is missing" in message:
        return "Event UID is missing."
    return "Unsupported or malformed event resource."


def parse_report(data, calendar_id, *, limit=10000, use_full=True, repair_missing_uids=False):
    """Split UID resources before decoding so one bad event cannot abort peers."""
    lines = fallback.unfolded(data)
    if not lines or "BEGIN:VCALENDAR" not in {line.upper() for line in lines} or "END:VCALENDAR" not in {line.upper() for line in lines}:
        raise ValueError("Expected a complete VCALENDAR document.")
    if sum(line.upper() == 'BEGIN:VCALENDAR' for line in lines) > 1:
        return _parse_calendar_stream(lines, calendar_id, limit=limit, use_full=use_full,
                                      repair_missing_uids=repair_missing_uids)
    try:
        header, blocks = _blocks(data)
    except ValueError:
        uids, stack = set(), []
        for line in lines:
            left, _, value = line.partition(":")
            if left.upper() == "BEGIN":
                stack.append(value.upper())
            elif left.upper() == "END":
                if stack:
                    stack.pop()
            elif stack == ["VCALENDAR", "VEVENT"] and left.split(";")[0].upper() == "UID":
                uids.add(fallback.text(value))
        uid = next(iter(uids)) if len(uids) == 1 else ""
        raw = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
        resource_id = hashlib.sha256((uid or raw).encode()).hexdigest()[:12]
        reason = "Malformed or unclosed iCalendar component."
        return ParseReport((), (ResourceIssue(resource_id, reason),),
                           (RejectedResource(uid, raw, resource_id, reason),))
    timezones = [block for block in blocks if block[0] == "BEGIN:VTIMEZONE"]
    event_blocks = [block for block in blocks if block[0] == "BEGIN:VEVENT"]
    generated_count = sum(_generated_block(block) for block in event_blocks)
    if len(event_blocks) - generated_count > limit or generated_count > range_codec.MAX_GENERATED_RANGES:
        raise ValueError(f"Import exceeds {limit:,} event components.")
    grouped = {}
    generated_uids, duplicate_counts = set(), {}
    used_uids = {_uid_of(block) for block in event_blocks if _uid_of(block)}
    for index, block in enumerate(event_blocks):
        uid = _uid_of(block)
        if not uid and repair_missing_uids and _property_of(block, "RECURRENCE-ID") is None:
            # Some file exporters omit UID altogether. Repair standalone
            # masters for a reviewed import, but never guess which series an
            # unidentified detached exception belongs to. Stable content IDs
            # also make importing the same file again idempotent.
            fingerprint = hashlib.sha256("\n".join(block).encode()).hexdigest()
            ordinal = duplicate_counts.get(fingerprint, 0)
            while True:
                uid = "urn:uuid:" + str(uuid5(NAMESPACE_URL, f"debacal:import:missing-uid:{fingerprint}:{ordinal}"))
                ordinal += 1
                if uid not in used_uids:
                    break
            duplicate_counts[fingerprint] = ordinal
            used_uids.add(uid)
            generated_uids.add(uid)
            block = _with_uid(block, uid)
        grouped.setdefault(uid or f"missing-uid-{index}", []).append(block)
    decoded_groups = {}
    if len(grouped) > 1:
        try:
            # Decode shared VTIMEZONE definitions once for the whole file.
            # A malformed component must still use the isolated resource path,
            # and repaired missing UIDs have no matching original component.
            for component in _read(data, use_full):
                uid = fallback.prop(component, 'UID')
                if uid:
                    decoded_groups.setdefault(uid, []).append(component)
        except (ValueError, TypeError, KeyError, OverflowError):
            decoded_groups = {}
    events, issues, rejected = [], [], []
    for key, group in grouped.items():
        resource_blocks = timezones + group
        raw = fallback.folded(["BEGIN:VCALENDAR", *header,
                               *(line for block in resource_blocks for line in block), "END:VCALENDAR"]).decode()
        uid = _uid_of(group[0])
        resource_id = hashlib.sha256((uid or raw).encode()).hexdigest()[:12]
        try:
            if not uid:
                raise ValueError("Event UID is missing.")
            parsed = parse(raw, calendar_id, limit=limit, use_full=use_full,
                           _source=decoded_groups.get(uid), _resource_context=(header, resource_blocks))
            events.extend(parsed)
            if uid in generated_uids and parsed:
                issues.append(ResourceIssue(resource_id, "Missing event UID; generated a stable identifier for import."))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            reason = _safe_reason(exc)
            issues.append(ResourceIssue(resource_id, reason))
            rejected.append(RejectedResource(uid, raw, resource_id, reason))
    return reject_duplicate_identities(ParseReport(
        tuple(sorted(events, key=lambda event: (event.start.timestamp(), event.id))), tuple(issues), tuple(rejected)))


def _parse_calendar_stream(lines, calendar_id, *, limit, use_full, repair_missing_uids):
    from .ics_stream import calendar_documents
    try:
        documents = list(calendar_documents(lines))
    except ValueError:
        raw = fallback.folded(lines).decode()
        resource_id = hashlib.sha256(raw.encode()).hexdigest()[:12]
        reason = 'Malformed or unclosed calendar stream. View the original to inspect its VCALENDAR boundaries.'
        return ParseReport((), (ResourceIssue(resource_id, reason),),
                           (RejectedResource('', raw, resource_id, reason),))
    prepared, count, generated = [], 0, 0
    for document in documents:
        data = fallback.folded(document)
        try:
            context = _blocks(data)
        except ValueError:
            context = None  # The ordinary per-resource path retains damage.
        event_blocks = [block for block in context[1] if block[0] == 'BEGIN:VEVENT'] if context else []
        count += len(event_blocks)
        generated += sum(_generated_block(block) for block in event_blocks)
        prepared.append((data, context, {_uid_of(block) for block in event_blocks if _uid_of(block)}))
    if count - generated > limit or generated > range_codec.MAX_GENERATED_RANGES:
        raise ValueError(f'Import exceeds {limit:,} event components.')
    events, issues, rejected, sources, seen, duplicates = [], [], [], {}, set(), set()
    for data, context, raw_uids in prepared:
        report = parse_report(data, calendar_id, limit=limit, use_full=use_full,
                              repair_missing_uids=repair_missing_uids)
        present = raw_uids | {event.ical_uid for event in report.events} | {item.uid for item in report.rejected if item.uid}
        duplicates.update(seen & present)
        seen.update(present)
        for uid in raw_uids:
            try:
                raw = _resource_for_uid(data, uid, context=context)
            except (ValueError, TypeError, KeyError):
                raw = data.decode()
            sources.setdefault(uid, []).append(raw)
        for item in (*report.events, *report.rejected):
            uid = item.ical_uid if isinstance(item, Event) else item.uid
            if uid and uid not in raw_uids:
                sources.setdefault(uid, []).append(item.raw_ics)
        events.extend(report.events)
        issues.extend(report.issues)
        rejected.extend(report.rejected)
    if duplicates:
        # Different documents can legitimately reuse timezone identifiers, but
        # importing competing versions of one UID into one destination would
        # silently overwrite an event. Retain both versions for review instead.
        ids = {hashlib.sha256(uid.encode()).hexdigest()[:12] for uid in duplicates}
        events = [event for event in events if event.ical_uid not in duplicates]
        issues = [item for item in issues if item.resource_id not in ids]
        rejected = [item for item in rejected if item.uid not in duplicates]
        reason = ('Different calendar documents contain the same event UID. '
                  'Import those calendars separately to choose which version to keep.')
        for uid in sorted(duplicates):
            resource_id = hashlib.sha256(uid.encode()).hexdigest()[:12]
            issues.append(ResourceIssue(resource_id, reason))
            rejected.append(RejectedResource(uid, '\r\n'.join(sources[uid]), resource_id, reason))
    return reject_duplicate_identities(ParseReport(
        tuple(sorted(events, key=lambda event: (event.start.timestamp(), event.id))), tuple(issues), tuple(rejected)))


_DATE_PROPERTIES = {'DTSTART', 'DTEND', 'RDATE', 'EXDATE', 'RECURRENCE-ID', alarm_fold_ics.ANCHOR, alarm_rdate_ics.ZONE}


def _references_timezone(parameters, raw):
    # A PERIOD can finish with a UTC endpoint or duration while its start
    # still depends on this resource's explicit timezone definition.
    return bool(parameters.get('TZID') and parameters.get('VALUE') != 'DATE'
                and any(not piece.split('/', 1)[0].upper().endswith('Z') for piece in raw.split(',')))


def _timezone_definitions(blocks, referenced=None):
    definitions = {}
    for block in blocks:
        if block[0] != 'BEGIN:VTIMEZONE':
            continue
        identifier = fallback.text(_property_of(block, 'TZID') or '')
        if not identifier:
            continue  # An unused invalid definition must not reject healthy events.
        if referenced is not None and identifier not in referenced:
            continue
        definition = fallback.folded(block).decode()
        if identifier in definitions and definitions[identifier] != definition:
            raise TimezoneError(f'Timezone {identifier!r} has conflicting definitions in one calendar resource.')
        definitions[identifier] = definition
    # Authenticate generated source only after validating duplicate identifiers.
    # Exact current/legacy output expresses native IANA intent; decode its
    # clocks natively too, rather than merely removing its model metadata later.
    return {identifier: definition for identifier, definition in definitions.items()
            if _generated_timezone_coverage(tuple(fallback.unfolded(definition))) is None}


def _read(data, use_full=True):
    from .ics import ICalendar
    header, blocks = _blocks(data)
    for line in header:
        if not line:
            continue
        name, _parameters, value = fallback.content_line(line)
        if name.replace(' ', '').replace('\t', '') == 'CALSCALE':
            if value.strip().upper() != 'GREGORIAN':
                raise fallback.CalendarValueError(
                    'Unsupported CALSCALE: Debacal can interpret Gregorian calendars only. '
                    'The original resource is retained; its dates have not been converted.')
    lexical = fallback.components(data)
    referenced = {params['TZID'] for component in lexical for key in _DATE_PROPERTIES
                  for params, value in component.get(key, [])
                  if _references_timezone(params, value)}
    definitions = _timezone_definitions(blocks, referenced)
    events = (block for block in blocks if block[0] == 'BEGIN:VEVENT')
    for component, block in zip(lexical, events, strict=True):
        component['_timezone_definitions'] = definitions
        if 'BEGIN:VALARM' in block:
            component['_alarm_source'] = block
    if not use_full or ICalendar is None:
        return lexical
    # The library's global TZID cache can substitute another resource's rules.
    # Parse ordinary properties with the library, but decode time properties
    # ourselves from the original lexical values and this resource's context.
    # Unused malformed VTIMEZONEs must not poison unrelated event components.
    neutral = ['BEGIN:VCALENDAR', *header]
    for block in blocks:
        if block[0] != 'BEGIN:VEVENT':
            continue
        for line in without_alarms(block):
            if not line:
                continue
            key, params, value = fallback.content_line(line)
            if key == alarm_fold_ics.ORIGINAL:
                continue  # Decode this optional private payload independently of the event.
            if key == 'RDATE' and (params.get('VALUE') == 'PERIOD' or '/' in value):
                # Periods are decoded from their lexical form below. Letting
                # the library discard an invalid value would hide the issue.
                continue
            if key in _DATE_PROPERTIES:
                params.pop('TZID', None)
                line = key + ''.join(';' + name + '=' + fallback.parameter(item) for name, item in params.items()) + ':' + value
            neutral.append(line)
    neutral.append('END:VCALENDAR')
    calendar = ICalendar.from_ical(fallback.folded(neutral))
    if calendar.name != "VCALENDAR":
        raise ValueError("Expected a VCALENDAR document.")
    originals = calendar.walk('VEVENT')
    if len(lexical) != len(originals):
        raise ValueError('Could not match events to their original components.')
    result = []
    for index, original in enumerate(originals):
        component = {"_decoded": {}, '_timezone_definitions': definitions}
        if '_alarm_source' in lexical[index]:
            component['_alarm_source'] = lexical[index]['_alarm_source']
        if alarm_fold_ics.ORIGINAL in lexical[index]:
            component[alarm_fold_ics.ORIGINAL] = lexical[index][alarm_fold_ics.ORIGINAL]
        for key, values in original.items():
            if key in {'SUMMARY', 'DESCRIPTION', 'LOCATION'} and any(
                    str(params.get('ENCODING', '')).upper() == 'QUOTED-PRINTABLE'
                    for params, _ in lexical[index].get(key, ())):
                # Decode the declared transfer encoding before TEXT escapes.
                # Library decode/to_ical round-trips can otherwise insert
                # escapes into QP text and change its meaning a second time.
                component[key] = lexical[index][key]
                continue
            if key in _DATE_PROPERTIES or key == 'DURATION':
                component[key] = lexical[index].get(key, [])
                continue
            values = values if isinstance(values, list) else [values]
            for value in values:
                raw = value.to_ical()
                component.setdefault(key, []).append((dict(value.params), raw.decode() if isinstance(raw, bytes) else str(raw)))
                if hasattr(value, "dts"):
                    component["_decoded"].setdefault(key, []).extend(v.dt for v in value.dts)
                elif hasattr(value, "dt"):
                    component["_decoded"].setdefault(key, []).append(value.dt)
        for key in _DATE_PROPERTIES | {'DURATION'}:
            if key in lexical[index]:
                component[key] = lexical[index][key]
        result.append(component)
    return result


def _datetime(value, tz):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def _decoded(component, key, tz):
    values = component.get("_decoded", {}).get(key)
    if values is not None:
        return values
    result, date_context = [], None
    for parameters, raw in component.get(key, []):
        for piece in raw.split(","):
            if parameters.get('VALUE') == 'PERIOD' or '/' in piece:
                if key != 'RDATE':
                    raise PeriodError('Only RDATE can contain recurrence periods.')
                result.append(_decode_period(piece, parameters, tz, component.get('_timezone_definitions')).start)
                continue
            decoded_parameters = parameters
            if (key in {'RDATE', 'EXDATE'} and parameters.get('VALUE') in {None, 'DATE'}
                    and re.fullmatch(r'\d{8}Z', piece)):
                # Historical Google birthday exports append a meaningless UTC
                # suffix to recurrence DATEs. Recover only within a proven
                # all-day context, never by inventing a time for timed series.
                if date_context is None:
                    starts = component.get('DTSTART', [])
                    date_context = False
                    if len(starts) == 1:
                        start_parameters, start_value = starts[0]
                        _start, date_context = fallback.dt(start_value, start_parameters, tz,
                                                           component.get('_timezone_definitions'))
                if date_context:
                    piece = piece[:-1]
                    decoded_parameters = dict(parameters) | {'VALUE': 'DATE'}
            decoded, all_day = fallback.dt(piece, decoded_parameters, tz, component.get('_timezone_definitions'))
            result.append(decoded.date() if all_day else decoded)
    return result


def _decode_period(raw, parameters, tz, definitions):
    """Decode RFC 5545 PERIOD without replacing its occurrence-specific range."""
    start_text, separator, endpoint = raw.partition('/')
    if not separator or not start_text or not endpoint or '/' in endpoint:
        raise PeriodError('An RDATE period needs a start date-time and an end date-time or positive duration.')
    parameters = dict(parameters) | {'VALUE': 'DATE-TIME'}
    try:
        start, start_date = fallback.dt(start_text, parameters, tz, definitions)
        if start_date:
            raise PeriodError('RDATE periods use date-times, not all-day DATE values.')
        floating = not parameters.get('TZID') and not start_text.upper().endswith('Z')
        spec = ''
        if endpoint.upper().startswith(('P', '+P', '-P')):
            days, seconds = parse_duration_spec(endpoint.upper())
            if days == seconds == 0:
                raise PeriodError('An RDATE period duration must be positive.')
            end = (start + timedelta(days=days, seconds=seconds) if floating
                   else add_duration(start, endpoint.upper()))
            spec = normalized_spec(endpoint.upper())
        else:
            end, end_date = fallback.dt(endpoint, parameters, tz, definitions)
            if end_date:
                raise PeriodError('RDATE periods use date-times, not all-day DATE values.')
            end_floating = not parameters.get('TZID') and not endpoint.upper().endswith('Z')
            if floating != end_floating:
                raise PeriodError('An RDATE period must use compatible start and end timezone forms.')
        valid = end.replace(tzinfo=None) > start.replace(tzinfo=None) if floating else end.timestamp() > start.timestamp()
        if not valid:
            raise PeriodError('The end of an RDATE period must follow its start.')
    except (TimezoneError, PeriodError):
        raise
    except (ValueError, TypeError, OverflowError):
        raise PeriodError('An RDATE period contains an invalid date-time or duration.') from None
    identifier = '' if floating or start_text.upper().endswith('Z') else str(parameters.get('TZID', ''))
    return _RecurrencePeriod(start, end, spec, floating, identifier)


def _period_overrides(component, master, tz):
    from .recurrence import occurrence_id
    result = {}
    for parameters, raw in component.get('RDATE', []):
        if parameters.get('VALUE') != 'PERIOD' and '/' not in raw:
            continue
        if master.all_day:
            raise PeriodError('RDATE periods require a timed DTSTART.')
        for value in raw.split(','):
            period = _decode_period(value, parameters, tz, component.get('_timezone_definitions'))
            if period.floating_time != master.floating_time:
                raise PeriodError('An RDATE period must use the same floating or fixed time mode as DTSTART.')
            key = _key(period.start, tz, master.floating_time)
            override = replace(master, id=occurrence_id(master.id, key), start=period.start, end=period.end,
                               all_day=False, timezone_id=period.timezone_id, floating_time=period.floating_time,
                               duration_spec=period.duration_spec,
                               recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                               raw_ics='', series_id=master.id, recurrence_id=key,
                               schedule_expression='', expression_anchor='', schedule_syntax=1, range_policy='')
            if key in result and Event.from_dict(result[key]).end.timestamp() != override.end.timestamp():
                raise PeriodError('Conflicting RDATE periods assign different ranges to the same occurrence.')
            result.setdefault(key, materialize_alarms(master, override).to_dict())
    return result


def _key(value, tz, floating=False):
    from .recurrence import recurrence_key
    return recurrence_key(value, not isinstance(value, datetime), floating_time=floating)


def _floating(component, key='DTSTART'):
    params, value = component[key][0]
    return params.get('VALUE') != 'DATE' and len(value) != 8 and not params.get('TZID') and not value.upper().endswith('Z')


def _event_title(component, default='Untitled event'):
    value = fallback.prop(component, 'SUMMARY', default)
    return value if value.strip() else 'Untitled event'


def _used_definitions(components):
    result, inspected = {}, set()
    for component in components:
        definitions = component.get('_timezone_definitions', {})
        for key in _DATE_PROPERTIES:
            for params, value in component.get(key, []):
                identifier = params.get('TZID')
                if identifier in inspected or identifier not in definitions or not _references_timezone(params, value):
                    continue
                # Every component in this UID shares one resource context.
                # Detached instances can reference it thousands of times.
                inspected.add(identifier)
                definition = definitions[identifier]
                block = tuple(fallback.unfolded(definition))
                # Native zones generated by this codec remain native model
                # zones; preserving their raw blocks still makes exports portable.
                if _generated_timezone_coverage(block) is None:
                    result[identifier] = definition
    return result


def _component_end(component, start, all_day, tz, *, inherited=None, duration_zone=None):
    if 'DTEND' in component:
        if len(component['DTEND']) != 1:
            raise ValueError('An event must have only one DTEND.')
        endpoint = _decoded(component, 'DTEND', tz)[0]
        if (not isinstance(endpoint, datetime)) != all_day:
            raise fallback.CalendarValueError('DTEND must use the same DATE or DATE-TIME value type as DTSTART.')
        end = _datetime(endpoint, tz)
        if 'DURATION' in component:
            # Legacy generators sometimes redundantly emit both. Recover only
            # when each independently specifies the same end; disagreements
            # must remain available for review rather than choosing a winner.
            duration_component = {key: value for key, value in component.items() if key != 'DTEND'}
            duration_end, _ = _component_end(duration_component, start, all_day, tz,
                                            inherited=inherited, duration_zone=duration_zone)
            floating = _floating(component) if 'DTSTART' in component else bool(inherited and inherited.floating_time)
            if all_day:
                matches = end.date() == duration_end.date()
            elif floating:
                matches = _floating(component, 'DTEND') and end.replace(tzinfo=None) == duration_end.replace(tzinfo=None)
            else:
                matches = not _floating(component, 'DTEND') and end.timestamp() == duration_end.timestamp()
            if not matches:
                raise fallback.CalendarValueError('DTEND and DURATION specify different event ends. View the original resource to choose the intended duration.')
            nominal_days, _seconds = parse_duration_spec(fallback.prop(component, 'DURATION'))
            if (nominal_days and not all_day and not floating
                    and ('RRULE' in component or 'RDATE' in component)
                    and not isinstance(duration_zone or start.tzinfo, timezone)):
                # Agreement at the anchor does not prove agreement at later
                # DST changes: DTEND retains elapsed duration, while P1D/P1W
                # retain nominal calendar days. Only known fixed-offset zones
                # make those declarations interchangeable for a recurrence.
                raise fallback.CalendarValueError('DTEND and DURATION agree for the first occurrence but may differ across timezone changes. Remove one declaration to choose the intended recurring duration.')
        return end, ''
    if 'DURATION' in component:
        if len(component['DURATION']) != 1:
            raise ValueError('An event must have only one DURATION.')
        value = fallback.prop(component, 'DURATION')
        days, seconds = parse_duration_spec(value)
        if all_day and (seconds or not days):
            raise ValueError('An all-day event duration must use positive whole days or weeks.')
        floating = _floating(component) if 'DTSTART' in component else bool(inherited and inherited.floating_time)
        if not all_day and floating:
            return start + timedelta(days=days, seconds=seconds), normalized_spec(value)
        cursor = start if all_day or duration_zone is None else start.astimezone(duration_zone)
        return add_duration(cursor, value), '' if all_day else normalized_spec(value)
    if inherited is not None:
        return end_for(inherited, start), inherited.duration_spec if not all_day else ''
    return start + (timedelta(days=1) if all_day else timedelta(0)), ''


def _schedule_syntax(component):
    try:
        version = int(fallback.prop(component, "X-DEBACAL-PATTERN-VERSION", "1"))
    except (ValueError, TypeError):
        raise ValueError("Custom schedule syntax version must be 1 or 2.") from None
    if version not in {1, 2}:
        raise ValueError("Custom schedule syntax version must be 1 or 2.")
    return version


def _blocks(data):
    """Return complete top-level blocks; never drop unclosed components."""
    lines = fallback.unfolded(data)
    header, blocks, current, stack = [], [], [], []
    seen_calendar = False
    for line in lines:
        key, separator, value = line.partition(":")
        marker = key.upper() if separator else ""
        if marker == "BEGIN":
            name = value.upper()
            if not stack:
                if name != "VCALENDAR" or seen_calendar:
                    raise ValueError("Expected one VCALENDAR envelope.")
                seen_calendar = True
                stack.append(name)
                continue
            stack.append(name)
            current.append("BEGIN:" + name)
        elif marker == "END":
            name = value.upper()
            if not stack or stack[-1] != name:
                raise ValueError("Mismatched iCalendar component boundary.")
            if len(stack) == 1:
                stack.pop()
                continue
            current.append("END:" + name)
            stack.pop()
            if len(stack) == 1:
                blocks.append(current)
                current = []
        elif len(stack) == 1:
            header.append(line)
        elif stack:
            current.append(line)
        elif line.strip():
            raise ValueError("Unexpected data outside VCALENDAR.")
    if stack or not seen_calendar:
        raise ValueError("Unclosed iCalendar component.")
    return header, blocks


def _generated_block(block):
    markers = (range_codec.GENERATED, alarm_rdate_ics.GENERATED)
    return any(line.upper().startswith(markers) for line in block) and any(
        _property_of(block, name) is not None for name in markers)


def _property_of(block, name):
    depth = 0
    for line in block:
        try:
            marker, _, value = fallback.content_line(line)
        except ValueError:
            continue  # Decode failures belong to the containing UID's report.
        if marker == "BEGIN":
            depth += 1
            continue
        if marker == "END":
            depth -= 1
            continue
        if depth == 1 and marker == name:
            return value
    return None


def _uid_of(block):
    return fallback.text(_property_of(block, "UID") or "")


def _with_uid(block, uid):
    """Replace an empty event UID while keeping nested alarm UIDs intact."""
    result, depth = [block[0], "UID:" + uid], 1
    for line in block[1:]:
        left, _, value = line.partition(":")
        marker = left.upper()
        if depth == 1 and left.split(";")[0].upper() == "UID":
            continue
        result.append(line)
        if marker == "BEGIN":
            depth += 1
        elif marker == "END":
            depth -= 1
    return result


def _resource_for_uid(data, uid, *, synthetic=False, discard_generated=(), definitions=None, floating=False, context=None):
    # Bulk imports already split the source into complete resource blocks.
    # Reusing that structure avoids reparsing every UID document and its
    # shared timezone definitions a second time.
    header, blocks = _blocks(data) if context is None else context
    selected_events = [b for b in blocks if b[0] == 'BEGIN:VEVENT'
                       and (_uid_of(b) == uid or (synthetic and not _uid_of(b)))]
    selected_ids = {id(block) for block in selected_events}
    referenced = set()
    for block in selected_events:
        for line in block:
            if ';TZID=' in line.upper():
                try:
                    _, params, _ = fallback.content_line(line)
                except ValueError:
                    continue  # Malformed retained alarm properties have their own diagnostic.
                if params.get('TZID'):
                    referenced.add(params['TZID'])
    selected = [b for b in blocks if (b[0] == 'BEGIN:VTIMEZONE'
                and fallback.text(_property_of(b, 'TZID') or '') in referenced) or id(b) in selected_ids]
    if discard_generated:
        from .ics import local_timezone
        tz, omitted = local_timezone(), set(discard_generated)
        retained = []
        for block in selected:
            if block[0] == 'BEGIN:VEVENT' and _generated_block(block):
                component = fallback.components('\n'.join(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
                component['_timezone_definitions'] = definitions or {}
                if 'RECURRENCE-ID' in component and _key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz, floating) in omitted:
                    continue
            retained.append(block)
        selected = retained
    return fallback.folded(["BEGIN:VCALENDAR", *header, *(line for block in selected for line in block), "END:VCALENDAR"]).decode()


def _component_alarms(component, uid, recurrence_id=''):
    lines = component.get('_alarm_source')
    return read_alarms('\n'.join(lines), component_key=uid + '/' + recurrence_id) if lines else ()


def parse(data, calendar_id, window_start=None, window_end=None, *, limit=10000,
          read_only_recurring=False, use_full=True, _source=None, _resource_context=None):
    """Import series without materializing any recurrence occurrences.

    Window parameters remain accepted for API compatibility, but do not truncate
    the preserved recurrence. The display layer chooses its expansion window.
    """
    from .ics import local_timezone
    from .recurrence import canonical_rule, occurrence_id, recurrence_set
    tz = local_timezone()
    source = _read(data, use_full) if _source is None else _source
    generated_count = sum(range_codec.GENERATED in component or alarm_rdate_ics.GENERATED in component
                          for component in source)
    if len(source) - generated_count > limit or generated_count > range_codec.MAX_GENERATED_RANGES:
        raise ValueError(f"Import exceeds {limit:,} event components.")
    generated_blocks = {}
    if any(range_codec.GENERATED in component for component in source):
        _, raw_blocks = _blocks(data)
        definitions = {identifier: definition for component in source
                       for identifier, definition in component.get('_timezone_definitions', {}).items()}
        floating_uids = {fallback.prop(component, 'UID'): _floating(component)
                         for component in source if 'RECURRENCE-ID' not in component and 'DTSTART' in component}
        for block in raw_blocks:
            if block[0] != 'BEGIN:VEVENT' or _property_of(block, range_codec.GENERATED) is None:
                continue
            raw_component = fallback.components('\n'.join(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
            raw_component['_timezone_definitions'] = definitions
            if 'RECURRENCE-ID' in raw_component:
                key = _key(_decoded(raw_component, 'RECURRENCE-ID', tz)[0], tz,
                           floating_uids.get(_uid_of(block), False))
                slot = (_uid_of(block), key)
                generated_blocks[slot] = block if slot not in generated_blocks else None
    grouped = {}
    for component in source:
        if "EXRULE" in component:
            raise fallback.CalendarValueError('This event uses legacy EXRULE exclusions, which Debacal cannot yet import. '
                                              'Export exclusions as EXDATE in the source application, or retain the original for review.')
        if len(component.get("RRULE", [])) > 1:
            raise fallback.CalendarValueError('This event contains multiple RRULE properties. '
                                              'Debacal cannot safely choose which recurrence rule to use; the original is retained.')
        if any(str(params.get("RANGE", "")).upper() == "THISANDFUTURE"
               for params, _ in component.get("RECURRENCE-ID", [])):
            raise fallback.CalendarValueError('This series changes this and all future occurrences '
                                              '(RECURRENCE-ID;RANGE=THISANDFUTURE), which Debacal cannot yet import. '
                                              'The original series is retained for review.')
        uid = fallback.prop(component, "UID") or str(uuid5(NAMESPACE_URL, repr(component)))
        grouped.setdefault(uid, []).append(component)
    result = []
    for uid, components in grouped.items():
        masters = [c for c in components if "RECURRENCE-ID" not in c]
        if not masters:
            raise fallback.CalendarValueError('These are detached occurrences without their original recurring event. '
                                              'Import the complete series from its source; the detached data is retained for review.')
        if len(masters) > 1:
            raise fallback.CalendarValueError('Several master events use the same UID. '
                                              'Debacal cannot safely choose a version; the original resource is retained.')
        master_component = masters[0]
        if fallback.prop(master_component, "STATUS").upper() == "CANCELLED":
            continue
        if "DTSTART" not in master_component:
            raise ValueError("An event is missing DTSTART.")
        if len(master_component['DTSTART']) != 1:
            raise fallback.CalendarValueError('An event must have exactly one DTSTART.')
        raw_start = _decoded(master_component, "DTSTART", tz)[0]
        all_day = not isinstance(raw_start, datetime)
        event_start = _datetime(raw_start, tz)
        # DATE without an endpoint lasts one floating day; DATE-TIME is a point.
        event_end, duration_spec = _component_end(master_component, event_start, all_day, tz)
        event_id = str(uuid5(NAMESPACE_URL, f"debacal:{calendar_id}:{uid}:"))
        if fallback.prop(master_component, "X-DEBACAL-CALENDAR") == calendar_id:
            event_id = fallback.prop(master_component, "X-DEBACAL-ID", event_id)
        params = master_component["DTSTART"][0][0]
        floating = not all_day and _floating(master_component)
        timezone_id = ('' if all_day or floating else
                       'UTC' if master_component['DTSTART'][0][1].upper().endswith('Z') else str(params.get('TZID', '')))
        definitions = _used_definitions(components)
        event_start, event_end, timezone_id, alarms = alarm_fold_ics.restore_alarms(
            master_component, uid + '/', event_start, event_end, timezone_id, _component_alarms(master_component, uid))
        master = Event(
            id=event_id, calendar_id=calendar_id,
            title=_event_title(master_component), start=event_start, end=event_end,
            description=fallback.prop(master_component, "DESCRIPTION"), location=fallback.prop(master_component, "LOCATION"),
            group_id=fallback.prop(master_component, "X-DEBACAL-GROUP") or None,
            candidate=fallback.prop(master_component, "X-DEBACAL-CANDIDATE").upper() == "TRUE",
            all_day=all_day, ical_uid=uid, timezone_id=timezone_id,
            timezone_definitions=definitions, floating_time=floating,
            duration_spec=duration_spec, alarms=alarms,
            recurrence_rule=fallback.prop(master_component, "RRULE"),
            recurrence_dates=tuple(_key(value, tz, floating) for value in _decoded(master_component, "RDATE", tz)),
            excluded_dates=tuple(_key(value, tz, floating) for value in _decoded(master_component, "EXDATE", tz)),
            raw_ics='',
            schedule_expression=fallback.prop(master_component, "X-DEBACAL-PATTERN"),
            expression_anchor=fallback.prop(master_component, "X-DEBACAL-PATTERN-ANCHOR"),
            schedule_syntax=_schedule_syntax(master_component),
            range_policy='cut' if fallback.prop(master_component, range_codec.POLICY).lower() == 'cut' else '',
        )
        master = replace(master, recurrence_rule=canonical_rule(master.recurrence_rule, master))
        # A foreign cancellation changes the canonical base just like EXDATE.
        # Include it before checking the signature, rather than regenerating a
        # range that another client intentionally removed.
        cancelled = {_key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz, floating)
                     for component in components if 'RECURRENCE-ID' in component
                     and fallback.prop(component, 'STATUS').upper() == 'CANCELLED'}
        if cancelled:
            master = replace(master, excluded_dates=tuple(set(master.excluded_dates) | cancelled))
        expected = {}
        policy_fingerprint = range_codec.signature(master) if master.range_policy else ''
        verified_policy = (master.range_policy == 'cut'
                           and fallback.prop(master_component, range_codec.SIGNATURE) == policy_fingerprint)
        current_marker = range_codec.generated_marker(master, policy_fingerprint) if verified_policy else None
        if verified_policy:
            expected = range_codec.expected_ranges(master)
            present = {_key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz, floating)
                       for component in components if 'RECURRENCE-ID' in component}
            verified_policy = expected.keys() <= present
        if not verified_policy:
            master = replace(master, range_policy='')
        discarded = set()
        overrides, exclusions = _period_overrides(master_component, master, tz), set(master.excluded_dates)
        rdate_sources = {}
        zoned_master = alarm_rdate_ics.candidate(master, master_component)
        for component in components:
            if "RECURRENCE-ID" not in component:
                continue
            key = _key(_decoded(component, "RECURRENCE-ID", tz)[0], tz, floating)
            tagged = generated_blocks.get((uid, key))
            if verified_policy and tagged and key in expected:
                marker = fallback.prop(component, range_codec.GENERATED)
                if marker == policy_fingerprint:
                    generated = (range_codec.safe_generated_block(tagged)
                                 and range_codec.matches_generated(component, master, key, expected,
                                     decode=_decoded, tz=tz, fingerprint=policy_fingerprint))
                elif marker == current_marker:
                    expected_block = _cut_child_block(master, uid, key, *expected[key], marker)
                    generated = alarm_rdate_ics.matches_child(tagged, expected_block)
                else:
                    generated = False
                if generated:
                    discarded.add(key)
                    continue
            if fallback.prop(component, "STATUS").upper() == "CANCELLED":
                exclusions.add(key)
                continue
            if 'DTSTART' in component and len(component['DTSTART']) != 1:
                raise fallback.CalendarValueError('An event must have exactly one DTSTART.')
            inherited = Event.from_dict(overrides[key]) if key in overrides else master
            raw_override_start = _decoded(component, 'DTSTART' if 'DTSTART' in component else 'RECURRENCE-ID', tz)[0]
            override_all_day = not isinstance(raw_override_start, datetime)
            override_floating = not override_all_day and (_floating(component) if 'DTSTART' in component else floating)
            override_start = _datetime(raw_override_start, tz)
            override_zone = override_start.tzinfo
            override_timezone_id = ('' if override_all_day or override_floating else
                                    'UTC' if component.get('DTSTART', [({}, '')])[0][1].upper().endswith('Z') else
                                    str(component.get('DTSTART', [({}, '')])[0][0].get('TZID', '')) or master.timezone_id)
            if 'DTSTART' not in component:
                from .recurrence import event_zone
                override_start = inherited.start if key in overrides else override_start
                override_zone, override_timezone_id = event_zone(inherited), inherited.timezone_id
                override_floating = inherited.floating_time
            override_end, override_spec = _component_end(component, override_start, override_all_day, tz,
                inherited=inherited if 'DTSTART' not in component else None, duration_zone=override_zone)
            override_start, override_end, override_timezone_id, alarms = alarm_fold_ics.restore_alarms(
                component, uid + '/' + key, override_start, override_end, override_timezone_id,
                _component_alarms(component, uid, key))
            override = replace(
                master, id=occurrence_id(master.id, key), start=override_start, end=override_end,
                title=_event_title(component, master.title),
                description=fallback.prop(component, "DESCRIPTION", master.description),
                location=fallback.prop(component, "LOCATION", master.location),
                all_day=override_all_day,
                floating_time=override_floating,
                duration_spec=override_spec, timezone_id=override_timezone_id, alarms=alarms,
                recurrence_rule="", recurrence_dates=(), excluded_dates=(), recurrence_overrides={}, raw_ics="",
                series_id=master.id, recurrence_id=key,
                schedule_expression="", expression_anchor="",
                schedule_syntax=1,
                range_policy='',
            )
            overrides[key] = override.to_dict()
            if zoned_master is not None:
                unique = key not in rdate_sources and alarm_rdate_ics.GENERATED in component
                rdate_sources[key] = component.get('_alarm_source') if unique else None
        if zoned_master is not None:
            verified = alarm_rdate_ics.verified_children(zoned_master, overrides, rdate_sources, _rdate_child_block)
            if verified is not None:
                master = zoned_master
                for key in verified:
                    del overrides[key]
                discarded.update(verified)
        master = replace(master, recurrence_overrides=overrides, excluded_dates=tuple(sorted(exclusions)))
        master = replace(master, raw_ics=_resource_for_uid(data, uid, synthetic='UID' not in master_component,
                                                          discard_generated=discarded,
                                                          definitions=master.timezone_definitions, floating=master.floating_time,
                                                          context=_resource_context))
        master = replace(master, recurrence_rule=canonical_rule(master.recurrence_rule, master))
        recurrence_set(master)  # Validate before an import reaches the SQLite transaction.
        result.append(master)
    return sorted(result, key=lambda event: (event.start.timestamp(), event.id))


def _date_property(key, value, *, floating=False):
    if len(value) == 10:
        return f"{key};VALUE=DATE:{date.fromisoformat(value):%Y%m%d}"
    parsed = datetime.fromisoformat(value)
    if floating:
        return f"{key}:{parsed:%Y%m%dT%H%M%S}"
    if parsed.tzinfo is None:
        raise ValueError("Recurrence dates must include a timezone.")
    return f"{key}:{parsed.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def rekey_recurrence_templates(event, transform):
    """Keep detached raw metadata aligned with a projected recurrence ID.

    Structured exceptions remain authoritative for their event fields. Their
    retained templates, including cancelled components, must follow the same
    recurrence-key transform so export can still find the original metadata.
    """
    raw = event.raw_ics
    if not raw:
        return raw
    from .recurrence import event_zone
    zone = event_zone(event)
    header, blocks = _blocks(raw)
    projected, changed = [], False
    for block in blocks:
        if block[0] != "BEGIN:VEVENT" or _uid_of(block) != event.ical_uid:
            projected.append(block)
            continue
        component = fallback.components(fallback.folded(["BEGIN:VCALENDAR", *block, "END:VCALENDAR"]))[0]
        component['_timezone_definitions'] = event.timezone_definitions
        if "RECURRENCE-ID" not in component:
            projected.append(block)
            continue
        key = _key(_decoded(component, "RECURRENCE-ID", zone)[0], zone, event.floating_time)
        moved_key = transform(key)
        if moved_key == key:
            projected.append(block)
            continue
        replacement = _date_property("RECURRENCE-ID", moved_key, floating=event.floating_time)
        moved, depth = [], 0
        for line in block:
            left, _, value = line.partition(":")
            marker = left.upper()
            if marker == "BEGIN":
                depth += 1
            elif marker == "END":
                depth -= 1
            if depth == 1 and left.split(";", 1)[0].upper() == "RECURRENCE-ID":
                moved.append(replacement)
            else:
                moved.append(line)
        projected.append(moved)
        changed = True
    if not changed:
        return raw
    return fallback.folded(["BEGIN:VCALENDAR", *header,
                            *(line for block in projected for line in block), "END:VCALENDAR"]).decode()


def _named_start(event):
    # A v2 literal uses calendar-day/month steps in its fixed event's zone.
    # Preserve that zone even when its interoperable schedule uses UTC RDATEs.
    authored_v2 = bool(event.schedule_expression and event.schedule_syntax == 2)
    zoned_alarms = event.alarms is not None and any(
        'D' in (duration or '').upper() or 'W' in (duration or '').upper()
        for alarm in event.alarms for duration in (alarm.trigger, alarm.repeat_interval))
    return bool((event.recurrence_rule or authored_v2 or event.range_policy or event.duration_spec or event.timezone_definitions or zoned_alarms or event.end_timezone_id) and not event.all_day and not event.floating_time
                and event.timezone_id and (event.timezone_id not in {'UTC', 'Etc/UTC'}
                                          or event.timezone_id in event.timezone_definitions))


def _known_lines(event, uid, recurrence_id="", *, recurrence_floating=False, named_detached=False):
    lines = fallback.event_lines(event)[1:-1]
    lines = [line for line in lines if not line.startswith("UID:")]
    end_zone = event.end_timezone_id
    nominal_duration = bool(event.duration_spec and not event.all_day and not recurrence_id)
    named_start = (_named_start(event) or named_detached and not event.all_day
                   and not event.floating_time and bool(event.timezone_id))
    if event.floating_time:
        lines = [line for line in lines if not line.startswith(('DTSTART:', 'DTEND:'))]
        lines.append(f'DTSTART:{event.start:%Y%m%dT%H%M%S}')
        if event.end.replace(tzinfo=None) > event.start.replace(tzinfo=None):
            lines.append(f'DTEND:{event.end:%Y%m%dT%H%M%S}')
    fixed_late_start = False
    # An explicit set of instants with an elapsed duration has no wall-clock
    # recurrence to anchor. Its primary can use UTC just like a standalone
    # event; writing a TZID wall time here would select the earlier fold.
    wall_schedule = event.recurring and (event.recurrence_rule or event.range_policy or nominal_duration
                                         or event.schedule_expression and event.schedule_syntax == 2)
    if (nominal_duration or named_start) and not wall_schedule and not event.floating_time:
        local_start = event.start.astimezone(event_timezone(event))
        fixed_late_start = bool(local_start.fold and local_start.replace(fold=0).timestamp() != local_start.timestamp())
        if fixed_late_start:
            nominal_duration = False  # One fixed range can use exact UTC endpoints.
    if (not recurrence_id or end_zone or named_detached) and not fixed_late_start and named_start:
        zone = event_timezone(event)
        lines = [line for line in lines if not line.startswith(("DTSTART:", "DTEND:"))]
        lines.append(f"DTSTART;TZID={fallback.parameter(event.timezone_id)}:{event.start.astimezone(zone):%Y%m%dT%H%M%S}")
        if event.end.timestamp() > event.start.timestamp():
            local_end = event.end.astimezone(zone)
            if local_end.fold and local_end.replace(fold=0).timestamp() != local_end.timestamp():
                # A TZID clock alone denotes the first repeated-hour instant.
                # Keep the selected endpoint unambiguous, including ranges
                # whose written end clock precedes their written start clock.
                if (event.recurrence_rule and not recurrence_id and not end_zone and not nominal_duration
                        and any(alarm_fold_ics.needs_projection(a, {'END'}) for a in event.alarms or ())):
                    # An elapsed DURATION reaches the late end in its own
                    # scheduling zone for every instance of the rule.
                    seconds = int(event.end.timestamp() - event.start.timestamp())
                    lines.append(f'DURATION:PT{seconds}S')
                else:
                    lines.append(f"DTEND:{event.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}")
            else:
                lines.append(f"DTEND;TZID={fallback.parameter(event.timezone_id)}:{local_end:%Y%m%dT%H%M%S}")
    if nominal_duration:
        lines = [line for line in lines if not line.startswith(('DTEND:', 'DTEND;'))]
        lines.append('DURATION:' + event.duration_spec)
    elif end_zone and event.end.timestamp() > event.start.timestamp():
        lines = [line for line in lines if not line.startswith(('DTEND:', 'DTEND;'))]
        if ((end_zone in {'UTC', 'Etc/UTC'} and end_zone not in event.timezone_definitions)
                or event.end.fold and event.end.replace(fold=0).timestamp() != event.end.timestamp()):
            lines.append(f'DTEND:{event.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}')
        else:
            lines.append(f'DTEND;TZID={fallback.parameter(end_zone)}:{event.end:%Y%m%dT%H%M%S}')
    lines.insert(0, "UID:" + fallback.escaped(uid))
    if event.schedule_expression and not recurrence_id:
        lines.append("X-DEBACAL-PATTERN:" + fallback.escaped(event.schedule_expression))
        lines.append("X-DEBACAL-PATTERN-VERSION:" + str(event.schedule_syntax))
    if event.expression_anchor and not recurrence_id:
        lines.append("X-DEBACAL-PATTERN-ANCHOR:" + fallback.escaped(event.expression_anchor))
    if event.range_policy and not recurrence_id:
        lines.extend((range_codec.POLICY + ':' + event.range_policy,
                      range_codec.SIGNATURE + ':' + range_codec.signature(event)))
    if recurrence_id:
        lines.append(_date_property("RECURRENCE-ID", recurrence_id, floating=recurrence_floating))
    elif event.recurrence_rule:
        from .recurrence import canonical_rule
        rule = canonical_rule(event.recurrence_rule, event)
        if any(char in rule for char in "\r\n"):
            raise ValueError("A recurrence rule must be a single line.")
        if event.all_day:
            zone = event_timezone(event)
            def date_until(match):
                value = match[1]
                if "T" not in value:
                    return "UNTIL=" + value
                parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                return "UNTIL=" + parsed.astimezone(zone).strftime("%Y%m%d")
            rule = re.sub(r"UNTIL=([^;]+)", date_until, rule)
        lines.append("RRULE:" + rule)
    if not recurrence_id:
        lines.extend(_date_property("RDATE", value, floating=event.floating_time) for value in event.recurrence_dates)
        lines.extend(_date_property("EXDATE", value, floating=event.floating_time) for value in event.excluded_dates)
    return lines


@lru_cache(maxsize=64)
def _timezone_definition(timezone_id, first_date, last_date):
    try:
        from icalendar import Timezone
    except ImportError:
        raise ValueError("Exporting a named-timezone recurrence needs the 'icalendar' dependency to generate VTIMEZONE. "
                         "Install the project dependencies, or retain an imported timezone definition.") from None
    from .timezone_generation import generate_verified_timezone
    zone = resolve_timezone(timezone_id)
    generated = generate_verified_timezone(zone, timezone_id, first_date, last_date)
    return fallback.unfolded(generated.to_ical())


def _patch_block(block, event, uid, recurrence_id="", *, recurrence_floating=False, utc_range=False, named_detached=False):
    known = {"UID", "SUMMARY", "DTSTART", "DTEND", "DURATION", "DTSTAMP", "DESCRIPTION", "LOCATION",
             "X-DEBACAL-ID", "X-DEBACAL-CALENDAR", "X-DEBACAL-GROUP", "X-DEBACAL-CANDIDATE",
             "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"}
    known.update({"X-DEBACAL-PATTERN", "X-DEBACAL-PATTERN-ANCHOR", "X-DEBACAL-PATTERN-VERSION"})
    known.update(range_codec.PROPERTIES)
    if event.alarms is not None:
        known.update({alarm_fold_ics.ANCHOR, alarm_fold_ics.ORIGINAL, alarm_rdate_ics.ZONE, alarm_rdate_ics.GENERATED})
    result, nested = ["BEGIN:VEVENT"], 0
    for line in block[1:-1]:
        key = line.split(":", 1)[0].split(";")[0].upper()
        if line.startswith("BEGIN:"):
            nested += 1
        if nested or key not in known:
            result.append(line)
        if line.startswith("END:"):
            nested -= 1
    range_event = alarm_rdate_ics.utc_range(event) if utc_range else event
    result.extend(_known_lines(range_event, uid, recurrence_id, recurrence_floating=recurrence_floating,
                               named_detached=named_detached))
    result.append('END:VEVENT')
    if event.alarms is not None and (len(event.alarms) or 'BEGIN:VALARM' in result):
        alarms, annotations = alarm_fold_ics.project_alarms(event, result, uid + '/' + recurrence_id)
        result[-1:-1] = annotations
        result = fallback.unfolded(write_alarms('\n'.join(result), alarms))
    return result


def _cut_child_block(event, uid, key, start, end, marker, template=None):
    from .event_duration import occurrence_end_zone
    from .recurrence import occurrence_id
    child = replace(event, id=occurrence_id(event.id, key), start=start, end=occurrence_end_zone(event, end),
                    recurrence_rule='', recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                    series_id=event.id, recurrence_id=key, range_policy='', schedule_expression='',
                    expression_anchor='', schedule_syntax=1, raw_ics='', duration_spec='')
    child = materialize_alarms(event, child) if event.alarms else child
    block = _patch_block(template or ['BEGIN:VEVENT', 'END:VEVENT'], child, uid, key,
                         recurrence_floating=event.floating_time)
    block.insert(-1, range_codec.GENERATED + ':' + marker)
    return block


def _rdate_child_block(child, uid, key):
    block = _patch_block(['BEGIN:VEVENT', 'END:VEVENT'], child, uid, key, utc_range=True)
    block.insert(-1, alarm_rdate_ics.GENERATED + ':1')
    return block


def _timezone_span(event):
    return (date(max(1, min(1970, event.start.year - 1)), 1, 1),
            date(min(9999, max(2101, event.end.year + 2)), 1, 1))


def _timezone_signature(block):
    """Compare observance structure, ignoring descriptive timezone metadata.

    Retain the component nesting: moving an offset between DAYLIGHT/STANDARD
    observances must never be mistaken for an equivalent definition.
    """
    stack = []
    for line in block:
        key, parameters, value = fallback.content_line(line)
        if key == 'BEGIN':
            stack.append([value.upper(), [], []])
        elif key == 'END':
            name, properties, children = stack.pop()
            node = name, tuple(sorted(properties)), tuple(sorted(children))
            if stack:
                stack[-1][2].append(node)
            else:
                return node
        elif key not in {'COMMENT', 'LAST-MODIFIED', 'TZURL'}:
            if key == 'RRULE':
                value = ';'.join(sorted(value.upper().split(';')))
            elif key == 'RDATE':
                value = ','.join(sorted(value.split(',')))
            # Parameter values can be case-sensitive even though property
            # names are not. Be conservative for unrecognized extensions.
            if key == 'TZID' and parameters.get('VALUE', '').upper() == 'TEXT':
                parameters.pop('VALUE')
            encoded = ''.join(';' + name + '=' + fallback.parameter(item) for name, item in sorted(parameters.items()))
            stack[-1][1].append((key + encoded, fallback.text(value) if key == 'TZID' else value))
    raise ValueError('Incomplete timezone definition.')


def _generated_timezone_coverage(block):
    """Recognize retained generated zones by content, never by a comment alone."""
    # Imported blocks are untrusted and can be large. Cache only ordinary
    # definitions so a few unusual exports cannot retain their entire payloads.
    if sum(len(line) for line in block) > 65_536:
        return None
    return _cached_generated_timezone_coverage(block)


@lru_cache(maxsize=64)
def _cached_generated_timezone_coverage(block):
    identifier = fallback.text(_property_of(block, 'TZID') or '')
    claims = [match.groups() for line in block if (match := re.fullmatch(
        r'COMMENT:This timezone only works from (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)\.', line))]
    if len(claims) != 1:
        return None
    try:
        first, last = map(date.fromisoformat, claims[0])
        if last <= first or (last.year, last.month, last.day) > (first.year + 512, first.month, first.day):
            return None
        signature = _timezone_signature(block)
        if signature == _timezone_signature(_timezone_definition(identifier, first, last)):
            return first, last
        # Older Debacal versions used the library generator directly. Its
        # exact output can contain incorrect/missing transitions; recognize
        # that complete source fingerprint as native intent so it does not
        # become a foreign explicit rule after updating the generator.
        from .timezone_generation import legacy_generated_timezone
        legacy = legacy_generated_timezone(resolve_timezone(identifier), identifier, first, last)
        if signature == _timezone_signature(fallback.unfolded(legacy.to_ical())):
            return first, last
    except (ValueError, KeyError, ImportError):
        pass
    return None


def _unique_timezones(blocks):
    """One unambiguous definition per TZID in a complete calendar object."""
    result, known = [], {}
    for block in blocks:
        if block[0] == 'BEGIN:VTIMEZONE':
            identifier = fallback.text(_property_of(block, 'TZID') or '')
            if not identifier:
                raise ValueError('A timezone definition is missing its TZID.')
            signature = _timezone_signature(block)
            if identifier in known:
                if known[identifier] != signature:
                    # Never guess which imported offset rules should win.
                    # Separate resources remain valid sync/export units.
                    raise ConflictingTimezoneDefinitions(identifier)
                continue
            known[identifier] = signature
        result.append(block)
    return result


def normalize_caldav_resource(data: str | bytes) -> bytes:
    """Prepare a valid storage envelope without rewriting event semantics.

    RFC 4791 section 4.1 forbids VCALENDAR's METHOD property in calendar
    object resources. Imported files can also omit or duplicate their producer
    and version headers, which strict servers reject. Repair that transport
    metadata only at the upload boundary. Event properties, alarms, timezone
    definitions and unknown metadata remain intact. A different calendar scale
    cannot safely be relabelled Gregorian.
    """
    from .caldav_validation import normalize_event_metadata, normalize_header_tokens
    original_header, original_blocks = _blocks(data)
    header = normalize_header_tokens(original_header)
    blocks = normalize_event_metadata(original_blocks)
    properties = {'VERSION': [], 'PRODID': [], 'CALSCALE': []}
    named = []
    for line in header:
        key = line.split(':', 1)[0].split(';', 1)[0].upper()
        named.append((key, line))
        if key in properties:
            properties[key].append((line, fallback.content_line(line)[2]))
    scales = properties['CALSCALE']
    if any(value.strip().upper() != 'GREGORIAN' for _line, value in scales):
        raise fallback.CalendarValueError(
            'Unsupported CALSCALE for CalDAV upload. Debacal uses the Gregorian calendar; '
            'review the original calendar scale rather than changing its dates automatically.')
    versions, producers = properties['VERSION'], properties['PRODID']
    replacements = {
        'VERSION': versions[0][0] if len(versions) == 1 and versions[0][1] == '2.0' else 'VERSION:2.0',
        'PRODID': producers[0][0] if len(producers) == 1 and producers[0][1].strip()
                  else 'PRODID:-//Debacal//Local-first calendar//EN',
        'CALSCALE': scales[0][0] if len(scales) == 1 else 'CALSCALE:GREGORIAN',
    }
    stored_header, seen = [], set()
    for key, line in named:
        if key == 'METHOD':
            continue
        if key in replacements:
            if key not in seen:
                stored_header.append(replacements[key])
                seen.add(key)
        else:
            stored_header.append(line)
    for key in ('VERSION', 'PRODID'):
        if key not in seen:
            stored_header.append(replacements[key])
    if stored_header == original_header and blocks == original_blocks:
        return data.encode("utf-8") if isinstance(data, str) else data
    return fallback.folded(["BEGIN:VCALENDAR", *stored_header,
                            *(line for block in blocks for line in block), "END:VCALENDAR"])


def _named_detached(override):
    # Fixed detached DURATION ranges traditionally use UTC endpoints. A
    # nominal reminder additionally owns its endpoint's calculation zone.
    return not override.duration_spec or any(
        alarm_fold_ics.needs_projection(alarm, {'START', 'END'}) for alarm in override.alarms or ())


def _union_span(first, second):
    return min(first[0], second[0]), max(first[1], second[1])


def _required_timezone_spans(event):
    if _named_start(event) and event.timezone_id not in event.timezone_definitions:
        yield event.timezone_id, _timezone_span(event)
    end_id = event.end_timezone_id
    if end_id and end_id != 'UTC' and end_id not in event.timezone_definitions:
        yield end_id, _timezone_span(event)
    for payload in event.recurrence_overrides.values():
        if not (payload.get('end_timezone_id') or payload.get('timezone_id') and not payload.get('all_day')):
            continue
        override = Event.from_dict(payload)
        if override.end_timezone_id or _named_detached(override):
            for identifier in (override.timezone_id, override.end_timezone_id):
                if identifier and identifier != 'UTC' and identifier not in event.timezone_definitions:
                    yield identifier, _timezone_span(override)


def serialize_resource(event, *, timezone_spans=None):
    raw = event.raw_ics
    header, blocks = _blocks(raw) if raw else (["VERSION:2.0", "PRODID:-//Debacal//Local-first calendar//EN", "CALSCALE:GREGORIAN"], [])
    uid = event.ical_uid or event.id
    event_blocks = [b for b in blocks if b[0] == "BEGIN:VEVENT"]
    masters, detached = [], {}
    for block in event_blocks:
        c = fallback.components(fallback.folded(["BEGIN:VCALENDAR", *block, "END:VCALENDAR"]))[0]
        c['_timezone_definitions'] = event.timezone_definitions
        if "RECURRENCE-ID" in c:
            key = _key(_decoded(c, "RECURRENCE-ID", event_timezone(event))[0], event_timezone(event), event.floating_time)
            detached[key] = block
        else:
            masters.append(block)
    if len(masters) > 1:
        raise ValueError("An event resource contains more than one master.")
    result = [b for b in blocks if b[0] != "BEGIN:VEVENT"]
    # Copies and splits may have no raw template. Their scoped definitions are
    # part of the event model, so they remain independently exportable.
    existing_ids = {fallback.text(_property_of(block, 'TZID') or '') for block in result if block[0] == 'BEGIN:VTIMEZONE'}
    for identifier, definition in event.timezone_definitions.items():
        if identifier not in existing_ids:
            result.append(fallback.unfolded(definition))
            existing_ids.add(identifier)
    required = {}
    for identifier, span in _required_timezone_spans(event):
        required[identifier] = _union_span(required.get(identifier, span), span)
    for index, block in enumerate(result):
        if block[0] == 'BEGIN:VTIMEZONE':
            coverage = _generated_timezone_coverage(tuple(block))
            if coverage is not None:
                identifier = fallback.text(_property_of(block, 'TZID') or '')
                coverage = _union_span(required.get(identifier, coverage), coverage)
                result[index] = _timezone_definition(identifier, *(timezone_spans or {}).get(identifier, coverage))
    for identifier, span in required.items():
        if identifier not in existing_ids:
            result.insert(0, _timezone_definition(identifier, *(timezone_spans or {}).get(identifier, span)))
            existing_ids.add(identifier)
    master_template = masters[0] if masters else ['BEGIN:VEVENT', 'END:VEVENT']
    if event.alarms is None and (event.range_policy or _property_of(master_template, alarm_rdate_ics.ZONE) is not None):
        from .alarm_ics import hydrate_alarms
        event = hydrate_alarms(event)
    if alarm_rdate_ics.applies(event):
        master_block = _patch_block(master_template, alarm_rdate_ics.utc_range(event), uid)
        master_block[-1:-1] = alarm_rdate_ics.zone_lines(event)
        result.append(master_block)
        result.extend(_rdate_child_block(child, uid, key)
                      for key, child in alarm_rdate_ics.exceptions(event, event.recurrence_overrides))
    else:
        result.append(_patch_block(master_template, event, uid))
    for key, payload in sorted(event.recurrence_overrides.items()):
        override = Event.from_dict(payload)
        result.append(_patch_block(detached.get(key, ["BEGIN:VEVENT", "END:VEVENT"]), override, uid, key,
                                   recurrence_floating=event.floating_time, named_detached=_named_detached(override)))
    if event.range_policy == 'cut':
        marker = range_codec.generated_marker(event, range_codec.signature(event))
        for key, start, end in range_codec.generated_ranges(event):
            result.append(_cut_child_block(event, uid, key, start, end, marker, detached.get(key)))
    # Keep imported cancellation metadata while it remains excluded. Removing
    # the exclusion also removes the cancelled detached component.
    for key in set(detached) - event.recurrence_overrides.keys():
        if key in event.excluded_dates and any(line == "STATUS:CANCELLED" for line in detached[key]):
            result.append(detached[key])
    return fallback.folded(["BEGIN:VCALENDAR", *header,
                            *(line for block in _unique_timezones(result) for line in block), "END:VCALENDAR"])


def export(events):
    events = tuple(events)
    if len(events) == 1:
        # A single UID is also the sync transport's resource format. Preserve
        # imported calendar-level metadata when creating a split or a copy.
        return serialize_resource(events[0])
    # Generate each named zone once over the union of required coverage. Block
    # equality alone cannot consolidate the same zone generated for two eras.
    spans = {}
    for event in events:
        for identifier, span in _required_timezone_spans(event):
            spans[identifier] = _union_span(spans.get(identifier, span), span)
        if event.raw_ics:
            for block in _blocks(event.raw_ics)[1]:
                if block[0] != 'BEGIN:VTIMEZONE':
                    continue
                coverage = _generated_timezone_coverage(tuple(block))
                if coverage is not None:
                    identifier = fallback.text(_property_of(block, 'TZID') or '')
                    spans[identifier] = _union_span(spans.get(identifier, coverage), coverage)
    components = []
    for event in events:
        _, blocks = _blocks(serialize_resource(event, timezone_spans=spans))
        components.extend(blocks)
    try:
        components = _unique_timezones(components)
    except ConflictingTimezoneDefinitions as error:
        # Independent, parsed resources can use the same TZID for different
        # explicit clocks. RFC 5545 streams preserve those calendar scopes.
        # Do not apply this to a native event with a contradictory raw template:
        # its model has not established which definition owns the timestamps.
        affected = [event for event in events if error.identifier in event.timezone_definitions
                    or event.timezone_id == error.identifier]
        if not affected or any(error.identifier not in event.timezone_definitions for event in affected):
            raise
        return b''.join(serialize_resource(event) for event in events)
    return fallback.folded(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Debacal//Local-first calendar//EN", "CALSCALE:GREGORIAN",
                            *(line for block in components for line in block), "END:VCALENDAR"])


def patch(data, event):
    parsed = parse(data, event.calendar_id)
    if len(parsed) != 1:
        raise ValueError("A CalDAV resource must contain exactly one event UID.")
    original = parsed[0]
    # Resolve legacy local intent against its own family before replacing the
    # transport envelope. The block patcher reconciles it on the chosen template.
    from .alarm_ics import hydrate_alarms
    event = hydrate_alarms(event)
    return serialize_resource(replace(event, raw_ics=data.decode() if isinstance(data, bytes) else data,
                                      ical_uid=original.ical_uid))
