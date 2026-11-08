"""The highlighted union, calendar gestures and pending paste share one model."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, 17, 31, 123456, tzinfo=ZONE)
CTRL = Qt.KeyboardModifier.ControlModifier


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    errors = []
    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.placement.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.resize(1500, 1050)
        window.go_date(START.date())
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()

        def insert(*events):
            store.apply('Synthetic events', events)
            window.refresh()
            app.processEvents()

        yield app, window, store, insert
        QApplication.clipboard().clear()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def sample(key='a', start=START, **kwargs):
    if kwargs.get('recurrence_rule'):
        start = start.replace(microsecond=0)
    return Event(key, 'local', 'Planning ' + key, start, start + timedelta(hours=1), **kwargs)


def point(window, day, hour):
    canvas = window.canvas
    width = (canvas.width() - canvas.gutter) / len(canvas.days)
    return QPoint(round(canvas.gutter + (canvas.days.index(day) + .5) * width),
                  round(canvas.time_top + (hour - canvas.start_hour) * canvas.hour_height))


@pytest.mark.parametrize('scope,key,remaining', [('single', Qt.Key.Key_F2, 4),
                                               ('group', Qt.Key.Key_F3, 0),
                                               ('future', Qt.Key.Key_F4, 2)])
def test_mode_keys_highlight_exact_affected_events_and_delete_once(workspace, scope, key, remaining):
    app, window, store, insert = workspace
    master = sample('series', start=START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    insert(master)
    window.set_view('month')
    values = [event for event in window.visible_events if event.series_id == master.id]
    window.select_event(values[2].id)
    QTest.keyClick(window.month, key)
    assert window.selection.scope == scope
    expected = values[2:3] if scope == 'single' else values if scope == 'group' else values[2:]
    assert window.selected_ids == {event.id for event in expected}
    assert window.month.selected_ids == window.selected_ids
    before = len(store.history())
    QTest.keyClick(window.month, Qt.Key.Key_Delete)
    assert len(store.query_occurrences(START - timedelta(days=5), START + timedelta(days=70))) == remaining
    assert len(store.history()) == before + 1
    window.undo()
    assert store.list_events() == [master]


def test_ctrl_click_toggles_multi_selection_and_delete_is_atomic(workspace):
    app, window, store, insert = workspace
    a, b = sample('a'), sample('b', START.replace(hour=13))
    insert(a, b)
    for event, modifiers in ((a, Qt.KeyboardModifier.NoModifier), (b, CTRL)):
        rect = next(rect for rect, value in window.canvas.rects() if value.id == event.id)
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, modifiers, rect.center().toPoint())
    assert window.selected_ids == {a.id, b.id}
    before = len(store.history())
    window.delete_event()
    assert not store.list_events() and len(store.history()) == before + 1
    window.undo()
    assert {event.id for event in store.list_events()} == {a.id, b.id}


def test_bulk_editor_changes_common_fields_and_relative_times_only(workspace):
    _, window, store, insert = workspace
    a = sample('a', description='Keep A')
    b = sample('b', START.replace(hour=13), description='Keep B')
    insert(a, b)
    window.select_event(a.id)
    window.select_event(b.id, additive=True)
    def edit(dialog):
        assert dialog.common_fields_only
        dialog.title.setText('Shared title')
        dialog.start.setDateTime(dialog.start.dateTime().addSecs(3600))
        dialog.end.setDateTime(dialog.end.dateTime().addSecs(3600))
        dialog.save()
        return QDialog.DialogCode.Accepted
    with patch.object(EventDialog, 'exec', edit):
        window.edit_event()
    values = {event.id: event for event in store.list_events()}
    assert values[a.id].title == values[b.id].title == 'Shared title'
    assert values[a.id].description == 'Keep A' and values[b.id].description == 'Keep B'
    assert values[a.id].start == a.start + timedelta(hours=1)
    assert values[b.id].start == b.start + timedelta(hours=1)
    assert len(store.history()) == 2
    window.undo()
    assert {event.id: event for event in store.list_events()} == {a.id: a, b.id: b}


def test_ctrl_drag_copies_the_complete_multi_selection(workspace):
    app, window, store, insert = workspace
    a, b = sample('a'), sample('b', START.replace(hour=13))
    insert(a, b)
    window.select_event(a.id)
    window.select_event(b.id, additive=True)
    app.processEvents()
    rect = next(rect for rect, value in window.canvas.rects() if value.id == a.id)
    begin = rect.center().toPoint()
    width = (window.canvas.width() - window.canvas.gutter) / len(window.canvas.days)
    finish = begin + QPoint(round(width), 0)
    QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, CTRL, begin)
    QTest.mouseMove(window.canvas, finish)
    QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, CTRL, finish)
    app.processEvents()
    assert store.get_event(a.id) == a and store.get_event(b.id) == b
    copies = [event for event in store.list_events() if event.id not in {a.id, b.id}]
    assert len(copies) == 2
    assert {event.start for event in copies} == {a.start + timedelta(days=1), b.start + timedelta(days=1)}
    assert len(store.history()) == 2
    window.undo()
    assert len(store.list_events()) == 2


@pytest.mark.parametrize('confirm', ['enter', 'click'])
def test_paste_stages_at_relative_week_and_nomove_confirmation_keeps_precise_clock(workspace, confirm):
    app, window, store, insert = workspace
    source = sample()
    insert(source)
    window.select_event(source.id)
    QTest.keyClick(window.canvas, Qt.Key.Key_C, CTRL)
    window.navigate(2)
    QTest.keyClick(window.canvas, Qt.Key.Key_V, CTRL)
    target = source.start + timedelta(days=14)
    assert window.pending_paste.target == target
    assert store.list_events() == [source] and len(store.history()) == 1
    assert len(window.canvas.preview_events) == 1
    if confirm == 'enter':
        QTest.keyClick(window.canvas, Qt.Key.Key_Return)
    else:
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=point(window, date(2026, 11, 20), 15))
    assert window.pending_paste is None
    copied = next(event for event in store.list_events() if event.id != source.id)
    assert copied.start == target
    assert not window.canvas.preview_events and len(store.history()) == 2


def test_moved_paste_preview_tracks_pointer_with_stable_grid_and_escape_cancels(workspace):
    app, window, store, insert = workspace
    source = sample()
    insert(source)
    window.select_event(source.id)
    window.copy_event()
    window.paste_events()
    axes = window.canvas.start_hour, window.canvas.end_hour, window.canvas.hour_height
    QTest.mouseMove(window.canvas, point(window, date(2026, 11, 5), 15.25))
    app.processEvents()
    assert window.pending_paste.moved
    assert window.pending_paste.target == datetime(2026, 11, 5, 15, 15, tzinfo=ZONE)
    assert axes == (window.canvas.start_hour, window.canvas.end_hour, window.canvas.hour_height)
    assert store.list_events() == [source]
    QTest.keyClick(window.canvas, Qt.Key.Key_Escape)
    assert window.pending_paste is None and not window.canvas.preview_events
    assert store.list_events() == [source] and len(store.history()) == 1


def test_series_preview_contains_all_visible_occurrences_and_scope_change_does_not_drop_copy(workspace):
    _, window, store, insert = workspace
    master = sample('series', start=START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    insert(master)
    window.set_view('month')
    window.select_event(window.visible_events[0].id)
    window.set_selection_mode('group')
    window.copy_event()
    window.navigate(1)
    window.set_selection_mode('single')
    window.paste_events()
    assert len(window.month.preview_events) == 5
    assert store.list_events() == [master]
    window.confirm_paste()
    copied = next(event for event in store.list_events() if event.id != master.id)
    assert copied.recurrence_rule == master.recurrence_rule
    assert len(store.query_occurrences(datetime(2026, 12, 1, tzinfo=ZONE), datetime(2027, 1, 10, tzinfo=ZONE))) == 5


def test_search_navigation_focuses_results_and_delete_acts_on_highlight(workspace):
    app, window, store, insert = workspace
    a, b = sample('a'), sample('b', START + timedelta(days=14))
    insert(a, b)
    window.focus_search()
    window.search.setText('Planning')
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    assert window.results.hasFocus() and window.selected_id == a.id
    QTest.keyClick(window.results, Qt.Key.Key_Down)
    assert window.selected_id == b.id and window.anchor == b.start.date()
    QTest.keyClick(window.results, Qt.Key.Key_Delete)
    assert store.get_event(a.id) == a and store.get_event(b.id) is None
    assert window.search.text() == 'Planning'


def test_folded_search_occurrence_uses_same_future_selection_and_delete(workspace):
    app, window, store, insert = workspace
    master = sample('series', start=START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    insert(master)
    window.focus_search()
    window.search.setText('Planning')
    parent = window.results.topLevelItem(0)
    parent.setExpanded(True)
    app.processEvents()
    child = parent.child(2)
    window.results.setCurrentItem(child)
    window.set_selection_mode('future')
    targets = window.results.selection_targets()
    marked = {row_id for row_id, value in targets if row_id in window.results.selected_ids()}
    assert child.data(0, Qt.ItemDataRole.UserRole) in marked
    assert parent.child(0).data(0, Qt.ItemDataRole.UserRole) not in marked
    window.results.setFocus()
    QTest.keyClick(window.results, Qt.Key.Key_Delete)
    assert len(store.query_occurrences(START - timedelta(days=5), START + timedelta(days=70))) == 2


def test_all_day_is_fixed_and_multi_day_has_one_connected_hit(workspace):
    app, window, _, insert = workspace
    start = START.replace(day=2, hour=0, minute=0, second=0, microsecond=0)
    all_day = Event('trip', 'local', 'Trip', start, start + timedelta(days=4), all_day=True)
    insert(all_day, sample())
    before = window.all_day_canvas.mapToGlobal(QPoint(0, 0))
    window.scroll.verticalScrollBar().setValue(1000)
    app.processEvents()
    assert window.all_day_canvas.mapToGlobal(QPoint(0, 0)) == before
    assert len([value for _, value in window.all_day_canvas.rects() if value.id == all_day.id]) == 1
    assert not any(value.all_day for _, value in window.canvas.rects())


def test_copy_paste_at_original_time_keeps_every_selected_event(workspace):
    _, window, store, insert = workspace
    a, b = sample('a'), sample('b', START.replace(hour=13))
    insert(a, b)
    window.select_event(a.id)
    window.select_event(b.id, additive=True)
    window.copy_event(a.id)
    window.paste_events(target=a.start)
    window.confirm_paste()
    copies = [event for event in store.list_events() if event.id not in {a.id, b.id}]
    assert len(copies) == 2
    assert {event.start for event in copies} == {a.start, b.start}


def test_moved_recurring_occurrence_remains_selected_when_it_overlaps_sibling(workspace):
    _, window, store, insert = workspace
    master = sample('series', start=START.replace(day=2), recurrence_rule='FREQ=DAILY;COUNT=5')
    insert(master)
    values = sorted(window.visible_events, key=lambda event: event.start)
    earlier, moved = values[0], values[2]
    window.select_event(moved.id)
    window.move_event(moved.id, earlier.start, earlier.end, reference=moved)
    assert window.selected_id == moved.id
    window.delete_event()
    assert store.get_occurrence(moved.id) is None
    assert store.get_occurrence(earlier.id) == earlier


def test_paste_multiple_series_retains_both_families_in_selection(workspace):
    _, window, store, insert = workspace
    a = sample('a', start=START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    b = sample('b', start=START.replace(day=3, hour=13), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    insert(a, b)
    window.set_view('month')
    window.set_selection_mode('group')
    window.select_event(next(value.id for value in window.visible_events if value.series_id == a.id))
    window.select_event(next(value.id for value in window.visible_events if value.series_id == b.id), additive=True)
    window.copy_event()
    window.navigate(1)
    window.paste_events()
    assert len(window.pending_paste.events) == 2
    window.confirm_paste()
    assert len(store.list_events()) == 4
    assert len({reference.series_id for reference in window.selection.refs}) == 2


def test_fixed_all_day_drag_copies_one_complete_date_span(workspace):
    app, window, store, insert = workspace
    start = START.replace(day=2, hour=0, minute=0, second=0, microsecond=0)
    source = Event('trip', 'local', 'Trip', start, start + timedelta(days=3), all_day=True)
    insert(source)
    window.select_event(source.id)
    app.processEvents()
    canvas = window.all_day_canvas
    rect = next(rect for rect, value in canvas.rects() if value.id == source.id)
    begin = rect.center().toPoint()
    width = (canvas.width() - canvas.gutter) / len(canvas.days)
    finish = begin + QPoint(round(width), 0)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, CTRL, begin)
    QTest.mouseMove(canvas, finish)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, CTRL, finish)
    assert store.get_event(source.id) == source
    copied = next(value for value in store.list_events() if value.id != source.id)
    assert copied.all_day and copied.start == source.start + timedelta(days=1)
    assert copied.end.date() - copied.start.date() == timedelta(days=3)


def test_same_time_multi_move_keeps_both_selected_for_the_next_delete(workspace):
    _, window, store, insert = workspace
    a, b = sample('a'), sample('b')
    insert(a, b)
    window.select_event(a.id)
    window.select_event(b.id, additive=True)
    window.move_event(a.id, a.start + timedelta(hours=1), a.end + timedelta(hours=1))
    assert {event.id for event in window.selection.refs} == {a.id, b.id}
    assert window.selected_ids == {a.id, b.id}
    window.delete_event()
    assert store.list_events() == []
    assert len(store.history()) == 3


def test_same_time_independent_future_tails_remain_selected_after_the_split(workspace):
    _, window, store, insert = workspace
    a = sample('a', START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    b = sample('b', START.replace(day=2), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    insert(a, b)
    window.set_view('month')
    av = [event for event in window.visible_events if event.series_id == a.id]
    bv = [event for event in window.visible_events if event.series_id == b.id]
    window.set_selection_mode('future')
    window.select_event(av[2].id)
    window.select_event(bv[2].id, additive=True)
    window.move_event(av[2].id, av[2].start + timedelta(days=1), av[2].end + timedelta(days=1))
    roots = {event.series_id for event in window.selection.refs}
    assert len(roots) == 2 and not roots.intersection({a.id, b.id})
    assert len(window.selected_ids) == 6
    window.delete_event()
    remaining = store.query_occurrences(START - timedelta(days=5), START + timedelta(days=70))
    assert len(remaining) == 4
    assert {event.id for event in store.list_events()} == {a.id, b.id}
