from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from debacal.daily_presence import daily_day_span, regular_day_starts, merged_days
from debacal.models import Event
from debacal.prepared_recurrence import PreparedSeries
from debacal.store import Store

UTC=timezone.utc
START=datetime(2026,1,1,9,tzinfo=UTC)


def source(**changes):
    return replace(Event('daily','local','Synthetic daily',START,START+timedelta(hours=1),
                         recurrence_rule='FREQ=DAILY'),**changes)


def check(value, lower, upper):
    prepared=PreparedSeries(value)
    span=daily_day_span(prepared,lower,upper,UTC)
    assert span is not None
    expected={start.date().toordinal() for _key,start,_end,_spec in prepared.iter_rule_ranges(lower,upper)}
    assert set(span)==expected
    with patch('debacal.timeutil.local_zone',return_value=UTC),Store(':memory:') as store:
        store.apply_remote([value])
        expected=store.query_occurrences(lower,upper)
        with patch.object(PreparedSeries,'iter_rule_ranges',side_effect=AssertionError('No full daily iteration')):
            actual=store.query_year_presence(lower,upper)
        assert len(actual)==len(expected)
        assert [item.id for item in actual]==[item.id for item in expected]
        expected_days={item.start.date() for item in expected}
        assert set(actual._start_days)==expected_days
        for offset,counts in enumerate(actual.summaries):
            day=lower.date()+timedelta(days=offset)
            assert counts==({'local':(1,'')} if day in expected_days else {})
        return actual,expected


@pytest.mark.parametrize('rule',['FREQ=DAILY','FREQ=DAILY;COUNT=400','FREQ=DAILY;COUNT=1',
                                'FREQ=DAILY;UNTIL=20261231T090000Z','FREQ=DAILY;UNTIL=20261231T085959Z',
                                'FREQ=DAILY;UNTIL=20251231T090000Z'])
@pytest.mark.parametrize('window',[(datetime(2026,1,1,tzinfo=UTC),datetime(2027,1,1,tzinfo=UTC)),
                                 (datetime(2025,12,29,tzinfo=UTC),datetime(2026,1,3,tzinfo=UTC)),
                                 (datetime(2026,2,1,tzinfo=UTC),datetime(2026,2,4,tzinfo=UTC))])
def test_supported_bounds_and_anchor(rule,window):
    check(source(recurrence_rule=rule),*window)


@pytest.mark.parametrize('duration',[timedelta(),timedelta(minutes=1),timedelta(hours=1),timedelta(hours=15)])
@pytest.mark.parametrize('lower_time',[(9,0,0),(9,0,1),(9,59,59),(10,0,0),(23,59,59)])
def test_partial_day_half_open_overlap_points_and_midnight_end(duration,lower_time):
    lower=datetime(2026,1,1,*lower_time,tzinfo=UTC)
    upper=datetime(2026,1,4,9,tzinfo=UTC)
    check(source(end=START+duration),lower,upper)


@pytest.mark.parametrize('changes',[
    {'recurrence_rule':'FREQ=WEEKLY'}, {'recurrence_rule':'FREQ=DAILY;INTERVAL=2'},
    {'recurrence_rule':'FREQ=DAILY;BYDAY=MO,TU'}, {'recurrence_rule':'FREQ=DAILY;BYMONTH=1'},
    {'recurrence_rule':'FREQ=DAILY;BYHOUR=9,10'}, {'recurrence_rule':'FREQ=DAILY;BYMINUTE=0,30'},
    {'recurrence_rule':'FREQ=DAILY;BYSETPOS=1'}, {'recurrence_rule':'FREQ=DAILY;BYMONTHDAY=1'},
    {'excluded_dates':(START.isoformat(),)}, {'recurrence_dates':((START+timedelta(days=10)).isoformat(),)},
    {'floating_time':True}, {'range_policy':'cut'}, {'duration_spec':'P1D','end':START+timedelta(days=1)},
    {'end':START+timedelta(hours=16)}, {'start':START.replace(microsecond=1)},
    {'start':START.astimezone(ZoneInfo('Europe/Berlin')),'end':(START+timedelta(hours=1)).astimezone(ZoneInfo('Europe/Berlin'))},
    {'start':START.replace(hour=0),'end':START.replace(hour=0)+timedelta(days=1),'all_day':True},
])
def test_strict_unsupported_schedules_fall_back(changes):
    assert daily_day_span(PreparedSeries(source(**changes)),START,START+timedelta(days=7),UTC) is None


def test_override_viewer_and_rule_clock_reject_optimization():
    override=source().to_dict()
    for value in [source(recurrence_overrides={START.isoformat():override}),
                  source(recurrence_rule='FREQ=DAILY;BYSECOND=30')]:
        assert daily_day_span(PreparedSeries(value),START,START+timedelta(days=7),UTC) is None
    assert daily_day_span(PreparedSeries(source()),START,START+timedelta(days=7),ZoneInfo('Europe/London')) is None


def test_large_count_union_budget_and_summary_never_allocates_events_or_ids():
    lower,upper=datetime(2026,1,1,tzinfo=UTC),datetime(2027,1,1,tzinfo=UTC)
    with patch('debacal.timeutil.local_zone',return_value=UTC),Store(':memory:') as store:
        store.apply_remote([source()])
        with patch.object(PreparedSeries,'iter_rule_ranges',side_effect=AssertionError('No native loop')), \
             patch.object(PreparedSeries,'_project_values',side_effect=AssertionError('No Event allocation')), \
             patch('debacal.prepared_recurrence.occurrence_id',side_effect=AssertionError('No ID encoding')):
            values=store.query_year_presence(lower,upper)
            assert len(values)==365 and len(values._start_days)==365
            with patch('debacal.year_projection.OCCURRENCE_LIMIT',364):
                issues=[]
                assert not store.query_year_presence(lower,upper,issues=issues)
                assert len(issues)==1


def test_overlapping_ordinal_spans_merge_without_duplicate_days():
    assert list(merged_days([range(1,5),range(3,9),range(10,12),range(12,15),range(30,30)]))==list(range(1,9))+list(range(10,15))


@pytest.mark.parametrize('frequency', ['DAILY', 'WEEKLY'])
@pytest.mark.parametrize('interval', [1, 2, 7, 31, 10000])
@pytest.mark.parametrize('bound', ['', ';COUNT=1', ';COUNT=400', ';UNTIL=20261231T090000Z',
                                  ';UNTIL=20261231T085959Z', ';UNTIL=20251231T090000Z'])
def test_stepped_regular_starts_match_independent_dateutil_rule(frequency, interval, bound):
    from dateutil.rrule import rrulestr
    rule = f'FREQ={frequency};INTERVAL={interval}' + bound
    value = source(recurrence_rule=rule)
    lower, upper = datetime(2026, 2, 3, 9, 30, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    span = regular_day_starts(PreparedSeries(value), lower, upper, UTC)
    assert span is not None
    expected = {start.date().toordinal() for start in rrulestr(rule, dtstart=START).between(
        lower - timedelta(hours=1), upper, inc=True)
        if start < upper and start + timedelta(hours=1) > lower}
    assert set(span) == expected
    assert span.step == interval * (7 if frequency == 'WEEKLY' else 1)


@pytest.mark.parametrize('frequency,interval', [('DAILY', 3), ('WEEKLY', 1), ('WEEKLY', 2)])
@pytest.mark.parametrize('duration', [timedelta(), timedelta(hours=1), timedelta(hours=15)])
def test_stepped_summary_gaps_details_and_navigation_remain_exact(frequency, interval, duration):
    lower, upper = datetime(2025, 12, 30, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
    value = source(end=START + duration, recurrence_rule=f'FREQ={frequency};INTERVAL={interval};COUNT=400')
    with patch('debacal.timeutil.local_zone', return_value=UTC), Store(':memory:') as store:
        store.apply_remote([value])
        expected = store.query_occurrences(lower, upper)
        with patch.object(PreparedSeries, 'iter_rule_ranges', side_effect=AssertionError('Expanded stepped UTC rule')):
            actual = store.query_year_presence(lower, upper)
        assert len(actual) == len(expected)
        assert set(actual._start_days) == {event.start.date() for event in expected}
        for offset, counts in enumerate(actual.summaries):
            day = lower.date() + timedelta(days=offset)
            assert counts == ({'local': (1, '')} if day in actual._start_days else {})
        for index, event in enumerate(expected):
            assert actual.adjacent_event(event.id, 1).id == expected[(index + 1) % len(expected)].id
            assert actual.events_on_day(event.start.date())[0].id == event.id
        assert len(actual.filtered('Synthetic daily')) == len(expected)
        assert not actual.filtered('does not match')


@pytest.mark.parametrize('rule', ['FREQ=DAILY;BYDAY=MO', 'FREQ=WEEKLY;BYDAY=MO,WE',
                                 'FREQ=WEEKLY;BYDAY=MO', 'FREQ=WEEKLY;BYMONTH=1',
                                 'FREQ=WEEKLY;BYHOUR=9,10'])
def test_calendar_or_clock_filtered_rules_keep_general_iteration(rule):
    assert regular_day_starts(PreparedSeries(source(recurrence_rule=rule)), START,
                              START + timedelta(days=30), UTC) is None


def test_noncontiguous_starts_are_never_merged_as_full_occupancy_spans():
    with pytest.raises(ValueError, match='contiguous'):
        list(merged_days([range(1, 10, 2)]))


def test_last_supported_day_keeps_canonical_endpoint_overflow_handling():
    first = datetime(9999, 12, 30, 23, tzinfo=UTC)
    value = source(start=first, end=first + timedelta(hours=1))
    lower = datetime(9999, 12, 31, tzinfo=UTC)
    upper = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert regular_day_starts(PreparedSeries(value), lower, upper, UTC) is None
