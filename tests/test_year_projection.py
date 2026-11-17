"""Compact year presence agrees with ordinary occurrence queries and lazy details."""
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Calendar, Event
from debacal.prepared_recurrence import PreparedSeries
from debacal.recurrence import RecurrenceRangeError, overlaps, recurrence_key
from debacal.search_panel import event_matches_query
from debacal.store import Store


UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')


def event(identifier='synthetic', start=None, end=None, **changes):
    start = start or datetime(2026, 1, 1, 9, tzinfo=BERLIN)
    return Event(identifier, changes.pop('calendar_id', 'local'), changes.pop('title', 'Synthetic appointment'),
                 start, end if end is not None else start + timedelta(hours=1), **changes)


def signature(value):
    return (value.id, value.calendar_id, value.title, value.start.timestamp(), value.end.timestamp(),
            value.all_day, value.floating_time, value.duration_spec, value.recurrence_id)


def assert_projection_matches(store, start, end, zone, *, calendars=None, query=''):
    with patch('debacal.timeutil.local_zone', return_value=zone):
        expected = store.query_occurrences(start, end, calendars)
        projection = store.query_year_presence(start, end, calendars)
        if query:
            expected = [value for value in expected if event_matches_query(
                value, query, store.get_event(value.series_id or value.id))]
            projection = projection.filtered(query)
        assert len(projection) == len(expected)
        assert [signature(value) for value in projection] == [signature(value) for value in expected]
        days = (end.date() - start.date()).days + bool(end.time() != time.min)
        assert len(projection.summaries) == days
        for offset in range(days):
            day = start.date() + timedelta(days=offset)
            lower = datetime.combine(day, time.min, zone)
            upper = datetime.combine(day + timedelta(days=1), time.min, zone)
            details = [value for value in expected if overlaps(value, lower, upper)]
            counts = Counter(value.calendar_id for value in details)
            assert projection.summaries[offset] == {identifier: (count, '') for identifier, count in counts.items()}
            assert [signature(value) for value in projection.events_on_day(day, limit=1000)] == [
                signature(value) for value in details]
        assert set(value.id for value in projection.known_events) <= {value.id for value in expected}
        assert [signature(projection.event_by_id(value.id)) for value in expected] == [
            signature(value) for value in expected]
        return projection, expected


@pytest.mark.parametrize('kind', ['daily', 'weekly', 'dst', 'nominal', 'cut', 'all_day',
                                 'floating', 'point', 'explicit', 'finite'])
def test_summary_counts_and_lazy_events_match_standard_queries(kind):
    zone = BERLIN
    start = datetime(2026, 1, 1, 9, tzinfo=zone)
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400')
    lower, upper = datetime(2026, 1, 1, tzinfo=zone), datetime(2026, 1, 12, tzinfo=zone)
    if kind == 'daily':
        source = replace(source, recurrence_dates=(start.isoformat(), (start + timedelta(days=2)).isoformat(),
                                                   (start + timedelta(days=4, hours=2)).isoformat()))
    elif kind == 'weekly':
        source = replace(source, recurrence_rule='FREQ=WEEKLY;COUNT=400',
                         excluded_dates=((start + timedelta(days=7)).isoformat(),))
        upper = datetime(2026, 2, 2, tzinfo=zone)
    elif kind == 'dst':
        start = datetime(2026, 10, 20, 2, 30, tzinfo=zone)
        source = event(start=start, recurrence_rule='FREQ=DAILY;COUNT=400')
        lower, upper = datetime(2026, 10, 23, tzinfo=zone), datetime(2026, 10, 29, tzinfo=zone)
    elif kind == 'nominal':
        start = datetime(2026, 3, 25, 10, tzinfo=zone)
        source = event(start=start, end=start + timedelta(days=1), duration_spec='P1D',
                       recurrence_rule='FREQ=DAILY;COUNT=400')
        lower, upper = datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 4, 3, tzinfo=zone)
    elif kind == 'cut':
        start = datetime(2026, 1, 28, tzinfo=zone)
        source = event(start=start, end=start + timedelta(days=3), all_day=True,
                       recurrence_rule='FREQ=MONTHLY;COUNT=400', range_policy='cut')
        lower, upper = datetime(2026, 2, 1, tzinfo=zone), datetime(2026, 3, 5, tzinfo=zone)
    elif kind == 'all_day':
        start = datetime(2026, 1, 1, tzinfo=zone)
        source = event(start=start, end=start + timedelta(days=2), all_day=True,
                       recurrence_rule='FREQ=DAILY;COUNT=400')
    elif kind == 'floating':
        source = replace(source, floating_time=True)
        zone = ZoneInfo('America/New_York')
        lower, upper = lower.replace(tzinfo=zone), upper.replace(tzinfo=zone)
    elif kind == 'point':
        source = replace(source, end=source.start)
    elif kind == 'explicit':
        source = replace(source, recurrence_rule='',
                         recurrence_dates=tuple((start + timedelta(days=offset * 2)).isoformat()
                                                for offset in range(1, 401)))
    elif kind == 'finite':
        source = replace(source, recurrence_rule='FREQ=DAILY;COUNT=5')
    with patch('debacal.timeutil.local_zone', return_value=zone), Store(':memory:') as store:
        store.apply_remote([source])
        assert_projection_matches(store, lower, upper, zone)


def test_detached_moves_filter_actual_ranges_and_titles_without_native_matches():
    start = datetime(2026, 1, 1, 9, tzinfo=BERLIN)
    inside_key = recurrence_key(start + timedelta(days=3))
    outside_key = recurrence_key(start + timedelta(days=40))
    moved_out = event('synthetic/out', start=start + timedelta(days=40),
                      series_id='synthetic', recurrence_id=inside_key, title='Outside the shown range')
    moved_in = event('synthetic/in', start=start + timedelta(days=4, hours=2),
                     series_id='synthetic', recurrence_id=outside_key, title='Needle detached appointment')
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400',
                   recurrence_overrides={inside_key: moved_out.to_dict(), outside_key: moved_in.to_dict()})
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 10, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        assert_projection_matches(store, lower, upper, BERLIN)
        projected, values = assert_projection_matches(store, lower, upper, BERLIN, query='Needle')
        assert len(values) == 1 and values[0].title == moved_in.title
        with patch.object(PreparedSeries, 'query_rule', side_effect=AssertionError('Native source does not match')):
            assert projected.events_on_day(date(2026, 1, 5))[0].title == moved_in.title


def test_distinct_recurrence_identities_at_the_same_actual_start_are_counted_separately():
    start = datetime(2026, 1, 1, 9, tzinfo=BERLIN)
    second = recurrence_key(start + timedelta(days=1))
    duplicate = event('synthetic/duplicate', start=start, series_id='synthetic', recurrence_id=second)
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400', recurrence_overrides={second: duplicate.to_dict()})
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        projection, expected = assert_projection_matches(
            store, datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 4, tzinfo=BERLIN), BERLIN)
        assert projection.summaries[0]['local'][0] == 2
        assert expected[0].id != expected[1].id and expected[0].start.timestamp() == expected[1].start.timestamp()


def test_repeated_daylight_saving_hour_has_two_distinct_instant_events():
    first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=0)
    second = first.replace(fold=1)
    source = event(start=first, end=first + timedelta(minutes=10),
                   recurrence_dates=(second.isoformat(),))
    lower, upper = datetime(2026, 10, 25, tzinfo=BERLIN), datetime(2026, 10, 26, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        projection, expected = assert_projection_matches(store, lower, upper, BERLIN)
        assert projection.summaries == [{'local': (2, '')}]
        assert [value.start.timestamp() for value in expected] == [first.timestamp(), second.timestamp()]


def test_partial_day_window_preserves_civil_dates_for_all_day_and_instant_ranges():
    zone = ZoneInfo('America/New_York')
    lower, upper = datetime(2026, 1, 1, 23, 45, tzinfo=zone), datetime(2026, 1, 2, 0, 15, tzinfo=zone)
    # The DATE retains 2 January even though its source timezone is far ahead.
    date_start = datetime(2026, 1, 2, tzinfo=ZoneInfo('Pacific/Kiritimati'))
    values = [event('date', start=date_start, end=date_start + timedelta(days=1), all_day=True),
              event('midnight', start=lower, end=upper),
              event('instant-point', start=upper - timedelta(minutes=1), end=upper - timedelta(minutes=1))]
    with patch('debacal.timeutil.local_zone', return_value=zone), Store(':memory:') as store:
        store.apply_remote(values)
        projection, expected = assert_projection_matches(store, lower, upper, zone)
        assert len(expected) == 3
        assert projection.summaries == [{'local': (1, '')}, {'local': (3, '')}]


def test_calendar_filter_and_empty_selection_match_normal_query_semantics():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 10, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Synthetic other', '#123456'))
        store.apply_remote([event('first', recurrence_rule='FREQ=DAILY;COUNT=400'),
                            event('second', calendar_id='other', recurrence_rule='FREQ=DAILY;COUNT=400')])
        projection, _ = assert_projection_matches(store, lower, upper, BERLIN, calendars=['other'])
        assert all(set(day) == {'other'} for day in projection.summaries)
        empty, _ = assert_projection_matches(store, lower, upper, BERLIN, calendars=[])
        assert not empty and not empty.known_events


def test_over_budget_source_is_omitted_whole_including_its_explicit_anchor():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 11, tzinfo=BERLIN)
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400')
    healthy = event('healthy', title='Healthy ordinary event')
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source, healthy])
        with patch('debacal.event_range_index.OCCURRENCE_LIMIT', 5), patch('debacal.year_projection.OCCURRENCE_LIMIT', 5):
            expected_issues, actual_issues = [], []
            expected = store.query_occurrences(lower, upper, issues=expected_issues)
            projection = store.query_year_presence(lower, upper, issues=actual_issues)
            assert [value.id for value in projection] == [value.id for value in expected] == ['healthy']
            assert [master.id for master, _ in expected_issues] == [master.id for master, _ in actual_issues] == [source.id]
            assert projection.event_by_id(source.id) is None
            assert projection.summaries[0]['local'][0] == 1
            assert not projection.filtered('Synthetic appointment')
            with pytest.raises(RecurrenceRangeError):
                store.query_year_presence(lower, upper)


def test_rule_and_explicit_union_budget_deduplicates_shared_keys_before_counting():
    start = datetime(2026, 1, 1, 9, tzinfo=BERLIN)
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 6, tzinfo=BERLIN)
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400',
                   recurrence_dates=(start.isoformat(), (start + timedelta(days=2)).isoformat()))
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        with patch('debacal.event_range_index.OCCURRENCE_LIMIT', 5), patch('debacal.year_projection.OCCURRENCE_LIMIT', 5):
            assert len(store.query_year_presence(lower, upper)) == len(store.query_occurrences(lower, upper)) == 5
        store.apply_remote([replace(source, recurrence_dates=source.recurrence_dates + (
            (start + timedelta(days=3, hours=2)).isoformat(),))])
        with patch('debacal.event_range_index.OCCURRENCE_LIMIT', 5), patch('debacal.year_projection.OCCURRENCE_LIMIT', 5):
            issues = []
            projection = store.query_year_presence(lower, upper, issues=issues)
            assert not projection and len(issues) == 1 and not projection.known_events


def test_year_summary_and_native_ranges_allocate_no_occurrence_events_or_encoded_ids():
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400')
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2027, 1, 1, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        with patch.object(PreparedSeries, '_project_values', side_effect=AssertionError('Do not create occurrence Events')), \
             patch('debacal.recurrence.occurrence_id', side_effect=AssertionError('Do not encode occurrence IDs')), \
             patch('debacal.prepared_recurrence.occurrence_id', side_effect=AssertionError('Do not encode occurrence IDs')):
            projection = store.query_year_presence(lower, upper)
            record = projection._sources[source.id]
            assert len(list(record.prepared.iter_rule_ranges(lower, upper))) == 365
            assert len(projection) == 365
            assert len(projection.known_events) == 1
            assert all(day == {'local': (1, '')} for day in projection.summaries)


def test_selected_identity_and_selected_sources_generate_only_the_requested_family():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 12, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([event('first', recurrence_rule='FREQ=DAILY;COUNT=400'),
                            event('second', recurrence_rule='FREQ=DAILY;COUNT=400')])
        projection = store.query_year_presence(lower, upper)
        expected = next(value for value in store.query_occurrences(lower, upper)
                        if value.series_id == 'first' and value.start.day == 3)
        original = PreparedSeries.query_rule
        called = []
        def query(prepared, *args, **kwargs):
            called.append(prepared.master.id)
            return original(prepared, *args, **kwargs)
        with patch.object(PreparedSeries, 'query_rule', side_effect=AssertionError('Resolve one identity, not a family')):
            assert [signature(value) for value in projection.selected_events([expected.id])] == [signature(expected)]
        with patch.object(PreparedSeries, 'query_rule', new=query):
            selected = projection.for_sources(['first'])
        assert called == ['first']
        assert len(selected) == 11 and all(value.series_id == 'first' for value in selected)


def test_day_detail_limit_bounds_native_event_projection_per_source():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    lower, upper = start, start + timedelta(days=3)
    with patch('debacal.timeutil.local_zone', return_value=UTC), Store(':memory:') as store:
        store.apply_remote([event('hourly', start=start, end=start + timedelta(minutes=10),
                                  recurrence_rule='FREQ=HOURLY;COUNT=400')])
        projection = store.query_year_presence(lower, upper)
        original = PreparedSeries._project_values
        allocated = []
        def project(prepared, *args):
            allocated.append(prepared.master.id)
            return original(prepared, *args)
        with patch.object(PreparedSeries, '_project_values', new=project):
            values = projection.events_on_day(date(2026, 1, 2), limit=5)
        assert len(values) == 5 and len(allocated) <= 5


def test_sparse_boundaries_merge_dense_overlaps_and_separate_calendar_totals():
    lower, upper = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC)
    start = lower + timedelta(hours=9)
    with patch('debacal.timeutil.local_zone', return_value=UTC), Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Synthetic other', '#123456'))
        store.apply_remote([
            event('dense', start=start, end=start + timedelta(days=1), recurrence_rule='FREQ=HOURLY;COUNT=400'),
            event('additional', start=start + timedelta(days=2), end=start + timedelta(days=5)),
            event('point', calendar_id='other', start=start + timedelta(days=3), end=start + timedelta(days=3)),
        ])
        projection, _expected = assert_projection_matches(store, lower, upper, UTC)
        # The previous midnight ends exactly at this day's lower boundary.
        # Only the other 23 prior starts and 24 current starts touch this day.
        assert projection.summaries[3] == {'local': (48, ''), 'other': (1, '')}


@pytest.mark.parametrize('zone_name,first,last,clock,duration', [
    ('Europe/Berlin', (2026, 3, 28), (2026, 4, 3), (22, 30), timedelta(hours=1)),
    ('America/Goose_Bay', (1988, 10, 28), (1988, 11, 2), (1, 30), timedelta(minutes=31)),
    ('Pacific/Apia', (2011, 12, 28), (2012, 1, 3), (9, 30), timedelta(hours=1)),
])
@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=400', 'FREQ=WEEKLY;INTERVAL=2;COUNT=400'])
@pytest.mark.parametrize('partial', [False, True])
def test_utc_source_geometry_in_arbitrary_viewer_matches_general_projection(
        zone_name, first, last, clock, duration, rule, partial):
    from debacal import year_projection
    zone = ZoneInfo(zone_name)
    lower = datetime(*first, 18 if partial else 0, 20 if partial else 0, tzinfo=zone)
    upper = datetime(*last, 6 if partial else 0, 15 if partial else 0, tzinfo=zone)
    start = datetime(first[0], first[1], 1, *clock, tzinfo=UTC)
    sources = [event(identifier, start=start, end=start + delta, recurrence_rule=rule)
               for identifier, delta in [('first', duration), ('second', duration), ('point', timedelta())]]
    with patch('debacal.timeutil.local_zone', return_value=zone), Store(':memory:') as store:
        store.apply_remote(sources)
        expected = store.query_occurrences(lower, upper)
        with patch('debacal.year_projection.regular_day_starts', return_value=None):
            baseline = store.query_year_presence(lower, upper)
        with patch.object(PreparedSeries, 'iter_rule_ranges', side_effect=AssertionError('Expanded strict UTC source')), \
             patch('debacal.year_projection.day_interval', wraps=year_projection.day_interval) as conversions:
            actual = store.query_year_presence(lower, upper)
        assert len(actual) == len(expected)
        assert actual.summaries == baseline.summaries
        assert [signature(value) for value in actual] == [signature(value) for value in expected]
        assert set(actual._start_days) == {value.start.astimezone(zone).date() for value in expected}
        assert actual._instant_days == baseline._instant_days
        assert conversions.call_count < len(expected)
        assert not hasattr(actual, 'viewer_geometry')
        if zone_name == 'America/Goose_Bay' and not partial and rule.startswith('FREQ=DAILY;'):
            # At 02:01Z the clock falls from 30 October 00:01 to 29 October
            # 22:01. The non-midnight endpoint still touches 30 October before
            # the exclusive end; a direct end.date() shortcut would lose it.
            assert actual.summaries[(date(1988, 10, 30) - lower.date()).days]['local'][0] > 0


def test_midstream_source_failure_discards_all_local_boundaries_and_navigation_days():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 2, 1, tzinfo=BERLIN)
    source = event('failing', recurrence_rule='FREQ=WEEKLY;COUNT=400')
    healthy = event('healthy', start=lower + timedelta(days=3, hours=12))
    original = PreparedSeries.iter_rule_ranges
    def partial_failure(prepared, *args, **kwargs):
        for index, value in enumerate(original(prepared, *args, **kwargs)):
            yield value
            if prepared.master.id == source.id and index == 1:
                raise RecurrenceRangeError('Synthetic late source failure')
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source, healthy])
        with patch.object(PreparedSeries, 'iter_rule_ranges', new=partial_failure):
            issues = []
            projection = store.query_year_presence(lower, upper, issues=issues)
        assert len(projection) == 1
        assert projection.known_events == (store.get_occurrence(healthy.id),)
        assert projection._start_days == (healthy.start.date(),)
        assert sum(day.get('local', (0, ''))[0] for day in projection.summaries) == 1
        assert [master.id for master, _error in issues] == [source.id]


@pytest.mark.parametrize('finite', [False, True])
def test_floating_snapshot_keeps_its_captured_viewer_zone_for_later_identity_lookups(finite):
    source = event(floating_time=True, recurrence_rule='FREQ=DAILY;COUNT=' + ('4' if finite else '400'))
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 8, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        projection = store.query_year_presence(lower, upper)
        expected = store.query_occurrences(lower, upper)
        with patch('debacal.timeutil.local_zone', return_value=ZoneInfo('America/New_York')):
            for value in expected:
                actual = projection.event_by_id(value.id)
                assert actual is not None
                assert actual.start.timestamp() == value.start.timestamp()
                assert actual.start.tzinfo == BERLIN


def test_existing_projection_and_lazy_details_keep_their_original_source_revision():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2026, 1, 8, tzinfo=BERLIN)
    source = event(recurrence_rule='FREQ=DAILY;COUNT=400')
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([source])
        projection = store.query_year_presence(lower, upper)
        expected = store.query_occurrences(lower, upper)
        store.apply_remote([replace(source, title='New revision', excluded_dates=(recurrence_key(source.start),))])
        assert [signature(value) for value in projection] == [signature(value) for value in expected]
        assert projection.event_by_id(expected[0].id).title == source.title
        refreshed = store.query_year_presence(lower, upper)
        assert len(refreshed) == len(expected) - 1
        assert all(value.title == 'New revision' for value in refreshed)


def test_adjacent_navigation_matches_full_query_order_without_full_materialization():
    zone = ZoneInfo('America/New_York')
    lower, upper = datetime(2026, 6, 1, tzinfo=zone), datetime(2026, 6, 8, tzinfo=zone)
    midnight = datetime(2026, 6, 3, tzinfo=zone)
    date_midnight = datetime(2026, 6, 3, tzinfo=UTC)
    values = [event('native', start=midnight.replace(day=1, hour=9),
                    recurrence_rule='FREQ=DAILY;COUNT=400'),
              event('timed-midnight', start=midnight),
              event('date-midnight', start=date_midnight, end=date_midnight + timedelta(days=1), all_day=True),
              event('long-before-view', start=lower - timedelta(days=14), end=upper + timedelta(days=1)),
              event('tie-a', start=midnight.replace(hour=10)),
              event('tie-b', start=midnight.replace(hour=10))]
    with patch('debacal.timeutil.local_zone', return_value=zone), Store(':memory:') as store:
        store.apply_remote(values)
        expected = store.query_occurrences(lower, upper)
        projection = store.query_year_presence(lower, upper)
        expected_days = sorted({value.start.date() if value.all_day else value.start.astimezone(zone).date()
                                for value in expected})
        with patch.object(type(projection), '__iter__', side_effect=AssertionError('Do not iterate the full view')), \
             patch.object(projection, 'for_sources', side_effect=AssertionError('Do not project every family')):
            for index, value in enumerate(expected):
                assert signature(projection.adjacent_event(value.id, 1)) == signature(expected[(index + 1) % len(expected)])
                assert signature(projection.adjacent_event(value.id, -1)) == signature(expected[(index - 1) % len(expected)])
            for unknown in (None, '', 'not-in-this-view'):
                assert signature(projection.adjacent_event(unknown, 1)) == signature(expected[0])
                assert signature(projection.adjacent_event(unknown, -1)) == signature(expected[-1])
            for day in expected_days:
                expected_day = [value for value in expected if (
                    value.start.date() if value.all_day else value.start.astimezone(zone).date()) == day]
                assert [signature(value) for value in projection.events_starting_on(day)] == [
                    signature(value) for value in expected_day]
                later = next((value for value in expected_days if value > day), None)
                earlier = next((value for value in reversed(expected_days) if value < day), None)
                assert projection.adjacent_start_day(day, 1) == later
                assert projection.adjacent_start_day(day, -1) == earlier
            assert projection.adjacent_start_day(expected_days[0] - timedelta(days=1), 1) == expected_days[0]
            assert projection.adjacent_start_day(expected_days[-1] + timedelta(days=1), -1) == expected_days[-1]


def test_empty_projection_navigation_returns_no_event():
    lower, upper = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2027, 1, 1, tzinfo=BERLIN)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        projection = store.query_year_presence(lower, upper)
        assert projection.adjacent_event('missing', 1) is None
        assert projection.adjacent_event(None, -1) is None
        assert projection.adjacent_start_day(lower.date(), 1) is None
        assert projection.events_starting_on(lower.date()) == []


def test_legal_year_one_positive_offset_range_is_not_quarantined():
    zone = timezone(timedelta(hours=2))
    start = datetime(1, 1, 1, 0, 10, tzinfo=zone)
    source = event(start=start, end=start + timedelta(minutes=10))
    lower, upper = datetime(1, 1, 1, tzinfo=zone), datetime(1, 1, 3, tzinfo=zone)
    with patch('debacal.timeutil.local_zone', return_value=zone), Store(':memory:') as store:
        store.apply_remote([source])
        projection, expected = assert_projection_matches(store, lower, upper, zone)
        assert len(projection) == len(expected) == 1
        assert not store.list_quarantined_resources()


def test_inactive_calendars_are_excluded_before_bounded_detail_selection():
    lower, upper = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    with patch('debacal.timeutil.local_zone', return_value=UTC), Store(':memory:') as store:
        store.save_calendar(Calendar('hidden', 'Hidden', '#123456'))
        start = lower + timedelta(hours=8)
        store.apply_remote([event(f'hidden-{index}', start=start, calendar_id='hidden') for index in range(105)]
                           + [event('active', start=start + timedelta(hours=1))])
        projection = store.query_year_presence(lower, upper)
        restricted = projection.without_calendars({'hidden'})
        assert len(projection) == 106 and len(restricted) == 1
        assert restricted.without_calendars({'hidden'}) is restricted
        assert [value.id for value in restricted.events_on_day(start.date(), limit=1)] == ['active']
        assert restricted.adjacent_event(None, 1).id == 'active'
        assert restricted.event_by_id('hidden-0') is None
        assert restricted.filtered('appointment').summaries == restricted.summaries
