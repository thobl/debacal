"""Detected regular controls never reapply hidden artifacts from earlier edits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.recurrence import expand_event, occurrence, recurrence_key
from debacal.store import Store


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    owned, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=START.tzinfo), \
         patch('debacal.series_preview.local_zone', return_value=START.tzinfo), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(source=None, all_day=False):
            selected = occurrence(source, source.start) if source else None
            start = selected.start if selected else START.replace(hour=0) if all_day else START
            dialog = EventDialog(store.list_calendars(), start,
                                 start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                                 event=selected, series=source, edit_scope='group')
            owned.append(dialog)
            if source is None:
                dialog.title.setText('A regular appointment')
                dialog.all_day.setChecked(all_day)
            return dialog
        yield create, store
        for dialog in reversed(owned):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def projected(dialog):
    return dialog._project_preview(dialog.build_events()[0])


def instances(event):
    return expand_event(event, event.start - timedelta(days=1), event.start + timedelta(days=40))


def ranges(event):
    # All-day ranges are floating dates; timed ranges retain their exact instants.
    return [(value.all_day, recurrence_key(value.start, value.all_day),
             recurrence_key(value.end, value.all_day), value.title) for value in instances(event)]


@pytest.mark.parametrize('all_day', [False, True])
@pytest.mark.parametrize('reopen', [False, True])
@pytest.mark.parametrize('detached', [False, True])
def test_changing_regular_interval_after_tail_deletions_keeps_the_visible_count(editors, all_day, reopen, detached):
    create, store = editors
    count = 8 if detached else 5
    if detached:
        start = START.replace(hour=0) if all_day else START
        source = Event('series', 'local', 'A regular appointment', start,
                       start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                       recurrence_rule=f'FREQ=DAILY;COUNT={count}', all_day=all_day)
        nominal = start + timedelta(days=3)
        individual = replace(occurrence(source, nominal),
                             end=nominal + (timedelta(days=2) if all_day else timedelta(hours=2)),
                             title='A distinct appointment')
        source = replace(source, recurrence_overrides={recurrence_key(nominal, all_day): individual.to_dict()})
        store.apply('Create series', [source])
        dialog = create(source, all_day=all_day)
    else:
        source = None
        dialog = create(all_day=all_day)
        dialog.schedule_mode.setCurrentData('repeat')
        dialog.repeat.setCurrentData('DAILY')
        dialog.repeat_count.setValue(count)
    start = projected(dialog).start
    for offset in (count - 2, count - 1):
        dialog.toggle_date((start + timedelta(days=offset)).date())
    assert dialog.repeat.currentData() == 'DAILY'
    assert dialog.repeat_count.value() == count - 2
    if reopen:
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        if source is None:
            store.apply('Create modified series', dialog.result_events)
            source = store.get_event(dialog.result_events[0].id)
        else:
            store.edit_selection((occurrence(source, source.start),), 'group', dialog.edit_reference,
                                 dialog.result_events[0], replace_schedule=dialog.schedule_replacement)
            source = store.get_event(source.id)
        dialog = create(source, all_day=all_day)
    dialog.interval.setValue(3)
    edited = projected(dialog)
    assert [value.start for value in instances(edited)] == [start + timedelta(days=3 * index) for index in range(count - 2)]
    assert not edited.excluded_dates and not edited.recurrence_dates
    if detached:
        actual = next(value for value in instances(edited) if value.recurrence_id == recurrence_key(nominal, all_day))
        assert actual.title == individual.title and actual.end == individual.end
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    restored = parse_ics(export_ics([edited]), 'local')[0]
    assert ranges(restored) == ranges(edited)
