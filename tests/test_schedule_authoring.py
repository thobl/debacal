"""Authoring projections prove the complete canonical schedule before adoption."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.models import Event
from debacal.recurrence import RecurrenceError, expand_event, recurrence_key, recurrence_set
from debacal.schedule_authoring import (
    expression_event, expression_matches, reconstruct_authoring,
)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def source(rule='FREQ=WEEKLY', start=START, *, all_day=False, **values):
    return Event('series', 'local', 'An appointment', start, start + timedelta(days=1) if all_day else start + timedelta(hours=1),
                 all_day=all_day, recurrence_rule=rule, timezone_id=getattr(start.tzinfo, 'key', ''), **values)


def stamps(event, days=3700):
    return [value.start.timestamp() for value in expand_event(event, event.start - timedelta(days=20), event.start + timedelta(days=days))]


def test_endless_weekly_deleted_second_and_third_is_a_complete_compact_sequence():
    event = source(excluded_dates=tuple(recurrence_key(START + timedelta(weeks=week)) for week in (1, 2)))
    view = reconstruct_authoring(event)
    assert view.expression == '+3 every(1w)' and view.unbounded and view.starts is None
    assert expression_matches(event, view.expression)
    assert not expression_matches(event, 'every(1w)')
    assert stamps(expression_event(event, view.expression)) == stamps(event)


@pytest.mark.parametrize('frequency,start', [
    ('DAILY', START), ('WEEKLY', START),
    ('MONTHLY', START.replace(year=2027, month=1, day=31)),
    ('YEARLY', START.replace(year=2028, month=2, day=29)),
])
@pytest.mark.parametrize('interval', [1, 2, 3])
def test_modified_calendar_tails_roundtrip_without_month_or_leap_date_clipping(frequency, start, interval):
    event = source(f'FREQ={frequency};INTERVAL={interval}', start)
    base = list(recurrence_set(event).xafter(start, count=7, inc=True))
    event = replace(event, excluded_dates=tuple(recurrence_key(base[index]) for index in (1, 2)),
                    recurrence_dates=(recurrence_key(start + timedelta(days=1, hours=2)),))
    view = reconstruct_authoring(event)
    assert expression_matches(event, view.expression)
    horizon = 9000 if frequency == 'DAILY' else 365 * 40
    assert stamps(expression_event(event, view.expression), days=horizon) == stamps(event, days=horizon)


def test_redundant_distant_exceptions_do_not_force_an_unbounded_prefix_expansion():
    event = source(recurrence_dates=(recurrence_key(START + timedelta(weeks=10000)),),
                   excluded_dates=(recurrence_key(START + timedelta(weeks=10000, days=1)),))
    assert reconstruct_authoring(event).expression == 'every(1w)'


def test_failed_sequence_conversion_and_invalid_text_leave_the_entire_draft_unchanged():
    event = source('FREQ=WEEKLY;BYDAY=MO,WE')
    draft = ScheduleDraft.from_event(event)
    before = deepcopy(draft.__dict__)
    with pytest.raises(RecurrenceError):
        draft.use_sequence(event)
    assert draft.__dict__ == before
    with pytest.raises(ValueError):
        draft.set_expression('2*(+1', START)
    assert draft.__dict__ == before


def test_a_converted_finite_advanced_rule_can_still_move_as_an_exact_sequence():
    event = source('FREQ=MONTHLY;BYDAY=-1WE;COUNT=4')
    draft = ScheduleDraft.from_event(event)
    draft.use_sequence(event)
    start = event.start + timedelta(days=1)
    end = event.end + timedelta(days=1)
    moved = replace(event, start=start, end=end, **draft.fields(start, end))
    assert stamps(moved, 200) == [value + timedelta(days=1).total_seconds() for value in stamps(event, 200)]
    assert expression_matches(moved, moved.schedule_expression)


def test_large_real_exception_keeps_a_canonical_advanced_view_without_partial_frequency_state():
    event = source()
    draft = ScheduleDraft.from_event(event)
    changed = draft.toggle_date(event, (START + timedelta(weeks=1000)).date())
    assert draft.kind == 'advanced' and not draft.expression
    assert changed.recurrence_rule == event.recurrence_rule
    assert changed.excluded_dates == (recurrence_key(START + timedelta(weeks=1000)),)
    before = deepcopy(draft.__dict__)
    with pytest.raises(RecurrenceError, match='prefix'):
        draft.use_sequence(changed)
    assert draft.__dict__ == before


def test_a_stale_daily_annotation_does_not_change_a_weekly_rule_when_the_primary_moves():
    event = source(schedule_expression='every(1d)', schedule_syntax=2,
                   excluded_dates=tuple(recurrence_key(START + timedelta(weeks=week)) for week in (1, 2)))
    draft = ScheduleDraft.from_event(event)
    assert expression_matches(event, draft.expression)
    moved = START + timedelta(days=1)
    fields = draft.fields(moved, moved + timedelta(hours=1))
    edited = replace(event, start=moved, end=moved + timedelta(hours=1), **fields)
    assert 'FREQ=WEEKLY' in edited.recurrence_rule
    assert stamps(edited) == [value + timedelta(days=1).total_seconds() for value in stamps(event)]
    assert expression_matches(edited, edited.schedule_expression)


def test_saving_repairs_only_stale_current_syntax_metadata_and_retains_canonical_fields():
    event = source(schedule_expression='every(1d)', schedule_syntax=2,
                   excluded_dates=(recurrence_key(START + timedelta(weeks=1)),))
    draft = ScheduleDraft.from_event(event)
    fields = draft.fields(event.start, event.end)
    assert fields['schedule_expression'] == draft.expression
    assert expression_matches(event, fields['schedule_expression'])
    for name in ('recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides'):
        assert fields[name] == getattr(event, name)


@pytest.mark.parametrize('rule', ['FREQ=WEEKLY;INTERVAL=02', 'FREQ=WEEKLY;BYHOUR=09'])
def test_harmless_integer_spellings_do_not_block_standard_tail_authoring(rule):
    event = source(rule)
    view = reconstruct_authoring(event)
    assert view.expression == ('every(2w)' if 'INTERVAL' in rule else 'every(1w)')
    assert expression_matches(event, view.expression)


def test_seeded_finite_edits_to_weekly_tails_always_describe_the_complete_set():
    rng = Random(20261108)
    for _ in range(25):
        excluded = tuple(recurrence_key(START + timedelta(weeks=week)) for week in rng.sample(range(1, 14), 4))
        dates = tuple(recurrence_key(START + timedelta(days=day, hours=2)) for day in rng.sample(range(-10, 50), 3))
        event = source(excluded_dates=excluded, recurrence_dates=dates)
        view = reconstruct_authoring(event)
        assert expression_matches(event, view.expression)
        assert stamps(expression_event(event, view.expression)) == stamps(event)


def test_excluded_primary_cannot_be_silently_reintroduced_by_converting_to_text():
    event = source(excluded_dates=(recurrence_key(START),))
    draft = ScheduleDraft.from_event(event)
    before = deepcopy(draft.__dict__)
    with pytest.raises(RecurrenceError, match='primary'):
        draft.use_sequence(event)
    assert draft.__dict__ == before
    assert not expression_matches(event, 'every(1w)')
