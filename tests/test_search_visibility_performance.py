"""Calendar visibility must not reformat unrelated expanded search rows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.search_panel import SearchPanel, COLOR_ROLE, SUBTITLE_ROLE
from debacal.store import Store
from debacal.timeutil import local_zone


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        store.apply_remote([Event('series', 'local', 'Synthetic series', START,
                                  START + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=100'),
                            Event('ordinary', 'local', 'Synthetic ordinary', START,
                                  START + timedelta(hours=1))])
        panel = SearchPanel(store, Preferences(date_format='iso', time_format='24h'))
        panel.set_range(START, START + timedelta(days=7))
        try:
            yield store, panel
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def expanded_series(panel):
    panel.populate('series')
    root = panel.topLevelItem(0)
    root.setExpanded(True)
    panel.load_more(root)
    assert root.childCount() == 21
    return root


def test_visibility_only_change_keeps_expanded_rows_without_reformatting(workspace):
    store, panel = workspace
    root = expanded_series(panel)
    child = root.child(12)
    panel.setCurrentItem(child)
    selected = panel.event_id(child)
    panel.set_row_selection({selected})
    subtitle, tooltip = child.data(SUBTITLE_ROLE), child.toolTip(0)
    revision = panel._presentation_revision
    store.set_calendar_visible('local', False)
    with patch.object(panel, '_when', side_effect=AssertionError('Dates did not change')), \
            patch.object(panel, '_present_event_details', side_effect=AssertionError('Details did not change')):
        assert not panel.update_context(store.list_calendars(), panel.preferences)
        panel.populate('series')
    assert panel._calendars['local'].visible is False
    assert panel._presentation_revision == revision
    assert panel.topLevelItem(0) is root and root.child(12) is child
    assert panel.currentItem() is child and panel.selected_row_ids() == {selected}
    assert child.data(SUBTITLE_ROLE) == subtitle and child.toolTip(0) == tooltip


@pytest.mark.parametrize('change', ['name', 'color', 'format', 'font'])
def test_display_changes_still_refresh_expanded_rows(workspace, change):
    store, panel = workspace
    root = expanded_series(panel)
    child = root.child(0)
    calendar = store.get_calendar('local')
    preferences = panel.preferences
    if change == 'name':
        calendar = replace(calendar, name='Renamed source')
    elif change == 'color':
        calendar = replace(calendar, color='#abcdef')
    elif change == 'format':
        preferences = replace(preferences, date_format='european', time_format='12h')
    else:
        preferences = replace(preferences, font_scale=125)
    with patch.object(panel, '_when', wraps=panel._when) as when, \
            patch.object(panel, '_present_event_details', wraps=panel._present_event_details) as details:
        assert panel.update_context([calendar], preferences)
    assert when.call_count and details.call_count
    assert child.data(COLOR_ROLE) == calendar.color
    assert calendar.name in child.toolTip(0)
    assert preferences.format_time(START.astimezone(local_zone())) in child.toolTip(0)


def test_visibility_changes_still_issue_fresh_upcoming_queries(workspace):
    store, panel = workspace
    store.save_calendar(Calendar('other', 'Other', '#123456'))
    store.apply_remote([Event('other-event', 'other', 'Other event', START,
                             START + timedelta(minutes=30))])
    with patch.object(store, 'upcoming_occurrences', wraps=store.upcoming_occurrences) as query:
        panel.populate_upcoming(START)
        assert panel.row_item('other-event') is not None
        store.set_calendar_visible('other', False)
        panel.populate_upcoming(START)
        assert panel.row_item('other-event') is None
        store.set_calendar_visible('other', True)
        panel.populate_upcoming(START)
        assert panel.row_item('other-event') is not None
    assert query.call_count == 3
    assert 'other' in query.call_args_list[0].args[2]
    assert 'other' not in query.call_args_list[1].args[2]
    assert 'other' in query.call_args_list[2].args[2]
