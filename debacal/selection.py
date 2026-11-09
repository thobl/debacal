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


def is_whole_root(event: Event) -> bool:
    """A selected raw recurring resource explicitly represents its family."""
    return event.recurring and not event.series_id


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

    def select(self, event: Event, additive: bool = False, *, master: Event | None = None) -> None:
        if not additive:
            self._refs = [event]
            return
        master_family = lambda value: (value.calendar_id, value.series_id or value.id)
        related_roots = [value for value in self._refs if master_family(value) == master_family(event)]
        if is_whole_root(event) or any(is_whole_root(value) for value in related_roots):
            self._refs = [value for value in self._refs if master_family(value) != master_family(event)]
            if is_whole_root(event) and not any(is_whole_root(value) for value in related_roots):
                self._refs.append(event)
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
                if master is not None:
                    return _nominal(value, master)
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
    if event.recurrence_id:
        from .coincident_occurrences import logical_occurrence_start
        return logical_occurrence_start(master, event.recurrence_id).timestamp()
    return event.start.timestamp()


def selection_roots(store, refs: Iterable[Event], scope: str) -> list[Event]:
    """Normalize overlapping roots without expanding infinite recurrence.

    Callers that mutate storage review every original reference first. Missing
    roots are ignored here so repainting a stale selection remains harmless.
    """
    scope = validate_scope(scope)
    refs = list(dict((event.id, event) for event in refs).values())
    whole = {event.id for event in refs if is_whole_root(event)}
    refs = [event for event in refs if not event.series_id or event.series_id not in whole]
    if scope == "single":
        return refs
    roots: dict[str, Event] = {}
    groups: dict[tuple[str, str], Event] = {}
    for reference in refs:
        master = store.get_event(_master_id(reference))
        if master is None:
            continue
        if master.recurring or reference.series_id:
            value = master if scope == "group" or is_whole_root(reference) else reference
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
        members = (store.list_group_events(calendar_id, group_id) if hasattr(store, 'list_group_events')
                   else store.list_events(calendar_ids=[calendar_id]))
        for member in members:
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
        explicit = {root.id for root in roots}
        whole = {root.id for root in roots if is_whole_root(root)}
        return {event.id for event in visible_events if event.id in explicit or _master_id(event) in whole}
    by_master = {_master_id(root): root for root in roots}
    future_boundaries = {}
    result = set()
    for event in visible_events:
        root = by_master.get(_master_id(event))
        if root is None:
            continue
        if scope == "future" and not is_whole_root(root):
            master_id = _master_id(event)
            if master_id not in future_boundaries:
                master = store.get_event(master_id)
                future_boundaries[master_id] = (master, _nominal(root, master) if master is not None else None)
            master, boundary = future_boundaries[master_id]
            if master is None or _nominal(event, master) < boundary:
                continue
        result.add(event.id)
    return result


def capture_selection(store, refs: Iterable[Event], scope: str, *,
                      view_origin: date | None = None, view_kind: str | None = None,
                      reference: Event | None = None):
    """Capture an exact union, never references to mutable source events."""
    from .event_clipboard import EventClipboard, _resolve
    from .series_operations import detach_event, partition_series
    refs = list(refs)
    if not refs:
        raise ValueError("Select an event before copying.")
    with store.read_snapshot():
        for value in [*refs, *([reference] if reference is not None else [])]:
            store._reviewed_master(value)
        roots = selection_roots(store, refs, scope)
        snapshots = []
        for root in roots:
            master = store.get_event(_master_id(root))
            if is_whole_root(root):
                snapshots.append(master)
            elif scope == "single":
                snapshots.append(detach_event(master, root))
            elif scope == "future" and master.recurring:
                _, tail = partition_series(master, root)
                if tail is not None:
                    snapshots.append(tail)
            else:
                snapshots.append(master)
        primary = reference or refs[-1]
        if is_whole_root(primary):
            from .recurrence import occurrence, recurrence_set
            try:
                primary = occurrence(primary, next(iter(recurrence_set(primary))))
            except StopIteration:
                raise ValueError("This selected series contains no dated occurrences.") from None
        detached = next((event for event in snapshots if event.id == primary.id and not event.recurring), None)
        if detached is None and scope == "future" and primary.series_id:
            # The last RDATE can become an ordinary tail. Its resource ID is
            # still the master's, while the reviewed reference is an occurrence.
            detached = next((event for event in snapshots
                             if event.id == primary.series_id and not event.recurring
                             and event.start.timestamp() == key_datetime(primary.recurrence_id, event).timestamp()), None)
        primary = detached if detached is not None else _resolve(tuple(snapshots), primary)
        return EventClipboard(tuple(snapshots), primary, view_origin=view_origin,
                              view_kind=view_kind, exact_selection=True)
