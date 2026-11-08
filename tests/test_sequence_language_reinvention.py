"""The current occurrence language has no implicit or obsolete spellings."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
import pytest

from debacal.patterns import (
    PatternError, calendar_clamp_options, clamp_relevant, compile_sequence, expand_sequence, expression_for_regular,
    reconstruct_sequence, relative_sequence_steps, sequence_tail,
    serialize_sequence, split_relative_sequence, with_calendar_clamp,
)
from debacal.regular_schedule import RegularSchedule, count_to_until, retain_relative_intent, until_to_count


def at(year=2026, month=10, day=31, hour=9):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def days(text, anchor):
    return [value.date() for value in expand_sequence(text, anchor)]


def test_reference_is_not_implicitly_an_occurrence():
    anchor = at()
    assert expand_sequence('none', anchor) == []
    assert expand_sequence('+0d', anchor) == [anchor]
    assert expand_sequence('+1d', anchor) == [anchor + timedelta(days=1)]
    assert reconstruct_sequence([], anchor=anchor) == 'none'
    assert reconstruct_sequence([anchor], anchor=anchor) == '+0d'
    compiled = compile_sequence('+1d', anchor)
    assert compiled.recurrence_rule == ''
    assert compiled.recurrence_dates == ((anchor + timedelta(days=1)).isoformat(),)
    assert compiled.excluded_dates == (anchor.isoformat(),)
    assert not compiled.unbounded


@pytest.mark.parametrize('text', [
    '', ' ', '+1', '+0', '+1h', '+1min', 'every(1)', 'every(1h)',
    'every(1min)', '3.2', '3.2.2027.', 'every(1d, until=3.2.2027.)',
    '+1d!', 'every(1w!)',
])
def test_obsolete_and_ambiguous_spellings_are_rejected(text):
    with pytest.raises(PatternError):
        compile_sequence(text, at())


def test_partial_month_has_a_dot_but_complete_year_does_not():
    assert days('3.2.', at()) == [date(2027, 2, 3)]
    assert days('3.2.2027', at()) == [date(2027, 2, 3)]
    text = reconstruct_sequence([at(2027, 2, 3)], anchor=at())
    assert text == '3.' or text == '3.2.'
    assert not serialize_sequence([at(2027, 2, 3)], anchor=at()).endswith('2027.')


@pytest.mark.parametrize('anchor,unit,interval', [
    (at(), 'm', 1), (at(), 'm', 2), (at(2024, 2, 29), 'y', 1),
    (at(2024, 2, 29), 'y', 2), (at(2026, 1, 30), 'm', 1),
])
def test_relative_calendar_steps_match_standard_recurrence_skipping(anchor, unit, interval):
    frequency = 'MONTHLY' if unit == 'm' else 'YEARLY'
    expected = list(rrulestr(f'FREQ={frequency};INTERVAL={interval};COUNT=13', dtstart=anchor))
    assert expand_sequence(f'+0d 12*+{interval}{unit}', anchor) == expected
    assert expand_sequence(f'every({interval}{unit}, count=13)', anchor) == expected


def test_clamping_retains_the_preferred_day_through_short_months():
    expected = [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31),
                date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)]
    assert days('+0d 5*+1m!', at()) == expected
    assert days('every(1m!, count=6)', at()) == expected
    assert days('+1m! every(1m!, count=5)', at()) == expected[1:]
    assert 'BYSETPOS=-1' in compile_sequence('every(1m!)', at()).recurrence_rule


def test_standard_tail_starts_its_own_calendar_phase_after_a_clamped_step():
    assert days('+1m! every(1m, count=3)', at()) == [
        date(2026, 11, 30), date(2026, 12, 30), date(2027, 1, 30)]


def test_day_week_and_absolute_instructions_reset_clamp_preference():
    anchor = at(2027, 1, 31)
    assert days('+1m! +1d +1m!', anchor) == [date(2027, 2, 28), date(2027, 3, 1), date(2027, 4, 1)]
    assert days('+1m! +0d +1m!', anchor) == [date(2027, 2, 28), date(2027, 2, 28), date(2027, 3, 28)]
    assert days('+1m! 28.2.2027 +1m!', anchor) == [date(2027, 2, 28), date(2027, 2, 28), date(2027, 3, 28)]


def test_year_clamp_returns_to_leap_day():
    anchor = at(2024, 2, 29)
    expected = [date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28),
                date(2027, 2, 28), date(2028, 2, 29)]
    assert days('+0d 4*+1y!', anchor) == expected
    assert days('every(1y!, count=5)', anchor) == expected


@pytest.mark.parametrize('clamp', [False, True])
def test_regular_expression_round_trip_preserves_clamp_and_bounds(clamp):
    for bound in ({'count': 17}, {'until': date(2029, 3, 4)}, {}):
        text = expression_for_regular('MONTHLY', 2, clamp=clamp, **bound)
        tail = sequence_tail(text)
        assert tail.clamp is clamp
        assert tail.unit == 'm'
        assert tail.interval == 2
        assert compile_sequence(text, at()).unbounded is (not bound)


@pytest.mark.parametrize('text', [
    '+0d 12*+1w', '+0d 12*+1m!', '+0d 12*+1m',
    '+2w 3*+1w', '3.2.2027 10:30-6.2.2027 2.3.',
    '+0d 10:00 11:00 9:00 10:00 11:00',
])
def test_normalization_reproduces_all_starts_with_an_independent_reference(text):
    anchor = at()
    expected = expand_sequence(text, anchor)
    for normalizer in (reconstruct_sequence, serialize_sequence):
        normalized = normalizer(expected, anchor=anchor)
        assert expand_sequence(normalized, anchor) == expected
        assert not any(part.endswith('2027.') for part in normalized.split())
    assert expand_sequence(reconstruct_sequence(expected), expected[0]) == expected


def test_clamped_run_normalizes_to_readable_relative_instructions():
    anchor = at()
    text = '+0d 12*+1m!'
    assert reconstruct_sequence(expand_sequence(text, anchor), anchor=anchor) == text
    assert relative_sequence_steps(text) == ((0, 'd', False), *((1, 'm', True),) * 12)


def test_finite_calendar_programs_compile_to_interoperable_bounded_rules():
    anchor = at()
    for text in ('+0d 5*+1m', '+0d 5*+1m!', '+0d 5*+1w'):
        compiled = compile_sequence(text, anchor)
        assert 'COUNT=6' in compiled.recurrence_rule
        assert not compiled.recurrence_dates and not compiled.excluded_dates
        assert list(rrulestr(compiled.recurrence_rule, dtstart=anchor)) == expand_sequence(text, anchor)
    first = at(2026, 11, 1)
    assert 'FREQ=MONTHLY' in compile_sequence('+0d 4*1.', first).recurrence_rule
    assert 'FREQ=HOURLY' in compile_sequence('+0d 10:00 11:00 12:00', first).recurrence_rule


def test_optional_rule_inference_keeps_precise_finite_programs_lossless():
    anchor = at().replace(second=17, microsecond=123456)
    compiled = compile_sequence('+0d 2*+1d', anchor)
    assert compiled.recurrence_rule == ''
    assert compiled.recurrence_dates == tuple(
        (anchor + timedelta(days=offset)).isoformat() for offset in (1, 2))
    assert not compiled.excluded_dates


def test_splitting_emits_selected_reference_explicitly():
    anchor = at()
    values = expand_sequence('+0d 5*+1w', anchor)
    before, after = split_relative_sequence('+0d 5*+1w', anchor, values[3])
    assert expand_sequence(before, anchor) == values[:3]
    assert expand_sequence(after, values[3]) == values[3:]
    assert after.startswith('+0d')


def test_splitting_clamped_short_month_declines_unfaithful_reanchoring():
    anchor = at()
    values = expand_sequence('+0d 5*+1m!', anchor)
    assert split_relative_sequence('+0d 5*+1m!', anchor, values[1]) is None


def test_splitting_first_actual_occurrence_rebases_an_excluded_reference():
    anchor = at()
    values = expand_sequence('5*+1m', anchor)
    before, after = split_relative_sequence('5*+1m', anchor, values[0])
    assert before is None
    assert expand_sequence(after, values[0]) == values


def test_all_day_empty_and_excluded_reference_compile_as_dates():
    anchor = at(hour=0)
    assert compile_sequence('none', anchor, all_day=True).excluded_dates == ('2026-10-31',)
    fields = compile_sequence('+1d every(1d, count=3)', anchor, all_day=True).fields
    assert fields['excluded_dates'] == ('2026-10-31',)


def test_wall_clock_preserved_through_daylight_saving_change():
    anchor = datetime(2026, 10, 24, 9, tzinfo=ZoneInfo('Europe/Berlin'))
    assert [value.hour for value in expand_sequence('+0d 3*+1w', anchor)] == [9] * 4


def test_group_budget_counts_emitted_instructions_without_implicit_reference():
    assert len(expand_sequence('4*+1d', at(), limit=4)) == 4
    with pytest.raises(PatternError):
        expand_sequence('5*+1d', at(), limit=4)


def test_clamp_checkbox_rewrites_only_unit_suffixes_and_preserves_formatting():
    text = ' +0d, 2*(+1m +3d) every(2y, until=31.12.2030)  '
    changed = with_calendar_clamp(text, True)
    assert changed == ' +0d, 2*(+1m! +3d) every(2y!, until=31.12.2030)  '
    assert with_calendar_clamp(changed, False) == text
    assert calendar_clamp_options('+0d 12*+1m', at()) == (True, False)
    assert calendar_clamp_options('+0d 12*+1m!', at()) == (True, True)
    assert calendar_clamp_options('+0d 12*+1w', at()) == (False, False)
    for invalid in ('+1h', '+2', 'schedule{}', ''):
        assert calendar_clamp_options(invalid, at()) == (False, False)


def test_clamp_option_appears_only_for_calendar_intervals_that_need_it():
    assert clamp_relevant(at(), 'MONTHLY', 1)
    assert not clamp_relevant(at(), 'MONTHLY', 12)
    assert not clamp_relevant(at(day=28), 'MONTHLY', 1)
    assert clamp_relevant(at(2024, 2, 29), 'YEARLY', 1)
    assert not clamp_relevant(at(2024, 2, 29), 'YEARLY', 400)


@pytest.mark.parametrize('kind,anchor', [('MONTHLY', at()), ('YEARLY', at(2024, 2, 29))])
@pytest.mark.parametrize('clamp', [False, True])
def test_regular_controls_round_trip_clamped_calendar_rules(kind, anchor, clamp):
    regular = RegularSchedule(kind, 1, 7, clamp=clamp)
    restored = RegularSchedule.from_rule(regular.rule(anchor), anchor)
    assert restored is not None
    assert restored.kind == kind and restored.interval == 1 and restored.count == 7
    assert restored.clamp is clamp
    until = count_to_until(anchor, kind, 1, 7, clamp=clamp)
    assert until_to_count(anchor, kind, 1, until, clamp=clamp) == 7
    text = '+0d 6*+1' + ('m' if kind == 'MONTHLY' else 'y') + ('!' if clamp else '')
    assert retain_relative_intent(text, anchor, restored) is restored


def test_nonmatching_calendar_rule_cannot_masquerade_as_a_clamp_control():
    assert RegularSchedule.from_rule('FREQ=MONTHLY;BYMONTHDAY=28,29;BYSETPOS=-1;COUNT=7', at()) is None
