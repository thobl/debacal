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


def test_custom_range_addition_survives_rule_edit_save_and_ics_roundtrip(author):
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
    text = '  5*+1d  '
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
def test_future_regular_controls_use_nominal_anchor_for_moved_primary(author, setting):
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
    assert dialog.repeat_until.value().date() == (START + timedelta(days=4)).date()
    if setting == 'count':
        dialog.repeat_count.setValue(4)
    else:
        dialog.repeat_until.set_value(START + timedelta(days=5))
    assert not dialog.error.text()
    edited = save(dialog)
    assert edited.start == START + timedelta(days=2)
    store.edit_future(selected, edited, replace_schedule=dialog.schedule_replacement)
    values = store.query_occurrences(START - timedelta(days=1), START + timedelta(days=100))
    assert {value.start for value in values} == {START + timedelta(days=value) for value in (0, 1, 3, 4, 5)} | {moved_start}
    assert next(value for value in values if value.start == moved_start).end - moved_start == timedelta(hours=3)
