"""Contextual recovery of malformed DATE suffixes from Google birthdays.

The public ical.js google_birthday fixture has an all-day master and RDATEs
such as 20131210Z. These tests use synthetic identities and dates instead of
copying upstream event contents.
"""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from debacal.ics import export_ics, normalize_caldav_ics
from debacal.ics_fallback import components
from debacal.ics_resources import parse_report
from debacal.recurrence import expand_events


def document(*properties):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic Birthday Compatibility//EN',
        'BEGIN:VEVENT', 'UID:date-recurrence@example.invalid',
        'DTSTAMP:20260101T000000Z', 'SUMMARY:Synthetic birthday', *properties,
        'END:VEVENT', 'END:VCALENDAR', '',
    ])


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('start_parameter', [';VALUE=DATE', ''])
@pytest.mark.parametrize('recurrence_parameter', ['', ';VALUE=DATE'])
def test_all_day_recurrence_dates_with_utc_suffix_survive_edit_export(full, start_parameter, recurrence_parameter):
    raw = document('DTSTART' + start_parameter + ':20261110',
                   'DTEND' + start_parameter + ':20261111',
                   'RRULE:FREQ=DAILY;COUNT=2',
                   'RDATE' + recurrence_parameter + ':20241110Z,20251110Z',
                   'EXDATE' + recurrence_parameter + ':20261111Z')
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.rejected
    item, = report.events
    assert item.all_day
    assert set(item.recurrence_dates) == {'2024-11-10', '2025-11-10'}
    assert item.excluded_dates == ('2026-11-11',)
    assert '20241110Z' in item.raw_ics
    begin, finish = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)
    expected = [date(year, 11, 10) for year in (2024, 2025, 2026)]
    assert [occurrence.start.date() for occurrence in expand_events([item], begin, finish)] == expected
    outgoing = normalize_caldav_ics(export_ics([replace(item, title='Edited birthday')]))
    block, = components(outgoing)
    assert all(parameters['VALUE'] == 'DATE' for parameters, _value in block['RDATE'])
    assert block['EXDATE'][0][0]['VALUE'] == 'DATE'
    assert {value for _parameters, raw_value in block['RDATE'] for value in raw_value.split(',')} == {'20241110', '20251110'}
    assert block['EXDATE'][0][1] == '20261111'
    restored = parse_report(outgoing, 'synthetic', use_full=full)
    assert not restored.rejected
    assert [occurrence.start.date() for occurrence in expand_events(restored.events, begin, finish)] == expected


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('recurrence', [
    'RDATE:20260230Z',  # Invalid calendar dates cannot be repaired.
    'RDATE;VALUE=DATE-TIME:20261112Z',
    'RDATE;VALUE=PERIOD:20261112Z/P1D',
    'RDATE:2026-11-12Z',
    'RDATE:20261112ZZ',
])
def test_other_recurrence_forms_are_not_reinterpreted(full, recurrence):
    raw = document('DTSTART;VALUE=DATE:20261110', 'DTEND;VALUE=DATE:20261111', recurrence)
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.events and len(report.rejected) == 1
    assert recurrence in report.rejected[0].raw_ics


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('key', ['RDATE', 'EXDATE'])
def test_date_suffix_repair_never_adds_midnight_occurrences_to_timed_series(full, key):
    raw = document('DTSTART:20261110T090000Z', 'DTEND:20261110T100000Z', key + ':20261112Z')
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.events and len(report.rejected) == 1
    assert key + ':20261112Z' in report.rejected[0].raw_ics


@pytest.mark.parametrize('full', [False, True])
def test_valid_timed_utc_recurrence_remains_a_timestamp(full):
    report = parse_report(document('DTSTART:20261110T090000Z', 'DTEND:20261110T100000Z',
                                   'RDATE:20261112T090000Z'), 'synthetic', use_full=full)
    assert not report.rejected
    item, = report.events
    assert not item.all_day and item.recurrence_dates == ('2026-11-12T09:00:00+00:00',)
