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
    assert '45min elapsed' in tooltip


@pytest.mark.parametrize('fold', [0, 1])
@pytest.mark.parametrize('operation', ['move', 'resize'])
def test_real_gesture_preserves_repeated_hour_phase_in_preview_and_commit(desktop, fold, operation):
    app, owned = desktop
    start = datetime(2026, 10, 25, 2, 0, tzinfo=ZONE, fold=fold)
    source = Event('source', 'local', 'Repeated hour', start,
                   datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=fold))
    canvas = CalendarCanvas()
    owned.append(canvas)
    canvas.set_data([start.date()], [source], [CALENDAR])
    canvas.resize(600, 2000)
    canvas.fit_to_viewport(2000, full_day=True)
    canvas.show()
    app.processEvents()
    rect = canvas.rects()[0][0]
    origin = QPoint(round(rect.center().x()), round(rect.bottom() - 2 if operation == 'resize' else rect.top() + 3))
    target = origin + QPoint(0, round(canvas.hour_height / 4))
    preview = QSignalSpy(canvas.drag_preview_requested)
    committed = QSignalSpy(canvas.move_requested)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=target)
    assert preview.count() and committed.count() == 1
    for values in (preview.at(preview.count() - 1), committed.at(0)):
        moved_start, moved_end = values[1:3]
        expected_start_shift = timedelta(minutes=15 if operation == 'move' else 0)
        assert elapsed_duration(source.start, moved_start) == expected_start_shift
        assert elapsed_duration(source.end, moved_end) == timedelta(minutes=15)
        assert moved_start.fold == fold and moved_end.fold == fold


def test_dragging_into_nonexistent_hour_rejects_preview_and_commit_once(desktop):
    app, owned = desktop
    start = datetime(2027, 3, 28, 1, 15, tzinfo=ZONE)
    source = Event('source', 'local', 'Before the clock change', start, start + timedelta(minutes=30))
    canvas = CalendarCanvas()
    owned.append(canvas)
    canvas.set_data([start.date()], [source], [CALENDAR])
    canvas.resize(600, 2000)
    canvas.fit_to_viewport(2000, full_day=True)
    canvas.show()
    app.processEvents()
    rect = canvas.rects()[0][0]
    origin = QPoint(round(rect.center().x()), round(rect.top() + 3))
    target = origin + QPoint(0, round(canvas.hour_height))
    preview, committed, rejected = (QSignalSpy(signal) for signal in (
        canvas.drag_preview_requested, canvas.move_requested, canvas.gesture_rejected))
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=target)
    assert preview.count() == committed.count() == 0
    assert rejected.count() == 1 and 'clocks change' in rejected.at(0)[0]


@pytest.mark.parametrize('crossing', [False, True])
def test_repeated_hour_events_are_both_visible_labeled_and_mouse_selectable(desktop, crossing):
    app, owned = desktop
    start = datetime(2026, 10, 25, 2, 45 if crossing else 15, tzinfo=ZONE)
    end = datetime(2026, 10, 25, 2, 15, tzinfo=ZONE, fold=1) if crossing else start + timedelta(minutes=30)
    first = Event('first', 'local', 'First phase', start, end)
    second_start = start.replace(fold=1)
    second = Event('second', 'local', 'Second phase', second_start,
                   datetime.fromtimestamp(second_start.timestamp() + 1800, ZONE))
    canvas = CalendarCanvas()
    owned.append(canvas)
    canvas.set_data([start.date()], [first, second], [CALENDAR])
    canvas.resize(700, 2000)
    canvas.fit_to_viewport(2000, full_day=True)
    canvas.show()
    app.processEvents()
    rects = {item.id: rect for rect, item in canvas.rects()}
    assert rects['first'].height() > 0 and rects['second'].height() > 0
    assert not rects['first'].intersects(rects['second'])
    selected = QSignalSpy(canvas.selected)
    for identity in ('first', 'second'):
        point = rects[identity].center().toPoint()
        assert canvas.hit(point)[1].id == identity
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
        assert selected.at(selected.count() - 1)[0] == identity
    with patch('debacal.calendar_widgets.text') as draw_text:
        canvas.grab()
    labels = [call.args[2] for call in draw_text.call_args_list]
    assert any('CEST' in label and 'First phase' in label for label in labels)
    assert any('CET' in label and 'Second phase' in label for label in labels)
    assert 'CEST' in event_tooltip(first, canvas.preferences)
    assert 'CET' in event_tooltip(second, canvas.preferences)
