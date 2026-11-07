"""Bounded views of real recurring series and stable occurrence identities.

Only the master is stored. RDATE/EXDATE and detached exceptions are carried by
that master, retaining normal iCalendar recurrence semantics on export/sync.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from heapq import merge
from typing import Iterable
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo
import re

from dateutil.rrule import rruleset, rrulestr

from .models import Event


MAX_MINUTELY_COUNT = 10000


class RecurrenceError(ValueError):
    pass


class RecurrenceRangeError(RecurrenceError):
    """A valid schedule exceeds the requested projection's work budget."""


def event_zone(event: Event):
    return ZoneInfo(event.timezone_id) if event.timezone_id else event.start.tzinfo


def recurrence_key(value: datetime | date, all_day: bool = False) -> str:
    if all_day or not isinstance(value, datetime):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return value.astimezone(timezone.utc).isoformat()


def key_datetime(key: str, master: Event) -> datetime:
    if len(key) == 10:
        return datetime.combine(date.fromisoformat(key), time.min, event_zone(master))
    value = datetime.fromisoformat(key)
    if value.tzinfo is None:
        value = value.replace(tzinfo=event_zone(master))
    return value.astimezone(event_zone(master))


def schedule_fields(starts: Iterable[datetime], *, all_day: bool = False) -> dict:
    """Encode a finite timestamp sequence as a verified COUNT rule or RDATEs.

    The first instruction's timestamp remains DTSTART, including when explicit
    later instructions name earlier dates. Calendar-month clamping, different
    times and DST folds only use RRULE if every generated instant matches.
    """
    values = list(starts)
    if not values:
        raise RecurrenceError("A schedule needs its initial event.")
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise RecurrenceError("Schedule timestamps must include a timezone.")
    zone = values[0].tzinfo
    if all_day:
        values = [datetime.combine(value.astimezone(zone).date(), time.min, zone) for value in values]
    ordered = []
    seen = set()
    for value in values:
        key = recurrence_key(value, all_day)
        if key not in seen:
            seen.add(key)
            ordered.append(value.astimezone(zone))
    fallback = {"recurrence_rule": "", "recurrence_dates": tuple(recurrence_key(value, all_day) for value in ordered[1:])}
    if len(ordered) < 2 or any(later.timestamp() <= earlier.timestamp() for earlier, later in zip(ordered, ordered[1:])):
        return fallback
    wall = [value.replace(tzinfo=None) for value in ordered]
    candidates = []
    if len({value.time() for value in wall}) == 1:
        day_gaps = [(later - earlier).days for earlier, later in zip(wall, wall[1:])]
        if len(set(day_gaps)) == 1 and day_gaps[0] > 0:
            days = day_gaps[0]
            candidates.append(("WEEKLY", days // 7) if days % 7 == 0 else ("DAILY", days))
        month_gaps = [(later.year - earlier.year) * 12 + later.month - earlier.month for earlier, later in zip(wall, wall[1:])]
        if len(set(month_gaps)) == 1 and month_gaps[0] > 0:
            if month_gaps[0] % 12 == 0 and len({(value.month, value.day) for value in wall}) == 1:
                candidates.insert(0, ("YEARLY", month_gaps[0] // 12))
            elif len({value.day for value in wall}) == 1:
                candidates.insert(0, ("MONTHLY", month_gaps[0]))
    if not all_day:
        clock_gaps = {later - earlier for earlier, later in zip(wall, wall[1:])}
        if len(clock_gaps) == 1:
            gap = clock_gaps.pop()
            if gap > timedelta(0) and gap % timedelta(minutes=1) == timedelta(0):
                minutes = gap // timedelta(minutes=1)
                if minutes % 60 == 0:
                    candidates.append(("HOURLY", minutes // 60))
                elif len(ordered) <= MAX_MINUTELY_COUNT:
                    candidates.append(("MINUTELY", minutes))
    for frequency, interval in candidates:
        rule = f"FREQ={frequency};INTERVAL={interval};COUNT={len(ordered)}"
        try:
            generated = list(rrulestr(rule, dtstart=ordered[0]))
        except (ValueError, TypeError, OverflowError):
            continue
        if (all(_valid_wall_time(value) for value in generated)
                and [value.astimezone(timezone.utc) for value in generated]
                == [value.astimezone(timezone.utc) for value in ordered]):
            return {"recurrence_rule": rule, "recurrence_dates": ()}
    return fallback


def base_schedule_starts(master: Event, *, limit: int = 10000) -> list[datetime]:
    """Return a finite canonical schedule, retaining DTSTART as its first item.

    Authoring metadata describes the base schedule, independently of detached
    exceptions and exclusions. Its literal language never replaces RRULE/RDATE
    as the stored functional schedule.
    """
    if master.recurrence_rule:
        fields = normalized_rule(master.recurrence_rule, master)
        if "COUNT" not in fields and "UNTIL" not in fields:
            raise RecurrenceError("An unbounded repeating rule cannot retain a finite custom sequence. Choose a regular rule or a finite custom sequence.")
    bare = replace(master, excluded_dates=(), recurrence_overrides={})
    result = [master.start.astimezone(event_zone(master))]
    initial = recurrence_key(master.start, master.all_day)
    for value in recurrence_set(bare):
        if recurrence_key(value, master.all_day) == initial:
            continue
        result.append(value.astimezone(event_zone(master)))
        if len(result) > limit:
            raise RecurrenceError(f"The custom sequence exceeds {limit:,} events.")
    return result


def occurrence_id(master_id: str, key: str) -> str:
    return master_id + "::occurrence::" + quote(key, safe="")


def split_occurrence_id(value: str) -> tuple[str, str] | None:
    master_id, separator, key = value.rpartition("::occurrence::")
    return (master_id, unquote(key)) if separator and master_id and key else None


def canonical_rule(rule: str, master: Event) -> str:
    """Repair vendor UNTIL types without changing their inclusive wall boundary.

    RFC 5545 needs UTC UNTIL for timezone-aware timed DTSTART. Some exporters
    write a floating local datetime instead; interpret it in DTSTART's IANA
    zone, including DST, before converting to UTC. DATE schedules retain DATE.
    """
    if not rule:
        return ""
    zone = event_zone(master)
    parts = []
    for part in rule.strip().removeprefix("RRULE:").split(";"):
        name, separator, value = part.partition("=")
        if not separator:
            raise RecurrenceError("Malformed recurrence property.")
        name, value = name.strip().upper(), value.strip().upper()
        if name == "UNTIL":
            try:
                if re.fullmatch(r"\d{8}", value):
                    last_day = datetime.strptime(value, "%Y%m%d").date()
                    if master.all_day:
                        value = last_day.strftime("%Y%m%d")
                    else:
                        boundary = datetime.combine(last_day, time.max, zone)
                        value = boundary.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                else:
                    utc = value.endswith("Z")
                    boundary = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
                    boundary = boundary.replace(tzinfo=timezone.utc if utc else zone, fold=1)
                    if not utc and not _valid_wall_time(boundary):
                        raise ValueError("Nonexistent local boundary")
                    value = (boundary.astimezone(zone).strftime("%Y%m%d") if master.all_day else
                             boundary.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            except (ValueError, OverflowError) as exc:
                raise RecurrenceError("Invalid recurrence end boundary (UNTIL).") from exc
        parts.append(name + "=" + value)
    return ";".join(parts)


def _seek_rule(rule: str, start: datetime, after: datetime | None) -> tuple[str, datetime]:
    """Skip whole simple periods without changing the rule's local phase.

    dateutil starts iteration at DTSTART even for xafter(). Calendar rules with
    count-sensitive filters or complex month/year constraints retain that exact
    path; plain daily/weekly schedules can start close to the requested window.
    """
    if after is None:
        return rule, start
    parts = dict(piece.split("=", 1) for piece in rule.split(";"))
    if set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST", "BYDAY"}:
        return rule, start
    frequency = parts.get("FREQ")
    if frequency not in {"DAILY", "WEEKLY"}:
        return rule, start
    if "COUNT" in parts and "BYDAY" in parts:
        weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[start.weekday()]
        if frequency != "WEEKLY" or parts["BYDAY"] != weekday:
            return rule, start
    interval = int(parts.get("INTERVAL", "1"))
    period_days = interval * (7 if frequency == "WEEKLY" else 1)
    elapsed = after.astimezone(start.tzinfo).replace(tzinfo=None) - start.replace(tzinfo=None)
    skipped = max(0, elapsed.days // period_days - 1)
    if not skipped:
        return rule, start
    if "COUNT" in parts:
        remaining = int(parts["COUNT"]) - skipped
        if remaining <= 0:
            return "", start
        parts["COUNT"] = str(remaining)
    return ";".join(f"{key}={value}" for key, value in parts.items()), start + timedelta(days=period_days * skipped)


def recurrence_set(master: Event, *, after: datetime | None = None):
    """Return standard recurrence dates, optionally seeking a requested suffix.

    With ``after`` only values at/after that boundary are guaranteed complete;
    callers inspecting recurrence history should omit it. RDATE and EXDATE
    remain unchanged, including explicitly added dates before DTSTART.
    """
    return _recurrence_set(master, after=after)


def _recurrence_set(master: Event, *, after: datetime | None = None, include_dates: bool = True):
    if after is not None and (after.tzinfo is None or after.utcoffset() is None):
        raise ValueError("The recurrence boundary must include a timezone.")
    rules = rruleset()
    start = master.start.astimezone(event_zone(master))
    if include_dates:
        rules.rdate(start)
    if master.recurrence_rule:
        rule = canonical_rule(master.recurrence_rule, master)
        if master.all_day:
            def date_until(match):
                last_day = datetime.strptime(match[1], "%Y%m%d").date()
                inclusive = datetime.combine(last_day, time.max, event_zone(master))
                return "UNTIL=" + inclusive.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rule = re.sub(r"UNTIL=(\d{8})(?=;|$)", date_until, rule, flags=re.I)
        for part in rule.upper().split(";"):
            name, _, value = part.partition("=")
            if name in {"INTERVAL", "COUNT"}:
                try:
                    positive = int(value) > 0
                except ValueError:
                    positive = False
                if not positive:
                    raise RecurrenceError(f"Recurrence {name.lower()} must be a positive integer.")
        # Dense unbounded rules can generate millions of invisible historical
        # values before dateutil reaches a requested date. Finite custom minute
        # sequences remain safe to expand within the same bounded authoring cap.
        fields = dict(part.split("=", 1) for part in rule.upper().split(";"))
        if fields.get("FREQ") == "SECONDLY":
            raise RecurrenceError("Secondly recurrence rules are not supported.")
        if fields.get("FREQ") == "MINUTELY":
            if (not 0 < int(fields.get("COUNT", "0")) <= MAX_MINUTELY_COUNT
                    or fields.keys() - {"FREQ", "INTERVAL", "COUNT", "WKST"}):
                raise RecurrenceError(f"Minutely recurrence rules need a simple finite count of at most {MAX_MINUTELY_COUNT:,} events.")
        try:
            # Parse the original first so seeking cannot hide an invalid rule
            # (for example an exhausted COUNT with an invalid BYDAY).
            parsed = rrulestr(rule, dtstart=start)
            sought_rule, sought_start = _seek_rule(rule, start, after)
            if sought_rule:
                rules.rrule(parsed if sought_start == start else rrulestr(sought_rule, dtstart=sought_start))
        except (ValueError, TypeError, OverflowError) as exc:
            raise RecurrenceError(f"Invalid recurrence rule: {exc}") from exc
    if include_dates:
        for key in master.recurrence_dates:
            rules.rdate(key_datetime(key, master))
    for key in master.excluded_dates:
        rules.exdate(key_datetime(key, master))
    return rules


def normalized_rule(rule: str, master: Event) -> dict[str, str]:
    """Normalize harmless RRULE defaults in the context of its DTSTART."""
    parts = dict(piece.strip().split("=", 1) for piece in rule.upper().removeprefix("RRULE:").split(";") if "=" in piece)
    if not parts:
        return {}
    if parts.get("INTERVAL", "1").lstrip("0") == "1":
        parts.pop("INTERVAL", None)
    if parts.get("WKST", "MO") == "MO":
        parts.pop("WKST", None)
    if parts.get("FREQ") == "WEEKLY" and "BYDAY" not in parts:
        parts["BYDAY"] = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][master.start.astimezone(event_zone(master)).weekday()]
    for name, value in parts.items():
        if "," in value:
            parts[name] = ",".join(sorted(set(value.split(","))))
    return parts


def rules_equivalent(first: str, second: str, master: Event) -> bool:
    return normalized_rule(first, master) == normalized_rule(second, master)


def shift_rule(rule: str, shift: timedelta, master: Event) -> str:
    """Move an unchanged rule with its DTSTART, guarding constrained rules.

    Plain weekly BYDAY and inclusive UNTIL bounds follow the series movement.
    More complex month/year constraints require an explicit rule edit so their
    meaning is never silently changed.
    """
    if not rule or not shift:
        return rule
    parts = dict(piece.split("=", 1) for piece in rule.upper().removeprefix("RRULE:").split(";") if "=" in piece)
    old = master.start.astimezone(event_zone(master))
    new = old + shift
    days = (new.date() - old.date()).days
    time_changed = old.timetz().replace(tzinfo=None) != new.timetz().replace(tzinfo=None)
    if time_changed and any(name in parts for name in ("BYHOUR", "BYMINUTE", "BYSECOND")):
        raise RecurrenceError("This custom rule fixes the event's time. Update its rule explicitly when moving the whole series.")
    if days:
        if any(name in parts for name in ("BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH")):
            raise RecurrenceError("This custom rule fixes calendar dates. Update its rule explicitly when moving the whole series.")
        if "BYDAY" in parts:
            weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
            tokens = parts["BYDAY"].split(",")
            if parts.get("FREQ") not in {"WEEKLY", "DAILY"} or any(token not in weekdays for token in tokens):
                raise RecurrenceError("This custom weekday rule needs an explicit rule edit when moving the whole series.")
            parts["BYDAY"] = ",".join(weekdays[(weekdays.index(token) + days) % 7] for token in tokens)
    if "UNTIL" in parts:
        bound = parts["UNTIL"]
        if re.fullmatch(r"\d{8}", bound):
            value = datetime.strptime(bound, "%Y%m%d") + timedelta(days=days)
            parts["UNTIL"] = value.strftime("%Y%m%d")
        else:
            try:
                value = datetime.strptime(bound, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                moved = value.astimezone(event_zone(master)) + shift
                parts["UNTIL"] = moved.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            except ValueError as exc:
                raise RecurrenceError("Could not move this recurrence's UNTIL boundary. Edit its rule explicitly.") from exc
    return ";".join(f"{name}={value}" for name, value in parts.items())


def updated_series(master: Event, reference: Event, edited: Event, replace_schedule: bool = False) -> Event:
    """Project a whole-series edit without accessing storage or mutating inputs.

    Relative edits shift the original master from the selected occurrence. An
    absolute schedule replacement instead takes the editor's DTSTART/date set.
    Both previews and persisted edits use this same rule/exception projection.
    """
    def checked_wall(value):
        if not _valid_wall_time(value):
            raise RecurrenceError(
                "This move would use a local time that does not exist because of a daylight-saving transition. "
                "Choose another time."
            )
        return value

    if replace_schedule:
        updated = replace(edited, id=master.id, calendar_id=master.calendar_id,
                          ical_uid=master.ical_uid, raw_ics=master.raw_ics,
                          series_id="", recurrence_id="", read_only=master.read_only,
                          group_id=master.group_id, recurrence_overrides={})
        overrides = {}
        if updated.recurring:
            rules = recurrence_set(updated)
            zone = event_zone(updated)
            duration = (updated.end.astimezone(zone).replace(tzinfo=None)
                        - updated.start.astimezone(zone).replace(tzinfo=None))
            for key, payload in master.recurrence_overrides.items():
                when = key_datetime(key, updated)
                previous = rules.before(when, inc=True)
                if previous is None or recurrence_key(previous, updated.all_day) != key:
                    continue
                value = Event.from_dict(payload)
                override_start = value.start.astimezone(zone)
                if duration <= timedelta(0):
                    override_end = datetime.fromtimestamp(override_start.timestamp() + updated.end.timestamp() - updated.start.timestamp(), zone)
                else:
                    override_end = override_start + duration
                checked_wall(override_start)
                checked_wall(override_end)
                overrides[key] = replace(value, id=occurrence_id(master.id, key),
                                         start=override_start, end=override_end,
                                         timezone_id=updated.timezone_id, title=updated.title,
                                         description=updated.description, location=updated.location,
                                         all_day=updated.all_day, candidate=updated.candidate,
                                         schedule_expression="", expression_anchor="", schedule_syntax=1).to_dict()
        return replace(updated, recurrence_overrides=overrides)
    if not edited.recurrence_rule and not edited.recurrence_dates and not edited.schedule_expression:
        single = replace(edited, id=master.id, calendar_id=master.calendar_id,
                         series_id="", recurrence_id="", recurrence_overrides={}, excluded_dates=(),
                         raw_ics=master.raw_ics, ical_uid=master.ical_uid,
                         schedule_expression="", expression_anchor="", schedule_syntax=1)
        return single
    zone = event_zone(master)
    wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
    shift = wall(edited.start) - wall(reference.start)
    duration = wall(edited.end) - wall(edited.start)
    start = checked_wall((wall(master.start) + shift).replace(tzinfo=zone))
    if duration <= timedelta(0):
        end = datetime.fromtimestamp(start.timestamp() + edited.end.timestamp() - edited.start.timestamp(), zone)
    else:
        end = start + duration
    checked_wall(end)

    def shift_key(key):
        value = checked_wall((wall(key_datetime(key, master)) + shift).replace(tzinfo=zone))
        return recurrence_key(value, edited.all_day)

    dates = tuple(shift_key(key) for key in master.recurrence_dates) if edited.recurrence_dates == master.recurrence_dates else edited.recurrence_dates
    exclusions = tuple(shift_key(key) for key in master.excluded_dates) if edited.excluded_dates == master.excluded_dates else edited.excluded_dates
    rule = edited.recurrence_rule
    if rules_equivalent(rule, master.recurrence_rule, master):
        rule = shift_rule(rule, shift, master)
    overrides = {}
    for key, payload in master.recurrence_overrides.items():
        moved_key = shift_key(key)
        value = Event.from_dict(payload)
        override_start = checked_wall((wall(value.start) + shift).replace(tzinfo=zone))
        if duration <= timedelta(0):
            override_end = datetime.fromtimestamp(override_start.timestamp() + edited.end.timestamp() - edited.start.timestamp(), zone)
        else:
            override_end = override_start + duration
        checked_wall(override_end)
        overrides[moved_key] = replace(value, id=occurrence_id(master.id, moved_key),
                                       start=override_start, end=override_end,
                                       recurrence_id=moved_key, timezone_id=master.timezone_id,
                                       title=edited.title, description=edited.description,
                                       location=edited.location, all_day=edited.all_day,
                                       candidate=edited.candidate,
                                       schedule_expression="", expression_anchor="", schedule_syntax=1).to_dict()
    expression, anchor = edited.schedule_expression, edited.expression_anchor
    from .ics_resources import rekey_recurrence_templates
    updated = replace(master, title=edited.title, description=edited.description, location=edited.location,
                      start=start, end=end, all_day=edited.all_day, candidate=edited.candidate,
                      recurrence_rule=rule, recurrence_dates=dates,
                      excluded_dates=exclusions, recurrence_overrides=overrides,
                      schedule_expression=expression, expression_anchor=anchor,
                      schedule_syntax=edited.schedule_syntax,
                      raw_ics=rekey_recurrence_templates(master, shift_key))
    if expression:
        from .patterns import shift_schedule_expression
        if edited.schedule_syntax == 2:
            # The v2 whole-series editor can convert legacy authoring text on
            # open. That new literal must follow the same timestamp move even
            # though it differs from the original stored expression.
            expression, anchor = shift_schedule_expression(
                expression, anchor or master.start.astimezone(zone).date().isoformat(),
                shift, base_schedule_starts(updated), syntax=2)
        elif expression == master.schedule_expression and anchor == master.expression_anchor:
            expression, anchor = shift_schedule_expression(
                expression, anchor, shift, base_schedule_starts(updated), syntax=1)
        updated = replace(updated, schedule_expression=expression, expression_anchor=anchor)
    return updated


def _valid_wall_time(value: datetime) -> bool:
    return value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) == value.replace(tzinfo=None)


def occurrence(master: Event, start: datetime, key: str | None = None) -> Event:
    key = key or recurrence_key(start, master.all_day)
    values = dict(
        id=occurrence_id(master.id, key), start=start,
        series_id=master.id, recurrence_id=key, recurrence_rule="",
        recurrence_dates=(), excluded_dates=(), recurrence_overrides={}, raw_ics="",
    )
    if key in master.recurrence_overrides:
        override = Event.from_dict(master.recurrence_overrides[key])
        return replace(override, **(values | {"start": override.start}),
                       calendar_id=master.calendar_id, read_only=master.read_only,
                       ical_uid=master.ical_uid)
    zone = event_zone(master)
    # A one-hour weekly meeting keeps its wall-clock length and local hour as
    # the UTC offset changes. Fold-only events retain their elapsed duration.
    wall_duration = (master.end.astimezone(zone).replace(tzinfo=None)
                     - master.start.astimezone(zone).replace(tzinfo=None))
    if wall_duration <= timedelta(0):
        end = datetime.fromtimestamp(start.timestamp() + master.end.timestamp() - master.start.timestamp(), zone)
    else:
        end = start + wall_duration
    return replace(master, end=end, **values)


def overlaps(event: Event, start: datetime, end: datetime) -> bool:
    """Include point events at a range's start, preserving its exclusive end."""
    event_start, event_end = event.start.timestamp(), event.end.timestamp()
    if event_start == event_end:
        return start.timestamp() <= event_start < end.timestamp()
    return event_start < end.timestamp() and event_end > start.timestamp()


def expand_series(master: Event, start: datetime, end: datetime, *, limit: int = 10000,
                  truncate: bool = False) -> list[Event]:
    if start.tzinfo is None or end.tzinfo is None or end.timestamp() <= start.timestamp():
        raise ValueError("Occurrence range must have timezone-aware, ordered boundaries.")
    if truncate and limit <= 0:
        return []
    if not master.recurring:
        return [master] if overlaps(master, start, end) else []
    zone = event_zone(master)
    duration = max(timedelta(0), master.end.astimezone(zone).replace(tzinfo=None)
                   - master.start.astimezone(zone).replace(tzinfo=None),
                   timedelta(seconds=master.end.timestamp() - master.start.timestamp()))
    lower = start.astimezone(zone) - duration
    excluded = set(master.excluded_dates)
    result = {}
    for key, payload in master.recurrence_overrides.items():
        if key in excluded:
            continue
        item = occurrence(master, key_datetime(key, master), key)
        if overlaps(item, start, end):
            result[item.id] = item
    try:
        # dateutil orders explicit dates sharing a ZoneInfo by wall time; during
        # a repeated hour that can put fold=1 before an earlier UTC fold=0 date.
        # Merge UTC-sorted finite dates with the rule's valid chronological
        # stream so both the exclusive range boundary and top-N stop are safe.
        rules = _recurrence_set(master, after=lower, include_dates=False)
        initial = master.start.astimezone(zone)
        if initial.fold and master.recurrence_rule and initial.timestamp() >= lower.timestamp():
            # Keep dateutil's existing rule-owned DTSTART deduplication. Its
            # generated anchor uses fold=0, even when DTSTART carries fold=1;
            # splitting the streams must not create an extra COUNT occurrence.
            anchor = rules.before(initial, inc=True)
            if anchor is not None and anchor.replace(tzinfo=None) == initial.replace(tzinfo=None):
                initial = anchor
        explicit = [initial, *(key_datetime(key, master) for key in master.recurrence_dates)]
        explicit.sort(key=lambda value: value.timestamp())
        finite = (value for value in explicit if value.timestamp() >= lower.timestamp())
        regular = (value for value in rules.xafter(lower, inc=True) if _valid_wall_time(value))
        for generated in merge(finite, regular, key=lambda value: value.timestamp()):
            if generated.timestamp() >= end.timestamp():
                break
            if truncate and len(result) >= limit:
                earliest = sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))[:limit]
                result = {item.id: item for item in earliest}
                if generated.timestamp() > earliest[-1].start.timestamp():
                    break
            key = recurrence_key(generated, master.all_day)
            if key in master.recurrence_overrides or key in excluded or not _valid_wall_time(generated):
                continue
            item = occurrence(master, generated, key)
            if overlaps(item, start, end):
                result[item.id] = item
            if not truncate and len(result) > limit:
                raise RecurrenceRangeError(f"This range exceeds {limit:,} occurrences. Choose a smaller date range.")
    except (OverflowError, TypeError) as exc:
        raise RecurrenceError(f"Could not expand recurrence: {exc}") from exc
    if not truncate and len(result) > limit:
        raise RecurrenceRangeError(f"This range exceeds {limit:,} occurrences. Choose a smaller date range.")
    values = sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id)
                    if truncate else (item.start.timestamp(), item.id))
    return values[:limit] if truncate else values


def expand_events(events: Iterable[Event], start: datetime, end: datetime, *, limit: int = 10000) -> list[Event]:
    result = []
    for master in events:
        result.extend(expand_series(master, start, end, limit=max(0, limit - len(result))))
        if len(result) > limit:
            raise RecurrenceRangeError(f"This range exceeds {limit:,} occurrences. Choose a smaller date range.")
    return sorted(result, key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))


def expand_events_safe(events: Iterable[Event], start: datetime, end: datetime, *, limit: int = 10000):
    """Isolate malformed resources; guard each valid resource independently.

    A broad view may contain many independent daily series. Their aggregate
    size does not invalidate later resources. A single excessively dense series
    instead raises an explicit range error, never an ingestion/quarantine issue.
    """
    result, issues = [], []
    for master in events:
        try:
            result.extend(expand_series(master, start, end, limit=limit))
        except RecurrenceRangeError:
            raise
        except (ValueError, TypeError, OverflowError, KeyError) as exc:
            issues.append((master, RecurrenceError("Unsupported or invalid recurrence rule.")))
    return sorted(result, key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id)), tuple(issues)


expand_event = expand_series
