"""Endless custom tails use standard recurrence fields without finite expansion."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import (
    PatternError, compile_sequence, expand_sequence, expression_for_regular,
    sequence_tail, shift_unbounded_expression,
)
from debacal.recurrence import (
    RecurrenceError, expand_event, occurrence, recurrence_key, rules_equivalent, updated_series,
)
from debacal.series_operations import partition_series
from debacal.schedule_authoring import expression_event, expression_matches
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def authored(expression='every(1w)', start=START, **changes):
    values = dict(id='series', calendar_id='local', ical_uid='endless-series', title='Meeting',
                  start=start, end=start + timedelta(hours=1), timezone_id='Europe/Berlin',
                  schedule_expression=expression, expression_anchor=start.date().isoformat(),
                  schedule_syntax=2)
    prototype = Event(**(values | {name: value for name, value in changes.items()
                                  if name in ('start', 'end', 'all_day', 'timezone_id')}))
    return replace(expression_event(prototype, expression), **changes)


def visible(event, days=50):
    return expand_event(event, event.start - timedelta(days=1), event.start + timedelta(days=days))


def stamps(values):
    return [value.astimezone(timezone.utc) for value in values]


def moved_selection(source, selected, delta):
    fields = {name: getattr(source, name) for name in (
        'recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides',
        'schedule_expression', 'expression_anchor', 'schedule_syntax', 'timezone_id')}
    return replace(selected, start=selected.start + delta, end=selected.end + delta, **fields)


def assert_roundtrip(event):
    raw = export_ics([event])
    loaded = parse_ics(raw.decode(), event.calendar_id)[0]
    assert 'RRULE:' in raw.decode()
    assert loaded.schedule_expression == event.schedule_expression
    assert loaded.expression_anchor == event.expression_anchor
    assert loaded.schedule_syntax == 2
    assert loaded.recurrence_dates == event.recurrence_dates
    assert loaded.excluded_dates == event.excluded_dates
    if event.all_day:
        assert [item.start.date() for item in visible(loaded)] == [item.start.date() for item in visible(event)]
    else:
        assert stamps(item.start for item in visible(loaded)) == stamps(item.start for item in visible(event))
    return loaded


@pytest.mark.parametrize('text,frequency,interval', [
    ('every(1d)', 'DAILY', 1), ('every(2w)', 'WEEKLY', 2),
    ('every(1m)', 'MONTHLY', 1), ('every(3y)', 'YEARLY', 3),
    ('  every(2w)  ', 'WEEKLY', 2), ('EVERY( 2 W )', 'WEEKLY', 2),
])
def test_standard_tails_are_ordinary_unbounded_rules(text, frequency, interval):
    compiled = compile_sequence(text, START)
    assert compiled.recurrence_rule == f'FREQ={frequency};INTERVAL={interval}'
    assert compiled.prefix_starts == ()
    assert compiled.recurrence_dates == compiled.excluded_dates == ()
    assert expression_for_regular(frequency, interval) == f'every({interval}{sequence_tail(text).unit})'
    with pytest.raises(PatternError, match='no finite list'):
        expand_sequence(text, START)


def test_prefix_is_preserved_with_standard_date_exceptions():
    source = authored('+0d +1d +3d every(2d)')
    expected = [0, 1, 4, 6, 8, 10]
    assert [(item.start.date() - START.date()).days for item in visible(source, 11)] == expected
    assert source.recurrence_dates == (recurrence_key(START + timedelta(days=1)),)
    assert source.excluded_dates == (recurrence_key(START + timedelta(days=2)),)
    assert_roundtrip(source)


def test_tail_can_change_weekday_and_clock_without_changing_the_primary():
    source = authored('+0d 10:00-7.11.2026 every(1w)')
    instances = visible(source, 20)
    assert instances[0].start == START
    assert [item.start.weekday() for item in instances[1:]] == [5, 5, 5]
    assert [item.start.hour for item in instances[1:]] == [10, 10, 10]
    assert 'BYDAY=SA' in source.recurrence_rule
    assert 'BYHOUR=10' in source.recurrence_rule
    assert_roundtrip(source)


def test_monthly_and_yearly_tails_skip_missing_dates_like_regular_controls():
    january = START.replace(year=2027, month=1, day=31)
    source = authored('every(1m)', january)
    assert [item.start.month for item in visible(source, 155)] == [1, 3, 5]
    # Relative month steps skip missing dates; explicit clamps stay available.
    assert expand_sequence('+1m', january)[-1].date() == january.replace(month=3).date()
    assert expand_sequence('+1m!', january)[-1].day == 28
    leap = START.replace(year=2028, month=2, day=29)
    source = authored('every(1y)', leap)
    assert [item.start.year for item in visible(source, 365 * 9)] == [2028, 2032, 2036]


def test_all_day_tails_use_date_keys_and_reject_clock_instructions():
    primary = START.replace(hour=0)
    source = authored('+0d +1d +3d every(2d)', primary, all_day=True, end=primary + timedelta(days=1))
    assert source.recurrence_dates == ('2026-11-05',)
    assert source.excluded_dates == ('2026-11-06',)
    assert_roundtrip(source)
    with pytest.raises(PatternError, match='timed event'):
        compile_sequence('10:00 every(1d)', primary, all_day=True)


@pytest.mark.parametrize('text', [
    'every(0d)', 'every(1000w)', 'every(1h)', 'every(1d) +1',
    'every(1d) every(1w)', '2*every(1d)', '(every(1d))', 'every()',
])
def test_malformed_or_nested_tails_fail_before_any_schedule_change(text):
    with pytest.raises(PatternError):
        compile_sequence(text, START)


@pytest.mark.parametrize('text', ['+1w every(2w)', '+1m every(2m)', '+1y every(2y)', '+1d every(2d)'])
def test_misaligned_tail_phase_is_explicitly_rejected(text):
    with pytest.raises(PatternError, match='does not align'):
        compile_sequence(text, START)


def test_compile_visits_only_a_bounded_prefix_and_refuses_distant_exception_sets():
    source = authored('every(1d)')
    assert len(visible(source, 3)) == 3
    with pytest.raises(PatternError, match='10,000'):
        compile_sequence('1.1.2100 every(1d)', START)


def test_invalid_wall_times_and_unrepresentable_subseconds_are_rejected():
    with pytest.raises(PatternError, match='clocks change'):
        compile_sequence('02:30-28.3.2027 every(1d)', START)
    with pytest.raises(PatternError, match='whole-second'):
        compile_sequence('every(1d)', START.replace(microsecond=1))


def test_model_compiles_text_and_validates_the_prefix_without_expanding_the_tail():
    draft = ScheduleDraft()
    regular = draft.set_expression(' every(2w) ', START)
    assert regular.kind == 'WEEKLY' and regular.interval == 2 and regular.ending == 'never'
    assert draft.validation_starts(START) == [START]
    fields = draft.fields(START)
    assert fields['recurrence_rule'] == 'FREQ=WEEKLY;INTERVAL=2'
    assert fields['schedule_expression'] == ' every(2w) '
    assert fields['schedule_syntax'] == 2


def test_switching_an_indefinite_regular_rule_to_custom_preserves_its_exceptions():
    source = authored('every(1w)')
    exception = occurrence(source, START + timedelta(weeks=2))
    exception = replace(exception, start=exception.start + timedelta(hours=2), end=exception.end + timedelta(hours=2))
    source = replace(source, schedule_expression='', schedule_syntax=1,
                     recurrence_dates=(recurrence_key(START + timedelta(days=1)),),
                     excluded_dates=(recurrence_key(START + timedelta(weeks=1)),),
                     recurrence_overrides={exception.recurrence_id: exception.to_dict()})
    draft = ScheduleDraft.from_event(source)
    with patch('debacal.event_schedule.base_schedule_starts', side_effect=AssertionError('finite expansion')):
        draft.use_sequence(source)
        fields = draft.fields(START)
        assert len(draft.validation_starts(START)) > 1
    assert fields['schedule_expression'] != 'every(1w)'
    assert expression_matches(source, fields['schedule_expression'])
    assert fields['recurrence_dates'] == source.recurrence_dates
    assert fields['excluded_dates'] == source.excluded_dates
    assert fields['recurrence_overrides'] == source.recurrence_overrides
    assert_roundtrip(replace(source, **fields))


@pytest.mark.parametrize('zone', [timezone.utc, ZoneInfo('Pacific/Auckland')])
def test_all_day_regular_to_custom_keeps_floating_dates_and_survives_move(tmp_path, zone):
    initial = datetime(2026, 11, 4, tzinfo=zone)
    source = authored('every(1w)', initial, all_day=True, end=initial + timedelta(days=1),
                      schedule_expression='', schedule_syntax=1, timezone_id=getattr(zone, 'key', ''))
    draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    draft.use_sequence(source)
    fields = draft.fields(initial, initial + timedelta(days=1), all_day=True)
    assert fields['schedule_expression'] == 'every(1w)'
    assert fields['expression_anchor'] == '2026-11-04'
    primary = datetime(2026, 11, 4, tzinfo=ZONE)
    assert draft.validation_starts(primary, all_day=True)[0].date() == primary.date()
    authored_source = assert_roundtrip(replace(source, **fields))
    with Store(tmp_path / 'calendar.sqlite') as store:
        store.apply('Create series', [authored_source])
        selected = visible(authored_source)[1]
        store.edit_series(selected, moved_selection(authored_source, selected, timedelta(days=1)))
        moved = store.get_event(source.id)
        assert moved.start.date() == initial.date() + timedelta(days=1)
        assert moved.expression_anchor == '2026-11-05'
        assert_roundtrip(moved)


def test_imported_multiday_rule_has_compact_weekday_text():
    source = authored(recurrence_rule='FREQ=WEEKLY;BYDAY=MO,WE', schedule_expression='')
    draft = ScheduleDraft.from_event(source)
    before = draft.fields(START)
    draft.use_sequence(source)
    fields = draft.fields(START)
    assert expression_matches(source, draft.expression)
    assert 'on=MO+WE' in draft.expression
    assert rules_equivalent(fields['recurrence_rule'], before['recurrence_rule'], source)
    assert fields['recurrence_dates'] == before['recurrence_dates']
    assert fields['excluded_dates'] == before['excluded_dates']


def test_reopening_renaming_and_syncing_an_authored_tail_retains_literal():
    source = assert_roundtrip(authored('  +0d +1d, +3d every(2d)  '))
    draft = ScheduleDraft.from_event(source)
    assert draft.expression == source.schedule_expression
    renamed = replace(source, title='Renamed', **draft.fields(source.start, source.end))
    assert_roundtrip(updated_series(source, source, renamed))


@pytest.mark.parametrize('text', [' every(1w) ', '+0d 10:00-7.11.2026 every(1w)', '+0d 7.11.2026 every(1m)'])
def test_actual_store_moves_keep_indefinite_authoring_and_standard_sync(tmp_path, text):
    source = authored(text)
    delta = timedelta(days=1, hours=1)
    with Store(tmp_path / 'calendar.sqlite') as store:
        store.apply('Create series', [source])
        selected = visible(source)[1]
        store.edit_series(selected, moved_selection(source, selected, delta))
        moved = store.get_event(source.id)
        assert stamps(item.start for item in visible(moved)) == stamps(item.start + delta for item in visible(source))
        assert sequence_tail(moved.schedule_expression)
        assert_roundtrip(moved)
        reopened = ScheduleDraft.from_event(moved)
        assert reopened.expression == moved.schedule_expression
        assert reopened.validation_starts(moved.start)[0] == moved.start
        store.undo()
        assert store.get_event(source.id).schedule_expression == text


def test_primary_edit_of_absolute_prefix_is_not_applied_twice():
    source = authored('+0d 10:00-7.11.2026 every(1w)')
    draft = ScheduleDraft.from_event(source)
    start = source.start + timedelta(days=1, hours=1)
    end = source.end + timedelta(days=1, hours=1)
    fields = draft.fields(start, end)
    edited = replace(source, start=start, end=end, **fields)
    moved = updated_series(source, source, edited)
    assert moved.schedule_expression == '+0d 11:00-8. every(1w)'
    assert '12:00' not in moved.schedule_expression
    assert draft.validation_starts(start) == [start, START.replace(day=8, hour=11)]
    assert_roundtrip(moved)


def test_duplicate_final_prefix_instruction_sets_the_tail_cursor_exactly():
    source = authored('+0d +1d +2d 5.11.2026 every(1d)')
    assert compile_sequence(source.schedule_expression, START).tail_start == START + timedelta(days=1)
    text, _ = shift_unbounded_expression(source.schedule_expression, START, START + timedelta(days=2))
    compiled = compile_sequence(text, START + timedelta(days=2))
    assert compiled.tail_start == START + timedelta(days=3)
    assert stamps(compiled.prefix_starts) == stamps(value + timedelta(days=2) for value in
                                                   compile_sequence(source.schedule_expression, START).prefix_starts)


@pytest.mark.parametrize('text', [' every(2w) ', '+0d 10:00-7.11.2026 every(1w)', '+0d 7.11.2026 every(1m)'])
def test_future_split_and_move_keep_infinite_definition_and_old_events(tmp_path, text):
    source = authored(text)
    selected = visible(source)[2]
    head, tail = partition_series(source, selected)
    assert head.schedule_expression and expression_matches(head, head.schedule_expression)
    head_tail = sequence_tail(head.schedule_expression)
    assert head_tail is None or not head_tail.unbounded
    assert sequence_tail(tail.schedule_expression)
    assert rules_equivalent(tail.recurrence_rule, compile_sequence(tail.schedule_expression, tail.start).recurrence_rule, tail)
    assert_roundtrip(tail)
    with Store(tmp_path / 'calendar.sqlite') as store:
        store.apply('Create series', [source])
        delta = timedelta(days=2)
        store.edit_future(selected, moved_selection(source, selected, delta))
        parts = store.list_events()
        assert len(parts) == 2
        past = store.get_event(source.id)
        future = next(item for item in parts if item.id != source.id)
        assert stamps(item.start for item in visible(past)) == stamps(item.start for item in visible(source)[:2])
        assert sequence_tail(future.schedule_expression)
        assert_roundtrip(future)
        assert future.start == selected.start + delta


def test_graphical_exceptions_to_an_infinite_tail_survive_source_reopen_and_sync():
    source = authored('every(1w)')
    draft = ScheduleDraft.from_event(source)
    changed = draft.toggle_date(source, (START + timedelta(weeks=1)).date())
    changed = draft.toggle_date(changed, (START + timedelta(days=2)).date())
    assert draft.expression != source.schedule_expression
    assert expression_matches(changed, draft.expression)
    assert recurrence_key(START + timedelta(weeks=1)) in changed.excluded_dates
    assert recurrence_key(START + timedelta(days=2)) in changed.recurrence_dates
    loaded = assert_roundtrip(changed)
    reopened = ScheduleDraft.from_event(loaded)
    fields = reopened.fields(loaded.start, loaded.end)
    assert fields['schedule_expression'] == changed.schedule_expression
    assert expression_matches(loaded, fields['schedule_expression'])
    assert fields['excluded_dates'] == changed.excluded_dates
    assert fields['recurrence_dates'] == changed.recurrence_dates


@pytest.mark.parametrize('changes', [
    {'recurrence_rule': 'FREQ=DAILY;INTERVAL=1'},
    {'recurrence_dates': ()},
    {'excluded_dates': ()},
])
def test_stale_remote_tail_metadata_never_overwrites_canonical_rules_or_exceptions(changes):
    source = authored('+0d +1d +3d every(2d)', **changes)
    draft = ScheduleDraft.from_event(source)
    assert draft.expression and expression_matches(source, draft.expression)
    # Merely opening/editing a title must retain imported fields exactly.
    fields = draft.fields(source.start, source.end)
    assert fields['recurrence_rule'] == source.recurrence_rule
    assert fields['recurrence_dates'] == source.recurrence_dates
    assert fields['excluded_dates'] == source.excluded_dates
    delta = timedelta(days=2)
    selected = visible(source)[1]
    moved = updated_series(source, selected, moved_selection(source, selected, delta))
    assert stamps(item.start for item in visible(moved)) == stamps(item.start + delta for item in visible(source))
    assert moved.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


@pytest.mark.parametrize('text', ['every(broken)', 'every(1d) +2', '+1d every(2d)',
                               '02:30-28.3.2027 every(1d)'])
def test_malformed_or_unrepresentable_remote_tail_annotations_do_not_block_standard_moves(text):
    source = authored('every(1w)', schedule_expression=text)
    draft = ScheduleDraft.from_event(source)
    assert draft.expression and expression_matches(source, draft.expression)
    selected = visible(source)[1]
    delta = timedelta(days=1)
    renamed = updated_series(source, selected, moved_selection(source, selected, timedelta(0)))
    assert renamed.recurrence_rule == source.recurrence_rule
    moved = updated_series(source, selected, moved_selection(source, selected, delta))
    assert stamps(item.start for item in visible(moved)) == stamps(item.start + delta for item in visible(source))
    assert moved.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)
