"""Remote metadata must never take another resource's global database identity."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import sqlite3
from unittest.mock import patch

import pytest

from debacal.ics import export_ics, parse_ics_report
from debacal.models import Calendar, Event
from debacal.store import EventOwnershipConflict, Store
from debacal.sync import RemoteResource, SyncEngine
from test_sync import setup as setup_transport, synchronize
from test_vdirsyncer import setup as setup_bridge, sync as sync_bridge


START = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)


def event(identifier='shared-id', calendar_id='local', uid='original-uid', **changes):
    return replace(Event(identifier, calendar_id, 'Existing owner', START,
                         START + timedelta(hours=1), ical_uid=uid), **changes)


@pytest.mark.parametrize('local_write', [False, True])
@pytest.mark.parametrize('new_calendar,new_uid', [('other', 'original-uid'), ('other', 'different-uid'),
                                                 ('local', 'different-uid')])
def test_identity_change_cannot_overwrite_the_owner_or_partially_write_a_batch(local_write, new_calendar, new_uid):
    original = event()
    with Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Other', '#123456'))
        store.apply('Original event', [original])
        history = store.history()
        write = (lambda values: store.apply('Invalid replacement', values)) if local_write else store.apply_remote
        with pytest.raises(EventOwnershipConflict, match='X-DEBACAL-ID'):
            write([event('healthy', uid='healthy-uid'),
                   replace(original, calendar_id=new_calendar, ical_uid=new_uid)])
        assert store.get_event(original.id) == original
        assert store.get_event('healthy') is None
        assert store.history() == history


def test_calendar_uid_owner_is_preserved_even_when_the_incoming_native_id_is_new():
    original = event()
    with Store(':memory:') as store:
        store.apply_remote([original])
        with pytest.raises(EventOwnershipConflict, match='UID'):
            store.apply_remote([replace(original, id='different-id', title='Duplicate UID')])
        assert store.list_events() == [original]


@pytest.mark.parametrize('calendar_id,uid', [('other', 'original-uid'), ('local', 'replaced-uid')])
def test_remote_delete_checks_calendar_and_reviewed_uid(calendar_id, uid):
    original = event()
    with Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Other', '#123456'))
        store.apply_remote([original])
        with pytest.raises(EventOwnershipConflict):
            store.apply_remote(delete_ids=[original.id], calendar_id=calendar_id,
                               expected_uids={original.id: uid})
        assert store.get_event(original.id) == original
        with pytest.raises(ValueError, match='calendar identity'):
            store.apply_remote(delete_ids=[original.id])
        assert store.get_event(original.id) == original


def test_deletion_inventory_cannot_delete_a_new_uid_created_by_another_connection(tmp_path):
    original = event()
    replacement = replace(original, ical_uid='new-resource')
    with Store(tmp_path / 'calendar.sqlite') as store, Store(tmp_path / 'calendar.sqlite') as racing:
        store.apply_remote([original])
        # A subscription deleted this old resource, then a different resource
        # reused its native ID before the first inventory committed its delete.
        racing.apply_remote(delete_ids=[original.id], calendar_id='local',
                            expected_uids={original.id: original.ical_uid})
        racing.apply_remote([replacement])
        with pytest.raises(EventOwnershipConflict):
            store.apply_remote(delete_ids=[original.id], calendar_id='local',
                               expected_uids={original.id: original.ical_uid})
        assert store.get_event(original.id) == replacement


@pytest.mark.parametrize('mutation', ['upsert', 'delete'])
def test_accepting_a_conflict_cannot_touch_another_calendar_or_discard_reviewed_edits(mutation):
    original = event()
    with Store(':memory:') as store:
        store.save_calendar(Calendar('remote', 'Remote', '#123456', kind='caldav'))
        reviewed = event('reviewed', 'remote', 'reviewed-uid')
        store.apply_remote([original])
        store.apply('Local edit to review', [reviewed])
        pending = store.pending_changes('remote')
        upserts = [replace(reviewed, title='Server choice')]
        delete_ids = []
        if mutation == 'upsert':
            upserts.append(replace(original, calendar_id='remote', title='Stolen owner'))
        else:
            delete_ids.append(original.id)
        with pytest.raises(EventOwnershipConflict):
            store.accept_remote('remote', reviewed.id, reviewed, [change['id'] for change in pending],
                                upserts=upserts, delete_ids=delete_ids)
        assert store.get_event(original.id) == original
        assert store.get_event(reviewed.id) == reviewed
        assert store.pending_changes('remote') == pending


def test_sync_state_and_deleted_resource_uid_reservations_cannot_be_stolen():
    original = event()
    with Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Other', '#123456'))
        store.apply_remote([original])
        with pytest.raises(EventOwnershipConflict):
            store.set_sync_state('other', original.id, 'https://calendar.invalid/stolen.ics', '1',
                                 {'event': replace(original, calendar_id='other').to_dict()})
        assert not store.list_sync_state('other')
        store.set_sync_state('local', original.id, 'https://calendar.invalid/original.ics', '1',
                             {'event': original.to_dict()})
        store.apply_remote(delete_ids=[original.id], calendar_id='local', expected_uids={original.id: original.ical_uid})
        with pytest.raises(EventOwnershipConflict, match='UID'):
            store.apply_remote([replace(original, id='new-id')])
        assert store.get_event('new-id') is None
        assert len(store.list_sync_state('local')) == 1


@pytest.mark.parametrize('same_uid', [False, True])
def test_subscription_quarantines_a_foreign_owner_and_continues_healthy_resources(tmp_path, monkeypatch, same_uid):
    path = tmp_path / 'calendar.sqlite'
    original = event()
    collision = replace(original, calendar_id='feed', ical_uid=original.ical_uid if same_uid else 'copied-uid')
    healthy = event('healthy', 'feed', 'healthy-uid')
    body = export_ics([collision, healthy])
    monkeypatch.setattr('debacal.sync.urlopen', lambda *_args, **_kwargs: io.BytesIO(body))
    with Store(path) as store:
        store.save_calendar(Calendar('feed', 'Feed', '#123456', kind='ics', url='https://calendar.invalid/feed.ics'))
        store.apply('Local original', [original])
        engine = SyncEngine(path)
        try:
            messages = synchronize(engine)
            assert store.get_event(original.id) == original
            assert store.get_event(healthy.id).ical_uid == healthy.ical_uid
            assert any(item.kind == 'issue' and 'X-DEBACAL-ID' in item.message for item in messages)
            assert not any(item.kind == 'error' for item in messages)
            rejected, = store.list_quarantined_resources('feed')
            assert collision.ical_uid in rejected['raw_ics']
        finally:
            engine.close()


def test_subscription_owner_check_and_write_share_a_cross_connection_transaction(tmp_path):
    path = tmp_path / 'calendar.sqlite'
    incoming = event(calendar_id='feed', uid='feed-uid')
    with Store(path) as store, Store(path) as racing:
        store.save_calendar(Calendar('feed', 'Feed', '#123456', kind='ics'))
        racing.connection.execute('PRAGMA busy_timeout=0')
        original_lookup = store.get_events
        observed = []
        def lookup(ids):
            result = original_lookup(ids)
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                racing.apply_remote([event()])
            observed.append(True)
            return result
        with patch.object(store, 'get_events', side_effect=lookup):
            report = store.refresh_subscription(parse_ics_report(export_ics([incoming]), 'feed'), 'feed')
        assert observed == [True]
        assert report.events[0].ical_uid == incoming.ical_uid
        assert store.get_event(incoming.id).calendar_id == 'feed'


@pytest.mark.parametrize('arrival', ['bootstrap', 'during-upload', 'after-inventory'])
@pytest.mark.parametrize('owner_remote', [False, True])
def test_vdir_quarantines_foreign_native_ids_including_arrivals_after_inventory(tmp_path, arrival, owner_remote):
    store, calendar, cli, backend = setup_bridge(tmp_path)
    try:
        if owner_remote:
            store.save_calendar(Calendar('foreign', 'Other account', '#123456', kind='caldav'))
        original = event(calendar_id='foreign' if owner_remote else 'local')
        collision = replace(original, calendar_id=calendar.id, ical_uid='server-uid')
        healthy = event('healthy', calendar.id, 'healthy-uid')
        if arrival != 'after-inventory':
            store.apply('Original local owner', [original])
        store.apply('Healthy upload', [healthy])
        def server_arrival():
            cli.remote[collision.ical_uid] = export_ics([collision]).decode()
        if arrival == 'during-upload':
            cli.before_push = server_arrival
        else:
            server_arrival()
        if arrival == 'after-inventory':
            identify_inventory = backend._identify_inventory
            with Store(store.path) as racing:
                injected = []
                def race(*args, **kwargs):
                    inventory = identify_inventory(*args, **kwargs)
                    if not injected:
                        racing.apply('Original arrived after inventory', [original])
                        injected.append(True)
                    return inventory
                with patch.object(backend, '_identify_inventory', side_effect=race):
                    result = sync_bridge(store, calendar, backend)
                assert injected == [True]
        else:
            result = sync_bridge(store, calendar, backend)
        assert store.get_event(original.id) == original
        assert store.get_event(healthy.id) == healthy
        assert not store.pending_changes(calendar.id)
        assert len(store.pending_changes('foreign')) == int(owner_remote)
        assert healthy.ical_uid in cli.remote and collision.ical_uid in cli.remote
        assert result.issues
        assert len(store.list_quarantined_resources(calendar.id)) == 1
    finally:
        store.close()


def test_direct_caldav_quarantines_foreign_native_ids_without_blocking_healthy_resources(tmp_path):
    store, transport, engine = setup_transport(tmp_path)
    try:
        original = event()
        collision = replace(original, calendar_id='remote', ical_uid='remote-uid')
        healthy = event('healthy', 'remote', 'healthy-uid')
        store.apply('Local owner', [original])
        for item in (collision, healthy):
            href = transport.new_href(item.id)
            transport.resources[href] = RemoteResource(href, 'server-version', export_ics([item]).decode())
        messages = synchronize(engine)
        assert store.get_event(original.id) == original
        assert store.get_event(healthy.id).ical_uid == healthy.ical_uid
        assert any(item.kind == 'issue' and 'X-DEBACAL-ID' in item.message for item in messages)
        assert not any(item.kind == 'error' for item in messages)
        assert len(store.list_quarantined_resources('remote')) == 1
        assert not store.get_sync_state('remote', original.id)
    finally:
        engine.close()
        store.close()
