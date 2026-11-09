"""Known timezone namespaces use reference clocks without overriding supplied rules.

All inputs are synthetic. Expected instants come from ZoneInfo or declared
fixed offsets, and the tests include persistence, recurrence and sync transport.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.ics_resources import RejectedResource, parse_report
from debacal.models import Calendar, Event
from debacal.recurrence import expand_events
from debacal.store import Store
from debacal.timezones import TimezoneError, resolve_timezone
from debacal.vdirsyncer_backend import semantic
from test_calendar_standards_interop import document, fixed_zone, vevent
from test_custom_timezone_model import event_editors
from test_vdirsyncer import setup, sync


UTC = timezone.utc
PREFIXES = [
    '/freeassociation.sourceforge.net/',
    '/freeassociation.sourceforge.net/Tzfile/',
    '/citadel.org/20240101/',
    '/mozilla.org/20070129_1/',
]


def accepted(raw, full=True, calendar_id='synthetic'):
    report = parse_report(raw, calendar_id, use_full=full)
    assert not report.issues and not report.rejected, [item.reason for item in report.issues]
    return list(report.events)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('prefix', PREFIXES)
@pytest.mark.parametrize('name', [
    'Europe/Berlin', 'America/Argentina/Buenos_Aires', 'Asia/Kathmandu', 'Etc/GMT+5', 'UTC',
])
def test_namespace_aliases_preserve_reference_clocks_through_store_edit_export(prefix, name, full):
    alias = prefix + name
    raw = document(vevent('prefixed-clock', '20260115T110000', '20260115T120000', tzid=alias,
                          extra=['RRULE:FREQ=WEEKLY;COUNT=2']))
    event, = accepted(raw, full)
    canonical = '' if name == 'UTC' else name
    expected = datetime(2026, 1, 15, 11, tzinfo=ZoneInfo(name)).astimezone(UTC)
    assert event.timezone_id == canonical
    assert event.start.astimezone(UTC) == expected
    assert event.timezone_definitions == {}
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply('Import prefixed clock', [event])
        edited = replace(store.list_events()[0], title='Edited prefixed appointment')
        store.apply('Edit imported clock', [edited])
        exported = export_ics(store.list_events())
    restored, = accepted(exported, full)
    assert restored.timezone_id == canonical
    assert restored.title == edited.title
    assert semantic(restored) == semantic(edited)
    instances = expand_events([restored], expected - timedelta(days=1), expected + timedelta(days=9))
    assert [item.start.astimezone(UTC) for item in instances] == [expected, expected + timedelta(weeks=1)]


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('prefix', PREFIXES)
def test_prefixed_recurrence_exclusions_and_detached_edits_keep_dst_instants(prefix, full):
    alias = prefix + 'Europe/Berlin'
    master = vevent('prefixed-series', '20261023T110000', '20261023T120000', tzid=alias,
                    extra=['RRULE:FREQ=DAILY;COUNT=5',
                           f'EXDATE;TZID="{alias}":20261024T110000',
                           f'RDATE;TZID="{alias}":20261028T110000'])
    moved = vevent('prefixed-series', '20261026T140000', '20261026T150000', tzid=alias,
                   extra=[f'RECURRENCE-ID;TZID="{alias}":20261026T110000'])
    event, = accepted(document(master, moved), full)
    begin, finish = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    expected = [datetime(2026, 10, day, hour, tzinfo=UTC)
                for day, hour in [(23, 9), (25, 10), (26, 13), (27, 10), (28, 10)]]
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply('Import prefixed series', [event])
        actual = store.query_occurrences(begin, finish)
        assert [item.start.astimezone(UTC) for item in actual] == expected
        exported = export_ics(store.list_events())
    restored, = accepted(exported, full)
    assert semantic(restored) == semantic(event)
    assert [item.start.astimezone(UTC) for item in expand_events([restored], begin, finish)] == expected


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('prefix', [*PREFIXES, '/unknown.example/'])
def test_exact_definition_wins_over_a_prefixed_database_shaped_name(prefix, full):
    alias = prefix + 'Europe/Berlin'
    raw = document(vevent('explicit-prefixed', '20260115T110000', '20260115T120000', tzid=alias,
                          extra=['RRULE:FREQ=WEEKLY;COUNT=2']), zones=[fixed_zone(alias, '+0300')])
    event, = accepted(raw, full)
    assert event.timezone_id == alias
    assert alias in event.timezone_definitions
    assert event.start.astimezone(UTC) == datetime(2026, 1, 15, 8, tzinfo=UTC)
    restored, = accepted(export_ics([replace(event, title='Edited explicit rules')]), full)
    assert restored.timezone_id == alias
    assert restored.start.timestamp() == event.start.timestamp()
    assert restored.timezone_definitions == event.timezone_definitions


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_invalid_matching_definition_is_not_replaced_with_namespace_alias_rules(full):
    alias = PREFIXES[0] + 'Europe/Berlin'
    bad = fixed_zone(alias, '+0300').replace('TZOFFSETTO:+0300', 'TZOFFSETTO:broken')
    raw = document(vevent('bad-explicit', '20260115T110000', '20260115T120000', tzid=alias), zones=[bad])
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.events and len(report.rejected) == 1
    assert alias in report.issues[0].reason
    assert 'VTIMEZONE' in report.issues[0].reason
    assert 'TZOFFSETTO:broken' in report.rejected[0].raw_ics


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_differently_named_definition_does_not_supply_rules_for_an_alias(full):
    alias = PREFIXES[0] + 'Europe/Berlin'
    raw = document(vevent('unrelated-definition', '20260115T110000', '20260115T120000', tzid=alias),
                   zones=[fixed_zone('Europe/Berlin', '+0300')])
    event, = accepted(raw, full)
    assert event.timezone_id == 'Europe/Berlin'
    assert event.timezone_definitions == {}
    assert event.start.astimezone(UTC) == datetime(2026, 1, 15, 10, tzinfo=UTC)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_alias_and_detached_canonical_name_definition_do_not_cross_bind(full):
    alias = PREFIXES[0] + 'Europe/Berlin'
    master = vevent('colliding-names', '20261023T110000', '20261023T120000', tzid=alias,
                    extra=['RRULE:FREQ=DAILY;COUNT=3'])
    detached = vevent('colliding-names', '20261024T140000', '20261024T150000', tzid='Europe/Berlin',
                      extra=[f'RECURRENCE-ID;TZID="{alias}":20261024T110000'])
    imported, = accepted(document(master, detached, zones=[fixed_zone('Europe/Berlin', '+0300')]), full)
    assert imported.timezone_id == alias
    assert 'Europe/Berlin' in imported.timezone_definitions
    begin, finish = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC)
    expected = [datetime(2026, 10, day, hour, tzinfo=UTC) for day, hour in [(23, 9), (24, 11), (25, 10)]]
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply('Import independent timezone names', [imported])
        current = store.list_events()[0]
        assert [item.start.astimezone(UTC) for item in store.query_occurrences(begin, finish)] == expected
    for _ in range(2):
        edited = replace(current, title='Edited independent timezone names')
        restored, = accepted(export_ics([edited]), full)
        assert [item.start.astimezone(UTC) for item in expand_events([restored], begin, finish)] == expected
        assert semantic(restored) == semantic(edited)
        current = restored


@pytest.mark.parametrize('nominal_duration', [False, True], ids=['exact-end', 'nominal-day'])
def test_title_edit_in_editor_preserves_distinct_alias_and_exception_rules(event_editors, nominal_duration):
    alias = PREFIXES[0] + 'Europe/Berlin'
    master = vevent('editor-collision', '20261023T110000', '20261023T120000', tzid=alias,
                    extra=['RRULE:FREQ=DAILY;COUNT=3'])
    if nominal_duration:
        master = master.replace(f'DTEND;TZID="{alias}":20261023T120000', 'DURATION:P1D')
    detached = vevent('editor-collision', '20261024T140000', '20261024T150000', tzid='Europe/Berlin',
                      extra=[f'RECURRENCE-ID;TZID="{alias}":20261024T110000'])
    imported, = accepted(document(master, detached, zones=[fixed_zone('Europe/Berlin', '+0300')]),
                          calendar_id='local')
    editor = event_editors(imported)
    editor.title.setText('Renamed without changing the clocks')
    edited = editor._project_preview(editor.build_events()[0])
    begin, finish = datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC)
    expected = [(item.start.timestamp(), item.end.timestamp()) for item in expand_events([imported], begin, finish)]
    assert [(item.start.timestamp(), item.end.timestamp()) for item in expand_events([edited], begin, finish)] == expected


@pytest.mark.parametrize('alias', [
    '/unknown.example/Europe/Berlin',
    '/freeassociation.sourceforge.net.evil/Europe/Berlin',
    '/Freeassociation.sourceforge.net/Europe/Berlin',
    '/freeassociation.sourceforge.net/Unknown/Location',
    '/freeassociation.sourceforge.net/../Europe/Berlin',
    '/freeassociation.sourceforge.net//Europe/Berlin',
    '/freeassociation.sourceforge.net/Europe/Berlin/',
    '/freeassociation.sourceforge.net/Tzfile/Etc/../../Europe/Berlin',
    '/freeassociation.sourceforge.net/Tzfile/',
    '/mozilla.org/Europe/Berlin',
    '/citadel.org/Europe/Berlin',
])
def test_unknown_or_malformed_namespaces_are_not_guessed(alias):
    with pytest.raises(TimezoneError):
        resolve_timezone(alias)
    raw = document(vevent('unresolved-prefix', '20260115T110000', '20260115T120000', tzid=alias),
                   vevent('healthy-peer', '20260115T110000Z', '20260115T120000Z'))
    report = parse_report(raw, 'synthetic')
    assert len(report.events) == len(report.rejected) == 1
    assert report.events[0].ical_uid == 'healthy-peer@example.invalid'
    assert alias in report.rejected[0].reason


@pytest.mark.parametrize('prefix', PREFIXES)
def test_stored_alias_payload_rehydrates_to_database_timezone_without_losing_instants(prefix):
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo('Europe/Berlin'), fold=1)
    original = Event('stored-alias', 'local', 'Stored appointment', start, start + timedelta(hours=2))
    payload = original.to_dict()
    payload['timezone_id'] = prefix + 'Europe/Berlin'
    restored = Event.from_dict(payload)
    assert restored.timezone_id == 'Europe/Berlin'
    assert restored.start.timestamp() == start.timestamp() and restored.start.fold == 1
    assert semantic(restored) == semantic(original)


def test_offline_bridge_normalizes_prefixed_resources_without_false_conflicts(tmp_path):
    alias = PREFIXES[1] + 'Europe/Berlin'
    raw = document(vevent('prefix-transport', '20261023T110000', '20261023T120000', tzid=alias,
                          extra=['RRULE:FREQ=DAILY;COUNT=5']))
    store, calendar, cli, backend = setup(tmp_path)
    try:
        cli.remote['prefix-transport@example.invalid'] = raw
        uid = 'prefix-transport@example.invalid'
        backend._quarantine(store, calendar.id, backend.calendar_path(calendar.id), [
            RejectedResource(uid, raw, hashlib.sha256(uid.encode()).hexdigest()[:12],
                             'Unsupported timezone definition.'),
        ])
        first = sync(store, calendar, backend)
        assert not first.issues and not first.conflicts
        assert not store.list_quarantined_resources(calendar.id)
        event, = store.list_events()
        assert event.timezone_id == 'Europe/Berlin'
        edited = replace(event, title='Edited transported appointment')
        store.apply('Edit transported appointment', [edited])
        result = sync(store, calendar, backend)
        assert not result.issues and not result.conflicts
        assert not store.pending_changes()
        remote, = parse_ics(cli.remote['prefix-transport@example.invalid'], calendar.id)
        assert semantic(remote) == semantic(edited)
        again = sync(store, calendar, backend)
        assert not again.issues and not again.conflicts
    finally:
        store.close()
