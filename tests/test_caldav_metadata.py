"""Retained metadata is preserved unless an exact duplicate is harmless."""
from dataclasses import replace

import pytest

from debacal.ics import export_ics, normalize_caldav_ics, parse_ics
from debacal.ics_fallback import CalendarValueError


def resource(extra=(), headers=()):
    return ('\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic Metadata Test//EN',
        *headers, 'BEGIN:VEVENT', 'UID:synthetic-metadata@example.invalid',
        'DTSTAMP:20261101T120000Z', 'DTSTART:20261110T090000Z',
        'DTEND:20261110T100000Z', 'SUMMARY:Synthetic appointment',
        *extra, 'END:VEVENT', 'END:VCALENDAR', '',
    ])).encode()


@pytest.mark.parametrize('name,first,second', [
    ('ORGANIZER', 'mailto:first@example.invalid', 'mailto:second@example.invalid'),
    ('STATUS', 'CONFIRMED', 'TENTATIVE'), ('SEQUENCE', '1', '2'),
    ('URL', 'https://first.example.invalid/', 'https://second.example.invalid/'),
])
def test_conflicting_retained_singletons_need_review_without_choosing_a_winner(name, first, second):
    raw = resource([name + ':' + first, name + ':' + second])
    event, = parse_ics(raw, 'synthetic')
    exported = export_ics([replace(event, title='Edited synthetic appointment')])
    with pytest.raises(CalendarValueError, match='Conflicting ' + name) as caught:
        normalize_caldav_ics(exported)
    assert 'Review the original resource' in str(caught.value)
    assert first not in str(caught.value) and second not in str(caught.value)
    assert (name + ':' + first).encode() in exported
    assert (name + ':' + second).encode() in exported


@pytest.mark.parametrize('line', [
    'ORGANIZER;CN="Synthetic host":mailto:host@example.invalid',
    'STATUS:CONFIRMED', 'CREATED:20261101T120000Z',
])
def test_exact_duplicates_of_singleton_metadata_are_collapsed_only_for_upload(line):
    raw = resource([line, line])
    result = normalize_caldav_ics(raw)
    assert result.count(line.encode()) == 1
    assert raw.count(line.encode()) == 2
    assert normalize_caldav_ics(result) == result


def test_repeatable_unknown_and_nested_metadata_are_unchanged():
    extra = [
        'ORGANIZER:mailto:host@example.invalid',
        'ATTENDEE:mailto:guest@example.invalid', 'ATTENDEE:mailto:guest@example.invalid',
        'CATEGORIES:One', 'CATEGORIES:Two', 'X-SYNTHETIC:First', 'X-SYNTHETIC:Second',
        'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT15M',
        'DESCRIPTION:Synthetic reminder', 'ORGANIZER:mailto:nested@example.invalid',
        'END:VALARM',
    ]
    raw = resource(extra)
    assert normalize_caldav_ics(raw) == raw


def test_upload_repairs_header_token_whitespace_from_public_icalendar_issue_351():
    # Minimal synthetic equivalent of icalendar's public issue_351 fixture;
    # no upstream event content or personal identifiers are copied.
    header = 'REFRESH - INTERVAL; VALUE = DURATION:P1D'
    raw = resource(headers=[header])
    event, = parse_ics(raw, 'synthetic')
    exported = export_ics([event])
    assert header.encode() in exported
    normalized = normalize_caldav_ics(exported)
    assert b'REFRESH-INTERVAL;VALUE=DURATION:P1D' in normalized
    assert header.encode() not in normalized
    assert normalize_caldav_ics(normalized) == normalized


def test_header_token_repairs_preserve_text_and_quoted_parameter_contents():
    raw = resource(headers=['X - SYNTHETIC; X - SOURCE="Keep : spaces ; and punctuation":Keep  text'])
    normalized = normalize_caldav_ics(raw)
    assert (b'X-SYNTHETIC;X-SOURCE="Keep : spaces ; and punctuation":Keep  text') in normalized
    assert normalize_caldav_ics(normalized) == normalized
