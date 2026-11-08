"""Bounded every syntax has one inclusive meaning in text and calendar data."""
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import (PatternError, compile_sequence, expand_sequence,
                              expression_for_regular, is_relative_sequence, sequence_tail, serialize_sequence,
                              shift_schedule_expression, shift_unbounded_expression)
from debacal.recurrence import expand_event


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def resource(text, anchor=START, *, all_day=False):
    compiled = compile_sequence(text, anchor, all_day=all_day)
    return Event('series', 'local', 'Meeting', anchor,
                 anchor + timedelta(days=1) if all_day else anchor + timedelta(hours=1),
                 all_day=all_day, timezone_id=ZONE.key,
                 schedule_expression=text, schedule_syntax=2, **compiled.fields)


def visible(master):
    return [event.start for event in expand_event(master, master.start - timedelta(days=1),
                                                 master.start + timedelta(days=400))]


@pytest.mark.parametrize('bound', ['count=3', 'COUNT = 0003', 'until=6.11.2026', 'until=06.11.2026.'])
def test_count_and_until_include_the_prototype_and_final_day(bound):
    text = f'every(1d, {bound})'
    tail = sequence_tail(text)
    assert not tail.unbounded
    expected = [START + timedelta(days=index) for index in range(3)]
    assert expand_sequence(text, START) == expected
    master = resource(text)
    assert visible(master) == expected
    loaded, = parse_ics(export_ics([master]), 'local')
    assert visible(loaded) == expected
    assert loaded.schedule_expression == text


@pytest.mark.parametrize('text', ['+1d +3d every(2d, count=3)',
                                  '+1d +3d every(2d, until=12.11.2026)'])
def test_bounds_apply_to_tail_and_leave_the_finite_prefix_intact(text):
    expected = [START + timedelta(days=index) for index in (0, 1, 4, 6, 8)]
    assert expand_sequence(text, START) == expected
    assert visible(resource(text)) == expected


def test_a_different_weekday_tail_counts_from_its_cursor_not_the_prototype():
    text = '7. every(1w, count=3)'
    expected = [START, START + timedelta(days=3), START + timedelta(days=10), START + timedelta(days=17)]
    assert expand_sequence(text, START) == expected
    assert visible(resource(text)) == expected


@pytest.mark.parametrize('text', ['every(1m, count=3)', 'every(1m, until=31.5.2027)'])
def test_bounded_calendar_months_skip_missing_dates_rather_than_clip(text):
    january = START.replace(year=2027, month=1, day=31)
    expected = [january, january.replace(month=3), january.replace(month=5)]
    assert expand_sequence(text, january) == expected
    assert visible(resource(text, january)) == expected
    assert expand_sequence('2*+1m', january)[1].day == 28


def test_all_day_bound_is_a_date_and_timed_bound_is_utc():
    anchor = START.replace(hour=0)
    master = resource('every(1d, until=6.11.2026)', anchor, all_day=True)
    assert master.recurrence_rule.endswith('UNTIL=20261106')
    assert [value.date() for value in visible(master)] == [date(2026, 11, index) for index in (4, 5, 6)]
    loaded, = parse_ics(export_ics([master]), 'local')
    assert [value.date() for value in visible(loaded)] == [date(2026, 11, index) for index in (4, 5, 6)]
    timed = resource('every(1d, until=6.11.2026)')
    assert timed.recurrence_rule.endswith('UNTIL=20261106T225959Z')


def test_standard_every_count_keeps_its_count_through_a_clock_gap():
    anchor = START.replace(year=2027, month=3, day=27, hour=2, minute=30)
    expected = [anchor, anchor.replace(day=28, hour=3), anchor.replace(day=29)]
    text = 'every(1d, count=3)'
    assert expand_sequence(text, anchor) == expected
    assert visible(resource(text, anchor)) == expected
    # Explicit relative-date instructions still cannot request a nonexistent
    # local clock. Standard recurrence interpretation is a separate operation.
    with pytest.raises(PatternError, match='clocks change'):
        expand_sequence('+1d', anchor)
    text = '+2d every(1d, count=2)'
    expected = [anchor, anchor.replace(day=29), anchor.replace(day=30)]
    assert expand_sequence(text, anchor) == expected
    assert visible(resource(text, anchor)) == expected


def test_large_finite_compile_visits_only_the_prefix_and_expansion_respects_its_budget():
    from dateutil.rrule import rrulestr
    iterations = []
    def limited(rule, *, dtstart):
        values = iter(rrulestr(rule, dtstart=dtstart))
        for _ in range(3):
            iterations.append(1)
            yield next(values)
        raise AssertionError('Compilation must not enumerate the finite tail')
    with patch('dateutil.rrule.rrulestr', side_effect=limited):
        result = compile_sequence('every(1d, count=100000)', START)
    assert result.recurrence_rule.endswith('COUNT=100000') and len(iterations) == 1
    with pytest.raises(PatternError, match='limit of 5'):
        expand_sequence('every(1d, count=100000)', START, limit=5)


@pytest.mark.parametrize('unit', ['d', 'w', 'm', 'y'])
def test_bounded_count_cannot_silently_run_out_of_calendar_years(unit):
    with pytest.raises(PatternError, match='supported years'):
        compile_sequence(f'every(1{unit}, count=3)', START.replace(year=9999, month=12, day=31))


@pytest.mark.parametrize('text', [
    'every(1d, count=0)', 'every(1d, count=100001)', 'every(1d, count=1.5)',
    'every(1d, until=13.)', 'every(1d, until=31.2.2027)',
    'every(1d, count=2, until=6.11.2026)', 'every(1d, until=2026-11-06)',
    'every(1d, until=3.11.2026)', '+2 every(1w, until=10.11.2026)',
])
def test_invalid_bounds_fail_instead_of_becoming_an_endless_rule(text):
    with pytest.raises(PatternError):
        compile_sequence(text, START)


def test_regular_formatter_emits_one_bound_and_no_terminal_year_dot():
    assert expression_for_regular('DAILY', count=3) == 'every(1d, count=3)'
    assert expression_for_regular('MONTHLY', 2, until=date(2027, 1, 3)) == 'every(2m, until=3.1.2027)'
    with pytest.raises(PatternError):
        expression_for_regular('DAILY', count=3, until=date(2026, 11, 6))
    assert serialize_sequence([START, START.replace(year=2025)]) == '4.11.2025'
    assert expand_sequence('4.11.2025.', START)[1] == START.replace(year=2025)


@pytest.mark.parametrize('text', ['every(1d, count=3)', 'every(1d, until=6.11.2026)',
                                  '+1d +3d every(2d, count=3)'])
def test_moving_a_bounded_tail_retains_count_or_translates_the_cutoff(text):
    delta = timedelta(days=2, hours=1)
    moved, anchor = shift_unbounded_expression(text, START, START + delta)
    assert expand_sequence(moved, START + delta) == [value + delta for value in expand_sequence(text, START)]
    assert anchor == (START + delta).date().isoformat()
    if 'count=' in text:
        assert sequence_tail(moved).count == sequence_tail(text).count
    else:
        assert sequence_tail(moved).until == date(2026, 11, 8)


def test_clock_and_partial_date_literals_survive_a_move_when_still_equivalent():
    text = ' 10:00, 11:00  '
    delta = timedelta(days=3)
    values = [value + delta for value in expand_sequence(text, START)]
    assert shift_schedule_expression(text, START.date().isoformat(), delta, values)[0] == text
    text = '4*1.'
    delta = timedelta(days=2)
    values = [value + delta for value in expand_sequence(text, START)]
    moved, _ = shift_schedule_expression(text, START.date().isoformat(), delta, values)
    assert moved == '4*3.' and expand_sequence(moved, values[0]) == values


@pytest.mark.parametrize('text', ['4*+1m', '2*(+1y +2d)', ' +2, +3w ', '+1h +5min'])
def test_relative_program_classification_looks_through_multipliers_and_groups(text):
    assert is_relative_sequence(text)


@pytest.mark.parametrize('text', ['', ' ', '2*1.', '+2 10:00', '+1w every(1w)',
                                  'every(1d, count=3)', '(+1', '+0', None])
def test_other_and_invalid_programs_do_not_opt_in_to_relative_reanchoring(text):
    assert not is_relative_sequence(text)
