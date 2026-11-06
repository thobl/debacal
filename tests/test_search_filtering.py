"""Search and optional calendar filtering share resource/occurrence semantics."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.search_panel import SearchPanel, SUBTITLE_ROLE, event_matches_query
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def event(**changes):
    return replace(Event('series', 'local', 'Planning', START, START + timedelta(hours=1),
                         description='Weekly notes', location='Office'), **changes)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store, patch('debacal.search_panel.local_zone', return_value=ZONE):
        panel = SearchPanel(store, Preferences(date_format='iso', time_format='24h'))
        panel.set_range(START, START + timedelta(days=7))
        try:
            yield store, panel
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


@pytest.mark.parametrize('field', ['title', 'description', 'location'])
def test_filter_matches_casefolded_substring_in_title_notes_or_location(field):
    source = event(**{field: 'Ein Besuch in der Straße'})
    assert event_matches_query(source, ' STRASSE ')
    assert not event_matches_query(source, 'unrelated')
    assert event_matches_query(source, '   ')


def test_matching_master_includes_overrides_but_override_only_hit_is_date_specific(workspace):
    store, _ = workspace
    master = event(recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create', [master])
    original = store.query_occurrences(START, START + timedelta(days=4))
    store.edit_occurrence(original[1], replace(original[1], title='Dentist', description='Private appointment', location='Clinic'))
    values = store.query_occurrences(START, START + timedelta(days=4))
    current = store.get_event(master.id)
    assert all(event_matches_query(value, 'planning', current) for value in values)
    assert [value.id for value in values if event_matches_query(value, 'clinic', current)] == [original[1].id]
    assert not event_matches_query(values[1], 'planning')


@pytest.mark.parametrize('query,expected', [('planning', 3), ('clinic', 1)])
def test_filtered_occurrences_agree_with_dated_rows_under_matching_search_parent(workspace, query, expected):
    store, panel = workspace
    master = event(recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create', [master])
    original = store.query_occurrences(START, START + timedelta(days=4))
    store.edit_occurrence(original[1], replace(original[1], title='Dentist', description='Private', location='Clinic'))
    current = store.get_event(master.id)
    values = store.query_occurrences(START, START + timedelta(days=4))
    matching = {value.id for value in values if event_matches_query(value, query, current)}
    panel.populate(query)
    root = panel.item(0)
    root.setExpanded(True)
    children = {panel.event_id(root.child(index)) for index in range(root.childCount()) if panel.event_id(root.child(index))}
    assert len(matching) == expected and children == matching


def test_filtering_is_pure_and_never_expands_a_master():
    master = event(recurrence_rule='FREQ=WEEKLY')
    before = master.to_dict()
    with patch('debacal.recurrence.expand_series', side_effect=AssertionError('No expansion')):
        assert event_matches_query(master, 'notes')
        assert SearchPanel.matches_event(master, 'office')
    assert master.to_dict() == before


def test_repeating_suffix_removed_and_tooltips_show_safe_full_details_and_row_scope(workspace):
    store, panel = workspace
    calendar = Calendar('work', 'Work <home>', '#123456')
    store.save_calendar(calendar)
    master = event(title='A long <b>meeting</b> & notes', calendar_id=calendar.id,
                   location='Room & wing', recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create', [master])
    panel.set_range(START + timedelta(days=1), START + timedelta(days=7))
    panel.populate('meeting')
    root = panel.item(0)
    assert 'repeating' not in root.data(SUBTITLE_ROLE)
    assert '2026-11-05' in root.data(SUBTITLE_ROLE)
    tooltip = root.toolTip(0)
    assert '&lt;b&gt;meeting&lt;/b&gt;' in tooltip and 'Work &lt;home&gt;' in tooltip
    assert 'Room &amp; wing' in tooltip and '2026-11-05' in tooltip
    assert 'whole series' in tooltip and '<b>meeting</b>' not in tooltip
    root.setExpanded(True)
    child = root.child(0)
    assert 'repeating' not in child.data(SUBTITLE_ROLE)
    assert 'Select this occurrence.' in child.toolTip(0)
