"""The authoring form and its schedule draft share one lossless edit path."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.recurrence import expand_event, recurrence_key, schedule_fields
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def visible(master):
    return expand_event(master, START - timedelta(days=1), START + timedelta(days=100))


@pytest.fixture
def author():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    dialogs, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, value, _trace: errors.append(value)):
        def create(master=None, selected=None, scope='group'):
            if master is not None:
                selected = selected or visible(master)[0]
            dialog = EventDialog(store.list_calendars(), selected.start if selected else START,
                                 selected.end if selected else START + timedelta(minutes=45),
                                 event=selected, series=master, edit_scope=scope)
            dialogs.append(dialog)
            if master is None:
                dialog.title.setText('A draft')
            return dialog
        yield create, store
        for dialog in reversed(dialogs):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    return dialog.result_events[0]


def test_regular_count_until_and_anchor_controls_remain_synchronized(author):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('DAILY')
    dialog.repeat_count.setValue(5)
    dialog.interval.setValue(2)
    assert dialog.repeat_until.value().date() == (START + timedelta(days=8)).date()
    dialog.repeat_until.set_value(START + timedelta(days=6))
    assert dialog.repeat_count.value() == 4
    dialog.start.set_value(START + timedelta(days=2))
    assert dialog.repeat_count.value() == 3
    event = save(dialog)
    assert len(visible(event)) == 3
    assert visible(event)[-1].start.date() == (START + timedelta(days=6)).date()


def test_primary_range_stays_editable_after_preview_builds_regular_and_custom_series(author):
    create, _ = author
    dialog = create()
    for days in (1, 2, 3):
        dialog.toggle_date((START + timedelta(days=days)).date())
    assert dialog.repeat.currentData() == 'DAILY'
    dialog.toggle_date((START + timedelta(days=2)).date())
    assert dialog.repeat.currentData() == 'RDATE'
    delta = timedelta(days=2, hours=1)
    dialog.start.set_value(START + delta)
    event = save(dialog)
    assert {value.start for value in visible(event)} == {START + delta + timedelta(days=value) for value in (0, 1, 3)}
    assert event.end - event.start == timedelta(minutes=45)


def test_custom_range_addition_is_restored_after_trying_a_clean_regular_pattern(author):
    create, store = author
    source = Event('source', 'local', 'A series', START, START + timedelta(minutes=45),
                   recurrence_rule='FREQ=DAILY;COUNT=4', timezone_id=ZONE.key)
    store.apply('Create series', [source])
    dialog = create(source)
    added = START + timedelta(days=8, hours=2)
    dialog._add_range(added, added + timedelta(hours=2))
    assert dialog.repeat.currentData() == 'RDATE'
    dialog.repeat.setCurrentData('DAILY')
    dialog.repeat_count.setValue(5)
    assert all(value.start != added for value in visible(dialog.build_events()[0]))
    dialog.repeat.setCurrentData('RDATE')
    dialog.title.setText('Changed after adding an occurrence')
    dialog.location.setText('Another room')
    edited = save(dialog)
    store.edit_series(dialog.edit_reference, edited, replace_schedule=dialog.schedule_replacement)
    final = store.get_event(source.id)
    detached = next(value for value in visible(final) if value.start == added)
    assert detached.end - detached.start == timedelta(hours=2)
    assert detached.title == 'Changed after adding an occurrence' and detached.location == 'Another room'
    parsed = parse_ics(export_ics([final]), 'local')[0]
    detached = next(value for value in visible(parsed) if value.start == added)
    assert detached.end - detached.start == timedelta(hours=2)
    reopened = create(final)
    reopened.title.setText('Title edit after reopening')
    store.edit_series(reopened.edit_reference, save(reopened), replace_schedule=reopened.schedule_replacement)
    detached = next(value for value in visible(store.get_event(source.id)) if value.start == added)
    assert detached.end - detached.start == timedelta(hours=2)
    assert detached.title == 'Title edit after reopening'


@pytest.mark.parametrize('scope', ['group', 'future'])
def test_picker_edits_keep_occurrence_identity_and_duration_when_saving_scope(author, scope):
    create, store = author
    text = '  +0d 5*+1d  '
    source = Event('source', 'local', 'A series', START, START + timedelta(minutes=45),
                   **schedule_fields(expand_sequence(text, START)), schedule_expression=text,
                   expression_anchor=START.date().isoformat(), schedule_syntax=2, timezone_id=ZONE.key)
    store.apply('Create series', [source])
    selected = visible(source)[1] if scope == 'future' else visible(source)[0]
    dialog = create(source, selected, scope)
    target = visible(dialog._project_preview(dialog.build_events()[0]))[2]
    key = target.recurrence_id
    moved = target.start + timedelta(hours=2)
    dialog.edit_occurrence(target)
    picker = dialog.range_picker
    picker.set_range(moved, moved + timedelta(hours=3))
    picker.accept()
    edited = save(dialog)
    assert key in edited.recurrence_overrides
    assert recurrence_key(moved) not in edited.recurrence_dates
    if scope == 'future':
        store.edit_future(selected, edited, replace_schedule=dialog.schedule_replacement)
        all_values = store.query_occurrences(START - timedelta(days=1), START + timedelta(days=100))
    else:
        store.edit_series(dialog.edit_reference, edited, replace_schedule=dialog.schedule_replacement)
        all_values = visible(store.get_event(source.id))
    changed = next(value for value in all_values if value.start == moved)
    assert changed.recurrence_id == key
    assert changed.end - changed.start == timedelta(hours=3)


@pytest.mark.parametrize('setting', ['count', 'until'])
def test_future_moved_reference_starts_custom_and_explicit_regular_reset_uses_chosen_start(author, setting):
    create, store = author
    source = Event('source', 'local', 'A series', START, START + timedelta(minutes=45),
                   recurrence_rule='FREQ=DAILY;COUNT=5', timezone_id=ZONE.key)
    store.apply('Create', [source])
    selected = visible(source)[2]
    moved_start = selected.start + timedelta(days=10)
    from dataclasses import replace
    store.edit_occurrence(selected, replace(selected, start=moved_start, end=moved_start + timedelta(hours=3)))
    source = store.get_event(source.id)
    selected = next(value for value in visible(source) if value.start == moved_start)
    dialog = create(source, selected, 'future')
    assert dialog.repeat.currentData() == 'RDATE'
    assert dialog._draft.schedule_anchor(moved_start) == START + timedelta(days=2)
    # Actual starts have no daily regularity: the first nominal occurrence was
    # moved ten days away. Explicitly choosing a regular pattern resets those
    # custom changes around the currently shown start, as for other sequences.
    dialog.repeat.setCurrentData('DAILY')
    if setting == 'count':
        dialog.repeat_count.setValue(4)
    else:
        dialog.repeat_until.set_value(moved_start + timedelta(days=3))
    assert not dialog.error.text()
    edited = save(dialog)
    assert edited.start == moved_start
    store.edit_future(selected, edited, replace_schedule=dialog.schedule_replacement)
    values = store.query_occurrences(START - timedelta(days=1), START + timedelta(days=100))
    assert {value.start for value in values} == {START, START + timedelta(days=1)} | {
        moved_start + timedelta(days=index) for index in range(4)}
    assert next(value for value in values if value.start == moved_start).end - moved_start == timedelta(hours=3)


def test_calendar_authoring_suspends_without_saving_and_can_resume_the_same_draft(author):
    create, store = author
    dialog = create()
    button = dialog.add_occurrences_button
    assert not button.text()
    assert button.toolTip() == 'Add occurrences in calendar…'
    assert dialog.schedule_mode_row.indexOf(button) == dialog.schedule_mode_row.indexOf(dialog.add_occurrence_button) + 1

    assert dialog.begin_calendar_occurrences()
    assert dialog.result() == EventDialog.PICK_OCCURRENCES
    assert not dialog.result_events
    added = START + timedelta(days=3, hours=2)
    assert dialog.add_calendar_occurrence(added)
    assert dialog.begin_calendar_occurrences()
    assert dialog.add_calendar_occurrence(added + timedelta(days=1))
    event = save(dialog)
    assert {item.start for item in visible(event)} == {START, added, added + timedelta(days=1)}
    assert store.query_occurrences(START, START + timedelta(days=10)) == []


@pytest.mark.parametrize('mode', ['repeat', 'alternatives'])
@pytest.mark.parametrize('custom', [False, True])
def test_calendar_addition_preserves_existing_regular_custom_and_alternative_dates(author, mode, custom):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData(mode)
    if custom:
        dialog.repeat.setCurrentData('RDATE')
        dialog.pattern.setText('+0d +2d +5d')
        expected = {START + timedelta(days=index) for index in (0, 2, 7)}
    else:
        dialog.repeat.setCurrentData('WEEKLY')
        dialog.repeat_count.setValue(3)
        expected = {START + timedelta(weeks=index) for index in range(3)}
    assert dialog.begin_calendar_occurrences()
    added = START + timedelta(days=10, hours=3)
    assert dialog.add_calendar_occurrence(added)
    event = save(dialog)
    assert {item.start for item in visible(event)} == expected | {added}
    assert all(item.end - item.start == timedelta(minutes=45) for item in visible(event))
    assert event.candidate is (mode == 'alternatives')


def test_calendar_authoring_commits_typed_duration_and_pending_sequence_before_suspension(author):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.blockSignals(True)
    dialog.pattern.setText('+0d +3d +6d')
    dialog.pattern.blockSignals(False)
    dialog.duration.setText('1h 30min')
    assert dialog.begin_calendar_occurrences(), dialog.error.text()
    assert dialog._draft.expression == '+0d +3d +6d'
    added = START + timedelta(days=8)
    assert dialog.occurrence_range_at(added) == (added, added + timedelta(minutes=90))
    assert dialog.add_calendar_occurrence(added)
    items = visible(save(dialog))
    assert {item.start for item in items} == {START + timedelta(days=index) for index in (0, 3, 8, 9)}
    assert all(item.end - item.start == timedelta(minutes=90) for item in items)


def test_invalid_sequence_stays_in_editor_without_discarding_the_draft(author):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('+0d +3d')
    valid = dialog._draft.expression
    dialog.pattern.setText('+0d +nonsense')
    assert not dialog.begin_calendar_occurrences()
    assert dialog.result() != EventDialog.PICK_OCCURRENCES
    assert dialog.pattern.text() == '+0d +nonsense'
    assert dialog._draft.expression == valid
    assert dialog.error.text()
    assert not dialog.result_events
    dialog.pattern.setText('+0d +3d')
    assert dialog.begin_calendar_occurrences()


def test_calendar_drag_preserves_added_occurrence_identity_and_updates_its_duration(author):
    create, _ = author
    dialog = create()
    changes = []
    dialog.draft_changed.connect(lambda: changes.append(True))
    added = START + timedelta(days=4, hours=2)
    assert dialog.add_calendar_occurrence(added)
    item = next(item for item in visible(dialog.calendar_occurrence_master()) if item.start == added)
    moved = added + timedelta(days=1, hours=1)
    assert dialog.edit_calendar_occurrence(item, moved, moved + timedelta(hours=2), duration_spec='')
    event = save(dialog)
    items = visible(event)
    assert len(items) == 2
    assert items[0].start == START
    changed = next(item for item in items if item.start == moved)
    assert changed.recurrence_id == item.recurrence_id
    assert changed.end - changed.start == timedelta(hours=2)
    assert len(changes) == 2


def test_calendar_dragging_primary_updates_prototype_and_preserves_relative_schedule(author):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('+0d +2d +5d')
    primary = visible(dialog.calendar_occurrence_master())[0]
    assert dialog.preview.is_primary(primary)
    moved = START + timedelta(hours=2)
    assert dialog.edit_calendar_occurrence(primary, moved, moved + timedelta(hours=1), duration_spec='')
    assert dialog.value(dialog.start) == moved
    assert dialog.value(dialog.end) == moved + timedelta(hours=1)
    event = save(dialog)
    assert {item.start for item in visible(event)} == {moved + timedelta(days=index) for index in (0, 2, 7)}
    assert all(item.end - item.start == timedelta(hours=1) for item in visible(event))
    assert dialog.occurrence_range_at(moved + timedelta(days=8))[1] == moved + timedelta(days=8, hours=1)


def test_calendar_authoring_button_follows_schedule_editability(author):
    create, _ = author
    source = Event('source', 'local', 'A series', START, START + timedelta(minutes=45),
                   recurrence_rule='FREQ=DAILY;COUNT=4', timezone_id=ZONE.key)
    dialog = create(source, scope='single')
    assert not dialog.add_occurrences_button.isEnabled()
    assert not dialog.begin_calendar_occurrences()
    dialog.scope.setCurrentData('group')
    assert dialog.add_occurrences_button.isEnabled()
    assert dialog.begin_calendar_occurrences()


def test_calendar_occurrences_keep_all_day_spans_and_can_be_resized_individually(author):
    create, _ = author
    dialog = create()
    dialog.all_day.setChecked(True)
    first = START.replace(hour=0)
    dialog._set_range(first, first + timedelta(days=2))
    assert dialog.begin_calendar_occurrences()
    added = first + timedelta(days=8)
    assert dialog.add_calendar_occurrence(added)
    instance = next(item for item in visible(dialog.calendar_occurrence_master()) if item.start == added)
    assert instance.end == added + timedelta(days=2)
    assert dialog.edit_calendar_occurrence(instance, added, added + timedelta(days=3))
    event = save(dialog)
    values = visible(event)
    assert event.all_day
    assert next(item for item in values if item.start == first).end == first + timedelta(days=2)
    assert next(item for item in values if item.start == added).end == added + timedelta(days=3)


def test_calendar_occurrence_default_duration_keeps_nominal_days_across_dst(author):
    create, _ = author
    dialog = create()
    dialog.duration.setText('1d')
    assert dialog.begin_calendar_occurrences()
    added = datetime(2027, 3, 27, 12, tzinfo=ZONE)
    start, end = dialog.occurrence_range_at(added)
    assert start == added and end == added + timedelta(days=1)
    assert end.timestamp() - start.timestamp() == 23 * 3600
    assert dialog.add_calendar_occurrence(added)
    event = save(dialog)
    item = expand_event(event, added - timedelta(days=1), added + timedelta(days=2))[0]
    assert item.start == added and item.end == end
    assert item.duration_spec == 'P1D'


def test_calendar_primary_resize_replaces_nominal_duration_for_subsequent_additions(author):
    create, _ = author
    dialog = create()
    dialog.duration.setText('1d')
    assert dialog.begin_calendar_occurrences()
    primary = visible(dialog.calendar_occurrence_master())[0]
    assert primary.duration_spec == 'P1D'
    assert dialog.edit_calendar_occurrence(primary, START, START + timedelta(hours=2))
    assert dialog.duration_spec == ''
    target = START + timedelta(days=4)
    assert dialog.occurrence_range_at(target) == (target, target + timedelta(hours=2))
    assert dialog.add_calendar_occurrence(target)
    assert all(item.end - item.start == timedelta(hours=2) for item in visible(save(dialog)))


def test_calendar_primary_move_preserves_nominal_days_when_elapsed_duration_changes(author):
    create, _ = author
    dialog = create()
    dialog.duration.setText('1d')
    assert dialog.begin_calendar_occurrences()
    primary = visible(dialog.calendar_occurrence_master())[0]
    moved = datetime(2027, 3, 27, 12, tzinfo=ZONE)
    end = moved + timedelta(days=1)
    assert dialog.edit_calendar_occurrence(primary, moved, end)
    assert dialog.duration_spec == 'P1D'
    assert dialog.occurrence_range_at(moved) == (moved, end)
    assert end.timestamp() - moved.timestamp() == 23 * 3600


def test_calendar_addition_retains_an_indefinite_rule_and_duplicate_occurrences(author):
    create, _ = author
    dialog = create()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('WEEKLY')
    dialog.no_end.setChecked(True)
    assert dialog.begin_calendar_occurrences()
    added = START + timedelta(days=1)
    assert dialog.add_calendar_occurrence(added)
    assert dialog.add_calendar_occurrence(added)
    event = save(dialog)
    items = expand_event(event, START, START + timedelta(days=15))
    assert [item.start for item in items].count(added) == 2
    future = START + timedelta(weeks=100)
    assert any(item.start == future for item in expand_event(event, future, future + timedelta(days=1)))
