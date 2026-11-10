"""Calendar-storage normalization must not erase imported event metadata."""
from dataclasses import replace

import pytest

from debacal.ics import export_ics, normalize_caldav_ics, parse_ics, patch_ics


def invitation(method="METHOD:REQUEST"):
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Synthetic DAV Test//EN",
        method, "X-WR-CALNAME:Synthetic imported calendar", "CALSCALE:GREGORIAN",
        "BEGIN:VTIMEZONE", "TZID:Synthetic/Offset", "BEGIN:STANDARD",
        "DTSTART:19700101T000000", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0100",
        "END:STANDARD", "END:VTIMEZONE",
        "BEGIN:VEVENT", "UID:synthetic-invitation@example.invalid",
        "DTSTAMP:20260101T000000Z", "SUMMARY:Synthetic invitation",
        "DTSTART;TZID=Synthetic/Offset:20261110T100000",
        "DTEND;TZID=Synthetic/Offset:20261110T110000", "RRULE:FREQ=WEEKLY;COUNT=2",
        "ORGANIZER:mailto:host@example.invalid",
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:guest@example.invalid",
        "X-SYNTHETIC-METADATA:Retained", "DESCRIPTION:METHOD:REQUEST",
        "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M",
        "DESCRIPTION:METHOD:REQUEST", "X-SYNTHETIC-ALARM:Retained",
        "END:VALARM", "END:VEVENT",
        "BEGIN:VEVENT", "UID:synthetic-invitation@example.invalid",
        "RECURRENCE-ID;TZID=Synthetic/Offset:20261117T100000",
        "DTSTAMP:20260101T000000Z", "SUMMARY:Moved synthetic invitation",
        "DTSTART;TZID=Synthetic/Offset:20261117T120000",
        "DTEND;TZID=Synthetic/Offset:20261117T130000",
        "X-SYNTHETIC-EXCEPTION:Retained", "END:VEVENT", "END:VCALENDAR", "",
    ]).encode()


@pytest.mark.parametrize("method", ["METHOD:REQUEST", "method:PUBLISH",
                                    "METHOD;X-SOURCE=import:REQUEST"])
@pytest.mark.parametrize("as_text", [False, True])
def test_caldav_removes_only_calendar_method_and_preserves_complete_resource(method, as_text):
    raw = invitation(method)
    result = normalize_caldav_ics(raw.decode() if as_text else raw)
    # Compare the full payload: attendees, alarms, scoped zones, detached
    # exceptions and unknown properties must survive this transport change.
    assert result == raw.replace((method + "\r\n").encode(), b"", 1)
    assert normalize_caldav_ics(result) == result
    original, = parse_ics(raw, "synthetic")
    stored, = parse_ics(result, "synthetic")
    assert replace(original, raw_ics="").to_dict() == replace(stored, raw_ics="").to_dict()


def test_caldav_normalization_keeps_nested_method_properties():
    raw = invitation().replace(b"BEGIN:VALARM\r\n", b"BEGIN:VALARM\r\nMETHOD:Nested value\r\n")
    result = normalize_caldav_ics(raw)
    assert b"\r\nMETHOD:REQUEST\r\n" not in result
    assert b"BEGIN:VALARM\r\nMETHOD:Nested value\r\n" in result


def test_normalization_leaves_storage_resources_byte_for_byte_unchanged():
    raw = invitation().replace(b"METHOD:REQUEST\r\n", b"", 1)
    assert normalize_caldav_ics(raw) == raw


def test_file_export_and_patch_keep_invitation_method():
    raw = invitation()
    event, = parse_ics(raw, "synthetic")
    edited = replace(event, title="Edited synthetic invitation")
    for result in (export_ics([event]), patch_ics(raw, edited)):
        assert b"\r\nMETHOD:REQUEST\r\n" in result
        assert b"\r\nMETHOD:REQUEST\r\n" not in normalize_caldav_ics(result)
