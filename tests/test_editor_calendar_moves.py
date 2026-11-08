"""Calendar choices in the real edit dialog reach atomic workspace moves."""
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
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import occurrence
from debacal.schedule_authoring import expression_matches
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 8, 9, tzinfo=ZONE)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    destination = Calendar('target', 'Target', '#b77457', visible=False)
    store.save_calendar(destination)
    errors = []
    with patch('debacal.ui.now', return_value=START), \
         patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, value, __: errors.append(value)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        window.activateWindow()
        app.processEvents()
        yield store, window, destination
        window.close()
        window.executor.shutdown(wait=True)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_editing_series_calendar_moves_exact_scope_and_reselects_the_destination(workspace, scope):
    store, window, destination = workspace
    source = Event('series', 'local', 'A meeting', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY;COUNT=3', timezone_id=ZONE.key)
    store.apply('Create series', [source])
    selected = occurrence(source, START + timedelta(weeks=1))
    window.go_date(selected.start.date())
    window.set_selection_mode(scope)
    window.select_event(selected.id)
    before = store.query_occurrences(START, START + timedelta(days=30))

    def accept(dialog):
        assert dialog.calendar.isEnabled()
        dialog.calendar.setCurrentIndex(dialog.calendar.findData(destination.id))
        assert dialog.calendar_change_requested
        assert dialog.preview.masters[0].calendar_id == destination.id
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()

    with patch.object(EventDialog, 'exec', accept):
        window.edit_event()
    assert not window.local_issues
    after = store.query_occurrences(START, START + timedelta(days=30))
    assert [(item.start, item.end) for item in after] == [(item.start, item.end) for item in before]
    moved = [item for item in after if item.calendar_id == destination.id]
    assert len(moved) == {'single': 1, 'group': 3, 'future': 2}[scope]
    assert store.get_calendar(destination.id).visible
    assert window.selection.primary.calendar_id == destination.id
    assert window.selection.primary.start == selected.start
    assert len(store.history()) == 2
    store.undo()
    restored = store.query_occurrences(START, START + timedelta(days=30))
    assert all(item.calendar_id == source.calendar_id for item in restored)
    assert [(item.start, item.end) for item in restored] == [(item.start, item.end) for item in before]


@pytest.mark.parametrize('choose_calendar', [False, True])
def test_mixed_calendar_edit_only_unifies_calendars_after_an_explicit_choice(workspace, choose_calendar):
    store, window, destination = workspace
    store.set_calendar_visible(destination.id, True)
    first = Event('first', 'local', 'First', START, START + timedelta(hours=1))
    second = replace(first, id='second', calendar_id=destination.id, title='Second',
                     start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1))
    store.apply('Create events', [first, second])
    window.refresh()
    window.select_event(second.id)
    window.select_event(first.id, additive=True)

    def accept(dialog):
        assert dialog.common_fields_only
        assert dialog.calendar.currentData() == first.calendar_id
        if choose_calendar:
            # Choosing the already displayed calendar is still an explicit
            # request to move the selection into it.
            dialog.calendar.activated.emit(dialog.calendar.currentIndex())
        dialog.title.setText('Edited')
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()

    with patch.object(EventDialog, 'exec', accept):
        window.edit_event()
    assert not window.local_issues
    values = store.list_events()
    assert len(values) == 2 and {value.title for value in values} == {'Edited'}
    assert {value.calendar_id for value in values} == ({'local'} if choose_calendar else {'local', destination.id})


def test_calendar_choice_and_move_intent_are_preserved_per_scope(workspace):
    store, window, destination = workspace
    source = Event('series', 'local', 'A meeting', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY;COUNT=3')
    dialog = EventDialog(store.list_calendars(), START, source.end, event=occurrence(source, START),
                         series=source, edit_scope='single', preferences=Preferences())
    try:
        dialog.calendar.setCurrentIndex(dialog.calendar.findData(destination.id))
        assert dialog.calendar_change_requested
        dialog.scope.setCurrentData('group')
        assert dialog.calendar.currentData() == source.calendar_id and not dialog.calendar_change_requested
        dialog.scope.setCurrentData('single')
        assert dialog.calendar.currentData() == destination.id and dialog.calendar_change_requested
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize('rule', ['FREQ=WEEKLY;COUNT=4', 'FREQ=WEEKLY'])
def test_preview_time_edit_and_calendar_transfer_keep_the_sequence_and_exception(workspace, rule):
    store, window, destination = workspace
    source = Event('series', 'local', 'A meeting', START, START + timedelta(hours=1),
                   recurrence_rule=rule, timezone_id=ZONE.key)
    store.apply('Create series', [source])
    selected = occurrence(source, START + timedelta(weeks=1))
    window.set_selection_mode('group')
    window.select_event(selected.id)
    moved_start = selected.start + timedelta(hours=1, minutes=30)

    def accept(dialog):
        target = next(item for item in dialog.preview.visible_events if item.recurrence_id == selected.recurrence_id)
        dialog.edit_occurrence(target)
        picker = dialog.range_picker
        picker.set_start(moved_start)
        picker.accept()
        assert not dialog.error.text()
        assert '10:30' in dialog.pattern.text()
        assert expression_matches(dialog.preview.masters[0], dialog.pattern.text())
        dialog.calendar.setCurrentIndex(dialog.calendar.findData(destination.id))
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()

    with patch.object(EventDialog, 'exec', accept):
        window.edit_event()
    assert not window.local_issues
    moved, = store.list_events(calendar_ids=[destination.id])
    assert moved.recurrence_rule == rule
    assert expression_matches(moved, moved.schedule_expression)
    override = Event.from_dict(moved.recurrence_overrides[selected.recurrence_id])
    assert (override.start, override.end) == (moved_start, moved_start + timedelta(hours=1))
    loaded, = parse_ics(export_ics([moved]), destination.id)
    assert expression_matches(loaded, loaded.schedule_expression)
    assert set(loaded.recurrence_overrides) == {selected.recurrence_id}
    store.undo()
    assert store.get_event(source.id) == source
    assert store.list_events(calendar_ids=[destination.id]) == []
