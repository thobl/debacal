"""Selector intersections stay exact through certification and repeated moves."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.recurrence import expand_event
from debacal.regular_schedule import RegularSchedule
from debacal.schedule_authoring import (
    expression_matches, recurrence_matches, reconstruct_authoring, simplified_rule,
)
from debacal.store import Store


START = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)


def source(rule):
    return Event('series', 'local', 'Independent calendar', START,
                 START + timedelta(hours=1), recurrence_rule=rule)


@pytest.mark.parametrize(('rule', 'different', 'expected'), [
    ('FREQ=MONTHLY;BYDAY=MO;BYMONTHDAY=1;COUNT=6',
     'FREQ=MONTHLY;BYDAY=MO;COUNT=6',
     ['2026-06-01', '2027-02-01', '2027-03-01', '2027-11-01', '2028-05-01', '2029-01-01']),
    ('FREQ=YEARLY;BYMONTH=6;BYDAY=MO;COUNT=8',
     'FREQ=YEARLY;BYDAY=MO;COUNT=8',
     ['2026-06-01', '2026-06-08', '2026-06-15', '2026-06-22', '2026-06-29',
      '2027-06-07', '2027-06-14', '2027-06-21']),
    ('FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=1;COUNT=3',
     'FREQ=YEARLY;BYMONTHDAY=1;COUNT=3',
     ['2026-06-01', '2027-06-01', '2028-06-01']),
    ('FREQ=HOURLY;BYHOUR=9;COUNT=3', 'FREQ=HOURLY;COUNT=3',
     ['2026-06-01', '2026-06-02', '2026-06-03']),
])
def test_matching_reference_selectors_are_not_proof_of_equivalence(rule, different, expected):
    event = source(rule)
    values = expand_event(event, START - timedelta(days=1), START.replace(year=2030))
    assert [item.start.isoformat() for item in values] == [day + 'T09:00:00+00:00' for day in expected]
    assert not recurrence_matches(event, source(different))
    assert not recurrence_matches(source(different), event)
    assert recurrence_matches(event, replace(event, recurrence_rule=simplified_rule(event)))


@pytest.mark.parametrize('ending', ['', ';COUNT=6'])
def test_monthday_weekday_intersection_survives_reconstruction_wire_and_two_moves(ending):
    event = source('FREQ=MONTHLY;BYDAY=MO;BYMONTHDAY=1' + ending)
    wrong = 'every(1m, on=MO' + (', count=6)' if ending else ')')
    assert not expression_matches(event, wrong)
    view = reconstruct_authoring(event)
    assert expression_matches(event, view.expression)
    event = replace(event, schedule_expression=view.expression, schedule_syntax=2)
    restored, = parse_ics(export_ics([event]), 'local')
    with Store(':memory:') as store:
        store.apply('Save reconstructed schedule', [restored])
        for hour in (10, 11):
            current = store.get_event(restored.id)
            selected = expand_event(current, START - timedelta(days=1), START + timedelta(days=60))[0]
            target = selected.start.replace(hour=hour)
            store.move_selection([selected], 'group', selected, target, target + timedelta(hours=1))
            moved = store.get_event(restored.id)
            values = expand_event(moved, START - timedelta(days=1), START + timedelta(days=60))
            assert [item.start for item in values] == [START.replace(hour=hour)]
            assert expression_matches(moved, moved.schedule_expression)


def test_overflow_controls_cannot_rewrite_a_span_that_does_not_fit_any_month():
    start = datetime(2026, 10, 31, 9, tzinfo=timezone.utc)
    rule = 'FREQ=MONTHLY;BYMONTHDAY=31,-2;BYSETPOS=1;COUNT=3'
    event = replace(source(rule), start=start, end=start + timedelta(hours=1))
    values = expand_event(event, start, start.replace(year=2027, month=3, day=1))
    assert [item.start.date().isoformat() for item in values] == [
        '2026-10-31', '2026-11-29', '2026-12-30', '2027-01-30']
    assert RegularSchedule.from_rule(rule, start) is None


def test_retained_second_phase_anchor_does_not_certify_a_shorter_count_as_exact_until():
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2025, 10, 26, 2, 15, tzinfo=zone, fold=1)
    end = datetime.fromtimestamp(start.timestamp() + 900, zone)
    counted = Event('late', 'local', 'Retained repeated hour', start, end,
                    recurrence_rule='FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;COUNT=1')
    bounded = replace(counted, recurrence_rule='FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20261025T003000Z')
    begin, finish = datetime(2025, 10, 1, tzinfo=zone), datetime(2027, 1, 1, tzinfo=zone)
    # The native rule's first phase is intentional. The later cutoff includes
    # a second year's occurrence even though the reference carries fold=1.
    assert [item.start.astimezone(timezone.utc).isoformat() for item in expand_event(counted, begin, finish)] == [
        '2025-10-26T00:15:00+00:00']
    assert [item.start.astimezone(timezone.utc).isoformat() for item in expand_event(bounded, begin, finish)] == [
        '2025-10-26T00:15:00+00:00', '2026-10-25T00:15:00+00:00']
    assert not recurrence_matches(counted, bounded)
    assert not recurrence_matches(bounded, counted)
    assert RegularSchedule.from_rule(counted.recurrence_rule, start) is None
    assert RegularSchedule.from_rule(bounded.recurrence_rule, start) is None

    view = reconstruct_authoring(bounded)
    assert view.structured and expression_matches(bounded, view.expression)
    saved = replace(bounded, schedule_expression=view.expression, schedule_syntax=2)
    with Store(':memory:') as store:
        store.apply('Save retained second-phase document', [saved])
        for hour in (3, 4):
            current = store.get_event(saved.id)
            selected = expand_event(current, begin, finish)[0]
            target = selected.start.replace(hour=hour, fold=0)
            store.move_selection([selected], 'group', selected, target, target + timedelta(minutes=15))
            moved = store.get_event(saved.id)
            assert [(item.start.date().isoformat(), item.start.hour, item.start.minute)
                    for item in expand_event(moved, begin, finish)] == [
                ('2025-10-26', hour, 15), ('2026-10-25', hour, 15)]
            assert expression_matches(moved, moved.schedule_expression)


@pytest.mark.parametrize('frequency', ['MONTHLY', 'YEARLY'])
def test_regular_controls_refuse_only_an_actually_ambiguous_second_phase(frequency):
    zone = ZoneInfo('Europe/Berlin')
    late = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    rule = f'FREQ={frequency};COUNT=1'
    assert RegularSchedule.from_rule(rule, late) is None
    assert RegularSchedule.from_rule(rule, late.replace(fold=0)) is not None
    assert RegularSchedule.from_rule(rule, late.astimezone(timezone.utc).replace(fold=1)) is not None
