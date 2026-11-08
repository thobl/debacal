"""Workspace layout stays stable during search, selection, and synchronization."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from debacal.dialogs import EventDialog
from debacal.date_time_picker import DateTimeRangePicker
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.sync import SyncMessage
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(5):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        settle(app)
        try:
            yield window, store, app
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert errors == []


def grid_geometry(window):
    return (window.stack.geometry(), window.scroll.viewport().size(),
            window.canvas.start_hour, window.canvas.end_hour, window.canvas.hour_height)


@pytest.mark.parametrize('size', [(1400, 900), (1030, 690)])
def test_search_replaces_agenda_without_moving_or_rescaling_calendar(workspace, size):
    window, store, app = workspace
    window.resize(*size)
    event = Event('upcoming', 'local', 'Upcoming review', START + timedelta(days=2), START + timedelta(days=2, hours=1))
    store.apply('Create review', [event])
    window.refresh()
    settle(app)
    assert window.results.agenda_mode
    assert window.results.event_id(window.results.item(0)) == event.id
    baseline = grid_geometry(window)
    window.focus_search()
    window.search.setText('Upcoming')
    settle(app)
    assert not window.results.agenda_mode
    assert window.results_heading.text() == 'Search results'
    assert grid_geometry(window) == baseline
    window.exit_search()
    settle(app)
    assert window.results.agenda_mode and window.results.isVisible()
    assert grid_geometry(window) == baseline


def test_sync_and_long_multiline_feedback_never_change_calendar_geometry(workspace):
    window, store, app = workspace
    store.save_calendar(Calendar('remote', 'Remote', '#123abc', kind='caldav', url='https://calendar.invalid/'))
    window.engine = SimpleNamespace(events=Queue(), conflicts=[], sync_now=lambda: None, close=lambda: None)
    window.refresh(calendars=True)
    settle(app)
    baseline, icon_size = grid_geometry(window), window.sync_btn.size()
    window.sync_now()
    settle(app)
    assert 'Syncing' in window.status.text()
    assert not window.sync_btn.isEnabled()
    assert grid_geometry(window) == baseline
    window.engine.events.put(SyncMessage('error', ('A detailed resource error\n' * 40), 'remote'))
    window.engine.events.put(SyncMessage('finished', 'Sync finished'))
    window.poll()
    settle(app)
    assert 'Sync failed' in window.status.text()
    assert window.status_issues()[0].details == 'A detailed resource error\n' * 40
    assert window.warning_button.isVisible()
    assert window.sync_btn.isEnabled() and window.sync_btn.size() == icon_size
    assert grid_geometry(window) == baseline


def test_month_mode_keeps_full_day_and_other_controls_in_place(workspace):
    window, _, app = workspace
    buttons = [*window.view_buttons.values(), *window.selection_buttons.values(), window.full_day_button]
    baseline = [button.geometry() for button in buttons]
    window.set_view('month')
    settle(app)
    assert window.full_day_button.isVisible() and not window.full_day_button.isEnabled()
    assert [button.geometry() for button in buttons] == baseline
    window.set_view('day')
    settle(app)
    assert window.full_day_button.isEnabled()
    assert [button.geometry() for button in buttons] == baseline


def test_quick_calendar_choice_is_persisted_and_used_by_new_event(workspace):
    window, store, _ = workspace
    store.save_calendar(Calendar('work', 'Work', '#123abc'))
    store.save_calendar(Calendar('subscription', 'Read only', '#789abc', kind='ics'))
    window.refresh(calendars=True)
    assert not window.calendar_rows['subscription'].choice.isEnabled()
    QTest.mouseClick(window.calendar_rows['work'].choice, Qt.MouseButton.LeftButton)
    assert store.get_default_calendar().id == 'work'
    chosen = []
    def inspect(dialog):
        chosen.append(dialog.calendar.currentData())
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        window.create_event(START)
    assert chosen == ['work']
    assert window.calendar_rows['work'].choice.isChecked()
    assert not window.calendar_rows['local'].choice.isChecked()


def test_preferences_apply_locale_visuals_and_shortcuts_in_one_save(workspace):
    window, store, _ = workspace
    def change(dialog):
        dialog.locale.setCurrentIndex(dialog.locale.findData('de_DE'))
        dialog.font_scale.setValue(120)
        dialog.shortcut_fields['new_event'].setKeySequence('Ctrl+N')
        dialog.save()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', change):
        window.show_preferences()
    assert Preferences.load(store).locale_name == 'de_DE'
    assert window.preferences.font_scale == 120
    assert window.new_button.toolTip().endswith('(Ctrl+N)')
    assert window.mini.locale().name() == 'de_DE'
    assert window.locale().name() == 'de_DE'
    assert store.get_setting('keybindings')['new_event'] == 'Ctrl+N'


def test_event_editor_date_popups_follow_chosen_date_language(workspace):
    _, store, _ = workspace
    dialog = EventDialog(store.list_calendars(), START, preferences=Preferences(locale_name='en_GB'))
    try:
        for field in (dialog.start, dialog.end, dialog.repeat_until):
            assert field.locale().name() == 'en_GB'
            field.picker_requested.emit()
            picker = dialog.range_picker
            assert isinstance(picker, DateTimeRangePicker)
            assert picker.calendar.locale().name() == 'en_GB'
            picker.reject()
    finally:
        dialog.close()


def test_next_event_selection_uses_current_series_scope(workspace):
    window, store, app = workspace
    master = Event('series', 'local', 'Review', START + timedelta(hours=1), START + timedelta(hours=2),
                   recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create series', [master])
    window.refresh()
    window.set_view('day')
    window.set_selection_mode('group')
    window.results.setCurrentRow(0)
    assert window.selected_ids == {event.id for event in window.visible_events}
    assert len(window.selected_ids) == 1
    assert len(window.results.selected_ids()) == 3
    assert window.results.hasFocus()
    QTest.keyClick(window.results, Qt.Key.Key_Delete)
    settle(app)
    assert store.list_events() == []


def test_workspace_actions_have_tooltips_and_no_redundant_edit_delete_buttons(workspace):
    window, _, _ = workspace
    assert window.windowTitle() == 'Debacal'
    assert all(button.toolTip() for button, _, _ in window.command_buttons)
    assert not any(button.text() in ('Edit', 'Delete', 'Edit only this event')
                   for button in window.findChildren(QPushButton))
