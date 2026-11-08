"""Authoring text, server colors and default-calendar integration regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import CalendarDialog, EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.patterns import expand_pattern, expand_sequence
from debacal.legacy_patterns import expand_pattern as expand_legacy_pattern
from debacal.store import Store
from debacal.ui import MainWindow

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 10, tzinfo=ZONE)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    windows = []
    with Store(':memory:') as store:
        with patch('debacal.event_dialog.local_zone', return_value=ZONE):
            yield store, windows
        for window in reversed(windows):
            window.close()
        app.processEvents()


def test_custom_language_survives_storage_sync_and_editing_a_later_occurrence(desktop):
    store, windows = desktop
    literal = '4*(+2)  +3 3*+2'
    create = EventDialog(store.list_calendars(), START)
    windows.append(create)
    create.title.setText('Seminar')
    create.pattern.setText(literal)
    create.save()
    assert create.result() == QDialog.DialogCode.Accepted, create.error.text()
    master = create.result_events[0]
    assert master.schedule_expression == literal
    assert master.schedule_syntax == 2
    assert master.expression_anchor == START.date().isoformat()
    store.apply('Create', [master])
    imported = parse_ics(export_ics([master]), master.calendar_id)[0]
    assert imported.schedule_expression == literal
    assert imported.expression_anchor == master.expression_anchor
    instances = store.query_occurrences(START, START + timedelta(days=400))
    selected = instances[4]
    edit = EventDialog(store.list_calendars(), selected.start, selected.end,
                       event=selected, series=master, edit_scope='group')
    windows.append(edit)
    assert edit.pattern.text() == literal
    assert edit.value(edit.start) == master.start
    assert edit.dates() == expand_pattern(literal, START.date())
    edit.pattern.setText('2*(+2) +3 2*+2')
    edit.save()
    assert edit.result() == QDialog.DialogCode.Accepted, edit.error.text()
    store.edit_series(edit.edit_reference, edit.result_events[0], replace_schedule=edit.schedule_replacement)
    changed = store.get_event(master.id)
    assert changed.schedule_expression == '2*(+2) +3 2*+2'
    dates = [item.start.date() for item in store.query_occurrences(START, START + timedelta(days=400))]
    assert dates == expand_pattern(changed.schedule_expression, START.date())
    store.undo()
    assert store.get_event(master.id).schedule_expression == literal


@pytest.mark.parametrize('authored', [False, True])
def test_legacy_custom_dates_reopen_as_equivalent_new_language(desktop, authored):
    store, windows = desktop
    literal = '13(Mo X) 2X 4(Mo X)'
    dates = expand_legacy_pattern(literal, START.date())
    times = [START.replace(year=day.year, month=day.month, day=day.day) for day in dates]
    master = Event('legacy', 'local', 'Seminar', times[0], times[0] + timedelta(hours=1),
                   recurrence_dates=tuple(value.isoformat() for value in times[1:]),
                   schedule_expression=literal if authored else '',
                   expression_anchor=START.date().isoformat() if authored else '')
    store.apply('Create', [master])
    selected = store.query_occurrences(START, START + timedelta(days=400))[1]
    dialog = EventDialog(store.list_calendars(), selected.start, selected.end, event=selected, series=master, edit_scope='group')
    windows.append(dialog)
    assert 'Mo' not in dialog.pattern.text()
    assert '+2' in dialog.pattern.text()
    assert dialog.dates() == dates
    # Opening old data displays the current language without rewriting it.
    preserved = dialog.build_events()[0]
    assert preserved.schedule_syntax == master.schedule_syntax
    assert preserved.schedule_expression == master.schedule_expression
    # An explicit configuration edit adopts the current language on save.
    dialog.pattern.setText(' ' + dialog.pattern.text() + ' ')
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    store.edit_series(dialog.edit_reference, dialog.result_events[0], replace_schedule=dialog.schedule_replacement)
    migrated = store.get_event(master.id)
    assert migrated.schedule_syntax == 2
    assert [value.date() for value in expand_sequence(migrated.schedule_expression, migrated.start)] == dates
    assert migrated.expression_anchor == master.start.date().isoformat()
    store.undo()
    assert store.get_event(master.id) == master


def test_one_date_pattern_can_be_edited_and_moved_without_losing_its_language(desktop):
    store, windows = desktop
    create = EventDialog(store.list_calendars(), START)
    windows.append(create)
    create.title.setText('One custom slot')
    create.pattern.setText('4.11.2026')
    create.save()
    master = create.result_events[0]
    store.apply('Create', [master])
    selected = store.query_occurrences(START, START + timedelta(days=14))[0]
    edit = EventDialog(store.list_calendars(), selected.start, selected.end, event=selected, series=master, edit_scope='group')
    windows.append(edit)
    assert edit.pattern.text() == '4.11.2026'
    edit.title.setText('Moved custom slot')
    edit.start.setDateTime(edit.start.dateTime().addDays(1))
    edit.save()
    assert edit.result() == QDialog.DialogCode.Accepted, edit.error.text()
    store.edit_series(edit.edit_reference, edit.result_events[0], replace_schedule=edit.schedule_replacement)
    changed = store.get_event(master.id)
    assert changed.start.date() == master.start.date() + timedelta(days=1)
    assert changed.end - changed.start == master.end - master.start
    assert 'Mo' not in changed.schedule_expression
    assert changed.schedule_expression == changed.expression_anchor == ''
    assert expand_pattern(changed.schedule_expression, changed.start.date()) == [changed.start.date()]


def test_password_only_calendar_edit_preserves_arbitrary_server_color(desktop):
    _, windows = desktop
    source = Calendar('remote', 'Nextcloud', '#123abc', kind='caldav',
                      url='https://cloud.test/calendar/', username='user', server_color='#123abc')
    dialog = CalendarDialog(source)
    windows.append(dialog)
    dialog.password.setText('synthetic-app-password')
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_calendar.color == '#123abc'
    assert dialog.result_calendar.server_color == '#123abc'
    assert dialog.result_calendar.use_server_color
    assert dialog.color.count() > 5


def test_manual_color_override_and_return_to_server_color(desktop):
    _, windows = desktop
    source = Calendar('remote', 'Remote', '#123abc', kind='caldav', url='https://cloud.test/c/', server_color='#123abc')
    dialog = CalendarDialog(source)
    windows.append(dialog)
    dialog.use_server_color.setChecked(False)
    dialog.add_color('Custom', '#a14fe7')
    dialog.color.setCurrentIndex(dialog.color.findData('#a14fe7'))
    dialog.save()
    manual = dialog.result_calendar
    assert manual.color == '#a14fe7' and not manual.use_server_color
    updated = replace(manual, server_color='#102030')
    assert updated.color == '#a14fe7'
    follow = replace(updated, use_server_color=True)
    assert follow.color == '#102030'


def test_new_event_uses_default_calendar_while_edits_keep_their_calendar(desktop):
    store, windows = desktop
    remote = Calendar('remote', 'Work', '#123abc', kind='caldav', url='https://cloud.test/c/')
    store.save_calendar(remote)
    store.set_default_calendar(remote.id)
    window = MainWindow(store, enable_sync=False)
    windows.append(window)
    selected = []
    def inspect(dialog):
        selected.append(dialog.calendar.currentData())
        return QDialog.DialogCode.Rejected
    with patch.object(EventDialog, 'exec', inspect):
        window.create_event(START)
    assert selected == [remote.id]
    original = Event('old', 'local', 'Personal event', START, START + timedelta(hours=1))
    edit = EventDialog(store.list_calendars(), START, event=original, default_calendar_id=remote.id)
    windows.append(edit)
    assert edit.calendar.currentData() == 'local'
