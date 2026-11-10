"""RFC calendar streams retain envelope scope and isolate rejected resources."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from debacal.ics import export_ics, normalize_caldav_ics, parse_ics_report
from debacal.ics_resources import parse_report
from debacal.models import Calendar
from debacal.store import Store


def document(uid, *, offset='+0100', properties=()):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic calendar stream//EN',
        'BEGIN:VTIMEZONE', 'TZID:Synthetic/shared', 'BEGIN:STANDARD',
        'DTSTART:19700101T000000', 'TZOFFSETFROM:' + offset, 'TZOFFSETTO:' + offset,
        'END:STANDARD', 'END:VTIMEZONE', 'BEGIN:VEVENT', 'UID:' + uid,
        'DTSTAMP:20260101T000000Z', 'SUMMARY:Synthetic stream event',
        'DTSTART;TZID=Synthetic/shared:20261110T110000',
        'DTEND;TZID=Synthetic/shared:20261110T120000',
        *properties, 'END:VEVENT', 'END:VCALENDAR', '',
    ])


@pytest.mark.parametrize('full', [False, True])
def test_calendar_stream_keeps_reused_timezone_definitions_separate(full):
    raw = '\r\n' + document('first', offset='+0100') + '\r\n' + document('second', offset='+0300')
    report = parse_report(raw, 'stream', use_full=full)
    assert not report.issues and len(report.events) == 2
    expected = {'first': 10, 'second': 8}
    with Store(':memory:') as store:
        store.save_calendar(Calendar('stream', 'Synthetic', '#123456'))
        store.import_report(report, calendar_id='stream')
        edited = [replace(event, title='Edited stream event') for event in store.list_events()]
        store.apply('Edit stream', edited)
        for event in store.list_events():
            assert event.start.astimezone(timezone.utc) == datetime(2026, 11, 10, expected[event.ical_uid], tzinfo=timezone.utc)
            exported = normalize_caldav_ics(export_ics([event]))
            assert exported.count(b'BEGIN:VCALENDAR') == 1
            restored, = parse_report(exported, 'stream', use_full=full).events
            assert restored.start.timestamp() == event.start.timestamp()
        combined = export_ics(store.list_events())
        assert combined.count(b'BEGIN:VCALENDAR') == 2
        after = parse_report(combined, 'stream', use_full=full)
        assert not after.issues
        assert {event.ical_uid: event.start.timestamp() for event in after.events} == {
            event.ical_uid: event.start.timestamp() for event in edited}


@pytest.mark.parametrize('full', [False, True])
def test_invalid_uid_in_one_envelope_does_not_discard_healthy_peers(full):
    report = parse_report(document('broken', properties=['RRULE:FREQ=INVALID']) + document('healthy'),
                          'stream', use_full=full)
    assert [event.ical_uid for event in report.events] == ['healthy']
    assert [item.uid for item in report.rejected] == ['broken']


def test_duplicate_uid_across_envelopes_keeps_both_sources_for_review():
    raw = document('duplicate', offset='+0100') + document('duplicate', offset='+0300') + document('healthy')
    report = parse_ics_report(raw, 'stream')
    assert [event.ical_uid for event in report.events] == ['healthy']
    item, = report.rejected
    assert item.uid == 'duplicate' and 'same event UID' in item.reason
    assert item.raw_ics.count('BEGIN:VCALENDAR') == 2
    assert 'TZOFFSETTO:+0100' in item.raw_ics and 'TZOFFSETTO:+0300' in item.raw_ics


def test_component_limit_applies_across_all_envelopes():
    with pytest.raises(ValueError, match='exceeds 1'):
        parse_ics_report(document('first') + document('second'), 'stream', limit=1)


def test_generated_marker_inside_alarm_cannot_bypass_stream_component_limit():
    from debacal import ics_range_policy
    alarm = ['BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT1M', 'DESCRIPTION:Synthetic',
             ics_range_policy.GENERATED + ':synthetic', 'END:VALARM']
    raw = ''.join(document(uid, properties=alarm) for uid in ('one', 'two', 'three'))
    with pytest.raises(ValueError, match='exceeds 2'):
        parse_ics_report(raw, 'stream', limit=2)


@pytest.mark.parametrize('cancelled_first', [False, True])
def test_competing_cancelled_and_active_documents_are_both_retained(cancelled_first):
    versions = [document('duplicate'), document('duplicate', properties=['STATUS:CANCELLED'])]
    if cancelled_first:
        versions.reverse()
    report = parse_ics_report(''.join(versions) + document('healthy'), 'stream')
    assert [event.ical_uid for event in report.events] == ['healthy']
    rejected, = report.rejected
    assert rejected.raw_ics.count('UID:duplicate') == 2
    assert 'STATUS:CANCELLED' in rejected.raw_ics


@pytest.mark.parametrize('raw', [document('first').replace('END:VCALENDAR', '') + document('second'),
                               document('first') + 'Unexpected text\r\n' + document('second')])
def test_ambiguous_envelope_boundaries_are_retained_without_guessing(raw):
    report = parse_ics_report(raw, 'stream')
    assert not report.events
    item, = report.rejected
    assert 'calendar stream' in item.reason
    assert 'UID:first' in item.raw_ics and 'UID:second' in item.raw_ics
