"""RFC 5545's prescribed unfolding handles UTF-8 split between octets."""
from dataclasses import replace

import pytest

from debacal.ics import export_ics, normalize_caldav_ics, parse_ics_report
from debacal.ics_fallback import unfolded


def document(description):
    return (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic UTF8 Fold//EN\r\n'
            b'BEGIN:VEVENT\r\nUID:folded-text@example.invalid\r\nDTSTAMP:20260101T000000Z\r\n'
            b'DTSTART:20261110T090000Z\r\nDTEND:20261110T100000Z\r\nSUMMARY:Synthetic\r\n'
            b'DESCRIPTION:' + description + b'\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')


@pytest.mark.parametrize('value', ['Réunion', '日本語', '📅 calendar'])
@pytest.mark.parametrize('continuation', [b'\r\n ', b'\r\n\t', b'\n ', b'\r '])
def test_unfolds_each_possible_byte_split_before_import_and_export(value, continuation):
    encoded = value.encode()
    for index in range(1, len(encoded)):
        raw = document(encoded[:index] + continuation + encoded[index:])
        report = parse_ics_report(raw, 'synthetic')
        assert not report.issues
        item, = report.events
        assert item.description == value
        restored, = parse_ics_report(normalize_caldav_ics(export_ics([replace(item, title='Edited')])),'synthetic').events
        assert restored.description == value


def test_unfolding_still_rejects_non_utf8_and_keeps_significant_whitespace():
    with pytest.raises(UnicodeDecodeError):
        unfolded(document(b'Invalid \xff text'))
    assert unfolded(b'DESCRIPTION:one\r\n  two') == ['DESCRIPTION:one two']
    assert unfolded(b'\xef\xbb\xbfBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n') == ['BEGIN:VCALENDAR', 'END:VCALENDAR']
