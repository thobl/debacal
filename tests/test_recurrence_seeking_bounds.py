"""Historical seeking retains raw RRULE phase/count and bounds generated work."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dateutil.rrule import _iterinfo, rrule, rrulestr
import pytest

from debacal.models import Event
from debacal.recurrence import RecurrenceRangeError, expand_event, expand_events_safe, recurrence_set


@pytest.mark.parametrize('rule', [
    'FREQ=HOURLY;INTERVAL=5;BYHOUR=9,13,20;BYDAY=MO,WE;COUNT=500',
    'FREQ=HOURLY;INTERVAL=25;BYMINUTE=0,20,40;BYSECOND=0,30;COUNT=200',
    'FREQ=HOURLY;BYMINUTE=0,20,40;BYSECOND=0,30;BYSETPOS=1,-1;COUNT=500',
    'FREQ=DAILY;INTERVAL=2;BYDAY=MO,WE,FR;BYHOUR=8,12,18;BYMINUTE=0,30;COUNT=100',
    'FREQ=DAILY;BYHOUR=8,12,18;BYMINUTE=0,30;BYSETPOS=1,-1;COUNT=100',
    'FREQ=WEEKLY;BYDAY=MO,WE,FR;WKST=SU;BYHOUR=8,12;COUNT=70',
    'FREQ=WEEKLY;INTERVAL=3;BYDAY=MO,FR;BYHOUR=8,12;BYSETPOS=1,-1;COUNT=50',
    'FREQ=HOURLY;BYHOUR=9;BYMINUTE=0,30;COUNT=1',
    'FREQ=DAILY;BYDAY=MO;BYHOUR=8;COUNT=1',
])
@pytest.mark.parametrize('offset', [4, 26])
def test_static_period_seek_matches_independent_dateutil_rules_including_partial_periods(rule, offset):
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 1, 9, 15, 20, tzinfo=zone)
    event = Event('series', 'local', 'Phase and count', start, start + timedelta(minutes=10), recurrence_rule=rule)
    lower, upper = start + timedelta(days=offset), start + timedelta(days=offset + 25)
    expected = rrulestr(rule, dtstart=start).between(lower, upper, inc=True)
    actual = recurrence_set(event, after=lower).between(lower, upper, inc=True)
    assert [value.timestamp() for value in actual] == [value.timestamp() for value in expected]


@pytest.mark.parametrize('dense', [False, True])
def test_ancient_hourly_and_dense_daily_rules_generate_only_a_bounded_suffix(dense):
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    if dense:
        rule = ('FREQ=DAILY;BYHOUR=' + ','.join(map(str, range(24)))
                + ';BYMINUTE=' + ','.join(map(str, range(60))))
        duration, count = timedelta(minutes=1), 1440
    else:
        rule, duration, count = 'FREQ=HOURLY', timedelta(hours=1), 24
    event = Event('series', 'local', 'Old dense rule', first, first + duration, recurrence_rule=rule)
    produced, original = 0, rrule._iter

    def observed(rule):
        nonlocal produced
        for value in original(rule):
            produced += 1
            assert produced < 5000, 'The query must not enumerate the years before its window.'
            yield value

    with patch.object(rrule, '_iter', observed):
        actual = expand_event(event, begin, begin + timedelta(days=1))
    assert [item.start for item in actual] == [begin + duration * index for index in range(count)]
    assert produced < 3 * count + 10


def test_empty_calendar_filters_have_a_period_budget_even_without_emitted_candidates():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    event = Event('empty', 'local', 'Empty calendar intersection', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;COUNT=10')
    original_factory = rrule._iter.__globals__['_iterinfo']
    visited, original = 0, _iterinfo.ddayset

    def dayset(self, *args):
        nonlocal visited
        visited += 1
        return original(self, *args)

    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    with patch('debacal.recurrence_budget.MAX_PERIOD_WORK', 32), patch.object(_iterinfo, 'ddayset', dayset):
        with pytest.raises(RecurrenceRangeError, match='computation budget'):
            expand_event(event, begin, begin + timedelta(days=1))
    assert visited == 32
    assert rrule._iter.__globals__['_iterinfo'] is original_factory
    assert event.recurrence_rule == 'FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;COUNT=10'


def test_invisible_generated_history_is_also_budgeted():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    event = Event('history', 'local', 'Filtered historical hours', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=HOURLY;BYMONTH=1')
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    with patch('debacal.recurrence_budget.MAX_CANDIDATE_WORK', 20):
        with pytest.raises(RecurrenceRangeError, match='computation budget'):
            expand_event(event, begin, begin + timedelta(days=1))


def test_a_small_expired_filtered_count_remains_exact_and_cheap():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    event = Event('expired', 'local', 'Ten historical hours', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=HOURLY;BYMONTH=1;COUNT=10')
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    with patch('debacal.recurrence_budget.MAX_PERIOD_WORK', 20), \
            patch('debacal.recurrence_budget.MAX_CANDIDATE_WORK', 20):
        assert expand_event(event, begin, begin + timedelta(days=1)) == []


def test_opt_in_budget_issues_keep_healthy_resources_and_are_not_malformed_issues():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    empty = Event('empty', 'local', 'Expensive source', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30')
    healthy = Event('healthy', 'local', 'Healthy source', begin, begin + timedelta(hours=1))
    issues = []
    with patch('debacal.recurrence_budget.MAX_PERIOD_WORK', 16):
        actual, malformed = expand_events_safe([empty, healthy], begin, begin + timedelta(days=1),
                                               range_issues=issues)
    assert actual == [healthy]
    assert malformed == ()
    assert len(issues) == 1 and issues[0][0] == empty
    assert isinstance(issues[0][1], RecurrenceRangeError)


def test_unknown_dateutil_calendar_interface_is_refused_before_unbounded_iteration():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    event = Event('source', 'local', 'Monthly rule', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=MONTHLY;COUNT=3')
    original = rrule._iter

    def unknown_iterator(self):
        yield from original(self)

    with patch.object(rrule, '_iter', unknown_iterator):
        with pytest.raises(RecurrenceRangeError, match='engine cannot enforce'):
            expand_event(event, first, first + timedelta(days=100))


@pytest.mark.parametrize('frequency', ['DAILY', 'YEARLY'])
def test_dense_precomputed_timesets_are_charged_before_calendar_projection(frequency):
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    rule = (f'FREQ={frequency};BYMONTH=1;BYHOUR=' + ','.join(map(str, range(24)))
            + ';BYMINUTE=0,15,30,45')
    event = Event('dense', 'local', 'Precomputed clocks', first, first + timedelta(minutes=1), recurrence_rule=rule)
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    with patch('debacal.recurrence_budget.MAX_CANDIDATE_WORK', 50):
        with pytest.raises(RecurrenceRangeError, match='computation budget'):
            expand_event(event, begin, begin + timedelta(days=1))


def test_private_iterator_budget_keeps_an_existing_profiler_and_library_globals():
    import cProfile
    import sys
    first = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    event = Event('profiled', 'local', 'Profiled calendar', first, first + timedelta(hours=1),
                  recurrence_rule='FREQ=MONTHLY;COUNT=3')
    factory = rrule._iter.__globals__['_iterinfo']
    profile = cProfile.Profile()
    profile.enable()
    hook = sys.getprofile()  # Newer CPython can implement cProfile via monitoring.
    try:
        actual = expand_event(event, first, first + timedelta(days=100))
        assert sys.getprofile() is hook
        assert rrule._iter.__globals__['_iterinfo'] is factory
    finally:
        profile.disable()
    assert [item.start.date().isoformat() for item in actual] == ['2026-01-01', '2026-02-01', '2026-03-01']
