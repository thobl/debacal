"""Selected weekdays, ordinal month days and clipped phases stay standard rules."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
import pytest

from debacal.patterns import (
    PatternError, calendar_clamp_options, compile_sequence, expand_sequence,
    expression_for_regular, sequence_tail, shift_unbounded_expression, with_calendar_clamp,
)
from debacal.regular_schedule import RegularSchedule, count_to_until, until_to_count


UTC = timezone.utc
MONDAY = datetime(2026, 11, 2, 9, tzinfo=UTC)


def rule_starts(compiled, anchor):
    exclusions = set(compiled.excluded_dates)
    return [value for value in rrulestr(compiled.recurrence_rule, dtstart=anchor)
            if value.astimezone(UTC).isoformat() not in exclusions]


def test_weekday_tail_has_one_readable_canonical_spelling():
    text = expression_for_regular('WEEKLY', 2, weekdays=(2, 0), count=7, week_start='SU')
    assert text == 'every(2w, on=MO+WE, week_start=SU, count=7)'
    tail = sequence_tail(text)
    assert tail.weekdays == ('MO', 'WE') and tail.week_start == 'SU'
    assert tail.count == 7
    assert sequence_tail('every(1w, on=we+mo)').weekdays == ('MO', 'WE')


@pytest.mark.parametrize('interval', [1, 2, 3])
@pytest.mark.parametrize('week_start', ['MO', 'SU', 'WE'])
def test_selected_weekdays_compile_to_the_exact_standard_rule(interval, week_start):
    text = expression_for_regular('WEEKLY', interval, weekdays=('MO', 'WE'),
                                  week_start=week_start, count=9)
    compiled = compile_sequence(text, MONDAY)
    actual = expand_sequence(text, MONDAY)
    expected = list(rrulestr(f'FREQ=WEEKLY;INTERVAL={interval};BYDAY=MO,WE;WKST={week_start};COUNT=9', dtstart=MONDAY))
    assert actual == expected
    assert compiled.tail_start == MONDAY
    assert compiled.recurrence_dates == compiled.excluded_dates == ()


def test_off_weekday_reference_is_only_included_when_explicit():
    anchor = MONDAY + timedelta(days=1)
    text = 'every(1w, on=MO+WE, count=3)'
    values = expand_sequence(text, anchor)
    assert [value.day for value in values] == [4, 9, 11]
    compiled = compile_sequence(text, anchor)
    assert compiled.tail_start.day == 4
    assert anchor.isoformat() in compiled.excluded_dates
    assert expand_sequence('+0d ' + text, anchor) == [anchor, *values]


@pytest.mark.parametrize('text', [
    'every(1w, on=MO+XX)', 'every(1w, on=-1FR)', 'every(1d, on=MO)',
    'every(1m!, on=-1FR)', 'every(1m, day=31, on=MO)',
    'every(1w, week_start=XX)', 'every(1m, week_start=SU)',
    'every(1w, on=MO, on=WE)', 'every(1w, count=2, until=3.11.2026)',
    'every(1m, on=0FR)', 'every(1m, on=6FR)', 'every(1m, day=0)',
])
def test_invalid_selectors_are_rejected_before_adoption(text):
    with pytest.raises(PatternError):
        compile_sequence(text, MONDAY)


@pytest.mark.parametrize('selectors', [('-1FR',), ('1MO', '-1FR'), ('MO', 'WE'), ('5MO',)])
@pytest.mark.parametrize('unit,frequency', [('m', 'MONTHLY'), ('y', 'YEARLY')])
def test_monthly_and_yearly_weekday_selection_uses_standard_byday(selectors, unit, frequency):
    text = expression_for_regular(frequency, 2, weekdays=selectors, count=17)
    expected_rule = f'FREQ={frequency};INTERVAL=2;BYDAY=' + ','.join(sequence_tail(text).weekdays)
    if unit == 'y':
        expected_rule += ';BYMONTH=11'
    expected = list(rrulestr(expected_rule + ';COUNT=17', dtstart=MONDAY))
    assert expand_sequence(text, MONDAY) == expected
    assert compile_sequence(text, MONDAY).tail_start == expected[0]


def test_selected_weekday_prefix_retains_additions_and_count_is_tail_only():
    text = '+0d +1d every(1w, on=MO+WE, count=4)'
    assert [value.day for value in expand_sequence(text, MONDAY)] == [2, 3, 4, 9, 11, 16]
    compiled = compile_sequence(text, MONDAY)
    assert compiled.tail_start.day == 4
    assert compiled.recurrence_dates == ((MONDAY + timedelta(days=1)).isoformat(),)


def test_until_and_all_day_exclusions_use_calendar_dates():
    anchor = (MONDAY + timedelta(days=1)).replace(hour=0)
    compiled = compile_sequence('every(1w, on=MO+WE, until=11.11.2026)', anchor, all_day=True)
    assert compiled.recurrence_rule.endswith('UNTIL=20261111')
    assert compiled.excluded_dates == ('2026-11-03',)


def test_moving_a_two_week_pattern_rotates_week_origin_as_well_as_weekdays():
    text = 'every(2w, on=MO+WE, count=4)'
    new_anchor = MONDAY - timedelta(days=1)
    moved, _ = shift_unbounded_expression(text, MONDAY, new_anchor)
    tail = sequence_tail(moved)
    assert set(tail.weekdays) == {'SU', 'TU'}
    assert tail.week_start == 'SU'
    assert [value.day for value in expand_sequence(moved, new_anchor)] == [1, 3, 15, 17]


def test_moving_an_ordinal_across_a_month_boundary_is_guarded():
    anchor = datetime(2027, 4, 30, 9, tzinfo=UTC)  # Last Friday; tomorrow is May's first Saturday.
    with pytest.raises(PatternError, match='weekday pattern'):
        shift_unbounded_expression('every(1m, on=-1FR)', anchor, anchor + timedelta(days=1))


def test_weekday_times_keep_local_clock_across_dst():
    zone = ZoneInfo('Europe/Berlin')
    anchor = datetime(2027, 3, 22, 9, tzinfo=zone)
    values = expand_sequence('every(1w, on=MO+WE, count=5)', anchor)
    assert [value.hour for value in values] == [9] * 5
    assert values[0].utcoffset() != values[-1].utcoffset()


def test_clamped_tail_can_keep_day_31_after_its_reference_is_clipped():
    anchor = datetime(2026, 11, 30, 9, tzinfo=UTC)
    text = 'every(1m!, day=31, count=5)'
    assert [(value.month, value.day) for value in expand_sequence(text, anchor)] == [
        (11, 30), (12, 31), (1, 31), (2, 28), (3, 31)]
    assert calendar_clamp_options(text, anchor) == (True, True)
    compiled = compile_sequence(text, anchor)
    regular = RegularSchedule.from_rule(compiled.recurrence_rule, anchor)
    assert regular.preferred_day == 31 and regular.clamp
    assert regular.rule(anchor) == compiled.recurrence_rule
    assert regular.until == count_to_until(anchor, 'MONTHLY', 1, 5, clamp=True, preferred_day=31)
    assert until_to_count(anchor, 'MONTHLY', 1, regular.until, clamp=True, preferred_day=31) == 5


def test_clamping_a_calendar_prefix_leaves_ordinal_weekday_tail_unchanged():
    text = '+1m every(1m, on=-1FR)'
    changed = with_calendar_clamp(text, True)
    assert changed == '+1m! every(1m, on=-1FR)'
    compile_sequence(changed, datetime(2026, 10, 31, 9, tzinfo=UTC))


def test_nondefault_week_origin_is_editable_without_losing_the_standard_clause():
    anchor = MONDAY - timedelta(days=1)
    rule = 'FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,SU;WKST=SU;COUNT=4'
    regular = RegularSchedule.from_rule(rule, anchor)
    assert regular.week_start == 'SU' and regular.weekdays == (1, 6)
    assert regular.until.day == 17
    assert list(rrulestr(regular.rule(anchor), dtstart=anchor)) == list(rrulestr(rule, dtstart=anchor))


def test_ordinal_standard_rule_retains_its_simple_model_when_reference_matches():
    anchor = datetime(2026, 11, 27, 9, tzinfo=UTC)
    regular = RegularSchedule.from_rule('FREQ=MONTHLY;INTERVAL=1;BYDAY=-1FR;COUNT=3', anchor)
    assert regular.selectors == ('-1FR',)
    assert regular.until == datetime(2027, 1, 29).date()
