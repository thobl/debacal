"""Synthetic events shared by reminder tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from debacal.alarms import Alarm
from debacal.models import Event


BASE = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)


def alarm_event(*, alarms: tuple[Alarm, ...] | None = (), **changes) -> Event:
    return replace(Event('e', 'c', 'Example', BASE, BASE + timedelta(hours=1),
                         ical_uid='e', alarms=alarms), **changes)
