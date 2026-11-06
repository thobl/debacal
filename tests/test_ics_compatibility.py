"""Reviewed file imports tolerate missing IDs without weakening remote sync."""
from datetime import datetime, timedelta, timezone

import pytest

from debacal.ics import export_ics, parse_ics, parse_ics_report
from debacal.ics_fallback import components, prop
from debacal.ics_resources import parse_report
from debacal.import_preparation import prepare_sources
from debacal.import_sources import ImportSource
from debacal.recurrence import expand_events


def document(*events):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic fixture//EN\r\n'
            'X-WR-CALNAME:Synthetic calendar\r\n' + ''.join(events) + 'END:VCALENDAR\r\n').encode()


def event(*, uid='', start='20261109T090000Z', extra='', title='Synthetic meeting'):
    return ('BEGIN:VEVENT\r\n' + ('UID:' + uid + '\r\n' if uid else '') +
            'SUMMARY:' + title + '\r\nDTSTART:' + start + '\r\n'
            'DTEND:20261109T100000Z\r\nDTSTAMP:20261101T120000Z\r\n' + extra + 'END:VEVENT\r\n')


@pytest.mark.parametrize('use_full', [False, True])
def test_reviewed_uid_repair_keeps_dates_metadata_and_alarm_identity(use_full):
    data = document(event(extra=(
        'DESCRIPTION:Synthetic notes\\nSecond paragraph\r\n'
        'X-SYNTHETIC-PROPERTY:Retain this\r\n'
        'BEGIN:VALARM\r\nUID:synthetic-alarm@example.invalid\r\n'
        'ACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:Synthetic reminder\r\nEND:VALARM\r\n')))
    report = parse_report(data, 'destination', use_full=use_full, repair_missing_uids=True)
    assert len(report.events) == len(report.issues) == 1 and not report.rejected
    imported = report.events[0]
    assert imported.ical_uid.startswith('urn:uuid:')
    assert imported.ical_uid != 'synthetic-alarm@example.invalid'
    assert imported.start == datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
    assert imported.end == imported.start + timedelta(hours=1)
    assert imported.description == 'Synthetic notes\nSecond paragraph'
    assert 'generated a stable identifier' in report.issues[0].reason
    assert 'Synthetic meeting' not in report.issues[0].reason
    assert 'X-SYNTHETIC-PROPERTY:Retain this' in imported.raw_ics
    assert 'UID:synthetic-alarm@example.invalid' in imported.raw_ics
    exported = export_ics([imported])
    checked = parse_ics_report(exported, 'destination')
    assert not checked.issues and not checked.rejected
    assert len(checked.events) == 1 and checked.events[0].ical_uid == imported.ical_uid
    assert checked.events[0].start == imported.start and checked.events[0].end == imported.end
    assert b'UID:synthetic-alarm@example.invalid' in exported


def test_missing_identifiers_stay_strict_by_default_for_remote_resources():
    data = document(event())
    strict = parse_ics_report(data, 'remote')
    assert not strict.events and len(strict.rejected) == 1
    assert strict.issues[0].reason == 'Event UID is missing.'
    with pytest.raises(ValueError, match='UID is missing'):
        parse_ics(data, 'remote')
    repaired = parse_ics_report(data, 'local', repair_missing_uids=True)
    assert len(repaired.events) == 1 and not repaired.rejected


def test_repeated_imports_get_stable_distinct_ids_including_identical_events():
    duplicate = event()
    other = event(uid='existing-identity@example.invalid', title='Other synthetic meeting')
    first = parse_ics_report(document(duplicate, duplicate, other), 'local', repair_missing_uids=True)
    again = parse_ics_report(document(other, duplicate, duplicate), 'local', repair_missing_uids=True)
    assert len(first.events) == len({item.id for item in first.events}) == 3
    assert {item.id for item in first.events} == {item.id for item in again.events}
    assert {item.ical_uid for item in first.events} == {item.ical_uid for item in again.events}
    assert any(item.ical_uid == 'existing-identity@example.invalid' for item in first.events)
    assert len(first.issues) == 2 and not first.rejected


def test_repair_does_not_override_invalid_dates_or_guess_detached_series_membership():
    detached = ('BEGIN:VEVENT\r\nRECURRENCE-ID:20261110T090000Z\r\n'
                'DTSTART:20261110T110000Z\r\nEND:VEVENT\r\n')
    report = parse_ics_report(document(event(), event(start='not-a-date'), detached), 'local',
                              repair_missing_uids=True)
    assert len(report.events) == 1 and len(report.rejected) == 2
    assert report.rejected[1].uid == ''
    assert 'RECURRENCE-ID' in report.rejected[1].raw_ics
    assert sum('generated a stable identifier' in issue.reason for issue in report.issues) == 1


def test_empty_uid_is_repaired_once_without_an_extra_empty_property():
    body = event(extra='UID:\r\n')
    report = parse_ics_report(document(body), 'local', repair_missing_uids=True)
    assert len(report.events) == 1 and not report.rejected
    component = components(report.events[0].raw_ics)[0]
    assert len(component['UID']) == 1
    assert prop(component, 'UID') == report.events[0].ical_uid


@pytest.mark.parametrize('use_full', [False, True])
def test_missing_uid_recurring_master_remains_a_standard_series(use_full):
    report = parse_report(document(event(extra='RRULE:FREQ=WEEKLY;COUNT=3\r\n')), 'local',
                          use_full=use_full, repair_missing_uids=True)
    assert len(report.events) == 1 and not report.rejected
    master = report.events[0]
    assert master.recurrence_rule == 'FREQ=WEEKLY;COUNT=3'
    assert len(expand_events(report.events, master.start, master.start + timedelta(days=30))) == 3
    exported = export_ics(report.events)
    assert len(parse_ics(exported, 'local')) == 1 and b'RRULE:FREQ=WEEKLY;COUNT=3' in exported


def test_file_preparation_enables_reviewed_repair_without_changing_source(tmp_path):
    data = document(event(), event(title='Second synthetic meeting'))
    source = tmp_path / 'missing-identifiers.ics'
    source.write_bytes(data)
    prepared = prepare_sources([ImportSource.from_path(source)])
    assert prepared.event_count == 2
    assert len(prepared.warnings) == 1 and 'Missing event UID' in prepared.warnings[0]
    assert source.read_bytes() == data
    report = prepared.items[0].remap('destination')
    assert len(report.events) == 2 and all(item.calendar_id == 'destination' for item in report.events)
    assert not parse_ics_report(export_ics(report.events), 'destination').issues
