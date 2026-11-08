"""Small, UI-independent calendar values.

Dates are timezone-aware. JSON keeps the original UTC offset; SQLite uses UTC
timestamps for ordering so events from different timezones compare correctly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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

    def __post_init__(self) -> None:
        if not self.id or not self.calendar_id:
            raise ValueError("An event needs an ID and a calendar.")
        if not self.title.strip():
            raise ValueError("An event needs a title.")
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
        if self.end.timestamp() < self.start.timestamp():
            raise ValueError("The end must not be before the start.")
        if self.all_day and self.end.timestamp() == self.start.timestamp():
            raise ValueError("All-day events must end after the start date.")
        if not self.ical_uid:
            object.__setattr__(self, "ical_uid", self.id)
        zone = self.timezone_id or getattr(self.start.tzinfo, "key", "")
        # UTC needs no named-zone definition. Both ICS parsers must retain the
        # same semantics even when one returns ZoneInfo('UTC') and one UTC.
        object.__setattr__(self, "timezone_id", "" if zone in {"UTC", "Etc/UTC"} else zone)
        if self.timezone_id:
            try:
                ZoneInfo(self.timezone_id)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError(f"Unknown event timezone: {self.timezone_id}") from exc
        def canonical_date(value):
            if len(value) == 10:
                return datetime.fromisoformat(value).date().isoformat()
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(self.timezone_id) if self.timezone_id else self.start.tzinfo)
            if self.all_day:
                zone = ZoneInfo(self.timezone_id) if self.timezone_id else self.start.tzinfo
                return parsed.astimezone(zone).date().isoformat()
            return parsed.astimezone(timezone.utc).isoformat()

        object.__setattr__(self, "recurrence_dates", tuple(sorted({canonical_date(value) for value in self.recurrence_dates})))
        object.__setattr__(self, "excluded_dates", tuple(sorted({canonical_date(value) for value in self.excluded_dates})))
        object.__setattr__(self, "recurrence_overrides", {canonical_date(key): deepcopy(value) for key, value in self.recurrence_overrides.items()})
        if not isinstance(self.duration_spec, str):
            raise ValueError('An event duration specification must be text.')
        if self.duration_spec:
            from .event_duration import add_duration, normalized_spec
            spec = normalized_spec(self.duration_spec)
            if spec and not self.all_day:
                zone = ZoneInfo(self.timezone_id) if self.timezone_id else self.start.tzinfo
                try:
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
        value = asdict(self)
        value["start"] = self.start.isoformat()
        value["end"] = self.end.isoformat()
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
        if value.get("timezone_id"):
            try:
                zone = ZoneInfo(value["timezone_id"])
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError(f"Unknown event timezone: {value['timezone_id']}") from exc
            if value.get('all_day'):
                # DATE values float. A midnight clock gap must not turn its
                # retained date into 01:00 (or a different civil date).
                value["start"] = value["start"].replace(tzinfo=zone)
                value["end"] = value["end"].replace(tzinfo=zone)
            else:
                value["start"] = value["start"].astimezone(zone)
                value["end"] = value["end"].astimezone(zone)
        return cls(**value)
