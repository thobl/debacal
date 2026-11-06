"""Real-window checks for shared editing and search-row selection semantics."""
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

from debacal.dialogs import EventDialog
from debacal.models import Event
from debacal.recurrence import rules_equivalent
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 9, tzinfo=ZONE)
CTRL = Qt.KeyboardModifier.ControlModifier


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    errors = []
    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.dialogs.local_zone', return_value=ZONE), \
         patch('debacal.search_panel.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.resize(1500, 1000)
        window.go_date(START.date())
        window.show()
        window.activateWindow()
        app.processEvents()
        yield app, window, store
        app.clipboard().clear()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def sample(identifier, *, recurring=False, hour=9):
    start = START.replace(hour=hour)
    return Event(identifier, 'local', 'Planning ' + identifier, start, start + timedelta(hours=1),
                 recurrence_rule='FREQ=DAILY;COUNT=5' if recurring else '')


def insert(window, store, *events):
    store.apply('Synthetic events', events)
    window.refresh()
    QApplication.processEvents()


def search(window):
    window.focus_search()
    window.search.setText('Planning')
    QApplication.processEvents()


def parent(window, identifier):
    return next(window.results.topLevelItem(index) for index in range(window.results.topLevelItemCount())
                if window.results.event_id(window.results.topLevelItem(index)) == identifier)


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_double_click_and_edit_shortcut_open_identical_scoped_editor(workspace, scope):
    app, window, store = workspace
    master = sample('series', recurring=True)
    insert(window, store, master)
    occurrence = window.visible_events[2]
    window.select_event(occurrence.id)
    window.set_selection_mode(scope)
    seen = []
    def inspect(dialog):
        seen.append((dialog.original.id, dialog.scope.currentData(), set(dialog.scope_buttons)))
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_E)
        rect = next(rect for rect, value, _ in window.canvas.rects() if value.id == occurrence.id)
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        QTest.mouseDClick(window.canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0][1] == scope and seen[0][2] == {'single', 'group', 'future'}
    assert len(store.history()) == 1


@pytest.mark.parametrize('modifiers', [Qt.KeyboardModifier.NoModifier, CTRL])
def test_real_double_click_retains_the_pre_click_multi_selection(workspace, modifiers):
    app, window, store = workspace
    a, b = sample('a'), sample('b', hour=13)
    insert(window, store, a, b)
    window.select_event(a.id)
    window.select_event(b.id, additive=True)
    seen = []
    def inspect(dialog):
        seen.append((dialog.common_fields_only, {value.id for value in window.selection.refs}))
        return QDialog.DialogCode.Rejected
    rect = next(rect for rect, value, _ in window.canvas.rects() if value.id == a.id)
    with patch.object(EventDialog, 'exec', inspect):
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, modifiers, rect.center().toPoint())
        QTest.mouseDClick(window.canvas, Qt.MouseButton.LeftButton, modifiers, rect.center().toPoint())
    assert seen == [(True, {a.id, b.id})]


def test_stationary_ctrl_updates_copy_badge_in_the_real_window(workspace):
    app, window, store = workspace
    a = sample('a')
    insert(window, store, a)
    rect = next(rect for rect, value, _ in window.canvas.rects() if value.id == a.id)
    begin, finish = rect.center().toPoint(), rect.center().toPoint() + QPoint(0, 30)
    QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=begin)
    QTest.mouseMove(window.canvas, finish)
    app.processEvents()
    assert window.canvas.preview_events
    before = window.canvas.grab().toImage()
    QTest.keyPress(window.canvas, Qt.Key.Key_Control)
    app.processEvents()
    assert window.canvas.gesture_modifiers & CTRL
    copying = window.canvas.grab().toImage()
    assert copying != before
    QTest.keyRelease(window.canvas, Qt.Key.Key_Control)
    app.processEvents()
    assert not window.canvas.gesture_modifiers & CTRL
    assert window.canvas.grab().toImage() == before
    QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=finish)


@pytest.mark.parametrize('mode', ['single', 'group', 'future'])
def test_series_root_selects_and_deletes_whole_series_in_every_mode(workspace, mode):
    _, window, store = workspace
    a, b = sample('a', recurring=True), sample('b', recurring=True, hour=13)
    insert(window, store, a, b)
    window.set_selection_mode(mode)
    search(window)
    window.results.setCurrentItem(parent(window, a.id))
    assert window.results.selected_row_ids() == {a.id}
    assert window.selected_ids == {value.id for value in window.visible_events if value.series_id == a.id}
    window.copy_event()
    copied = window.read_event_clipboard()
    assert len(copied.masters) == 1 and copied.masters[0].recurrence_rule == a.recurrence_rule
    before = len(store.history())
    window.delete_event()
    assert store.get_event(a.id) is None and store.get_event(b.id) == b
    assert len(store.history()) == before + 1


@pytest.mark.parametrize('mode', ['group', 'future'])
def test_dated_child_selects_only_itself_and_does_not_select_parent(workspace, mode):
    app, window, store = workspace
    master = sample('series', recurring=True)
    insert(window, store, master)
    window.set_selection_mode(mode)
    search(window)
    root = parent(window, master.id)
    root.setExpanded(True)
    app.processEvents()
    child = root.child(0)
    child_id = window.results.event_id(child)
    window.results.setCurrentItem(child)
    assert window.results.selected_row_ids() == {child_id}
    assert not root.isSelected() and window.selected_ids == {child_id}
    window.delete_event()
    assert store.get_event(master.id) is not None and store.get_occurrence(child_id) is None
    assert len(store.query_occurrences(START, START + timedelta(days=7))) == 4


@pytest.mark.parametrize('root_first', [True, False])
def test_ctrl_mixed_parent_and_other_series_child_preserves_exact_union(workspace, root_first):
    app, window, store = workspace
    a, b = sample('a', recurring=True), sample('b', recurring=True, hour=13)
    insert(window, store, a, b)
    window.set_selection_mode('group')
    search(window)
    b_root = parent(window, b.id)
    b_root.setExpanded(True)
    app.processEvents()
    child_id = window.results.event_id(b_root.child(1))
    values = [a.id, child_id] if root_first else [child_id, a.id]
    window.results.result_selected.emit(values[0], Qt.KeyboardModifier.NoModifier)
    window.results.result_selected.emit(values[1], CTRL)
    assert window.results.selected_row_ids() == {a.id, child_id}
    expected = {value.id for value in window.visible_events if value.series_id == a.id} | {child_id}
    assert window.selected_ids == expected
    window.copy_event()
    copied = window.read_event_clipboard()
    assert len(copied.masters) == 2 and sum(value.recurring for value in copied.masters) == 1
    before = len(store.history())
    window.delete_event()
    assert store.get_event(a.id) is None
    assert store.get_event(b.id).recurrence_rule == b.recurrence_rule and store.get_occurrence(child_id) is None
    assert len(store.query_occurrences(START, START + timedelta(days=7))) == 4
    assert len(store.history()) == before + 1
    window.undo()
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}


def test_editing_search_root_opens_all_scope_and_keeps_standard_recurrence(workspace):
    _, window, store = workspace
    master = sample('series', recurring=True)
    insert(window, store, master)
    search(window)
    window.results.setCurrentItem(parent(window, master.id))
    def edit(dialog):
        assert dialog.scope.currentData() == 'group'
        dialog.title.setText('Renamed series')
        dialog.save()
        return QDialog.DialogCode.Accepted
    with patch.object(EventDialog, 'exec', edit):
        window.edit_event()
    changed = store.get_event(master.id)
    assert changed.title == 'Renamed series' and rules_equivalent(changed.recurrence_rule, master.recurrence_rule, master)
    assert not changed.recurrence_overrides


@pytest.mark.parametrize('key,scope,renamed', [(Qt.Key.Key_F2, 'single', 1), (Qt.Key.Key_F4, 'future', 3)])
def test_search_root_editor_can_switch_to_one_or_following(workspace, key, scope, renamed):
    app, window, store = workspace
    master = sample('series', recurring=True)
    insert(window, store, master)
    window.go_date((START + timedelta(days=2)).date())
    window.set_view('day')
    search(window)
    window.results.setCurrentItem(parent(window, master.id))
    def edit(dialog):
        assert dialog.scope.currentData() == 'group'
        dialog.show()
        dialog.activateWindow()
        app.processEvents()
        QTest.keyClick(dialog.title, key)
        assert dialog.scope.currentData() == scope
        dialog.title.setText('Changed selection')
        dialog.save()
        return QDialog.DialogCode.Accepted
    with patch.object(EventDialog, 'exec', edit):
        window.edit_event()
    values = store.query_occurrences(START, START + timedelta(days=7))
    assert len(values) == 5
    assert sum(value.title == 'Changed selection' for value in values) == renamed
    assert all(value.title == master.title for value in values[:2])
    window.undo()
    assert store.list_events() == [master]


def test_keyboard_more_loads_ten_dates_without_changing_selection(workspace):
    app, window, store = workspace
    source = sample('infinite')
    source = Event(source.id, source.calendar_id, source.title, source.start, source.end,
                   recurrence_rule='FREQ=DAILY')
    insert(window, store, source)
    search(window)
    root = parent(window, source.id)
    root.setExpanded(True)
    app.processEvents()
    assert root.childCount() == 11
    child = root.child(9)
    child_id = window.results.event_id(child)
    window.results.setCurrentItem(child)
    window.results.setCurrentItem(root.child(10))
    QTest.keyClick(window.results, Qt.Key.Key_Return)
    assert root.childCount() == 21
    assert window.selected_id == child_id
    assert window.results.selected_row_ids() == {child_id}
    assert len(store.history()) == 1


def test_mixed_search_bulk_title_edit_keeps_both_custom_definitions(workspace):
    app, window, store = workspace
    a, b = [replace(sample(key, recurring=True, hour=hour), schedule_expression='4*+1d',
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
            for key, hour in [('a', 9), ('b', 13)]]
    insert(window, store, a, b)
    search(window)
    b_root = parent(window, b.id)
    b_root.setExpanded(True)
    app.processEvents()
    child_id = window.results.event_id(b_root.child(1))
    window.results.result_selected.emit(a.id, Qt.KeyboardModifier.NoModifier)
    window.results.result_selected.emit(child_id, CTRL)
    def edit(dialog):
        assert dialog.common_fields_only and dialog.scope.currentData() == 'single'
        dialog.title.setText('New shared title')
        dialog.save()
        return QDialog.DialogCode.Accepted
    with patch.object(EventDialog, 'exec', edit):
        window.edit_event()
    for original in (a, b):
        changed = store.get_event(original.id)
        assert changed.schedule_expression == original.schedule_expression
        assert changed.expression_anchor == original.expression_anchor
        assert changed.schedule_syntax == 2
        assert rules_equivalent(changed.recurrence_rule, original.recurrence_rule, original)
    assert store.get_event(a.id).title == 'New shared title'
    assert store.get_event(b.id).title == b.title
    assert store.get_occurrence(child_id).title == 'New shared title'
    assert len(store.history()) == 2
    window.undo()
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}
