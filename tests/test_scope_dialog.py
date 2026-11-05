"""Scope changes preserve editor state and preview the same future as storage."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from lebacal.dialogs import EventDialog, ShortcutsDialog
from lebacal.models import Event
from lebacal.recurrence import expand_series, recurrence_key
from lebacal.store import Store

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
BINDINGS = {'edit_event': 'E', 'copy_event': 'C', 'paste_event': 'V', 'duplicate_event': 'D',
            'delete_event': 'Del', 'day_view': '1', 'cycle_scope': 'Alt+S', 'undo': 'Ctrl+Z'}


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    widgets = []
    with patch('lebacal.dialogs.local_zone', return_value=ZONE):
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


def test_alt_scope_shortcut_works_inside_text_and_ctrl_copy_paste_stays_native(editors):
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
    assert dialog.scope.currentData() == 'group'
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier)
    assert dialog.scope.currentData() == 'future'


def test_scope_shortcut_is_configurable_and_one_off_does_not_cycle(editors):
    _, _, _, create = editors
    one = create()
    one.cycle_scope()
    assert one.scope.currentData() == 'single' and not one.scope_shortcut.isEnabled()
    master = sample()
    dialog = create(master, occurrences(master)[1], scope_shortcut='Alt+X',
                    scope_modifiers={'all': 'Meta', 'following': 'Alt'})
    assert dialog.scope_shortcut.key().toString() == 'Alt+X'
    assert 'Meta' in dialog.scope.itemText(dialog.scope.findData('group'))
    assert 'Alt' in dialog.scope.itemText(dialog.scope.findData('future'))


@pytest.mark.parametrize(('other', 'message'), [('Ctrl+E', 'unique'), ('Shift+C', 'unique'),
                                              ('Ctrl+Shift+D', 'unique'), ('Ctrl+E, X', 'start')])
def test_shortcut_editor_checks_generated_scope_collisions(editors, other, message):
    _, _, widgets, _ = editors
    dialog = ShortcutsDialog(BINDINGS | {'other': other}, BINDINGS | {'other': other})
    widgets.append(dialog)
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert message in dialog.error.text()


def test_shortcut_editor_saves_scope_preferences_and_rejects_ambiguity(editors):
    _, _, widgets, _ = editors
    dialog = ShortcutsDialog(BINDINGS, BINDINGS)
    widgets.append(dialog)
    dialog.scope_modifiers['all'].setCurrentText('Alt')
    dialog.scope_modifiers['following'].setCurrentText('Alt')
    dialog.save()
    assert 'different' in dialog.error.text()
    dialog.scope_modifiers['following'].setCurrentText('Meta')
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_scope_modifiers == {'all': 'Alt', 'following': 'Meta'}
    assert dialog.result_bindings == BINDINGS
    dialog.restore_defaults(BINDINGS)
    assert {key: field.currentText() for key, field in dialog.scope_modifiers.items()} == {'all': 'Ctrl', 'following': 'Shift'}
