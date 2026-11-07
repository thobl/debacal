"""Real keyboard and mouse gestures share one scope policy and atomic history."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QPoint, Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.preferences_dialog import PreferencesDialog
from debacal.event_clipboard import EventClipboard
from debacal.ics import export_ics
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow, EVENT_MIME, DEFAULT_BINDINGS


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 10, tzinfo=ZONE)
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CASES = [('single', Qt.Key.Key_F2), ('group', Qt.Key.Key_F3), ('future', Qt.Key.Key_F4)]


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    master = Event('series', 'local', 'Weekly review', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY;BYDAY=MO;COUNT=5')
    store.apply('Create baseline', [master])
    errors, windows = [], []
    with patch('debacal.ui.local_zone', return_value=ZONE), patch('debacal.dialogs.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        window = MainWindow(store, enable_sync=False)
        windows.append(window)
        window.anchor = date(2026, 11, 16)
        window.refresh()
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()
        selected = all_events(store)[2]
        window.select_event(selected.id)
        yield app, window, store, master, selected, windows
        QApplication.clipboard().clear()
        for widget in reversed(windows):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def all_events(store):
    return store.query_occurrences(START - timedelta(days=5), START + timedelta(days=150))


def key(window, letter, modifiers=NONE):
    window.activateWindow()
    window.canvas.setFocus()
    QApplication.instance().processEvents()
    QTest.keyClick(window.canvas, letter, modifiers)
    QApplication.instance().processEvents()


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_edit_key_uses_three_scopes_and_one_undo(workspace, scope, mode_key):
    _, window, store, master, _, _ = workspace
    seen = []
    def edit(dialog):
        seen.append(dialog.scope.currentData())
        dialog.title.setText('Changed review')
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()
    with patch.object(EventDialog, 'exec', edit):
        key(window, mode_key)
        key(window, Qt.Key.Key_E)
    assert seen == [scope]
    events = all_events(store)
    assert len(events) == 5
    assert sum(event.title == 'Changed review' for event in events) == {'single': 1, 'group': 5, 'future': 3}[scope]
    assert len(store.list_events()) == (2 if scope == 'future' else 1)
    assert window.selected_id
    assert window.event_by_id(window.selected_id).title == 'Changed review'
    assert len(store.history()) == 2
    window.undo()
    assert store.list_events() == [master]


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_delete_key_uses_three_scopes(workspace, scope, mode_key):
    _, window, store, master, _, _ = workspace
    key(window, mode_key)
    key(window, Qt.Key.Key_Delete)
    assert len(all_events(store)) == {'single': 4, 'group': 0, 'future': 2}[scope]
    assert len(store.history()) == 2
    window.undo()
    assert store.list_events() == [master]


def test_held_modifiers_do_not_change_the_persistent_selection_mode(workspace):
    _, window, store, _, _, _ = workspace
    key(window, Qt.Key.Key_F4)
    key(window, Qt.Key.Key_Delete, CTRL | SHIFT)
    assert len(all_events(store)) == 5 and window.selection.scope == 'future'
    key(window, Qt.Key.Key_Delete)
    assert len(all_events(store)) == 2


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_copy_and_paste_keys_preserve_scope_with_fresh_identities(workspace, scope, mode_key):
    _, window, store, master, _, _ = workspace
    key(window, mode_key)
    key(window, Qt.Key.Key_C, CTRL)
    assert QApplication.clipboard().mimeData().hasFormat(EVENT_MIME)
    assert 'BEGIN:VCALENDAR' in QApplication.clipboard().text()
    assert len(store.history()) == 1
    target = datetime(2027, 1, 4, 10, tzinfo=ZONE)
    with patch.object(window, 'paste_target', return_value=target):
        for count in range(2):
            key(window, Qt.Key.Key_V, CTRL)
            assert window.pending_paste is not None
            assert len(store.list_events()) == 1 + count
            key(window, Qt.Key.Key_Return)
    copies = [event for event in store.list_events() if event.id != master.id]
    assert len(copies) == 2
    assert len({event.ical_uid for event in [master, *copies]}) == 3
    assert len(all_events(store)) == 5 + {'single': 2, 'group': 10, 'future': 6}[scope]
    assert sum(event.start == target for event in all_events(store)) == 2
    assert len(store.history()) == 3
    window.undo()
    assert len(store.list_events()) == 2
    assert store.get_event(master.id) == master


def test_changing_mode_after_copy_preserves_the_exact_captured_series(workspace):
    _, window, store, master, _, _ = workspace
    key(window, Qt.Key.Key_F3)
    key(window, Qt.Key.Key_C, CTRL)
    key(window, Qt.Key.Key_F2)
    window.paste_events(scope='single', target=datetime(2027, 1, 4, 10, tzinfo=ZONE))
    assert store.list_events() == [master]
    window.confirm_paste()
    copied = next(event for event in store.list_events() if event.id != master.id)
    assert copied.recurring and len(all_events(store)) == 10


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_day_key_changes_view_without_changing_clipboard_or_events(workspace, scope, mode_key):
    _, window, store, master, _, _ = workspace
    QApplication.clipboard().setText('Keep this text')
    key(window, mode_key)
    key(window, Qt.Key.Key_D)
    assert QApplication.clipboard().text() == 'Keep this text'
    assert window.view == 'day'
    assert len(all_events(store)) == 5
    assert len(store.history()) == 1
    assert store.list_events() == [master]
    assert 'duplicate_event' not in window.bindings


def test_main_text_fields_keep_native_copy_paste_and_literal_typing(workspace):
    _, window, store, _, _, _ = workspace
    window.search.setFocus()
    window.search.setText('hello world')
    window.search.selectAll()
    QTest.keyClick(window.search, Qt.Key.Key_C, CTRL)
    assert QApplication.clipboard().text() == 'hello world'
    QTest.keyClick(window.search, Qt.Key.Key_C)
    QTest.keyClick(window.search, Qt.Key.Key_V)
    QTest.keyClick(window.search, Qt.Key.Key_V, CTRL)
    assert window.search.text() == 'cvhello world'
    assert len(store.history()) == 1


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_mouse_drag_moves_the_requested_scope(workspace, scope, mode_key):
    app, window, store, master, selected, _ = workspace
    canvas = window.canvas
    key(window, mode_key)
    rect = next(rect for rect, event in canvas.rects() if event.id == selected.id)
    begin = rect.center().toPoint()
    day_width = (canvas.width() - canvas.gutter) / len(canvas.days)
    finish = begin + QPoint(round(2 * day_width), 0)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, NONE, begin)
    QTest.mouseMove(canvas, finish, delay=1)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, NONE, finish)
    app.processEvents()
    events = all_events(store)
    assert len(events) == 5
    assert sum(event.start.weekday() == 2 for event in events) == {'single': 1, 'group': 5, 'future': 3}[scope]
    assert window.selected_id
    assert window.event_by_id(window.selected_id).start.weekday() == 2
    assert len(store.history()) == 2
    window.undo()
    assert store.list_events() == [master]


@pytest.mark.parametrize('scope,mode_key', CASES)
def test_mouse_resize_changes_duration_for_requested_scope(workspace, scope, mode_key):
    app, window, store, master, selected, _ = workspace
    canvas = window.canvas
    key(window, mode_key)
    rect = next(rect for rect, event in canvas.rects() if event.id == selected.id)
    begin = QPoint(round(rect.center().x()), round(rect.bottom() - 2))
    finish = begin + QPoint(0, canvas.hour_height)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, NONE, begin)
    QTest.mouseMove(canvas, finish, delay=1)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, NONE, finish)
    app.processEvents()
    assert sum(event.end - event.start == timedelta(hours=2) for event in all_events(store)) == {'single': 1, 'group': 5, 'future': 3}[scope]
    window.undo()
    assert store.list_events() == [master]


def test_double_click_edits_existing_event_in_persistent_following_mode(workspace):
    _, window, _, master, selected, _ = workspace
    seen = []
    key(window, Qt.Key.Key_F4)
    rect = next(rect for rect, event in window.canvas.rects() if event.id == selected.id)
    def inspect(dialog):
        seen.append((dialog.original.id, dialog.series.id, dialog.scope.currentData()))
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        QTest.mouseDClick(window.canvas, Qt.MouseButton.LeftButton, NONE, rect.center().toPoint())
    assert seen == [(selected.id, master.id, 'future')]


def test_configured_mode_shortcuts_and_persistence(workspace):
    app, window, store, _, _, windows = workspace
    def configure(dialog):
        assert dialog.tabs.currentWidget() is dialog.shortcut_editor
        dialog.shortcut_fields['scope_single'].setKeySequence('F5')
        dialog.shortcut_fields['scope_all'].setKeySequence('F6')
        dialog.shortcut_fields['scope_following'].setKeySequence('F7')
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', configure):
        window.show_shortcuts()
    assert window.bindings['scope_single'] == 'F5'
    assert window.bindings['scope_all'] == 'F6'
    assert window.bindings['scope_following'] == 'F7'
    seen = []
    def inspect(dialog):
        seen.append(dialog.scope.currentData())
        assert dialog.mode_shortcuts['scope_all'].key().toString() == 'F6'
        assert dialog.mode_shortcuts['scope_following'].key().toString() == 'F7'
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        key(window, Qt.Key.Key_F6)
        key(window, Qt.Key.Key_E)
        key(window, Qt.Key.Key_F7)
        key(window, Qt.Key.Key_E)
    assert seen == ['group', 'future']
    other = MainWindow(store, enable_sync=False)
    windows.append(other)
    assert other.bindings['scope_all'] == 'F6' and other.bindings['scope_following'] == 'F7'
    assert other.selection.scope == 'future'
    app.processEvents()


def test_old_default_bindings_migrate_without_overwriting_other_customizations(workspace):
    _, _, store, _, _, windows = workspace
    store.set_setting('keybindings_version', 0)
    store.set_setting('keybindings', {'day_view': '1', 'copy_event': 'C', 'paste_event': 'V',
                                     'duplicate_event': 'D', 'edit_single': 'Shift+E', 'search': 'Ctrl+F', 'new_event': 'Ctrl+N'})
    other = MainWindow(store, enable_sync=False)
    windows.append(other)
    assert other.bindings['day_view'] == 'D'
    assert other.bindings['copy_event'] == 'Ctrl+C'
    assert other.bindings['paste_event'] == 'Ctrl+V'
    assert 'duplicate_event' not in other.bindings
    assert other.bindings['new_event'] == 'Ctrl+N'
    assert other.bindings['edit_single'] == ''
    assert other.bindings['search'] == 'Ctrl+F'


def test_readonly_moves_and_malformed_clipboard_leave_history_untouched(workspace):
    _, window, store, _, selected, _ = workspace
    calendar = store.get_calendar('local')
    store.save_calendar(replace(calendar, read_only=True))
    window.move_event(selected.id, selected.start + timedelta(days=2), selected.end + timedelta(days=2), scope='future')
    assert len(store.history()) == 1
    assert 'read-only' in window.local_issues[-1].details
    QApplication.clipboard().setText('ordinary text')
    window.paste_events(scope='group')
    assert len(store.history()) == 1


def test_external_ics_clipboard_pastes_into_default_calendar(workspace):
    _, window, store, master, _, _ = workspace
    source = Event('external', 'other', 'External appointment', START, START + timedelta(hours=2))
    QApplication.clipboard().setText(export_ics([source]).decode())
    target = datetime(2027, 1, 4, 10, tzinfo=ZONE)
    window.paste_events(scope='single', target=target)
    assert store.list_events() == [master]
    window.confirm_paste()
    copied = next(event for event in store.list_events() if event.id != master.id)
    assert copied.calendar_id == 'local' and copied.title == 'External appointment'
    assert copied.start == target and copied.end - copied.start == timedelta(hours=2)


def test_month_paste_changes_date_without_rounding_copied_clock(workspace):
    app, window, _, _, _, _ = workspace
    source_start = START.replace(second=30, microsecond=123456)
    source = Event('precise', 'local', 'Precise event', source_start, source_start + timedelta(hours=1))
    selection = EventClipboard.capture([source], source)
    window.set_view('month')
    app.processEvents()
    target_day = date(2026, 11, 20)
    window.select_day(target_day)
    index = (target_day - window.month.start).days
    point = QPoint(round((index % 7 + .5) * window.month.width() / 7),
                   round(28 + (index // 7 + .5) * (window.month.height() - 28) / 6))
    with patch('PySide6.QtGui.QCursor.pos', return_value=window.month.mapToGlobal(point)):
        assert window.paste_target(selection) == source_start.replace(day=20)


def test_unsupported_future_partition_is_reported_before_dialog_or_mutation(workspace):
    _, window, store, _, _, _ = workspace
    start = datetime(2026, 3, 2, 10, tzinfo=ZONE)
    advanced = Event('advanced', 'local', 'Advanced yearly rule', start, start + timedelta(hours=1),
                     recurrence_rule='FREQ=YEARLY;BYWEEKNO=10;BYDAY=MO;COUNT=5')
    store.apply('Create advanced rule', [advanced])
    selected = store.query_occurrences(datetime(2027, 1, 1, tzinfo=ZONE), datetime(2027, 12, 31, tzinfo=ZONE))[0]
    with patch.object(EventDialog, 'exec') as execute:
        window.edit_event(selected.id, scope='future')
    execute.assert_not_called()
    assert store.get_event(advanced.id) == advanced
    assert len(store.history()) == 2
    assert 'whole-series' in window.local_issues[-1].details


def test_paste_rejects_nonexistent_end_without_creating_an_event(workspace):
    _, window, store, master, _, _ = workspace
    source_start = datetime(2027, 3, 27, 1, 30, tzinfo=ZONE)
    source = Event('clock-gap', 'local', 'Clock change', source_start, source_start + timedelta(hours=1))
    selection = EventClipboard.capture([source], source)
    with patch.object(window, 'read_event_clipboard', return_value=selection):
        window.paste_events(scope='single', target=source_start + timedelta(days=1))
    assert store.list_events() == [master]
    assert len(store.history()) == 1
    assert 'clock change' in window.local_issues[-1].details
