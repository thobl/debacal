"""Read an import once, then preview and import the same calendar snapshot.

All functions are independent of widgets and storage. Callers run file reading,
parsing, recurrence expansion, and destination remapping on a worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5

from .ics import parse_ics_report
from .ics_resources import ParseReport
from .import_sources import ImportSource, MAX_IMPORT_BYTES, MAX_PENDING_IMPORT_BYTES
from .models import Event
from .preferences import Preferences
from .recurrence import (
    RecurrenceRangeError, event_zone, expand_series, key_datetime, occurrence,
    occurrence_id, recurrence_key, recurrence_set,
)
from .timeutil import local_zone, now


PREVIEW_CALENDAR_ID = '__debacal_import_preview__'
MAX_IMPORT_EVENTS = 50_000


def _display_date(event: Event, value: datetime | None = None) -> date:
    value = event.start if value is None else value
    return value.date() if event.all_day else value.astimezone(local_zone()).date()


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
    return ParseReport(tuple(events), report.issues, report.rejected)


@dataclass(frozen=True, slots=True)
class PreparedSource:
    index: int
    label: str
    report: ParseReport | None = None
    error: str = ''

    def remap(self, calendar_id: str) -> ParseReport:
        if self.report is None:
            raise ValueError(self.error or 'This calendar could not be read.')
        return remap_calendar(self.report, calendar_id)


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
    events: tuple[Event, ...] = field(init=False)
    warnings: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        # Matching UIDs imported into one calendar replace each other. Preview
        # the final version, retaining individual source reports for undo.
        by_id = {event.id: event for item in self.items if item.report is not None
                 for event in item.report.events}
        object.__setattr__(self, 'events', tuple(sorted(by_id.values(), key=lambda event: (event.start.timestamp(), event.id))))
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
                    key = recurrence_key(value, master.all_day)
                    if key not in master.excluded_dates and key not in master.recurrence_overrides:
                        consider(occurrence(master, value, key))
                nominal_boundary = (datetime.combine(today, time.min, event_zone(master))
                                    if master.all_day else boundary)
                rule = recurrence_set(master, after=nominal_boundary)
                for index, value in enumerate(rule.xafter(nominal_boundary, inc=True)):
                    key = recurrence_key(value, master.all_day)
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
            report = parse_ics_report(data, PREVIEW_CALENDAR_ID)
            if event_count + len(report.events) > MAX_IMPORT_EVENTS:
                raise ValueError('Import exceeds 50,000 events. Choose fewer files.')
            event_count += len(report.events)
            items.append(PreparedSource(index, source.label, report))
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            items.append(PreparedSource(index, source.label, error=str(exc)))
    return PreparedImport(tuple(items))
