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
from .schedule_replacement import ScheduleReplacement


MAX_MINUTELY_COUNT = 10000
MAX_REANCHOR_IDENTITIES = 10000


class RecurrenceError(ValueError):
    pass


class RecurrenceRangeError(RecurrenceError):
    """A valid schedule exceeds the requested projection's work budget."""


def event_zone(event: Event):
    return ZoneInfo(event.timezone_id) if event.timezone_id else event.start.tzinfo


def normalize_recurrence_time(value: datetime) -> datetime:
    """Resolve a generated RRULE clock through its represented instant.

    RFC 5545 erratum 4271 applies the pre-gap UTC offset to nonexistent rule
    times. Dateutil supplies that offset; a UTC round trip exposes the real
    local clock while retaining its instant and recurrence identity. Explicit
    user input is validated separately and must never be repaired this way.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError('The recurrence needs a timezone-aware start.')
    try:
        return value.astimezone(timezone.utc).astimezone(value.tzinfo)
    except (ValueError, OverflowError) as error:
        raise RecurrenceError('The recurrence extends beyond the supported calendar years.') from error


def normalize_recurrence_stream(values, *, all_day=False):
    """Normalize and sort only the local window affected by a clock gap.

    Nominal 02:45 can normalize later than a following valid 03:00. Buffer
    through the last displaced wall clock before yielding that small window;
    ordinary recurrence history remains a lazy, constant-memory stream.
    """
    if all_day:
        yield from values
        return
    displaced, boundary = [], None
    for nominal in values:
        # Close the previous local window before examining a later gap. An
        # annual rule can land inside a gap every year; extending the old
        # boundary first would otherwise retain its entire unbounded history.
        if boundary is not None and nominal.replace(tzinfo=None) > boundary:
            ordered = {value.astimezone(timezone.utc): value for value in displaced}
            yield from (ordered[key] for key in sorted(ordered))
            displaced, boundary = [], None
        actual = normalize_recurrence_time(nominal)
        if actual.replace(tzinfo=None) != nominal.replace(tzinfo=None):
            wall = actual.replace(tzinfo=None)
            boundary = wall if boundary is None else max(boundary, wall)
        if boundary is None:
            yield actual
            continue
        displaced.append(actual)
        if len(displaced) > 2 * MAX_MINUTELY_COUNT:
            raise RecurrenceRangeError('This clock-change window exceeds 20,000 recurrence instances. Choose a smaller date range.')
    if displaced:
        ordered = {value.astimezone(timezone.utc): value for value in displaced}
        yield from (ordered[key] for key in sorted(ordered))


def _exclusion_walls(value: datetime):
    """Match a normalized exclusion against dateutil's nominal gap clocks.

    Exclusions use UTC identities in storage. Dateutil compares values sharing
    a timezone by wall clock, so a real 03:30 exclusion would otherwise leave
    the nominal 02:30 gap rule value visible. Inspect only nearby offsets,
    including whole-day timezone jumps, rather than scanning recurrence history.
    """
    yield value
    offsets = set()
    for days in (-2, -1, 1, 2):
        try:
            offsets.add((value + timedelta(days=days)).utcoffset())
        except (OverflowError, ValueError):
            continue
    for offset in offsets:
        if offset is None or offset == value.utcoffset():
            continue
        try:
            nominal = (value.replace(tzinfo=None) + offset - value.utcoffset()).replace(tzinfo=value.tzinfo)
            if (not _valid_wall_time(nominal) and nominal.timestamp() == value.timestamp()):
                yield nominal
        except (OverflowError, ValueError):
            continue


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

    This helper deliberately ignores exclusions and detached ranges. Current
    authoring text describes actual starts via schedule_authoring.displayed_starts;
    use this nominal view only for legacy metadata and base-identity operations.
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
        excluded = key_datetime(key, master)
        for value in (excluded,) if master.all_day else _exclusion_walls(excluded):
            rules.exdate(value)
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


class _RelativeReanchorMap(dict):
    """Resolve a tail's ordinal only when an operation needs that identity."""
    def __init__(self, values, resolve):
        super().__init__(values)
        self._resolve = resolve

    def __missing__(self, key):
        value = self._resolve(key)
        if value is None:
            raise KeyError(key)
        self[key] = value
        return value

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def relative_reanchor_map(master: Event, start: datetime) -> dict[str, str] | None:
    """Map an authored relative program's instruction identities to a new root.

    Relative instructions are reevaluated from the independent anchor. The
    instruction's actual source start identifies its logical occurrence, even
    when that occurrence is a detached exception with another RECURRENCE-ID.
    The resulting canonical schedule can then use the new starts as identities
    while retaining each exception's content and duration.
    """
    from .patterns import (PatternError, compile_sequence, expand_sequence,
                           is_relative_sequence, sequence_tail)
    from .schedule_authoring import authoring_anchor, expression_matches
    text = master.schedule_expression
    if master.schedule_syntax != 2:
        return None
    try:
        tail = sequence_tail(text)
    except PatternError:
        return None
    relative = (not tail.prefix or is_relative_sequence(tail.prefix)) if tail else is_relative_sequence(text)
    if not relative or not expression_matches(master, text):
        return None
    previous = authoring_anchor(master)
    current = (datetime.combine(start.date(), time.min, previous.tzinfo) if master.all_day
               else start.astimezone(previous.tzinfo))
    try:
        if tail:
            old_compiled = compile_sequence(text, previous, all_day=master.all_day)
            from .patterns import shift_unbounded_expression
            shifted, _ = shift_unbounded_expression(text, previous, current)
            new_compiled = compile_sequence(shifted, current, all_day=master.all_day)
            old, new = old_compiled.prefix_starts, new_compiled.prefix_starts
        else:
            old = expand_sequence(text, previous)
            new = expand_sequence(text, current)
    except PatternError as error:
        raise RecurrenceError(str(error)) from error
    old_keys = [recurrence_key(value, master.all_day) for value in old]
    new_keys = [recurrence_key(value, master.all_day) for value in new]
    if (len(old_keys) != len(new_keys) or len(set(old_keys)) != len(old_keys)
            or len(set(new_keys)) != len(new_keys)):
        # A set program whose coincident instructions change multiplicity has
        # no one-to-one logical correspondence. Its canonical document remains
        # movable through the ordinary timestamp projection instead.
        return None
    actual_mapping = dict(zip(old_keys, new_keys))
    overrides = {key: recurrence_key(Event.from_dict(payload).start, master.all_day)
                 for key, payload in master.recurrence_overrides.items() if key not in master.excluded_dates}
    if tail:
        new_tail = sequence_tail(shifted)

        def linear_tail_identity(target):
            days = tail.interval * (7 if tail.unit == 'w' else 1)
            approximate = (target.date() - old_compiled.tail_start.date()).days // days
            # Generated local clocks can normalize across midnight (including
            # a skipped civil day). Verify nearby nominal periods by instant,
            # rather than treating that normalized wall clock as the rule time.
            for index in range(max(0, approximate - 2), approximate + 3):
                if tail.count is not None and index >= tail.count:
                    continue
                nominal = old_compiled.tail_start + timedelta(days=index * days)
                if tail.until is not None and nominal.date() > tail.until:
                    continue
                actual = nominal if master.all_day else normalize_recurrence_time(nominal)
                if recurrence_key(actual, master.all_day) != recurrence_key(target, master.all_day):
                    continue
                moved = new_compiled.tail_start + timedelta(days=index * days)
                if new_tail.until is not None and moved.date() > new_tail.until:
                    return None
                if not master.all_day:
                    moved = normalize_recurrence_time(moved)
                return recurrence_key(moved, master.all_day)
            return None

        def tail_stream(compiled, anchor):
            bare = replace(master, start=anchor, end=anchor + (master.end - master.start),
                           recurrence_rule=compiled.recurrence_rule,
                           recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
            values = normalize_recurrence_stream(_recurrence_set(bare, include_dates=False),
                                                all_day=master.all_day)
            yield from (value for value in values if value.timestamp() >= compiled.tail_start.timestamp())
        pairs = iter(zip(tail_stream(old_compiled, previous), tail_stream(new_compiled, current)))
        scanned = 0

        def resolve(key):
            nonlocal scanned
            actual = overrides.get(key, key)
            if actual in actual_mapping:
                return actual_mapping[actual]
            target = key_datetime(actual, master)
            if target.timestamp() < old_compiled.tail_start.timestamp():
                return None
            if tail.unit in ('d', 'w'):
                return linear_tail_identity(target)
            for before, after in pairs:
                scanned += 1
                if scanned > MAX_REANCHOR_IDENTITIES:
                    raise RecurrenceRangeError('This relative move needs more than 10,000 recurrence identities. Choose a nearer occurrence.')
                before_key = recurrence_key(before, master.all_day)
                actual_mapping[before_key] = recurrence_key(after, master.all_day)
                if before.timestamp() >= target.timestamp():
                    return actual_mapping.get(actual)
            return None
    else:
        def resolve(key):
            return actual_mapping.get(overrides.get(key, key))

    mapping = _RelativeReanchorMap(actual_mapping, resolve)
    for key, payload in master.recurrence_overrides.items():
        if key in master.excluded_dates:
            continue
        actual = recurrence_key(Event.from_dict(payload).start, master.all_day)
        mapped = resolve(key)
        if mapped is not None:
            mapping[key] = mapped
    return mapping


def _annotate_schedule(event: Event, preferred: str | None = None) -> Event:
    """Attach a complete expression without changing canonical identities.

    The authoring layer retains a certified literal and otherwise chooses its
    compact set language or lossless canonical document. An excluded anchor,
    detached prototype, or coincident starts never require clearing the text.
    """
    from .schedule_authoring import authoring_anchor, reconstruct_authoring
    expression = event.schedule_expression if preferred is None else preferred
    if not event.recurring and not expression and not event.expression_anchor:
        # A plain event needs no stored annotation. Its editor still has the
        # total authoring view (+0d) when a sequence is requested.
        return event
    candidate = replace(event, schedule_expression=expression if event.schedule_syntax == 2 else '',
                        schedule_syntax=2)
    authored = reconstruct_authoring(candidate)
    return replace(candidate, schedule_expression=authored.expression,
                   expression_anchor=authoring_anchor(candidate).date().isoformat())


def updated_series(master: Event, reference: Event, edited: Event,
                   replace_schedule: bool | ScheduleReplacement = False) -> Event:
    """Project a whole-series edit without accessing storage or mutating inputs.

    Relative edits reanchor an authored relative program; standard rules and
    concrete date sets shift the original master from the selected occurrence. An
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

    zone = event_zone(master)
    wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
    shared_changes = {name: getattr(edited, name) for name in ('title', 'description', 'location')
                      if getattr(edited, name) != getattr(reference, name)}

    def same_duration(first, second):
        first_wall = wall(first.end) - wall(first.start)
        second_wall = wall(second.end) - wall(second.start)
        return (first.all_day == second.all_day and first_wall == second_wall and
                (first_wall > timedelta(0) or first.end.timestamp() - first.start.timestamp()
                 == second.end.timestamp() - second.start.timestamp()))

    if replace_schedule:
        updated = replace(edited, id=master.id, calendar_id=master.calendar_id,
                          ical_uid=master.ical_uid, raw_ics=master.raw_ics,
                          series_id="", recurrence_id="", read_only=master.read_only,
                          group_id=master.group_id, recurrence_overrides={},
                          **{name: shared_changes.get(name, getattr(master, name))
                             for name in ('title', 'description', 'location')})
        overrides = {}
        if updated.recurring:
            rules = recurrence_set(updated)
            zone = event_zone(updated)
            duration = (updated.end.astimezone(zone).replace(tzinfo=None)
                        - updated.start.astimezone(zone).replace(tzinfo=None))
            inherited_overrides = {} if replace_schedule is ScheduleReplacement.RESET else master.recurrence_overrides
            for key, payload in inherited_overrides.items():
                replacement = edited.recurrence_overrides.get(key)
                if replacement is not None and replacement != payload:
                    continue
                when = key_datetime(key, updated)
                previous = rules.before(when, inc=True)
                if previous is None or recurrence_key(previous, updated.all_day) != key:
                    continue
                value = Event.from_dict(payload)
                override_start = value.start.astimezone(zone)
                own_duration = ((value.end.astimezone(zone).replace(tzinfo=None) - override_start.replace(tzinfo=None))
                                if same_duration(edited, master) else duration)
                duration_source = value if same_duration(edited, master) else updated
                if own_duration <= timedelta(0):
                    override_end = datetime.fromtimestamp(override_start.timestamp() + duration_source.end.timestamp() - duration_source.start.timestamp(), zone)
                else:
                    override_end = override_start + own_duration
                checked_wall(override_start)
                checked_wall(override_end)
                overrides[key] = replace(value, id=occurrence_id(master.id, key),
                                         start=override_start, end=override_end,
                                         timezone_id=updated.timezone_id, **shared_changes,
                                         all_day=updated.all_day, candidate=updated.candidate,
                                         schedule_expression="", expression_anchor="", schedule_syntax=1).to_dict()
            # New or changed detached ranges are authoritative. Retain their
            # own ends rather than projecting the master's duration over them.
            # Unchanged source exceptions keep the common-field projection.
            for key, payload in edited.recurrence_overrides.items():
                if replace_schedule is not ScheduleReplacement.RESET and payload == master.recurrence_overrides.get(key):
                    continue
                when = key_datetime(key, updated)
                previous = rules.before(when, inc=True)
                if previous is None or recurrence_key(previous, updated.all_day) != key:
                    continue
                value = Event.from_dict(payload)
                checked_wall(value.start)
                checked_wall(value.end)
                overrides[key] = replace(value, id=occurrence_id(master.id, key),
                    calendar_id=master.calendar_id, ical_uid=master.ical_uid,
                    series_id=master.id, recurrence_id=key, timezone_id=updated.timezone_id,
                    read_only=master.read_only, group_id=master.group_id,
                    **shared_changes,
                    candidate=updated.candidate,
                    recurrence_rule="", recurrence_dates=(), excluded_dates=(),
                    recurrence_overrides={}, raw_ics="", all_day=updated.all_day,
                    schedule_expression="", expression_anchor="", schedule_syntax=1).to_dict()
        return _annotate_schedule(replace(updated, recurrence_overrides=overrides))
    if not edited.recurrence_rule and not edited.recurrence_dates and not edited.schedule_expression:
        single = replace(edited, id=master.id, calendar_id=master.calendar_id,
                         series_id="", recurrence_id="", recurrence_overrides={}, excluded_dates=(),
                         raw_ics=master.raw_ics, ical_uid=master.ical_uid,
                         schedule_expression="", expression_anchor="", schedule_syntax=1)
        return _annotate_schedule(single)
    shift = wall(edited.start) - wall(reference.start)
    duration_changed = not same_duration(edited, reference)
    duration_source = edited if duration_changed else master
    duration = wall(duration_source.end) - wall(duration_source.start)
    start = checked_wall((wall(master.start) + shift).replace(tzinfo=zone))
    if duration <= timedelta(0):
        end = datetime.fromtimestamp(start.timestamp() + duration_source.end.timestamp() - duration_source.start.timestamp(), zone)
    else:
        end = start + duration
    checked_wall(end)

    relative_keys = None
    if (shift and edited.all_day == master.all_day and edited.schedule_syntax == 2
            and edited.schedule_expression == master.schedule_expression
            and rules_equivalent(edited.recurrence_rule, master.recurrence_rule, master)):
        relative_keys = relative_reanchor_map(master, start)

    def shift_key(key):
        if relative_keys is not None and key in relative_keys:
            return relative_keys[key]
        value = checked_wall((wall(key_datetime(key, master)) + shift).replace(tzinfo=zone))
        return recurrence_key(value, edited.all_day)

    dates = tuple(shift_key(key) for key in master.recurrence_dates) if edited.recurrence_dates == master.recurrence_dates else edited.recurrence_dates
    exclusions = tuple(shift_key(key) for key in master.excluded_dates) if edited.excluded_dates == master.excluded_dates else edited.excluded_dates
    rule = edited.recurrence_rule
    projected_expression = None
    if relative_keys is not None:
        from .patterns import compile_sequence, sequence_tail, shift_unbounded_expression
        # DTSTART is an anchor, not an implicit occurrence. Compiling at that
        # anchor also preserves its EXDATE when the program omits +0d.
        text = master.schedule_expression
        if sequence_tail(text):
            text, _ = shift_unbounded_expression(text, master.start.astimezone(zone), start)
            projected_expression = text
        compiled = compile_sequence(text, start, all_day=edited.all_day)
        fields = compiled.fields
        rule, dates = fields['recurrence_rule'], fields['recurrence_dates']
        exclusions = fields.get('excluded_dates', ())
    shifted_authoring = None
    if relative_keys is None and rules_equivalent(rule, master.recurrence_rule, master):
        if shift and master.schedule_syntax == 2 and edited.schedule_expression == master.schedule_expression:
            from .patterns import PatternError, compile_sequence, sequence_tail, shift_unbounded_expression
            from .schedule_authoring import expression_matches
            try:
                tail = sequence_tail(master.schedule_expression)
            except PatternError:
                tail = None
            if tail:
                author_start = (datetime.combine(master.start.date(), time.min, zone) if master.all_day
                                else master.start.astimezone(zone))
                author_moved = (datetime.combine(start.date(), time.min, zone) if edited.all_day
                                else start.astimezone(zone))
                if expression_matches(master, master.schedule_expression, authoring_zone=zone):
                    shifted_authoring = shift_unbounded_expression(master.schedule_expression,
                        author_start, author_moved)
                    rule = compile_sequence(shifted_authoring[0], author_moved,
                                            all_day=edited.all_day).recurrence_rule
        if shifted_authoring is None:
            rule = shift_rule(rule, shift, master)
    overrides = {}
    for key, payload in master.recurrence_overrides.items():
        moved_key = shift_key(key)
        value = Event.from_dict(payload)
        if relative_keys is not None and key in relative_keys:
            override_start = checked_wall(key_datetime(moved_key, master))
        else:
            override_start = checked_wall((wall(value.start) + shift).replace(tzinfo=zone))
        override_duration = duration if duration_changed else wall(value.end) - wall(value.start)
        override_source = edited if duration_changed else value
        if override_duration <= timedelta(0):
            override_end = datetime.fromtimestamp(override_start.timestamp() + override_source.end.timestamp() - override_source.start.timestamp(), zone)
        else:
            override_end = override_start + override_duration
        checked_wall(override_end)
        overrides[moved_key] = replace(value, id=occurrence_id(master.id, moved_key),
                                       start=override_start, end=override_end,
                                       recurrence_id=moved_key, timezone_id=master.timezone_id,
                                       **shared_changes, all_day=edited.all_day,
                                       candidate=edited.candidate,
                                       schedule_expression="", expression_anchor="", schedule_syntax=1).to_dict()
    expression, anchor = edited.schedule_expression, edited.expression_anchor
    from .ics_resources import rekey_recurrence_templates
    updated = replace(master, **shared_changes,
                      start=start, end=end, all_day=edited.all_day, candidate=edited.candidate,
                      recurrence_rule=rule, recurrence_dates=dates,
                      excluded_dates=exclusions, recurrence_overrides=overrides,
                      schedule_expression=expression, expression_anchor=anchor,
                      schedule_syntax=edited.schedule_syntax,
                      raw_ics=rekey_recurrence_templates(master, shift_key))
    # Relative instructions remain verbatim when their reevaluated schedule is
    # certified. Absolute programs and canonical documents are reconstructed
    # from the projected identities, never discarded as optional annotations.
    preferred = (projected_expression if projected_expression is not None else
                 shifted_authoring[0] if shifted_authoring is not None else expression)
    return _annotate_schedule(updated, preferred)


def _valid_wall_time(value: datetime) -> bool:
    return value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) == value.replace(tzinfo=None)


def occurrence(master: Event, start: datetime, key: str | None = None) -> Event:
    key = key or recurrence_key(start, master.all_day)
    if not master.all_day:
        start = normalize_recurrence_time(start)
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
    if not master.all_day:
        end = normalize_recurrence_time(end)
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
        # A different-zone boundary makes dateutil compare instants rather
        # than nominal wall clocks around a skipped hour. Normalized rule
        # values then merge with finite dates in the same chronological order.
        regular = normalize_recurrence_stream(rules.xafter(lower.astimezone(timezone.utc), inc=True),
                                             all_day=master.all_day)
        for generated in merge(finite, regular, key=lambda value: value.timestamp()):
            if generated.timestamp() >= end.timestamp():
                break
            if truncate and len(result) >= limit:
                earliest = sorted(result.values(), key=lambda item: (item.start.timestamp(), item.end.timestamp(), item.id))[:limit]
                result = {item.id: item for item in earliest}
                if generated.timestamp() > earliest[-1].start.timestamp():
                    break
            key = recurrence_key(generated, master.all_day)
            if key in master.recurrence_overrides or key in excluded:
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
