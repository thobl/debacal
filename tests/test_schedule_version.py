"""Version authoring metadata without changing standard calendar timestamps."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from debacal.ics import export_ics, parse_ics, parse_ics_report, patch_ics
from debacal.ics_fallback import parse as parse_fallback
from debacal.models import Event
from debacal.recurrence import expand_events
from debacal.vdirsyncer_backend import semantic


UTC = timezone.utc


def event(**values):
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    return Event("authored", "local", "Authored schedule", start, start + timedelta(hours=1), **values)


def test_old_json_records_default_to_legacy_syntax_without_migration():
    original = event(schedule_expression="10.11., 12.11.", expression_anchor="2026-11-10")
    old_record = original.to_dict()
    old_record.pop("schedule_syntax")
    restored = Event.from_dict(old_record)
    assert restored.schedule_syntax == 1
    assert restored.schedule_expression == original.schedule_expression
    assert restored.start == original.start
    assert restored.expression_anchor == "2026-11-10"
    current = replace(restored, schedule_syntax=2)
    assert Event.from_dict(current.to_dict()).schedule_syntax == 2


@pytest.mark.parametrize("version", [0, 3, -1, "2", 2.0, True, None])
def test_only_integer_supported_syntax_versions_are_accepted(version):
    with pytest.raises(ValueError, match="syntax version"):
        event(schedule_syntax=version)


@pytest.mark.parametrize("parse", [parse_ics, parse_fallback])
def test_old_ics_authoring_metadata_without_version_remains_legacy(parse):
    raw = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:legacy\r\nSUMMARY:Legacy source\r\n"
           "DTSTART:20261110T090000Z\r\nDTEND:20261110T100000Z\r\nRDATE:20261112T090000Z\r\n"
           "X-DEBACAL-PATTERN:10.11.\\, 12.11.\r\nX-DEBACAL-PATTERN-ANCHOR:2026-11-10\r\n"
           "END:VEVENT\r\nEND:VCALENDAR\r\n")
    restored = parse(raw, "local")[0]
    assert restored.schedule_syntax == 1
    assert restored.schedule_expression == "10.11., 12.11."
    assert restored.start.hour == 9
    assert restored.expression_anchor == "2026-11-10"


@pytest.mark.parametrize("parse", [parse_ics, parse_fallback])
def test_new_syntax_multiple_times_roundtrip_as_standard_rdates(parse):
    original = event(
        schedule_syntax=2, schedule_expression="11:00 +1d", expression_anchor="2026-11-10",
        recurrence_dates=("2026-11-10T11:00:00+00:00", "2026-11-11T11:00:00+00:00"),
    )
    from debacal.patterns import expand_sequence
    assert tuple(value.isoformat() for value in expand_sequence(original.schedule_expression, original.start)[1:]) == original.recurrence_dates
    data = export_ics([original])
    assert b"X-DEBACAL-PATTERN-VERSION:2\r\n" in data
    assert b"RDATE:20261110T110000Z\r\n" in data
    assert b"RDATE:20261111T110000Z\r\n" in data
    restored = parse(data, "local")[0]
    assert restored.schedule_syntax == 2
    assert restored.schedule_expression == original.schedule_expression
    assert restored.expression_anchor == original.expression_anchor
    assert restored.start == original.start
    assert restored.recurrence_dates == original.recurrence_dates
    instances = expand_events([restored], datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 12, tzinfo=UTC))
    assert [item.start.hour for item in instances] == [9, 11, 11]
    assert semantic(original) == semantic(restored)


def test_patch_replaces_old_version_and_preserves_unknown_server_metadata():
    raw = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:server-uid\r\nSUMMARY:Imported\r\n"
           "DTSTART:20261110T090000Z\r\nDTEND:20261110T100000Z\r\nRDATE:20261112T090000Z\r\n"
           "X-REMOTE-UNKNOWN:leave this untouched\r\nX-DEBACAL-PATTERN:10.11.\\, 12.11.\r\n"
           "X-DEBACAL-PATTERN-ANCHOR:2026-11-10\r\nX-DEBACAL-PATTERN-VERSION:1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    imported = parse_ics(raw, "local")[0]
    edited = replace(imported, schedule_expression="11:00 +1d", schedule_syntax=2,
                     recurrence_dates=("2026-11-10T11:00:00+00:00", "2026-11-11T11:00:00+00:00"))
    patched = patch_ics(raw, edited)
    assert b"UID:server-uid\r\n" in patched
    assert b"X-REMOTE-UNKNOWN:leave this untouched\r\n" in patched
    assert patched.count(b"X-DEBACAL-PATTERN-VERSION:") == 1
    assert b"X-DEBACAL-PATTERN-VERSION:2\r\n" in patched
    restored = parse_ics(patched, "local")[0]
    assert semantic(restored) == semantic(edited)


def test_inactive_version_is_not_exported_and_does_not_create_sync_conflicts():
    original = event(schedule_syntax=2)
    data = export_ics([original])
    assert b"X-DEBACAL-PATTERN-VERSION" not in data
    restored = parse_ics(data, "local")[0]
    assert restored.schedule_syntax == 1
    assert semantic(restored) == semantic(original)
    active = replace(original, schedule_expression="11:00", expression_anchor="2026-11-10")
    assert semantic(active) != semantic(replace(active, schedule_syntax=1))


def test_unknown_ics_syntax_version_is_isolated_and_retained_for_review():
    raw = export_ics([event(schedule_expression="11:00", expression_anchor="2026-11-10", schedule_syntax=2)])
    raw = raw.replace(b"X-DEBACAL-PATTERN-VERSION:2", b"X-DEBACAL-PATTERN-VERSION:3")
    report = parse_ics_report(raw, "local")
    assert not report.events
    assert len(report.rejected) == 1
    assert report.issues[0].reason == "Unsupported custom schedule version."
    assert "X-DEBACAL-PATTERN-VERSION:3" in report.rejected[0].raw_ics


def test_version_two_metadata_survives_outbox_upload_and_fresh_remote_pull(tmp_path):
    from test_vdirsyncer import setup, sync
    store, calendar, cli, backend = setup(tmp_path)
    authored = replace(event(schedule_syntax=2, schedule_expression="11:00 +1d", expression_anchor="2026-11-10",
                             recurrence_dates=("2026-11-10T11:00:00+00:00", "2026-11-11T11:00:00+00:00")),
                       calendar_id=calendar.id)
    store.apply("Create versioned custom schedule", [authored])
    assert sync(store, calendar, backend).sent == 1
    returned = parse_ics(cli.remote[authored.ical_uid], calendar.id)[0]
    assert returned.schedule_syntax == 2
    assert returned.recurrence_dates == authored.recurrence_dates
    assert semantic(returned) == semantic(authored)
    sync(store, calendar, backend)
    assert store.get_event(authored.id).schedule_syntax == 2
    assert not store.pending_changes()


@pytest.mark.parametrize("parse", [parse_ics, parse_fallback])
def test_version_two_irregular_rdates_preserve_authoring_zone_across_dst(parse):
    from zoneinfo import ZoneInfo
    from debacal.patterns import expand_sequence
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 10, 20, 9, tzinfo=zone)
    expression = "+5d 11:00-2.11.2026."
    expected = expand_sequence(expression, start)
    original = Event("dst-authored", "local", "DST authoring", start, start + timedelta(hours=1),
                     recurrence_dates=tuple(value.isoformat() for value in expected[1:]),
                     schedule_expression=expression, expression_anchor="2026-10-20", schedule_syntax=2)
    data = export_ics([original])
    assert b"DTSTART;TZID=Europe/Berlin:20261020T090000" in data
    assert b"BEGIN:VTIMEZONE" in data
    assert b"RDATE:20261025T080000Z" in data
    restored = parse(data, "local")[0]
    assert restored.timezone_id == "Europe/Berlin"
    assert restored.recurrence_dates == original.recurrence_dates
    assert [value.timestamp() for value in expand_sequence(restored.schedule_expression, restored.start)] == [value.timestamp() for value in expected]
    from debacal.recurrence import updated_series
    shifted = updated_series(restored, restored, replace(restored, start=restored.start + timedelta(days=1),
                                                        end=restored.end + timedelta(days=1)))
    observed = expand_events([shifted], datetime(2026, 10, 1, tzinfo=zone), datetime(2026, 12, 1, tzinfo=zone))
    assert [value.start.hour for value in observed] == [9, 9, 11]
    assert semantic(restored) == semantic(original)
    clock_zone_lost = replace(original, start=original.start.astimezone(UTC), end=original.end.astimezone(UTC), timezone_id="")
    assert semantic(clock_zone_lost) != semantic(original)
