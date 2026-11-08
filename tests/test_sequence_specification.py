"""Certificates and controls agree on exact, bounded recurrence semantics."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.regular_schedule import RegularSchedule
from debacal.schedule_authoring import (
    expression_event, expression_matches, reconstruct_authoring, recurrence_matches,
)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def source(rule, **changes):
    return replace(Event('series', 'local', 'Meeting', START, START + timedelta(hours=1),
                         recurrence_rule=rule, timezone_id=ZONE.key), **changes)


@pytest.mark.parametrize('offset,count', [(-1, 3), (0, 4), (1, 4)])
def test_exact_until_includes_only_occurrences_at_or_before_its_instant(offset, count):
    cutoff = (START + timedelta(days=3, seconds=offset)).astimezone(timezone.utc)
    rule = 'FREQ=DAILY;UNTIL=' + cutoff.strftime('%Y%m%dT%H%M%SZ')
    regular = RegularSchedule.from_rule(rule, START)
    assert regular.count == count
    assert regular.until == (START + timedelta(days=count - 1)).date()
    event = source(rule)
    draft = ScheduleDraft.from_event(event)
    assert draft.count == len(draft.starts(START)) == count
    text = reconstruct_authoring(event).expression
    assert len(expand_sequence(text, START)) == count


def test_weekly_exact_cutoff_on_matching_weekday_does_not_round_up():
    cutoff = (START + timedelta(weeks=3, seconds=-1)).astimezone(timezone.utc)
    rule = 'FREQ=WEEKLY;UNTIL=' + cutoff.strftime('%Y%m%dT%H%M%SZ')
    regular = RegularSchedule.from_rule(rule, START)
    assert regular.count == 3 and regular.until == (START + timedelta(weeks=2)).date()


def test_off_rule_weekday_primary_does_not_show_a_misleading_regular_count():
    event = source('FREQ=WEEKLY;BYDAY=TU;COUNT=3')
    assert START.weekday() == 2
    assert RegularSchedule.from_rule(event.recurrence_rule, START) is None
    draft = ScheduleDraft.from_event(event)
    assert draft.kind == 'advanced'
    assert draft.fields(START)['recurrence_rule'] == event.recurrence_rule


@pytest.mark.parametrize('frequency,count', [('DAILY', 50000), ('WEEKLY', 1000), ('MONTHLY', 600)])
def test_large_bounded_rules_have_exact_symbolic_authoring_certificates(frequency, count):
    event = source(f'FREQ={frequency};COUNT={count}')
    view = reconstruct_authoring(event)
    assert view.starts is None and not view.unbounded
    assert f'count={count}' in view.expression
    with patch('debacal.schedule_authoring._finite_starts', side_effect=AssertionError('Must prove symbolically')):
        assert expression_matches(event, view.expression)
        assert recurrence_matches(event, expression_event(event, view.expression))
    annotated = replace(event, schedule_expression=view.expression, schedule_syntax=2,
                        expression_anchor=START.date().isoformat())
    draft = ScheduleDraft.from_event(annotated)
    assert draft.kind == frequency and draft.count == count
    assert draft.validation_starts(START) == [START]
    assert draft.fields(START, event.end)['schedule_expression'] == view.expression


def test_large_count_and_exact_until_compare_without_materializing_history():
    count = 5000
    final = START + timedelta(days=count - 1)
    first = source(f'FREQ=DAILY;COUNT={count}')
    second = source('FREQ=DAILY;UNTIL=' + final.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    with patch('debacal.schedule_authoring._finite_starts', side_effect=AssertionError('Must prove symbolically')):
        assert recurrence_matches(first, second)
        assert not recurrence_matches(first, replace(second, recurrence_dates=((START + timedelta(days=count + 3)).isoformat(),)))


def test_reconstructed_set_order_is_deterministic_and_keeps_the_primary_first():
    dates = [START + timedelta(days=8), START - timedelta(days=2), START + timedelta(days=11)]
    first = source('', recurrence_dates=tuple(value.isoformat() for value in dates))
    second = replace(first, recurrence_dates=tuple(reversed(first.recurrence_dates)))
    a, b = reconstruct_authoring(first), reconstruct_authoring(second)
    assert a == b
    assert a.starts[0] == START
    assert list(a.starts[1:]) == sorted(dates, key=lambda value: value.timestamp())
    assert set(expand_sequence(a.expression, START)) == {START, *dates}


def test_monthly_skipping_stays_a_standard_rule_through_custom_view_and_text_edit():
    start = START.replace(year=2027, month=1, day=31)
    event = source('FREQ=MONTHLY;COUNT=5', start=start, end=start + timedelta(hours=1))
    view = reconstruct_authoring(event)
    assert view.expression == 'every(1m, count=5)'
    draft = ScheduleDraft.from_event(event)
    draft.use_sequence(event)
    assert recurrence_matches(event, replace(event, **draft.fields(start)))
    draft.set_expression(' ' + view.expression + ' ', start)
    fields = draft.fields(start)
    assert 'FREQ=MONTHLY' in fields['recurrence_rule'] and 'COUNT=5' in fields['recurrence_rule']
    assert not fields['recurrence_dates']
    assert [value.date() for value in draft.starts(start)] == [
        start.date(), start.replace(month=3).date(), start.replace(month=5).date(),
        start.replace(month=7).date(), start.replace(month=8).date()]


@pytest.mark.parametrize('month,day,expression,kind', [
    (1, 31, '+1m', 'custom'), (1, 29, '2*+1m', 'custom'),
    (1, 15, '2*+1m', 'MONTHLY'), (3, 31, '2*+12m', 'YEARLY'),
    (2, 29, '+1y', 'custom'),
])
def test_regular_classification_preserves_calendar_unit_intent(month, day, expression, kind):
    start = START.replace(year=2028, month=month, day=day)
    event = expression_event(source('', start=start, end=start + timedelta(hours=1)), expression)
    event = replace(event, schedule_expression=expression, schedule_syntax=2,
                    expression_anchor=start.date().isoformat())
    reopened = ScheduleDraft.from_event(event)
    assert reopened.kind == kind
    typed = ScheduleDraft.from_event()
    typed.set_expression(expression, start)
    assert typed.kind == kind
    assert reopened.expression == typed.expression == expression
