"""The main calendar authors an unsaved editor draft without editing stored events."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from time import monotonic
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from debacal.event_dialog import EventDialog
from debacal.models import Event
from debacal.recurrence import expand_event
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
DURATION = timedelta(minutes=45)
CTRL = Qt.KeyboardModifier.ControlModifier


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    errors = []
    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.year_view.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.resize(1500, 1050)
        window.go_date(START.date())
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()
        yield app, window, store
        QApplication.clipboard().clear()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def instances(master):
    return expand_event(master, START - timedelta(days=30), START + timedelta(days=400))


def timed_point(window, day, hour):
    canvas = window.canvas
    width = (canvas.width() - canvas.gutter) / len(canvas.days)
    return QPoint(round(canvas.gutter + (canvas.days.index(day) + .5) * width),
                  round(canvas.time_top + (hour - canvas.start_hour) * canvas.hour_height))


def month_point(window, day):
    widget = window.month
    index = (day - widget.start).days
    width = (widget.width() - widget.week_gutter) / 7
    height = (widget.height() - widget.header_height) / 6
    return QPoint(round(widget.week_gutter + (index % 7 + .5) * width),
                  round(widget.header_height + (index // 7 + .85) * height))


def wait_for(condition, timeout=2):
    """Allow native pointer/window delivery without guessing an X11 delay."""
    if condition():
        return
    loop = QEventLoop()
    poll, deadline = QTimer(), QTimer()
    poll.timeout.connect(lambda: loop.quit() if condition() else None)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start(5)
    deadline.start(round(timeout * 1000))
    try:
        loop.exec()
    finally:
        poll.stop()
        deadline.stop()
    assert condition(), 'The native calendar interaction did not finish.'


def play_editor(window, actions, *, opener=None):
    """Drive genuine nested dialog/calendar loops from a native timer.

    A callback runs only after its phase has actually become active. Keeping
    Python out of a busy polling loop also lets database workers complete.
    """
    state, errors = {}, []
    pending = list(actions)
    deadline = monotonic() + 10
    running = False
    timer = QTimer(window)

    def stop_with_error(error):
        errors.append(error)
        pending.clear()
        if window.occurrence_picker is not None:
            window.finish_occurrence_picking()
        dialog = state.get('dialog')
        if dialog is not None:
            QTimer.singleShot(0, dialog.reject)

    def advance():
        nonlocal running
        if running or not pending:
            return
        if monotonic() >= deadline:
            stop_with_error(AssertionError('The editor workflow did not reach its next phase.'))
            return
        phase, action = pending[0]
        dialog = QApplication.activeModalWidget()
        ready = (window.occurrence_picker is not None if phase == 'calendar'
                 else isinstance(dialog, EventDialog) and not dialog.isMinimized())
        if not ready:
            return
        running = True
        try:
            if phase == 'dialog':
                state.setdefault('dialog', dialog)
                assert state['dialog'] is dialog, 'Returning from the calendar must reuse the editor.'
            pending.pop(0)
            action(state)
        except BaseException as error:
            stop_with_error(error)
        finally:
            running = False

    timer.timeout.connect(advance)
    timer.start(5)
    try:
        (opener or (lambda: window.create_event(START, START + DURATION)))()
    finally:
        timer.stop()
        timer.deleteLater()
    if errors:
        raise errors[0]
    assert not pending, 'The workflow returned before all interactions were performed.'
    assert window.occurrence_picker is None
    return state


def begin(state):
    dialog = state['dialog']
    state['editor_flags'] = dialog.windowFlags()
    dialog.title.setText('A calendar-authored draft')
    QTest.mouseClick(dialog.add_occurrences_button, Qt.MouseButton.LeftButton)


def test_calendar_mode_suspends_modal_editor_and_save_is_one_undoable_operation(workspace):
    app, window, store = workspace

    def add_dates(state):
        dialog = state['dialog']
        assert dialog.isMinimized()
        assert QApplication.activeModalWidget() is None
        assert store.list_events() == [] and store.history() == []
        assert window.canvas.occurrence_picking_active
        target = datetime(2026, 11, 5, 14, tzinfo=ZONE)
        point = timed_point(window, target.date(), 14)
        QTest.mouseMove(window.canvas, point)
        wait_for(lambda: window.canvas.preview_events and window.canvas.preview_events[0].start == target)
        ghost = window.canvas.preview_events[0]
        assert ghost.start == target and ghost.end - ghost.start == DURATION
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=point)
        assert {event.start for event in instances(dialog.calendar_occurrence_master())} == {START, target}
        window.set_view('month')
        other = date(2026, 11, 12)
        QTest.mouseClick(window.month, Qt.MouseButton.LeftButton, pos=month_point(window, other))
        assert store.list_events() == [] and store.history() == []
        state['expected'] = {START, target, START.replace(day=12)}
        assert window.finish_occurrences_button.isVisible()
        QTest.mouseClick(window.finish_occurrences_button, Qt.MouseButton.LeftButton)

    def save(state):
        dialog = state['dialog']
        assert not dialog.isMinimized()
        assert dialog.title.text() == 'A calendar-authored draft'
        assert {event.start for event in instances(dialog.calendar_occurrence_master())} == state['expected']
        assert all(not surface.occurrence_picking_active for surface in window.day_surfaces)
        dialog.save()

    state = play_editor(window, [('dialog', begin), ('calendar', add_dates), ('dialog', save)])
    app.processEvents()
    assert len(store.list_events()) == len(store.history()) == 1
    assert {event.start for event in instances(store.list_events()[0])} == state['expected']
    window.undo()
    assert store.list_events() == []


def test_reentering_calendar_mode_keeps_edits_and_cancel_discards_every_occurrence(workspace):
    _, window, store = workspace

    def first_pass(state):
        state['dialog'].location.setText('Room 4')
        state['dialog'].description.setPlainText('Keep this form state across calendar visits.')
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, date(2026, 11, 5), 13))
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_Escape)

    def resume(state):
        dialog = state['dialog']
        assert dialog.location.text() == 'Room 4'
        assert dialog.description.toPlainText() == 'Keep this form state across calendar visits.'
        assert len(instances(dialog.calendar_occurrence_master())) == 2
        QTest.mouseClick(dialog.add_occurrences_button, Qt.MouseButton.LeftButton)

    def second_pass(state):
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 2
        window.navigate(1)
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, date(2026, 11, 12), 11))
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 3
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_Return, CTRL)

    play_editor(window, [('dialog', begin), ('calendar', first_pass), ('dialog', resume),
                         ('calendar', second_pass), ('dialog', lambda state: state['dialog'].reject())])
    assert store.list_events() == [] and store.history() == []
    assert not any(surface.preview_events for surface in window.event_surfaces)


def test_draft_occurrence_moves_and_resizes_without_changing_prototype_or_store(workspace):
    app, window, store = workspace
    added = datetime(2026, 11, 5, 13, tzinfo=ZONE)
    moved = added + timedelta(days=1, hours=1)

    def change_range(state):
        dialog = state['dialog']
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, added.date(), 13))
        app.processEvents()
        rect = next(rect for rect, event in window.canvas.rects() if event.start == added)
        start = rect.center().toPoint()
        day_width = (window.canvas.width() - window.canvas.gutter) / len(window.canvas.days)
        finish = start + QPoint(round(day_width), round(window.canvas.hour_height))
        QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.canvas, finish)
        QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=finish)
        app.processEvents()
        values = instances(dialog.calendar_occurrence_master())
        assert {event.start for event in values} == {START, moved}
        changed = next(event for event in values if event.start == moved)
        assert changed.end - changed.start == DURATION
        rect = next(rect for rect, event in window.canvas.rects() if event.start == moved)
        start = QPoint(round(rect.center().x()), round(rect.bottom() - 2))
        finish = start + QPoint(0, round(window.canvas.hour_height))
        QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.canvas, finish)
        QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=finish)
        changed = next(event for event in instances(dialog.calendar_occurrence_master()) if event.start == moved)
        assert changed.end - changed.start == DURATION + timedelta(hours=1)
        assert dialog.start.value() == START and dialog.end.value() == START + DURATION
        assert store.list_events() == [] and store.history() == []
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', change_range),
                         ('dialog', lambda state: state['dialog'].save())])
    values = instances(store.list_events()[0])
    assert next(event for event in values if event.start == moved).end - moved == DURATION + timedelta(hours=1)


def test_existing_events_and_history_are_protected_during_calendar_authoring(workspace):
    _, window, store = workspace
    source = Event('existing', 'local', 'Existing choice', START + timedelta(hours=2),
                   START + timedelta(hours=3), candidate=True, group_id='choices')
    other = Event('other', 'local', 'Other choice', START + timedelta(days=1, hours=2),
                  START + timedelta(days=1, hours=3), candidate=True, group_id='choices')
    store.apply('Existing calendar entries', [source, other])
    window.refresh()

    def try_mutations(state):
        original = store.list_events()
        with patch.object(window, 'exec_event_dialog', side_effect=AssertionError('Opened a nested event editor')), \
             patch.object(QMenu, 'exec', side_effect=AssertionError('Opened an event context menu')):
            window.edit_event(source.id)
            window.delete_event(source.id)
            window.confirm_candidate(source.id)
            window.move_event(source.id, source.start + timedelta(days=2), source.end + timedelta(days=2))
            window.undo()
            window.redo()
            window.event_menu(source.id, QPoint())
            window.select_event(source.id)
            window.canvas.setFocus()
            QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
            QTest.keyClick(window.canvas, Qt.Key.Key_E)
            QTest.keyClick(window.canvas, Qt.Key.Key_Z, CTRL)
        assert store.list_events() == original and len(store.history()) == 1
        rect = next(rect for rect, event in window.canvas.rects() if event.id == source.id)
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 2
        assert store.list_events() == original
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', try_mutations),
                         ('dialog', lambda state: state['dialog'].reject())])
    assert store.list_events() == [source, other]
    assert len(store.history()) == 1


@pytest.mark.parametrize('view', ['month', 'year'])
def test_date_only_targets_preserve_prototype_time_and_duration(workspace, view):
    _, window, store = workspace
    target = date(2027, 2, 16)

    def add_from_overview(state):
        window.set_view(view)
        window.go_date(target)
        widget = window.month if view == 'month' else window.year
        point = month_point(window, target) if view == 'month' else widget.day_rect(target).center().toPoint()
        expected = START.replace(year=target.year, month=target.month, day=target.day)
        QTest.mouseMove(widget, point)
        wait_for(lambda: widget.preview_events and widget.preview_events[0].start == expected)
        assert widget.preview_events[0].start == expected
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
        added = next(event for event in instances(state['dialog'].calendar_occurrence_master()) if event.start == expected)
        assert added.end - added.start == DURATION
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', add_from_overview),
                         ('dialog', lambda state: state['dialog'].reject())])
    assert store.list_events() == []


def test_all_day_prototype_stays_all_day_when_adding_in_timed_area(workspace):
    _, window, store = workspace
    primary = START.replace(hour=0)
    duration = timedelta(days=2)
    target = primary + timedelta(days=2)

    def add_all_day(state):
        QTest.mouseMove(window.canvas, timed_point(window, target.date(), 14))
        wait_for(lambda: window.canvas.preview_events and window.canvas.preview_events[0].start == target)
        preview = window.canvas.preview_events[0]
        assert preview.all_day and preview.start == target and preview.end - preview.start == duration
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, target.date(), 14))
        added = next(event for event in instances(state['dialog'].calendar_occurrence_master()) if event.start == target)
        assert added.all_day and added.end - added.start == duration
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', add_all_day),
                         ('dialog', lambda state: state['dialog'].save())],
                opener=lambda: window.create_event(primary, primary + duration, all_day=True))
    assert len(store.list_events()) == 1
    assert all(event.all_day and event.end - event.start == duration for event in instances(store.list_events()[0]))


@pytest.mark.parametrize('scope', ['group', 'future'])
def test_adding_to_existing_series_only_changes_the_saved_editor_scope(workspace, scope):
    _, window, store = workspace
    source = Event('series', 'local', 'Existing series', START, START + DURATION,
                   recurrence_rule='FREQ=WEEKLY;COUNT=3')
    store.apply('Existing series', [source])
    original = instances(source)
    reference = original[1]
    window.set_view('month')
    window.select_event(reference.id)

    def add_to_scope(state):
        initial = instances(state['dialog'].calendar_occurrence_master())
        assert len(initial) == (3 if scope == 'group' else 2)
        target = START + timedelta(days=22, hours=2)
        assert state['dialog'].add_calendar_occurrence(target)
        assert store.list_events() == [source]
        assert len(store.history()) == 1
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', add_to_scope),
                         ('dialog', lambda state: state['dialog'].save())],
                opener=lambda: window.edit_event(reference.id, scope=scope))
    actual = store.query_occurrences(START - timedelta(days=1), START + timedelta(days=40))
    assert {event.start for event in actual} == {event.start for event in original} | {START + timedelta(days=22, hours=2)}
    assert len(store.history()) == 2
    window.undo()
    assert store.list_events() == [source]


@pytest.mark.parametrize('action,shortcut,key,modifiers', [
    ('dialog_close', 'Ctrl+Backspace', Qt.Key.Key_Backspace, CTRL),
    ('dialog_accept', 'Ctrl+Shift+Return', Qt.Key.Key_Return, CTRL | Qt.KeyboardModifier.ShiftModifier),
])
def test_finish_uses_the_configured_dialog_shortcuts(workspace, action, shortcut, key, modifiers):
    _, window, store = workspace
    window.bindings[action] = shortcut

    def finish_with_binding(_state):
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, key, modifiers)

    play_editor(window, [('dialog', begin), ('calendar', finish_with_binding),
                         ('dialog', lambda state: state['dialog'].reject())])
    assert store.list_events() == [] and store.history() == []


def test_restoring_the_minimized_editor_finishes_calendar_mode(workspace):
    _, window, store = workspace

    def restore_editor(state):
        dialog = state['dialog']
        assert dialog.isMinimized()
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, date(2026, 11, 5), 13))
        dialog.showNormal()
        dialog.raise_()
        dialog.activateWindow()

    def cancel(state):
        assert window.occurrence_picker is None
        assert not window.isMinimized()
        assert state['dialog'].windowFlags() == state['editor_flags']
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 2
        state['dialog'].reject()

    play_editor(window, [('dialog', begin), ('calendar', restore_editor), ('dialog', cancel)])
    assert store.list_events() == [] and store.history() == []


def test_closing_main_window_during_calendar_mode_discards_the_unsaved_draft(workspace):
    _, window, store = workspace

    def close_workspace(state):
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, date(2026, 11, 5), 13))
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 2
        window.close()

    play_editor(window, [('dialog', begin), ('calendar', close_workspace)])
    assert not window.isVisible()
    assert store.list_events() == [] and store.history() == []


def test_reusing_a_moved_occurrences_old_slot_adds_a_fresh_prototype_range(workspace):
    app, window, store = workspace
    added = START + timedelta(days=1, hours=4)
    moved = added + timedelta(days=1, hours=1)

    def move_and_reuse(state):
        dialog = state['dialog']
        assert dialog.add_calendar_occurrence(added, added + timedelta(hours=2))
        app.processEvents()
        rect = next(rect for rect, event in window.canvas.rects() if event.start == added)
        start = rect.center().toPoint()
        width = (window.canvas.width() - window.canvas.gutter) / len(window.canvas.days)
        finish = start + QPoint(round(width), round(window.canvas.hour_height))
        QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.canvas, finish)
        QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=finish)
        assert {event.start for event in instances(dialog.calendar_occurrence_master())} == {START, moved}
        point = timed_point(window, added.date(), 13)
        QTest.mouseMove(window.canvas, point)
        wait_for(lambda: window.canvas.preview_events and window.canvas.preview_events[0].start == added)
        preview = window.canvas.preview_events[0]
        assert preview.end - preview.start == DURATION
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=point)
        values = instances(dialog.calendar_occurrence_master())
        assert {event.start for event in values} == {START, added, moved}
        assert len({event.id for event in values}) == 3
        assert next(event for event in values if event.start == added).end - added == DURATION
        assert next(event for event in values if event.start == moved).end - moved == timedelta(hours=2)
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', move_and_reuse),
                         ('dialog', lambda state: state['dialog'].save())])
    assert len(instances(store.list_events()[0])) == 3


def test_timed_canvas_drag_does_not_change_an_all_day_prototypes_duration(workspace):
    _, window, store = workspace
    primary = START.replace(hour=0)
    duration = timedelta(days=2)
    target = primary + timedelta(days=2)

    def drag_timed_area(state):
        start = timed_point(window, target.date(), 13)
        finish = timed_point(window, target.date(), 15)
        QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.canvas, finish)
        QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=finish)
        values = instances(state['dialog'].calendar_occurrence_master())
        added = next(event for event in values if event.start == target)
        assert added.all_day and added.end == target + duration
        assert added.start.hour == added.end.hour == 0
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', drag_timed_area),
                         ('dialog', lambda state: state['dialog'].save())],
                opener=lambda: window.create_event(primary, primary + duration, all_day=True))
    assert len(instances(store.list_events()[0])) == 2


def test_nonexistent_dst_clock_has_no_preview_and_cannot_add_an_occurrence(workspace):
    _, window, store = workspace
    gap = datetime(2027, 3, 28, 2, 30, tzinfo=ZONE)

    def choose_gap(state):
        window.set_view('day')
        window.go_date(gap.date())
        if not window.full_day:
            window.toggle_full_day()
        window.scroll.verticalScrollBar().setValue(0)
        window.canvas.placement_moved.emit(gap)
        assert not window.canvas.preview_events
        QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                         pos=timed_point(window, gap.date(), 2.5))
        assert len(instances(state['dialog'].calendar_occurrence_master())) == 1
        assert any('does not exist' in issue.details for issue in window.local_issues)
        window.finish_occurrence_picking()

    play_editor(window, [('dialog', begin), ('calendar', choose_gap),
                         ('dialog', lambda state: state['dialog'].reject())])
    assert store.list_events() == []


def test_incoming_import_waits_for_both_calendar_mode_and_its_editor(workspace):
    _, window, store = workspace
    payload = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n'

    def queue_import(_state):
        assert window.import_data(payload)
        window._process_import_request()
        assert len(window._import_requests) == 1
        review.assert_not_called()
        window.finish_occurrence_picking()

    def cancel_editor(state):
        window._process_import_request()
        assert len(window._import_requests) == 1
        review.assert_not_called()
        state['dialog'].reject()

    with patch('debacal.ui.ImportDialog') as review:
        review.return_value.exec.return_value = QDialog.DialogCode.Rejected
        play_editor(window, [('dialog', begin), ('calendar', queue_import), ('dialog', cancel_editor)])
        window._process_import_request()
        review.assert_called_once()
    assert not window._import_requests and not window._import_memory_bytes
    assert store.list_events() == []
