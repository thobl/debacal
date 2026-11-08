"""A split keeps relative instruction units, not coincident concrete day gaps."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.patterns import (PatternError, expand_sequence, relative_sequence_steps,
                              split_relative_sequence)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2027, 1, 31, 9, tzinfo=ZONE)


@pytest.mark.parametrize('text', ['4*+1m', '3*(+1m +2d)', '2*(+1y +2)',
                                  '4*+1d', '2*(+1h +30min)'])
def test_every_relative_instruction_boundary_partitions_the_complete_sequence(text):
    values = expand_sequence(text, START)
    for index, boundary in enumerate(values[1:], 1):
        before, after = split_relative_sequence(text, START, boundary.astimezone(timezone.utc))
        assert expand_sequence(before, START) == values[:index]
        assert expand_sequence(after, boundary) == values[index:]


def test_monthly_split_and_subsequent_reanchor_keep_calendar_month_intent():
    before, after = split_relative_sequence('4*+1m', START, START.replace(month=3, day=28))
    assert (before, after) == ('+1m', '2*+1m')
    reanchored = START.replace(month=3, day=30)
    assert expand_sequence(after, reanchored) == [reanchored,
        reanchored.replace(month=4), reanchored.replace(month=5)]


def test_yearly_split_preserves_leap_day_arithmetic_when_reanchored():
    initial = START.replace(year=2028, month=2, day=29)
    before, after = split_relative_sequence('4*+1y', initial, initial.replace(year=2030, day=28))
    assert (before, after) == ('+1y', '2*+1y')
    reanchored = initial.replace(year=2032)
    assert expand_sequence(after, reanchored) == [reanchored,
        reanchored.replace(year=2033, day=28), reanchored.replace(year=2034, day=28)]


def test_split_at_a_group_boundary_keeps_the_relative_motif():
    values = expand_sequence('3*(+1m +2d)', START)
    before, after = split_relative_sequence('3*(+1m +2d)', START, values[2])
    assert before == '+1m' and after == '2*(+1m +2d)'


def test_split_before_prototype_has_no_head_and_keeps_literal_tail():
    raw = '  4*(+1m) '
    assert split_relative_sequence(raw, START, START) == (None, raw)


def test_last_occurrence_becomes_a_singleton_tail():
    text = '4*+1m'
    before, after = split_relative_sequence(text, START, expand_sequence(text, START)[-1])
    assert before == '3*+1m' and after == ''


def test_non_relative_programs_are_unsupported_and_unknown_boundaries_are_rejected():
    for text in ('4*1.', '10:00 +1m', 'every(1m)', ''):
        assert split_relative_sequence(text, START, START) is None
    with pytest.raises(PatternError, match='instruction boundary'):
        split_relative_sequence('4*+1m', START, START + timedelta(hours=1))


def test_large_uniform_relative_program_splits_without_a_large_date_list():
    text = '800*+1d'
    boundary = START + timedelta(days=400)
    before, after = split_relative_sequence(text, START, boundary)
    assert (before, after) == ('399*+1d', '400*+1d')


def test_public_relative_steps_keep_expanded_units_for_calendar_intent_checks():
    assert relative_sequence_steps('2*(+1m +2) +3w') == (
        (1, 'm'), (2, 'w'), (1, 'm'), (2, 'w'), (3, 'w'))
    assert relative_sequence_steps('+1h +30min') == ((1, 'h'), (30, 'min'))


def test_public_relative_steps_do_not_classify_other_or_invalid_programs():
    for text in ('', ' ', '2*1.', 'every(1d)', '+1d 10:00', '+0', None):
        assert relative_sequence_steps(text) is None
