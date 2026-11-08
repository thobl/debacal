"""Coincident compact events keep real content through editing and import."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar
from debacal.recurrence import expand_event
from debacal.schedule_authoring import expression_matches
from debacal.store import Store


START = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


@pytest.fixture
def editor(tmp_path):
    app = QApplication.instance() or QApplication([])
    widgets = []
    store = Store(tmp_path / 'calendar.sqlite3')
    calendars = [Calendar('local', 'Personal', '#557766')]
    with patch('debacal.event_dialog.local_zone', return_value=timezone.utc):
        def create(source=None):
            dialog = EventDialog(calendars, source.start if source else START,
                                 source.end if source else START + timedelta(hours=1),
                                 event=source, series=source, edit_scope='group')
            widgets.append(dialog)
            return dialog
        yield store, create
        for dialog in widgets:
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    store.close()


def occurrences(event):
    return expand_event(event, START - timedelta(days=3), START + timedelta(days=35))


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    return dialog.result_events[0]


@pytest.mark.parametrize('all_day', [False, True])
def test_new_coincident_events_inherit_actual_dialog_content_and_sync(editor, all_day):
    store, create = editor
    dialog = create()
    dialog.title.setText('Seminar')
    dialog.location.setText('Room 7')
    dialog.description.setPlainText('Shared notes')
    dialog.all_day.setChecked(all_day)
    dialog.pattern.setText('+0d +0d +1w')
    source = save(dialog)
    store.apply('Create series', [source])
    actual = occurrences(source)
    assert len(actual) == 3 and actual[0].start == actual[1].start
    assert len({item.id for item in actual}) == 3
    assert all((item.title, item.location, item.description, item.calendar_id) ==
               ('Seminar', 'Room 7', 'Shared notes', 'local') for item in actual)
    assert expression_matches(source, source.schedule_expression)
    loaded = parse_ics(export_ics([source]), 'local')[0]
    def range_and_title(item):
        return (item.start.date(), item.end.date(), item.title) if all_day else (item.start, item.end, item.title)
    assert [range_and_title(item) for item in occurrences(loaded)] == [range_and_title(item) for item in actual]
    reopened = create(loaded)
    assert not reopened.pattern.text().startswith('schedule{')
    assert len(reopened.sequence_starts()) == 3
    store.undo()
    assert store.get_event(source.id) is None
    store.redo()
    assert len(occurrences(store.get_event(source.id))) == 3


def test_add_same_time_then_remove_just_one_preview_occurrence(editor):
    store, create = editor
    dialog = create()
    dialog.title.setText('Appointment')
    dialog.pattern.setText('+0d +1w')
    dialog._add_range(START, START + timedelta(minutes=30))
    source = save(dialog)
    actual = occurrences(source)
    assert len(actual) == 3
    store.apply('Create choices', [source])
    duplicate = next(item for item in actual if item.start == START and item.end - item.start == timedelta(minutes=30))
    store.edit_occurrence(duplicate, replace(duplicate, title='Separate title', description='Retain this note'))
    source = store.get_event(source.id)
    dialog = create(source)
    selected = next(item for item in occurrences(source) if item.title == 'Separate title')
    dialog.remove_occurrence(selected)
    changed = save(dialog)
    store.edit_series(source, changed, replace_schedule=dialog.schedule_replacement)
    remaining = occurrences(store.get_event(source.id))
    assert len(remaining) == 2
    assert sum(item.start == START for item in remaining) == 1
    assert all(item.title == 'Appointment' for item in remaining)
    assert not dialog.pattern.text().startswith('schedule{')
    store.undo()
    restored = next(item for item in occurrences(store.get_event(source.id)) if item.title == 'Separate title')
    assert restored.description == 'Retain this note'


def test_weekday_text_can_reopen_edit_counts_and_preserve_both_weekdays(editor):
    _, create = editor
    dialog = create()
    dialog.title.setText('Meetings')
    dialog.pattern.setText('every(1w, on=MO+WE, count=6)')
    source = save(dialog)
    reopened = create(source)
    assert 'on=MO+WE' in reopened.pattern.text()
    if reopened._draft.kind == 'WEEKLY':
        reopened.repeat_count.setValue(8)
    else:
        reopened.pattern.setText('every(1w, on=MO+WE, count=8)')
    changed = save(reopened)
    assert len(occurrences(changed)) == 8
    assert {item.start.weekday() for item in occurrences(changed)} == {0, 2}
    assert expression_matches(changed, changed.schedule_expression)


def test_header_edits_after_adding_a_coincident_range_apply_to_the_entire_new_draft(editor):
    _, create = editor
    dialog = create()
    dialog.title.setText('Initial title')
    dialog.pattern.setText('+0d +1w')
    dialog._add_range(START, START + timedelta(minutes=30))
    dialog.title.setText('Final title')
    dialog.location.setText('Final room')
    dialog.description.setPlainText('Final notes')
    source = save(dialog)
    assert len(occurrences(source)) == 3
    assert {(item.title, item.location, item.description) for item in occurrences(source)} == {
        ('Final title', 'Final room', 'Final notes')}
    assert sorted((item.end - item.start).total_seconds() for item in occurrences(source)) == [1800, 3600, 3600]
