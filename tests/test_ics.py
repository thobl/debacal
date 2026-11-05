from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lebacal.ics import export_ics, parse_ics, patch_ics, has_recurrence
from lebacal.models import Event


UTC = timezone.utc

BERLIN_TIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19961027T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19960331T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
"""


def semantic(event):
    return {key: value for key, value in event.to_dict().items() if key != "raw_ics"}


def test_unicode_escaped_round_trip_and_stable_id():
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    original = Event("event-1", "local", "Research, café; " + "ü" * 100, start, start + timedelta(hours=1),
                     description="Line one\nLine two\\tail", location="Office, 2", group_id="group", candidate=True)
    data = export_ics([original])
    assert all(len(line) <= 75 for line in data.split(b"\r\n"))
    assert semantic(parse_ics(data, "local")[0]) == semantic(original)
    assert parse_ics(data, "other")[0].id != original.id


def test_all_day_exclusive_end():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:holiday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261112
END:VEVENT
END:VCALENDAR
"""
    event = parse_ics(raw, "local")[0]
    assert event.all_day
    assert (event.end.date() - event.start.date()).days == 2
    assert semantic(parse_ics(export_ics([event]), "local")[0]) == semantic(event)


def test_timezone_and_recurrence_exclusion_override():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
SUMMARY:Class
DTSTART;TZID=Europe/Berlin:20261020T090000
DTEND;TZID=Europe/Berlin:20261020T100000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/Berlin:20261027T090000
END:VEVENT
BEGIN:VEVENT
UID:series
RECURRENCE-ID;TZID=Europe/Berlin:20261103T090000
SUMMARY:Moved class
DTSTART;TZID=Europe/Berlin:20261103T110000
DTEND;TZID=Europe/Berlin:20261103T120000
END:VEVENT
END:VCALENDAR
"""
    raw = raw.replace("VERSION:2.0\n", "VERSION:2.0\n" + BERLIN_TIMEZONE)
    events = parse_ics(raw, "local", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert len(events) == 1
    assert events[0].recurrence_rule == "FREQ=WEEKLY;COUNT=4"
    assert len(events[0].excluded_dates) == 1
    assert len(events[0].recurrence_overrides) == 1
    from lebacal.recurrence import expand_events
    occurrences = expand_events(events, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert [event.start.hour for event in occurrences] == [9, 11, 9]
    assert [event.start.utcoffset() for event in occurrences] == [timedelta(hours=2), timedelta(hours=1), timedelta(hours=1)]
    assert not any(e.read_only for e in events)
    exported = export_ics(events)
    assert b"RRULE:FREQ=WEEKLY;COUNT=4" in exported
    assert b"RECURRENCE-ID:" in exported
    assert len(parse_ics(exported, "local")) == 1
    assert len(expand_events(parse_ics(exported, "local"), datetime(2026, 10, 1, tzinfo=UTC),
                             datetime(2026, 12, 1, tzinfo=UTC))) == 3
    assert has_recurrence(raw)


def test_patch_preserves_unknown_properties_and_remote_uid():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:external-id
SUMMARY:Original
DTSTART:20261110T090000Z
DTEND:20261110T100000Z
X-EXAMPLE:must stay
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder
TRIGGER:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""
    original = parse_ics(raw, "local")[0]
    from dataclasses import replace
    patched = patch_ics(raw, replace(original, title="New", description="Edited"))
    assert b"UID:external-id" in patched
    assert b"X-EXAMPLE:must stay" in patched
    assert b"DESCRIPTION:Reminder" in patched
    assert parse_ics(patched, "local")[0].title == "New"


def test_limit_and_malformed_file():
    with pytest.raises(ValueError):
        parse_ics("garbage", "local")
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
SUMMARY:Class
DTSTART:20261101T090000Z
DTEND:20261101T100000Z
RRULE:FREQ=DAILY;COUNT=20
END:VEVENT
END:VCALENDAR
"""
    assert len(parse_ics(raw, "local", limit=1)) == 1  # Rules are preserved, never truncated.
    with pytest.raises(ValueError, match="exceeds"):
        parse_ics(raw, "local", limit=0)


def test_external_extension_text_is_escaped_in_offline_export():
    from lebacal import ics_fallback
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    original = Event("id,;\\\n", "calendar,;\\\n", "Title", start, start + timedelta(hours=1),
                     group_id="external,;\\\ngroup")
    data = ics_fallback.export([original])
    assert len(ics_fallback.components(data)) == 1
    assert semantic(ics_fallback.parse(data, original.calendar_id)[0]) == semantic(original)


def test_recurrence_identity_is_independent_of_timezone_representation():
    from lebacal.ics import occurrence_key
    from datetime import date
    local = datetime(2026, 11, 10, 9, tzinfo=ZoneInfo("Europe/Berlin"))
    assert occurrence_key(local) == occurrence_key(local.astimezone(UTC))
    assert occurrence_key(date(2026, 11, 10)) == "2026-11-10"


def test_full_and_offline_backends_have_identical_recurring_ids():
    from lebacal import ics, ics_fallback
    if ics.ICalendar is None:
        pytest.skip("Full icalendar backend is not installed in this offline environment")
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:allday-series
SUMMARY:All day
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261111
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:timed-series
SUMMARY:Timed
DTSTART;TZID=Europe/Berlin:20261020T090000
DTEND;TZID=Europe/Berlin:20261020T100000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/Berlin:20261027T090000
END:VEVENT
BEGIN:VEVENT
UID:timed-series
RECURRENCE-ID;TZID=Europe/Berlin:20261103T090000
SUMMARY:Moved
DTSTART;TZID=Europe/Berlin:20261103T110000
DTEND;TZID=Europe/Berlin:20261103T120000
END:VEVENT
END:VCALENDAR
"""
    raw = raw.replace("VERSION:2.0\n", "VERSION:2.0\n" + BERLIN_TIMEZONE)
    args = (raw, "local", datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    full = ics.parse_ics(*args)
    offline = ics_fallback.parse(*args)
    assert {e.id for e in full} == {e.id for e in offline}
    assert [e.recurrence_overrides.keys() for e in full] == [e.recurrence_overrides.keys() for e in offline]


def test_all_day_until_stays_date_on_wire_and_expands_inclusively():
    from lebacal.recurrence import expand_events
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:allday-until
SUMMARY:All day
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261111
RRULE:FREQ=DAILY;UNTIL=20261112
END:VEVENT
END:VCALENDAR
"""
    events = parse_ics(raw, "local")
    assert len(events) == 1
    assert events[0].recurrence_rule == "FREQ=DAILY;UNTIL=20261112"
    assert len(expand_events(events, datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC))) == 3
    exported = export_ics(events)
    assert b"UNTIL=20261112" in exported
    assert b"UNTIL=20261112T" not in exported
    from dataclasses import replace
    ui_rule = replace(events[0], recurrence_rule="FREQ=DAILY;UNTIL=20261112T225959Z")
    assert b"UNTIL=20261112\r\n" in export_ics([ui_rule])


def test_detached_edit_and_exclusion_roundtrip_preserve_unknown_metadata():
    from dataclasses import replace
    from lebacal.recurrence import expand_events
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:keep-the-series
SUMMARY:Series
DTSTART;TZID=Europe/Berlin:20261020T090000
DTEND;TZID=Europe/Berlin:20261020T100000
RRULE:FREQ=WEEKLY;COUNT=4
X-MASTER:preserved
END:VEVENT
BEGIN:VEVENT
UID:keep-the-series
RECURRENCE-ID;TZID=Europe/Berlin:20261027T090000
SUMMARY:Moved
DTSTART;TZID=Europe/Berlin:20261027T110000
DTEND;TZID=Europe/Berlin:20261027T120000
X-OVERRIDE:also preserved
END:VEVENT
END:VCALENDAR
"""
    raw = raw.replace("VERSION:2.0\n", "VERSION:2.0\n" + BERLIN_TIMEZONE)
    master = parse_ics(raw, "local")[0]
    key = next(iter(master.recurrence_overrides))
    detached = Event.from_dict(master.recurrence_overrides[key])
    edited = replace(master, recurrence_overrides={key: replace(detached, title="Edited exception").to_dict()},
                     excluded_dates=("2026-11-03T08:00:00+00:00",))
    data = patch_ics(raw, edited)
    assert b"X-MASTER:preserved" in data
    assert b"X-OVERRIDE:also preserved" in data
    assert data.count(b"UID:keep-the-series") == 2
    assert data.count(b"RECURRENCE-ID:") == 1
    imported = parse_ics(data, "local")
    assert len(imported) == 1
    occurrences = expand_events(imported, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert len(occurrences) == 3
    assert occurrences[1].title == "Edited exception"
    assert occurrences[1].start.hour == 11


def test_large_infinite_series_is_not_flattened_or_truncated_on_import():
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:forever
SUMMARY:Forever
DTSTART:20000101T090000Z
DTEND:20000101T100000Z
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""
    imported = parse_ics(raw, "local")
    assert len(imported) == 1
    assert imported[0].recurrence_rule == "FREQ=DAILY"
    assert b"RRULE:FREQ=DAILY" in export_ics(imported)


def test_named_zone_rule_export_fails_clearly_if_timezone_generator_is_missing(monkeypatch):
    import sys
    from lebacal.ics_resources import _timezone_definition
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 11, 10, 9, tzinfo=zone)
    event = Event("named", "local", "Named zone", start, start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;COUNT=5")
    _timezone_definition.cache_clear()
    monkeypatch.setitem(sys.modules, "icalendar", None)
    with pytest.raises(ValueError, match="generate VTIMEZONE"):
        export_ics([event])
    from dataclasses import replace
    nonrecurring = replace(event, recurrence_rule="")
    assert b"DTSTART:20261110T080000Z" in export_ics([nonrecurring])
    explicit = replace(nonrecurring, recurrence_dates=("2026-11-17T08:00:00+00:00",))
    assert b"TZID=" not in export_ics([explicit])


def test_installed_timezone_generator_includes_vtimezone_for_new_dst_series():
    from lebacal import ics
    if ics.ICalendar is None:
        pytest.skip("The maintained icalendar timezone generator is unavailable offline")
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 20, 9, tzinfo=zone)
    event = Event("named", "local", "DST weekly", start, start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;COUNT=5")
    exported = export_ics([event])
    assert b"BEGIN:VTIMEZONE" in exported
    assert b"TZID:Europe/Berlin" in exported
    assert b"DTSTART;TZID=Europe/Berlin:20261020T090000" in exported


def test_floating_until_uses_dtstart_zone_and_preserves_inclusive_boundary():
    from lebacal.recurrence import expand_events
    raw = ("BEGIN:VCALENDAR\nVERSION:2.0\n" + BERLIN_TIMEZONE +
           "BEGIN:VEVENT\nUID:naive-until\nSUMMARY:Boundary\nDTSTART;TZID=Europe/Berlin:20261020T090000\n"
           "DTEND;TZID=Europe/Berlin:20261020T100000\nRRULE:FREQ=WEEKLY;UNTIL=20261103T090000\nEND:VEVENT\nEND:VCALENDAR\n")
    event = parse_ics(raw, "local")[0]
    assert "UNTIL=20261103T080000Z" in event.recurrence_rule
    occurrences = expand_events([event], datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert len(occurrences) == 3
    assert all(item.start.hour == 9 for item in occurrences)
    assert b"UNTIL=20261103T080000Z" in export_ics([event])


def test_report_isolates_invalid_uid_and_keeps_private_text_out_of_diagnostics():
    from lebacal.ics import parse_ics_report
    raw = ("BEGIN:VCALENDAR\nVERSION:2.0\n"
           "BEGIN:VEVENT\nUID:good\nSUMMARY:Healthy\nDTSTART:20261110T090000Z\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:bad\nSUMMARY:Do not disclose this title\nDTSTART:20261110T090000Z\nRRULE:FREQ=DAILY;UNTIL=garbage\nEND:VEVENT\nEND:VCALENDAR\n")
    report = parse_ics_report(raw, "local")
    assert len(report.events) == 1
    assert report.events[0].ical_uid == "good"
    assert len(report.rejected) == 1 and report.rejected[0].uid == "bad"
    assert "Do not disclose" not in report.issues[0].reason
    assert len(report.issues[0].resource_id) == 12


def test_report_preserves_truncated_resource_instead_of_empty_success():
    from lebacal.ics import parse_ics_report
    raw = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:broken\nDTSTART:20261110T090000Z\n"
           "BEGIN:VALARM\nACTION:DISPLAY\nEND:VCALENDAR\n")
    report = parse_ics_report(raw, "local")
    assert not report.events
    assert len(report.issues) == len(report.rejected) == 1
    assert report.rejected[0].uid == "broken"
    assert report.rejected[0].raw_ics == raw


def test_nested_alarm_uid_never_replaces_missing_event_uid():
    from lebacal.ics import parse_ics_report
    from lebacal.vdirsyncer_backend import raw_uid
    raw = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nDTSTART:20261110T090000Z\n"
           "DTEND:20261110T100000Z\nBEGIN:VALARM\nUID:alarm-only\nACTION:DISPLAY\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR\n")
    report = parse_ics_report(raw, "local")
    assert report.issues and not report.events
    assert report.rejected[0].uid == ""
    assert raw_uid(raw) == ""


@pytest.mark.parametrize('use_full', [False, True])
@pytest.mark.parametrize('ending', ['', 'DTEND:20261110T100000Z\n', 'DURATION:PT0S\n'])
def test_imported_timed_points_preserve_their_instant(use_full, ending):
    """Accept valid missing-end points and tolerate servers' explicit zero ends."""
    from lebacal import ics_resources
    raw = ('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:point@example.invalid\n'
           'SUMMARY:Point\nDTSTART:20261110T100000Z\n' + ending +
           'RRULE:FREQ=WEEKLY;COUNT=2\nEND:VEVENT\nEND:VCALENDAR\n')
    event = ics_resources.parse(raw, 'local', use_full=use_full)[0]
    assert event.start == event.end
    assert event.start == datetime(2026, 11, 10, 10, tzinfo=UTC)
    exported = export_ics([event])
    assert b'DTEND' not in exported
    assert b'DURATION' not in exported
    imported = parse_ics(exported, 'local')[0]
    assert imported.start == imported.end == event.start
    assert imported.recurrence_rule == event.recurrence_rule


@pytest.mark.parametrize('use_full', [False, True])
def test_named_timezone_point_series_matches_reported_sync_failure(use_full):
    from lebacal import ics_resources
    from lebacal.recurrence import expand_events
    raw = ('BEGIN:VCALENDAR\nVERSION:2.0\n' + BERLIN_TIMEZONE +
           'BEGIN:VEVENT\nUID:zero-duration-test@example.invalid\nSUMMARY:Point\n'
           'DTSTART;TZID=Europe/Berlin:20261020T100000\n'
           'DTEND;TZID=Europe/Berlin:20261020T100000\n'
           'RRULE:FREQ=WEEKLY;COUNT=2\nEND:VEVENT\nEND:VCALENDAR\n')
    event = ics_resources.parse(raw, 'local', use_full=use_full)[0]
    data = export_ics([event])
    # The timezone definition has its own DTSTART properties; only the event
    # must omit an equal DTEND and keep the TZID recurrence wall time.
    event_component = data.split(b'BEGIN:VEVENT', 1)[1].split(b'END:VEVENT', 1)[0]
    assert b'DTSTART;TZID=Europe/Berlin:' in event_component
    assert b'DTEND' not in event_component
    occurrences = expand_events([parse_ics(data, 'local')[0]],
                               datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert len(occurrences) == 2
    assert all(item.start == item.end and item.start.hour == 10 for item in occurrences)
    assert [item.start.utcoffset() for item in occurrences] == [timedelta(hours=2), timedelta(hours=1)]


def test_detached_override_zero_duration_is_not_replaced_with_master_duration():
    raw = ('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:point-override\n'
           'SUMMARY:Class\nDTSTART:20261110T100000Z\nDTEND:20261110T110000Z\n'
           'RRULE:FREQ=WEEKLY;COUNT=2\nEND:VEVENT\nBEGIN:VEVENT\nUID:point-override\n'
           'RECURRENCE-ID:20261117T100000Z\nDTSTART:20261117T120000Z\n'
           'DURATION:PT0S\nEND:VEVENT\nEND:VCALENDAR\n')
    master = parse_ics(raw, 'local')[0]
    override = Event.from_dict(next(iter(master.recurrence_overrides.values())))
    assert override.start == override.end
    data = export_ics([master])
    round_tripped = Event.from_dict(next(iter(parse_ics(data, 'local')[0].recurrence_overrides.values())))
    assert round_tripped.start == round_tripped.end == override.start


def test_negative_remote_duration_stays_an_error():
    raw = ('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:bad-end\n'
           'SUMMARY:Invalid\nDTSTART:20261110T100000Z\nDTEND:20261110T090000Z\n'
           'END:VEVENT\nEND:VCALENDAR\n')
    with pytest.raises(ValueError, match='before'):
        parse_ics(raw, 'local')
