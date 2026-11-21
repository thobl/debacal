"""Reminder intent across canonical snapshots, history, upload, and conflict review."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alarm_support import BASE, alarm_event
from test_vdirsyncer import setup, sync
from debacal.alarms import Alarm, alarm_trigger
from debacal.alarm_ics import hydrate_alarms
from debacal.conflict_dialog import ConflictDialog
from debacal.event_clipboard import EventClipboard
from debacal.ics import export_ics, parse_ics, patch_ics
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.vdirsyncer_backend import semantic

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
WIRE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:e
SUMMARY:Example
DTSTART:20261121T090000Z
DTEND:20261121T100000Z
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT5M
DESCRIPTION:Start
X-DEVICE:watch
END:VALARM
END:VEVENT
END:VCALENDAR
"""


def legacy(event):
    value = event.to_dict()
    for payload in [value, *value['recurrence_overrides'].values()]:
        payload.pop('alarms', None)
        payload.pop('end_timezone_id', None)
    return Event.from_dict(value)


def store_semantic(event):
    return Store._semantic_event(event.to_dict())


def test_remove_survives_remote_template_history_and_conflict(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    with store:
        cli.remote['e'] = WIRE
        sync(store, calendar, backend)
        original, = store.list_events(calendar_ids=[calendar.id])
        old = legacy(original)
        store.apply_remote([old])
        state = store.get_sync_state(calendar.id, old.id)
        base = state['base_payload'] | {'event': old.to_dict()}
        store.set_sync_state(calendar.id, old.id, state['href'], state['etag'], base)
        hydrated, = store.get_events_with_alarms([old.id])
        assert semantic(hydrated) == semantic(old) == semantic(original)
        assert store.apply('Load reminders', [hydrated]) is None
        assert not store.history() and not store.pending_changes()
        removed = replace(hydrated, alarms=())
        operation = store.edit_selection([hydrated], 'single', hydrated, removed)
        change, = store.pending_changes()
        assert change['payload']['alarms'] == []
        assert parse_ics(patch_ics(WIRE, store.get_event(old.id)), calendar.id)[0].alarms == ()
        assert semantic(removed) != semantic(old)
        store.undo(operation)
        restored = store.get_event(old.id)
        assert restored.alarms[0].trigger == '-PT5M'
        assert 'X-DEVICE:watch' in restored.alarms[0].raw_ics
        store.redo(operation)
        assert store.get_event(old.id).alarms == ()
        cli.remote['e'] = WIRE.replace('watch', 'phone')
        result = sync(store, calendar, backend)
        conflict, = result.conflicts
        assert conflict.local.alarms == ()
        assert 'X-DEVICE:phone' in conflict.remote.alarms[0].raw_ics
        backend.set_decision(calendar.id, conflict.id, 'remote')
        assert not sync(store, calendar, backend).conflicts
        assert 'X-DEVICE:phone' in store.get_event(old.id).alarms[0].raw_ics
        assert not store.pending_changes()


def test_saving_against_unchanged_legacy_baseline_uploads_removal(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    with store:
        cli.remote['e'] = WIRE
        sync(store, calendar, backend)
        event, = store.list_events(calendar_ids=[calendar.id])
        state = store.get_sync_state(calendar.id, event.id)
        payload = state['base_payload'] | {'event': legacy(event).to_dict()}
        store.set_sync_state(calendar.id, event.id, state['href'], state['etag'], payload)
        store.apply('Remove reminder', [replace(event, alarms=())])
        result = sync(store, calendar, backend)
        assert result.sent == 1 and not result.conflicts
        assert not parse_ics(cli.remote['e'], calendar.id)[0].alarms


@pytest.mark.parametrize('comparison', [semantic, store_semantic])
def test_alarm_properties_ignore_keys_and_wire_format_but_include_unknown_parameters(comparison):
    item, = parse_ics(WIRE, 'c')
    altered, = parse_ics(WIRE.replace('TRIGGER:-PT5M', 'TRIGGER;RELATED=START:-PT300S')
                        .replace('DESCRIPTION:Start\nX-DEVICE:watch', 'X-DEVICE:watch\nDESCRIPTION:Start'), 'c')
    altered = replace(altered, alarms=(replace(altered.alarms[0], key='other'),))
    assert comparison(item) == comparison(altered)
    device, = parse_ics(WIRE.replace('X-DEVICE:', 'X-DEVICE;X-CHANNEL=home:'), 'c')
    assert comparison(item) != comparison(device)


@pytest.mark.parametrize('comparison', [semantic, store_semantic])
@pytest.mark.parametrize('dated', [False, True])
def test_presentation_only_zones_compare_equal_after_roundtrip(comparison, dated):
    event = (alarm_event(start=datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1),
                         end=datetime(2026, 10, 25, 3, 30, tzinfo=UTC), recurrence_dates=('2026-10-27T01:30:00+00:00',))
             if dated else alarm_event(start=datetime(2026, 10, 23, 9, tzinfo=BERLIN),
                                       end=datetime(2026, 10, 24, 7, tzinfo=UTC),
                                       duration_spec='P1D', recurrence_rule='FREQ=DAILY;COUNT=4'))
    restored, = parse_ics(export_ics([event]), event.calendar_id)
    assert comparison(restored) == comparison(event)


def test_authored_rdate_timezone_change_survives_same_instant_save_and_history(tmp_path):
    start = datetime(2026, 11, 21, 10, tzinfo=BERLIN)
    source = alarm_event(start=start, end=start + timedelta(hours=1), recurrence_dates=('2026-11-22T09:00:00+00:00',))
    ny = ZoneInfo('America/New_York')
    edited = replace(source, timezone_id=ny.key, start=source.start.astimezone(ny), end=source.end.astimezone(ny))
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([source])
        op = store.apply('Change timezone', [edited])
        assert op is not None
        saved = store.get_event(source.id)
        assert saved.timezone_id == ny.key and saved.start.hour == 4
        assert store.pending_changes()[0]['payload']['timezone_id'] == ny.key
        assert store_semantic(source) != store_semantic(saved)
        store.undo(op)
        assert store.get_event(source.id).timezone_id == BERLIN.key
        store.redo(op)
        assert store.get_event(source.id).timezone_id == ny.key


@pytest.mark.parametrize('fold', [False, True], ids=['normal', 'repeated-hour'])
def test_changed_named_rdate_zone_invalidates_local_history(fold, tmp_path):
    start = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=BERLIN) if fold else BASE.astimezone(BERLIN)
    source = alarm_event(start=start, end=(start.astimezone(UTC) + timedelta(hours=1)),
                         recurrence_dates=('2026-11-22T09:00:00+00:00',))
    stockholm = ZoneInfo('Europe/Stockholm')
    edited = replace(source, timezone_id=stockholm.key, start=start.astimezone(stockholm))
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply('Create', [source])
        store.apply_remote([edited])
        assert not store.history()[0]['can_undo']


@pytest.mark.parametrize('comparison', [semantic, store_semantic])
@pytest.mark.parametrize('role,repeat', [('START', False), ('END', False), ('END', True)])
def test_equal_instants_with_different_nominal_calculation_zones_compare_different(comparison, role, repeat):
    anchor = datetime(2026, 10, 24 if repeat else 25, 10, tzinfo=BERLIN)
    alarm = Alarm('n', 'DISPLAY', '-PT5M' if repeat else '-P1D', related=role, description='Notify',
                  repeat=int(repeat), repeat_interval='P1D' if repeat else None)
    item = alarm_event(start=anchor, end=anchor + timedelta(hours=1), alarms=(alarm,))
    zone = UTC if role == 'START' else ZoneInfo('America/Phoenix')
    other = replace(item, start=item.start.astimezone(zone), timezone_id='',
                    end=item.end.astimezone(zone)) if role == 'START' else replace(item, end=item.end.astimezone(zone))
    assert alarm_trigger(alarm, item.start, item.end, repeat_index=int(repeat)) != alarm_trigger(
        alarm, other.start, other.end, repeat_index=int(repeat))
    assert comparison(item) != comparison(other)


def test_legacy_end_zone_recovered_only_for_matching_canonical_endpoint():
    raw = WIRE.replace('DTSTART:20261121T090000Z', 'DTSTART;TZID=Europe/Berlin:20261025T020000').replace(
        'DTEND:20261121T100000Z', 'DTEND;TZID=America/Phoenix:20261024T213000').replace(
        'TRIGGER:-PT5M', 'TRIGGER;RELATED=END:-P1D')
    fresh, = parse_ics(raw, 'c')
    old = legacy(fresh)
    restored = hydrate_alarms(old)
    assert restored.end_timezone_id == 'America/Phoenix'
    assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 4, 30, tzinfo=UTC)
    assert semantic(old) == semantic(fresh)
    edited = replace(old, end=old.end + timedelta(hours=2))
    hydrated = hydrate_alarms(edited)
    assert hydrated.end == edited.end and not hydrated.end_timezone_id


def test_clipboard_allows_rekey_but_rejects_changed_alarm_metadata():
    original, = parse_ics(WIRE, 'c')
    reference = replace(original, alarms=(replace(original.alarms[0], key='new'),))
    copied = EventClipboard.capture([original], reference)
    assert 'X-DEVICE:watch' in copied.reference.alarms[0].raw_ics
    stale, = parse_ics(WIRE.replace('watch', 'phone'), 'c')
    with pytest.raises(ValueError, match='changed'):
        EventClipboard.capture([original], stale)


def test_alarm_only_conflict_has_visible_reminder_descriptions():
    describe = lambda event: ConflictDialog.describe(SimpleNamespace(preferences=Preferences()), event)['Reminders']
    original, = parse_ics(WIRE, 'c')
    assert describe(replace(original, alarms=())) == 'None'
    assert '-PT5M' in describe(original)
    unsupported, = parse_ics(WIRE.replace('ACTION:DISPLAY', 'ACTION:PROCEDURE'), 'c')
    assert 'unsupported' in describe(unsupported).lower()
    changed, = parse_ics(WIRE.replace('watch', 'phone'), 'c')
    assert describe(original) != describe(changed)


def test_serializer_issue_keeps_only_affected_resource_queued(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    with store:
        failed = alarm_event(start=datetime(2026, 10, 25, 0, 10, tzinfo=UTC).astimezone(ZoneInfo('America/New_York')),
                             end=datetime(2026, 10, 25, 2, 45, tzinfo=BERLIN, fold=1), recurrence_rule='FREQ=DAILY',
                             alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END'),), calendar_id=calendar.id)
        good = alarm_event(id='good', ical_uid='good', calendar_id=calendar.id)
        store.apply('Add meetings', [failed, good])
        result = sync(store, calendar, backend)
        assert result.sent == 1 and set(cli.remote) == {'good'}
        assert [change['event_id'] for change in store.pending_changes()] == ['e']
        assert 'elapsed-hour reminder' in result.issues[0].reason
        assert store.get_event('e').to_dict() == failed.to_dict()


def test_legacy_detached_reminder_belongs_to_source_family_after_calendar_transfer(tmp_path):
    wire = WIRE.replace('SUMMARY:Example', 'SUMMARY:Example\nRRULE:FREQ=DAILY;COUNT=2').replace(
        'END:VCALENDAR', """BEGIN:VEVENT
UID:e
RECURRENCE-ID:20261122T090000Z
DTSTART:20261122T090000Z
DTEND:20261122T100000Z
SUMMARY:Second
BEGIN:VALARM
ACTION:AUDIO
TRIGGER:-PT1H
END:VALARM
END:VEVENT
END:VCALENDAR""")
    with Store(tmp_path / 'db') as store:
        for calendar in ['c', 'dest']:
            store.save_calendar(Calendar(calendar, calendar, '#123456', kind='caldav'))
        master, = parse_ics(wire, 'c')
        store.apply_remote([legacy(master)])
        dated = store.query_occurrences(BASE, BASE + timedelta(days=2))
        selected = dated[1]
        operation = store.edit_selection([selected], 'single', selected, replace(selected, calendar_id='dest'))
        moved, = store.list_events(calendar_ids=['dest'])
        assert moved.alarms[0].trigger == '-PT1H'
        restored = store.undo(operation)
        assert restored == operation
        family, = store.get_events_with_alarms([master.id])
        assert family.alarms[0].trigger == '-PT5M'
        assert Event.from_dict(next(iter(family.recurrence_overrides.values()))).alarms[0].trigger == '-PT1H'


def test_legacy_outbox_uses_own_reminders_before_selecting_server_template():
    original, = parse_ics(WIRE, 'c')
    local = replace(legacy(original), title='Local title')
    remote = WIRE.replace('TRIGGER:-PT5M', 'TRIGGER:-PT10M').replace('SUMMARY:Example', 'SUMMARY:Example\nX-SERVER:keep')
    upload = patch_ics(remote, local)
    assert b'X-SERVER:keep' in upload
    assert parse_ics(upload, 'c')[0].alarms[0].trigger == '-PT5M'


def test_query_and_reopen_keep_unhydrated_rows_lazy(tmp_path, monkeypatch):
    from debacal import alarm_ics
    path = tmp_path / 'db'
    original, = parse_ics(WIRE, 'c')
    with Store(path) as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply_remote([legacy(original)])
    def unexpected(*args, **kwargs):
        pytest.fail('ordinary query opened retained alarms')
    with monkeypatch.context() as context:
        context.setattr(alarm_ics, '_retained_alarms', unexpected)
        context.setattr(alarm_ics, 'hydrate_alarms', unexpected)
        with Store(path) as store:
            item, = store.query_occurrences(BASE, BASE + timedelta(days=1))
            assert item.alarms is None
            assert store.get_event(original.id).alarms is None
            assert store.query_source_events([original.id])[0].alarms is None


def test_unknown_edit_keeps_known_removal_even_when_retained_template_still_has_alarm(tmp_path):
    original, = parse_ics(WIRE, 'c')
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply_remote([replace(original, alarms=())])
        projected = replace(original, raw_ics='', alarms=None, title='New title', _query_projection=True)
        store.apply('Change title', [projected])
        saved = store.get_event(original.id)
        assert saved.alarms == () and saved.title == 'New title'


def test_ui_history_defers_unknown_alarm_comparison_and_worker_rejects_stale_undo(tmp_path, monkeypatch):
    from debacal import alarm_ics
    from debacal.store import HistoryConflict
    path = tmp_path / 'db'
    original, = parse_ics(WIRE, 'c')
    with Store(path) as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        operation = store.apply('Create', [original])
        old_after = {original.id: legacy(original).to_dict()}
        del old_after[original.id]['alarms']
        store.connection.execute('UPDATE operations SET after_data=? WHERE id=?', (json.dumps(old_after), operation))
        store.connection.commit()
        changed, = parse_ics(WIRE.replace('watch', 'phone'), 'c')
        store.apply_remote([changed])
        store.configure_ui_access(lambda: None)
        with Store(path, query_index=False) as writer:
            with writer._transaction(), monkeypatch.context() as scope:
                scope.setattr(alarm_ics, '_retained_alarms', lambda *args, **kwargs: pytest.fail('UI hydrated alarms'))
                assert store.history()[0]['can_undo']
                assert store.history_availability() == (True, False)
            with pytest.raises(HistoryConflict):
                writer.undo(operation)
            writer.apply_remote([original])
            writer.undo(operation)
            writer.redo(operation)
            assert writer.get_event(original.id).alarms[0].trigger == '-PT5M'
        # Known alarm changes always disable stale actions, including on the UI.
        store.connection.execute('UPDATE operations SET after_data=? WHERE id=?',
                                 (json.dumps({original.id: original.to_dict()}), operation))
        store.apply_remote([changed])
        assert not store.history()[0]['can_undo']
        assert store.history_availability() == (False, False)


def test_legacy_history_restores_snapshot_reminder_against_current_template(tmp_path):
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        original, = parse_ics(WIRE, 'c')
        store.apply_remote([original])
        edited = replace(original, title='Changed', alarms=())
        operation = store.apply('Edit', [edited])
        old_before = {original.id: legacy(original).to_dict()}
        del old_before[original.id]['alarms']
        store.connection.execute('UPDATE operations SET before_data=? WHERE id=?', (json.dumps(old_before), operation))
        latest = patch_ics(WIRE.replace('SUMMARY:Example', 'X-SERVER:keep\nSUMMARY:Example'), edited)
        store.apply_remote([replace(edited, raw_ics=latest.decode())])
        store.undo(operation)
        restored = store.get_event(original.id)
        assert restored.alarms[0].trigger == '-PT5M' and 'X-SERVER:keep' in restored.raw_ics
        store.redo(operation)
        assert store.get_event(original.id).alarms == ()


def test_reminder_only_selection_edit_retains_independent_end_calculation_zone(tmp_path):
    start = datetime(2026, 10, 25, 2, tzinfo=BERLIN)
    end = datetime(2026, 10, 24, 21, 30, tzinfo=ZoneInfo('America/Phoenix'))
    original = alarm_event(start=start, end=end)
    alarm = Alarm('n', 'DISPLAY', '-P1D', related='END')
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply_remote([original])
        store.edit_selection([original], 'single', original, replace(original, alarms=(alarm,)))
        saved = store.get_event(original.id)
        assert saved.end_timezone_id == 'America/Phoenix'
        assert alarm_trigger(saved.alarms[0], saved.start, saved.end) == datetime(2026, 10, 24, 4, 30, tzinfo=UTC)


def test_stale_legacy_source_rejects_structured_reminder_edit(tmp_path):
    from debacal.store import HistoryConflict
    original, = parse_ics(WIRE, 'c')
    updated, = parse_ics(WIRE.replace('watch', 'phone'), 'c')
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply_remote([legacy(updated)])
        with pytest.raises(HistoryConflict):
            store.edit_selection([original], 'single', original, replace(original, alarms=()))


def test_import_retains_changed_event_metadata_and_does_not_queue_alarm_rekey(tmp_path):
    from debacal.ics import parse_ics_report
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        original, = parse_ics(WIRE, 'c')
        store.apply_remote([original])
        rekey = replace(original, alarms=(replace(original.alarms[0], key='different'),))
        assert store.apply('Reload reminder', [rekey]) is None
        assert not store.history() and not store.pending_changes()
        changed = WIRE.replace('SUMMARY:Example', 'SUMMARY:Example\nX-SOURCE-LABEL:blue')
        store.import_report(parse_ics_report(changed, 'c'), calendar_id='c')
        assert 'X-SOURCE-LABEL:blue' in store.get_event(original.id).raw_ics
        assert store.get_event(original.id).alarms[0].trigger == '-PT5M'


def test_injected_sync_conflict_hydrates_legacy_outbox_on_worker_and_accepts_remote(tmp_path):
    from test_sync import setup as transport_setup, synchronize
    from debacal.sync import RemoteResource
    store, transport, engine = transport_setup(tmp_path)
    try:
        original, = parse_ics(WIRE, 'remote')
        store.apply('Import', [original])
        synchronize(engine)
        href, = transport.resources
        local = replace(original, title='Edited title')
        store.apply('Edit title', [local])
        with store._transaction():
            payload = local.to_dict()
            del payload['alarms']
            store.connection.execute('UPDATE outbox SET payload=?', (json.dumps(payload),))
            store.connection.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(payload), local.id))
        transport.resources[href] = RemoteResource(href, '"another"', WIRE.replace('watch', 'phone'))
        synchronize(engine)
        conflict, = engine.conflicts
        assert 'X-DEVICE:watch' in conflict.local.alarms[0].raw_ics
        engine.resolve_conflict(conflict.id, 'remote')
        synchronize(engine)
        assert not engine.conflicts
        assert 'X-DEVICE:phone' in store.get_event(local.id).alarms[0].raw_ics
    finally:
        engine.close()
        store.close()


def test_whole_resource_alarm_edit_keeps_absolute_alarm_off_detached_children(tmp_path):
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        item = alarm_event(recurrence_rule='FREQ=DAILY;COUNT=2')
        store.apply_remote([item])
        first, second = store.query_occurrences(BASE, BASE + timedelta(days=2))
        child_alarm = Alarm('child', 'DISPLAY', '-PT10M')
        store.edit_occurrence(second, replace(second, title='Detached', alarms=(child_alarm,)))
        absolute = Alarm('once', 'DISPLAY', '20261120T110000Z')
        store.edit_selection([first], 'group', first, replace(store.get_event(item.id), alarms=(absolute,)))
        saved = store.get_event(item.id)
        assert saved.alarms == (absolute,)
        assert Event.from_dict(saved.recurrence_overrides[second.recurrence_id]).alarms == (child_alarm,)


def test_conflict_reminder_summary_distinguishes_metadata_after_large_attachment_prefix():
    describe = lambda event: ConflictDialog.describe(SimpleNamespace(preferences=Preferences()), event)['Reminders']
    def attachment(last):
        wire = WIRE.replace('X-DEVICE:watch', 'ATTACH;VALUE=BINARY;ENCODING=BASE64:' + 'A' * 8000 + last)
        return parse_ics(wire, 'c')[0]
    first, second = describe(attachment('ABCD')), describe(attachment('ACEF'))
    assert first != second
    assert max(len(first), len(second)) < 1000


@pytest.mark.parametrize('comparison', [semantic, store_semantic])
def test_independent_dtend_timezone_remains_significant_on_rule(comparison):
    start = datetime(2026, 10, 23, 9, tzinfo=BERLIN)
    end = datetime(2026, 10, 23, 10, tzinfo=BERLIN)
    source = alarm_event(start=start, end=end.astimezone(ZoneInfo('America/Phoenix')),
                         recurrence_rule='FREQ=DAILY;COUNT=4')
    assert comparison(source) != comparison(replace(source, end=end.astimezone(UTC)))


def test_legacy_history_availability_recognizes_same_instant_with_recovered_end_zone():
    source, = parse_ics(WIRE.replace('DTSTART:20261121T090000Z', 'DTSTART;TZID=Europe/Berlin:20261025T020000')
                         .replace('DTEND:20261121T100000Z', 'DTEND;TZID=America/Phoenix:20261024T213000')
                         .replace('TRIGGER:-PT5M', 'TRIGGER;RELATED=END:-P1D'), 'c')
    with Store(':memory:') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        op = store.apply('Create', [source])
        with store._transaction():
            store.connection.execute('UPDATE operations SET after_data=? WHERE id=?',
                                     (json.dumps({source.id: legacy(source).to_dict()}), op))
        store.configure_ui_access(lambda: None)
        assert store.history_availability() == (True, False)
        store.undo(op)
        assert store.get_event(source.id) is None

def phoenix_legacy_family(detached):
    raw = WIRE.replace('DTSTART:20261121T090000Z', 'DTSTART;TZID=Europe/Berlin:20261025T020000').replace(
        'DTEND:20261121T100000Z', 'DTEND;TZID=America/Phoenix:20261024T213000').replace(
        'TRIGGER:-PT5M', 'TRIGGER;RELATED=END:-P1D')
    if detached:
        raw = raw.replace('BEGIN:VEVENT', """BEGIN:VEVENT
UID:e
DTSTART;TZID=Europe/Berlin:20261024T020000
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=2
SUMMARY:Master
END:VEVENT
BEGIN:VEVENT
RECURRENCE-ID;TZID=Europe/Berlin:20261025T020000""")
    parsed, = parse_ics(raw, 'c')
    return legacy(parsed)


@pytest.mark.parametrize('detached', [False, True], ids=['master', 'child'])
@pytest.mark.parametrize('edit', ['noop', 'description', 'move_endpoint', 'rezone_endpoint'])
def test_legacy_apply_keeps_matching_bound_provenance_independent_of_alarm_edit(tmp_path, detached, edit):
    family = phoenix_legacy_family(detached)
    def owner(value):
        return Event.from_dict(next(iter(value.recurrence_overrides.values()))) if detached else value
    def submit(component):
        if detached:
            key, = family.recurrence_overrides
            return replace(family, recurrence_overrides={key: component.to_dict()})
        return component
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([family])
        hydrated, = store.get_events_with_alarms([family.id])
        alarm, = owner(hydrated).alarms
        expected_zone = 'America/Phoenix'
        expected_fire = datetime(2026, 10, 24, 4, 30, tzinfo=UTC)
        submitted = family
        if edit != 'noop':
            component = replace(owner(family), alarms=(replace(alarm, description='Edited reminder'),))
            if edit == 'move_endpoint':
                component = replace(component, end=component.end + timedelta(hours=2))
                expected_zone = ''
                expected_fire = datetime(2026, 10, 24, 5, 30, tzinfo=UTC)
            elif edit == 'rezone_endpoint':
                component = replace(component, end=component.end.astimezone(UTC))
                expected_zone = 'UTC'
            submitted = submit(component)
        op = store.apply('Save reminder', [submitted])
        if edit == 'noop':
            assert op is None
            assert not store.history() and not store.pending_changes()
            saved, = store.get_events_with_alarms([family.id])
            versions = [saved]
        else:
            assert op is not None
            saved = store.get_event(family.id)
            change, = store.pending_changes()
            versions = [saved, Event.from_dict(change['payload'])]
        for version in versions:
            result = owner(version)
            assert result.end_timezone_id == expected_zone
            assert alarm_trigger(result.alarms[0], result.start, result.end) == expected_fire
            if edit != 'noop':
                assert result.alarms[0].description == 'Edited reminder'


def test_malformed_acknowledgements_have_distinct_conflict_descriptions():
    describe = lambda event: ConflictDialog.describe(SimpleNamespace(preferences=Preferences()), event)['Reminders']
    def malformed(text):
        return parse_ics(WIRE.replace('END:VALARM', f'ACKNOWLEDGED:{text}\nEND:VALARM'), 'c')[0]
    first, second = malformed('bad-a'), malformed('bad-b')
    assert semantic(first) != semantic(second)
    assert describe(first) != describe(second)
    assert 'bad-a' in describe(first) and 'bad-b' in describe(second)


@pytest.mark.parametrize('detached', [False, True], ids=['master', 'child'])
def test_modern_same_instant_end_zone_edit_is_authoritative_with_untouched_alarms(tmp_path, detached):
    family = hydrate_alarms(phoenix_legacy_family(detached))
    key = next(iter(family.recurrence_overrides), None)
    original = Event.from_dict(family.recurrence_overrides[key]) if detached else family
    edited = replace(original, end=original.end.astimezone(BERLIN), alarms=None)
    submitted = replace(family, recurrence_overrides={key: edited.to_dict()}) if detached else edited
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([family])
        assert store.apply('Use the primary timezone for the end', [submitted]) is not None
        saved = store.get_event(family.id)
        owner = Event.from_dict(saved.recurrence_overrides[key]) if detached else saved
        assert owner.end_timezone_id == ''
        assert owner.end.timestamp() == original.end.timestamp()
        assert alarm_trigger(owner.alarms[0], owner.start, owner.end) == datetime(2026, 10, 24, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize('comparison', [semantic, store_semantic])
def test_normal_rdate_with_declared_utc_end_remains_distinct(comparison):
    start = BASE.astimezone(BERLIN)
    event = alarm_event(start=start, end=start + timedelta(hours=1), recurrence_dates=('2026-11-22T09:00:00+00:00',))
    explicit_end = replace(event, end=event.end.astimezone(UTC))
    assert comparison(event) != comparison(explicit_end)


@pytest.mark.parametrize('detached', [False, True], ids=['master', 'child'])
def test_reimported_primary_zone_end_supersedes_same_instant_legacy_provenance(tmp_path, detached):
    from debacal.ics import parse_ics_report
    old = phoenix_legacy_family(detached)
    wire = old.raw_ics.replace('DTEND;TZID=America/Phoenix:20261024T213000',
                               'DTEND;TZID=Europe/Berlin:20261025T053000')
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456'))
        store.apply_remote([old])
        store.import_report(parse_ics_report(wire, 'c'), calendar_id='c')
        saved = store.get_event(old.id)
        owner = Event.from_dict(next(iter(saved.recurrence_overrides.values()))) if detached else saved
        assert owner.end_timezone_id == ''
        assert alarm_trigger(owner.alarms[0], owner.start, owner.end) == datetime(2026, 10, 24, 3, 30, tzinfo=UTC)


def legacy_late_fold_alarm_master():
    from test_ics_duration_semantics import strip_metadata
    start = datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN, fold=1)
    end = datetime(2026, 10, 25, 2, 45, tzinfo=BERLIN, fold=1)
    alarms = (Alarm('start', 'DISPLAY', '-P1D'), Alarm('end', 'DISPLAY', '-P1D', related='END'))
    wire = export_ics([alarm_event(start=start, end=end, alarms=alarms)])
    standard, = parse_ics(strip_metadata(wire), 'c')
    return replace(standard, alarms=None, raw_ics=wire.decode())


@pytest.mark.parametrize('edit_description', [False, True], ids=['noop', 'alarm-edit'])
def test_matching_legacy_late_fold_bounds_share_recovered_zone_in_history_and_outbox(tmp_path, edit_description):
    old = legacy_late_fold_alarm_master()
    assert old.start.isoformat() == '2026-10-25T01:00:00+00:00'
    assert old.end.isoformat() == '2026-10-25T01:45:00+00:00'
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([old])
        recovered, = store.get_events_with_alarms([old.id])
        assert recovered.start.isoformat() == '2026-10-25T02:00:00+01:00'
        assert recovered.end.isoformat() == '2026-10-25T02:45:00+01:00'
        assert recovered.start.fold == recovered.end.fold == 1
        assert recovered.timezone_id == BERLIN.key and not recovered.end_timezone_id
        submitted = old
        if edit_description:
            submitted = replace(old, alarms=tuple(replace(a, description='Edited') for a in recovered.alarms))
        operation = store.apply('Save', [submitted])
        if edit_description:
            assert operation is not None
            change, = store.pending_changes()
            assert [alarm['description'] for alarm in change['payload']['alarms']] == ['Edited', 'Edited']
            assert change['payload']['end'] == '2026-10-25T02:45:00+01:00'
            assert 'end_timezone_id' not in change['payload']
            before, after = store.connection.execute(
                'SELECT before_data, after_data FROM operations WHERE id=?', (operation,)).fetchone()
            for state in (json.loads(before)[old.id], json.loads(after)[old.id]):
                assert state['start'] == '2026-10-25T02:00:00+01:00'
                assert state['end'] == '2026-10-25T02:45:00+01:00'
                assert 'end_timezone_id' not in state
        else:
            assert operation is None
            assert not store.history() and not store.pending_changes()

def test_legacy_title_edit_preserves_declared_new_york_end_and_nominal_alarm(tmp_path):
    placeholder = legacy_late_fold_alarm_master()
    original = replace(placeholder, end=placeholder.end.astimezone(ZoneInfo('America/New_York')))
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([original])
        assert store.get_event(original.id).end_timezone_id == 'America/New_York'
        hydrated, = store.get_events_with_alarms([original.id])
        op = store.apply('Edit title', [replace(original, title='Edited title')])
        change, = store.pending_changes()
        before, after = store.connection.execute(
            'SELECT before_data, after_data FROM operations WHERE id=?', (op,)).fetchone()
        snapshots = {'worker read': hydrated.to_dict(), 'stored': store.get_event(original.id).to_dict(),
                     'outbox': change['payload'], 'history before': json.loads(before)[original.id],
                     'history after': json.loads(after)[original.id]}
        assert snapshots['stored']['title'] == snapshots['outbox']['title'] == 'Edited title'

        def end_reminder(payload):
            event = Event.from_dict(payload)
            alarm, = (item for item in event.alarms if item.related == 'END')
            return (payload.get('end_timezone_id'), payload['end'], alarm.trigger,
                    alarm_trigger(alarm, event.start, event.end))

        expected = ('America/New_York', '2026-10-24T21:45:00-04:00', '-P1D',
                    datetime(2026, 10, 24, 1, 45, tzinfo=UTC))
        assert {stage: end_reminder(payload) for stage, payload in snapshots.items()} == dict.fromkeys(snapshots, expected)


@pytest.mark.parametrize('identifier, independent', [('Europe/Berlin', ''), ('Europe/Vienna', 'Europe/Vienna')])
def test_legacy_noop_keeps_declared_end_when_recovered_start_has_same_clock(tmp_path, identifier, independent):
    placeholder = legacy_late_fold_alarm_master()
    original = replace(placeholder, end=placeholder.end.astimezone(ZoneInfo(identifier)))
    with Store(tmp_path / 'db') as store:
        store.save_calendar(Calendar('c', 'C', '#123456', kind='caldav'))
        store.apply_remote([original])
        assert store.get_event(original.id).end_timezone_id == identifier
        recovered, = store.get_events_with_alarms([original.id])
        assert recovered.timezone_id == BERLIN.key and recovered.end.tzinfo.key == identifier
        assert recovered.end_timezone_id == independent
        assert store.apply('Save', [original]) is None
        assert not store.history() and not store.pending_changes()

def test_legacy_private_alarm_hydration_obeys_declared_primary_zone():
    placeholder = legacy_late_fold_alarm_master()
    ny = ZoneInfo('America/New_York')
    original = replace(placeholder, start=placeholder.start.astimezone(ny), timezone_id=ny.key)
    recovered = hydrate_alarms(original)
    start_alarm, = (alarm for alarm in recovered.alarms if alarm.related == 'START')
    assert recovered.timezone_id == ny.key
    assert start_alarm.trigger == '-P1D'
    assert alarm_trigger(start_alarm, recovered.start, recovered.end) == datetime(2026, 10, 24, 1, tzinfo=UTC)
