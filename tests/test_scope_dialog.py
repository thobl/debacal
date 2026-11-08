"""Scope changes preserve editor state and preview the same future as storage."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.dialogs import EventDialog
from debacal.event_scope import DEFAULT_MODE_BINDINGS
from debacal.shortcuts import KeyboardShortcutsEditor
from debacal.models import Event
from debacal.recurrence import expand_series, recurrence_key
from debacal.store import Store

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
BINDINGS = DEFAULT_MODE_BINDINGS | {'edit_event': 'E', 'copy_event': 'Ctrl+C', 'paste_event': 'Ctrl+V',
            'delete_event': 'Del', 'day_view': 'D', 'undo': 'Ctrl+Z'}


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    widgets = []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        def create(master=None, selected=None, **kwargs):
            dialog = EventDialog(store.list_calendars(), selected.start if selected else START,
                                 event=selected, series=master, **kwargs)
            widgets.append(dialog)
            return dialog
        yield app, store, widgets, create
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    store.close()


def sample():
    return Event('series', 'local', 'Original', START, START + timedelta(hours=1),
                 recurrence_rule='FREQ=DAILY;COUNT=5', timezone_id=ZONE.key)


def occurrences(master):
    return expand_series(master, START - timedelta(days=10), START + timedelta(days=40))


def test_default_is_single_and_future_preview_does_not_restart_full_count(editors):
    _, _, _, create = editors
    master = sample()
    selected = occurrences(master)[2]
    dialog = create(master, selected)
    assert dialog.scope.currentData() == 'single'
    assert dialog.edit_reference == selected
    assert not dialog.repeat.isEnabled()
    assert dialog.build_events()[0].start == selected.start
    assert not dialog.build_events()[0].recurring
    dialog.scope.setCurrentIndex(dialog.scope.findData('future'))
    assert dialog.edit_reference == selected
    assert dialog.repeat.isEnabled()
    assert dialog.repeat_count.value() == 3
    assert [event.start for event in occurrences(dialog.preview.masters[0])] == [event.start for event in occurrences(master)[2:]]
    assert dialog.error.text() == ''


def test_scope_changes_cache_general_fields_and_recurrence_controls(editors):
    _, _, _, create = editors
    master = sample()
    selected = occurrences(master)[2]
    dialog = create(master, selected)
    dialog.title.setText('Single draft')
    dialog.scope.setCurrentIndex(dialog.scope.findData('group'))
    assert dialog.edit_reference == master and dialog.value(dialog.start) == master.start
    dialog.title.setText('All draft')
    dialog.repeat_count.setValue(8)
    dialog.scope.setCurrentIndex(dialog.scope.findData('future'))
    assert dialog.title.text() == selected.title
    assert dialog.value(dialog.start) == selected.start
    dialog.title.setText('Future draft')
    dialog.repeat_count.setValue(4)
    for scope, title, count in [('single', 'Single draft', 5), ('group', 'All draft', 8), ('future', 'Future draft', 4)]:
        dialog.scope.setCurrentIndex(dialog.scope.findData(scope))
        assert dialog.title.text() == title
        assert dialog.repeat_count.value() == count


def test_future_custom_schedule_fields_and_preview_match_store_split(editors):
    _, store, _, create = editors
    dates = [START + timedelta(days=day) for day in (0, 2, 5, 9)]
    master = replace(sample(), recurrence_rule='', recurrence_dates=tuple(recurrence_key(value) for value in dates[1:]),
                     schedule_expression='+2d +3d +4d', expression_anchor=START.date().isoformat(), schedule_syntax=2)
    store.apply('Create', [master])
    selected = occurrences(master)[1]
    dialog = create(master, selected, edit_scope='future')
    assert dialog.value(dialog.start) == selected.start
    assert dialog.sequence_starts() == dates[1:]
    dialog.title.setText('Remaining')
    edited = dialog.build_events()[0]
    preview = dialog.preview.masters[0]
    store.edit_future(selected, edited, replace_schedule=dialog.schedule_replacement, expected_master=master)
    future = next(value for value in store.list_events() if value.id != master.id)
    assert [value.start for value in occurrences(preview)] == [value.start for value in occurrences(future)] == dates[1:]


def test_future_weekly_off_rule_rdate_anchor_is_not_shifted_by_title_edit(editors):
    _, store, _, create = editors
    master = replace(sample(), recurrence_rule='FREQ=WEEKLY;BYDAY=WE;COUNT=5',
                     recurrence_dates=(recurrence_key(START + timedelta(days=2)),))
    store.apply('Create', [master])
    selected = occurrences(master)[1]
    dialog = create(master, selected, edit_scope='future')
    dialog.title.setText('Remaining Fridays and Wednesdays')
    edited = dialog.build_events()[0]
    assert edited.start == selected.start
    preview = dialog.preview.masters[0]
    expected = [value.start for value in occurrences(master)[1:]]
    assert [value.start for value in occurrences(preview)] == expected
    store.edit_future(selected, edited, expected_master=master)
    future = next(value for value in store.list_events() if value.id != master.id)
    assert [value.start for value in occurrences(future)] == expected


def test_future_off_rule_rdate_is_pinned_and_uses_primary_picker_after_a_move(editors):
    _, _, _, create = editors
    master = replace(sample(), recurrence_rule='FREQ=WEEKLY;BYDAY=WE;COUNT=5',
                     recurrence_dates=(recurrence_key(START + timedelta(days=2)),))
    selected = occurrences(master)[1]
    dialog = create(master, selected, edit_scope='future')
    projected = occurrences(dialog.preview.masters[0])
    pinned = [value for value in projected if dialog.preview.is_primary(value)]
    assert len(pinned) == 1 and pinned[0].recurrence_id == selected.recurrence_id
    dialog.preview.first_button.click()
    assert dialog.preview.selected_event.start == selected.start
    dialog.edit_occurrence(pinned[0])
    picker = dialog.range_picker
    assert picker.endpoint == 'start' and picker.windowTitle() != 'Edit occurrence'
    moved_start = selected.start + timedelta(hours=2)
    picker.set_start(moved_start)
    picker.accept()
    assert dialog.value(dialog.start) == moved_start
    projected = occurrences(dialog.preview.masters[0])
    pinned = [value for value in projected if dialog.preview.is_primary(value)]
    assert len(pinned) == 1 and pinned[0].start == moved_start


def test_future_moved_detached_occurrence_uses_nominal_cutoff_and_actual_fields(editors):
    _, store, _, create = editors
    master = sample()
    store.apply('Create', [master])
    selected = occurrences(master)[2]
    moved = replace(selected, start=selected.start + timedelta(days=10), end=selected.end + timedelta(days=10))
    store.edit_occurrence(selected, moved)
    master = store.get_event(master.id)
    selected = next(value for value in occurrences(master) if value.recurrence_id == selected.recurrence_id)
    dialog = create(master, selected, edit_scope='future')
    assert dialog.value(dialog.start) == moved.start
    dialog.title.setText('Remaining, with moved exception')
    edited = dialog.build_events()[0]
    preview = dialog.preview.masters[0]
    expected = sorted([START + timedelta(days=3), START + timedelta(days=4), moved.start])
    assert [value.start for value in occurrences(preview)] == expected
    store.edit_future(selected, edited, expected_master=master)
    future = next(value for value in store.list_events() if value.id != master.id)
    assert [value.start for value in occurrences(future)] == expected


@pytest.mark.parametrize(('displacement', 'duration'), [(timedelta(days=10), timedelta(hours=1)),
                                                     (timedelta(), timedelta(hours=3))])
def test_future_graphical_addition_from_moved_exception_keeps_existing_tail_dates(editors, displacement, duration):
    _, store, _, create = editors
    master = sample()
    store.apply('Create', [master])
    nominal = occurrences(master)[2]
    moved_start = nominal.start + displacement
    moved = replace(nominal, start=moved_start, end=moved_start + duration)
    store.edit_occurrence(nominal, moved)
    master = store.get_event(master.id)
    selected = next(value for value in occurrences(master) if value.recurrence_id == nominal.recurrence_id)
    dialog = create(master, selected, edit_scope='future')
    added = START + timedelta(days=20)
    expected = sorted([START + timedelta(days=3), START + timedelta(days=4), moved.start, added])
    dialog.toggle_date(added.date())
    assert not dialog.error.text()
    previewed = occurrences(dialog.preview.masters[0])
    assert [value.start for value in previewed] == expected
    assert all(value.end - value.start == (duration if value.start == moved.start else timedelta(hours=1)) for value in previewed)
    assert dialog.value(dialog.start) == moved.start
    assert dialog.value(dialog.end) == moved.end
    assert [value.start for value in previewed if dialog.preview.is_primary(value)] == [moved.start]
    edited = dialog.build_events()[0]
    store.edit_future(selected, edited, replace_schedule=dialog.schedule_replacement, expected_master=master)
    future = next(value for value in store.list_events() if value.id != master.id)
    persisted = occurrences(future)
    assert [(value.start, value.end) for value in persisted] == [(value.start, value.end) for value in previewed]
    assert [value.start for value in occurrences(store.get_event(master.id))] == [START, START + timedelta(days=1)]


def test_unsupported_future_partition_retains_previous_scope_and_draft(editors):
    _, _, _, create = editors
    master = replace(sample(), recurrence_rule='FREQ=YEARLY;BYWEEKNO=1;BYDAY=WE;COUNT=3')
    selected = expand_series(master, START, START + timedelta(days=800))[1]
    dialog = create(master, selected)
    dialog.title.setText('Still editing this occurrence')
    dialog.scope.setCurrentIndex(dialog.scope.findData('future'))
    assert dialog.scope.currentData() == 'single'
    assert dialog.title.text() == 'Still editing this occurrence'
    assert 'week-number' in dialog.error.text()


def test_function_mode_keys_work_inside_text_and_ctrl_copy_paste_stays_native(editors):
    app, _, _, create = editors
    master = sample()
    dialog = create(master, occurrences(master)[1])
    dialog.show()
    dialog.activateWindow()
    dialog.title.setFocus()
    app.processEvents()
    dialog.title.setText('Copy this text')
    dialog.title.selectAll()
    QTest.keyClick(dialog.title, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == 'Copy this text'
    assert dialog.scope.currentData() == 'single'
    dialog.title.clear()
    QTest.keyClick(dialog.title, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert dialog.title.text() == 'Copy this text'
    QTest.keyClick(dialog.title, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier)
    assert dialog.scope.currentData() == 'single'
    QTest.keyClick(dialog.title, Qt.Key.Key_F2)
    assert dialog.scope.currentData() == 'group'
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_F3)
    assert dialog.scope.currentData() == 'future'


def test_only_explicit_mode_shortcuts_are_available_and_remappable(editors):
    _, _, _, create = editors
    one = create()
    assert one.scope.currentData() == 'single'
    assert not hasattr(one, 'cycle_scope') and not hasattr(one, 'scope_shortcut')
    assert all(not shortcut.isEnabled() for shortcut in one.mode_shortcuts.values())
    master = sample()
    dialog = create(master, occurrences(master)[1], scope_bindings={'scope_all': 'Alt+A'})
    assert dialog.mode_shortcuts['scope_all'].key().toString() == 'Alt+A'
    assert 'Alt+A' in dialog.scope.buttons['group'].toolTip()
    assert 'F3' in dialog.scope.buttons['future'].toolTip()


@pytest.mark.parametrize(('other', 'message'), [('E', 'unique'), ('Ctrl+C', 'unique'),
                                              ('F1', 'unique'), ('Ctrl+C, X', 'start')])
def test_shortcut_editor_checks_duplicates_and_chord_prefixes(editors, other, message):
    _, _, widgets, _ = editors
    editor = KeyboardShortcutsEditor(BINDINGS | {'other': other}, BINDINGS | {'other': other})
    widgets.append(editor)
    with pytest.raises(ValueError, match=message):
        editor.value()


def test_shortcut_editor_restores_mode_defaults(editors):
    _, _, widgets, _ = editors
    editor = KeyboardShortcutsEditor(BINDINGS, BINDINGS)
    widgets.append(editor)
    editor.fields['scope_all'].setKeySequence('Alt+A')
    assert editor.value()['scope_all'] == 'Alt+A'
    editor.restore_defaults()
    assert editor.value() == BINDINGS
