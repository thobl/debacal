"""Validated, Qt-independent event snapshots for scoped copy and paste."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
from typing import Iterable
from uuid import uuid4

from .models import Event
from .recurrence import event_zone, key_datetime, occurrence, occurrence_id, recurrence_key, recurrence_set, updated_series, _valid_wall_time


MAX_CLIPBOARD_BYTES = 10 * 1024 * 1024
SCOPES = frozenset({"single", "group", "future"})


def _scope(value: str) -> str:
    if value not in SCOPES:
        raise ValueError("Choose a single event, the whole selection, or this and following events.")
    return value


def _semantic(event: Event) -> dict:
    value = event.to_dict()
    value.pop("raw_ics", None)
    if not value.get("schedule_expression"):
        value.pop("schedule_syntax", None)
    return value


def _moved_times(event: Event, shift: timedelta, zone) -> tuple[datetime, datetime]:
    start = (event.start.astimezone(zone).replace(tzinfo=None) + shift).replace(tzinfo=zone)
    end = (event.end.astimezone(zone).replace(tzinfo=None) + shift).replace(tzinfo=zone)
    if end.timestamp() < start.timestamp() or (end.timestamp() == start.timestamp() and event.end.timestamp() > event.start.timestamp()):
        end = datetime.fromtimestamp(start.timestamp() + event.end.timestamp() - event.start.timestamp(), zone)
    for value in (start, end):
        if not _valid_wall_time(value):
            raise ValueError(f'The copied local time {value:%d.%m.%Y %H:%M} falls in a daylight-saving clock change. Choose another paste time.')
    return start, end


def _resolve(masters: tuple[Event, ...], reference: Event) -> Event:
    """Resolve identity from captured resources, never from an external store."""
    master = next((event for event in masters if event.id == (reference.series_id or reference.id)), None)
    if master is None:
        raise ValueError("The clipboard reference does not belong to its captured events.")
    if not reference.series_id:
        if master.recurring:
            raise ValueError("Select a dated occurrence when copying a repeating event.")
        return master
    if not master.recurring or not reference.recurrence_id:
        raise ValueError("The clipboard occurrence has no repeating master.")
    key = reference.recurrence_id
    if reference.id != occurrence_id(master.id, key) or key in master.excluded_dates:
        raise ValueError("The clipboard occurrence does not belong to its captured schedule.")
    when = key_datetime(key, master)
    if recurrence_key(when, master.all_day) != key:
        raise ValueError("The clipboard occurrence has an invalid recurrence identifier.")
    if key not in master.recurrence_overrides:
        previous = recurrence_set(master).before(when, inc=True)
        if previous is None or recurrence_key(previous, master.all_day) != key:
            raise ValueError("The clipboard occurrence does not belong to its captured schedule.")
    return occurrence(master, when, key)


def _selected(masters: tuple[Event, ...], reference: Event, scope: str) -> tuple[tuple[Event, ...], Event]:
    if scope == "single":
        from .series_operations import detach_event
        master = next(event for event in masters if event.id == (reference.series_id or reference.id))
        single = detach_event(master, reference)
        return (single,), single
    if scope == "group":
        return masters, reference
    master = next(event for event in masters if event.id == (reference.series_id or reference.id))
    if master.recurring:
        from .series_operations import partition_series
        _, tail = partition_series(master, reference)
        if tail is None:
            raise ValueError("There are no events at or after this occurrence.")
        captured = (tail,)
        return captured, _resolve(captured, reference)
    captured = tuple(event for event in masters if event.start.timestamp() >= reference.start.timestamp())
    return captured, _resolve(captured, reference)


@dataclass(frozen=True, slots=True)
class EventClipboard:
    masters: tuple[Event, ...]
    reference: Event

    def __post_init__(self) -> None:
        masters = tuple(Event.from_dict(event.to_dict()) for event in self.masters)
        if not masters or any(event.series_id for event in masters):
            raise ValueError("The clipboard must contain stored event snapshots.")
        if len({event.id for event in masters}) != len(masters):
            raise ValueError("The clipboard contains duplicate event identifiers.")
        for master in masters:
            if master.recurring:
                recurrence_set(master)
            for key, payload in master.recurrence_overrides.items():
                override = Event.from_dict(payload)
                if (override.id != occurrence_id(master.id, key) or override.series_id != master.id
                        or override.recurrence_id != key or override.ical_uid != master.ical_uid
                        or override.calendar_id != master.calendar_id or override.recurrence_rule
                        or override.recurrence_dates or override.recurrence_overrides):
                    raise ValueError("The clipboard contains an inconsistent detached occurrence.")
        reference = Event.from_dict(self.reference.to_dict())
        if _semantic(_resolve(masters, reference)) != _semantic(reference):
            raise ValueError("The clipboard reference differs from its captured event.")
        object.__setattr__(self, "masters", masters)
        object.__setattr__(self, "reference", reference)

    @classmethod
    def capture(cls, masters: Iterable[Event], reference: Event, scope: str = "single") -> EventClipboard:
        scope = _scope(scope)
        values = tuple(masters)
        if not values or len({event.id for event in values}) != len(values) or any(event.series_id for event in values):
            raise ValueError("Copy complete, distinct event masters.")
        resolved = _resolve(values, reference)
        if _semantic(resolved) != _semantic(reference):
            raise ValueError("This event changed before it could be copied. Select it again.")
        selected, anchor = _selected(values, resolved, scope)
        return cls(selected, anchor)

    def to_bytes(self) -> bytes:
        data = json.dumps({"version": 1, "events": [event.to_dict() for event in self.masters],
                           "reference": self.reference.to_dict()}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_CLIPBOARD_BYTES:
            raise ValueError("This event selection exceeds the 10 MB clipboard limit.")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> EventClipboard:
        if not isinstance(data, bytes) or len(data) > MAX_CLIPBOARD_BYTES:
            raise ValueError("The event clipboard is invalid or exceeds its 10 MB limit.")
        try:
            value = json.loads(data)
            if (not isinstance(value, dict) or set(value) != {"version", "events", "reference"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or not isinstance(value["events"], list) or not isinstance(value["reference"], dict)):
                raise ValueError
            return cls(tuple(Event.from_dict(event) for event in value["events"]), Event.from_dict(value["reference"]))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
            raise ValueError("The clipboard does not contain valid Lebacal event snapshots.") from error

    def instantiate(self, scope: str, target: datetime, calendar_id: str) -> list[Event]:
        """Prepare fresh resources; callers persist the entire list atomically."""
        scope = _scope(scope)
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("The paste target must include a timezone.")
        if not calendar_id:
            raise ValueError("Choose a calendar for the pasted events.")
        selected, _ = _selected(self.masters, self.reference, scope)
        from .series_operations import reidentify_series
        group_ids = {event.group_id: str(uuid4()) for event in selected if event.group_id}
        result = []
        for event in selected:
            zone = event_zone(event)
            wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
            shift = (timedelta(days=(target.astimezone(zone).date() - self.reference.start.astimezone(zone).date()).days)
                     if event.all_day else wall(target) - wall(self.reference.start))
            start, end = _moved_times(event, shift, zone)
            moved = replace(event, start=start, end=end)
            if event.recurring:
                moved = updated_series(event, event, moved)
                # Whole-series editing intentionally applies the editor's
                # common fields/duration to exceptions. Copying preserves each
                # exception's own content and duration while moving its dates.
                preserved = {}
                for key, payload in event.recurrence_overrides.items():
                    new_key = recurrence_key((wall(key_datetime(key, event)) + shift).replace(tzinfo=zone), event.all_day)
                    override = Event.from_dict(payload)
                    override_start, override_end = _moved_times(override, shift, zone)
                    preserved[new_key] = replace(override, id=occurrence_id(event.id, new_key),
                                                 series_id=event.id, recurrence_id=new_key,
                                                 start=override_start, end=override_end).to_dict()
                moved = replace(moved, recurrence_overrides=preserved)
            new_id = str(uuid4())
            cloned = reidentify_series(moved, new_id, new_id)
            group_id = group_ids.get(event.group_id)
            overrides = {key: replace(Event.from_dict(payload), calendar_id=calendar_id,
                                      read_only=False, group_id=group_id).to_dict()
                         for key, payload in cloned.recurrence_overrides.items()}
            result.append(replace(cloned, calendar_id=calendar_id, read_only=False,
                                  group_id=group_id, recurrence_overrides=overrides))
        return result
