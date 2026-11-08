"""Calendar days and elapsed hours remain distinct across clock changes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.event_duration import (add_duration, add_elapsed, elapsed_duration,
                                   end_for, normalized_spec, parse_duration_spec, same_duration)
from debacal.models import Event


ZONE = ZoneInfo('Europe/Berlin')


@pytest.mark.parametrize('month, day, nominal_hours, exact_end_hour', [
    (3, 28, 23, 13), (10, 24, 25, 11),
])
def test_nominal_day_and_24_elapsed_hours_are_distinct(month, day, nominal_hours, exact_end_hour):
    start = datetime(2026, month, day, 12, tzinfo=ZONE)
    nominal = add_duration(start, 'P1D')
    exact = add_duration(start, 'PT24H')
    assert nominal == datetime(2026, month, day + 1, 12, tzinfo=ZONE)
    assert elapsed_duration(start, nominal) == timedelta(hours=nominal_hours)
    assert exact.hour == exact_end_hour and exact.day == day + 1
    assert elapsed_duration(start, exact) == timedelta(hours=24)


def test_nominal_days_are_applied_before_elapsed_clock_parts():
    start = datetime(2026, 10, 24, 2, 30, tzinfo=ZONE)
    end = add_duration(start, 'P1DT1H')
    assert end.astimezone(timezone.utc) == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert end.hour == 2 and end.fold == 1


def test_elapsed_range_retains_microseconds_and_repeated_hour():
    start = datetime(2026, 10, 25, 2, 30, 0, 123456, tzinfo=ZONE)
    end = add_elapsed(start, timedelta(hours=1))
    assert end.replace(tzinfo=None) == start.replace(tzinfo=None)
    assert end.fold == 1 and elapsed_duration(start, end) == timedelta(hours=1)
    assert add_elapsed(end, timedelta(0)) is end


@pytest.mark.parametrize('text, expected', [
    ('P1W', 'P7D'), ('+P1D', 'P1D'), ('P1DT60M', 'P1DT1H'),
    ('P1DT24H', 'P1DT24H'), ('PT24H', ''), ('P0DT24H', ''), ('PT0S', ''),
])
def test_specification_normalization_does_not_turn_elapsed_hours_into_days(text, expected):
    assert normalized_spec(text) == expected


@pytest.mark.parametrize('text', ['P', 'PT', 'P1DT', '-P1D', 'P1W1D', 'P1M', 'P1.5D', 'PT-1H'])
def test_malformed_duration_is_rejected(text):
    with pytest.raises(ValueError):
        parse_duration_spec(text)


def test_model_retains_nominal_intent_until_the_endpoint_is_explicitly_changed():
    start = datetime(2026, 10, 24, 12, tzinfo=ZONE)
    source = Event('day', 'local', 'Nominal day', start, start.replace(day=25), duration_spec='P1D')
    assert source.duration_spec == 'P1D'
    target = datetime(2026, 3, 28, 12, tzinfo=ZONE)
    assert end_for(source, target) == datetime(2026, 3, 29, 12, tzinfo=ZONE)
    moved = replace(source, start=target, end=end_for(source, target))
    assert moved.duration_spec == 'P1D' and same_duration(source, moved)
    resized = replace(source, end=add_elapsed(start, timedelta(hours=24)))
    assert resized.duration_spec == '' and not same_duration(source, resized)
    assert Event.from_dict(source.to_dict()) == source


def test_elapsed_model_does_not_inherit_the_starting_day_clock_duration():
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE)
    source = Event('range', 'local', 'An elapsed hour', start, start.replace(fold=1))
    tomorrow = datetime(2026, 10, 26, 2, 30, tzinfo=ZONE)
    assert end_for(source, tomorrow) == tomorrow.replace(hour=3)


def test_all_day_range_keeps_floating_dates_across_short_and_long_days():
    start = datetime(2026, 10, 24, tzinfo=ZONE)
    source = Event('day', 'local', 'All day', start, start.replace(day=26), all_day=True, duration_spec='P2D')
    assert source.duration_spec == ''
    target = datetime(2026, 3, 28, tzinfo=ZONE)
    assert end_for(source, target) == target.replace(day=30)


def test_floating_midnight_gap_survives_storage_and_keeps_a_calendar_day():
    zone = ZoneInfo('America/Sao_Paulo')
    source = Event('floating', 'local', 'Floating date', datetime(2018, 11, 4, tzinfo=zone),
                   datetime(2018, 11, 5, tzinfo=zone), all_day=True)
    restored = Event.from_dict(source.to_dict())
    assert restored == source and restored.start.hour == 0
    target = datetime(2018, 11, 6, tzinfo=zone)
    assert end_for(restored, target) == datetime(2018, 11, 7, tzinfo=zone)


def test_old_stored_nominal_duration_is_recovered_from_master_not_alarm():
    start = datetime(2026, 10, 24, 12, tzinfo=ZONE)
    source = Event('old', 'local', 'Nominal day', start, start.replace(day=25))
    raw = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:old\r\nDURATION:P1D\r\n'
           'BEGIN:VALARM\r\nDURATION:PT5M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    payload = source.to_dict()
    payload.pop('duration_spec')
    payload['raw_ics'] = raw
    assert Event.from_dict(payload).duration_spec == 'P1D'
    payload['raw_ics'] = raw.replace('DURATION:P1D\r\n', '')
    assert Event.from_dict(payload).duration_spec == ''


def test_large_retained_calendar_resources_do_not_remain_in_the_duration_cache():
    from debacal import event_duration
    event_duration._cached_retained_spec.cache_clear()
    raw = ('BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:large\nDURATION:P1D\nDESCRIPTION:'
           + 'x' * 70_000 + '\nEND:VEVENT\nEND:VCALENDAR\n')
    assert event_duration.retained_duration_spec(raw) == 'P1D'
    assert event_duration._cached_retained_spec.cache_info().currsize == 0
