"""Cut keeps a symbolic master and clips only native overflowing ranges."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event
from debacal.range_policies import (cut_range_for_key, iter_cut_ranges, occupied_span,
                                     occurrence_end, range_policy_options, shift_range_fits,
                                     uncut_occurrence_end)
from debacal.recurrence import expand_event, occurrence, recurrence_key


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
