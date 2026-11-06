"""Search selects real calendar occurrences without interrupting the query."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QEvent, QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from debacal.models import Calendar, Event
from debacal.recurrence import expand_series, recurrence_key
from debacal.store import Store
from debacal.selection import selection_roots
from debacal.ui import MainWindow

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
CTRL = Qt.KeyboardModifier.ControlModifier


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    windows, errors = [], []
    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.ui.now', return_value=START), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        windows.append(window)
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()
        yield app, window, store, windows
        for widget in reversed(windows):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def sample(identifier, *, day=0, calendar='local', title='Needle event', **kwargs):
    start = START + timedelta(days=day)
    return Event(identifier, calendar, title, start, start + timedelta(hours=1), **kwargs)


def search(window, text='Needle'):
    window.focus_search()
    window.search.setText(text)


def test_ctrl_f_focuses_and_selects_query_even_from_another_text_input(workspace):
    app, window, _, windows = workspace
    window.search.setText('Previous query')
    other = QLineEdit(window)
    other.show()
    windows.append(other)
    other.setText('Typing elsewhere')
    other.setFocus()
    app.processEvents()
    QTest.keyClick(other, Qt.Key.Key_F, CTRL)
    assert window.search.hasFocus()
    assert window.search.selectedText() == 'Previous query'
    assert other.text() == 'Typing elsewhere'


def test_ctrl_f_does_not_steal_focus_from_modal_editor(workspace):
    app, window, _, windows = workspace
    dialog = QDialog(window)
    dialog.setModal(True)
    field = QLineEdit(dialog)
    field.setText('Modal text')
    dialog.show()
    windows.append(dialog)
    field.setFocus()
    app.processEvents()
    QTest.keyClick(field, Qt.Key.Key_F, CTRL)
    assert field.hasFocus()
    assert not window.search.hasFocus()


def test_ctrl_f_as_second_stroke_respects_existing_calendar_shortcut(workspace):
    _, window, _, _ = workspace
    window.bindings['next'] = 'Ctrl+K, Ctrl+F'
    anchor = window.anchor
    QTest.keyClick(window.canvas, Qt.Key.Key_K, CTRL)
    QTest.keyClick(window.canvas, Qt.Key.Key_F, CTRL)
    assert window.anchor == anchor + timedelta(days=7)
    assert not window.search.hasFocus()


def test_old_search_default_migrates_but_custom_binding_is_preserved(workspace):
    app, _, store, windows = workspace
    for saved, expected in [('/', 'Ctrl+F'), ('Ctrl+P', 'Ctrl+P')]:
        store.set_setting('keybindings', {'search': saved})
        window = MainWindow(store, enable_sync=False)
        windows.append(window)
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()
        assert window.bindings['search'] == expected
        QTest.keyClick(window.canvas, Qt.Key.Key_F if expected == 'Ctrl+F' else Qt.Key.Key_P, CTRL)
        assert window.search.hasFocus()
        window.hide()


def test_search_field_arrows_reveal_and_wrap_without_rebuilding_query_or_index(workspace):
    _, window, store, _ = workspace
    events = [sample(str(index), day=day) for index, day in enumerate((0, 12, 35))]
    window.apply_events('Create search targets', events)
    with patch.object(store, 'list_events', wraps=store.list_events) as reads:
        search(window)
        index = window.results._index
        items = [window.results.item(row) for row in range(window.results.count())]
        for key, expected in [(Qt.Key.Key_Down, events[0]), (Qt.Key.Key_Down, events[1]),
                              (Qt.Key.Key_Down, events[2]), (Qt.Key.Key_Down, events[0]),
                              (Qt.Key.Key_Up, events[2])]:
            QTest.keyClick(QApplication.focusWidget(), key)
            assert window.selected_id == expected.id
            assert window.selected_day == expected.start.date()
            assert any(event.id == expected.id for event in window.visible_events)
            assert window.results.hasFocus()
            assert window.search.text() == 'Needle'
            assert window.results.isVisible()
            assert [window.results.item(row) for row in range(window.results.count())] == items
            assert window.results._index is index
        # Calendar reveals query their displayed date range; the unfiltered
        # read that builds the search index must occur only once.
        assert sum(not call.args and not call.kwargs for call in reads.call_args_list) == 1


def test_results_arrows_wrap_and_skip_result_limit_placeholder(workspace):
    _, window, _, _ = workspace
    events = [sample(f'event-{index}', day=index) for index in range(201)]
    window.apply_events('Create many search targets', events)
    search(window)
    assert window.results.count() == 201
    assert not window.results.event_id(window.results.item(200))
    assert not window.results.item(200).flags() & Qt.ItemFlag.ItemIsSelectable
    window.results.setCurrentRow(199)
    window.results.setFocus()
    QTest.keyClick(window.results, Qt.Key.Key_Down)
    assert window.results.currentRow() == 0
    assert window.selected_id == events[0].id
    assert window.results.hasFocus()
    QTest.keyClick(window.results, Qt.Key.Key_Up)
    assert window.results.currentRow() == 199
    assert window.selected_id == events[199].id
    assert window.search.text() == 'Needle'


def test_no_match_placeholder_is_not_a_navigation_target(workspace):
    _, window, _, _ = workspace
    event = sample('retained')
    window.apply_events('Create', [event])
    window.select_event(event.id)
    search(window, 'No such result')
    for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Return):
        QTest.keyClick(window.search, key)
        assert window.selected_id == event.id
        assert window.results.hasFocus()
    assert not window.results.event_id(window.results.currentItem())


def test_mouse_selection_reveals_hidden_calendar_and_keeps_result_list(workspace):
    app, window, store, _ = workspace
    hidden = Calendar('hidden', 'Hidden', '#123456', visible=False)
    store.save_calendar(hidden)
    events = [sample('first'), sample('hidden-target', day=45, calendar=hidden.id)]
    window.apply_events('Create', events)
    search(window)
    app.processEvents()
    item = window.results.item(1)
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton,
                     pos=window.results.visualItemRect(item).center())
    assert store.get_calendar(hidden.id).visible
    assert window.selected_id == events[1].id
    assert window.selected_day == events[1].start.date()
    assert window.results.currentItem() is item
    assert window.search.text() == 'Needle' and window.results.isVisible()


def test_ctrl_click_search_multiselection_and_delete_use_same_calendar_selection(workspace):
    app, window, store, _ = workspace
    events = [sample('first'), sample('second', day=1), sample('retained', day=2)]
    window.apply_events('Create', events)
    search(window)
    app.processEvents()
    first, second = window.results.item(0), window.results.item(1)
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton,
                     pos=window.results.visualItemRect(first).center())
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton, CTRL,
                     window.results.visualItemRect(second).center())
    assert window.selected_ids == {events[0].id, events[1].id}
    assert window.results.selected_ids() == window.selected_ids
    assert window.results.hasFocus()
    QTest.keyClick(window.results, Qt.Key.Key_Delete)
    assert store.get_event(events[0].id) is None
    assert store.get_event(events[1].id) is None
    assert store.get_event(events[2].id) == events[2]
    assert window.search.text() == 'Needle'
    assert len(store.history()) == 2


def test_all_day_search_selection_highlights_pinned_chip_while_grid_is_scrolled(workspace):
    app, window, _, _ = workspace
    midnight = START.replace(hour=0)
    event = Event('all-day-search', 'local', 'Needle all-day event', midnight,
                  midnight + timedelta(days=1), all_day=True)
    window.apply_events('Create all-day search target', [event])
    window.resize(window.minimumWidth(), window.minimumHeight())
    if not window.full_day:
        window.toggle_full_day()
    app.processEvents()
    bar = window.scroll.verticalScrollBar()
    bar.setValue(9 * 64)
    before = bar.value()
    assert before > 0
    search(window)
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    rect = next(rect for rect, value, _ in window.all_day_canvas.rects() if value.id == event.id)
    assert window.all_day_canvas.isVisible()
    assert rect.top() >= 0 and rect.bottom() <= window.all_day_canvas.height()
    assert event.id in window.all_day_canvas.selected_ids
    assert bar.value() == before
    assert window.selected_id == event.id and window.selected_day == event.start.date()
    assert window.results.hasFocus() and window.search.text() == 'Needle'


@pytest.mark.parametrize('view', ['week', 'month'])
def test_enter_commits_current_result_to_active_canvas_and_escape_exits(workspace, view):
    _, window, _, _ = workspace
    events = [sample('first'), sample('second', day=40)]
    window.apply_events('Create', events)
    window.set_view(view)
    search(window)
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    QTest.keyClick(window.results, Qt.Key.Key_Down)
    assert window.selected_id == events[1].id
    QTest.keyClick(window.results, Qt.Key.Key_Return)
    canvas = window.month if view == 'month' else window.canvas
    assert canvas.hasFocus()
    assert window.selected_id == events[1].id
    assert window.search.text() == 'Needle'
    QTest.keyClick(canvas, Qt.Key.Key_Escape)
    assert not window.search.text() and window.results.agenda_mode
    assert window.results_heading.text() == 'Next events'
    assert canvas.hasFocus()
    assert window.selected_id == events[1].id


def test_master_result_selects_real_historical_occurrence_with_excluded_initial_start(workspace):
    _, window, _, _ = workspace
    initial = START.replace(year=2025, month=1, day=1)
    master = replace(sample('recurring'), start=initial, end=initial + timedelta(hours=1),
                     recurrence_rule='FREQ=WEEKLY;COUNT=3', excluded_dates=(recurrence_key(initial),))
    window.apply_events('Create', [master])
    search(window)
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    selected = window.event_by_id(window.selected_id)
    assert selected.series_id == master.id
    assert selected.id != master.id
    assert selected.start == initial + timedelta(weeks=1)
    assert any(event.id == selected.id for event in window.visible_events)
    assert window.selected_day == selected.start.date()


def test_detached_title_parent_selects_series_and_dated_child_selects_moved_occurrence(workspace):
    app, window, store, _ = workspace
    master = sample('series', title='Regular study', recurrence_rule='FREQ=WEEKLY;COUNT=3')
    window.apply_events('Create', [master])
    instance = expand_series(master, START, START + timedelta(days=30))[1]
    moved = replace(instance, title='Needle detached result', start=instance.start + timedelta(days=40),
                    end=instance.end + timedelta(days=40))
    store.edit_occurrence(instance, moved)
    window.refresh()
    search(window)
    assert window.results.count() == 1
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    parent = window.results.topLevelItem(0)
    assert window.results.event_id(parent) == master.id
    assert window.results.row_scope(parent) == 'group'
    assert window.results.selected_row_ids() == {master.id}
    roots = selection_roots(store, window.selection.refs, window.operation_scope())
    assert roots == [store.get_event(master.id)]
    assert roots[0].recurrence_rule == master.recurrence_rule
    parent.setExpanded(True)
    child = next(parent.child(index) for index in range(parent.childCount())
                 if window.results.event_id(parent.child(index)) == moved.id)
    window.set_selection_mode('group')
    app.processEvents()
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton,
                     pos=window.results.visualItemRect(child).center())
    selected = store.get_occurrence(window.selected_id)
    assert selected.id == moved.id and selected.start == moved.start
    assert window.selected_day == moved.start.date()
    assert any(event.id == selected.id for event in window.visible_events)
    assert window.results.row_scope(child) == 'single'
    assert window.results.selected_row_ids() == {moved.id}
    assert not parent.isSelected()
    before = store.get_event(master.id)
    window.delete_event()
    remaining = store.query_occurrences(START, START + timedelta(days=100))
    assert len(remaining) == 2 and all(event.id != moved.id for event in remaining)
    assert store.get_event(master.id).recurrence_rule == master.recurrence_rule
    window.undo()
    assert store.get_event(master.id) == before


def test_actual_edits_invalidate_search_index_and_refresh_results(workspace):
    _, window, _, _ = workspace
    window.apply_events('Create', [sample('first')])
    search(window)
    index = window.results._index
    window.apply_events('Create another', [sample('second')])
    assert window.results._index is not index
    assert window.results.count() == 2
    assert window.search.text() == 'Needle'
