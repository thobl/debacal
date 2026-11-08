"""Event-day navigation and configured durations reach the actual workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import occurrence
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def event(identity, days=0, *, minutes=0, **values):
    start = START + timedelta(days=days, minutes=minutes)
    return Event(identity, 'local', identity, start, start + timedelta(hours=1), **values)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    window = None
    errors = []
    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.ui.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, value, __: errors.append(value)):
        window = MainWindow(store, enable_sync=False)
        window.preferences = replace(window.preferences, start_weekday=0)
        window.anchor = window.selected_day = START.date()
        window.show()
        window.activateWindow()
        window.refresh()
        app.processEvents()
        yield app, store, window
        window.close()
        window.executor.shutdown(wait=True)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def test_left_and_right_skip_empty_days_and_keep_the_closest_clock(workspace):
    _, store, window = workspace
    first, early, close = event('Wednesday'), event('Friday early', 2, minutes=-60), event('Friday close', 2, minutes=10)
    store.apply('Create', [first, early, close])
    window.refresh()
    window.select_event(first.id)
    window.select_by_arrow('right')
    assert window.selected_id == close.id
    window.select_by_arrow('left')
    assert window.selected_id == first.id
    window.select_by_arrow('left')
    assert window.selected_id == first.id


def test_day_navigation_follows_distant_events_across_view_boundaries(workspace):
    _, store, window = workspace
    first, distant = event('First'), event('Distant', 400)
    store.apply('Create', [first, distant])
    window.set_view('day')
    window.select_event(first.id)
    window.select_by_arrow('right')
    assert window.selected_id == distant.id and window.anchor == distant.start.date()
    window.select_by_arrow('left')
    assert window.selected_id == first.id and window.anchor == first.start.date()


def test_navigation_seeks_recurrence_and_honours_excluded_and_moved_dates(workspace):
    _, store, window = workspace
    source = event('Weekly', recurrence_rule='FREQ=WEEKLY;INTERVAL=1', timezone_id=ZONE.key)
    second = occurrence(source, START + timedelta(weeks=1))
    moved = occurrence(source, START + timedelta(weeks=2))
    moved = replace(moved, start=START + timedelta(days=3), end=START + timedelta(days=3, hours=1))
    source = replace(source, excluded_dates=(second.recurrence_id,),
                     recurrence_overrides={moved.recurrence_id: moved.to_dict()})
    store.apply('Create', [source])
    window.set_view('day')
    window.select_event(occurrence(source, START).id)
    window.select_by_arrow('right')
    assert window.selected_id == moved.id and window.anchor == moved.start.date()
    window.select_by_arrow('right')
    assert window.selection.primary.start == START + timedelta(weeks=3)


def test_navigation_excludes_hidden_calendars_and_filters_endless_series_by_search(workspace):
    _, store, window = workspace
    hidden = Calendar('hidden', 'Hidden', '#8b6961', visible=False)
    store.save_calendar(hidden)
    first = replace(event('First'), title='Needle')
    source = event('Noise', recurrence_rule='FREQ=DAILY;INTERVAL=1', timezone_id=ZONE.key)
    found = occurrence(source, START + timedelta(days=40))
    found = replace(found, title='Needle in an exception')
    source = replace(source, recurrence_overrides={found.recurrence_id: found.to_dict()})
    store.apply('Create', [first, source, replace(event('Hidden', 1), calendar_id=hidden.id)])
    window.set_view('day')
    window.search.setText('Needle')
    window.filter_search.setChecked(True)
    window.flush_search()
    window.apply_search_filter()
    window.select_event(first.id)
    window.select_by_arrow('right')
    assert window.selected_id == found.id and window.anchor == found.start.date()


def test_primary_selection_is_distinct_from_series_scope_in_calendar_and_agenda(workspace):
    _, store, window = workspace
    source = event('Daily', recurrence_rule='FREQ=DAILY;COUNT=5')
    store.apply('Create', [source])
    window.refresh()
    selected = occurrence(source, START + timedelta(days=1))
    window.set_selection_mode('group')
    window.select_event(selected.id)
    assert len(window.selected_ids) > 1
    for surface in (window.canvas, window.month, window.all_day_canvas):
        assert surface.primary_selected_ids == {selected.id}
        assert surface.selected_ids == window.selected_ids
    assert window.results.primary_selected_ids == {selected.id}
    window.set_selection_mode('future')
    assert window.canvas.primary_selected_ids == {selected.id}
    assert occurrence(source, START).id not in window.selected_ids


@pytest.mark.parametrize('explicit_minutes', [None, 25])
def test_event_editor_default_duration_preserves_explicit_and_existing_ranges(workspace, explicit_minutes):
    _, store, _ = workspace
    preferences = Preferences(default_event_duration_minutes=90)
    end = START + timedelta(minutes=explicit_minutes) if explicit_minutes is not None else None
    dialog = EventDialog(store.list_calendars(), START, end, preferences=preferences)
    try:
        assert dialog.value(dialog.end) - dialog.value(dialog.start) == timedelta(minutes=explicit_minutes or 90)
    finally:
        dialog.deleteLater()
    source = event('Existing')
    dialog = EventDialog(store.list_calendars(), START, event=source, preferences=preferences)
    try:
        assert dialog.value(dialog.end) - dialog.value(dialog.start) == timedelta(hours=1)
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize('origin', ['command', 'week', 'month', 'drag', 'all_day'])
def test_creation_paths_use_the_configured_duration_except_explicit_ranges(workspace, origin):
    app, _, window = workspace
    window.apply_preferences(replace(window.preferences, default_event_duration_minutes=90), window.bindings)
    captured = []

    def inspect(dialog):
        captured.append((dialog.value(dialog.end) - dialog.value(dialog.start), dialog.all_day.isChecked()))
        return QDialog.DialogCode.Rejected

    with patch.object(EventDialog, 'exec', inspect):
        if origin == 'command':
            window.create_event()
        elif origin == 'drag':
            window.canvas.create_requested.emit(START, START + timedelta(minutes=35))
        elif origin == 'all_day':
            window.create_all_day_event(START.replace(hour=0))
        else:
            window.set_view(origin)
            app.processEvents()
            if origin == 'week':
                canvas = window.canvas
                column = canvas.days.index(START.date())
                point = QPoint(round(canvas.gutter + (column + .5) * (canvas.width() - canvas.gutter) / len(canvas.days)),
                               round(canvas.time_top + (START.hour - canvas.start_hour) * canvas.hour_height))
                QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
            else:
                canvas = window.month
                offset = (START.date() - canvas.start).days
                point = QPoint(round(canvas.week_gutter + (offset % 7 + .5) * (canvas.width() - canvas.week_gutter) / 7),
                               round(canvas.header_height + (offset // 7 + .5) * (canvas.height() - canvas.header_height) / 6))
                QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    expected = timedelta(minutes=35 if origin == 'drag' else 1440 if origin == 'all_day' else 90)
    assert captured == [(expected, origin == 'all_day')]
