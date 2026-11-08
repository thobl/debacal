"""Workspace selections retain floating dates and only target visible sources."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, tzinfo=timezone.utc)
DISPLAY = ZoneInfo('America/Los_Angeles')


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, \
         patch('debacal.ui.local_zone', return_value=DISPLAY), \
         patch('debacal.calendar_widgets.local_zone', return_value=DISPLAY), \
         patch('debacal.year_view.local_zone', return_value=DISPLAY), \
         patch('debacal.ui.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.go_date(START.date())
        window.show()
        app.processEvents()
        yield store, window
        app.clipboard().clear()
        window.close()
        window.executor.shutdown(wait=True)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_source_preferences_disabling_a_calendar_drops_its_selected_events(workspace):
    store, window = workspace
    source = Event('selected', 'local', 'Selected event', START, START + timedelta(hours=1))
    store.apply_remote([source])
    window.refresh()
    window.select_event(source.id)
    calendar = store.get_calendar(source.calendar_id)
    store.save_calendar(replace(calendar, enabled=False))
    window.preferences_sources_changed()
    assert not window.selection.refs and window.selected_id is None
    window.delete_event()
    assert store.get_event(source.id) == source


def test_disabled_paste_destination_retains_preview_until_it_can_be_fixed(workspace):
    store, window = workspace
    source = Event('selected', 'local', 'Selected event', START, START + timedelta(hours=1))
    store.apply_remote([source])
    window.refresh()
    window.select_event(source.id)
    window.copy_event()
    window.paste_events()
    pending = window.pending_paste
    assert pending is not None
    calendar = store.get_calendar(pending.calendar_id)
    store.save_calendar(replace(calendar, enabled=False))
    identities = store.event_ids()
    window.confirm_paste()
    assert window.pending_paste is pending
    assert store.event_ids() == identities
    assert any('no longer writable' in issue.details for issue in window.local_issues)


@pytest.mark.parametrize('copy', [False, True])
@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('view', ['week', 'month', 'year'])
def test_all_day_move_and_copy_keep_the_floating_selected_date(workspace, copy, recurring, view):
    store, window = workspace
    source = Event('day', 'local', 'Floating date', START, START + timedelta(days=1), all_day=True,
                   recurrence_rule='FREQ=WEEKLY;COUNT=3' if recurring else '')
    store.apply('Create', [source])
    window.set_view(view)
    window.refresh()
    window.select_event(source.id)
    selected = window.event_by_id(window.selected_id)
    target = selected.start + timedelta(days=2)
    window.move_event(selected.id, target, target + timedelta(days=1), scope='group', copy=copy)
    assert window.anchor == window.selected_day == target.date()
    assert window.selection.primary.all_day
    if recurring:
        assert window.selection.primary.start.date() == target.date()
    else:
        assert window.selection.primary.start == target


@pytest.mark.parametrize('copy', [False, True])
def test_mixed_selection_uses_clicked_all_day_date_when_timed_resource_is_first(workspace, copy):
    store, window = workspace
    timed_start = datetime(2026, 11, 4, 10, tzinfo=DISPLAY)
    timed = Event('timed', 'local', 'Timed', timed_start, timed_start + timedelta(hours=1))
    all_day = Event('day', 'local', 'All day', START, START + timedelta(days=1), all_day=True)
    store.apply('Create', [timed, all_day])
    window.refresh()
    window.select_event(timed.id)
    window.select_event(all_day.id, additive=True)
    target = START + timedelta(days=2)
    window.move_event(all_day.id, target, target + timedelta(days=1), copy=copy)
    assert window.anchor == window.selected_day == target.date()
    assert len(window.selection.refs) == 2
    assert window.selection.primary.all_day


@pytest.mark.parametrize('keep_other', [False, True])
def test_hiding_calendar_drops_only_its_selection_before_keyboard_actions(workspace, keep_other):
    store, window = workspace
    store.save_calendar(Calendar('other', 'Other', '#587c67'))
    hidden = Event('hidden', 'other', 'Hidden', START, START + timedelta(hours=1))
    visible = Event('visible', 'local', 'Visible', START, START + timedelta(hours=1))
    store.apply('Create', [hidden, visible])
    window.refresh()
    if keep_other:
        window.select_event(visible.id)
    window.select_event(hidden.id, additive=keep_other)
    window._search_selection_active = True
    window._search_row_ids = {value.id for value in window.selection.refs}
    window.toggle_calendar('other', False)
    assert window.selection.ids == ({visible.id} if keep_other else set())
    assert not window._search_selection_active and not window._search_row_ids
    window.delete_event()
    assert store.get_event(hidden.id) == hidden
    assert (store.get_event(visible.id) is None) == keep_other
