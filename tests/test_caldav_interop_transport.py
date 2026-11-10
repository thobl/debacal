"""Compatibility repairs reach the upload bridge without losing pending data."""
from dataclasses import replace

from icalendar import Calendar as ICalendar
import pytest

from debacal.ics import parse_ics_report
from test_vdirsyncer import sample, setup, sync


def imported(*extra, envelope=()):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', *envelope, 'METHOD:REQUEST',
        'BEGIN:VEVENT', 'UID:imported@example.invalid', 'DTSTAMP:20260101T000000Z',
        'DTSTART:20261110T090000Z', 'DURATION:PT1H', 'SUMMARY:Imported synthetic event',
        *extra, 'END:VEVENT', 'END:VCALENDAR', '',
    ])


@pytest.mark.parametrize('header', [(), ('VERSION:1.0',), ('VERSION:2.0', 'VERSION:2.0'),
                                   ('PRODID:One producer', 'PRODID:Another producer')])
def test_imported_envelope_is_repaired_only_when_staged_for_caldav(tmp_path, header):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        source = imported('ORGANIZER:mailto:host@example.invalid',
                          'ATTENDEE:mailto:guest@example.invalid',
                          'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT15M', 'DESCRIPTION:Reminder',
                          'END:VALARM', envelope=header)
        report = store.import_report(parse_ics_report(source, calendar.id), calendar_id=calendar.id)
        event, = report.events
        assert 'METHOD:REQUEST' in store.get_event(event.id).raw_ics
        result = sync(store, calendar, backend)
        assert result.sent == 1 and not result.issues and not store.pending_changes()
        payload = cli.remote[event.ical_uid]
        decoded = ICalendar.from_ical(payload)
        assert str(decoded['VERSION']) == '2.0'
        assert str(decoded['PRODID']) and 'METHOD' not in decoded
        assert payload.count('VERSION:') == payload.count('PRODID:') == 1
        assert 'ATTENDEE:mailto:guest@example.invalid' in payload
        assert len(decoded.walk('VALARM')) == 1
    finally:
        store.close()


def test_ambiguous_imported_metadata_is_retained_while_healthy_uid_uploads(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        source = imported('ORGANIZER:mailto:first@example.invalid', 'ORGANIZER:mailto:second@example.invalid',
                          envelope=('VERSION:2.0', 'PRODID:Synthetic'))
        report = store.import_report(parse_ics_report(source, calendar.id), calendar_id=calendar.id)
        event, = report.events
        healthy = sample(id='healthy-import-peer')
        store.apply('Create healthy peer', [healthy])
        result = sync(store, calendar, backend)
        assert result.sent == 1 and len(result.issues) == 1
        assert 'Conflicting ORGANIZER' in result.issues[0].reason
        assert 'first@example.invalid' not in result.issues[0].reason
        assert event.ical_uid not in cli.remote and healthy.ical_uid in cli.remote
        assert store.get_event(event.id) is not None
        assert [change['event_id'] for change in store.pending_changes()] == [event.id]
        retained, = store.list_quarantined_resources(calendar.id)
        assert 'first@example.invalid' in retained['raw_ics'] and 'second@example.invalid' in retained['raw_ics']
        # A corrected reviewed source with the same identity can be retried.
        corrected = replace(event, raw_ics=event.raw_ics.replace('ORGANIZER:mailto:second@example.invalid\r\n', ''))
        store.apply('Correct imported metadata', [corrected])
        retry = sync(store, calendar, backend)
        assert retry.sent == 1 and not retry.issues and not store.pending_changes()
        assert not store.list_quarantined_resources(calendar.id)
    finally:
        store.close()
