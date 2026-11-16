"""A year paint/filter/selection pass must not materialize unrelated families."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow
from debacal.year_projection import YearProjection
from debacal.year_view import YearView


START = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def dispose(app, widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


class CompactEvents:
    """Small protocol double which fails on accidental full materialization."""
    def __init__(self, events, *, first_day=date(2026, 1, 1)):
        self.zone = timezone.utc
        self.first_day = first_day
        self.known_events = tuple(event for event in events if not event.series_id)
        self._events = {event.id: event for event in events}
        self.summaries = [{} for _ in range((date(2027, 1, 1) - first_day).days)]
        for event in events:
            self.summaries[(event.start.date() - first_day).days].setdefault(event.calendar_id, (1, ''))
        self.lookups, self.selected, self.days = [], [], []

    def __iter__(self):
        raise AssertionError('The year overview must not expand every event')

    def __len__(self):
        return len(self._events)

    def selected_events(self, identifiers):
        self.selected.append(set(identifiers))
        return [self._events[identity] for identity in identifiers if identity in self._events]

    def event_by_id(self, identity):
        self.lookups.append(identity)
        return self._events.get(identity)

    def events_on_day(self, day, *, limit=100):
        self.days.append((day, limit))
        return [event for event in self._events.values() if event.start.date() == day][:limit]

    def without_calendars(self, identifiers):
        return type(self)([event for event in self._events.values() if event.calendar_id not in identifiers],
                          first_day=self.first_day)


def test_year_widget_uses_counts_and_resolves_only_current_selection(app):
    ordinary = Event('ordinary', 'local', 'Ordinary', START, START + timedelta(hours=1))
    recurring = replace(ordinary, id='occurrence', title='Daily', series_id='master')
    projection = CompactEvents([ordinary, recurring])
    view = YearView()
    try:
        with patch.object(view, '_event_interval', wraps=view._event_interval) as intervals:
            view.set_data(2026, projection, [Calendar('local', 'Local', '#123456')])
            assert intervals.call_count == 0
            assert view.events is projection
            assert set(view._event_map) == {'ordinary'}
            assert 'Local' in view._day_tooltip(START.date())
            view.resize(1040, view.heightForWidth(1040))
            view.show()
            app.processEvents()
            assert intervals.call_count == 0
            view.set_selection({'occurrence'})
            assert intervals.call_count == 1
            assert view._selected_summaries[(START.date() - date(2026, 1, 1)).days]
            assert view.event_rect('occurrence') == view.day_rect(START.date())
            assert projection.lookups == []
            assert view.events_on_day(START.date(), limit=1) == [ordinary]
            assert projection.days == [(START.date(), 1)]
            view.set_selection(set())
            assert set(view._event_map) == {'ordinary'}
            assert not view._intervals
            assert view.event_rect('occurrence') == view.day_rect(START.date())
            assert projection.lookups == ['occurrence']
    finally:
        dispose(app, view)


def test_compact_counts_align_partial_ranges_and_remove_disabled_sources(app):
    values = [Event(identity, identity, identity, START, START + timedelta(hours=1))
              for identity in ('local', 'hidden', 'disabled')]
    projection = CompactEvents(values, first_day=date(2026, 8, 1))
    sources = [Calendar('local', 'Local', '#123456'),
               Calendar('hidden', 'Hidden', '#abcdef', visible=False),
               Calendar('disabled', 'Disabled', '#fedcba', enabled=False)]
    view = YearView()
    try:
        view.set_data(2026, projection, sources)
        assert view._summaries[0] == {}
        assert view.calendars_on_day(START.date()) == ('local',)
        assert set(view._event_map) == {'local'}
        view.set_selection({'local', 'hidden', 'disabled'})
        assert set(view._selected_summaries[(START.date() - date(2026, 1, 1)).days]) == {'local'}
        assert [event.id for event in view.events_on_day(START.date())] == ['local']
    finally:
        dispose(app, view)


@pytest.fixture
def workspace(app):
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START):
        values = [Event('ordinary', 'local', 'Work ordinary', START, START + timedelta(hours=1)),
                  Event('daily', 'local', 'Work daily', START, START + timedelta(hours=1),
                        recurrence_rule='FREQ=DAILY;COUNT=400')]
        store.apply_remote(values)
        window = MainWindow(store, enable_sync=False)
        try:
            yield app, store, window
        finally:
            window.executor.shutdown(wait=True)
            dispose(app, window)


def test_year_refresh_filter_search_and_selection_never_iterate_full_projection(workspace):
    _, store, window = workspace
    with (patch.object(YearProjection, '__iter__', side_effect=AssertionError('Do not expand the year')),
          patch.object(YearProjection, '__getitem__', side_effect=AssertionError('Do not index the year')),
          patch.object(window.canvas, 'set_data', side_effect=AssertionError('Hidden week stays untouched')),
          patch.object(window.month, 'set_data', side_effect=AssertionError('Hidden month stays untouched')),
          patch.object(store, 'query_year_presence', wraps=store.query_year_presence) as queries):
        window.set_view('year')
        assert isinstance(window.visible_events, YearProjection)
        event = next(event for event in window.year.events_on_day(START.date()) if event.series_id)
        window.select_event(event.id)
        assert window.selected_ids == {event.id}
        window.set_selection_mode('group')
        assert len(window.selected_ids) > 100
        window.search.setText('Work daily')
        window.filter_search.setChecked(True)
        window.apply_search_filter()
        assert isinstance(window.visible_events, YearProjection)
        assert window.visible_event_by_id('ordinary') is None
        assert window.visible_event_by_id(event.id).series_id == 'daily'
        assert window.reveal_search_result(event.id)
        window.refresh()
        assert queries.call_count == 2
        assert all(value.series_id == 'daily' for value in window.visible_events_for_sources({'daily'}))


def test_appearance_reuses_only_the_current_unchanged_display_and_agenda(workspace):
    _, store, window = workspace
    window.set_view('year')
    with (patch.object(store, 'query_year_presence', wraps=store.query_year_presence) as queries,
          patch.object(window.results, 'populate_upcoming', wraps=window.results.populate_upcoming) as upcoming):
        projection = window.visible_events
        window.apply_preferences(replace(window.preferences, year_view_style='tints'), window.bindings)
        assert window.visible_events is projection
        assert queries.call_count == upcoming.call_count == 0
        window.refresh()
        assert queries.call_count == upcoming.call_count == 1
        assert window.visible_events is not projection
        changed = replace(store.get_event('ordinary'), title='Changed')
        store.apply_remote([changed])
        window.apply_preferences(replace(window.preferences, font_scale=110), window.bindings)
        assert queries.call_count == upcoming.call_count == 2
        assert window.visible_event_by_id(changed.id).title == 'Changed'
        store.set_calendar_visible('local', False)
        window.apply_preferences(replace(window.preferences, font_scale=105), window.bindings)
        assert window.visible_events == []
        assert upcoming.call_count == 3


def test_first_weekday_change_refreshes_new_range_and_hidden_views_on_activation(workspace):
    _, store, window = workspace
    with patch.object(store, 'query_occurrences', wraps=store.query_occurrences) as queries:
        window.apply_preferences(replace(window.preferences, start_weekday=6), window.bindings)
        assert queries.call_count == 1
        assert window.canvas.days[0].weekday() == 6
        window.set_view('month')
        assert queries.call_count == 2
        assert window.month.start.weekday() == 6
        assert window.month.preferences == window.preferences
        with patch.object(window.canvas, 'set_data', side_effect=AssertionError('Month has no hidden week layout')):
            window.refresh()
        window.set_view('week')
        assert window.canvas.days == window.dates()


def test_year_arrow_navigation_preserves_canonical_order_without_expanding_year(workspace):
    _, store, window = workspace
    midnight = datetime(2026, 8, 7, tzinfo=timezone.utc)
    evening = datetime(2026, 8, 6, 23, tzinfo=ZoneInfo('America/New_York'))
    store.apply_remote([
        Event('date', 'local', 'All day', midnight, midnight + timedelta(days=1), all_day=True),
        Event('evening', 'local', 'Late appointment', evening, evening + timedelta(minutes=30)),
    ])
    window.set_view('year')
    projection = window.visible_events
    ordered = store.query_occurrences(projection.start, projection.end, ['local'])
    identifiers = [event.id for event in ordered]
    with (patch.object(YearProjection, '__iter__', side_effect=AssertionError('Arrow must not expand the year')),
          patch.object(YearProjection, '__getitem__', side_effect=AssertionError('Arrow must not index the year'))):
        window.selection.clear()
        window.sync_selection()
        window.select_by_arrow('down')
        assert window.selected_id == identifiers[0]
        for expected in identifiers[1:6]:
            window.select_by_arrow('down')
            assert window.selected_id == expected
        for expected in reversed(identifiers[:5]):
            window.select_by_arrow('up')
            assert window.selected_id == expected
        window.select_by_arrow('up')
        assert window.selected_id == identifiers[-1]
        window.select_by_arrow('down')
        assert window.selected_id == identifiers[0]
        window.select_event('ordinary')
        window.select_by_arrow('right')
        chosen = window.event_by_id(window.selected_id)
        assert (chosen.start.date() if chosen.all_day else chosen.start.astimezone(projection.zone).date()) == date(2026, 8, 7)
        window.select_by_arrow('left')
        chosen = window.event_by_id(window.selected_id)
        assert (chosen.start.date() if chosen.all_day else chosen.start.astimezone(projection.zone).date()) == START.date()
