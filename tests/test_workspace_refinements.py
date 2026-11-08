"""Calendar controls, standard clipboard keys, and optional search filtering."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QEvent, QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Event, Calendar
from debacal.store import Store
from debacal.ui import MainWindow, EVENT_MIME
from debacal.appearance import stylesheet
from debacal.shortcuts import validate_bindings


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))
CTRL = Qt.KeyboardModifier.ControlModifier


def settle(app):
    for _ in range(5):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        window.activateWindow()
        settle(app)
        try:
            yield window, store, app
        finally:
            QApplication.clipboard().clear()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert errors == []


@pytest.mark.parametrize('view', ['day', 'week', 'month'])
def test_filter_is_optional_matches_effective_fields_and_restores_all_events(desktop, view):
    window, store, app = desktop
    events = [Event('title', 'local', 'Review work', START, START + timedelta(hours=1)),
              Event('location', 'local', 'Meet team', START + timedelta(hours=1), START + timedelta(hours=2), location='Review room'),
              Event('other', 'local', 'Unrelated', START + timedelta(hours=2), START + timedelta(hours=3))]
    store.apply('Create test events', events)
    window.refresh()
    window.set_view(view)
    window.search.setText('rEVieW')
    assert len(window.visible_events) == 3
    QTest.mouseClick(window.filter_search, Qt.MouseButton.LeftButton)
    settle(app)
    assert {event.id for event in window.visible_events} == {'title', 'location'}
    assert {event.id for event in window.canvas.events} == {'title', 'location'}
    assert {event.id for event in window.month.events} == {'title', 'location'}
    assert store.get_setting('filter_calendar_by_search') is True
    window.search.setText('Unrelated')
    assert [event.id for event in window.visible_events] == ['other']
    window.exit_search()
    assert len(window.visible_events) == 3
    assert window.filter_search.isChecked()
    assert len(store.list_events()) == 3 and len(store.history()) == 1


def test_filtered_series_can_show_override_only_or_entire_matching_master(desktop):
    window, store, _ = desktop
    master = Event('series', 'local', 'Weekly meeting', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create', [master])
    instances = store.query_occurrences(START, START + timedelta(days=4))
    store.edit_occurrence(instances[1], replace(instances[1], title='Special meeting'))
    window.refresh()
    window.filter_search.click()
    window.search.setText('Special')
    assert [event.title for event in window.visible_events] == ['Special meeting']
    window.search.setText('Weekly')
    assert len(window.visible_events) == 3
    window.filter_search.click()
    window.search.setText('No match')
    assert len(window.visible_events) == 3


def test_filter_clears_hidden_selection_so_delete_cannot_remove_an_unseen_event(desktop):
    window, store, _ = desktop
    hidden = Event('hidden', 'local', 'Other event', START, START + timedelta(hours=1))
    shown = replace(hidden, id='shown', ical_uid='shown', title='Match')
    store.apply('Create', [hidden, shown])
    window.refresh()
    window.select_event(hidden.id)
    window.search.setText('Match')
    window.filter_search.click()
    assert window.selected_id is None and not window.selection.refs
    window.canvas.setFocus()
    QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
    assert len(store.list_events()) == 2


def test_new_default_keys_yield_to_existing_custom_bindings(desktop):
    _, store, app = desktop
    store.set_setting('keybindings_version', 0)
    store.set_setting('keybindings', {'new_event': 'F', 'copy_event': 'C', 'paste_event': 'V',
                                     'day_view': '1', 'duplicate_event': 'D'})
    other = MainWindow(store, enable_sync=False)
    try:
        assert other.bindings['new_event'] == 'F'
        assert other.bindings['full_day'] == ''
        assert other.bindings['day_view'] == 'D'
        assert other.bindings['copy_event'] == 'Ctrl+C'
        validate_bindings(other.bindings)
    finally:
        other.close()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_clipboard_uses_only_control_keys_and_d_changes_to_day(desktop):
    window, store, app = desktop
    source = Event('source', 'local', 'Review', START, START + timedelta(hours=1))
    store.apply('Create', [source])
    window.refresh()
    window.select_event(source.id)
    window.canvas.setFocus()
    QApplication.clipboard().setText('Keep this text')
    QTest.keyClick(window.canvas, Qt.Key.Key_C)
    QTest.keyClick(window.canvas, Qt.Key.Key_V)
    assert QApplication.clipboard().text() == 'Keep this text'
    assert window.pending_paste is None
    QTest.keyClick(window.canvas, Qt.Key.Key_C, CTRL)
    assert QApplication.clipboard().mimeData().hasFormat(EVENT_MIME)
    QTest.keyClick(window.canvas, Qt.Key.Key_V, CTRL)
    assert window.pending_paste is not None
    QTest.keyClick(window.canvas, Qt.Key.Key_Escape)
    QTest.keyClick(window.canvas, Qt.Key.Key_D)
    settle(app)
    assert window.view == 'day' and store.list_events() == [source]
    assert 'duplicate_event' not in window.bindings


def test_calendar_name_selects_creation_target_and_eye_alone_controls_visibility(desktop):
    window, store, app = desktop
    store.save_calendar(Calendar('work', 'Work', '#123abc'))
    window.refresh(calendars=True)
    row = window.calendar_rows['work']
    QTest.mouseClick(row.choice, Qt.MouseButton.LeftButton)
    assert store.get_default_calendar().id == 'work'
    assert store.get_calendar('work').visible
    QTest.mouseClick(row.eye, Qt.MouseButton.LeftButton)
    settle(app)
    assert not store.get_calendar('work').visible
    assert store.get_default_calendar().id == 'work'
    assert row.eye.toolTip() == 'Show Work'
    QTest.mouseClick(window.calendar_rows['local'].choice, Qt.MouseButton.LeftButton)
    assert store.get_default_calendar().id == 'local'
    assert not store.get_calendar('work').visible


def test_creation_marker_clears_if_all_calendars_become_read_only(desktop):
    window, store, _ = desktop
    assert window.calendar_rows['local'].choice.isChecked()
    store.save_calendar(replace(store.get_calendar('local'), read_only=True))
    window.refresh()
    assert store.get_default_calendar() is None
    assert not window.calendar_rows['local'].choice.isChecked()
    assert not window.calendar_rows['local'].choice.isEnabled()


@pytest.mark.parametrize('size', [(1030, 690), (1400, 900)])
def test_small_calendar_list_needs_no_scroll_and_date_navigation_stays_near_heading(desktop, size):
    window, store, app = desktop
    for index in range(2):
        store.save_calendar(Calendar(f'extra-{index}', f'Extra {index}', '#123abc'))
    window.resize(*size)
    window.refresh(calendars=True)
    settle(app)
    assert not window.calendar_scroll.verticalScrollBar().isVisible()
    status_bottom = window.status.mapTo(window, window.status.rect().bottomRight()).y()
    assert status_bottom < window.height()
    assert all(row.isVisible() for row in window.calendar_rows.values())
    heading_left = window.heading.mapToGlobal(window.heading.rect().topLeft()).x()
    navigation_right = window.previous_button.mapToGlobal(window.previous_button.rect().topRight()).x()
    assert 0 < heading_left - navigation_right < 25
    assert window.next_button.mapToGlobal(window.next_button.rect().topLeft()).x() > heading_left
    assert '(T)' in window.today_button.toolTip()
    assert '(D)' in window.view_buttons['day'].toolTip()
    assert window.new_button.parentWidget() is not window.sidebar
    assert not hasattr(window, 'details')
    assert not hasattr(window, 'creation_calendar')
    assert not hasattr(window, 'app_menu')


def test_long_calendar_list_scrolls_as_fallback(desktop):
    window, store, app = desktop
    for index in range(25):
        store.save_calendar(Calendar(f'extra-{index}', f'Extra {index}', '#123abc'))
    window.resize(1030, 690)
    window.refresh(calendars=True)
    settle(app)
    assert window.calendar_scroll.verticalScrollBar().isVisible()
    assert window.calendar_scroll.verticalScrollBar().maximum() > 0
    assert window.results.isVisible() and window.results.height() >= window.results.minimumHeight()


@pytest.mark.parametrize('scale', [80, 140])
def test_sidebar_controls_do_not_overlap_with_large_or_small_text(desktop, scale):
    window, store, app = desktop
    for index in range(2):
        store.save_calendar(Calendar(f'extra-{index}', f'Extra {index}', '#123abc'))
    window.preferences = replace(window.preferences, font_scale=scale)
    window.setStyleSheet(stylesheet(scale))
    window.resize(1030, 690)
    window.refresh()
    settle(app)
    assert window.mini.mapTo(window.sidebar, window.mini.rect().bottomLeft()).y() < window.calendar_scroll.mapTo(
        window.sidebar, window.calendar_scroll.rect().topLeft()).y()
    assert window.calendar_scroll.mapTo(window.sidebar, window.calendar_scroll.rect().bottomLeft()).y() < window.search.mapTo(
        window.sidebar, window.search.rect().topLeft()).y()
    assert not window.calendar_scroll.verticalScrollBar().isVisible()
    assert window.status.mapTo(window, window.status.rect().bottomRight()).y() < window.height()


@pytest.mark.parametrize('view', ['day', 'week', 'month'])
@pytest.mark.parametrize('all_day', [False, True])
def test_inline_confirmation_keeps_chosen_slot_and_is_one_undoable_change(desktop, view, all_day):
    window, store, app = desktop
    start = START.replace(hour=0) if all_day else START
    master = Event('alternatives', 'local', 'Choose a date', start,
                   start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                   recurrence_rule='FREQ=DAILY;COUNT=3', candidate=True, all_day=all_day)
    store.apply('Create alternatives', [master])
    window.refresh()
    chosen = window.visible_events[1]
    window.select_event(chosen.id)
    window.set_view(view)
    settle(app)
    surface = window.month if view == 'month' else window.all_day_canvas if all_day else window.canvas
    hit = next(rect for rect, event_id in surface.confirm_hits if event_id == chosen.id)
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, pos=hit.center().toPoint())
    QTest.qWait(app.doubleClickInterval() + 30)
    settle(app)
    remaining = store.list_events()
    assert len(remaining) == 1 and remaining[0].start == chosen.start
    assert not remaining[0].candidate and not remaining[0].recurring
    assert len(store.history()) == 2
    window.undo()
    assert store.list_events() == [master]
