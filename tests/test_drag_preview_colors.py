"""Drag previews show source calendars for moves and the target for copies."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 10, tzinfo=ZONE)
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier


def settle(app):
    for _ in range(4):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, \
            patch('debacal.ui.now', return_value=START), \
            patch('debacal.ui.local_zone', return_value=ZONE), \
            patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
            patch('debacal.dialogs.local_zone', return_value=ZONE), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        for identity, color in [('red', '#a62a45'), ('blue', '#335eb8'), ('target', '#ddad16')]:
            store.save_calendar(Calendar(identity, identity.title(), color))
        Preferences(start_weekday=0).save(store)
        window = MainWindow(store, enable_sync=False)
        window.resize(1500, 1000)
        window.go_date(START.date())
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        settle(app)
        QTest.mouseClick(window.calendar_rows['target'].choice, Qt.MouseButton.LeftButton)
        assert store.get_default_calendar().id == 'target'
        yield app, window, store
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert errors == []


def insert_selection(desktop, surface, multiple=False):
    app, window, store = desktop
    all_day = surface == 'all_day'
    start = START.replace(hour=0) if all_day else START
    duration = timedelta(days=1) if all_day else timedelta(hours=1)
    first = Event('red-event', 'red', 'Red event', start, start + duration, all_day=all_day)
    values = [first]
    if multiple:
        second_start = start + (timedelta(days=2) if all_day else timedelta(hours=3))
        values.append(Event('blue-event', 'blue', 'Blue event', second_start,
                            second_start + duration, all_day=all_day))
    store.apply('Synthetic events', values)
    window.refresh()
    window.set_view('month' if surface == 'month' else 'week')
    window.select_event(first.id)
    for event in values[1:]:
        window.select_event(event.id, additive=True)
    settle(app)
    canvas = window.month if surface == 'month' else window.all_day_canvas if all_day else window.canvas
    canvas.setFocus()
    rect = next(rect for rect, event in canvas.month_layout()[1] if event.id == first.id) if surface == 'month' else next(
        rect for rect, event in canvas.rects() if event.id == first.id)
    begin = rect.center().toPoint()
    width = (canvas.width() - canvas.week_gutter) / 7 if surface == 'month' else (canvas.width() - canvas.gutter) / len(canvas.days)
    finish = begin + QPoint(round(width), 0)
    return canvas, begin, finish, values


def start_drag(canvas, begin, finish, modifiers=NONE):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=begin)
    movement = QMouseEvent(QEvent.Type.MouseMove, QPointF(finish),
                          QPointF(canvas.mapToGlobal(finish)), Qt.MouseButton.NoButton,
                          Qt.MouseButton.LeftButton, modifiers)
    QApplication.sendEvent(canvas, movement)


def preview_colors(canvas):
    return {event.title: canvas.calendars[event.calendar_id].color for event in canvas.preview_events}


@pytest.mark.parametrize('surface', ['week', 'month', 'all_day'])
@pytest.mark.parametrize('copy', [False, True])
def test_drag_preview_matches_calendar_of_final_move_or_copy(desktop, surface, copy):
    app, window, store = desktop
    canvas, begin, finish, values = insert_selection(desktop, surface)
    original = values[0]
    history = len(store.history())
    start_drag(canvas, begin, finish, CTRL if copy else NONE)
    assert preview_colors(canvas) == {'Red event': '#ddad16' if copy else '#a62a45'}
    assert store.get_event(original.id) == original and len(store.history()) == history
    assert not canvas.grab().isNull()
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, CTRL if copy else NONE, finish)
    settle(app)
    assert not canvas.preview_events
    assert len(store.history()) == history + 1
    if copy:
        assert store.get_event(original.id) == original
        copied = next(event for event in store.list_events() if event.id != original.id)
        assert copied.calendar_id == 'target'
        assert copied.start == original.start + timedelta(days=1)
    else:
        moved = store.get_event(original.id)
        assert moved.calendar_id == original.calendar_id
        assert moved.start == original.start + timedelta(days=1)
        assert len(store.list_events()) == 1


@pytest.mark.parametrize('surface', ['week', 'month', 'all_day'])
def test_ctrl_changes_multi_calendar_preview_without_mouse_motion(desktop, surface):
    app, window, store = desktop
    canvas, begin, finish, originals = insert_selection(desktop, surface, multiple=True)
    history = len(store.history())
    start_drag(canvas, begin, finish)
    expected_sources = {'Red event': '#a62a45', 'Blue event': '#335eb8'}
    assert preview_colors(canvas) == expected_sources
    target_times = {event.title: event.start for event in canvas.preview_events}
    for _ in range(2):
        QTest.keyPress(canvas, Qt.Key.Key_Control)
        assert preview_colors(canvas) == {'Red event': '#ddad16', 'Blue event': '#ddad16'}
        assert canvas.gesture_modifiers & CTRL
        QTest.keyRelease(canvas, Qt.Key.Key_Control)
        assert preview_colors(canvas) == expected_sources
        assert not canvas.gesture_modifiers & CTRL
        assert {event.title: event.start for event in canvas.preview_events} == target_times
    assert {event.id: event for event in store.list_events()} == {event.id: event for event in originals}
    assert len(store.history()) == history
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=finish)
    settle(app)
    assert {event.title: event.calendar_id for event in store.list_events()} == {
        'Red event': 'red', 'Blue event': 'blue'}
    assert {event.title: event.start for event in store.list_events()} == target_times
    assert len(store.history()) == history + 1


def test_repressing_ctrl_at_stationary_pointer_commits_copy_to_selected_calendar(desktop):
    app, window, store = desktop
    canvas, begin, finish, originals = insert_selection(desktop, 'week', multiple=True)
    start_drag(canvas, begin, finish)
    QTest.keyPress(canvas, Qt.Key.Key_Control)
    QTest.keyRelease(canvas, Qt.Key.Key_Control)
    QTest.keyPress(canvas, Qt.Key.Key_Control)
    assert {event.calendar_id for event in canvas.preview_events} == {'target'}
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, CTRL, finish)
    QTest.keyRelease(canvas, Qt.Key.Key_Control)
    settle(app)
    assert all(store.get_event(event.id) == event for event in originals)
    original_ids = {event.id for event in originals}
    copies = [event for event in store.list_events() if event.id not in original_ids]
    assert len(copies) == 2 and {event.calendar_id for event in copies} == {'target'}
    assert {event.title: event.start for event in copies} == {
        event.title: event.start + timedelta(days=1) for event in originals}
