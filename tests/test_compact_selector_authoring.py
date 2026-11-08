"""Named weekday and retained calendar preferences stay compact and exact."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import compile_sequence
from debacal.recurrence import expand_event, recurrence_key
from debacal.schedule_authoring import expression_event, expression_matches, reconstruct_authoring
from debacal.sequence_document import document_expression
from debacal.series_operations import partition_series


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)  # Wednesday.


def source(rule, start=START, *, all_day=False, **fields):
    return Event('series', 'local', 'Appointment', start,
                 start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                 recurrence_rule=rule, all_day=all_day, **fields)


def starts(event, years=3):
    return [item.start for item in expand_event(event, event.start - timedelta(days=10),
                                               event.start + timedelta(days=365 * years))]


@pytest.mark.parametrize('rule, fragment', [
    ('FREQ=WEEKLY;BYDAY=MO,WE', 'on=MO+WE'),
    ('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE', 'on=MO+WE'),
    ('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;WKST=SU', 'week_start=SU'),
    ('FREQ=MONTHLY;BYDAY=-1FR', 'on=-1FR'),
    ('FREQ=MONTHLY;BYDAY=-1FR;WKST=SU', 'on=-1FR'),
    ('FREQ=MONTHLY;BYDAY=2MO,-1FR', 'on='),
])
def test_ongoing_supported_rules_have_symbolic_compact_definitions_without_finite_sampling(rule, fragment):
    event = source(rule)
    with patch('debacal.schedule_authoring._finite_starts', side_effect=AssertionError('No finite sampling')):
        view = reconstruct_authoring(event)
    assert not view.structured and fragment in view.expression
    assert view.unbounded and view.starts is None
    assert expression_matches(event, view.expression)
    assert starts(expression_event(event, view.expression)) == starts(event)


@pytest.mark.parametrize('rule', [
    'FREQ=WEEKLY;BYDAY=MO,WE;COUNT=12',
    'FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=1000',
    'FREQ=MONTHLY;BYDAY=-1FR;COUNT=4',
    'FREQ=MONTHLY;BYDAY=-1FR;COUNT=1000',
    'FREQ=MONTHLY;BYDAY=2MO,-1FR;COUNT=8',
    'FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261230T080000Z',
])
def test_finite_rules_keep_selectors_and_true_bounds_instead_of_an_expanded_date_list(rule):
    event = source(rule)
    view = reconstruct_authoring(event)
    assert not view.structured and 'on=' in view.expression
    assert 'count=' in view.expression or 'until=' in view.expression
    assert expression_matches(event, view.expression)
    assert starts(expression_event(event, view.expression)) == starts(event)


@pytest.mark.parametrize('rule', [
    'FREQ=WEEKLY;BYDAY=MO,WE',
    'FREQ=WEEKLY;BYDAY=MO,WE;COUNT=12',
    'FREQ=MONTHLY;BYDAY=-1FR',
    'FREQ=MONTHLY;BYDAY=-1FR;COUNT=5',
])
def test_off_selector_reference_is_retained_only_by_an_explicit_prefix(rule):
    event = source(rule, START.replace(day=3))  # Tuesday.
    view = reconstruct_authoring(event)
    assert not view.structured and '+0d' in view.expression
    assert expression_matches(event, view.expression)
    assert starts(expression_event(event, view.expression)) == starts(event)
    # A bare selector intentionally excludes the off-selector reference.
    assert not expression_matches(event, view.expression.removeprefix('+0d '))


@pytest.mark.parametrize('all_day', [False, True])
def test_multiweekday_dates_with_exclusions_and_an_added_time_keep_exact_meaning(all_day):
    anchor = START.replace(hour=0 if all_day else 9)
    event = source('FREQ=WEEKLY;BYDAY=MO,WE', anchor, all_day=all_day,
                   excluded_dates=(recurrence_key(anchor + timedelta(days=5), all_day),
                                   recurrence_key(anchor + timedelta(days=7), all_day)),
                   recurrence_dates=(recurrence_key(anchor + timedelta(days=1,
                       hours=0 if all_day else 2), all_day),))
    view = reconstruct_authoring(event)
    assert not view.structured and 'on=MO+WE' in view.expression
    assert expression_matches(event, view.expression)
    rebuilt = expression_event(event, view.expression)
    assert starts(rebuilt) == starts(event)
    restored = parse_ics(export_ics([rebuilt]), 'local')[0]
    # ICS DATE values preserve floating calendar dates, not the exporting zone.
    assert [recurrence_key(value, restored.all_day) for value in starts(restored)] == [
        recurrence_key(value, event.all_day) for value in starts(event)]


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_bounded_tail_consumes_earlier_rule_count_only_once(kind):
    rule = 'FREQ=WEEKLY;BYDAY=MO,WE;COUNT=12' if kind == 'weekly' else 'FREQ=MONTHLY;BYDAY=-1FR;COUNT=6'
    event = source(rule)
    initial = starts(event)
    event = replace(event, excluded_dates=tuple(recurrence_key(value) for value in initial[1:3]))
    view = reconstruct_authoring(event)
    assert not view.structured and 'on=' in view.expression
    assert expression_matches(event, view.expression)
    assert starts(expression_event(event, view.expression)) == starts(event)


def test_clipped_monthly_suffix_remembers_day_31_with_compact_text():
    anchor = datetime(2026, 10, 31, 9, tzinfo=ZONE)
    event = replace(source('', anchor), **compile_sequence('every(1m!)', anchor).fields)
    selected = starts(event)[1]  # 30 November, a clamped occurrence.
    actual = next(item for item in expand_event(event, anchor, anchor + timedelta(days=70))
                  if item.start == selected)
    head, tail = partition_series(event, actual)
    assert tail.start.day == 30
    view = reconstruct_authoring(tail)
    assert not view.structured and 'day=31' in view.expression
    assert expression_matches(tail, view.expression)
    assert [value.day for value in starts(expression_event(tail, view.expression))[:5]] == [30, 31, 31, 28, 31]
    finish = anchor + timedelta(days=800)
    assert sorted(item.start for part in (head, tail)
                  for item in expand_event(part, anchor, finish)) == [
        item.start for item in expand_event(event, anchor, finish)]


@pytest.mark.parametrize('text', [
    'every(1m, on=-1FR, count=5)',
    'every(2w, on=MO+WE, week_start=SU, count=8)',
    'every(1m!, day=31, count=5)',
])
def test_extra_selector_state_stays_in_custom_instead_of_lossy_basic_controls(text):
    anchor = datetime(2026, 11, 30, 9, tzinfo=ZONE)
    event = replace(expression_event(source('', anchor), text), schedule_expression=text,
                    schedule_syntax=2)
    draft = ScheduleDraft.from_event(event)
    assert draft.kind == 'custom'
    assert draft.expression == text
    assert expression_matches(event, draft.expression)
    draft.set_expression(' ' + text + ' ', anchor)
    assert draft.kind == 'custom'
    assert expression_matches(replace(event, **draft.fields(anchor)), draft.expression)


def test_weekly_basic_controls_retain_selected_weekdays_when_count_changes():
    event = source('FREQ=WEEKLY;BYDAY=MO,WE;COUNT=8')
    draft = ScheduleDraft.from_event(event)
    assert draft.kind == 'WEEKLY' and draft.weekdays == (0, 2)
    draft.set_regular('WEEKLY', count=6, weekdays=draft.weekdays)
    assert 'on=MO+WE' in draft.expression
    changed = replace(event, **draft.fields(event.start))
    assert expression_matches(changed, draft.expression)
    assert len(starts(changed)) == 6


def test_preexisting_fallback_metadata_is_replaced_by_a_supported_compact_selector():
    event = source('FREQ=WEEKLY;BYDAY=MO,WE')
    event = replace(event, schedule_expression=document_expression(event), schedule_syntax=2)
    view = reconstruct_authoring(event)
    assert not view.structured and view.expression == 'every(1w, on=MO+WE)'
