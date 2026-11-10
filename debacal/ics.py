"""iCalendar import/export with preserved standard recurrence resources.

Recurring files retain their master rules and detached exceptions. Occurrences
are generated for display only, never as separate stored/synchronized events.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Iterable

try:
    from icalendar import Calendar as ICalendar
except ImportError:
    ICalendar = None

from .models import Event
from .timeutil import local_zone


def local_timezone():
    """Use the shared OS zone cache, retaining IANA daylight-saving rules."""
    return local_zone()


def _datetime(value, tz):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def occurrence_key(value, *, tz=None) -> str:
    """Backend-independent identity for a recurrence's original start.

    DATE instances use the calendar date. Timed instances use UTC, so a backend
    converting TZID times to UTC cannot change the occurrence's local identity.
    """
    if isinstance(value, datetime):
        return _datetime(value, tz or local_timezone()).astimezone(timezone.utc).isoformat()
    return value.isoformat()


def recurrence_window():
    """Default expansion: one year back through two years ahead."""
    today = datetime.now(local_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=366), today + timedelta(days=732)


def has_recurrence(data: str | bytes) -> bool:
    from .ics_resources import _read
    return any(any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID"))
               for component in _read(data))


def parse_ics(data: str | bytes, calendar_id: str, window_start=None, window_end=None,
              *, limit: int = 10000, read_only_recurring: bool = False) -> list[Event]:
    """Import complete UID resources, retaining recurrence and detached exceptions.

    Recurrences expand only when the UI requests a date range. Window arguments
    are accepted for compatibility and never truncate persisted event series.
    """
    report = parse_ics_report(data, calendar_id, limit=limit)
    if report.issues:
        raise ValueError(f"{len(report.issues)} event resource(s) require review: {report.issues[0].reason}")
    return list(report.events)


def parse_ics_report(data: str | bytes, calendar_id: str, *, limit: int = 10000, repair_missing_uids=False):
    """Return healthy resources and private per-resource quarantine details.

    Reviewed file imports may opt into stable missing-UID repair. Remote sync
    keeps the strict default so an invalid resource cannot acquire a new identity.
    """
    from .ics_resources import parse_report
    return parse_report(data, calendar_id, limit=limit, repair_missing_uids=repair_missing_uids)


def export_ics(events: Iterable[Event]) -> bytes:
    """Export real recurrence rules and exceptions, preserving unknown metadata."""
    from .ics_resources import export
    return export(events)


def patch_ics(data: str | bytes, event: Event) -> bytes:
    """Patch one complete UID resource, preserving server UID and unknown fields."""
    from .ics_resources import patch
    return patch(data, event)


def normalize_caldav_ics(data: str | bytes) -> bytes:
    """Prepare an ICS resource for calendar storage, rather than scheduling."""
    from .ics_resources import normalize_caldav_resource
    return normalize_caldav_resource(data)
