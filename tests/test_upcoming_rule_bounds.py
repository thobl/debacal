"""Top-K agendas prune generation without losing ties, overlaps or safe sources."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_range_index import EventRangeIndex
from debacal.models import Event


@pytest.mark.parametrize('zone_name', ['UTC', 'Europe/Berlin', 'America/New_York'])
@pytest.mark.parametrize('day', [(2026, 3, 29), (2026, 10, 25)])
@pytest.mark.parametrize('limit', [1, 3, 10])
def test_bounded_agenda_matches_full_oracle_across_clocks_and_dst(zone_name, day, limit, monkeypatch):
    zone = ZoneInfo(zone_name)
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: zone)
    start = datetime(*day, 1, tzinfo=zone)
    end = start + timedelta(days=30)
    index = EventRangeIndex()
    for number in range(14):
        first = (start - timedelta(days=21)).replace(hour=number % 8)
        event = Event(f'rule-{number}', f'calendar-{number % 2}', 'Repeated', first,
                      first + timedelta(hours=1), recurrence_rule='FREQ=DAILY;INTERVAL=2')
        if number % 5 == 0:
            event = replace(event, floating_time=True)
        elif number % 7 == 0:
            first = first.replace(hour=0)
            event = replace(event, start=first, end=first + timedelta(days=2), all_day=True)
        index.upsert(event)
    for number in range(5):
        first = start + timedelta(minutes=10 * number)
        index.upsert(Event(f'ordinary-{number}', 'calendar-0', 'Ordinary', first,
                           first + timedelta(minutes=15)))
    # Include a point, a long ongoing event, and ties on identical endpoints.
    index.upsert(Event('point', 'calendar-0', 'Point', start, start))
    index.upsert(Event('ongoing', 'calendar-0', 'Ongoing', start - timedelta(days=3),
                       start + timedelta(days=4)))
    for number in range(2):
        index.upsert(Event(f'tie-{number}', 'calendar-0', 'Tie', start,
                           start + timedelta(minutes=15)))
    complete, failures = index.query(start, end)
    assert not failures
    expected = sorted(complete, key=lambda value: index.order(value, zone))[:limit]
    actual, failures = index.query(start, end, limit=limit)
    assert not failures
    assert actual == expected


def test_validated_early_events_bound_later_rules_without_materializing_month():
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    index = EventRangeIndex()
    for number in range(10):
        first = start + timedelta(minutes=number)
        index.upsert(Event(f'early-{number}', 'local', 'Early', first, first + timedelta(minutes=1)))
    for number in range(25):
        first = start + timedelta(hours=3, minutes=number)
        index.upsert(Event(f'later-{number}', 'local', 'Later daily', first,
                           first + timedelta(minutes=30), recurrence_rule='FREQ=DAILY'))
    calls = []
    from debacal.prepared_recurrence import PreparedSeries
    original = PreparedSeries.query_rule

    def observe(self, lower, upper, **kwargs):
        values = original(self, lower, upper, **kwargs)
        calls.append((upper, len(values)))
        return values

    with patch.object(PreparedSeries, 'query_rule', observe):
        actual, failures = index.query(start, end, limit=10)
    assert not failures and len(actual) == 10
    assert len(calls) == 25 and all(upper <= start + timedelta(minutes=10) for upper, _count in calls)
    assert sum(count for _upper, count in calls) == 0


def test_unvalidated_explicit_dates_cannot_hide_later_healthy_rule_results():
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    index = EventRangeIndex()
    broken = Event('broken', 'local', 'Broken source', start + timedelta(minutes=1),
                   start + timedelta(minutes=2), recurrence_rule='FREQ=DAILY')
    healthy = Event('healthy', 'local', 'Healthy source', start + timedelta(hours=1),
                    start + timedelta(hours=2), recurrence_rule='FREQ=DAILY')
    index.upsert(broken)
    index.upsert(healthy)
    for limit in (1, 3):
        with patch.object(index.sources['broken'].prepared, 'query_rule', side_effect=ValueError('Invalid rule')):
            result, failures = index.query(start, start + timedelta(days=30), limit=limit, issues=[])
        assert len(result) == limit and all(value.series_id == 'healthy' for value in result)
        assert len(failures) == 1 and failures[0][0].id == 'broken'
