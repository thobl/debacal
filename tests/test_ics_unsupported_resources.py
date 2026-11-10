"""Unsupported calendar semantics are identifiable and never guessed away."""
import pytest

from debacal.ics import export_ics, normalize_caldav_ics
from debacal.ics_resources import parse_report


def event(uid, *properties):
    return '\r\n'.join(['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:20260101T000000Z',
                         'DTSTART:20261110T090000Z', 'DURATION:PT1H', *properties, 'END:VEVENT'])


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('bad,reason', [
    (event('unsupported', 'RECURRENCE-ID;RANGE=THISANDFUTURE:20261110T090000Z'), 'THISANDFUTURE'),
    (event('unsupported', 'RECURRENCE-ID:20261110T090000Z'), 'detached occurrences'),
    (event('unsupported', 'EXRULE:FREQ=WEEKLY;BYDAY=MO'), 'EXRULE'),
    (event('unsupported', 'RRULE:FREQ=DAILY', 'RRULE:FREQ=WEEKLY'), 'multiple RRULE'),
    (event('unsupported') + '\r\n' + event('unsupported'), 'Several master events'),
])
def test_specific_diagnostic_retains_failed_uid_and_accepts_healthy_peer(full, bad, reason):
    data = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', bad, event('healthy'), 'END:VCALENDAR', ''])
    report = parse_report(data, 'synthetic', use_full=full)
    assert [item.ical_uid for item in report.events] == ['healthy']
    rejected, = report.rejected
    assert rejected.uid == 'unsupported' and reason in rejected.reason
    assert 'UID:unsupported' in rejected.raw_ics
    assert 'UID:healthy' not in rejected.raw_ics


@pytest.mark.parametrize('scale', ['JULIAN', 'Synthetic-Unknown', ''])
@pytest.mark.parametrize('full', [False, True])
def test_unknown_calendar_scale_is_not_silently_interpreted_as_gregorian(scale, full):
    data = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'CALSCALE:' + scale,
                         event('scale'), 'END:VCALENDAR', ''])
    report = parse_report(data, 'synthetic', use_full=full)
    assert not report.events
    rejected, = report.rejected
    assert 'CALSCALE' in rejected.reason
    assert 'CALSCALE:' + scale in rejected.raw_ics


@pytest.mark.parametrize('full', [False, True])
def test_quoted_parameter_colon_does_not_change_calendar_scale(full):
    data = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:Synthetic',
                         'CALSCALE;X-SYNTHETIC="A:B":GREGORIAN', event('scale'), 'END:VCALENDAR', ''])
    report = parse_report(data, 'synthetic', use_full=full)
    assert not report.issues
    item, = report.events
    outgoing = normalize_caldav_ics(export_ics([item]))
    assert b'CALSCALE;X-SYNTHETIC="A:B":GREGORIAN' in outgoing
