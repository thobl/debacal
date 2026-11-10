"""Targeted legacy encoding recovery must preserve the original resource.

The source patterns are documented by KDE's separate vCalendar support and
libical's vCalendar MIME parser. These cases contain only synthetic text and
reserved .invalid identifiers; upstream personal test calendars are not copied.
"""
from dataclasses import replace

import pytest

from debacal.ics import export_ics, normalize_caldav_ics
from debacal.ics_fallback import components, prop
from debacal.ics_resources import parse_report


def document(*text):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic Legacy Text//EN',
        'BEGIN:VEVENT', 'UID:legacy-text@example.invalid',
        'DTSTAMP:20260101T000000Z', 'DTSTART:20261110T090000Z',
        'DTEND:20261110T100000Z', *text, 'END:VEVENT', 'END:VCALENDAR', '',
    ])


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('charset,encoded', [('UTF-8', 'R=C3=A9union'), ('ISO-8859-1', 'R=E9union'),
                                           ('Windows-1252', 'R=E9union')])
def test_explicit_quoted_printable_text_survives_edit_and_caldav_export(full, charset, encoded):
    raw = document(*[f'{key};ENCODING=QUOTED-PRINTABLE;CHARSET={charset}:{encoded}'
                     for key in ('SUMMARY', 'DESCRIPTION', 'LOCATION')],
                   'X-SYNTHETIC-OPAQUE;ENCODING=QUOTED-PRINTABLE:=41')
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.rejected
    item, = report.events
    assert (item.title, item.description, item.location) == ('Réunion',) * 3
    assert f'CHARSET={charset}:{encoded}' in item.raw_ics
    changed = replace(item, title=item.title + ' edited')
    exported = normalize_caldav_ics(export_ics([changed]))
    source = components(exported)[0]
    for key in ('SUMMARY', 'DESCRIPTION', 'LOCATION'):
        assert not source[key][0][0].get('ENCODING')
        assert not source[key][0][0].get('CHARSET')
    assert b'X-SYNTHETIC-OPAQUE;ENCODING=QUOTED-PRINTABLE:=41' in exported
    restored, = parse_report(exported, 'synthetic', use_full=full).events
    assert (restored.title, restored.description, restored.location) == ('Réunion edited', 'Réunion', 'Réunion')


@pytest.mark.parametrize('full', [False, True])
def test_standard_line_folding_is_applied_before_quoted_printable_decoding(full):
    raw = document('SUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:R=C3\r\n =A9union',
                   'DESCRIPTION;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:First=0ASecond=3DLine')
    item, = parse_report(raw, 'synthetic', use_full=full).events
    assert item.title == 'Réunion'
    assert item.description == 'First\nSecond=Line'


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('encoded,expected', [
    ('Comma=5C,tail', 'Comma,tail'),
    ('Semicolon=5C;tail', 'Semicolon;tail'),
    ('Comma\\=2Ctail', 'Comma,tail'),
    ('Semicolon\\=3Btail', 'Semicolon;tail'),
    ('Ends\\=5C', 'Ends\\'),
    ('Literal=5C=5Cn', 'Literal\\n'),
])
def test_quoted_printable_precedes_text_unescaping_in_both_parser_paths(full, encoded, expected):
    raw = document(*[f'{key};ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:{encoded}'
                     for key in ('SUMMARY', 'DESCRIPTION', 'LOCATION')])
    item, = parse_report(raw, 'synthetic', use_full=full).events
    assert (item.title, item.description, item.location) == (expected,) * 3
    restored, = parse_report(export_ics([replace(item, title='Edited')]), 'synthetic', use_full=full).events
    assert (restored.description, restored.location) == (expected,) * 2


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('property,reason', [
    ('SUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=Synthetic-Unknown:=41', 'Unsupported character set'),
    ('SUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:=FF', 'does not match'),
    ('SUMMARY;ENCODING=Synthetic-Unknown:Value', 'Unsupported encoding'),
    ('SUMMARY;ENCODING=QUOTED-PRINTABLE:Value=', 'Invalid quoted-printable'),
    ('SUMMARY;ENCODING=QUOTED-PRINTABLE:Value=G1', 'Invalid quoted-printable'),
])
def test_uninterpretable_declared_text_is_retained_with_an_actionable_diagnostic(full, property, reason):
    report = parse_report(document(property), 'synthetic', use_full=full)
    assert not report.events
    issue, = report.rejected
    assert reason in issue.reason
    assert property in issue.raw_ics


def test_encoded_identity_metadata_is_not_reinterpreted_by_display_text_recovery():
    component = {'UID': [({'ENCODING': 'QUOTED-PRINTABLE'}, 'event=41@example.invalid')]}
    assert prop(component, 'UID') == 'event=41@example.invalid'


@pytest.mark.parametrize('full', [False, True])
def test_unknown_text_charset_isolated_to_its_uid_preserves_healthy_neighbors(full):
    raw = document('SUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=Synthetic-Unknown:=41')
    healthy = '\r\n'.join([
        'BEGIN:VEVENT', 'UID:healthy@example.invalid', 'SUMMARY:Healthy neighbor',
        'DTSTART:20261110T110000Z', 'DTEND:20261110T120000Z', 'END:VEVENT', '',
    ])
    raw = raw.replace('END:VCALENDAR', healthy + 'END:VCALENDAR')
    report = parse_report(raw, 'synthetic', use_full=full)
    accepted, = report.events
    assert accepted.ical_uid == 'healthy@example.invalid' and accepted.title == 'Healthy neighbor'
    rejected, = report.rejected
    assert rejected.uid == 'legacy-text@example.invalid'
    assert 'Unsupported character set' in rejected.reason
    assert 'CHARSET=Synthetic-Unknown' in rejected.raw_ics
    assert 'healthy@example.invalid' not in rejected.raw_ics


@pytest.mark.parametrize('full', [False, True])
def test_decoded_newlines_are_exported_as_text_instead_of_component_boundaries(full):
    raw = document('SUMMARY:Synthetic',
                   'DESCRIPTION;ENCODING=QUOTED-PRINTABLE:Text=0D=0ABEGIN:VEVENT=0D=0AUID:extra')
    item, = parse_report(raw, 'synthetic', use_full=full).events
    exported = export_ics([item])
    assert len(components(exported)) == 1
    restored, = parse_report(exported, 'synthetic', use_full=full).events
    assert restored.description.replace('\r\n', '\n') == 'Text\nBEGIN:VEVENT\nUID:extra'
