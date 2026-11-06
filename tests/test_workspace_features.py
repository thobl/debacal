"""New calendar guidance, stable navigation, selection export, and year integration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import CalendarDialog, EventDialog
from debacal.ics import parse_ics
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.shortcuts import validate_bindings
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def settle(app):
    for _ in range(6):
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
        window.canvas.setFocus()
        settle(app)
        try:
            yield app, window, store
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert errors == []


def test_discovered_calendars_keep_names_without_changing_creation_selection(desktop):
    _, window, store = desktop
    def discovered(dialog):
        assert not hasattr(dialog, 'make_default')
        dialog.result_calendars = [Calendar('remote-a', 'Server work', '#123abc', kind='caldav', url='https://calendar.invalid/a/'),
                                   Calendar('remote-b', 'Server private', '#567abc', kind='caldav', url='https://calendar.invalid/b/')]
        dialog.result_calendar = dialog.result_calendars[0]
        return QDialog.DialogCode.Accepted
    with patch.object(CalendarDialog, 'exec', discovered):
        window.add_calendar()
    assert store.get_calendar('remote-a').name == 'Server work'
    assert store.get_calendar('remote-b').name == 'Server private'
    assert store.get_default_calendar().id == 'local'


def test_creation_preview_and_dialog_use_the_same_selected_calendar(desktop):
    _, window, store = desktop
    work = Calendar('work', 'Work', '#b43672')
    store.save_calendar(work)
    window.refresh(calendars=True)
    QTest.mouseClick(window.calendar_rows['work'].choice, Qt.MouseButton.LeftButton)
    assert window.canvas.creation_color().name() == work.color
    assert window.header.creation_color().name() == work.color
    assert window.month.creation_color().name() == work.color
    assert window.all_day_canvas.creation_color().name() == work.color
    assert window.year.creation_calendar == work
    chosen = []
    def inspect(dialog):
        chosen.append(dialog.calendar.currentData())
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        window.create_event(START)
    assert chosen == [work.id]


@pytest.mark.parametrize('scope,count', [('single', 1), ('group', 5), ('future', 3)])
def test_export_button_writes_exact_selection_scope(desktop, tmp_path, scope, count):
    _, window, store = desktop
    source = Event('series', 'local', 'Review', START, START + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=5')
    store.apply('Create', [source])
    window.refresh()
    instance = window.visible_events[2]
    window.select_event(instance.id)
    window.set_selection_mode(scope)
    target = tmp_path / 'selection.ics'
    history = store.history()
    with patch('debacal.ui.QFileDialog.getSaveFileName', return_value=(str(target), 'Calendar files (*.ics)')):
        QTest.mouseClick(window.export_selection_button, Qt.MouseButton.LeftButton)
    from debacal.recurrence import expand_events
    imported = parse_ics(target.read_bytes(), 'imported')
    events = expand_events(imported, START - timedelta(days=1), START + timedelta(days=10))
    assert len(events) == count
    assert store.list_events() == [source] and store.history() == history


def test_year_shortcut_leap_year_navigation_drilldown_and_creation(desktop):
    app, window, _ = desktop
    window.go_date(date(2028, 2, 29))
    QTest.keyClick(window.canvas, Qt.Key.Key_Y)
    settle(app)
    assert window.view == 'year' and len(window.dates()) == 366
    assert window.year.year == 2028 and window.heading.text() == '2028'
    assert window.stack.currentWidget() is window.year_scroll
    assert not window.full_day_button.isEnabled()
    window.navigate(1)
    assert window.anchor == date(2029, 2, 28) and window.year.year == 2029
    point = window.year.month_rect(4).center().toPoint()
    QTest.mouseClick(window.year, Qt.MouseButton.LeftButton, pos=point)
    settle(app)
    assert window.view == 'month' and window.anchor == date(2029, 4, 1)
    window.set_view('year')
    settle(app)
    selected = []
    def inspect(dialog):
        selected.append((dialog.value(dialog.start).date(), dialog.all_day.isChecked()))
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        point = window.year.day_rect(date(2029, 4, 12)).center().toPoint()
        QTest.mouseDClick(window.year, Qt.MouseButton.LeftButton, pos=point)
    assert selected == [(date(2029, 4, 12), True)]


def test_year_markers_preserve_ctrl_selection_and_search_follows_other_year(desktop):
    app, window, store = desktop
    a = Event('a', 'local', 'First', START, START + timedelta(hours=1))
    b = replace(a, id='b', title='Second', start=START + timedelta(days=2), end=START + timedelta(days=2, hours=1))
    future = replace(a, id='future', title='Next year', start=START.replace(year=2027), end=(START + timedelta(hours=1)).replace(year=2027))
    store.apply('Create', [a, b, future])
    window.refresh()
    window.set_view('year')
    settle(app)
    QTest.mouseClick(window.year, Qt.MouseButton.LeftButton, pos=window.year.event_rect(a.id).center().toPoint())
    QTest.mouseClick(window.year, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     pos=window.year.event_rect(b.id).center().toPoint())
    assert window.selected_ids == {a.id, b.id}
    window.search.setText('Next year')
    window.cycle_search_result(1)
    settle(app)
    assert window.view == 'year' and window.year.year == 2027
    assert window.selected_id == future.id and future.id in window.year.selected_ids


def test_year_copy_paste_retains_the_relative_date_and_time(desktop):
    app, window, store = desktop
    original = Event('copy', 'local', 'Copy in year', START, START + timedelta(hours=1))
    store.apply('Create', [original])
    window.refresh()
    window.set_view('year')
    window.select_event(original.id)
    window.year.setFocus()
    try:
        QTest.keyClick(window.year, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        window.navigate(1)
        QTest.keyClick(window.year, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert window.pending_paste is not None
        assert window.pending_paste.target.date() == date(2027, 11, 4)
        assert window.pending_paste.target.astimezone(ZONE).hour == START.hour
        QTest.keyClick(window.year, Qt.Key.Key_Return)
        settle(app)
        assert len(store.list_events()) == 2
        assert window.view == 'year'
        assert window.selected_day == date(2027, 11, 4)
    finally:
        QApplication.clipboard().clear()


def test_year_keeps_all_valid_series_when_annual_total_exceeds_ten_thousand(desktop):
    _, window, store = desktop
    start = START.replace(month=1, day=1)
    store.apply_remote([Event(f'series-{index}', 'local', 'Daily', start, start + timedelta(hours=1),
                              recurrence_rule='FREQ=DAILY') for index in range(30)])
    window.set_view('year')
    assert len(window.visible_events) == 365 * 30
    assert len({event.series_id for event in window.visible_events}) == 30
    assert store.list_quarantined_resources() == []


def test_too_dense_view_explains_range_limit_without_quarantining_a_valid_series(desktop):
    _, window, store = desktop
    from debacal.recurrence import RecurrenceRangeError
    source = Event('dense', 'local', 'Dense', START, START + timedelta(hours=1), recurrence_rule='FREQ=HOURLY')
    store.apply_remote([source])
    window.refresh()
    window.select_event(window.visible_events[0].id)
    with patch.object(store, 'query_occurrences', side_effect=RecurrenceRangeError('Choose a smaller date range.')):
        window.set_view('year')
    assert 'smaller date range' in window.notice.text()
    assert not window.selection.refs and not window.visible_events
    assert store.list_events() == [source]
    assert store.list_quarantined_resources() == []


def test_redo_uses_only_ctrl_y(desktop):
    _, window, store = desktop
    source = Event('event', 'local', 'Review', START, START + timedelta(hours=1))
    window.apply_events('Create', [source])
    window.canvas.setFocus()
    QTest.keyClick(window.canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert store.list_events() == []
    QTest.keyClick(window.canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert store.list_events() == []
    QTest.keyClick(window.canvas, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert store.list_events() == [source]
    assert window.redo_button.toolTip() == 'Redo (Ctrl+Y)'
    assert 'redo_alternative' not in window.bindings


def test_new_keys_preserve_custom_shortcuts_on_upgrade(desktop):
    app, _, store = desktop
    store.set_setting('keybindings_version', 2)
    store.set_setting('keybindings', {'new_event': 'Y', 'redo': 'Alt+R', 'history': 'Ctrl+Shift+Z'})
    other = MainWindow(store, enable_sync=False)
    try:
        assert other.bindings['new_event'] == 'Y' and other.bindings['year_view'] == ''
        assert other.bindings['redo'] == 'Alt+R' and 'redo_alternative' not in other.bindings
        validate_bindings(other.bindings)
    finally:
        other.close()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_disabled_redo_shortcut_stays_disabled_on_upgrade(desktop):
    app, _, store = desktop
    store.set_setting('keybindings_version', 2)
    store.set_setting('keybindings', {'redo': ''})
    other = MainWindow(store, enable_sync=False)
    try:
        assert other.bindings['redo'] == '' and 'redo_alternative' not in other.bindings
    finally:
        other.close()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize('version,saved,expected', [
    (3, {'redo': 'Ctrl+Y', 'redo_alternative': 'Ctrl+Shift+Z'}, 'Ctrl+Y'),
    (3, {'redo': 'Ctrl+Y', 'redo_alternative': 'Alt+R'}, 'Alt+R'),
    (3, {'redo': 'Alt+R', 'redo_alternative': 'Ctrl+Shift+Z'}, 'Alt+R'),
    (3, {'redo': '', 'redo_alternative': ''}, ''),
    (3, {'redo': 'Ctrl+Y', 'history': 'Ctrl+Y'}, ''),
    (3, {'redo': 'Ctrl+Y', 'history': 'Ctrl+Y, H'}, ''),
    (4, {'redo': 'Ctrl+Shift+Z'}, 'Ctrl+Y'),
    (4, {'redo': 'Alt+R'}, 'Alt+R'),
    (4, {'redo': ''}, ''),
    (4, {'redo': 'Ctrl+Shift+Z', 'history': 'Ctrl+Y'}, ''),
    (4, {'redo': 'Ctrl+Shift+Z', 'history': 'Ctrl+Y, H'}, ''),
])
def test_redo_upgrade_consolidates_defaults_without_losing_custom_actions(desktop, version, saved, expected):
    app, _, store = desktop
    store.set_setting('keybindings_version', version)
    store.set_setting('keybindings', saved)
    other = MainWindow(store, enable_sync=False)
    try:
        assert other.bindings['redo'] == expected
        assert 'redo_alternative' not in other.bindings
        assert 'redo_alternative' not in store.get_setting('keybindings')
        assert store.get_setting('keybindings_version') == 5
        if 'history' in saved:
            assert other.bindings['history'] == saved['history']
        validate_bindings(other.bindings)
    finally:
        other.close()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize('locale', ['en_GB', 'de_DE'])
def test_date_navigation_positions_stay_fixed_for_every_month(desktop, locale):
    app, window, _ = desktop
    window.preferences = Preferences(locale_name=locale, date_format='european')
    window.go_date(date(2026, 1, 15))
    window.set_view('month')
    settle(app)
    buttons = [window.previous_button, window.today_button, window.next_button]
    positions = [button.mapTo(window, button.rect().topLeft()) for button in buttons]
    for _ in range(14):
        QTest.mouseClick(window.next_button, Qt.MouseButton.LeftButton)
        settle(app)
        assert [button.mapTo(window, button.rect().topLeft()) for button in buttons] == positions
    assert window.anchor.year == 2027
