"""Cut keeps a symbolic master and clips only native overflowing ranges."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event
from debacal.patterns import PatternError, compile_sequence
from debacal.range_policies import (cut_range_for_key, iter_cut_ranges, occupied_span,
                                     occurrence_end, range_policy_options, shift_range_fits,
                                     uncut_occurrence_end)
from debacal.recurrence import expand_event, occurrence, recurrence_key
from debacal.regular_schedule import count_to_until, until_to_count
from debacal.schedule_authoring import expression_event, expression_matches


ZONE = ZoneInfo('Europe/Berlin')


def event(*, all_day=True, start=None, end=None, rule='FREQ=MONTHLY;COUNT=3', policy='cut'):
    start = start or datetime(2027, 1, 28, 0 if all_day else 9, tzinfo=ZONE)
    end = end or (start + timedelta(days=2) if all_day else start + timedelta(days=1, hours=1))
    return Event('range', 'local', 'A monthly range', start, end, all_day=all_day,
                  recurrence_rule=rule, range_policy=policy)


def visible(source, start=None, end=None):
    return expand_event(source, start or datetime(2027, 1, 1, tzinfo=ZONE),
                         end or datetime(2027, 4, 1, tzinfo=ZONE))


def test_all_day_cut_uses_the_exclusive_month_boundary():
    source = event()
    items = visible(source)
    assert [(item.start.date().isoformat(), item.end.date().isoformat()) for item in items] == [
        ('2027-01-28', '2027-01-30'), ('2027-02-28', '2027-03-01'), ('2027-03-28', '2027-03-30')]
    assert not source.recurrence_overrides
    assert source.end.date().isoformat() == '2027-01-30'


def test_timed_cut_truncates_at_month_end_instead_of_using_the_original_end_clock():
    source = event(all_day=False)
    items = visible(source)
    february = items[1]
    assert february.start == datetime(2027, 2, 28, 9, tzinfo=ZONE)
    assert february.end == datetime(2027, 3, 1, tzinfo=ZONE)
    assert items[2].end == datetime(2027, 3, 29, 10, tzinfo=ZONE)


@pytest.mark.parametrize('all_day', [False, True])
def test_authored_cut_skips_missing_start_dates_and_only_shortens_existing_ranges(all_day):
    start = datetime(2027, 1, 30, 0 if all_day else 9, tzinfo=ZONE)
    duration = timedelta(days=2) if all_day else timedelta(days=1, hours=1)
    text = 'every(1m, overflow=cut, span=2, count=5)'
    authored = expression_event(event(start=start, end=start + duration, all_day=all_day), text)
    # The standard start rule keeps day 30. Cut changes April/June's end,
    # whereas Shift would invent a February start and move April earlier.
    assert 'BYSETPOS' not in authored.recurrence_rule
    items = visible(authored, datetime(2027, 1, 1, tzinfo=ZONE), datetime(2027, 8, 1, tzinfo=ZONE))
    assert [(item.start.month, item.start.day) for item in items] == [(1, 30), (3, 30), (4, 30), (5, 30), (6, 30)]
    assert items[2].end == datetime(2027, 5, 1, tzinfo=ZONE)
    assert items[4].end == datetime(2027, 7, 1, tzinfo=ZONE)
    assert items[3].end == items[3].start + duration
    assert expression_matches(authored, text)


def test_cut_with_explicit_preferred_day_keeps_it_instead_of_clamping_it():
    start = datetime(2027, 1, 28, tzinfo=ZONE)
    text = 'every(1m, day=30, overflow=cut, span=2, count=4)'
    authored = expression_event(event(start=start, end=start + timedelta(days=2)), text)
    assert 'BYMONTHDAY=30' in authored.recurrence_rule
    items = visible(authored, datetime(2027, 1, 1, tzinfo=ZONE), datetime(2027, 7, 1, tzinfo=ZONE))
    assert [(item.start.month, item.start.day) for item in items] == [(1, 30), (3, 30), (4, 30), (5, 30)]
    assert items[2].end == datetime(2027, 5, 1, tzinfo=ZONE)
    assert expression_matches(authored, text)


def test_yearly_cut_skips_years_without_the_starting_leap_day():
    start = datetime(2028, 2, 29, tzinfo=ZONE)
    text = 'every(1y, overflow=cut, span=2, count=3)'
    authored = expression_event(event(start=start, end=start + timedelta(days=2)), text)
    items = visible(authored, datetime(2028, 1, 1, tzinfo=ZONE), datetime(2037, 1, 1, tzinfo=ZONE))
    assert [(item.start.year, item.start.month, item.start.day) for item in items] == [
        (2028, 2, 29), (2032, 2, 29), (2036, 2, 29)]
    assert all(item.end == datetime(item.start.year, 3, 1, tzinfo=ZONE) for item in items)
    assert expression_matches(authored, text)


def test_cut_regular_count_and_until_controls_count_only_existing_start_dates():
    start = datetime(2027, 1, 30, tzinfo=ZONE)
    options = dict(overflow='cut', range_span=2)
    assert count_to_until(start, 'MONTHLY', 1, 4, **options) == datetime(2027, 5, 30).date()
    assert until_to_count(start, 'MONTHLY', 1, datetime(2027, 4, 30).date(), **options) == 3


@pytest.mark.parametrize('unit', ['1m', '1m!'])
def test_cut_count_bound_proof_accounts_for_months_with_missing_start_dates(unit):
    start = datetime(9999, 1, 30, tzinfo=ZONE)
    compile_sequence(f'every({unit}, overflow=cut, span=2, count=11)', start, all_day=True)
    with pytest.raises(PatternError, match='beyond the supported years'):
        compile_sequence(f'every({unit}, overflow=cut, span=2, count=12)', start, all_day=True)


def test_impossible_preferred_cut_start_has_a_validation_error():
    start = datetime(2027, 2, 28, tzinfo=ZONE)
    with pytest.raises(PatternError, match='cannot retain its exact time'):
        compile_sequence('every(1y, day=30, overflow=cut, span=2, count=3)', start, all_day=True)


def test_fixed_duration_imported_rule_still_flows_across_months():
    items = visible(event(policy=''))
    assert items[1].end == datetime(2027, 3, 2, tzinfo=ZONE)


def test_single_day_cut_never_shortens_a_valid_day():
    source = event(end=datetime(2027, 1, 29, tzinfo=ZONE))
    assert all(item.end.date() - item.start.date() == timedelta(days=1) for item in visible(source))
    assert list(iter_cut_ranges(source)) == []


def test_leap_year_february_keeps_the_two_day_range():
    source = event(start=datetime(2028, 1, 28, tzinfo=ZONE), end=datetime(2028, 1, 30, tzinfo=ZONE))
    items = visible(source, datetime(2028, 1, 1, tzinfo=ZONE), datetime(2028, 4, 1, tzinfo=ZONE))
    assert items[1].start == datetime(2028, 2, 28, tzinfo=ZONE)
    assert items[1].end == datetime(2028, 3, 1, tzinfo=ZONE)
    assert list(iter_cut_ranges(source)) == []


def test_cut_takes_effect_even_if_a_split_reference_raw_duration_crosses_the_month():
    source = event(start=datetime(2027, 2, 28, tzinfo=ZONE), end=datetime(2027, 3, 2, tzinfo=ZONE))
    items = visible(source)
    assert items[0].end == datetime(2027, 3, 1, tzinfo=ZONE)
    assert items[1].end == datetime(2027, 3, 30, tzinfo=ZONE)


def test_manual_detached_end_and_explicit_added_date_keep_their_own_ranges():
    source = event()
    february = datetime(2027, 2, 28, tzinfo=ZONE)
    override = replace(occurrence(source, february), end=february + timedelta(days=4), description='Explicit range')
    source = replace(source, recurrence_overrides={recurrence_key(february, True): override.to_dict()},
                     recurrence_dates=('2027-01-31',))
    items = visible(source)
    added = next(item for item in items if item.start.day == 31)
    detached = next(item for item in items if item.start.month == 2)
    assert added.end == datetime(2027, 2, 2, tzinfo=ZONE)
    assert detached.end == datetime(2027, 3, 4, tzinfo=ZONE)
    assert detached.description == 'Explicit range'
    assert list(iter_cut_ranges(source)) == []


def test_cut_generator_only_yields_changed_native_ranges_and_respects_count_and_exclusions():
    source = event(rule='FREQ=MONTHLY;COUNT=16')
    ranges = list(iter_cut_ranges(source))
    assert [(key, start.date(), end.date()) for key, start, end in ranges] == [
        ('2027-02-28', datetime(2027, 2, 28).date(), datetime(2027, 3, 1).date())]
    assert cut_range_for_key(source, '2027-02-28') == ranges[0][1:]
    assert cut_range_for_key(source, '2027-01-28') is None
    assert cut_range_for_key(source, '2027-02-27') is None
    assert list(iter_cut_ranges(replace(source, excluded_dates=('2027-02-28',)))) == []


def test_yearly_cut_and_date_until_keep_native_date_identities():
    source = event(start=datetime(2028, 2, 28, tzinfo=ZONE), end=datetime(2028, 3, 1, tzinfo=ZONE),
                   rule='FREQ=YEARLY;UNTIL=20300228')
    assert [key for key, _start, _end in iter_cut_ranges(source)] == ['2029-02-28', '2030-02-28']


def test_export_iterator_does_not_construct_an_event_per_generated_occurrence():
    source = event(rule='FREQ=MONTHLY')
    with patch.object(Event, 'from_dict', side_effect=AssertionError('Generated an Event')):
        ranges = list(iter_cut_ranges(source))
    assert len(ranges) > 5000
    assert ranges[-1][0] == '9999-02-28'


@pytest.mark.parametrize('kind', ['DAILY', 'WEEKLY'])
def test_non_calendar_rules_ignore_a_stale_cut_flag(kind):
    source = event(rule=f'FREQ={kind};COUNT=4')
    assert list(iter_cut_ranges(source)) == []
    start = datetime(2027, 1, 31, tzinfo=ZONE)
    assert occurrence_end(source, start) == uncut_occurrence_end(source, start)


@pytest.mark.parametrize('start,end,all_day,span', [
    (datetime(2027, 1, 28, tzinfo=ZONE), datetime(2027, 1, 30, tzinfo=ZONE), True, 2),
    (datetime(2027, 1, 28, 23, tzinfo=ZONE), datetime(2027, 1, 29, tzinfo=ZONE), False, 1),
    (datetime(2027, 1, 28, 23, tzinfo=ZONE), datetime(2027, 1, 29, 1, tzinfo=ZONE), False, 2),
    (datetime(2027, 1, 28, tzinfo=ZONE), datetime(2027, 1, 28, tzinfo=ZONE), False, 1),
])
def test_occupied_span_honors_midnight_and_point_ranges(start, end, all_day, span):
    assert occupied_span(start, end, all_day) == span


def test_overflow_controls_consider_the_end_even_when_the_start_never_overflows():
    source = event()
    assert range_policy_options(source.start, source.end, 'MONTHLY', all_day=True) == (True, True)
    assert range_policy_options(source.start, source.start + timedelta(days=1), 'MONTHLY', all_day=True) == (False, False)
    assert range_policy_options(source.start, source.end, 'MONTHLY', 12, all_day=True) == (False, True)


def test_intentional_cross_month_template_keeps_ordinary_semantics_but_existing_cut_survives_split():
    source = event(start=datetime(2027, 2, 28, tzinfo=ZONE), end=datetime(2027, 3, 2, tzinfo=ZONE))
    assert range_policy_options(source.start, source.end, 'MONTHLY', all_day=True) == (False, False)
    assert range_policy_options(source.start, source.end, 'MONTHLY', all_day=True, range_policy='cut') == (True, True)
    assert range_policy_options(source.start, source.end, 'MONTHLY', all_day=True, span=2) == (True, True)
    january = event(start=datetime(2027, 1, 31, tzinfo=ZONE), end=datetime(2027, 2, 2, tzinfo=ZONE))
    assert range_policy_options(january.start, january.end, 'MONTHLY', all_day=True) == (True, False)


def test_shift_cannot_fit_a_range_longer_than_the_selected_months():
    source = event(start=datetime(2027, 1, 1, tzinfo=ZONE), end=datetime(2027, 2, 1, tzinfo=ZONE))
    assert not shift_range_fits(source.start, 'MONTHLY', 1, 31)
    assert not shift_range_fits(source.start, 'MONTHLY', 2, 31)  # September has 30 days.
    assert shift_range_fits(source.start, 'MONTHLY', 12, 31)  # January only.
    assert shift_range_fits(source.start, 'MONTHLY', 1, 28)
    leap = datetime(2028, 2, 1, tzinfo=ZONE)
    assert not shift_range_fits(leap, 'YEARLY', 4, 29)  # 2100 is not a leap year.


def test_preferred_start_and_long_gregorian_cycles_are_retained():
    source = event(start=datetime(2028, 2, 28, tzinfo=ZONE), end=datetime(2028, 3, 1, tzinfo=ZONE))
    assert range_policy_options(source.start, source.end, 'YEARLY', 4, all_day=True) == (True, True)
    assert range_policy_options(source.start, source.end, 'MONTHLY', 12, all_day=True, preferred_day=29, span=1) == (True, False)


@pytest.mark.parametrize('invalid', [None, 'skip', 'shift', True, 1])
def test_only_cut_is_an_explicit_stored_range_policy(invalid):
    with pytest.raises(ValueError, match='range policy'):
        event(policy=invalid)


def test_range_policy_defaults_and_roundtrips_through_event_records():
    source = event()
    assert Event.from_dict(source.to_dict()) == source
    old = source.to_dict()
    old.pop('range_policy')
    assert Event.from_dict(old).range_policy == ''
