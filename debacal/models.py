"""Small, UI-independent calendar values.

Internal datetimes are timezone-aware. JSON retains scoped timezone definitions
and offsets for fixed instants, or plain wall clocks for floating timed events.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .timezones import event_timezone, resolve_timezone, timezone_identifier, validate_definitions


@dataclass(frozen=True, slots=True)
class Calendar:
    id: str
    name: str
    color: str
    visible: bool = True
    kind: str = "local"
    url: str = ""
    username: str = ""
    read_only: bool = False
    server_color: str = ""
    use_server_color: bool = True
    enabled: bool = True
    server_name: str = ""
    name_alias: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.name.strip():
            raise ValueError("A calendar needs an ID and a name.")
        if self.kind not in {"local", "caldav", "ics"}:
            raise ValueError(f"Unknown calendar type: {self.kind}")
        if type(self.enabled) is not bool:
            raise ValueError("A calendar's enabled state must be true or false.")
        if not isinstance(self.server_name, str) or not isinstance(self.name_alias, str):
            raise ValueError("Calendar source names and aliases must be text.")
        if self.server_color and self.use_server_color:
            object.__setattr__(self, "color", self.server_color)
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Use a calendar URL without embedded credentials. Supply the password separately.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Calendar:
        return cls(**value)


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    group_id: str | None = None
    candidate: bool = False
    all_day: bool = False
    read_only: bool = False
    ical_uid: str = ""
    recurrence_rule: str = ""
    recurrence_dates: tuple[str, ...] = ()
    excluded_dates: tuple[str, ...] = ()
    recurrence_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    timezone_id: str = ""
    timezone_definitions: dict[str, str] = field(default_factory=dict)
    floating_time: bool = False
    raw_ics: str = ""
    # These identify a displayed occurrence. Stored series masters leave them
    # empty; edits are embedded in the master so a series is one sync resource.
    series_id: str = ""
    recurrence_id: str = ""
    # An optional authoring source. Standard RRULE/RDATE fields remain usable
    # by other calendar apps; this literal text makes custom patterns editable.
    schedule_expression: str = ""
    expression_anchor: str = ""
    # Version 1 marks unannotated or obsolete data; it has no interpreter.
    # Version 2 uses the explicit-reference language or a schedule document.
    schedule_syntax: int = 1
    # Only Cut needs local range semantics; Skip/Shift are standard RRULEs.
    range_policy: str = ""
    # Timed ranges normally retain elapsed time. RFC DURATION day/week parts
    # retain nominal calendar days instead; clock-only durations need no flag.
    duration_spec: str = ""
    # Internal display/query values omit transport text. Replacements retain
    # this marker so edits can recover the canonical ICS before saving.
    _query_projection: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.id or not self.calendar_id:
            raise ValueError("An event needs an ID and a calendar.")
        if not self.title.strip():
            raise ValueError("An event needs a title.")
        if type(self.floating_time) is not bool or self.floating_time and self.all_day:
            raise ValueError('Only timed events can use floating wall-clock times.')
        if not isinstance(self.schedule_expression, str) or not isinstance(self.expression_anchor, str):
            raise ValueError("Custom schedule text and its anchor must be strings.")
        if type(self.schedule_syntax) is not int or self.schedule_syntax not in {1, 2}:
            raise ValueError("Custom schedule syntax version must be 1 or 2.")
        if not isinstance(self.range_policy, str) or self.range_policy not in {"", "cut"}:
            raise ValueError("The stored event range policy must be empty or cut.")
        if self.expression_anchor:
            try:
                object.__setattr__(self, "expression_anchor", date.fromisoformat(self.expression_anchor).isoformat())
            except ValueError as exc:
                raise ValueError("A custom schedule anchor must be a valid ISO date.") from exc
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.start, self.end)):
            raise ValueError("Event start and end must include a timezone.")
        if not self.all_day and (self.end.replace(tzinfo=None) < self.start.replace(tzinfo=None)
                                if self.floating_time else self.end.timestamp() < self.start.timestamp()):
            raise ValueError("The end must not be before the start.")
        if self.all_day and self.end.replace(tzinfo=None) <= self.start.replace(tzinfo=None):
            raise ValueError("All-day events must end after the start date.")
        if not self.ical_uid:
            object.__setattr__(self, "ical_uid", self.id)
        definitions = validate_definitions(self.timezone_definitions)
        object.__setattr__(self, 'timezone_definitions', definitions)
        zone = '' if self.floating_time else self.timezone_id or getattr(self.start.tzinfo, "key", "")
        resolved = resolve_timezone(zone, definitions) if zone else None
        if resolved is not None and zone not in definitions:
            # Known producer prefixes are database aliases. Store the actual
            # database key so recurrence, editing and VTIMEZONE generation all
            # use the same identity. Explicit resource definitions keep theirs.
            database_id = timezone_identifier(resolved)
            if database_id not in definitions:
                zone = database_id
            # Another instance in this UID may use the database-shaped name
            # for different explicit rules. Preserve the alias in that case
            # so its database rules cannot acquire that other definition.
        # UTC needs no named-zone definition. Both ICS parsers must retain the
        # same semantics even when one returns ZoneInfo('UTC') and one UTC.
        object.__setattr__(self, "timezone_id", "" if zone in {"UTC", "Etc/UTC"} and zone not in definitions else zone)
        if self.timezone_id:
            if self.timezone_id in definitions:
                if self.all_day:
                    object.__setattr__(self, 'start', self.start.replace(tzinfo=resolved))
                    object.__setattr__(self, 'end', self.end.replace(tzinfo=resolved))
                else:
                    object.__setattr__(self, 'start', self.start.astimezone(resolved))
                    object.__setattr__(self, 'end', self.end.astimezone(resolved))
        def canonical_date(value):
            if len(value) == 10:
                return datetime.fromisoformat(value).date().isoformat()
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=event_timezone(self))
            if self.all_day:
                zone = event_timezone(self)
                return parsed.astimezone(zone).date().isoformat()
            if self.floating_time:
                return parsed.astimezone(event_timezone(self)).replace(tzinfo=None).isoformat()
            return parsed.astimezone(timezone.utc).isoformat()

        object.__setattr__(self, "recurrence_dates", tuple(sorted({canonical_date(value) for value in self.recurrence_dates})))
        object.__setattr__(self, "excluded_dates", tuple(sorted({canonical_date(value) for value in self.excluded_dates})))
        object.__setattr__(self, "recurrence_overrides", {canonical_date(key): deepcopy(value) for key, value in self.recurrence_overrides.items()})
        if not isinstance(self.duration_spec, str):
            raise ValueError('An event duration specification must be text.')
        if self.duration_spec:
            from .event_duration import add_duration, normalized_spec, parse_duration_spec
            spec = normalized_spec(self.duration_spec)
            if spec and not self.all_day:
                zone = event_timezone(self)
                try:
                    if self.floating_time:
                        days, seconds = parse_duration_spec(spec)
                        matches = (self.start.replace(tzinfo=None) + timedelta(days=days, seconds=seconds)
                                   == self.end.replace(tzinfo=None))
                    else:
                        matches = add_duration(self.start.astimezone(zone), spec).timestamp() == self.end.timestamp()
                except (OverflowError, ValueError) as exc:
                    raise ValueError('The nominal duration exceeds the supported calendar range.') from exc
                if not matches:
                    spec = ''  # An explicit endpoint edit replaces nominal intent.
            else:
                spec = ''
            object.__setattr__(self, 'duration_spec', spec)

    @property
    def recurring(self) -> bool:
        # Exclusions alone can describe an empty schedule or an omitted anchor.
        # Projected occurrences already have their own identity and must never
        # be expanded again merely because they retain the author's text.
        return not self.series_id and bool(self.recurrence_rule or self.recurrence_dates or self.excluded_dates
                                          or self.recurrence_overrides
                                          or self.schedule_expression not in ('', '+0d'))

    def to_dict(self) -> dict[str, Any]:
        # Custom dateutil tzinfo values hold locks and cannot be deep-copied.
        # Datetimes become strings anyway; copy only the remaining JSON data.
        value = {item.name: deepcopy(getattr(self, item.name)) for item in fields(self)
                 if item.name not in {'start', 'end', '_query_projection'}}
        value["start"] = (self.start.replace(tzinfo=None) if self.floating_time else self.start).isoformat()
        value["end"] = (self.end.replace(tzinfo=None) if self.floating_time else self.end).isoformat()
        value["recurrence_dates"] = list(self.recurrence_dates)
        value["excluded_dates"] = list(self.excluded_dates)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Event:
        value = dict(value)
        if 'duration_spec' not in value:
            from .event_duration import retained_duration_spec
            value['duration_spec'] = retained_duration_spec(value.get('raw_ics', ''))
        for field in ("start", "end"):
            if isinstance(value[field], str):
                value[field] = datetime.fromisoformat(value[field])
        if value.get('floating_time'):
            from .timeutil import local_zone
            zone = local_zone()
            value['start'] = value['start'].replace(tzinfo=zone)
            value['end'] = value['end'].replace(tzinfo=zone)
        elif value.get("timezone_id"):
            zone = resolve_timezone(value['timezone_id'], value.get('timezone_definitions', {}))
            if value.get('all_day'):
                # DATE values float. A midnight clock gap must not turn its
                # retained date into 01:00 (or a different civil date).
                value["start"] = value["start"].replace(tzinfo=zone)
                value["end"] = value["end"].replace(tzinfo=zone)
            else:
                value["start"] = value["start"].astimezone(zone)
                value["end"] = value["end"].astimezone(zone)
        return cls(**value)


_EVENT_FIELD_NAMES = tuple(item.name for item in fields(Event))


def copy_event(event: Event) -> Event:
    """Shallow-copy a validated immutable event without pickle/validation work.

    Internal projections share source strings and timezone data, then replace
    only their identity and range. The static field list includes private
    projection provenance so those replacements retain transport semantics.
    """
    result = object.__new__(Event)
    write = object.__setattr__
    for name in _EVENT_FIELD_NAMES:
        write(result, name, getattr(event, name))
    return result
