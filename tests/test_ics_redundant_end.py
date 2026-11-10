"""Recover contradictory-property syntax only when its range is unambiguous."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from debacal.ics import export_ics, normalize_caldav_ics
from debacal.ics_fallback import components
from debacal.ics_resources import parse_report


def document(*properties):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic Legacy Range//EN',
        'BEGIN:VEVENT', 'UID:redundant-end@example.invalid',
        'DTSTAMP:20260101T000000Z', 'SUMMARY:Synthetic range',
        *properties, 'END:VEVENT', 'END:VCALENDAR', '',
    ])


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('properties,end', [
    (['DTSTART:20261110T090000Z', 'DTEND:20261110T100000Z', 'DURATION:PT1H'],
     '2026-11-10T10:00:00+00:00'),
    (['DTSTART:20261110T090000', 'DTEND:20261110T100000', 'DURATION:PT1H'],
     None),
    (['DTSTART;VALUE=DATE:20261110', 'DTEND;VALUE=DATE:20261112', 'DURATION:P2D'],
     None),
    (['DTSTART;TZID=Europe/Berlin:20261024T110000', 'DTEND;TZID=Europe/Berlin:20261025T110000', 'DURATION:P1D'],
     '2026-10-25T10:00:00+00:00'),
    (['DTSTART;TZID=Europe/Berlin:20261024T110000', 'DTEND:20261025T090000Z', 'DURATION:PT24H'],
     '2026-10-25T09:00:00+00:00'),
])
def test_equal_independently_computed_ends_import_and_export_one_range(full, properties, end):
    report = parse_report(document(*properties), 'synthetic', use_full=full)
    assert not report.rejected
    item, = report.events
    assert 'DTEND' in item.raw_ics and 'DURATION' in item.raw_ics
    if end is not None:
        assert item.end.astimezone(timezone.utc) == datetime.fromisoformat(end)
    outgoing = normalize_caldav_ics(export_ics([replace(item, title='Edited synthetic range')]))
    component, = components(outgoing)
    assert 'DTEND' in component and 'DURATION' not in component
    restored, = parse_report(outgoing, 'synthetic', use_full=full).events
    assert (restored.start, restored.end, restored.all_day, restored.floating_time) == (
        item.start, item.end, item.all_day, item.floating_time)


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('properties', [
    ['DTSTART:20261110T090000Z', 'DTEND:20261110T110000Z', 'DURATION:PT1H'],
    ['DTSTART;VALUE=DATE:20261110', 'DTEND;VALUE=DATE:20261112', 'DURATION:P1D'],
    ['DTSTART;TZID=Europe/Berlin:20261024T110000', 'DTEND;TZID=Europe/Berlin:20261025T110000', 'DURATION:PT24H'],
    ['DTSTART;TZID=Europe/Berlin:20261025T023000', 'DTEND:20261025T013000Z', 'DURATION:PT0S'],
    ['DTSTART:20261110T090000', 'DTEND:20261110T100000Z', 'DURATION:PT1H'],
])
def test_disagreeing_range_declarations_are_quarantined_instead_of_guessed(full, properties):
    report = parse_report(document(*properties), 'synthetic', use_full=full)
    assert not report.events
    issue, = report.rejected
    assert 'DTEND and DURATION specify different event ends' in issue.reason
    assert 'DURATION:' in issue.raw_ics and 'DTEND' in issue.raw_ics


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('start,end,recurrence', [
    ('20260307T110000', '20260308T110000', 'RRULE:FREQ=WEEKLY;COUNT=6'),
    ('20260128T110000', '20260129T110000', 'RRULE:FREQ=MONTHLY;COUNT=4'),
    ('20260307T110000', '20260308T110000', 'RDATE;TZID=Europe/Berlin:20260328T110000'),
])
def test_nominal_duration_agreement_at_anchor_does_not_guess_recurring_dst_semantics(full, start, end, recurrence):
    raw = document('DTSTART;TZID=Europe/Berlin:' + start,
                   'DTEND;TZID=Europe/Berlin:' + end, 'DURATION:P1D', recurrence)
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.events
    issue, = report.rejected
    assert 'may differ across timezone changes' in issue.reason


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('properties', [
    ['DTSTART:20260307T110000Z', 'DTEND:20260308T110000Z', 'DURATION:P1D'],
    ['DTSTART:20260307T110000', 'DTEND:20260308T110000', 'DURATION:P1D'],
    ['DTSTART;VALUE=DATE:20260307', 'DTEND;VALUE=DATE:20260308', 'DURATION:P1D'],
    ['DTSTART;TZID=Europe/Berlin:20260307T110000', 'DTEND;TZID=Europe/Berlin:20260308T110000', 'DURATION:PT24H'],
])
def test_unambiguous_recurring_duration_forms_remain_recoverable(full, properties):
    report = parse_report(document(*properties, 'RRULE:FREQ=WEEKLY;COUNT=6'), 'synthetic', use_full=full)
    assert not report.rejected
    item, = report.events
    outgoing = normalize_caldav_ics(export_ics([item]))
    assert 'DURATION' not in components(outgoing)[0]
    assert not parse_report(outgoing, 'synthetic', use_full=full).rejected
