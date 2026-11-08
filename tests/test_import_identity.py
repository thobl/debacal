"""Reviewed imports update UID owners without duplicate or cross-calendar writes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import patch

import pytest

from debacal.ics import export_ics, parse_ics_report
from debacal.ics_resources import ParseReport, RejectedResource, ResourceIssue, reject_duplicate_identities
from debacal.import_preparation import reidentify_imports
from debacal.models import Calendar, Event
from debacal.recurrence import occurrence, occurrence_id, recurrence_key
from debacal.schedule_authoring import expression_matches
from debacal.sequence_document import document_expression
from debacal.store import HistoryConflict, Store, _EVENT_UID_SQL


START = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)


def event(identifier='native-id', **changes):
    return replace(Event(identifier, 'local', 'Appointment', START, START + timedelta(hours=1)), **changes)


def test_same_uid_import_without_native_metadata_updates_one_master_and_its_exceptions():
    source = event(ical_uid='standard-uid', recurrence_rule='FREQ=WEEKLY;COUNT=3')
    when = START + timedelta(weeks=1)
    detached = replace(occurrence(source, when), title='Moved occurrence',
                       start=when + timedelta(hours=2), end=when + timedelta(hours=3))
    source = replace(source, recurrence_overrides={recurrence_key(when): detached.to_dict()})
    source = replace(source, schedule_expression=document_expression(source),
                     expression_anchor=START.date().isoformat(), schedule_syntax=2)
    raw = export_ics([replace(source, title='Updated in another client')])
    stripped = b'\r\n'.join(line for line in raw.split(b'\r\n') if not line.startswith(b'X-DEBACAL-ID:'))
    report = parse_ics_report(stripped, 'local')
    assert report.events[0].id != source.id
    report = reidentify_imports(report, existing_events=[source])
    updated, = report.events
    assert updated.id == source.id and updated.ical_uid == source.ical_uid
    assert updated.title == 'Updated in another client'
    key = recurrence_key(when)
    override = updated.recurrence_overrides[key]
    assert override['id'] == occurrence_id(source.id, key)
    assert override['series_id'] == source.id and override['title'] == detached.title
    assert expression_matches(updated, updated.schedule_expression)
    with Store(':memory:') as store:
        store.apply_remote([source])
        store.apply('Import updated calendar', report.events)
        assert len(store.list_events()) == 1
        assert store.get_event(source.id) == updated
        store.undo()
        restored = store.get_event(source.id)
        # History retains the latest transport template, while restoring the
        # authoritative fields and detached content that export patches into it.
        assert replace(restored, raw_ics=source.raw_ics) == source
        assert parse_ics_report(export_ics([restored]), 'local').events[0].title == source.title


def test_reidentification_rebuilds_stale_custom_metadata_from_canonical_dates():
    source = event(ical_uid='standard-uid', recurrence_rule='FREQ=WEEKLY;COUNT=3')
    changed = replace(source, id='different-client-id', schedule_expression='+0d +1d',
                      expression_anchor=START.date().isoformat(), schedule_syntax=2)
    report = parse_ics_report(export_ics([changed]), 'local')
    updated, = reidentify_imports(report, existing_events=[source]).events
    assert updated.id == source.id
    assert expression_matches(updated, updated.schedule_expression)
    assert updated.schedule_expression != changed.schedule_expression


def test_uid_identity_preservation_is_calendar_scoped_and_rechecks_collisions():
    source = event(ical_uid='shared-uid')
    report = parse_ics_report(export_ics([replace(source, id='incoming')]), 'local')
    foreign = replace(source, calendar_id='another-calendar')
    unchanged = reidentify_imports(report, existing_events=[foreign])
    assert unchanged.events == report.events
    updated = reidentify_imports(report, existing_events=[source])
    assert updated.events[0].id == source.id
    copied = replace(source, ical_uid='different-uid')
    conflict = reject_duplicate_identities(updated, existing_events=[copied])
    assert not conflict.events and len(conflict.rejected) == 1


def test_ambiguous_existing_uid_owners_are_preserved_for_review():
    source = event(ical_uid='shared-uid')
    report = parse_ics_report(export_ics([replace(source, id='incoming')]), 'local')
    checked = reidentify_imports(report, existing_events=[source, replace(source, id='another-owner')])
    assert not checked.events and len(checked.rejected) == 1
    assert 'Multiple stored events' in checked.issues[0].reason


def test_import_ownership_check_and_write_block_a_concurrent_sync_upsert(tmp_path):
    path = tmp_path / 'calendar.sqlite'
    incoming = event('shared-id', calendar_id='remote', ical_uid='import-uid')
    remote = replace(incoming, title='Arriving from sync', ical_uid='remote-uid')
    with Store(path) as store, Store(path) as syncing:
        store.save_calendar(Calendar('remote', 'Personal', '#123456', kind='caldav',
                                     url='https://calendar.invalid/personal/'))
        syncing.connection.execute('PRAGMA busy_timeout=0')
        original = store.get_events
        blocked = []
        def concurrent_arrival(ids):
            values = original(ids)
            # This write used to succeed after the collision lookup, allowing
            # the following import upsert to overwrite an unrelated UID.
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                syncing.apply_remote([remote])
            blocked.append(True)
            return values
        with patch.object(store, 'get_events', side_effect=concurrent_arrival):
            report = store.import_report(ParseReport((incoming,)), calendar_id='remote')
        assert blocked == [True]
        assert report.events == (incoming,)
        assert store.get_event(incoming.id) == incoming
        assert len(store.history()) == 1
        # Once the import commits, its outbox protects it from the server
        # update until the real sync/conflict workflow reviews that change.
        with pytest.raises(HistoryConflict, match='pending local edits'):
            syncing.apply_remote([remote])
        store.undo()
        assert store.get_event(incoming.id) is None


def test_import_rejections_and_accepted_edits_roll_back_together_on_failure():
    incoming = event('incoming')
    rejected = RejectedResource('bad-uid', 'Malformed source retained for review', 'bad-resource', 'Malformed event')
    report = ParseReport((incoming,), (ResourceIssue(rejected.resource_id, rejected.reason),), (rejected,))
    with Store(':memory:') as store:
        store.save_calendar(replace(store.get_calendar('local'), enabled=False))
        with pytest.raises(ValueError, match='disabled'):
            store.import_report(report, calendar_id='local')
        assert store.get_event(incoming.id) is None
        assert not store.list_quarantined_resources()
        assert not store.history()


def test_uid_lookup_is_indexed_calendar_scoped_chunked_and_decodes_only_matches():
    wanted = event(ical_uid='shared-uid')
    with Store(':memory:') as store:
        store.save_calendar(Calendar('another-calendar', 'Other', '#123456'))
        unrelated = [event(f'unrelated-{index}') for index in range(1000)]
        store.apply_remote([wanted, replace(wanted, id='foreign', calendar_id='another-calendar'), *unrelated])
        requested = [f'missing-{index}' for index in range(600)] + ['shared-uid', 'shared-uid']
        with patch.object(Event, 'from_dict', wraps=Event.from_dict) as decode:
            assert store.get_events_by_uid('local', requested) == [wanted]
            assert store.get_events_by_uid('local', []) == []
            assert decode.call_count == 1
        plans = store.connection.execute(
            f'EXPLAIN QUERY PLAN SELECT data FROM events WHERE calendar_id=? AND ({_EVENT_UID_SQL}) IN (?)',
            ('local', 'shared-uid')).fetchall()
        assert any('events_calendar_uid' in row[3] for row in plans)


def test_uid_index_upgrade_handles_older_effective_uids_and_unrelated_corrupt_rows(tmp_path):
    path = tmp_path / 'calendar.sqlite'
    missing, empty, corrupt = event('missing'), event('empty'), event('corrupt')
    with Store(path) as store:
        store.apply_remote([missing, empty, corrupt])
        with store._transaction():
            first = missing.to_dict()
            first.pop('ical_uid')
            store.connection.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(first), missing.id))
            store.connection.execute('UPDATE events SET data=? WHERE id=?',
                                     (json.dumps(empty.to_dict() | {'ical_uid': ''}), empty.id))
            store.connection.execute('UPDATE events SET data=? WHERE id=?', ('not json', corrupt.id))
            store.connection.execute('DROP INDEX events_calendar_uid')
    with Store(path) as store:
        assert store.get_events_by_uid('local', [missing.id, empty.id, corrupt.id]) == [empty, missing]
        assert store.get_events_by_uid('local', ['unrelated']) == []


def test_reserved_uid_projection_includes_older_fields_and_remote_deletions_without_decoding():
    with Store(':memory:') as store:
        missing, empty, named = event('missing'), event('empty'), event('named', ical_uid='named-uid')
        store.apply_remote([missing, empty, named])
        store.set_sync_state('local', 'deleted', 'https://calendar.invalid/deleted.ics', 'version',
                             {'event': event('deleted', ical_uid='reserved-remote').to_dict()})
        store.set_sync_state('local', 'no-uid', '', '', {'event': {'ical_uid': ''}})
        with store._transaction():
            older = missing.to_dict()
            older.pop('ical_uid')
            store.connection.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(older), missing.id))
            store.connection.execute('UPDATE events SET data=? WHERE id=?',
                                     (json.dumps(empty.to_dict() | {'ical_uid': ''}), empty.id))
            store.connection.execute('INSERT INTO sync_metadata VALUES (?,?,?)', ('local', 'corrupt', 'not json'))
        with patch('debacal.store.json.loads', side_effect=AssertionError('UID projection must not decode payloads')):
            assert store.used_calendar_uids('local') == {'missing', 'empty', 'named-uid', 'reserved-remote'}
            assert store.used_calendar_uids('absent') == set()


def test_calendar_transfer_reserves_remote_uid_without_loading_destination_events():
    with Store(':memory:') as store:
        store.save_calendar(Calendar('destination', 'Destination', '#123456', kind='caldav'))
        source = event('source', ical_uid='reserved-remote')
        unrelated = [event(f'unrelated-{index}', calendar_id='destination') for index in range(1000)]
        store.apply_remote([source, *unrelated])
        deleted = event('deleted', calendar_id='destination', ical_uid=source.ical_uid)
        store.set_sync_state('destination', deleted.id, 'https://calendar.invalid/deleted.ics', 'version',
                             {'event': deleted.to_dict()})
        with patch.object(store, 'list_events', side_effect=AssertionError('Destination events must stay unloaded')), \
             patch.object(store, 'list_sync_state', side_effect=AssertionError('Sync templates must stay unloaded')), \
             patch.object(Event, 'from_dict', wraps=Event.from_dict) as decode:
            store.edit_selection([source], 'single', source, replace(source, calendar_id='destination'),
                                 calendar_id='destination')
        assert decode.call_count < 20
        assert store.get_event(source.id) is None
        transferred, = [item for item in store.list_events(calendar_ids=['destination']) if item.title == source.title
                         and item.id not in {value.id for value in unrelated}]
        assert transferred.ical_uid != source.ical_uid
        assert transferred.calendar_id == 'destination'
        store.undo()
        assert store.get_event(source.id) == source
