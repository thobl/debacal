"""The new sequence language drives preview, editing, storage and normal ICS."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 7, 9, tzinfo=ZONE)


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    widgets = []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), patch('debacal.ui.local_zone', return_value=ZONE):
        def create(start=START, **kwargs):
            dialog = EventDialog(store.list_calendars(), start, start + timedelta(minutes=90),
                                 preferences=Preferences(0, 'european', '24h'), **kwargs)
            widgets.append(dialog)
            dialog.title.setText('A sequence')
            return dialog
        yield store, create, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    store.close()


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    return dialog.result_events[0]


def occurrences(store, master):
    return store.query_occurrences(master.start - timedelta(days=60), master.start + timedelta(days=400))


def test_user_mixed_date_example_preserves_fixed_event_clock_and_duration(editor):
    store, create, _ = editor
    dialog = create()
    literal = '13. +1 18. 2. 3.2. +2'
    dialog.pattern.setText(literal)
    master = save(dialog)
    store.apply('Create sequence', [master])
    expected = [(2026, 11, 7), (2026, 11, 13), (2026, 11, 20), (2026, 12, 18),
                (2027, 1, 2), (2027, 2, 3), (2027, 2, 17)]
    values = occurrences(store, master)
    assert [value.start for value in values] == [datetime(*day, 9, tzinfo=ZONE) for day in expected]
    assert all(value.end - value.start == timedelta(minutes=90) for value in values)
    assert master.start == START and master.schedule_expression == literal and master.schedule_syntax == 2
    assert len(dialog.preview.events_on(START.date())) == 1
    returned = parse_ics(export_ics([master]), master.calendar_id)[0]
    assert returned.recurrence_dates == master.recurrence_dates
    assert returned.schedule_expression == literal and returned.schedule_syntax == 2


def test_time_only_sequence_keeps_multiple_daily_slots_and_edits_from_fixed_start(editor):
    store, create, _ = editor
    dialog = create()
    literal = '10:00 11:00 9:00 10:00 11:00'
    dialog.pattern.setText(literal)
    master = save(dialog)
    store.apply('Create daily choices', [master])
    values = occurrences(store, master)
    assert [(value.start.day, value.start.hour) for value in values] == [(7, 9), (7, 10), (7, 11), (8, 9), (8, 10), (8, 11)]
    assert all(value.end - value.start == timedelta(minutes=90) for value in values)
    assert len(dialog.preview.events_on(START.date())) == 3
    assert len(dialog.preview.events_on((START + timedelta(days=1)).date())) == 3
    assert not master.recurrence_rule and len(master.recurrence_dates) == 5
    selected = values[4]
    edit = create(event=selected, series=master, edit_scope='group')
    assert edit.pattern.text() == literal
    assert edit.value(edit.start) == START
    assert edit.edit_reference == master
    edit.scope.setCurrentIndex(edit.scope.findData('single'))
    assert edit.value(edit.start) == selected.start
    assert edit.edit_reference == selected
    edit.title.setText('Only this slot')
    changed = save(edit)
    store.edit_occurrence(selected, changed)
    assert [value.title for value in occurrences(store, master)].count('Only this slot') == 1
    store.undo()
    edit.scope.setCurrentIndex(edit.scope.findData('group'))
    assert edit.value(edit.start) == START
    assert edit.title.text() == 'A sequence'
    edit.pattern.setText('10:30 +1d 9:00')
    changed = save(edit)
    assert edit.schedule_replacement
    store.edit_series(edit.edit_reference, changed, replace_schedule=True)
    assert [(value.start.day, value.start.hour, value.start.minute) for value in occurrences(store, master)] == [
        (7, 9, 0), (7, 10, 30), (8, 10, 30), (9, 9, 0)]
    store.undo()
    assert store.get_event(master.id) == master


def test_relative_hour_minute_sequence_saves_syncs_and_reopens_without_rewriting(editor):
    store, create, _ = editor
    dialog = create()
    literal = '+2h +30min +1d +45min'
    dialog.pattern.setText(literal)
    expected = expand_sequence(literal, START)
    master = save(dialog)
    assert not master.recurrence_rule and len(master.recurrence_dates) == 4
    store.apply('Create varying times', [master])
    values = occurrences(store, master)
    assert [value.start.timestamp() for value in values] == [value.timestamp() for value in expected]
    assert all(value.end - value.start == timedelta(minutes=90) for value in values)
    returned = parse_ics(export_ics([master]), master.calendar_id)[0]
    assert returned.schedule_expression == literal and returned.schedule_syntax == 2
    reopen = create(event=returned, series=returned, edit_scope='group')
    assert reopen.pattern.text() == literal
    assert [value.timestamp() for value in reopen.sequence_starts()] == [value.timestamp() for value in expected]


@pytest.mark.parametrize('start, text, expected_rule', [
    (START, '4*+2', 'FREQ=WEEKLY;INTERVAL=2;COUNT=5'),
    (START, '4*+2h', 'FREQ=HOURLY;INTERVAL=2;COUNT=5'),
    (START, '4*+15min', 'FREQ=MINUTELY;INTERVAL=15;COUNT=5'),
    (datetime(2027, 1, 1, 9, tzinfo=ZONE), '4*1.', 'FREQ=MONTHLY;INTERVAL=1;COUNT=5'),
])
def test_regular_authored_sequences_use_standard_rules_and_reopen_as_text(editor, start, text, expected_rule):
    store, create, _ = editor
    dialog = create(start)
    dialog.pattern.setText(text)
    master = save(dialog)
    assert master.recurrence_rule == expected_rule
    assert not master.recurrence_dates
    store.apply('Create regular sequence', [master])
    values = occurrences(store, master)
    assert len(values) == 5
    selected = values[2]
    edit = create(event=selected, series=master, edit_scope='group')
    expected_kind = expected_rule.split(';', 1)[0].partition('=')[2]
    assert edit.repeat.currentData() == (expected_kind if expected_kind in ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY') else 'RDATE')
    assert edit.pattern.text() == text
    assert edit.value(edit.start) == master.start
    edit.title.setText('Rename regular sequence')
    store.edit_series(edit.edit_reference, save(edit))
    assert store.get_event(master.id).recurrence_rule == expected_rule
    assert store.get_event(master.id).schedule_expression == text


def test_actual_workspace_group_edit_uses_master_reference_from_later_occurrence(editor):
    store, create, widgets = editor
    dialog = create()
    dialog.pattern.setText('10:00 +1d 9:00')
    master = save(dialog)
    store.apply('Create sequence', [master])
    before = occurrences(store, master)
    window = MainWindow(store, enable_sync=False)
    widgets.append(window)
    def edit(dialog):
        assert dialog.value(dialog.start) == master.start
        dialog.start.setDateTime(dialog.start.dateTime().addSecs(3600))
        dialog.title.setText('Moved whole sequence')
        save(dialog)
        return dialog.result()
    with patch.object(EventDialog, 'exec', edit):
        window.edit_event(before[-1].id, group=True)
    moved = store.get_event(master.id)
    assert moved.start == START + timedelta(hours=1)
    assert moved.end - moved.start == master.end - master.start
    assert [value.start for value in occurrences(store, moved)] == [value.start + timedelta(hours=1) for value in before]
    reopen = create(event=occurrences(store, moved)[1], series=moved, edit_scope='group')
    assert reopen.sequence_starts() == [value.start for value in occurrences(store, moved)]


def test_all_day_clocks_are_rejected_and_generated_duration_resolves_dst_gap(editor):
    store, create, _ = editor
    dialog = create()
    dialog.all_day.setChecked(True)
    dialog.pattern.setText('2*+1d')
    master = save(dialog)
    store.apply('Create all-day sequence', [master])
    assert len(occurrences(store, master)) == 3
    assert all(value.all_day and value.end - value.start == timedelta(days=1) for value in occurrences(store, master))
    dialog.pattern.setText('10:00')
    dialog.save()
    assert not dialog.result_events and 'timed event' in dialog.error.text()
    gap = create(datetime(2027, 3, 27, 1, 30, tzinfo=ZONE))
    gap.end.setDateTime(gap.start.dateTime().addSecs(3600))
    gap.pattern.setText('+1d')  # A generated end follows standard timezone interpretation.
    gap.save()
    assert gap.result_events and not gap.error.text()
    store.apply('Create range crossing clock change', gap.result_events)
    values = occurrences(store, gap.result_events[0])
    assert [(value.start.hour, value.end.hour) for value in values] == [(1, 2), (1, 3)]
    assert len(gap.preview.visible_events) == 2


def test_unrepresentable_imported_precision_is_retained_on_ordinary_edits(editor):
    store, create, _ = editor
    initial = START.replace(second=30, microsecond=123456)
    second = START.replace(hour=10, second=59, microsecond=234567)
    master = Event('precise', 'local', 'Imported precise times', initial, initial + timedelta(minutes=90),
                   recurrence_dates=(second.isoformat(),))
    store.apply('Import precise schedule', [master])
    selected = occurrences(store, master)[1]
    dialog = create(event=selected, series=master, edit_scope='group')
    dialog.title.setText('Rename precise schedule')
    store.edit_series(dialog.edit_reference, save(dialog))
    assert [value.start for value in occurrences(store, master)] == [initial, second]


def test_stale_authoring_text_never_replaces_times_changed_by_another_client(editor):
    store, create, _ = editor
    first = START.replace(hour=11)
    second = (START + timedelta(days=1)).replace(hour=15)
    master = Event('external', 'local', 'Changed in another client', START, START + timedelta(minutes=90),
                   recurrence_dates=(first.isoformat(), second.isoformat()), schedule_syntax=2,
                   schedule_expression='11:00-7.11.2026 11:00-8.11.2026', expression_anchor=START.date().isoformat())
    store.apply_remote([master])
    selected = occurrences(store, master)[1]
    dialog = create(event=selected, series=master, edit_scope='group')
    assert dialog.sequence_starts() == [START, first, second]
    extra_day = START + timedelta(days=2)
    dialog.toggle_date(extra_day.date())
    store.edit_series(dialog.edit_reference, save(dialog), replace_schedule=dialog.schedule_replacement)
    assert [value.start for value in occurrences(store, master)] == [START, first, second, extra_day]


def test_legacy_authored_count_rule_migrates_when_moved_without_losing_dates(editor):
    store, create, _ = editor
    initial = datetime(2026, 11, 9, 9, tzinfo=ZONE)
    master = Event('legacy-rule', 'local', 'Old regular custom series', initial, initial + timedelta(minutes=90),
                   recurrence_rule='FREQ=WEEKLY;INTERVAL=2;COUNT=3', schedule_expression='3(Mo X)',
                   expression_anchor='2026-11-04')
    store.apply('Import old custom rule', [master])
    selected = occurrences(store, master)[1]
    dialog = create(event=selected, series=master, edit_scope='group')
    assert len(dialog.sequence_starts()) == 3
    assert 'Mo' not in dialog.pattern.text()
    assert dialog.build_events()[0].schedule_syntax == 1
    dialog.start.setDateTime(dialog.start.dateTime().addDays(1))
    changed = save(dialog)
    store.edit_series(dialog.edit_reference, changed, replace_schedule=dialog.schedule_replacement)
    current = store.get_event(master.id)
    assert current.schedule_syntax == 2
    assert current.end - current.start == master.end - master.start
    assert [value.start for value in occurrences(store, current)] == [
        initial + timedelta(days=1), initial + timedelta(days=15), initial + timedelta(days=29)]
    reopen = create(event=occurrences(store, current)[-1], series=current, edit_scope='group')
    assert reopen.sequence_starts() == [value.start for value in occurrences(store, current)]


def test_changing_imported_utc_event_to_local_custom_rule_preserves_clock_across_dst(editor):
    store, create, _ = editor
    initial = datetime(2027, 3, 20, 8, tzinfo=ZoneInfo('UTC'))
    imported = Event('utc', 'local', 'Imported UTC event', initial, initial + timedelta(minutes=90))
    dialog = create(event=imported)
    dialog.pattern.setText('3*+1')
    master = save(dialog)
    store.apply('Create local custom rule', [master])
    assert master.timezone_id == 'Europe/Berlin'
    assert master.recurrence_rule == 'FREQ=WEEKLY;INTERVAL=1;COUNT=4'
    values = occurrences(store, master)
    assert [value.start.astimezone(ZONE).hour for value in values] == [9, 9, 9, 9]
    assert [value.start.astimezone(ZoneInfo('UTC')).hour for value in values] == [8, 8, 7, 7]
