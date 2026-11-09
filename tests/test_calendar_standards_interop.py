"""Interoperability contracts using synthetic RFC 5545 calendar resources.

These exercise the complete import/store/edit/export path rather than the
timezone helper alone: a parser accepting a resource is insufficient if later
serialization silently changes its clocks or loses its recurrence exceptions.
"""
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
import pytest

from debacal.ics import export_ics, parse_ics
from debacal.ics_resources import parse_report
from debacal.models import Calendar
from debacal.recurrence import expand_events
from debacal.store import Store


UTC = timezone.utc
WINDOWS_ID = "W. Europe Standard Time"
WINDOWS_ZONE = """BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""


def document(*events, zones=()):
    parts = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Synthetic Interop Tests//EN",
             *zones, *events, "END:VCALENDAR", ""]
    return "\r\n".join(parts).replace("\r\n", "\n").replace("\n", "\r\n")


def vevent(uid, start, end, *, tzid=None, extra=()):
    parameter = f';TZID="{tzid}"' if tzid else ""
    return "\n".join(["BEGIN:VEVENT", f"UID:{uid}@example.invalid", "DTSTAMP:20260101T000000Z",
                      "SUMMARY:Synthetic appointment", f"DTSTART{parameter}:{start}",
                      f"DTEND{parameter}:{end}", *extra, "END:VEVENT"])


def fixed_zone(identifier, offset):
    text_identifier = identifier.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return "\n".join(["BEGIN:VTIMEZONE", f"TZID:{text_identifier}", "BEGIN:STANDARD",
                      "DTSTART:19700101T000000", f"TZOFFSETFROM:{offset}",
                      f"TZOFFSETTO:{offset}", "END:STANDARD", "END:VTIMEZONE"])


def accepted(data, *, use_full=True, calendar_id="synthetic"):
    report = parse_report(data, calendar_id, use_full=use_full)
    assert not report.issues, [(issue.resource_id, issue.reason) for issue in report.issues]
    assert not report.rejected
    return list(report.events)


def utc_starts(events, start, end):
    return [event.start.astimezone(UTC) for event in expand_events(events, start, end)]


@contextmanager
def viewer_zone(identifier):
    zone = ZoneInfo(identifier)
    with patch("debacal.ics.local_timezone", return_value=zone), patch("debacal.timeutil.local_zone", return_value=zone):
        yield zone


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
@pytest.mark.parametrize("day,expected", [
    ("20260911", datetime(2026, 9, 11, 9, tzinfo=UTC)),
    # This embedded rule differs from historical Europe/Berlin: aliasing the
    # Windows name to Berlin would change this appointment by an hour.
    ("19951001", datetime(1995, 10, 1, 9, tzinfo=UTC)),
])
def test_windows_observances_survive_import_store_edit_and_export(tmp_path, use_full, day, expected):
    raw = document(vevent("windows-clock", day + "T110000", day + "T120000", tzid=WINDOWS_ID),
                   zones=[WINDOWS_ZONE])
    event, = accepted(raw, use_full=use_full)
    assert event.start.astimezone(UTC) == expected
    assert event.end.astimezone(UTC) == expected + timedelta(hours=1)
    assert event.timezone_id == WINDOWS_ID
    path = tmp_path / "synthetic.sqlite"
    with Store(path) as store:
        store.save_calendar(Calendar("synthetic", "Synthetic", "#123456"))
        store.apply("Import synthetic event", [event])
    with Store(path) as store:
        restored, = store.list_events()
        edited = replace(restored, title="Edited synthetic appointment")
        store.apply("Edit synthetic title", [edited])
        stored, = store.list_events()
    exported = export_ics([stored])
    component, = ICalendar.from_ical(exported).walk("VTIMEZONE")
    assert str(component["TZID"]) == WINDOWS_ID
    roundtrip, = accepted(exported, use_full=use_full)
    assert roundtrip.title == edited.title
    assert roundtrip.start.astimezone(UTC) == expected
    assert roundtrip.end.astimezone(UTC) == expected + timedelta(hours=1)


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_custom_timezone_applies_to_recurrence_dates_exclusions_and_detached_instances(use_full):
    master = vevent("windows-series", "20261023T110000", "20261023T120000", tzid=WINDOWS_ID,
                    extra=["RRULE:FREQ=DAILY;COUNT=5",
                           f'EXDATE;TZID="{WINDOWS_ID}":20261024T110000',
                           f'RDATE;TZID="{WINDOWS_ID}":20261028T110000'])
    detached = vevent("windows-series", "20261026T140000", "20261026T150000", tzid=WINDOWS_ID,
                      extra=[f'RECURRENCE-ID;TZID="{WINDOWS_ID}":20261026T110000'])
    events = accepted(document(master, detached, zones=[WINDOWS_ZONE]), use_full=use_full)
    start, end = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    expected = [datetime(2026, 10, day, hour, tzinfo=UTC)
                for day, hour in [(23, 9), (25, 10), (26, 13), (27, 10), (28, 10)]]
    assert utc_starts(events, start, end) == expected
    with Store(":memory:") as store:
        store.save_calendar(Calendar("synthetic", "Synthetic", "#123456"))
        store.apply("Import recurring event", events)
        assert [event.start.astimezone(UTC) for event in store.query_occurrences(start, end)] == expected
        restored = store.list_events()
    assert utc_starts(accepted(export_ics(restored), use_full=use_full), start, end) == expected


@pytest.mark.parametrize("duration,expected_end", [
    ("P1D", datetime(2026, 10, 25, 10, tzinfo=UTC)),
    ("PT24H", datetime(2026, 10, 25, 9, tzinfo=UTC)),
])
def test_custom_observances_preserve_nominal_days_versus_elapsed_hours(duration, expected_end):
    event_text = vevent("custom-duration", "20261024T110000", "20261025T110000", tzid=WINDOWS_ID)
    event_text = event_text.replace(f'DTEND;TZID="{WINDOWS_ID}":20261025T110000', f"DURATION:{duration}")
    event, = accepted(document(event_text, zones=[WINDOWS_ZONE]))
    assert event.start.astimezone(UTC) == datetime(2026, 10, 24, 9, tzinfo=UTC)
    assert event.end.astimezone(UTC) == expected_end
    roundtrip, = accepted(export_ics([replace(event, title="Edited nominal duration")]))
    assert roundtrip.end.astimezone(UTC) == expected_end


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_reused_custom_identifier_is_scoped_to_each_resource_not_parser_history(use_full):
    identifier = "Synthetic/shared-clock"
    calendars = [document(vevent(f"offset-{offset}", "20261110T110000", "20261110T120000", tzid=identifier),
                          zones=[fixed_zone(identifier, offset)]) for offset in ["+0200", "+0500"]]
    expected = [datetime(2026, 11, 10, hour, tzinfo=UTC) for hour in (9, 6)]
    # Alternation exposes both global TZID caches and last-definition-wins
    # resolution. Existing event values must survive parsing the other file.
    first, = accepted(calendars[0], use_full=use_full)
    second, = accepted(calendars[1], use_full=use_full)
    assert [event.start.astimezone(UTC) for event in (first, second)] == expected
    for index in [0, 1, 1, 0]:
        reparsed, = accepted(calendars[index], use_full=use_full)
        assert reparsed.start.astimezone(UTC) == expected[index]
        roundtrip, = accepted(export_ics([first if index == 0 else second]), use_full=use_full)
        assert roundtrip.start.astimezone(UTC) == expected[index]


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_explicit_definition_is_authoritative_even_when_tzid_is_an_iana_name(use_full):
    raw = document(vevent("embedded-iana", "20261110T110000", "20261110T120000", tzid="Europe/Berlin"),
                   zones=[fixed_zone("Europe/Berlin", "+0300")])
    event, = accepted(raw, use_full=use_full)
    expected = datetime(2026, 11, 10, 8, tzinfo=UTC)
    assert event.start.astimezone(UTC) == expected
    roundtrip, = accepted(export_ics([replace(event, title="Edited embedded zone")]), use_full=use_full)
    assert roundtrip.start.astimezone(UTC) == expected


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_quoted_timezone_parameter_can_contain_colons_and_semicolons(use_full):
    identifier = "Synthetic:Office;Clock"
    raw = document(vevent("quoted-clock", "20261110T110000", "20261110T120000", tzid=identifier),
                   zones=[fixed_zone(identifier, "+0230")])
    event, = accepted(raw, use_full=use_full)
    assert event.start.astimezone(UTC) == datetime(2026, 11, 10, 8, 30, tzinfo=UTC)
    roundtrip, = accepted(export_ics([event]), use_full=use_full)
    assert roundtrip.start == event.start


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_component_property_and_parameter_names_are_case_insensitive(use_full):
    raw = document(vevent("CaseSensitive-Uid", "20261110T110000", "20261110T120000", tzid="UTC"))
    raw = (raw.replace("BEGIN:", "begin:").replace("END:", "end:")
           .replace("VCALENDAR", "vcalendar").replace("VEVENT", "vevent")
           .replace("DTSTART", "dtstart").replace("DTEND", "DtEnd").replace("TZID=", "tzid=")
           .replace("SUMMARY:", "summary:").replace("UID:", "uid:"))
    event, = accepted(raw, use_full=use_full)
    assert event.ical_uid == "CaseSensitive-Uid@example.invalid"
    assert event.start == datetime(2026, 11, 10, 11, tzinfo=UTC)
    roundtrip, = accepted(export_ics([replace(event, title="Changed case resource")]), use_full=use_full)
    assert roundtrip.ical_uid == event.ical_uid
    assert roundtrip.title == "Changed case resource"


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_empty_physical_lines_in_events_are_tolerated_consistently(use_full):
    text = vevent("empty-line", "20261110T110000Z", "20261110T120000Z")
    raw = document(text.replace("DTSTAMP:", "\nDTSTAMP:").replace("END:VEVENT", "\nEND:VEVENT"))
    event, = accepted(raw, use_full=use_full)
    assert event.ical_uid == "empty-line@example.invalid"
    roundtrip, = accepted(export_ics([event]), use_full=use_full)
    assert roundtrip.start == event.start and roundtrip.end == event.end


def test_editing_preserves_unicode_folding_unknown_properties_and_alarms():
    raw = document(vevent("preserved-properties", "20261110T110000Z", "20261110T120000Z", extra=[
        "DESCRIPTION:First line\\nCafé\\, semicolon\\; and backslash\\\\",
        "LOCATION:Somewhere\\, étage 2", "X-SYNTHETIC-METADATA:an opaque",
        "  value spanning a folded line", "BEGIN:VALARM", "ACTION:DISPLAY",
        "DESCRIPTION:Synthetic reminder", "TRIGGER:-PT10M", "END:VALARM"]))
    event, = accepted(raw)
    assert event.description == "First line\nCafé, semicolon; and backslash\\"
    assert event.location == "Somewhere, étage 2"
    exported = export_ics([replace(event, title="An edited café appointment " + "ü" * 90)])
    assert all(len(line) <= 75 for line in exported.split(b"\r\n"))
    calendar = ICalendar.from_ical(exported)
    component, = calendar.walk("VEVENT")
    assert str(component["X-SYNTHETIC-METADATA"]) == "an opaque value spanning a folded line"
    alarm, = component.walk("VALARM")
    assert str(alarm["DESCRIPTION"]) == "Synthetic reminder"
    roundtrip, = accepted(exported)
    assert roundtrip.description == event.description
    assert roundtrip.location == event.location


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_start_and_end_may_have_different_timezones(use_full):
    raw = document(vevent("two-clock-endpoints", "20261110T120000", "20261110T150000", tzid="Synthetic/start")
                   .replace('DTEND;TZID="Synthetic/start"', 'DTEND;TZID="Synthetic/end"'),
                   zones=[fixed_zone("Synthetic/start", "+0100"), fixed_zone("Synthetic/end", "+0300")])
    event, = accepted(raw, use_full=use_full)
    assert event.start.astimezone(UTC) == datetime(2026, 11, 10, 11, tzinfo=UTC)
    assert event.end.astimezone(UTC) == datetime(2026, 11, 10, 12, tzinfo=UTC)
    roundtrip, = accepted(export_ics([event]), use_full=use_full)
    assert roundtrip.start.astimezone(UTC) == event.start.astimezone(UTC)
    assert roundtrip.end.astimezone(UTC) == event.end.astimezone(UTC)


def test_all_day_values_keep_calendar_dates_and_exclusive_end_in_another_viewer_zone():
    raw = document("\n".join(["BEGIN:VEVENT", "UID:all-day@example.invalid", "SUMMARY:Synthetic holiday",
                              "DTSTART;VALUE=DATE:20261110", "DTEND;VALUE=DATE:20261112", "END:VEVENT"]),
                   zones=[WINDOWS_ZONE])
    with viewer_zone("Europe/Berlin"):
        event, = accepted(raw)
        exported = export_ics([event])
    with viewer_zone("Pacific/Honolulu"):
        restored, = accepted(exported)
    assert restored.all_day
    assert restored.start.date().isoformat() == "2026-11-10"
    assert restored.end.date().isoformat() == "2026-11-12"
    assert b"DTSTART;VALUE=DATE:20261110" in exported
    assert b"DTEND;VALUE=DATE:20261112" in exported


def test_floating_one_off_keeps_unqualified_wall_clocks_across_import_store_and_export(tmp_path):
    raw = document(vevent("floating-clock", "20261110T110000", "20261110T120000"))
    with viewer_zone("Europe/Berlin"):
        event, = accepted(raw)
    with Store(tmp_path / "floating.sqlite") as store:
        store.save_calendar(Calendar("synthetic", "Synthetic", "#123456"))
        store.apply("Import floating event", [event])
    with viewer_zone("Pacific/Honolulu"):
        with Store(tmp_path / "floating.sqlite") as store:
            stored, = store.list_events()
            exported = export_ics([replace(stored, title="Edited floating event")])
        roundtrip, = accepted(exported)
    calendar = ICalendar.from_ical(exported)
    component, = calendar.walk("VEVENT")
    assert component["DTSTART"].dt == datetime(2026, 11, 10, 11)
    assert component["DTEND"].dt == datetime(2026, 11, 10, 12)
    assert roundtrip.start.replace(tzinfo=None) == datetime(2026, 11, 10, 11)
    assert roundtrip.end.replace(tzinfo=None) == datetime(2026, 11, 10, 12)


def test_floating_recurrence_exdates_and_overrides_share_wall_clock_identity():
    raw = document(vevent("floating-series", "20261023T110000", "20261023T120000", extra=[
        "RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20261024T110000", "RDATE:20261028T110000"]),
        vevent("floating-series", "20261026T140000", "20261026T150000", extra=["RECURRENCE-ID:20261026T110000"]))
    start, end = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    with viewer_zone("Europe/Berlin"):
        events = accepted(raw)
    with viewer_zone("Pacific/Honolulu"):
        roundtrip = accepted(export_ics(events))
        occurrences = expand_events(roundtrip, start, end)
    assert [event.start.replace(tzinfo=None) for event in occurrences] == [
        datetime(2026, 10, day, hour) for day, hour in [(23, 11), (25, 11), (26, 14), (27, 11), (28, 11)]]
    assert all(component["DTSTART"].dt.tzinfo is None for component in ICalendar.from_ical(export_ics(events)).walk("VEVENT"))


@pytest.mark.parametrize("recurring", [False, True], ids=["one-off", "recurring"])
def test_floating_storage_queries_use_viewer_wall_times_not_original_utc_index(tmp_path, recurring):
    path = tmp_path / "viewer-clock.sqlite"
    raw = document(vevent("floating-query", "20261110T110000", "20261110T120000", extra=(
        ["RRULE:FREQ=DAILY;COUNT=2"] if recurring else [])))
    with viewer_zone("Europe/Berlin"):
        event, = accepted(raw)
        with Store(path) as store:
            store.save_calendar(Calendar("synthetic", "Synthetic", "#123456"))
            store.apply("Import floating appointment", [event])
    with viewer_zone("Pacific/Honolulu") as zone:
        with Store(path) as store:
            start = datetime(2026, 11, 10, 11, 30, tzinfo=zone)
            end = datetime(2026, 11, 10, 11, 45, tzinfo=zone)
            occurrence, = store.query_occurrences(start, end)
    assert occurrence.start.replace(tzinfo=None) == datetime(2026, 11, 10, 11)
    assert occurrence.start.utcoffset() == timedelta(hours=-10)


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_empty_optional_summary_does_not_quarantine_an_otherwise_valid_event(use_full):
    raw = document(vevent("empty-summary", "20261110T110000Z", "20261110T120000Z")
                   .replace("SUMMARY:Synthetic appointment", "SUMMARY:"))
    event, = accepted(raw, use_full=use_full)
    assert event.title.strip()
    assert event.start == datetime(2026, 11, 10, 11, tzinfo=UTC)


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_unused_malformed_timezone_cannot_hide_healthy_utc_resources(use_full):
    # A bad source component must not make unrelated healthy events disappear.
    bad_zone = fixed_zone("Synthetic/unused", "+0100").replace("TZOFFSETTO:+0100", "TZOFFSETTO:bad-offset")
    raw = document(vevent("healthy-utc", "20261110T110000Z", "20261110T120000Z"), zones=[bad_zone])
    event, = accepted(raw, use_full=use_full)
    assert event.ical_uid == "healthy-utc@example.invalid"


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_conflicting_timezone_definitions_only_quarantine_resources_that_reference_them(use_full):
    identifier = "Synthetic/conflicting-clock"
    raw = document(vevent("conflicting-clock", "20261110T110000", "20261110T120000", tzid=identifier),
                   vevent("healthy-clock-peer", "20261111T110000Z", "20261111T120000Z"),
                   zones=[fixed_zone(identifier, "+0100"), fixed_zone(identifier, "+0200")])
    report = parse_report(raw, "synthetic", use_full=use_full)
    assert [event.ical_uid for event in report.events] == ["healthy-clock-peer@example.invalid"]
    assert [resource.uid for resource in report.rejected] == ["conflicting-clock@example.invalid"]
    assert len(report.issues) == 1
    assert identifier in report.issues[0].reason
    exported = export_ics(report.events)
    assert not ICalendar.from_ical(exported).walk("VTIMEZONE")
    restored, = accepted(exported, use_full=use_full)
    assert restored.ical_uid == "healthy-clock-peer@example.invalid"


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_unavailable_timezone_diagnostic_names_the_identifier_and_keeps_healthy_peers(use_full):
    identifier = "Synthetic/MissingClock"
    raw = document(vevent("missing-clock", "20261110T110000", "20261110T120000", tzid=identifier),
                   vevent("healthy-missing-peer", "20261111T110000Z", "20261111T120000Z"))
    report = parse_report(raw, "synthetic", use_full=use_full)
    assert [event.ical_uid for event in report.events] == ["healthy-missing-peer@example.invalid"]
    assert [resource.uid for resource in report.rejected] == ["missing-clock@example.invalid"]
    assert len(report.issues) == 1
    assert identifier in report.issues[0].reason
    assert "VTIMEZONE" in report.issues[0].reason


@pytest.mark.parametrize("use_full", [False, True], ids=["lexical", "icalendar"])
def test_period_dates_preserve_their_range_alongside_healthy_resources(use_full):
    raw = document(vevent("period", "20261110T110000Z", "20261110T120000Z", extra=[
        "RDATE;VALUE=PERIOD:20261111T110000Z/20261111T130000Z"]),
        vevent("healthy-peer", "20261112T110000Z", "20261112T120000Z"))
    report = parse_report(raw, "synthetic", use_full=use_full)
    assert len(report.events) == 2 and not report.rejected and not report.issues
    assert any(event.ical_uid == "healthy-peer@example.invalid" for event in report.events)
    start, end = datetime(2026, 11, 11, tzinfo=UTC), datetime(2026, 11, 12, tzinfo=UTC)
    occurrence, = expand_events(report.events, start, end)
    assert occurrence.end == datetime(2026, 11, 11, 13, tzinfo=UTC)
