"""A multi-resource export has one unambiguous VTIMEZONE for each identifier."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from icalendar import Calendar
import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Event


ZONE = ZoneInfo('Europe/Berlin')


def series(identifier, year):
    start = datetime(year, 10, 20, 9, tzinfo=ZONE)
    return Event(identifier, 'local', 'A series', start, start + timedelta(hours=1),
                 recurrence_rule='FREQ=WEEKLY;COUNT=4')


def assert_unique_timezones(data):
    # Structural assertion with an independent component parser: the app's
    # importer historically accepted duplicates and cannot certify this export.
    calendar = Calendar.from_ical(data)
    zones = calendar.walk('VTIMEZONE')
    identifiers = [str(item['TZID']) for item in zones]
    assert len(identifiers) == len(set(identifiers))
    return calendar, zones


@pytest.mark.parametrize('retained', [False, True])
def test_generated_timezones_share_union_coverage_across_distant_event_eras(retained):
    events = series('modern', 2026), series('future', 2110)
    if retained:
        events = tuple(replace(event, raw_ics=export_ics([event]).decode()) for event in events)
    calendar, zones = assert_unique_timezones(export_ics(events))
    assert len(zones) == 1 and str(zones[0]['TZID']) == 'Europe/Berlin'
    # Force evaluation of the exported observances instead of looking up the
    # host's IANA zone, which could conceal an insufficient coverage block.
    exported_zone = zones[0].to_tz(lookup_tzid=False)
    for year in (2026, 2110, 2111):
        for month in (1, 7, 11):
            clock = datetime(year, month, 10, 9)
            assert clock.replace(tzinfo=exported_zone).utcoffset() == clock.replace(tzinfo=ZONE).utcoffset()
    assert len(calendar.walk('VEVENT')) == 2


def test_comments_and_property_order_do_not_duplicate_imported_zone_identifiers():
    first, second = series('first', 2026), series('second', 2026)
    payload = export_ics([first]).decode()
    other = payload.replace('BEGIN:VTIMEZONE\r\n', 'BEGIN:VTIMEZONE\r\nCOMMENT:Another exporter\r\n')
    first, second = replace(first, raw_ics=payload), replace(second, raw_ics=other)
    calendar, zones = assert_unique_timezones(export_ics([first, second]))
    assert len(zones) == 1 and len(calendar.walk('VEVENT')) == 2


def test_conflicting_imported_definitions_fail_with_recovery_instead_of_choosing_an_offset():
    first, second = series('first', 2026), series('second', 2026)
    payload = export_ics([first]).decode()
    conflicting = payload.replace('TZOFFSETTO:+0200', 'TZOFFSETTO:+0300')
    assert conflicting != payload
    before = first.to_dict(), second.to_dict()
    with pytest.raises(ValueError, match='Incompatible timezone definitions.*Export.*separately'):
        export_ics([replace(first, raw_ics=payload), replace(second, raw_ics=conflicting)])
    assert (first.to_dict(), second.to_dict()) == before


def test_observance_nesting_is_part_of_definition_identity():
    from debacal.ics_resources import _timezone_signature
    daylight = ['BEGIN:DAYLIGHT', 'DTSTART:20260329T020000', 'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0200', 'END:DAYLIGHT']
    standard = ['BEGIN:STANDARD', 'DTSTART:20261025T030000', 'TZOFFSETFROM:+0200', 'TZOFFSETTO:+0100', 'END:STANDARD']
    first = ['BEGIN:VTIMEZONE', 'TZID:Europe/Berlin', *daylight, *standard, 'END:VTIMEZONE']
    swapped = [*first]
    a, b = swapped.index('TZOFFSETTO:+0200'), swapped.index('TZOFFSETTO:+0100')
    swapped[a], swapped[b] = swapped[b], swapped[a]
    assert _timezone_signature(first) != _timezone_signature(swapped)


@pytest.mark.parametrize('property_name', ['tzid', 'TZID;VALUE=TEXT'])
def test_existing_zone_identifier_is_found_independently_of_case_and_parameters(property_name):
    raw = '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VTIMEZONE', property_name + ':Europe/Berlin',
        'BEGIN:DAYLIGHT', 'DTSTART:19700329T020000', 'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0200',
        'RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3', 'END:DAYLIGHT',
        'BEGIN:STANDARD', 'DTSTART:19701025T030000', 'TZOFFSETFROM:+0200', 'TZOFFSETTO:+0100',
        'RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10', 'END:STANDARD', 'END:VTIMEZONE',
        'BEGIN:VEVENT', 'UID:synthetic-zone-case@example.invalid', 'SUMMARY:Synthetic meeting',
        'DTSTART;TZID=Europe/Berlin:20261020T090000', 'DTEND;TZID=Europe/Berlin:20261020T100000',
        'RRULE:FREQ=WEEKLY;COUNT=4', 'END:VEVENT', 'END:VCALENDAR', '',
    ])
    events = parse_ics(raw, 'local')
    calendar, zones = assert_unique_timezones(export_ics(events))
    assert len(calendar.walk('VEVENT')) == len(zones) == 1
    assert str(zones[0]['TZID']) == 'Europe/Berlin'
    # Retain the imported rule-based definition, rather than adding a generated
    # RDATE-based definition whose offsets only happen to agree near this event.
    assert all('RRULE' in observance for observance in zones[0].subcomponents)


@pytest.mark.parametrize('claims', [
    ['1970-01-01 to 2101-01-01', '1971-01-01 to 2101-01-01'],
    ['0001-01-01 to 9999-01-01'],
    ['1970-01-01 to 2482-01-02'],
])
def test_untrusted_coverage_claims_cannot_trigger_repeated_or_unbounded_generation(claims):
    from debacal.ics_resources import _generated_timezone_coverage
    block = ('BEGIN:VTIMEZONE', 'TZID:Europe/Berlin',
             *(f'COMMENT:This timezone only works from {claim}.' for claim in claims),
             'BEGIN:STANDARD', 'DTSTART:19700101T000000', 'TZOFFSETFROM:+0100',
             'TZOFFSETTO:+0100', 'END:STANDARD', 'END:VTIMEZONE')
    with patch('debacal.ics_resources._timezone_definition', side_effect=AssertionError('No regeneration')):
        assert _generated_timezone_coverage(block) is None
