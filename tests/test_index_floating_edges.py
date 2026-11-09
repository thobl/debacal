"""Floating interval queries retain instant correctness around clock changes."""

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_range_index import EventRangeIndex
from debacal.models import Event
from debacal.recurrence import overlaps
from debacal.store import Store


BERLIN = ZoneInfo('Europe/Berlin')


def floating(identity, start, end, **fields):
    return Event(identity, 'local', identity, start, end, floating_time=True, **fields)


@pytest.mark.parametrize(('begin', 'end'), [
    # Increasing instants, but decreasing local clock labels.
    (datetime(2026, 10, 25, 2, 45, tzinfo=BERLIN, fold=0),
     datetime(2026, 10, 25, 2, 15, tzinfo=BERLIN, fold=1)),
    # Even a window wholly inside the second hour can contain an ongoing
    # event whose first-hour start has a later wall-clock label.
    (datetime(2026, 10, 25, 2, 20, tzinfo=BERLIN, fold=1),
     datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)),
])
def test_repeated_hour_queries_include_ongoing_floating_intervals(begin, end):
    values = [
        floating('ongoing', datetime(2026, 10, 25, 2, 50, tzinfo=BERLIN),
                 datetime(2026, 10, 25, 3, 0, tzinfo=BERLIN)),
        floating('ended', datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN),
                 datetime(2026, 10, 25, 2, 10, tzinfo=BERLIN)),
        floating('later', datetime(2026, 10, 25, 3, 10, tzinfo=BERLIN),
                 datetime(2026, 10, 25, 3, 20, tzinfo=BERLIN)),
    ]
    expected = [value.id for value in values if overlaps(value, begin, end)]
    assert expected == ['ongoing']
    with patch('debacal.timeutil.local_zone', return_value=BERLIN):
        index = EventRangeIndex()
        for value in values:
            index.upsert(value)
        found, failures = index.query(begin, end)
        assert not failures
        assert [value.id for value in found] == expected
        upcoming, failures = index.query(begin, end, limit=1)
        assert not failures
        assert [value.id for value in upcoming] == expected


def test_floating_top_n_uses_instant_order_across_repeated_hour():
    first = floating('first', datetime(2026, 10, 25, 2, 50, tzinfo=BERLIN, fold=0),
                     datetime(2026, 10, 25, 3, 0, tzinfo=BERLIN))
    second = floating('second', datetime(2026, 10, 25, 2, 10, tzinfo=BERLIN, fold=1),
                      datetime(2026, 10, 25, 2, 20, tzinfo=BERLIN, fold=1))
    begin = datetime(2026, 10, 25, tzinfo=BERLIN)
    end = begin + timedelta(days=1)
    assert first.start.timestamp() < second.start.timestamp()
    with patch('debacal.timeutil.local_zone', return_value=BERLIN):
        index = EventRangeIndex()
        index.upsert(second)
        index.upsert(first)
        found, failures = index.query(begin, end, limit=1)
    assert not failures
    assert [value.id for value in found] == ['first']


def test_floating_top_n_uses_instant_order_through_missing_hour():
    # The existing Event representation interprets an explicit floating clock
    # in a gap with the pre-transition offset. Wall and instant order differ.
    first = floating('first', datetime(2026, 3, 29, 3, 10, tzinfo=BERLIN),
                     datetime(2026, 3, 29, 3, 20, tzinfo=BERLIN))
    second = floating('second', datetime(2026, 3, 29, 2, 50, tzinfo=BERLIN),
                      datetime(2026, 3, 29, 3, 55, tzinfo=BERLIN))
    begin = datetime(2026, 3, 29, tzinfo=BERLIN)
    end = begin + timedelta(days=1)
    assert first.start.timestamp() < second.start.timestamp()
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([second, first])
        found = store.upcoming_occurrences(begin, end, limit=1)
    assert [value.id for value in found] == ['first']


def test_prepared_floating_sources_follow_a_changed_system_timezone():
    new_zone = ZoneInfo('America/New_York')
    initial = datetime(2026, 11, 4, 9, tzinfo=BERLIN)
    day = initial + timedelta(days=2)
    with patch('debacal.timeutil.local_zone', return_value=BERLIN), Store(':memory:') as store:
        store.apply_remote([
            floating('single', day, day + timedelta(hours=1)),
            floating('finite', initial, initial + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=3'),
            floating('long', initial, initial + timedelta(hours=1), recurrence_rule='FREQ=DAILY'),
        ])
        begin = datetime(2026, 11, 6, 8, 45, tzinfo=new_zone)
        end = begin + timedelta(minutes=30)
        with patch('debacal.timeutil.local_zone', return_value=new_zone), \
                patch.object(Event, 'from_dict', side_effect=AssertionError('A zone change must not reparse sources')):
            found = store.query_occurrences(begin, end)
            assert {value.series_id or value.id for value in found} == {'single', 'finite', 'long'}
            assert all(value.start.tzinfo == new_zone and value.start.hour == 9 for value in found)
            upcoming = store.upcoming_occurrences(begin, end, limit=3)
            assert [value.id for value in upcoming] == [value.id for value in found]

