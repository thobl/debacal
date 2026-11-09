"""Choose imported occurrences without flattening unbounded recurrence rules."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import json
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .ics_resources import ParseReport, serialize_resource
from .models import Event
from .recurrence import key_datetime, occurrence
from .series_operations import _end_at, _has_visible_occurrence, detach_event, reidentify_series
from .timeutil import local_zone


def _family(event: Event) -> tuple[str, str]:
    return event.calendar_id, event.series_id or event.id


def _key(event: Event) -> tuple[str, str, str]:
    return (*_family(event), event.recurrence_id)


def display_date(event: Event) -> date:
    return event.start.date() if event.all_day or event.floating_time else event.start.astimezone(local_zone()).date()


def _without_authoring(event: Event) -> Event:
    return replace(event, schedule_expression='', expression_anchor='', schedule_syntax=1)


def _partial_identity(master: Event, keys: Iterable[str]) -> str:
    value = json.dumps(['debacal-import-selection-v1', master.calendar_id, master.ical_uid,
                        sorted(keys)], ensure_ascii=False, separators=(',', ':'))
    return str(uuid5(NAMESPACE_URL, value))


@dataclass(frozen=True, slots=True)
class ImportSelectionSnapshot:
    """Immutable inclusion decisions safe to pass to an import worker.

    ``choices`` contain only decisions differing from the global default.
    Optional effective masters avoid resurrecting an older duplicate resource
    when its last source version was deselected in the combined preview.
    """
    all_selected: bool = True
    choices: tuple[Event, ...] = ()
    masters: tuple[Event, ...] = ()
    _keys: frozenset = field(init=False, repr=False, compare=False)
    _by_family: dict = field(init=False, repr=False, compare=False)
    _masters: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, '_keys', frozenset(_key(event) for event in self.choices))
        by_family = {}
        for event in self.choices:
            by_family.setdefault(_family(event), []).append(event)
        object.__setattr__(self, '_by_family', by_family)
        object.__setattr__(self, '_masters', {_family(event): event for event in self.masters})

    @property
    def has_selection(self) -> bool:
        return self.all_selected or bool(self.choices)

    def is_selected(self, event: Event) -> bool:
        return self.all_selected != (_key(event) in self._keys)

    def filter_report(self, report: ParseReport) -> ParseReport:
        events = []
        for master in report.events:
            effective = self._masters.get(_family(master))
            if effective is not None and master != effective:
                continue
            selected = self._filter_master(master)
            if selected is not None:
                events.append(selected)
        return ParseReport(tuple(events), report.issues, report.rejected)

    def _filter_master(self, master: Event) -> Event | None:
        if not master.recurring:
            return master if self.is_selected(master) else None
        choices = self._by_family.get(_family(master), ())
        if self.all_selected:
            if not choices:
                return master
            # A nonrecurring final duplicate can supersede an older series.
            if any(not event.recurrence_id for event in choices):
                return None
            excluded = set(master.excluded_dates) | {event.recurrence_id for event in choices}
            result = _without_authoring(replace(master, excluded_dates=tuple(excluded),
                recurrence_overrides={key: value for key, value in master.recurrence_overrides.items()
                                      if key not in excluded}))
            return result if _has_visible_occurrence(result) else None
        keys = sorted({event.recurrence_id for event in choices if event.recurrence_id
                       and event.recurrence_id not in master.excluded_dates},
                      key=lambda key: key_datetime(key, master).timestamp())
        if not keys:
            return None
        if len(keys) == 1:
            reference = occurrence(master, key_datetime(keys[0], master), keys[0])
            result = detach_event(master, reference)
        else:
            start = key_datetime(keys[0], master)
            included = set(keys)
            result = _without_authoring(replace(master, start=start, end=_end_at(master, start),
                recurrence_rule='', recurrence_dates=tuple(keys[1:]), excluded_dates=(),
                recurrence_overrides={key: value for key, value in master.recurrence_overrides.items()
                                      if key in included}, series_id='', recurrence_id=''))
        identity = _partial_identity(master, keys)
        result = reidentify_series(result, identity, identity)
        # Clear old native resource identities and unused detached templates.
        # Destination remapping then gives this finite partial resource its own
        # ID rather than preserving the full original series' native ID.
        if result.raw_ics:
            result = replace(result, raw_ics=serialize_resource(result).decode())
        return result


class ImportSelection:
    """Interactive selection; only explicit changes need to be remembered."""

    def __init__(self, all_selected=True):
        self.all_selected = bool(all_selected)
        self._choices: dict[tuple[str, str, str], Event] = {}

    @property
    def has_selection(self):
        return self.all_selected or bool(self._choices)

    def is_selected(self, event: Event) -> bool:
        return self.all_selected != (_key(event) in self._choices)

    def set_selected(self, events: Iterable[Event], selected: bool):
        for event in events:
            if bool(selected) == self.all_selected:
                self._choices.pop(_key(event), None)
            else:
                self._choices[_key(event)] = event

    def toggle(self, event: Event):
        self.set_selected((event,), not self.is_selected(event))

    def toggle_date(self, day: date, events: Iterable[Event]):
        events = tuple(event for event in events if display_date(event) == day)
        self.set_selected(events, not all(self.is_selected(event) for event in events))

    def select_all(self):
        self.all_selected = True
        self._choices.clear()

    def select_none(self):
        self.all_selected = False
        self._choices.clear()

    def snapshot(self, masters: Iterable[Event] = ()) -> ImportSelectionSnapshot:
        return ImportSelectionSnapshot(self.all_selected, tuple(self._choices.values()), tuple(masters))
