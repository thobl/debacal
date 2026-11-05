"""Selection state and the exact union represented by each event scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable

from .models import Event
from .recurrence import key_datetime


SCOPES = frozenset({"single", "group", "future"})


def validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError("Choose one event, all events, or this and following events.")
    return scope


@dataclass
class EventSelection:
    scope: str = "single"
    _refs: list[Event] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self.set_scope(self.scope)
        self.replace(self._refs)

    @property
    def refs(self) -> tuple[Event, ...]:
        return tuple(self._refs)

    @property
    def primary(self) -> Event | None:
        return self._refs[-1] if self._refs else None

    @property
    def ids(self) -> set[str]:
        return {event.id for event in self._refs}

    def set_scope(self, scope: str) -> None:
        self.scope = validate_scope(scope)

    def replace(self, refs: Iterable[Event]) -> None:
        values = {}
        for event in refs:
            values.pop(event.id, None)
            values[event.id] = event
        self._refs = list(values.values())

    def select(self, event: Event, additive: bool = False) -> None:
        if not additive:
            self._refs = [event]
            return
        if self.scope == "single":
            if event.id in self.ids:
                self.discard(event.id)
            else:
                self._refs.append(event)
            return
        def family(value):
            key = ("series", value.series_id or value.id) if value.series_id or value.recurring else (
                ("group", value.group_id) if value.group_id else ("event", value.id))
            return value.calendar_id, key
        related = [value for value in self._refs if family(value) == family(event)]
        if not related:
            self._refs.append(event)
            return
        self._refs = [value for value in self._refs if family(value) != family(event)]
        if self.scope == "future":
            def nominal(value):
                if not value.recurrence_id:
                    return value.start.timestamp()
                parsed = datetime.fromisoformat(value.recurrence_id)
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
            if nominal(event) < min(nominal(value) for value in related):
                self._refs.append(event)

    def discard(self, event_id: str) -> None:
        self._refs = [event for event in self._refs if event.id != event_id]

    def clear(self) -> None:
        self._refs.clear()


def _master_id(event: Event) -> str:
    return event.series_id or event.id


def _nominal(event: Event, master: Event) -> float:
    return key_datetime(event.recurrence_id, master).timestamp() if event.recurrence_id else event.start.timestamp()


def selection_roots(store, refs: Iterable[Event], scope: str) -> list[Event]:
    """Normalize overlapping roots without expanding infinite recurrence.

    Callers that mutate storage review every original reference first. Missing
    roots are ignored here so repainting a stale selection remains harmless.
    """
    scope = validate_scope(scope)
    refs = list(dict((event.id, event) for event in refs).values())
    if scope == "single":
        return refs
    roots: dict[str, Event] = {}
    groups: dict[tuple[str, str], Event] = {}
    for reference in refs:
        master = store.get_event(_master_id(reference))
        if master is None:
            continue
        if master.recurring or reference.series_id:
            value = master if scope == "group" else reference
            previous = roots.get(master.id)
            if previous is None or (scope == "future" and _nominal(value, master) < _nominal(previous, master)):
                roots[master.id] = value
        elif master.group_id:
            key = (master.calendar_id, master.group_id)
            previous = groups.get(key)
            if previous is None or reference.start.timestamp() < previous.start.timestamp():
                groups[key] = reference
        else:
            roots[master.id] = master
    for (calendar_id, group_id), reference in groups.items():
        for member in store.list_events(calendar_ids=[calendar_id]):
            if member.group_id != group_id or (scope == "future" and member.start.timestamp() < reference.start.timestamp()):
                continue
            previous = roots.get(member.id)
            if previous is None or scope == "group" or _nominal(member, member) < _nominal(previous, member):
                roots[member.id] = member
    return list(roots.values())


def affected_visible(store, refs: Iterable[Event], scope: str, visible_events: Iterable[Event]) -> set[str]:
    """Return exactly the visible occurrence IDs affected by the selection."""
    roots = selection_roots(store, refs, scope)
    if scope == "single":
        return {event.id for event in visible_events if event.id in {root.id for root in roots}}
    by_master = {_master_id(root): root for root in roots}
    result = set()
    for event in visible_events:
        root = by_master.get(_master_id(event))
        if root is None:
            continue
        if scope == "future":
            master = store.get_event(_master_id(event))
            if master is None or _nominal(event, master) < _nominal(root, master):
                continue
        result.add(event.id)
    return result


def capture_selection(store, refs: Iterable[Event], scope: str, *,
                      view_origin: date | None = None, view_kind: str | None = None):
    """Capture an exact union, never references to mutable source events."""
    from .event_clipboard import EventClipboard, _resolve
    from .series_operations import detach_event, partition_series
    refs = list(refs)
    if not refs:
        raise ValueError("Select an event before copying.")
    with store._transaction():
        for reference in refs:
            store._reviewed_master(reference)
        roots = selection_roots(store, refs, scope)
        snapshots = []
        for root in roots:
            master = store.get_event(_master_id(root))
            if scope == "single":
                snapshots.append(detach_event(master, root))
            elif scope == "future" and master.recurring:
                _, tail = partition_series(master, root)
                if tail is not None:
                    snapshots.append(tail)
            else:
                snapshots.append(master)
        primary = refs[-1]
        if scope == "single":
            primary = next(event for event in snapshots if event.id == primary.id)
        else:
            primary = _resolve(tuple(snapshots), primary)
        return EventClipboard(tuple(snapshots), primary, view_origin=view_origin,
                              view_kind=view_kind, exact_selection=True)
