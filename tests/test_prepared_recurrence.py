"""Prepared recurrence queries stay exact on their first arbitrary window."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dateutil.rrule import _iterinfo, rrulestr
import pytest

from debacal.models import Event
from debacal.prepared_recurrence import PreparedSeries
from debacal.recurrence import RecurrenceRangeError, expand_series, occurrence, recurrence_key


UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')


def source(rule='FREQ=WEEKLY', **changes):
    start = datetime(2000, 1, 31, 9, 15, 20, tzinfo=BERLIN)
    return replace(Event('series', 'calendar', 'Prepared series', start, start + timedelta(hours=1),
                         recurrence_rule=rule, timezone_id=BERLIN.key), **changes)


RULES = [
    'FREQ=HOURLY;INTERVAL=5;BYHOUR=9,13,20;BYDAY=MO,WE;COUNT=50000',
    'FREQ=HOURLY;INTERVAL=25;BYMINUTE=0,20,40;BYSECOND=0,30;COUNT=100000',
    'FREQ=DAILY;INTERVAL=2;BYDAY=MO,WE,FR;BYHOUR=8,12,18;BYMINUTE=0,30;COUNT=10000',
    'FREQ=DAILY;BYHOUR=8,12,18;BYMINUTE=0,30;BYSETPOS=1,-1;COUNT=10000',
    'FREQ=WEEKLY;BYDAY=MO,WE,FR;WKST=SU;BYHOUR=8,12;COUNT=10000',
    'FREQ=WEEKLY;INTERVAL=3;BYDAY=MO,FR;BYHOUR=8,12;BYSETPOS=1,-1;COUNT=10000',
    'FREQ=MONTHLY',
    'FREQ=MONTHLY;INTERVAL=5;COUNT=1000',
    'FREQ=MONTHLY;BYMONTHDAY=31;COUNT=1000',
    'FREQ=MONTHLY;BYDAY=MO,WE;BYSETPOS=1,-1;COUNT=1000',
    'FREQ=MONTHLY;BYDAY=1MO,-1WE;BYHOUR=8,12;COUNT=1000',
    'FREQ=MONTHLY;BYMONTH=2,6,11;BYMONTHDAY=-1,-2;COUNT=1000',
    'FREQ=MONTHLY;BYMONTHDAY=1,15,-1;BYDAY=MO,TU,WE,TH,FR;COUNT=1000',
    'FREQ=MONTHLY;BYMONTHDAY=15,31;BYHOUR=8,12;BYSETPOS=1,-1;COUNT=1000',
    'FREQ=MONTHLY;UNTIL=20770101T000000Z;BYDAY=2TU',
    'FREQ=YEARLY',
    'FREQ=YEARLY;INTERVAL=3;COUNT=1000',
    'FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=1000',
    'FREQ=YEARLY;BYDAY=1MO,-1WE;BYHOUR=8,12;COUNT=1000',
    'FREQ=YEARLY;BYMONTH=2,6,11;BYMONTHDAY=-1,-2;COUNT=1000',
    'FREQ=YEARLY;BYYEARDAY=1,100,-1;COUNT=1000',
    'FREQ=YEARLY;BYWEEKNO=1,-1;BYDAY=MO;COUNT=1000',
    'FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1,-1;COUNT=1000',
    'FREQ=DAILY;BYMONTH=2;BYMONTHDAY=10,20',
    'FREQ=WEEKLY;BYMONTH=2;BYDAY=MO,WE',
    'FREQ=DAILY;BYMONTH=2,6,11;BYMONTHDAY=10,20;COUNT=1000',
    'FREQ=WEEKLY;BYMONTH=2,6,11;BYDAY=MO,WE;WKST=SU;COUNT=1000',
    'FREQ=HOURLY;BYMONTH=2,6,11;INTERVAL=5;BYHOUR=9,13,20',
]


@pytest.mark.parametrize('rule', RULES)
@pytest.mark.parametrize('year', [2000, 2001, 2026])
def test_prepared_rule_matches_existing_projection_for_unvisited_windows(rule, year):
    master = source(rule)
    prepared = PreparedSeries(master)
    begin = datetime(year, 2, 1, tzinfo=BERLIN)
    end = begin + timedelta(days=75)
    assert prepared.query(begin, end) == expand_series(master, begin, end)


@pytest.mark.parametrize('all_day,floating', [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=20', 'FREQ=MONTHLY;COUNT=1000', ''])
def test_explicit_dates_exclusions_and_detached_actual_ranges_are_prepared_once(all_day, floating, rule):
    start = datetime(2026, 10, 25, 0 if all_day else 9, tzinfo=BERLIN)
    duration = timedelta(days=2) if all_day else timedelta(hours=1)
    master = source(rule, start=start, end=start + duration, all_day=all_day, floating_time=floating)
    key = lambda value: recurrence_key(value, all_day, floating_time=floating)
    detached_key = key(start + timedelta(days=3))
    detached = occurrence(master, start + timedelta(days=3), detached_key)
    detached = replace(detached, start=start + timedelta(days=8), end=start + timedelta(days=8) + duration)
    master = replace(master, recurrence_dates=(key(start - timedelta(days=3)), key(start + timedelta(days=11))),
                     excluded_dates=(key(start + timedelta(days=1)),),
                     recurrence_overrides={detached_key: detached.to_dict()})
    prepared = PreparedSeries(master)
    for offset in (-4, 0, 4, 8, 11, 35):
        begin = start + timedelta(days=offset)
        end = begin + timedelta(days=3)
        assert prepared.query(begin, end) == expand_series(master, begin, end)


@pytest.mark.parametrize('point', [False, True])
@pytest.mark.parametrize('zone_name,start', [
    ('Europe/Berlin', (2027, 3, 26, 2, 30)),
    ('Australia/Lord_Howe', (2026, 10, 2, 2, 15)),
    ('Pacific/Apia', (2011, 12, 28, 0, 30)),
])
def test_first_queries_across_clock_gaps_keep_normalized_identities_and_order(zone_name, start, point):
    zone = ZoneInfo(zone_name)
    initial = datetime(*start, tzinfo=zone)
    duration = timedelta(0) if point else timedelta(hours=1)
    master = source('FREQ=DAILY;COUNT=7', start=initial, end=initial + duration, timezone_id=zone_name)
    prepared = PreparedSeries(master)
    for offset in range(5):
        begin = initial + timedelta(days=offset, minutes=20)
        end = begin + timedelta(days=2)
        assert prepared.query(begin, end) == expand_series(master, begin, end)


def test_repeated_hour_anchor_is_not_duplicated_into_an_extra_fold():
    initial = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
    master = source('FREQ=DAILY;COUNT=3', start=initial, end=initial + timedelta(hours=1))
    begin, end = initial - timedelta(hours=3), initial + timedelta(days=5)
    assert PreparedSeries(master).query(begin, end) == expand_series(master, begin, end)


@pytest.mark.parametrize('rule', ['FREQ=MONTHLY;BYMONTHDAY=31;COUNT=10000',
                                  'FREQ=YEARLY;BYDAY=MO;BYSETPOS=-1;COUNT=10000'])
def test_old_counted_calendar_rules_seek_without_visiting_history(rule):
    prepared = PreparedSeries(source(rule))
    begin = datetime(2077, 2, 1, tzinfo=BERLIN)
    visited = 0
    method = 'mdayset' if 'MONTHLY' in rule else 'ydayset'
    original = getattr(_iterinfo, method)

    def observed(self, *args):
        nonlocal visited
        visited += 1
        assert visited < 8, 'A first arbitrary query must not revisit the historical years.'
        return original(self, *args)

    with patch.object(_iterinfo, method, observed):
        prepared.query_rule(begin, begin + timedelta(days=35))
    assert visited < 8


def test_query_does_not_reparse_rules_or_reconstruct_models_or_remember_results():
    prepared = PreparedSeries(source('FREQ=MONTHLY;BYDAY=1MO;COUNT=10000'))
    begin = datetime(2077, 2, 1, tzinfo=BERLIN)
    end = begin + timedelta(days=35)
    with (patch('debacal.recurrence.rrulestr', side_effect=AssertionError('Rule reparsed')),
          patch.object(Event, '__post_init__', side_effect=AssertionError('Master revalidated'))):
        first = prepared.query_rule(begin, end)
        second = prepared.query_rule(begin, end)
    assert first == second and first
    assert all(a is not b for a, b in zip(first, second))
    assert prepared.rule._cache is None


def test_small_finite_unrolling_and_large_rule_probe_are_bounded():
    small = PreparedSeries(source('FREQ=MONTHLY;COUNT=3'))
    assert len(small.finite_rule_occurrences(10)) == 3
    large = PreparedSeries(source('FREQ=DAILY;COUNT=1000000'))
    assert large.finite_rule_occurrences(10) is None
    infinite = PreparedSeries(source('FREQ=DAILY'))
    assert infinite.finite_rule_occurrences(10) is None


def test_dense_ranges_retain_range_error_and_truncation_semantics():
    prepared = PreparedSeries(source('FREQ=HOURLY'))
    begin = prepared.master.start
    end = begin + timedelta(days=2)
    with pytest.raises(RecurrenceRangeError):
        prepared.query(begin, end, limit=3)
    assert prepared.query(begin, end, limit=3, truncate=True) == expand_series(
        prepared.master, begin, end, limit=3, truncate=True)


@pytest.mark.parametrize('all_day,floating', [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize('cut', [False, True])
def test_duration_and_month_boundary_cut_match_existing_occurrences(all_day, floating, cut):
    first = datetime(2026, 1, 28, 0 if all_day else 10, tzinfo=BERLIN)
    end = first + timedelta(days=4)
    master = source('FREQ=MONTHLY;COUNT=1000', start=first, end=end, all_day=all_day,
                    floating_time=floating, range_policy='cut' if cut else '',
                    duration_spec='' if all_day or floating else 'P4D')
    prepared = PreparedSeries(master)
    for month in (2, 3, 10):
        begin = datetime(2027, month, 1, tzinfo=BERLIN)
        end = begin + timedelta(days=40)
        assert prepared.query(begin, end) == expand_series(master, begin, end)


@pytest.mark.parametrize('rule', ['', 'FREQ=MONTHLY;COUNT=3', 'FREQ=MONTHLY;COUNT=1000'])
@pytest.mark.parametrize('cut', [False, True])
def test_floating_preparation_uses_one_zone_for_explicit_finite_and_native_rule_dates(rule, cut):
    first = datetime(2026, 1, 28, 10, tzinfo=BERLIN)
    master = source(rule, start=first, end=first + timedelta(days=4), floating_time=True,
                    range_policy='cut' if cut else '')
    begin, end = datetime(2026, 2, 1, tzinfo=BERLIN), datetime(2026, 3, 2, tzinfo=BERLIN)
    expected = expand_series(master, begin, end)
    # Direct expansion operates in the supplied master's scheduling zone. The
    # host timezone must not change just the prepared rule's projection.
    with patch('debacal.timeutil.local_zone', side_effect=AssertionError('Implicit viewer zone')):
        prepared = PreparedSeries(master)
        assert prepared.query(begin, end) == expected
        finite = prepared.finite_rule_occurrences(4)
        if finite is not None:
            assert all(value.start.tzinfo == BERLIN for value in finite)
        if rule:
            key = recurrence_key(first, floating_time=True)
            assert prepared.occurrence_at(key) == occurrence(master, first, key)


@pytest.mark.parametrize('cut', [False, True])
def test_floating_index_and_membership_follow_viewer_zone_changes_without_repreparation(cut):
    from debacal.event_range_index import EventRangeIndex

    first = datetime(2026, 1, 28, 10, tzinfo=BERLIN)
    master = source('FREQ=MONTHLY;COUNT=1000', start=first, end=first + timedelta(days=4),
                    floating_time=True, range_policy='cut' if cut else '')
    index = EventRangeIndex()
    index.upsert(master)
    prepared = index.sources[master.id].prepared
    for zone in (UTC, ZoneInfo('America/New_York'), BERLIN):
        rebound = replace(master, start=master.start.replace(tzinfo=zone), end=master.end.replace(tzinfo=zone))
        for month in (2, 3, 10):
            begin = datetime(2026, month, 1, tzinfo=zone)
            end = begin + timedelta(days=40)
            with patch('debacal.timeutil.local_zone', return_value=zone):
                actual, failures = index.query(begin, end)
            assert not failures
            expected = expand_series(rebound, begin, end)
            assert actual == expected
            for value in expected:
                if value.recurrence_id not in prepared.override_keys:
                    assert prepared.occurrence_at(value.recurrence_id, floating_zone=zone) == value
        assert index.sources[master.id].prepared is prepared


@pytest.mark.parametrize('viewer', ['UTC', 'America/New_York', 'Europe/Berlin'])
@pytest.mark.parametrize('added', [False, True])
def test_explicit_floating_viewer_zone_applies_to_standalone_and_date_only_series(viewer, added):
    first = datetime(2026, 3, 28, 9, tzinfo=BERLIN)
    master = source('', start=first, end=first + timedelta(hours=1), floating_time=True)
    if added:
        master = replace(master, recurrence_dates=('2026-03-29T09:00:00',))
    prepared = PreparedSeries(master)
    zone = ZoneInfo(viewer)
    rebound = replace(master, start=master.start.replace(tzinfo=zone), end=master.end.replace(tzinfo=zone))
    begin, end = datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 3, 31, tzinfo=zone)
    assert prepared.query(begin, end, floating_zone=zone) == expand_series(rebound, begin, end)
    assert prepared.master.start.tzinfo is BERLIN


def test_short_monthly_count_does_not_build_a_gregorian_cycle():
    with patch('debacal.prepared_recurrence._CalendarSeek', side_effect=AssertionError('Full cycle built')):
        prepared = PreparedSeries(source('FREQ=MONTHLY;COUNT=18'))
    assert len(prepared.finite_rule_occurrences(256)) == 18


@pytest.mark.parametrize('rule', ['FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30',
                                  'FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=29'])
def test_empty_or_sparse_window_stops_at_its_end_without_seeking_future_candidates(rule):
    prepared = PreparedSeries(source(rule))
    begin = datetime(2077, 10, 1, tzinfo=BERLIN)
    visited = 0
    original = _iterinfo.mdayset

    def observed(self, *args):
        nonlocal visited
        visited += 1
        assert visited < 6
        return original(self, *args)

    with patch.object(_iterinfo, 'mdayset', observed):
        assert prepared.query_rule(begin, begin + timedelta(days=7)) == []
    assert visited < 6


@pytest.mark.parametrize('rule', [
    'FREQ=HOURLY;INTERVAL=5;BYMONTH=2,6,11;BYHOUR=9,13,20;BYMINUTE=0,20,40;COUNT=100000',
    'FREQ=HOURLY;INTERVAL=25;BYMONTHDAY=1,15,-1;COUNT=10000',
    'FREQ=HOURLY;BYMONTH=1;BYSETPOS=1,-1;BYMINUTE=0,20,40;COUNT=1000000',
    'FREQ=DAILY;BYEASTER=0;COUNT=1000',
    'FREQ=YEARLY;BYEASTER=0;COUNT=1000',
])
def test_calendar_filtered_count_rank_matches_independent_engine_without_historical_query_work(rule):
    master = source(rule)
    prepared = PreparedSeries(master)
    begin = datetime(2026, 2, 1, tzinfo=BERLIN)
    end = begin + timedelta(days=30)
    expected = rrulestr(rule, dtstart=master.start).between(begin - timedelta(hours=1), end, inc=True)
    expected = [value for value in expected if value.timestamp() < end.timestamp()
                and value.timestamp() + 3600 > begin.timestamp()]
    visited = 0
    original = _iterinfo.ddayset

    def observed(self, *args):
        nonlocal visited
        visited += 1
        assert visited < 800, 'Work must be proportional to the requested month, not 26 years.'
        return original(self, *args)

    with patch.object(_iterinfo, 'ddayset', observed):
        actual = prepared.query_rule(begin, end)
    assert [item.start.timestamp() for item in actual] == [value.timestamp() for value in expected]


def test_maximum_supported_minutely_count_is_prepared_without_query_iteration():
    master = source('FREQ=MINUTELY;COUNT=10000')
    prepared = PreparedSeries(replace(master, end=master.start))
    assert len(prepared._finite_values) == 10000
    begin = prepared.master.start + timedelta(minutes=5000)
    with patch('debacal.prepared_recurrence.BoundedRule', side_effect=AssertionError('Rule iterated')):
        assert len(prepared.query_rule(begin, begin + timedelta(minutes=3))) == 3


def test_occurrence_membership_does_not_expand_all_overlapping_long_ranges():
    initial = datetime(2000, 1, 1, 9, tzinfo=UTC)
    master = source('FREQ=HOURLY', start=initial, end=initial + timedelta(days=1000), timezone_id='UTC')
    prepared = PreparedSeries(master)
    target = datetime(2026, 11, 4, 12, tzinfo=UTC)
    key = recurrence_key(target)
    with (patch.object(prepared, '_project', wraps=prepared._project) as project,
          patch.object(Event, '__post_init__', side_effect=AssertionError('Unrelated models constructed'))):
        actual = prepared.occurrence_at(key)
    assert actual.start == target
    assert actual.end == target + timedelta(days=1000)
    assert project.call_count == 1


@pytest.mark.parametrize('initial,target', [
    (datetime(1, 1, 1, tzinfo=UTC), datetime(1, 1, 1, tzinfo=UTC)),
    (datetime(1, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)),
])
def test_occurrence_membership_handles_representable_year_boundaries(initial, target):
    master = source('FREQ=DAILY;BYHOUR=23;BYMINUTE=59;BYSECOND=59', start=initial,
                    end=initial, timezone_id='UTC')
    prepared = PreparedSeries(master)
    value = prepared.occurrence_at(recurrence_key(target))
    if target.hour == 23:
        assert value.start == value.end == target
    else:
        assert value is None  # The primary is explicit; the rule starts at 23:59:59.


@pytest.mark.parametrize('rule', ['FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30',
                                  'FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29'])
def test_occurrence_membership_empty_window_does_not_search_future_years(rule):
    prepared = PreparedSeries(source(rule))
    target = datetime(9999, 12, 31, 23, 59, 59, tzinfo=BERLIN)
    with patch.object(prepared, '_project', side_effect=AssertionError('An unrelated occurrence was built')):
        assert prepared.occurrence_at(recurrence_key(target)) is None


def test_occurrence_membership_respects_exclusions_and_detached_rule_identities():
    master = source('FREQ=DAILY')
    excluded, detached = master.start + timedelta(days=2), master.start + timedelta(days=3)
    exception = occurrence(master, detached)
    master = replace(master, excluded_dates=(recurrence_key(excluded),),
                     recurrence_overrides={recurrence_key(detached): exception.to_dict()})
    prepared = PreparedSeries(master)
    assert prepared.occurrence_at(recurrence_key(master.start)).start == master.start
    assert prepared.occurrence_at(recurrence_key(excluded)) is None
    assert prepared.occurrence_at(recurrence_key(detached)) is None
