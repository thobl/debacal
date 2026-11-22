"""Read an import once, then preview and import the same calendar snapshot.

All functions are independent of widgets and storage. Callers run file reading,
parsing, recurrence expansion, and destination remapping on a worker thread.
"""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
import hashlib
from pathlib import Path
from typing import get_args
from uuid import NAMESPACE_URL, uuid5

from .alarm_ics import hydrate_alarms
from .alarms import Alarm
from .ics import parse_ics_report
from .ics_resources import ParseReport, RejectedResource, ResourceIssue, reject_duplicate_identities
from .import_sources import ImportSource, MAX_IMPORT_BYTES, MAX_PENDING_IMPORT_BYTES
from .models import Event
from .preferences import Preferences
from .recurrence import (
    RecurrenceRangeError, event_zone, expand_series, key_datetime, occurrence,
    occurrence_id, recurrence_key, recurrence_set,
)
from .reminder_preferences import ImportReminderMode, instantiate_reminder_templates, relative_templates
from .timeutil import local_zone, now


PREVIEW_CALENDAR_ID = '__debacal_import_preview__'
MAX_IMPORT_EVENTS = 50_000


def _selected_overrides(event: Event):
    excluded = set(event.excluded_dates)
    return ((key, payload) for key, payload in event.recurrence_overrides.items() if key not in excluded)


def count_reminder_sources(report: ParseReport) -> tuple[int, int]:
    """Count alarm-bearing and total selected components without expanding a rule."""
    with_alarms = total = 0
    for source in report.events:
        event = hydrate_alarms(source)
        with_alarms += bool(event.alarms)
        total += 1
        for _, payload in _selected_overrides(event):
            with_alarms += bool(payload.get('alarms'))
            total += 1
    return with_alarms, total


def _reviewed_templates(mode: str, alarms: tuple[Alarm, ...]) -> tuple[Alarm, ...]:
    if mode not in get_args(ImportReminderMode):
        raise ValueError('Unknown import reminder mode.')
    return relative_templates(alarms)


def transform_import_reminders(report: ParseReport, mode: str, alarms: tuple[Alarm, ...]) -> ParseReport:
    """Apply a manual policy to the filtered and destination-remapped report."""
    templates = _reviewed_templates(mode, alarms)
    if mode == 'keep' or (mode == 'add_missing' and not templates):
        return report
    if mode == 'remove':
        templates = ()

    events = []
    for source in report.events:
        event = hydrate_alarms(source)
        master_alarms = event.alarms
        if mode != 'add_missing' or master_alarms == ():
            master_alarms = instantiate_reminder_templates(templates, calendar_id=event.calendar_id,
                                                          event_uid=event.ical_uid)
        overrides = dict(event.recurrence_overrides)
        for key, payload in _selected_overrides(event):
            existing = payload.get('alarms')
            if mode == 'add_missing' and (existing is None or existing):
                continue
            child_alarms = instantiate_reminder_templates(templates, calendar_id=event.calendar_id,
                                                         event_uid=event.ical_uid, recurrence_id=key)
            overrides[key] = dict(payload, alarms=[alarm.to_dict() for alarm in child_alarms])
        events.append(replace(event, alarms=master_alarms, recurrence_overrides=overrides))
    return replace(report, events=tuple(events))


def _display_date(event: Event, value: datetime | None = None) -> date:
    value = event.start if value is None else value
    return value.date() if event.all_day or event.floating_time else value.astimezone(local_zone()).date()


def remap_calendar(report: ParseReport, calendar_id: str) -> ParseReport:
    """Match direct parsing into a destination without rereading its source.

    UID-derived IDs depend on the destination calendar. Native Debacal exports
    can instead preserve their explicit resource ID when imported back into the
    calendar they came from. Detached exceptions must follow that same ID.
    """
    if not calendar_id:
        raise ValueError('Choose a calendar for the imported events.')
    from .ics_fallback import components, prop

    events = []
    for event in report.events:
        identifier = str(uuid5(NAMESPACE_URL, f'debacal:{calendar_id}:{event.ical_uid}:'))
        if event.raw_ics:
            master = next((component for component in components(event.raw_ics)
                           if 'RECURRENCE-ID' not in component), None)
            if master is not None and prop(master, 'X-DEBACAL-CALENDAR') == calendar_id:
                identifier = prop(master, 'X-DEBACAL-ID', identifier)
        overrides = {
            key: replace(Event.from_dict(payload), id=occurrence_id(identifier, key),
                         calendar_id=calendar_id, series_id=identifier, recurrence_id=key).to_dict()
            for key, payload in event.recurrence_overrides.items()
        }
        events.append(replace(event, id=identifier, calendar_id=calendar_id,
                              recurrence_overrides=overrides))
    return reject_duplicate_identities(ParseReport(tuple(events), report.issues, report.rejected))


def reidentify_imports(report: ParseReport, *, existing_events=()) -> ParseReport:
    """Update matching UIDs even when another client removed native metadata.

    The destination calendar and standard UID own the persisted identity.
    Detached occurrences follow that master; pending source authoring text is
    retained only when it still describes the complete canonical schedule.
    """
    owners = {}
    for event in existing_events:
        owners.setdefault((event.calendar_id, event.ical_uid), set()).add(event.id)
    events, issues, rejected = [], list(report.issues), list(report.rejected)
    for event in report.events:
        identifiers = owners.get((event.calendar_id, event.ical_uid), ())
        if len(identifiers) > 1:
            resource_id = hashlib.sha256(event.ical_uid.encode()).hexdigest()[:12]
            reason = 'Multiple stored events share this calendar UID. Resolve their duplicate identities before importing an update.'
            issues.append(ResourceIssue(resource_id, reason))
            rejected.append(RejectedResource(event.ical_uid, event.raw_ics, resource_id, reason))
            continue
        if identifiers and event.id not in identifiers:
            identifier, = identifiers
            overrides = {
                key: replace(Event.from_dict(payload), id=occurrence_id(identifier, key),
                             series_id=identifier, recurrence_id=key).to_dict()
                for key, payload in event.recurrence_overrides.items()
            }
            event = replace(event, id=identifier, recurrence_overrides=overrides)
            if event.schedule_expression:
                from .schedule_authoring import expression_matches, reconstruct_authoring
                if not expression_matches(event, event.schedule_expression):
                    view = reconstruct_authoring(event)
                    event = replace(event, schedule_expression=view.expression,
                                    expression_anchor=event.start.date().isoformat(), schedule_syntax=2)
        events.append(event)
    return ParseReport(tuple(events), tuple(issues), tuple(rejected))


@dataclass(frozen=True, slots=True)
class PreparedSource:
    index: int
    label: str
    report: ParseReport | None = None
    error: str = ''
    wire_bytes: int = 0

    def remap(self, calendar_id: str) -> ParseReport:
        if self.report is None:
            raise ValueError(self.error or 'This calendar could not be read.')
        return remap_calendar(self.report, calendar_id)


def _last_source_versions(items: tuple[PreparedSource, ...]) -> tuple[PreparedSource, ...]:
    """Keep each UID in its last source while preserving every source's diagnostics."""
    if len(items) < 2:
        return items
    later_uids = set()
    effective = None
    for index in reversed(range(len(items))):
        item = items[index]
        if item.report is None:
            continue
        events = tuple(event for event in item.report.events if event.ical_uid not in later_uids)
        later_uids.update(event.ical_uid for event in events)
        if len(events) != len(item.report.events):
            if effective is None:
                effective = list(items)
            effective[index] = replace(item, report=replace(item.report, events=events))
    return tuple(effective) if effective is not None else items


@dataclass(frozen=True, slots=True)
class AcceptedImportSource:
    """One reviewed source, shared by its initial submission and every retry."""
    key: str
    item: PreparedSource
    calendar_id: str
    reminder_mode: str
    reminder_alarms: tuple[Alarm, ...]
    source_path: Path | None

    def prepare(self, in_memory: bool) -> ParseReport:
        report = self.item.remap(self.calendar_id)
        # An in-memory Store belongs to Qt. Finish its transformation here
        # before handing the ready report to the GUI connection.
        return self.transform(report) if in_memory else report

    def transform(self, report: ParseReport) -> ParseReport:
        return transform_import_reminders(report, self.reminder_mode, self.reminder_alarms)


@dataclass(frozen=True, slots=True)
class ImportPage:
    month: date
    start: date
    end: date
    events: tuple[Event, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedImport:
    items: tuple[PreparedSource, ...]
    reminder_mode: str = 'keep'
    reminder_alarms: tuple[Alarm, ...] = ()
    _effective_items: tuple[PreparedSource, ...] = field(init=False, repr=False, compare=False)
    reminder_counts: tuple[int, int] = field(init=False)
    events: tuple[Event, ...] = field(init=False)
    warnings: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'reminder_alarms', _reviewed_templates(self.reminder_mode, self.reminder_alarms))
        # The last source owns a repeated UID in this review. Retain the
        # original reports for diagnostics and direct destination remapping.
        effective = _last_source_versions(self.items)
        object.__setattr__(self, '_effective_items', effective)
        events = (event for item in effective if item.report is not None for event in item.report.events)
        object.__setattr__(self, 'events', tuple(sorted(events, key=lambda event: (event.start.timestamp(), event.id))))
        object.__setattr__(self, 'reminder_counts', count_reminder_sources(ParseReport(self.events)))
        warnings = []
        count = 0
        for item in self.items:
            if item.error:
                warnings.append(item.label + ': ' + item.error)
            if item.report is not None:
                count += len(item.report.events)
                warnings.extend(item.label + ': ' + issue.reason for issue in item.report.issues)
        duplicate_count = count - self.event_count
        if duplicate_count:
            warnings.append(f'{duplicate_count} duplicate event(s) use the version from the last source.')
        object.__setattr__(self, 'warnings', tuple(dict.fromkeys(warnings)))

    @property
    def event_count(self) -> int:
        return len(self.events)

    def with_reminders(self, mode: str, alarms: tuple[Alarm, ...]) -> PreparedImport:
        alarms = _reviewed_templates(mode, alarms)
        if (mode, alarms) == (self.reminder_mode, self.reminder_alarms):
            return self
        # Only the policy changes on Save. Share the worker's immutable preview
        # projections instead of sorting and counting the selected families on Qt.
        reviewed = copy(self)
        object.__setattr__(reviewed, 'reminder_mode', mode)
        object.__setattr__(reviewed, 'reminder_alarms', alarms)
        return reviewed

    def with_selection(self, selection) -> PreparedImport:
        """Filter reviewed reports on a worker without reading their sources again."""
        if selection.all_selected and not selection.choices:
            if self.items is self._effective_items:
                return self
            # The worker already computed this choice and its projections.
            reviewed = copy(self)
            object.__setattr__(reviewed, 'items', self._effective_items)
            return reviewed
        items = tuple(replace(item, report=selection.filter_report(item.report))
                      if item.report is not None else item for item in self._effective_items)
        return replace(self, items=items)

    def initial_month(self, preferences: Preferences, today: date | None = None) -> date:
        """Prefer an ongoing/upcoming occurrence over an old recurring anchor."""
        today = today or now().date()
        boundary = datetime.combine(today, time.min, local_zone())
        future, past = [], []

        def consider(event):
            day = _display_date(event)
            last = _display_date(event, event.end)
            ongoing = last > today if event.all_day else event.end.timestamp() > boundary.timestamp()
            if day < today and ongoing:
                future.append(today)
            elif day >= today:
                future.append(day)
            else:
                past.append(day)

        for master in self.events:
            if not master.recurring:
                consider(master)
                continue
            try:
                # Explicit/moved dates are finite and can lie outside the rule.
                for key, payload in master.recurrence_overrides.items():
                    if key not in master.excluded_dates:
                        consider(Event.from_dict(payload))
                for value in (master.start, *(key_datetime(key, master) for key in master.recurrence_dates)):
                    key = recurrence_key(value, master.all_day, floating_time=master.floating_time)
                    if key not in master.excluded_dates and key not in master.recurrence_overrides:
                        consider(occurrence(master, value, key))
                nominal_boundary = (datetime.combine(today, time.min, event_zone(master))
                                    if master.all_day else boundary)
                rule = recurrence_set(master, after=nominal_boundary)
                for index, value in enumerate(rule.xafter(nominal_boundary, inc=True)):
                    key = recurrence_key(value, master.all_day, floating_time=master.floating_time)
                    if key not in master.recurrence_overrides:
                        consider(occurrence(master, value, key))
                        break
                    if index >= 32:
                        break
            except (ValueError, TypeError, OverflowError, KeyError):
                # A preview issue must never prevent import of its unchanged
                # snapshot; month_page reports range/recurrence failures.
                past.append(_display_date(master))
        return (min(future) if future else max(past) if past else today).replace(day=1)

    def month_page(self, month: date, preferences: Preferences, *, limit=10000) -> ImportPage:
        """Expand only the displayed six-week grid, with a bounded result size."""
        month = month.replace(day=1)
        start = preferences.week_start(month)
        end = start + timedelta(days=42)
        begin = datetime.combine(start, time.min, local_zone())
        finish = datetime.combine(end, time.min, local_zone())
        limit = max(1, int(limit))
        events, warnings = [], []
        for master in self.events:
            remaining = limit - len(events)
            if not remaining:
                warnings.append(f'Preview limited to {limit:,} occurrences; the complete events and series will be imported.')
                break
            try:
                source_begin, source_finish = ((datetime.combine(start, time.min, event_zone(master)),
                                               datetime.combine(end, time.min, event_zone(master)))
                                              if master.all_day else (begin, finish))
                events.extend(expand_series(master, source_begin, source_finish, limit=remaining))
            except RecurrenceRangeError:
                events.extend(expand_series(master, source_begin, source_finish, limit=remaining, truncate=True))
                warnings.append(f'Preview limited to {limit:,} occurrences; the complete events and series will be imported.')
            except (ValueError, TypeError, OverflowError, KeyError) as exc:
                warnings.append(master.title + ': ' + str(exc))
        events.sort(key=lambda event: (event.start.timestamp(), event.end.timestamp(), event.id))
        return ImportPage(month, start, end, tuple(events), tuple(dict.fromkeys(warnings)))


def prepare_sources(sources) -> PreparedImport:
    """Read/parse each selected source once, preserving failures beside peers."""
    items, byte_count, event_count = [], 0, 0
    for index, source in enumerate(sources):
        if not isinstance(source, ImportSource):
            raise ValueError('Choose calendar import sources.')
        try:
            remaining = MAX_PENDING_IMPORT_BYTES - byte_count
            if remaining <= 0:
                raise ValueError('Import exceeds 60 MB in total. Choose fewer files.')
            if source.path is not None:
                with source.path.open('rb') as stream:
                    data = stream.read(min(MAX_IMPORT_BYTES, remaining) + 1)
            else:
                data = source.data
            byte_count += len(data)
            if len(data) > MAX_IMPORT_BYTES:
                raise ValueError('Choose a calendar file smaller than 20 MB.')
            if byte_count > MAX_PENDING_IMPORT_BYTES:
                raise ValueError('Import exceeds 60 MB in total. Choose fewer files.')
            report = parse_ics_report(data, PREVIEW_CALENDAR_ID, repair_missing_uids=True)
            if event_count + len(report.events) > MAX_IMPORT_EVENTS:
                raise ValueError('Import exceeds 50,000 events. Choose fewer files.')
            event_count += len(report.events)
            items.append(PreparedSource(index, source.label, report, wire_bytes=len(data)))
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            items.append(PreparedSource(index, source.label, error=str(exc)))
    return PreparedImport(tuple(items))
