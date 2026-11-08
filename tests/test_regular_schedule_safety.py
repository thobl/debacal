"""Regular controls must describe the literal source rule, including its root."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.recurrence import expand_event
from debacal.regular_schedule import RegularSchedule
from debacal.schedule_authoring import expression_matches, reconstruct_authoring, recurrence_matches
from debacal.store import Store


@pytest.mark.parametrize('month,day,selectors,until,dates,short_dates', [
    (10, 31, 'BYMONTHDAY=31,-2;BYSETPOS=1', '20261130T235959Z',
     ['2026-10-31', '2026-11-29', '2026-12-30', '2027-01-30'], ['2026-10-31', '2026-11-29']),
    (4, 30, 'BYMONTHDAY=30,-2;BYSETPOS=1', '20260531T235959Z',
     ['2026-04-30', '2026-05-30', '2026-06-29', '2026-07-30'], ['2026-04-30', '2026-05-30']),
    (4, 30, 'BYMONTHDAY=30,31;BYSETPOS=-2', '20260531T235959Z',
     ['2026-04-30', '2026-05-30', '2026-07-30', '2026-08-30'], ['2026-04-30', '2026-05-30']),
])
def test_overflow_rules_do_not_certify_a_different_count_or_root(
        month, day, selectors, until, dates, short_dates):
    start = datetime(2026, month, day, 9, tzinfo=timezone.utc)
    rule = f'FREQ=MONTHLY;{selectors}'
    source = Event('series', 'local', 'Independent dates', start, start + timedelta(days=2),
                   recurrence_rule=rule + ';COUNT=3')
    shorter = replace(source, recurrence_rule=rule + ';UNTIL=' + until)
    query_start, query_end = start - timedelta(days=2), start + timedelta(days=200)
    assert [item.start.date().isoformat() for item in expand_event(source, query_start, query_end)] == dates
    assert [item.start.date().isoformat() for item in expand_event(shorter, query_start, query_end)] == short_dates
    assert RegularSchedule.from_rule(source.recurrence_rule, start) is None
    assert RegularSchedule.from_rule(shorter.recurrence_rule, start) is None
    assert not recurrence_matches(source, shorter)
    assert not recurrence_matches(shorter, source)

    view = reconstruct_authoring(source)
    assert expression_matches(source, view.expression)
    source = replace(source, schedule_expression=view.expression, schedule_syntax=2)
    loaded, = parse_ics(export_ics([source]), 'local')
    with Store(':memory:') as store:
        store.apply('Save literal calendar', [loaded])
        for hour in (10, 11):
            current = store.get_event(source.id)
            target = current.start.replace(hour=hour)
            store.move_selection([current], 'group', current, target, target + timedelta(days=2))
            moved = store.get_event(source.id)
            actual = expand_event(moved, query_start, query_end)
            assert [item.start.date().isoformat() for item in actual] == dates
            assert {item.start.hour for item in actual} == {hour}
            assert expression_matches(moved, moved.schedule_expression)
