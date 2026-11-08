"""Search hierarchy, occurrence paging and calendar selection signals."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from time import perf_counter
import subprocess
import sys
import textwrap
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_series
from debacal.search_panel import SearchPanel, SUBTITLE_ROLE, COLOR_ROLE, MORE_ROLE, impossible_monthday_filter
from debacal.store import Store

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    errors = []
    with patch('debacal.search_panel.local_zone', return_value=ZONE), \
         patch('debacal.search_panel.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        panel = SearchPanel(store, Preferences())
        panel.resize(390, 600)
        panel.set_range(START, START + timedelta(days=7))
        panel.show()
        app.processEvents()
        yield app, store, panel
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def event(identifier='series', **kwargs):
    return Event(identifier, 'local', 'Needle meeting', START, START + timedelta(hours=1), **kwargs)


def genuine_children(parent):
    return [parent.child(index) for index in range(parent.childCount()) if SearchPanel.event_id(parent.child(index))]


def test_one_parent_per_series_and_colored_two_line_presentation(workspace):
    _, store, panel = workspace
    calendar = Calendar('colored', 'Synthetic calendar', '#123abc')
    store.save_calendar(calendar)
    master = replace(event(recurrence_rule='FREQ=DAILY;COUNT=3'), calendar_id=calendar.id)
    store.apply('Create', [master, event('ordinary')])
    panel.populate('needle')
    assert panel.topLevelItemCount() == 2
    parent = next(panel.item(index) for index in range(panel.count()) if panel.event_id(panel.item(index)) == master.id)
    assert parent.childCount() == 0
    assert parent.text() == master.title
    assert 'repeating' not in parent.data(SUBTITLE_ROLE)
    assert parent.data(COLOR_ROLE) == calendar.color
    assert calendar.name in parent.toolTip(0)
    assert master.title in parent.toolTip(0)
    parent.setExpanded(True)
    children = genuine_children(parent)
    assert len(children) == 3
    assert [store.get_occurrence(panel.event_id(item)).start for item in children] == [START + timedelta(days=day) for day in range(3)]
    assert all(panel.event_id(item) != master.id for item in children)


@pytest.mark.parametrize('agenda', [False, True])
def test_native_event_rows_expose_date_time_source_and_location_without_color_only_meaning(workspace, agenda):
    _, store, panel = workspace
    calendar = Calendar('colored', 'Synthetic calendar', '#123abc')
    store.save_calendar(calendar)
    source = replace(event(location='Room 42', recurrence_rule='FREQ=DAILY;COUNT=3'), calendar_id=calendar.id)
    store.apply('Create', [source])
    if agenda:
        panel.populate_upcoming(START)
        rows = [panel.topLevelItem(0)]
    else:
        panel.populate('needle')
        root = panel.topLevelItem(0)
        root.setExpanded(True)
        rows = [root, root.child(0)]
    for item in rows:
        assert item.data(Qt.ItemDataRole.AccessibleTextRole) == source.title
        details = item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        assert panel.preferences.format_date(START.date(), weekday=True) in details
        assert panel.preferences.format_time(START) in details
        assert calendar.name in details and source.location in details
    renamed = replace(calendar, name='New calendar alias')
    panel.update_context([renamed], replace(panel.preferences, time_format='12h'))
    for item in rows:
        details = item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
        assert renamed.name in details and calendar.name not in details
        assert panel.preferences.format_time(START) in details


def test_native_row_describes_repeated_hour_endpoints_and_true_elapsed_duration(workspace):
    _, store, panel = workspace
    start = datetime(2026, 10, 25, 2, 45, tzinfo=ZONE)
    end = datetime(2026, 10, 25, 2, 15, tzinfo=ZONE, fold=1)
    source = replace(event(), start=start, end=end)
    store.apply('Create', [source])
    panel.populate('needle')
    item = panel.topLevelItem(0)
    details = item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    assert '02:45 CEST' in details and '02:15 CET' in details
    assert '30min elapsed' in details
    assert '30min elapsed' in item.toolTip(0)


def test_finite_series_pages_preserve_all_distinct_occurrence_ids(workspace):
    _, store, panel = workspace
    store.apply('Create', [event(recurrence_rule='FREQ=DAILY;COUNT=250')])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(genuine_children(parent)) == 100
    assert parent.child(parent.childCount() - 1).data(MORE_ROLE) is True
    panel.load_more(parent)
    assert len(genuine_children(parent)) == 200
    panel.load_more(parent)
    children = genuine_children(parent)
    assert len(children) == len({panel.event_id(item) for item in children}) == 250
    assert parent.childCount() == 250


def test_unbounded_series_has_fixed_page_cap_and_bounded_context_seek(workspace):
    _, store, panel = workspace
    old = datetime(1980, 1, 1, 9, tzinfo=ZONE)
    master = replace(event(recurrence_rule='FREQ=HOURLY'), start=old, end=old + timedelta(hours=1))
    store.apply('Create', [master])
    started = perf_counter()
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert perf_counter() - started < 2
    assert len(genuine_children(parent)) == 10
    for _ in range(12):
        panel.load_more(parent)
    assert len(genuine_children(parent)) == 100
    assert parent.childCount() == 101
    assert panel.event_id(parent.child(100)) is None
    assert not parent.child(100).flags() & Qt.ItemFlag.ItemIsSelectable


@pytest.mark.parametrize('activation', ['mouse', 'keyboard'])
def test_capped_series_can_explicitly_restart_near_view_without_rebasing_other_results(workspace, activation):
    app, store, panel = workspace
    first, other = event('first', recurrence_rule='FREQ=DAILY'), event('other', recurrence_rule='FREQ=DAILY')
    store.apply('Create', [first, other])
    panel.populate('needle')
    parent, other_parent = panel.topLevelItem(0), panel.topLevelItem(1)
    parent.setExpanded(True)
    other_parent.setExpanded(True)
    for _ in range(9):
        panel.load_more(parent)
    panel.load_more(other_parent)
    old_ids = [panel.event_id(child) for child in genuine_children(parent)]
    other_ids = [panel.event_id(child) for child in genuine_children(other_parent)]
    assert len(old_ids) == 100
    later = START + timedelta(days=500)
    panel.set_range(later, later + timedelta(days=7))
    panel.populate('needle')
    parent.setExpanded(False)
    parent.setExpanded(True)
    assert [panel.event_id(child) for child in genuine_children(parent)] == old_ids
    # Even rebuilding a query keeps ordinary result-following stable.
    panel.populate('')
    panel.populate('needle')
    parent, other_parent = panel.topLevelItem(0), panel.topLevelItem(1)
    assert [panel.event_id(child) for child in genuine_children(parent)] == old_ids
    footer = parent.child(parent.childCount() - 1)
    assert footer.text() == 'Show dates near current view'
    assert footer.flags() & Qt.ItemFlag.ItemIsEnabled
    emitted = []
    panel.result_selected.connect(lambda *values: emitted.append(values))
    panel.scrollToItem(footer)
    app.processEvents()
    if activation == 'mouse':
        QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, pos=panel.visualItemRect(footer).center())
    else:
        panel.setCurrentItem(footer)
        QTest.keyClick(panel, Qt.Key.Key_Return)
    children = genuine_children(parent)
    assert len(children) == 10
    assert panel.result_event(panel.event_id(children[0])).start == later
    assert panel.result_event(panel.event_id(parent)).start == later
    assert [panel.event_id(child) for child in genuine_children(other_parent)] == other_ids
    assert all(panel.row_item(identity) is None for identity in old_ids)
    assert not emitted


def test_lazy_dates_start_near_requested_calendar_range(workspace):
    _, store, panel = workspace
    master = event(recurrence_rule='FREQ=DAILY;COUNT=500')
    store.apply('Create', [master])
    context = START + timedelta(days=120)
    panel.set_range(context, context + timedelta(days=7))
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert store.get_occurrence(panel.event_id(parent.child(0))).start == context


def test_selection_payloads_cache_parent_representative_and_off_page_children(workspace):
    _, store, panel = workspace
    master = event(recurrence_rule='FREQ=DAILY;COUNT=300')
    store.apply('Create', [master])
    context = START + timedelta(days=120)
    panel.set_range(context, context + timedelta(days=7))
    panel.populate('needle')
    parent = panel.item(0)
    representative = panel.result_event(master.id)
    assert representative.series_id == master.id and representative.start == context
    assert panel.selection_targets() == [(master.id, representative)]
    parent.setExpanded(True)
    panel.load_more(parent)
    children = genuine_children(parent)
    last_id = panel.event_id(children[-1])
    with patch.object(store, 'get_occurrence', wraps=store.get_occurrence) as reads:
        targets = dict(panel.selection_targets())
        assert targets[master.id] == representative
        assert targets[last_id].start > context + timedelta(days=7)
        assert panel.result_event(last_id) == targets[last_id]
        assert not reads.called


def test_matching_detached_override_is_grouped_under_its_master(workspace):
    _, store, panel = workspace
    master = replace(event(recurrence_rule='FREQ=WEEKLY;COUNT=3'), title='Original title')
    store.apply('Create', [master])
    selected = expand_series(master, START, START + timedelta(days=30))[1]
    moved = replace(selected, title='Needle override', start=selected.start + timedelta(days=40), end=selected.end + timedelta(days=40))
    store.edit_occurrence(selected, moved)
    panel.populate('needle')
    assert panel.topLevelItemCount() == 1
    parent = panel.item(0)
    assert parent.text() == master.title
    assert panel.event_id(parent) == master.id
    assert panel.result_event(master.id).id == moved.id
    parent.setExpanded(True)
    assert len(genuine_children(parent)) == 1
    assert panel.event_id(parent.child(0)) == moved.id
    assert 'Needle override' in parent.child(0).text()


def test_query_cache_and_expanded_pages_survive_refresh(workspace):
    _, store, panel = workspace
    store.apply('Create', [event(recurrence_rule='FREQ=DAILY;COUNT=250')])
    with patch.object(store, 'event_search_index', wraps=store.event_search_index) as reads:
        panel.populate('needle')
        parent = panel.item(0)
        parent.setExpanded(True)
        panel.load_more(parent)
        panel.populate('need')
        assert reads.call_count == 1
        assert panel.item(0).isExpanded()
        assert len(genuine_children(panel.item(0))) == 200
        panel.invalidate()
        panel.populate('need')
        assert reads.call_count == 2
        assert panel.item(0).isExpanded()
        assert len(genuine_children(panel.item(0))) == 200


def test_selection_sync_is_silent_and_cycle_visits_only_visible_event_rows(workspace):
    _, store, panel = workspace
    master = event(recurrence_rule='FREQ=DAILY;COUNT=3')
    store.apply('Create', [master])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    emitted = []
    panel.result_selected.connect(lambda identifier, modifiers: emitted.append((identifier, modifiers)))
    selected = panel.event_id(parent.child(1))
    panel.set_selection({selected})
    assert panel.selected_ids() == {selected}
    assert not emitted
    visited = [panel.cycle(1) for _ in range(5)]
    assert visited[:4] == [master.id, *(panel.event_id(parent.child(index)) for index in range(3))]
    assert visited[-1] == master.id
    parent.setExpanded(False)
    assert panel.count() == 1
    assert panel.cycle(1) == master.id


def test_ctrl_click_emits_explicit_modifier_and_keeps_native_multiselection(workspace):
    app, store, panel = workspace
    store.apply('Create', [event('one'), event('two')])
    panel.populate('needle')
    emitted = []
    panel.result_selected.connect(lambda identifier, modifiers: emitted.append((identifier, modifiers)))
    panel.setCurrentRow(0)
    app.processEvents()
    rect = panel.visualItemRect(panel.item(1))
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, rect.center())
    assert emitted[-1] == ('two', Qt.KeyboardModifier.ControlModifier)
    assert panel.selected_ids() == {'one', 'two'}


def test_empty_and_limit_placeholders_are_not_cycle_targets(workspace):
    _, store, panel = workspace
    panel.populate('missing')
    assert panel.count() == 1 and panel.cycle(1) is None
    store.apply('Create many', [event(f'item-{index}') for index in range(201)])
    panel.invalidate()
    panel.populate('needle')
    assert panel.topLevelItemCount() == 201
    assert panel.event_id(panel.item(200)) is None
    panel.setCurrentRow(199)
    assert panel.cycle(1) == panel.event_id(panel.item(0))


@pytest.mark.parametrize(('rule', 'impossible'), [
    ('FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30', True),
    ('FREQ=DAILY;BYMONTH=2,4;BYMONTHDAY=31', True),
    ('FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=-30,-31', True),
    ('FREQ=DAILY;BYMONTH=1;BYMONTHDAY=32', True),
    ('FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29', False),
    ('FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-29', False),
    ('FREQ=DAILY;BYMONTH=1,2;BYMONTHDAY=31', False),
    ('FREQ=MONTHLY;BYMONTHDAY=32', False),
    ('FREQ=HOURLY;BYMONTH=2', False),
])
def test_impossible_monthday_guard_is_narrow_and_handles_leap_and_negative_days(rule, impossible):
    assert impossible_monthday_filter(rule) is impossible


def test_empty_hourly_monthday_generator_keeps_explicit_dates_without_hanging_or_mutating_store():
    script = textwrap.dedent('''
        from datetime import datetime, timedelta, timezone
        from PySide6.QtWidgets import QApplication
        from debacal.models import Event
        from debacal.preferences import Preferences
        from debacal.search_panel import SearchPanel
        from debacal.store import Store
        app = QApplication([])
        store = Store(':memory:')
        start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
        explicit = (start + timedelta(days=1), start + timedelta(days=2))
        rule = 'FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30'
        source = Event('synthetic', 'local', 'Needle', start, start + timedelta(hours=1),
                       recurrence_rule=rule, recurrence_dates=tuple(value.isoformat() for value in explicit),
                       excluded_dates=(start.isoformat(),))
        store.apply('Create source', [source])
        pending = store.pending_changes()
        panel = SearchPanel(store, Preferences())
        panel.set_range(start, start + timedelta(days=7))
        panel.populate('needle')
        parent = panel.item(0)
        parent.setExpanded(True)
        dates = [panel.result_event(panel.event_id(parent.child(index))).start
                 for index in range(parent.childCount()) if panel.event_id(parent.child(index))]
        assert dates == list(explicit)
        assert store.get_event(source.id).recurrence_rule == rule
        assert store.pending_changes() == pending
        assert len(store.history()) == 1
        assert not store.list_quarantined_resources()
        panel.close()
        store.close()
    ''')
    result = subprocess.run([sys.executable, '-c', script], env=os.environ | {'QT_QPA_PLATFORM': 'offscreen'},
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
