"""A forgiving import must still produce a valid CalDAV storage envelope."""
from dataclasses import replace

from icalendar import Calendar
import pytest

from debacal.ics import export_ics, normalize_caldav_ics, parse_ics
from debacal.ics_fallback import CalendarValueError


def calendar(headers):
    return ('\r\n'.join([
        'BEGIN:VCALENDAR', *headers, 'X-SYNTHETIC-CALENDAR:Keep this header',
        'BEGIN:VEVENT', 'UID:synthetic-envelope@example.invalid',
        'DTSTAMP:20261101T120000Z', 'DTSTART:20261110T090000Z',
        'DTEND:20261110T100000Z', 'SUMMARY:Synthetic appointment',
        'ORGANIZER:mailto:host@example.invalid', 'ATTENDEE:mailto:guest@example.invalid',
        'X-SYNTHETIC-EVENT:Keep this property',
        'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT15M',
        'DESCRIPTION:Keep this reminder', 'END:VALARM',
        'END:VEVENT', 'END:VCALENDAR', '',
    ])).encode()


@pytest.mark.parametrize('headers', [
    [], ['VERSION:2.0'], ['PRODID:-//Synthetic Producer//EN'],
    ['VERSION:1.0', 'PRODID:-//Synthetic Legacy Producer//EN'],
    ['VERSION:2.0', 'VERSION:2.0', 'PRODID:-//Synthetic Producer//EN'],
    ['VERSION:2.0', 'PRODID:'],
    ['VERSION:2.0', 'PRODID:   '],
    ['VERSION:2.0', 'PRODID:-//First//EN', 'PRODID:-//Second//EN'],
])
@pytest.mark.parametrize('as_text', [False, True])
def test_import_edit_upload_repairs_transport_headers_without_changing_event_data(headers, as_text):
    raw = calendar(headers)
    event, = parse_ics(raw, 'synthetic')
    edited = replace(event, title='Edited synthetic appointment')
    exported = export_ics([edited])
    result = normalize_caldav_ics(exported.decode() if as_text else exported)
    parsed = Calendar.from_ical(result)
    assert str(parsed['VERSION']) == '2.0'
    assert isinstance(parsed['VERSION'], str)
    assert str(parsed['PRODID']).strip()
    assert isinstance(parsed['PRODID'], str)
    assert normalize_caldav_ics(result) == result
    for property in (b'ORGANIZER:mailto:host@example.invalid', b'ATTENDEE:mailto:guest@example.invalid',
                     b'X-SYNTHETIC-CALENDAR:Keep this header', b'X-SYNTHETIC-EVENT:Keep this property',
                     b'BEGIN:VALARM', b'DESCRIPTION:Keep this reminder'):
        assert property in result
    restored, = parse_ics(result, 'synthetic')
    assert (restored.title, restored.start, restored.end) == (edited.title, edited.start, edited.end)
    # The file-export path keeps its retained envelope; only DAV repairs it.
    for line in headers:
        assert line.encode() in exported


@pytest.mark.parametrize('headers', [
    ['VERSION:2.0', 'PRODID:-//Synthetic Producer//EN'],
    ['version:2.0', 'prodid:-//Synthetic Producer//EN', 'calscale:gregorian'],
    ['VERSION:2.0', 'PRODID;X-SOURCE="Producer:One":-//Synthetic Producer//EN', 'CALSCALE:GREGORIAN'],
])
def test_valid_transport_metadata_is_byte_for_byte_unchanged(headers):
    raw = calendar(headers)
    assert normalize_caldav_ics(raw) == raw


def test_repeated_gregorian_scale_is_collapsed_without_adding_an_absent_scale():
    headers = ['VERSION:2.0', 'PRODID:-//Synthetic Producer//EN']
    raw = calendar([*headers, 'CALSCALE:GREGORIAN', 'calscale:gregorian'])
    result = normalize_caldav_ics(raw)
    parsed = Calendar.from_ical(result)
    assert str(parsed['CALSCALE']) == 'GREGORIAN' and isinstance(parsed['CALSCALE'], str)
    assert normalize_caldav_ics(result) == result
    assert b'CALSCALE:' not in normalize_caldav_ics(calendar(headers))


@pytest.mark.parametrize('scales', [['CALSCALE:HEBREW'], ['CALSCALE:'],
                                   ['CALSCALE:GREGORIAN', 'CALSCALE:ISLAMIC']])
def test_unknown_calendar_scale_requires_review_instead_of_silently_changing_dates(scales):
    raw = calendar(['VERSION:2.0', 'PRODID:-//Synthetic Producer//EN', *scales])
    with pytest.raises(CalendarValueError, match='review the original calendar scale'):
        normalize_caldav_ics(raw)


def test_scheduling_method_is_removed_alongside_required_header_repairs():
    result = normalize_caldav_ics(calendar(['METHOD:REQUEST']))
    parsed = Calendar.from_ical(result)
    assert 'METHOD' not in parsed and str(parsed['VERSION']) == '2.0'
    assert str(parsed['PRODID']).strip()
