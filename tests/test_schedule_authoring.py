"""Authoring projections prove the complete canonical schedule before adoption."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from random import Random
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.models import Event
from debacal.recurrence import RecurrenceError, expand_event, occurrence, recurrence_key, recurrence_set
from debacal.regular_schedule import RegularSchedule
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
    assert view.expression == '+0d +3w every(1w)' and view.unbounded and view.starts is None
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


def test_complex_rule_conversion_is_lossless_and_invalid_text_leaves_draft_unchanged():
    event = source('FREQ=WEEKLY;BYDAY=MO,WE')
    draft = ScheduleDraft.from_event(event)
    draft.use_sequence(event)
    assert draft.expression.startswith('schedule{')
    assert expression_matches(event, draft.expression)
    before = deepcopy(draft.__dict__)
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


def test_large_real_exception_has_a_complete_editable_document_without_prefix_expansion():
    event = source()
    draft = ScheduleDraft.from_event(event)
    changed = draft.toggle_date(event, (START + timedelta(weeks=1000)).date())
    assert draft.kind == 'custom' and draft.expression.startswith('schedule{')
    assert changed.recurrence_rule == event.recurrence_rule
    assert changed.excluded_dates == (recurrence_key(START + timedelta(weeks=1000)),)
    draft.use_sequence(changed)
    assert expression_matches(changed, draft.expression)
    assert draft.fields(changed.start)['excluded_dates'] == changed.excluded_dates


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


def test_excluded_reference_has_a_complete_sequence_without_being_reintroduced():
    event = source(excluded_dates=(recurrence_key(START),))
    draft = ScheduleDraft.from_event(event)
    draft.use_sequence(event)
    assert expression_matches(event, draft.expression)
    assert recurrence_key(START) in draft.fields(START)['excluded_dates']
    assert not expression_matches(event, 'every(1w)')


@pytest.mark.parametrize('kind,start', [
    ('MONTHLY', START.replace(year=2027, month=1, day=31)),
    ('YEARLY', START.replace(year=2028, month=2, day=29)),
])
@pytest.mark.parametrize('count', [None, 6, 1000])
def test_unannotated_clamped_standard_rules_have_compact_text_and_regular_controls(kind, start, count):
    regular = RegularSchedule(kind, count=count, ending='never' if count is None else 'count', clamp=True)
    event = source(regular.rule(start), start)
    assert not event.schedule_expression
    view = reconstruct_authoring(event)
    assert not view.structured and '!' in view.expression
    assert expression_matches(event, view.expression)
    draft = ScheduleDraft.from_event(event)
    assert draft.kind == kind and draft.clamp and draft.count == count
    assert not expression_matches(event, f'every(1{"m" if kind == "MONTHLY" else "y"}'
                                  + (f', count={count})' if count is not None else ')'))


@pytest.mark.parametrize('bound_reference', [False, True])
@pytest.mark.parametrize('duration_change', [False, True])
def test_cross_zone_noop_and_duration_edits_preserve_valid_source_literal(bound_reference, duration_change):
    anchor = START.replace(tzinfo=timezone.utc)
    literal = '  +0d 11:30-5.  '
    event = replace(expression_event(source('', anchor), literal), schedule_expression=literal,
                    schedule_syntax=2, expression_anchor=anchor.date().isoformat())
    draft = ScheduleDraft.from_event(event, authoring_zone=ZONE)
    assert '12:30' in draft.expression
    selected = occurrence(event, datetime(2026, 11, 5, 11, 30, tzinfo=timezone.utc)) if bound_reference else event
    if bound_reference:
        draft.bind_primary_reference(selected.start, selected.end)
    fields = draft.fields(selected.start, selected.end + (timedelta(hours=1) if duration_change else timedelta()))
    saved = replace(event, **fields)
    assert saved.schedule_expression == literal
    assert expression_matches(saved, saved.schedule_expression)
    assert saved.recurrence_dates == event.recurrence_dates and saved.timezone_id == event.timezone_id


def test_cross_zone_stale_annotation_is_repaired_in_the_source_timezone():
    anchor = START.replace(tzinfo=timezone.utc)
    event = replace(expression_event(source('', anchor), '+0d 11:30-5.'),
                    schedule_expression='+0d 13:00-5.', schedule_syntax=2)
    draft = ScheduleDraft.from_event(event, authoring_zone=ZONE)
    saved = replace(event, **draft.fields(event.start, event.end))
    assert expression_matches(saved, saved.schedule_expression)
    assert '11:30' in saved.schedule_expression and saved.timezone_id == event.timezone_id


def test_cross_zone_graphical_occurrence_edit_stores_the_definition_in_the_source_timezone():
    anchor = START.replace(tzinfo=timezone.utc)
    event = replace(expression_event(source('', anchor), '+0d 11:30-5.'),
                    schedule_expression='+0d 11:30-5.', schedule_syntax=2)
    selected = occurrence(event, datetime(2026, 11, 5, 11, 30, tzinfo=timezone.utc))
    draft = ScheduleDraft.from_event(event, authoring_zone=ZONE)
    changed = draft.edit_occurrence(event, selected, selected.start + timedelta(hours=1),
                                    selected.end + timedelta(hours=1))
    assert '13:30' in draft.expression
    assert '12:30' in changed.schedule_expression
    assert changed.timezone_id == event.timezone_id
    assert expression_matches(changed, changed.schedule_expression)
    assert set(changed.recurrence_overrides) == {selected.recurrence_id}


def test_cross_zone_reanchoring_retains_the_source_month_clamp_instructions():
    anchor = datetime(2027, 1, 31, 9, tzinfo=timezone.utc)
    literal = '+0d 2*+1m!'
    event = replace(expression_event(source('', anchor), literal), schedule_expression=literal,
                    schedule_syntax=2)
    draft = ScheduleDraft.from_event(event, authoring_zone=ZONE)
    moved = anchor.replace(day=30)
    saved = replace(event, start=moved, end=moved + timedelta(hours=1), **draft.fields(moved))
    assert saved.schedule_expression == literal
    assert expression_matches(saved, literal)
    assert [value.start for value in expand_event(saved, moved, moved + timedelta(days=100))] == [
        moved, datetime(2027, 2, 28, 9, tzinfo=timezone.utc), datetime(2027, 3, 30, 9, tzinfo=timezone.utc)]


def test_cross_zone_reanchoring_translates_explicit_actual_clocks_once():
    anchor = START.replace(tzinfo=timezone.utc)
    literal = '+0d 11:30-5.'
    event = replace(expression_event(source('', anchor), literal), schedule_expression=literal,
                    schedule_syntax=2)
    draft = ScheduleDraft.from_event(event, authoring_zone=ZONE)
    shift = timedelta(days=1, hours=2)
    moved = anchor + shift
    saved = replace(event, start=moved, end=moved + timedelta(hours=1), **draft.fields(moved))
    assert expression_matches(saved, saved.schedule_expression)
    assert [value.start for value in expand_event(saved, moved, moved + timedelta(days=5))] == [
        moved, datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc)]
