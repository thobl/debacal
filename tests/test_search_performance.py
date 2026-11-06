"""Bounded search reads, stable rows and precise upcoming-cache behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import perf_counter
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import overlaps
from debacal.search_panel import SearchPanel, COLOR_ROLE, event_search_text
from debacal.store import Store


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def event(index=0, **changes):
    start = START + timedelta(minutes=index)
    return replace(Event(f'event-{index}', 'local', f'Project {index}', start,
                         start + timedelta(minutes=30)), **changes)


class ProjectionStore:
    """Expose only the bounded public APIs used by the optimized panel."""
    def __init__(self, events):
        self.events = {value.id: value for value in events}
        self.calendars = [Calendar('local', 'Local', '#123456')]
        self.version = 1
        self.index_reads = 0
        self.model_reads = []
        self.upcoming_reads = []

    def events_revision(self):
        return self.version

    def list_calendars(self):
        return self.calendars

    def event_search_index(self):
        self.index_reads += 1
        return [(value.id, value.calendar_id, event_search_text(value) + '\0' + '\0'.join(
                 ' '.join(str(payload.get(field, '') or '') for field in
                          ('title', 'description', 'location')).casefold()
                 for key, payload in value.recurrence_overrides.items() if key not in value.excluded_dates))
                for value in self.events.values()]

    def get_events(self, identifiers):
        self.model_reads.append(tuple(identifiers))
        return [self.events[key] for key in reversed(identifiers) if key in self.events]

    def list_events(self, *args, **kwargs):
        raise AssertionError('Search must not decode the complete event table.')

    def query_occurrences(self, *args, **kwargs):
        raise AssertionError('Agenda must not expand its complete date range.')

    def upcoming_occurrences(self, start, end, calendars, *, limit):
        self.upcoming_reads.append((start, end, tuple(calendars), limit))
        return sorted((value for value in self.events.values() if value.calendar_id in calendars
                       and overlaps(value, start, end)), key=lambda value: (value.start, value.end, value.id))[:limit]


@pytest.fixture
def panel_factory():
    app = QApplication.instance() or QApplication([])
    panels = []
    def make(store):
        panel = SearchPanel(store, Preferences(date_format='iso', time_format='24h'))
        panel.set_range(START, START + timedelta(days=7))
        panels.append(panel)
        return panel
    yield make
    for panel in panels:
        panel.close()
        panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope='module')
def large_events():
    return [event(index) for index in range(50_000)]


def test_fifty_thousand_matches_fetch_one_bounded_batch_and_reuse_rows(panel_factory, large_events):
    store = ProjectionStore(large_events)
    panel = panel_factory(store)
    started = perf_counter()
    panel.populate('P')
    first = panel.item(0)
    panel.setCurrentItem(first)
    panel.set_row_selection({panel.event_id(first)})
    for query in ('Pr', 'Pro', 'Proj', 'Proje', 'Project'):
        panel.populate(query)
        assert panel.item(0) is first and panel.currentItem() is first
        assert panel.selected_row_ids() == {large_events[0].id}
    assert perf_counter() - started < 2
    assert store.index_reads == 1
    assert len(store.model_reads) == 1 and len(store.model_reads[0]) == 256
    # The complete candidate index must survive the 200-row display cap.
    panel.populate('Project 49999')
    assert panel.event_id(panel.item(0)) == large_events[-1].id
    assert panel.topLevelItemCount() == 1
    assert sum(map(len, store.model_reads)) == 257


def test_broad_projection_candidates_are_exactly_checked_beyond_first_batch(panel_factory):
    store = ProjectionStore([event(index, title='Other') for index in range(400)] + [event(400, title='Needle')])
    original = store.event_search_index
    def broad_index():
        return [(identity, calendar, text + '\0needle') for identity, calendar, text in original()]
    store.event_search_index = broad_index
    panel = panel_factory(store)
    panel.populate('needle')
    assert panel.topLevelItemCount() == 1
    assert panel.event_id(panel.item(0)) == 'event-400'


def test_revision_changes_and_missing_candidate_models_are_safe(panel_factory):
    store = ProjectionStore([event(0, title='Needle')])
    panel = panel_factory(store)
    panel.populate('needle')
    store.events['event-0'] = event(0, title='Replacement')
    store.version += 1
    panel.populate('needle')
    assert panel.event_id(panel.item(0)) is None
    panel.populate('replacement')
    assert panel.event_id(panel.item(0)) == 'event-0'
    assert store.index_reads == 2
    # A worker can delete an indexed row between projection and model reads.
    store.events.clear()
    store.version += 1
    store.event_search_index = lambda: [('vanished', 'local', 'needle')]
    panel.populate('needle')
    assert panel.event_id(panel.item(0)) is None


def test_calendar_presentation_changes_keep_index_and_exact_selection(panel_factory):
    store = ProjectionStore([event()])
    panel = panel_factory(store)
    panel.populate('project')
    first = panel.item(0)
    panel.setCurrentItem(first)
    panel.set_row_selection({'event-0'})
    store.calendars = [replace(store.calendars[0], name='Renamed', color='#abcdef')]
    assert panel.update_context(store.calendars, Preferences(date_format='european'))
    panel.populate('project')
    assert panel.item(0) is first and panel.currentItem() is first
    assert first.data(COLOR_ROLE) == '#abcdef' and 'Renamed' in first.toolTip(0)
    assert panel.selected_row_ids() == {'event-0'} and store.index_reads == 1


def test_old_daily_representatives_seek_once_then_reuse_across_typing(panel_factory):
    old = datetime(1980, 1, 1, 9, tzinfo=timezone.utc)
    values = [event(index, start=old, end=old + timedelta(hours=1), recurrence_rule='FREQ=DAILY') for index in range(1000)]
    store = ProjectionStore(values)
    panel = panel_factory(store)
    from debacal.search_panel import recurrence_set
    with patch('debacal.search_panel.recurrence_set', wraps=recurrence_set) as seeks:
        started = perf_counter()
        panel.populate('P')
        initial = seeks.call_count
        for query in ('Pr', 'Pro', 'Proj', 'Project'):
            panel.populate(query)
        assert perf_counter() - started < 2
        assert initial == 200 and seeks.call_count == initial
        assert panel.result_event(values[0].id).start == START
        parent = panel.item(0)
        parent.setExpanded(True)
        assert len([parent.child(i) for i in range(parent.childCount()) if panel.event_id(parent.child(i))]) == 10
        assert seeks.call_count == initial + 1


def test_expanded_dates_keep_context_selection_and_pages_after_navigation_and_invalidation(panel_factory):
    store = ProjectionStore([event(recurrence_rule='FREQ=DAILY;COUNT=500')])
    panel = panel_factory(store)
    context = START + timedelta(days=120)
    panel.set_range(context, context + timedelta(days=7))
    panel.populate('project')
    parent = panel.item(0)
    parent.setExpanded(True)
    panel.load_more(parent)
    child = parent.child(150)
    identifier = panel.event_id(child)
    panel.setCurrentItem(child)
    panel.set_row_selection({identifier})
    panel.set_range(context + timedelta(days=200), context + timedelta(days=207))
    panel.populate('proj')
    assert panel.item(0) is parent and panel.currentItem() is child
    assert panel.result_event(identifier).start == context + timedelta(days=150)
    panel.invalidate()
    panel.populate('proj')
    assert panel.item(0).isExpanded() and panel.item(0).childCount() == 201
    assert panel.selected_row_ids() == {identifier}
    assert panel.event_id(panel.currentItem()) == identifier
    assert panel.result_event(identifier).start == context + timedelta(days=150)


@pytest.mark.parametrize('agenda', [False, True])
def test_rebuild_restores_current_row_independently_of_explicit_selection(panel_factory, agenda):
    store = ProjectionStore([event(0), event(1)])
    panel = panel_factory(store)
    populate = (lambda: panel.populate_upcoming(START)) if agenda else (lambda: panel.populate('project'))
    populate()
    panel.setCurrentItem(panel.row_item('event-0'))
    panel.set_row_selection({'event-1'})
    assert panel.event_id(panel.currentItem()) == 'event-0'
    assert panel.selected_row_ids() == {'event-1'}
    panel.invalidate()
    populate()
    assert panel.event_id(panel.currentItem()) == 'event-0'
    assert panel.selected_row_ids() == {'event-1'}


def test_upcoming_refills_after_short_events_expire_without_full_range_query(panel_factory):
    values = [event(index, start=START + timedelta(seconds=index), end=START + timedelta(seconds=index)) for index in range(80)]
    store = ProjectionStore(values)
    panel = panel_factory(store)
    assert panel.populate_upcoming(START) == 10
    first = panel.item(0)
    panel.setCurrentItem(first)
    assert panel.populate_upcoming(START) == 10
    assert panel.item(0) is first and panel.currentItem() is first
    assert panel.populate_upcoming(START + timedelta(seconds=5)) == 10
    assert [panel.event_id(panel.item(i)) for i in range(10)] == [f'event-{i}' for i in range(5, 15)]
    assert len(store.upcoming_reads) == 1
    panel.populate_upcoming(START + timedelta(seconds=15))
    assert [panel.event_id(panel.item(i)) for i in range(10)] == [f'event-{i}' for i in range(15, 25)]
    assert len(store.upcoming_reads) == 2
    assert all(read[-1] == 20 for read in store.upcoming_reads)
    assert store.index_reads == 0 and not store.model_reads


def test_upcoming_cache_respects_exact_points_visibility_and_horizon(panel_factory):
    values = [event(index, start=START + timedelta(minutes=index), end=START + timedelta(minutes=index)) for index in range(12)]
    horizon = event(99, start=START + timedelta(days=30, seconds=1), end=START + timedelta(days=30, seconds=1))
    store = ProjectionStore([*values, horizon])
    panel = panel_factory(store)
    panel.populate_upcoming(START, limit=20)
    assert 'event-99' not in panel.selected_ids()
    assert panel.row_item('event-99') is None
    panel.populate_upcoming(START + timedelta(microseconds=1), limit=20)
    assert panel.row_item('event-0') is None
    panel.populate_upcoming(START + timedelta(seconds=2), limit=20)
    assert panel.row_item('event-99') is not None
    assert len(store.upcoming_reads) == 1
    store.calendars = [replace(store.calendars[0], visible=False)]
    panel.populate_upcoming(START + timedelta(seconds=2), limit=20)
    assert panel.event_id(panel.item(0)) is None
    assert len(store.upcoming_reads) == 2


def test_upcoming_visibility_contexts_are_reused_and_event_updates_invalidate_them(panel_factory):
    store = ProjectionStore([event(0), event(1, calendar_id='other')])
    store.calendars.append(Calendar('other', 'Other', '#654321'))
    panel = panel_factory(store)
    panel.populate_upcoming(START)
    assert panel.row_item('event-0') is not None and panel.row_item('event-1') is not None
    store.calendars[1] = replace(store.calendars[1], visible=False)
    panel.populate_upcoming(START)
    assert panel.row_item('event-1') is None
    store.calendars[1] = replace(store.calendars[1], visible=True)
    panel.populate_upcoming(START)
    assert panel.row_item('event-1') is not None
    assert len(store.upcoming_reads) == 2
    store.events['event-1'] = replace(store.events['event-1'], title='Updated')
    store.version += 1
    panel.populate_upcoming(START)
    assert panel.row_item('event-1').text() == 'Updated'
    assert len(store.upcoming_reads) == 3


def test_external_store_commit_invalidates_projection_without_explicit_panel_signal(panel_factory, tmp_path):
    path = tmp_path / 'shared.sqlite'
    with Store(path) as first, Store(path) as second:
        first.apply('Create', [event(title='Needle')])
        panel = panel_factory(first)
        panel.populate('needle')
        second.apply('Rename', [event(title='Replacement')])
        panel.populate('replacement')
        assert panel.item(0).text() == 'Replacement'
        panel.populate('needle')
        assert panel.event_id(panel.item(0)) is None


def test_legacy_store_without_projection_retains_full_query_and_override_matching(panel_factory):
    master = event(recurrence_rule='FREQ=DAILY;COUNT=3')
    from debacal.recurrence import expand_series
    moved = replace(expand_series(master, START, START + timedelta(days=4))[1], title='Detached needle')
    master = replace(master, recurrence_overrides={moved.recurrence_id: moved.to_dict()})
    class LegacyStore:
        def list_calendars(self):
            return [Calendar('local', 'Local', '#123456')]
        def list_events(self):
            return [master]
    panel = panel_factory(LegacyStore())
    panel.populate('detached')
    assert panel.result_event(master.id).id == moved.id
    panel.populate('project')
    panel.item(0).setExpanded(True)
    assert len([panel.item(0).child(i) for i in range(panel.item(0).childCount())
                if panel.event_id(panel.item(0).child(i))]) == 3
