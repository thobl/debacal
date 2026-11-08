"""Workspace gestures use the same duration laws as the event editor."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.calendar_widgets import CalendarCanvas, MonthCanvas, event_tooltip
from debacal.event_dialog import EventDialog
from debacal.event_duration import elapsed_duration
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
CALENDAR = Calendar('local', 'Personal', '#587c67')


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, value, __: errors.append(value)):
        yield app, owned
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_command_creation_default_retains_elapsed_minutes(desktop):
    _, owned = desktop
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE)
    captured = []
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        owned.append(window)
        def inspect(dialog):
            captured.append((dialog.value(dialog.start), dialog.value(dialog.end)))
            return QDialog.DialogCode.Rejected
        with patch.object(EventDialog, 'exec', inspect):
            window.create_event(start=start)
        assert elapsed_duration(*captured[0]) == timedelta(hours=1)
        assert captured[0][1].hour == 2 and captured[0][1].fold == 1
        window.close()
        window.executor.shutdown(wait=True)


def test_week_double_click_default_is_elapsed_across_repeated_hour(desktop):
    app, owned = desktop
    canvas = CalendarCanvas()
    owned.append(canvas)
    canvas.set_data([date(2026, 10, 25)], [], [CALENDAR])
    canvas.resize(600, 720)
    canvas.fit_to_viewport(720, full_day=True)
    canvas.show()
    app.processEvents()
    created = QSignalSpy(canvas.create_requested)
    point = QPoint(round(canvas.gutter + (canvas.width() - canvas.gutter) / 2),
                   round(canvas.time_top + 2.5 * canvas.hour_height))
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    assert created.count() == 1
    start, end = created.at(0)
    assert elapsed_duration(start, end) == timedelta(hours=1)
    assert end.hour == 2 and end.fold == 1


def test_month_double_click_twenty_four_hours_is_not_a_nominal_day(desktop):
    app, owned = desktop
    preferences = Preferences(default_event_duration_minutes=1440)
    canvas = MonthCanvas()
    owned.append(canvas)
    canvas.set_preferences(preferences)
    canvas.set_data(preferences.week_start(date(2026, 10, 1)), 10, [], [CALENDAR])
    canvas.resize(900, 700)
    canvas.show()
    app.processEvents()
    created = QSignalSpy(canvas.create_requested)
    row, column = divmod((date(2026, 10, 24) - canvas.start).days, 7)
    point = QPoint(round(canvas.week_gutter + (column + .5) * (canvas.width() - canvas.week_gutter) / 7),
                   round(canvas.header_height + (row + .5) * (canvas.height() - canvas.header_height) / 6))
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    assert created.count() == 1
    start, end = created.at(0)
    assert elapsed_duration(start, end) == timedelta(hours=24)
    assert end.day == 25 and end.hour == 8


def test_drag_preview_and_commit_retain_elapsed_duration_across_fallback(desktop):
    app, owned = desktop
    start = datetime(2026, 10, 24, 2, 30, tzinfo=ZONE)
    source = Event('source', 'local', 'An appointment', start, start + timedelta(minutes=45))
    canvas = CalendarCanvas()
    owned.append(canvas)
    canvas.set_data([date(2026, 10, 24), date(2026, 10, 25)], [source], [CALENDAR])
    canvas.resize(700, 720)
    canvas.fit_to_viewport(720, full_day=True)
    canvas.show()
    app.processEvents()
    origin = canvas.rects()[0][0].center().toPoint()
    target = origin + QPoint(round((canvas.width() - canvas.gutter) / 2), 0)
    preview = QSignalSpy(canvas.drag_preview_requested)
    committed = QSignalSpy(canvas.move_requested)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=target)
    assert preview.count() and committed.count() == 1
    for values in (preview.at(preview.count() - 1), committed.at(0)):
        moved_start, moved_end = values[1:3]
        assert elapsed_duration(moved_start, moved_end) == timedelta(minutes=45)
        assert moved_end.fold == 1


def test_event_tooltip_distinguishes_repeated_clock_phases(desktop):
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE)
    end = datetime(2026, 10, 25, 2, 15, tzinfo=ZONE, fold=1)
    source = Event('repeat-hour', 'local', 'Forty-five minutes', start, end)
    tooltip = event_tooltip(source, Preferences(time_format='24h'))
    assert '02:30 CEST' in tooltip and '02:15 CET' in tooltip
