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
from zoneinfo import ZoneInfo
import re

from .models import Event
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
    if "schedule syntax version" in message:
        return "Unsupported custom schedule version."
    if "until" in message:
        return "Invalid recurrence end boundary (UNTIL)."
    if isinstance(exc, RecurrenceError) or "recurrence" in message or "rrule" in message:
        return "Unsupported or invalid recurrence rule."
    if "timezone" in message:
        return "Unsupported timezone definition."
    if "end" in message and "before" in message:
        return "Event ends before its start."
    if "uid is missing" in message:
        return "Event UID is missing."
    return "Unsupported or malformed event resource."


def parse_report(data, calendar_id, *, limit=10000, use_full=True, repair_missing_uids=False):
    """Split UID resources before decoding so one bad event cannot abort peers."""
    lines = fallback.unfolded(data)
    if not lines or "BEGIN:VCALENDAR" not in lines or "END:VCALENDAR" not in lines:
        raise ValueError("Expected a complete VCALENDAR document.")
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
    generated_count = sum(_property_of(block, range_codec.GENERATED) is not None for block in event_blocks)
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
        raw = fallback.folded(["BEGIN:VCALENDAR", *header,
                               *(line for block in timezones + group for line in block), "END:VCALENDAR"]).decode()
        uid = _uid_of(group[0])
        resource_id = hashlib.sha256((uid or raw).encode()).hexdigest()[:12]
        try:
            if not uid:
                raise ValueError("Event UID is missing.")
            parsed = parse(raw, calendar_id, limit=limit, use_full=use_full,
                           _source=decoded_groups.get(uid))
            events.extend(parsed)
            if uid in generated_uids and parsed:
                issues.append(ResourceIssue(resource_id, "Missing event UID; generated a stable identifier for import."))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            reason = _safe_reason(exc)
            issues.append(ResourceIssue(resource_id, reason))
            rejected.append(RejectedResource(uid, raw, resource_id, reason))
    return reject_duplicate_identities(ParseReport(
        tuple(sorted(events, key=lambda event: (event.start.timestamp(), event.id))), tuple(issues), tuple(rejected)))


def _read(data, use_full=True):
    from .ics import ICalendar
    if not use_full or ICalendar is None:
        return fallback.components(data)
    calendar = ICalendar.from_ical(data)
    if calendar.name != "VCALENDAR":
        raise ValueError("Expected a VCALENDAR document.")
    result = []
    for original in calendar.walk("VEVENT"):
        component = {"_decoded": {}}
        for key, values in original.items():
            values = values if isinstance(values, list) else [values]
            for value in values:
                raw = value.to_ical()
                component.setdefault(key, []).append((dict(value.params), raw.decode() if isinstance(raw, bytes) else str(raw)))
                if hasattr(value, "dts"):
                    component["_decoded"].setdefault(key, []).extend(v.dt for v in value.dts)
                elif hasattr(value, "dt"):
                    component["_decoded"].setdefault(key, []).append(value.dt)
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
    result = []
    for parameters, raw in component.get(key, []):
        for piece in raw.split(","):
            result.append(fallback.dt(piece, parameters, tz)[0] if parameters.get("VALUE") != "DATE" and len(piece) != 8
                          else fallback.dt(piece, parameters, tz)[0].date())
    return result


def _key(value, tz):
    from .ics import occurrence_key
    return occurrence_key(_datetime(value, tz) if isinstance(value, datetime) else value, tz=tz)


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


def _property_of(block, name):
    depth = 0
    for line in block:
        left, _, value = line.partition(":")
        marker = left.upper()
        if marker == "BEGIN":
            depth += 1
            continue
        if marker == "END":
            depth -= 1
            continue
        if depth == 1 and left.split(";")[0].upper() == name:
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


def _resource_for_uid(data, uid, *, synthetic=False, discard_generated=()):
    header, blocks = _blocks(data)
    selected = [b for b in blocks if b[0] == "BEGIN:VTIMEZONE" or
                (b[0] == "BEGIN:VEVENT" and (_uid_of(b) == uid or (synthetic and not _uid_of(b))))]
    if discard_generated:
        from .ics import local_timezone
        tz, omitted = local_timezone(), set(discard_generated)
        retained = []
        for block in selected:
            if block[0] == 'BEGIN:VEVENT' and _property_of(block, range_codec.GENERATED) is not None:
                component = fallback.components('\n'.join(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
                if 'RECURRENCE-ID' in component and _key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz) in omitted:
                    continue
            retained.append(block)
        selected = retained
    return fallback.folded(["BEGIN:VCALENDAR", *header, *(line for block in selected for line in block), "END:VCALENDAR"]).decode()


def parse(data, calendar_id, window_start=None, window_end=None, *, limit=10000,
          read_only_recurring=False, use_full=True, _source=None):
    """Import series without materializing any recurrence occurrences.

    Window parameters remain accepted for API compatibility, but do not truncate
    the preserved recurrence. The display layer chooses its expansion window.
    """
    from .ics import local_timezone
    from .recurrence import canonical_rule, occurrence_id, recurrence_set
    tz = local_timezone()
    source = _read(data, use_full) if _source is None else _source
    generated_count = sum(range_codec.GENERATED in component for component in source)
    if len(source) - generated_count > limit or generated_count > range_codec.MAX_GENERATED_RANGES:
        raise ValueError(f"Import exceeds {limit:,} event components.")
    generated_safety = {}
    if generated_count:
        _, raw_blocks = _blocks(data)
        for block in raw_blocks:
            if block[0] != 'BEGIN:VEVENT' or _property_of(block, range_codec.GENERATED) is None:
                continue
            raw_component = fallback.components('\n'.join(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
            if 'RECURRENCE-ID' in raw_component:
                key = _key(_decoded(raw_component, 'RECURRENCE-ID', tz)[0], tz)
                slot = (_uid_of(block), key)
                generated_safety[slot] = (range_codec.safe_generated_block(block)
                                          and slot not in generated_safety)
    grouped = {}
    for component in source:
        if "EXRULE" in component:
            raise ValueError("EXRULE calendars are unsupported. Convert exclusions to EXDATE before importing.")
        if len(component.get("RRULE", [])) > 1:
            raise ValueError("Multiple RRULE properties on one event are unsupported.")
        if any(str(params.get("RANGE", "")).upper() == "THISANDFUTURE"
               for params, _ in component.get("RECURRENCE-ID", [])):
            raise ValueError("RECURRENCE-ID RANGE=THISANDFUTURE is unsupported; split the series before importing.")
        uid = fallback.prop(component, "UID") or str(uuid5(NAMESPACE_URL, repr(component)))
        grouped.setdefault(uid, []).append(component)
    result = []
    for uid, components in grouped.items():
        masters = [c for c in components if "RECURRENCE-ID" not in c]
        if len(masters) != 1:
            raise ValueError("Each recurring UID must contain exactly one master event; detached-only imports are unsupported.")
        master_component = masters[0]
        if fallback.prop(master_component, "STATUS").upper() == "CANCELLED":
            continue
        if "DTSTART" not in master_component:
            raise ValueError("An event is missing DTSTART.")
        raw_start = _decoded(master_component, "DTSTART", tz)[0]
        all_day = not isinstance(raw_start, datetime)
        event_start = _datetime(raw_start, tz)
        if "DTEND" in master_component:
            event_end = _datetime(_decoded(master_component, "DTEND", tz)[0], tz)
        elif "DURATION" in master_component:
            event_end = event_start + fallback.duration(fallback.prop(master_component, "DURATION"))
        else:
            # RFC 5545: DATE without an end lasts one day; DATE-TIME without
            # an end is a point, not an invented one-hour appointment.
            event_end = event_start + (timedelta(days=1) if all_day else timedelta(0))
        event_id = str(uuid5(NAMESPACE_URL, f"debacal:{calendar_id}:{uid}:"))
        if fallback.prop(master_component, "X-DEBACAL-CALENDAR") == calendar_id:
            event_id = fallback.prop(master_component, "X-DEBACAL-ID", event_id)
        params = master_component["DTSTART"][0][0]
        timezone_id = str(params.get("TZID", "")) or getattr(event_start.tzinfo, "key", "")
        if timezone_id:
            try:
                ZoneInfo(timezone_id)
            except (KeyError, ValueError):
                raise ValueError(f"Custom timezone {timezone_id!r} cannot be edited safely yet. Use an IANA timezone.") from None
        master = Event(
            id=event_id, calendar_id=calendar_id,
            title=fallback.prop(master_component, "SUMMARY", "Untitled event"), start=event_start, end=event_end,
            description=fallback.prop(master_component, "DESCRIPTION"), location=fallback.prop(master_component, "LOCATION"),
            group_id=fallback.prop(master_component, "X-DEBACAL-GROUP") or None,
            candidate=fallback.prop(master_component, "X-DEBACAL-CANDIDATE").upper() == "TRUE",
            all_day=all_day, ical_uid=uid, timezone_id=timezone_id,
            recurrence_rule=fallback.prop(master_component, "RRULE"),
            recurrence_dates=tuple(_key(value, tz) for value in _decoded(master_component, "RDATE", tz)),
            excluded_dates=tuple(_key(value, tz) for value in _decoded(master_component, "EXDATE", tz)),
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
        cancelled = {_key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz)
                     for component in components if 'RECURRENCE-ID' in component
                     and fallback.prop(component, 'STATUS').upper() == 'CANCELLED'}
        if cancelled:
            master = replace(master, excluded_dates=tuple(set(master.excluded_dates) | cancelled))
        expected = {}
        policy_fingerprint = range_codec.signature(master) if master.range_policy else ''
        verified_policy = (master.range_policy == 'cut'
                           and fallback.prop(master_component, range_codec.SIGNATURE) == policy_fingerprint)
        if verified_policy:
            expected = range_codec.expected_ranges(master)
            present = {_key(_decoded(component, 'RECURRENCE-ID', tz)[0], tz)
                       for component in components if 'RECURRENCE-ID' in component}
            verified_policy = expected.keys() <= present
        if not verified_policy:
            master = replace(master, range_policy='')
        discarded = set()
        overrides, exclusions = {}, set(master.excluded_dates)
        for component in components:
            if "RECURRENCE-ID" not in component:
                continue
            key = _key(_decoded(component, "RECURRENCE-ID", tz)[0], tz)
            if (verified_policy and generated_safety.get((uid, key), False)
                    and range_codec.matches_generated(component, master, key, expected, decode=_decoded, tz=tz,
                                                       fingerprint=policy_fingerprint)):
                discarded.add(key)
                continue
            if fallback.prop(component, "STATUS").upper() == "CANCELLED":
                exclusions.add(key)
                continue
            raw_override_start = _decoded(component, 'DTSTART' if 'DTSTART' in component else 'RECURRENCE-ID', tz)[0]
            override_all_day = not isinstance(raw_override_start, datetime)
            override_start = _datetime(raw_override_start, tz)
            if "DTEND" in component:
                override_end = _datetime(_decoded(component, "DTEND", tz)[0], tz)
            elif "DURATION" in component:
                override_end = override_start + fallback.duration(fallback.prop(component, "DURATION"))
            else:
                # A complete timed override with DTSTART but no endpoint is a
                # point as well. A sparse exception that only changes metadata
                # still inherits the master timing.
                duration = (timedelta(days=1) if override_all_day else timedelta(0)) if "DTSTART" in component else event_end - event_start
                override_end = override_start + duration
            override = replace(
                master, id=occurrence_id(master.id, key), start=override_start, end=override_end,
                title=fallback.prop(component, "SUMMARY", master.title),
                description=fallback.prop(component, "DESCRIPTION", master.description),
                location=fallback.prop(component, "LOCATION", master.location),
                all_day=override_all_day,
                recurrence_rule="", recurrence_dates=(), excluded_dates=(), recurrence_overrides={}, raw_ics="",
                series_id=master.id, recurrence_id=key,
                schedule_expression="", expression_anchor="",
                schedule_syntax=1,
                range_policy='',
            )
            overrides[key] = override.to_dict()
        master = replace(master, recurrence_overrides=overrides, excluded_dates=tuple(sorted(exclusions)))
        master = replace(master, raw_ics=_resource_for_uid(data, uid, synthetic='UID' not in master_component,
                                                          discard_generated=discarded))
        master = replace(master, recurrence_rule=canonical_rule(master.recurrence_rule, master))
        recurrence_set(master)  # Validate before an import reaches the SQLite transaction.
        result.append(master)
    return sorted(result, key=lambda event: (event.start.timestamp(), event.id))


def _date_property(key, value):
    if len(value) == 10:
        return f"{key};VALUE=DATE:{date.fromisoformat(value):%Y%m%d}"
    parsed = datetime.fromisoformat(value)
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
        if "RECURRENCE-ID" not in component:
            projected.append(block)
            continue
        key = _key(_decoded(component, "RECURRENCE-ID", zone)[0], zone)
        moved_key = transform(key)
        if moved_key == key:
            projected.append(block)
            continue
        replacement = _date_property("RECURRENCE-ID", moved_key)
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
    return bool((event.recurrence_rule or authored_v2 or event.range_policy) and not event.all_day
                and event.timezone_id not in {"", "UTC", "Etc/UTC"})


def _known_lines(event, uid, recurrence_id=""):
    lines = fallback.event_lines(event)[1:-1]
    lines = [line for line in lines if not line.startswith("UID:")]
    if not recurrence_id and _named_start(event):
        zone = ZoneInfo(event.timezone_id)
        lines = [line for line in lines if not line.startswith(("DTSTART:", "DTEND:"))]
        lines.append(f"DTSTART;TZID={event.timezone_id}:{event.start.astimezone(zone):%Y%m%dT%H%M%S}")
        if event.end.timestamp() > event.start.timestamp():
            local_end = event.end.astimezone(zone)
            if local_end.fold and local_end.replace(fold=0).timestamp() != local_end.timestamp():
                # A TZID clock alone denotes the first repeated-hour instant.
                # Keep the selected endpoint unambiguous, including ranges
                # whose written end clock precedes their written start clock.
                lines.append(f"DTEND:{event.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}")
            else:
                lines.append(f"DTEND;TZID={event.timezone_id}:{local_end:%Y%m%dT%H%M%S}")
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
        lines.append(_date_property("RECURRENCE-ID", recurrence_id))
    elif event.recurrence_rule:
        from .recurrence import canonical_rule
        rule = canonical_rule(event.recurrence_rule, event)
        if any(char in rule for char in "\r\n"):
            raise ValueError("A recurrence rule must be a single line.")
        if event.all_day:
            zone = ZoneInfo(event.timezone_id) if event.timezone_id else event.start.tzinfo
            def date_until(match):
                value = match[1]
                if "T" not in value:
                    return "UNTIL=" + value
                parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                return "UNTIL=" + parsed.astimezone(zone).strftime("%Y%m%d")
            rule = re.sub(r"UNTIL=([^;]+)", date_until, rule)
        lines.append("RRULE:" + rule)
    if not recurrence_id:
        lines.extend(_date_property("RDATE", value) for value in event.recurrence_dates)
        lines.extend(_date_property("EXDATE", value) for value in event.excluded_dates)
    return lines


@lru_cache(maxsize=64)
def _timezone_definition(timezone_id, first_date, last_date):
    try:
        from icalendar import Timezone
    except ImportError:
        raise ValueError("Exporting a named-timezone recurrence needs the 'icalendar' dependency to generate VTIMEZONE. "
                         "Install the project dependencies, or retain an imported timezone definition.") from None
    return fallback.unfolded(Timezone.from_tzinfo(ZoneInfo(timezone_id), tzid=timezone_id,
                                                 first_date=first_date, last_date=last_date).to_ical())


def _patch_block(block, event, uid, recurrence_id=""):
    known = {"UID", "SUMMARY", "DTSTART", "DTEND", "DURATION", "DTSTAMP", "DESCRIPTION", "LOCATION",
             "X-DEBACAL-ID", "X-DEBACAL-CALENDAR", "X-DEBACAL-GROUP", "X-DEBACAL-CANDIDATE",
             "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"}
    known.update({"X-DEBACAL-PATTERN", "X-DEBACAL-PATTERN-ANCHOR", "X-DEBACAL-PATTERN-VERSION"})
    known.update(range_codec.PROPERTIES)
    result, nested = ["BEGIN:VEVENT"], 0
    for line in block[1:-1]:
        key = line.split(":", 1)[0].split(";")[0].upper()
        if line.startswith("BEGIN:"):
            nested += 1
        if nested or key not in known:
            result.append(line)
        if line.startswith("END:"):
            nested -= 1
    return [*result, *_known_lines(event, uid, recurrence_id), "END:VEVENT"]


def serialize_resource(event):
    from .ics import local_timezone
    raw = event.raw_ics
    header, blocks = _blocks(raw) if raw else (["VERSION:2.0", "PRODID:-//Debacal//Local-first calendar//EN", "CALSCALE:GREGORIAN"], [])
    uid = event.ical_uid or event.id
    event_blocks = [b for b in blocks if b[0] == "BEGIN:VEVENT"]
    masters, detached = [], {}
    for block in event_blocks:
        c = fallback.components(fallback.folded(["BEGIN:VCALENDAR", *block, "END:VCALENDAR"]))[0]
        if "RECURRENCE-ID" in c:
            key = _key(_decoded(c, "RECURRENCE-ID", local_timezone())[0], local_timezone())
            detached[key] = block
        else:
            masters.append(block)
    if len(masters) > 1:
        raise ValueError("An event resource contains more than one master.")
    result = [b for b in blocks if b[0] != "BEGIN:VEVENT"]
    if _named_start(event):
        existing_ids = {fallback.text(line.partition(":")[2]) for block in result if block[0] == "BEGIN:VTIMEZONE"
                        for line in block if line.startswith("TZID:")}
        if event.timezone_id not in existing_ids:
            # Maintained timezone generation is cached. Its documented range
            # covers 1970–2100 and extends to encompass historical event starts.
            first_date = date(max(1, min(1970, event.start.year - 1)), 1, 1)
            last_date = date(min(9999, max(2101, event.end.year + 2)), 1, 1)
            result.insert(0, _timezone_definition(event.timezone_id, first_date, last_date))
    result.append(_patch_block(masters[0] if masters else ["BEGIN:VEVENT", "END:VEVENT"], event, uid))
    for key, payload in sorted(event.recurrence_overrides.items()):
        override = Event.from_dict(payload)
        result.append(_patch_block(detached.get(key, ["BEGIN:VEVENT", "END:VEVENT"]), override, uid, key))
    if event.range_policy == 'cut':
        from .recurrence import occurrence_id
        fingerprint = range_codec.signature(event)
        for key, start, end in range_codec.generated_ranges(event):
            override = replace(event, id=occurrence_id(event.id, key), start=start, end=end,
                               recurrence_rule='', recurrence_dates=(), excluded_dates=(),
                               recurrence_overrides={}, series_id=event.id, recurrence_id=key,
                               range_policy='', schedule_expression='', expression_anchor='',
                               schedule_syntax=1, raw_ics='')
            block = _patch_block(detached.get(key, ['BEGIN:VEVENT', 'END:VEVENT']), override, uid, key)
            block.insert(-1, range_codec.GENERATED + ':' + fingerprint)
            result.append(block)
    # Keep imported cancellation metadata while it remains excluded. Removing
    # the exclusion also removes the cancelled detached component.
    for key in set(detached) - event.recurrence_overrides.keys():
        if key in event.excluded_dates and any(line == "STATUS:CANCELLED" for line in detached[key]):
            result.append(detached[key])
    return fallback.folded(["BEGIN:VCALENDAR", *header, *(line for block in result for line in block), "END:VCALENDAR"])


def export(events):
    events = tuple(events)
    if len(events) == 1:
        # A single UID is also the sync transport's resource format. Preserve
        # imported calendar-level metadata when creating a split or a copy.
        return serialize_resource(events[0])
    components, seen_timezones = [], set()
    for event in events:
        _, blocks = _blocks(serialize_resource(event))
        for block in blocks:
            if block[0] == "BEGIN:VTIMEZONE":
                identity = tuple(block)
                if identity in seen_timezones:
                    continue
                seen_timezones.add(identity)
            components.extend(block)
    return fallback.folded(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Debacal//Local-first calendar//EN", "CALSCALE:GREGORIAN",
                            *components, "END:VCALENDAR"])


def patch(data, event):
    parsed = parse(data, event.calendar_id)
    if len(parsed) != 1:
        raise ValueError("A CalDAV resource must contain exactly one event UID.")
    original = parsed[0]
    return serialize_resource(replace(event, raw_ics=data.decode() if isinstance(data, bytes) else data,
                                      ical_uid=original.ical_uid))
