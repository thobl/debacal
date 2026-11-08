"""Sequence text, saved recurrence, and gestures agree across edit paths."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_events, occurrence
from debacal.schedule_authoring import expression_event, expression_matches
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 10, tzinfo=ZONE)
TEXT = '  2*(+1 +2d)  '
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier


def settle(app):
    for _ in range(4):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    errors, dialogs = [], []
    with Store(':memory:') as store, \
         patch('debacal.ui.now', return_value=START), \
         patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        Preferences(start_weekday=0, date_format='european', locale_name='en_GB').save(store)
        store.save_calendar(Calendar('target', 'Other calendar', '#b6815c'))
        window = MainWindow(store, enable_sync=False)
        window.resize(1500, 1000)
        window.go_date(START.date())
        window.show()
        window.activateWindow()
        settle(app)
        yield app, window, store, dialogs
        QApplication.clipboard().clear()
        for dialog in reversed(dialogs):
            if dialog.range_picker:
                dialog.range_picker.close()
            dialog.close()
            dialog.deleteLater()
        window.close()
        window.executor.shutdown(wait=True)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def source(text=TEXT, *, all_day=False):
    start = START.replace(hour=0) if all_day else START
    base = Event('series', 'local', 'A sequence', start,
                 start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                 all_day=all_day, timezone_id=ZONE.key, schedule_expression=text,
                 expression_anchor=start.date().isoformat(), schedule_syntax=2)
    return expression_event(base, text)


def starts(values):
    return [item.start.astimezone(timezone.utc) for item in values]


def visible(masters):
    return expand_events(masters, START - timedelta(days=10), START + timedelta(days=100))


def put(desktop, master, *, index=0, scope='group', surface='week'):
    app, window, store, _ = desktop
    store.apply('Create sequence', [master])
    values = visible([master])
    selected = values[index]
    window.go_date(selected.start.date())
    window.set_view('month' if surface == 'month' else 'week')
    window.set_selection_mode(scope)
    window.select_event(selected.id)
    settle(app)
    return selected


def drag(desktop, selected, *, surface='week', copy=False, days=1, minutes=0, resize=False, before_release=None):
    app, window, _, _ = desktop
    canvas = window.month if surface == 'month' else window.all_day_canvas if surface == 'all_day' else window.canvas
    rects = canvas.month_layout()[1] if surface == 'month' else canvas.rects()
    rect = next(rect for rect, item in rects if item.id == selected.id)
    begin = QPoint(round(rect.center().x()), round(rect.bottom() - 2)) if resize else rect.center().toPoint()
    width = (canvas.width() - canvas.week_gutter) / 7 if surface == 'month' else (canvas.width() - canvas.gutter) / len(canvas.days)
    finish = begin + (QPoint(0, canvas.hour_height) if resize else
                      QPoint(round(days * width), round(minutes / 60 * getattr(canvas, 'hour_height', 0))))
    modifiers = CTRL if copy else NONE
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=begin)
    movement = QMouseEvent(QEvent.Type.MouseMove, QPointF(finish), QPointF(canvas.mapToGlobal(finish)),
                          Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, modifiers)
    QApplication.sendEvent(canvas, movement)
    if before_release is not None:
        before_release(canvas)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, modifiers, finish)
    settle(app)


def open_editor(desktop, master, *, selected=None, scope='group'):
    _, _, store, dialogs = desktop
    selected = selected or occurrence(master, master.start)
    dialog = EventDialog(store.list_calendars(), selected.start, selected.end,
                         event=selected, series=master, edit_scope=scope,
                         preferences=Preferences.load(store))
    dialogs.append(dialog)
    return dialog


@pytest.mark.parametrize('surface,text', [('week', TEXT), ('month', TEXT), ('all_day', TEXT),
                                         ('week', '  11:00 +1 12:00  '), ('month', '  11:00 +1 12:00  ')])
@pytest.mark.parametrize('operation', ['drag', 'dialog', 'clipboard', 'copy-drag'])
def test_rigid_translation_preserves_equivalent_definition_through_each_creation_path(desktop, surface, operation, text):
    _, window, store, _ = desktop
    master = source(text, all_day=surface == 'all_day')
    selected = put(desktop, master, surface=surface)
    expected = [value.start + timedelta(days=1) for value in visible([master])]
    if operation in ('drag', 'copy-drag'):
        drag(desktop, selected, surface=surface, copy=operation == 'copy-drag')
    elif operation == 'clipboard':
        window.copy_event()
        window.paste_events(target=selected.start + timedelta(days=1))
        assert store.list_events() == [master]
        assert window.pending_paste is not None
        window.confirm_paste()
    else:
        def accept(dialog):
            dialog.start.set_value(dialog.start.value() + timedelta(days=1))
            dialog.save()
            assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
            return dialog.result()
        with patch.object(EventDialog, 'exec', accept):
            window.edit_event()
    assert not window.local_issues
    copying = operation in ('clipboard', 'copy-drag')
    changed = next(item for item in store.list_events() if item.id != master.id) if copying else store.get_event(master.id)
    assert changed.schedule_expression == text
    assert expression_matches(changed, text)
    assert starts(visible([changed])) == [value.astimezone(timezone.utc) for value in expected]
    if copying:
        assert store.get_event(master.id) == master and changed.ical_uid != master.ical_uid
    assert len(store.history()) == 2
    restored, = parse_ics(export_ics([changed]), 'local')
    assert expression_matches(restored, text)
    assert starts(visible([restored])) == starts(visible([changed]))
    reopened = open_editor(desktop, restored)
    assert reopened.pattern.text() == text
    after = store.list_events()
    window.undo()
    assert store.list_events() == [master]
    window.redo()
    assert store.list_events() == after


@pytest.mark.parametrize('annotated', [False, True])
def test_daily_following_drag_split_reports_three_in_every_representation(desktop, annotated):
    _, window, store, _ = desktop
    master = source('every(1d)')
    if not annotated:
        master = replace(master, schedule_expression='', expression_anchor='')
    selected = put(desktop, master, index=3, scope='future')
    drag(desktop, selected)
    assert not window.local_issues
    before = store.get_event(master.id)
    past = visible([before])
    assert len(past) == 3
    assert [item.start for item in past] == [START + timedelta(days=index) for index in range(3)]
    dialog = open_editor(desktop, before)
    assert dialog.repeat.currentData() == 'DAILY'
    assert dialog.repeat_count.value() == 3
    assert dialog.repeat_until.value().date() == (START + timedelta(days=2)).date()
    assert len(visible(dialog.preview.masters)) == 3
    dialog.repeat.setCurrentData('RDATE')
    assert expression_matches(before, dialog.pattern.text())
    # Moving the tail again changes its location, never the bounded past.
    tail = next(item for item in store.list_events() if item.id != before.id)
    tail_first = visible([tail])[0]
    window.set_selection_mode('group')
    window.select_event(tail_first.id)
    window.move_event(tail_first.id, tail_first.start + timedelta(hours=1), tail_first.end + timedelta(hours=1))
    assert store.get_event(before.id) == before
    assert len(store.history()) == 3
    window.undo()
    window.undo()
    assert store.list_events() == [master]


def test_translating_a_calendar_date_pattern_regenerates_a_pattern_instead_of_full_dates(desktop):
    _, window, store, _ = desktop
    master = source('4*1.')
    selected = put(desktop, master)
    expected = [item.start + timedelta(days=1) for item in visible([master])]
    drag(desktop, selected)
    moved = store.get_event(master.id)
    assert moved.schedule_expression == '4*2.'
    assert expression_matches(moved, moved.schedule_expression)
    assert [item.start for item in visible([moved])] == expected
    window.undo()
    assert store.list_events() == [master]


@pytest.mark.parametrize('operation', ['drag', 'dialog'])
def test_future_moves_have_the_same_cutoff_and_count_then_survive_ics(desktop, operation):
    _, window, store, _ = desktop
    master = source('every(1d)')
    selected = put(desktop, master, index=3, scope='future')
    if operation == 'drag':
        drag(desktop, selected)
    else:
        def accept(dialog):
            dialog.start.set_value(dialog.start.value() + timedelta(days=1))
            dialog.save()
            assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
            return dialog.result()
        with patch.object(EventDialog, 'exec', accept):
            window.edit_event()
    assert not window.local_issues
    masters = store.list_events()
    assert len(masters) == 2
    before = store.get_event(master.id)
    tail = next(item for item in masters if item.id != master.id)
    assert [item.start for item in visible([before])] == [START + timedelta(days=index) for index in range(3)]
    assert tail.start == START + timedelta(days=4)
    assert expression_matches(tail, tail.schedule_expression)
    roundtrip = parse_ics(export_ics(masters), 'local')
    assert starts(visible(roundtrip)) == starts(visible(masters))
    for item in roundtrip:
        dialog = open_editor(desktop, item)
        assert len(visible(dialog.preview.masters)) == len(visible([item]))
        assert expression_matches(item, dialog.pattern.text())
    window.undo()
    assert store.list_events() == [master]


def test_resize_then_copy_delete_and_undo_keep_sequence_positions_and_durations(desktop):
    _, window, store, _ = desktop
    master = source()
    selected = put(desktop, master)
    drag(desktop, selected, resize=True)
    resized = store.get_event(master.id)
    assert resized.schedule_expression == TEXT and expression_matches(resized, TEXT)
    assert all(item.end - item.start == timedelta(hours=2) for item in visible([resized]))
    window.copy_event()
    window.paste_events(target=selected.start + timedelta(weeks=4))
    window.confirm_paste()
    copied = next(item for item in store.list_events() if item.id != master.id)
    assert copied.schedule_expression == TEXT and expression_matches(copied, TEXT)
    assert all(item.end - item.start == timedelta(hours=2) for item in visible([copied]))
    window.delete_event()
    assert store.list_events() == [resized]
    window.undo()
    assert store.get_event(copied.id) == copied
    window.undo()
    assert store.list_events() == [resized]
    window.undo()
    assert store.list_events() == [master]


@pytest.mark.parametrize('text', ['every(1d, count=7)', 'every(1d, until=8.11.2026)'])
def test_typed_bound_regular_controls_following_drag_and_roundtrip_agree(desktop, text):
    _, window, store, dialogs = desktop
    draft = EventDialog(store.list_calendars(), START, START + timedelta(hours=1),
                        preferences=Preferences.load(store))
    dialogs.append(draft)
    draft.title.setText('A bounded daily sequence')
    draft.schedule_mode.setCurrentData('repeat')
    draft.repeat.setCurrentData('RDATE')
    draft.pattern.setText(text)
    assert not draft.error.text()
    assert draft.repeat.currentData() == 'DAILY'
    assert draft.repeat_count.value() == 7
    assert draft.repeat_until.value().date() == (START + timedelta(days=6)).date()
    assert len(visible(draft.preview.masters)) == 7
    draft.save()
    assert draft.result() == QDialog.DialogCode.Accepted, draft.error.text()
    master, = draft.result_events
    assert expression_matches(master, text)
    selected = put(desktop, master, index=3, scope='future')
    drag(desktop, selected)
    assert not window.local_issues
    values = store.list_events()
    assert len(values) == 2
    for item in values:
        actual = visible([item])
        editor = open_editor(desktop, item)
        assert editor.repeat.currentData() == 'DAILY'
        assert editor.repeat_count.value() == len(actual)
        assert editor.repeat_until.value().date() == actual[-1].start.date()
        assert len(visible(editor.preview.masters)) == len(actual)
        editor.repeat.setCurrentData('RDATE')
        assert expression_matches(item, editor.pattern.text())
    assert len(visible(values)) == 7
    assert starts(visible(parse_ics(export_ics(values), 'local'))) == starts(visible(values))
    window.undo()
    assert store.list_events() == [master]
    window.redo()
    assert store.list_events() == values


def test_individual_time_edit_then_group_move_and_calendar_transfer_preserve_actual_definition(desktop):
    _, window, store, _ = desktop
    master = source()
    selected = put(desktop, master, index=2, scope='single')
    drag(desktop, selected, days=0, minutes=120)
    individual = store.get_event(master.id)
    actual = store.get_occurrence(selected.id)
    assert actual.start == selected.start + timedelta(hours=2)
    window.set_selection_mode('group')
    drag(desktop, actual)
    together = store.get_event(master.id)
    assert together.start == master.start + timedelta(days=1)
    changed_occurrence = next(item for item in visible([together]) if item.start.hour == 12)
    assert changed_occurrence.start == actual.start + timedelta(days=1)

    def accept(dialog):
        assert '12:00' in dialog.pattern.text()
        assert expression_matches(dialog.preview.masters[0], dialog.pattern.text())
        dialog.pattern.setText(' ' + dialog.pattern.text() + ' ')
        dialog.description.setPlainText('An edit after moving one occurrence and then the series')
        dialog.calendar.setCurrentIndex(dialog.calendar.findData('target'))
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()

    with patch.object(EventDialog, 'exec', accept):
        window.edit_event()
    assert not window.local_issues
    moved, = store.list_events()
    assert moved.calendar_id == 'target' and moved.id != master.id
    assert expression_matches(moved, moved.schedule_expression)
    assert starts(visible([moved])) == starts(visible([together]))
    assert len(moved.recurrence_overrides) == 1
    assert all(item.description.startswith('An edit after moving') for item in visible([moved]))
    imported, = parse_ics(export_ics([moved]), 'target')
    assert expression_matches(imported, imported.schedule_expression)
    assert starts(visible([imported])) == starts(visible([moved]))
    window.undo()
    assert store.list_events() == [together]
    window.undo()
    assert store.list_events() == [individual]
    window.undo()
    assert store.list_events() == [master]


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('operation,surface', [('drag', 'week'), ('drag', 'month'), ('copy-drag', 'week'),
                                             ('dialog', 'week'), ('clipboard', 'week')])
def test_relative_month_program_reanchors_without_losing_its_logical_selection(desktop, index, operation, surface):
    app, window, store, _ = desktop
    start = datetime(2027, 1, 31, 10, tzinfo=ZONE)
    prototype = Event('month-series', 'local', 'Month arithmetic', start, start + timedelta(hours=1),
                      timezone_id=ZONE.key, schedule_expression='2*+1m',
                      expression_anchor=start.date().isoformat(), schedule_syntax=2)
    master = expression_event(prototype, prototype.schedule_expression)
    store.apply('Create month program', [master])
    beginning, ending = start - timedelta(days=1), start + timedelta(days=100)
    original = expand_events([master], beginning, ending)
    selected = original[index]
    window.go_date(selected.start.date())
    window.set_view('month' if surface == 'month' else 'week')
    window.set_selection_mode('group')
    window.select_event(selected.id)
    settle(app)
    expected = [start.replace(day=30), start.replace(month=2, day=28), start.replace(month=3, day=28)]
    preview = []
    if operation in ('drag', 'copy-drag'):
        bounds = window.dates()
        def capture(canvas):
            preview.extend(item.start for item in canvas.preview_events)
        drag(desktop, selected, surface=surface, days=-1, copy=operation == 'copy-drag', before_release=capture)
        expected_preview = [value for value in expected if bounds[0] <= value.date() <= bounds[-1]]
        assert sorted(preview) == expected_preview
    elif operation == 'clipboard':
        window.copy_event()
        window.paste_events(target=selected.start - timedelta(days=1))
        assert window.pending_paste is not None
        assert [item.start for item in expand_events(window.pending_paste.events, beginning, ending)] == expected
        window.confirm_paste()
    else:
        def accept(dialog):
            dialog.start.set_value(start.replace(day=30))
            assert [item.start for item in expand_events(dialog.preview.masters, beginning, ending)] == expected
            dialog.save()
            assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
            return dialog.result()
        with patch.object(EventDialog, 'exec', accept):
            window.edit_event()
    assert not window.local_issues
    copying = operation in ('clipboard', 'copy-drag')
    changed = next(item for item in store.list_events() if item.id != master.id) if copying else store.get_event(master.id)
    assert changed.schedule_expression == '2*+1m'
    assert expression_matches(changed, changed.schedule_expression)
    assert [item.start for item in expand_events([changed], beginning, ending)] == expected
    assert window.selection.primary.start == expected[index]
    assert window.selected_day == expected[index].date()
    assert window.selection.primary.series_id == changed.id
    window.undo()
    assert store.list_events() == [master]


def test_valid_relative_starts_with_a_generated_duration_end_in_a_dst_gap_can_be_saved(desktop):
    _, _, store, dialogs = desktop
    start = datetime(2027, 3, 27, 1, 30, tzinfo=ZONE)
    dialog = EventDialog(store.list_calendars(), start, start + timedelta(hours=1),
                         preferences=Preferences.load(store))
    dialogs.append(dialog)
    dialog.title.setText('A range across the clock change')
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('2*+1d')
    first, last = start - timedelta(days=1), start + timedelta(days=5)
    preview = expand_events(dialog.preview.masters, first, last)
    assert len(preview) == 3
    assert preview[1].start == start.replace(day=28)
    assert preview[1].end == start.replace(day=28, hour=3, minute=30)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    master, = dialog.result_events
    operation = store.apply('Create across clock change', [master])
    imported = parse_ics(export_ics([master]), 'local')
    assert [(item.start, item.end) for item in expand_events(imported, first, last)] == [
        (item.start, item.end) for item in preview]
    store.undo(operation)
    assert store.list_events() == []


@pytest.mark.parametrize('operation', ['drag', 'dialog', 'clipboard'])
def test_clipped_month_reanchor_selects_the_same_instruction_instead_of_a_nearer_sibling(desktop, operation):
    app, window, store, _ = desktop
    start = datetime(2027, 1, 31, 10, tzinfo=ZONE)
    prototype = Event('month-series', 'local', 'Clipped month then two days', start,
                      start + timedelta(hours=1), timezone_id=ZONE.key,
                      schedule_expression='+1m +2d', expression_anchor=start.date().isoformat(), schedule_syntax=2)
    master = expression_event(prototype, prototype.schedule_expression)
    beginning, ending = start - timedelta(days=1), start + timedelta(days=100)
    store.apply('Create month program', [master])
    selected = expand_events([master], beginning, ending)[2]
    window.go_date(selected.start.date())
    window.set_selection_mode('group')
    window.select_event(selected.id)
    settle(app)
    if operation == 'drag':
        drag(desktop, selected, days=-1)
    elif operation == 'clipboard':
        window.copy_event()
        window.paste_events(target=selected.start - timedelta(days=1))
        window.confirm_paste()
    else:
        def accept(dialog):
            dialog.start.set_value(start.replace(day=30))
            dialog.save()
            assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
            return dialog.result()
        with patch.object(EventDialog, 'exec', accept):
            window.edit_event()
    assert not window.local_issues
    assert window.selection.primary.start == start.replace(month=3, day=2)
    assert window.selected_day == start.replace(month=3, day=2).date()
    changed = store.get_event(window.selection.primary.series_id)
    assert changed.start == start.replace(day=30)
    assert changed.schedule_expression == '+1m +2d'


def test_month_reanchor_keeps_two_exception_payloads_when_linear_dates_would_collide(desktop):
    app, window, store, _ = desktop
    start = datetime(2027, 1, 30, 10, tzinfo=ZONE)
    prototype = Event('month-series', 'local', 'Month arithmetic', start, start + timedelta(hours=1),
                      timezone_id=ZONE.key, schedule_expression='+1m +1d',
                      expression_anchor=start.date().isoformat(), schedule_syntax=2)
    master = expression_event(prototype, prototype.schedule_expression)
    store.apply('Create month program', [master])
    beginning, ending = start - timedelta(days=1), start + timedelta(days=100)
    values = expand_events([master], beginning, ending)
    for index, title, hours in ((1, 'February exception', 2), (2, 'March exception', 3)):
        item = store.get_occurrence(values[index].id)
        store.edit_occurrence(item, replace(item, title=title, end=item.start + timedelta(hours=hours)))
    baseline = store.get_event(master.id)
    window.go_date(start.date())
    window.set_selection_mode('group')
    window.select_event(values[0].id)
    settle(app)
    drag(desktop, store.get_occurrence(values[0].id))
    assert not window.local_issues
    changed = store.get_event(master.id)
    assert changed.start == start.replace(day=31)
    assert changed.schedule_expression == '+1m +1d'
    actual = expand_events([changed], beginning, ending)
    assert [(item.start.month, item.start.day, item.title, item.end - item.start) for item in actual] == [
        (1, 31, 'Month arithmetic', timedelta(hours=1)),
        (2, 28, 'February exception', timedelta(hours=2)),
        (3, 1, 'March exception', timedelta(hours=3)),
    ]
    assert expression_matches(changed, changed.schedule_expression)
    window.undo()
    assert store.get_event(master.id) == baseline
