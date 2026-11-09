"""Timezone interoperability through the real bridge with an offline DAV stand-in."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.ics_resources import RejectedResource
from debacal.models import Event
from debacal.vdirsyncer_backend import semantic
from test_calendar_standards_interop import (WINDOWS_ID, WINDOWS_ZONE, accepted,
                                             document, fixed_zone, vevent, viewer_zone)
from test_vdirsyncer import setup, sync


@pytest.mark.parametrize('floating', [False, True], ids=['explicit-observances', 'floating'])
def test_pull_edit_push_and_second_pull_preserve_timezone_intent(tmp_path, floating):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        raw = document(vevent('transport-clock', '20260911T110000', '20260911T120000',
                               tzid=None if floating else WINDOWS_ID,
                               extra=['RRULE:FREQ=WEEKLY;COUNT=8']),
                       zones=[] if floating else [WINDOWS_ZONE])
        uid = 'transport-clock@example.invalid'
        cli.remote[uid] = raw
        backend._quarantine(store, calendar.id, backend.calendar_path(calendar.id),
                            [RejectedResource(uid, raw, hashlib.sha256(uid.encode()).hexdigest()[:12],
                                              'Unsupported timezone definition.')])
        first = sync(store, calendar, backend)
        assert not first.issues and not first.conflicts
        assert not store.list_quarantined_resources(calendar.id)
        event, = store.list_events()
        assert event.floating_time is floating
        assert event.timezone_id == ('' if floating else WINDOWS_ID)
        edited = replace(event, title='Edited appointment')
        store.apply('Edit appointment', [edited])
        result = sync(store, calendar, backend)
        assert not result.issues and not result.conflicts
        assert not store.pending_changes()
        remote, = parse_ics(cli.remote[uid], calendar.id)
        assert semantic(remote) == semantic(edited)
        again = sync(store, calendar, backend)
        assert not again.conflicts and not again.issues
        assert semantic(store.list_events()[0]) == semantic(edited)
    finally:
        store.close()


def test_timezone_definition_formatting_does_not_create_a_false_sync_conflict():
    raw = document(vevent('format-clock', '20261110T110000', '20261110T120000', tzid=WINDOWS_ID,
                          extra=['RRULE:FREQ=WEEKLY;COUNT=26']),
                   zones=[WINDOWS_ZONE])
    before, = accepted(raw)
    after, = accepted(raw.replace('BEGIN:VTIMEZONE', 'BEGIN:VTIMEZONE\nCOMMENT:Provider metadata')
                          .replace('FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10',
                                   'BYMONTH=10;BYDAY=-1SU;INTERVAL=1;FREQ=YEARLY'))
    assert semantic(before) == semantic(after)
    changed, = accepted(raw.replace('TZOFFSETTO:+0200', 'TZOFFSETTO:+0300'))
    assert semantic(before) != semantic(changed)


def test_floating_sync_comparison_is_independent_of_viewer_timezone():
    with viewer_zone('Europe/Berlin'):
        event, = accepted(document(vevent('wall-clock', '20261110T110000', '20261110T120000')))
        value, payload = semantic(event), event.to_dict()
    with viewer_zone('Pacific/Honolulu'):
        restored = Event.from_dict(payload)
        assert semantic(restored) == value
        exported, = accepted(export_ics([restored]))
        assert semantic(exported) == value


def test_custom_zone_copy_without_raw_template_retains_rules():
    event, = accepted(document(vevent('copy-clock', '20260911T110000', '20260911T120000', tzid=WINDOWS_ID),
                               zones=[WINDOWS_ZONE]))
    copied = replace(event, id='new-copy', ical_uid='new-copy', raw_ics='', start=event.start + timedelta(days=1),
                     end=event.end + timedelta(days=1))
    restored, = accepted(export_ics([copied]))
    assert restored.timezone_definitions == copied.timezone_definitions
    assert restored.start.astimezone(timezone.utc) == datetime(2026, 9, 12, 9, tzinfo=timezone.utc)


def test_explicit_utc_named_zone_is_not_silently_exported_as_database_utc():
    event, = accepted(document(vevent('custom-utc', '20261110T110000', '20261110T120000', tzid='UTC'),
                               zones=[fixed_zone('UTC', '+0300')]))
    restored, = accepted(export_ics([event]))
    assert restored.timezone_definitions == event.timezone_definitions
    assert restored.start.astimezone(timezone.utc) == datetime(2026, 11, 10, 8, tzinfo=timezone.utc)


def test_standalone_second_repeated_hour_exports_an_unambiguous_instant():
    event, = accepted(document(vevent('late-fold', '20261025T023000', '20261025T033000', tzid=WINDOWS_ID),
                               zones=[WINDOWS_ZONE]))
    late = event.start.replace(fold=1)
    selected = replace(event, start=late, end=event.end)
    restored, = accepted(export_ics([selected]))
    assert restored.start.timestamp() == late.timestamp()
    assert semantic(restored) == semantic(selected)
