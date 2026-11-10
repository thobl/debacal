"""Explicit observances preserve UTC instants even for negative daylight time."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from debacal.ics import export_ics
from debacal.ics_resources import parse_report
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.timezones import resolve_timezone
from debacal.vdirsyncer_backend import semantic
from test_calendar_standards_interop import document, vevent


UTC = timezone.utc
IDENTIFIER = 'Synthetic/NegativeDST'
DEFINITION = '''BEGIN:VTIMEZONE
TZID:Synthetic/NegativeDST
BEGIN:STANDARD
DTSTART:20240101T000000
TZOFFSETFROM:+1000
TZOFFSETTO:+1200
TZNAME:Winter
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:20240505T080000
TZOFFSETFROM:+1200
TZOFFSETTO:+1000
TZNAME:Summer
END:DAYLIGHT
END:VTIMEZONE'''


@pytest.mark.parametrize('hours,minutes,fold', [(18, 0, 0), (19, 0, 0), (19, 59, 0),
                                             (20, 0, 1), (21, 0, 1), (21, 59, 1), (22, 0, 0)])
def test_negative_dst_utc_local_utc_identity_and_both_repeated_hour_phases(hours, minutes, fold):
    zone = resolve_timezone(IDENTIFIER, {IDENTIFIER: DEFINITION})
    instant = datetime(2024, 5, 4, hours, minutes, 1, 123456, tzinfo=UTC)
    local = instant.astimezone(zone)
    assert local.fold == fold
    assert local.astimezone(UTC) == instant
    assert local.utcoffset() == timedelta(hours=12 if hours < 20 else 10)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_negative_dst_ambiguous_import_survives_persistence_index_edit_and_export(full):
    raw = document(vevent('negative-dst', '20240505T070001', '20240505T073001', tzid=IDENTIFIER),
                   zones=[DEFINITION])
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.issues
    original, = report.events
    expected = datetime(2024, 5, 4, 19, 0, 1, tzinfo=UTC)
    assert original.start.astimezone(UTC) == expected
    restored = Event.from_dict(original.to_dict())
    assert restored.start.timestamp() == original.start.timestamp()
    assert restored.end.timestamp() == original.end.timestamp()
    assert restored.start.fold == 0
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.import_report(report, calendar_id='synthetic')
        persisted, = store.list_events()
        assert semantic(persisted) == semantic(original)
        shown, = store.query_occurrences(expected - timedelta(hours=1), expected + timedelta(hours=1))
        assert shown.start.timestamp() == expected.timestamp()
        edited = replace(persisted, title='Edited negative daylight event')
        store.apply('Edit imported event', [edited])
        exported = export_ics(store.list_events())
    after = parse_report(exported, 'synthetic', use_full=full)
    assert not after.issues
    final, = after.events
    assert semantic(final) == semantic(edited)


@pytest.mark.parametrize('fold,hour', [(0, 19), (1, 21)])
def test_negative_dst_saved_second_phase_preserves_its_fold(fold, hour):
    zone = resolve_timezone(IDENTIFIER, {IDENTIFIER: DEFINITION})
    start = datetime(2024, 5, 5, 7, 0, 1, tzinfo=zone, fold=fold)
    end = (start.astimezone(UTC) + timedelta(minutes=15)).astimezone(zone)
    original = Event('negative-fold', 'synthetic', 'Synthetic event', start, end,
                     timezone_id=IDENTIFIER, timezone_definitions={IDENTIFIER: DEFINITION})
    restored = Event.from_dict(original.to_dict())
    assert restored.start.astimezone(UTC) == datetime(2024, 5, 4, hour, 0, 1, tzinfo=UTC)
    assert restored.start.fold == fold
    assert semantic(restored) == semantic(original)
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply('Save a repeated-hour phase', [original])
        persisted, = store.list_events()
        assert persisted.start.fold == fold
        center = datetime(2024, 5, 4, hour, 0, 1, tzinfo=UTC)
        projected, = store.query_occurrences(center - timedelta(minutes=1), center + timedelta(minutes=20))
        assert projected.start.timestamp() == original.start.timestamp()
        payload = export_ics(store.list_events())
    report = parse_report(payload, 'synthetic')
    assert not report.issues
    exported, = report.events
    assert exported.start.timestamp() == original.start.timestamp()
    assert exported.end.timestamp() == original.end.timestamp()
