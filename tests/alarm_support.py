"""Synthetic events shared by reminder tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from debacal.alarms import Alarm
from debacal.models import Event
from debacal.reminder_transport import NotificationTransport


BASE = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)


def alarm_event(*, alarms: tuple[Alarm, ...] | None = (), **changes) -> Event:
    return replace(Event('e', 'c', 'Example', BASE, BASE + timedelta(hours=1),
                         ical_uid='e', alarms=alarms), **changes)


class FakeClock:
    def __init__(self, now=BASE):
        self.time = now

    def now(self):
        return self.time


def wait_for(predicate, *, timeout=5):
    from time import monotonic, sleep
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        app.processEvents()
        if value := predicate():
            return value
        sleep(.005)
    raise AssertionError('Timed out waiting for reminder worker.')



class FakeTransport(NotificationTransport):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.started = False
        self.ready = True

    def start(self):
        self.started = True

    @property
    def available(self):
        return self.ready

    def send(self, due, *, calendar_name, profile_label, silent):
        self.sent.append((due, calendar_name, profile_label, silent))

    def stop(self):
        self.started = False
