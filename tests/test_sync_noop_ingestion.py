"""Unchanged remote ingestion avoids work without weakening source authority."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.models import Calendar, Event
from debacal.recurrence import occurrence_id, recurrence_key
from debacal.store import EventOwnershipConflict, HistoryConflict, Store
from debacal.vdirsyncer_backend import VdirsyncerBackend, semantic


START = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)


def source(**changes):
    return replace(Event('source', 'remote', 'Synthetic appointment', START,
                         START + timedelta(hours=1), raw_ics='Synthetic retained source'), **changes)


@pytest.fixture
def store():
    with Store(':memory:') as value:
        value.save_calendar(Calendar('remote', 'Synthetic remote', '#123456', kind='caldav',
                                     url='https://calendar.example.test/collection/'))
        yield value


def canonical_family():
    overrides = {}
    for index in range(1, 50):
        start = START + timedelta(days=index)
        key = recurrence_key(start)
        overrides[key] = source(id=occurrence_id('source', key), start=start,
                                end=start + timedelta(hours=1), series_id='source',
                                recurrence_id=key).to_dict()
    return source(recurrence_rule='FREQ=DAILY;COUNT=400', recurrence_overrides=overrides)


def test_matching_inventory_identity_borrows_a_validated_family_without_override_reconstruction():
    original = canonical_family()
    with patch.object(Event, 'from_dict', side_effect=AssertionError('Reconstructed unchanged overrides')):
        assert VdirsyncerBackend._identify(original, original.id, validated=True) is original


def test_same_identity_without_inventory_validation_still_normalizes_override_fields():
    original = canonical_family()
    key = next(iter(original.recurrence_overrides))
    payloads = {identity: dict(value) for identity, value in original.recurrence_overrides.items()}
    payloads[key]['start'] = payloads[key]['start'].removesuffix('+00:00') + '+0000'
    value = replace(original, recurrence_overrides=payloads)
    expected = Event.from_dict(payloads[key]).to_dict()
    actual = VdirsyncerBackend._identify(value, original.id)
    assert actual.recurrence_overrides[key] == expected
    assert actual is not value
    assert value.recurrence_overrides[key]['start'] == payloads[key]['start']


@pytest.mark.parametrize('field', ['id', 'series_id'])
def test_identity_mismatch_keeps_full_override_normalization(field):
    original = canonical_family()
    key = next(iter(original.recurrence_overrides))
    payloads = {identity: dict(value) for identity, value in original.recurrence_overrides.items()}
    payloads[key][field] = 'old-identity'
    value = replace(original, recurrence_overrides=payloads)
    with patch.object(Event, 'from_dict', wraps=Event.from_dict) as restored:
        actual = VdirsyncerBackend._identify(value, original.id)
    assert restored.call_count == len(payloads)
    assert actual is not value
    for identity, payload in actual.recurrence_overrides.items():
        assert payload['id'] == occurrence_id(original.id, identity)
        assert payload['series_id'] == original.id
    assert value.recurrence_overrides[key][field] == 'old-identity'


def test_changed_source_identity_keeps_uid_and_independent_override_payloads():
    original = canonical_family()
    actual = VdirsyncerBackend._identify(original, 'new-source')
    assert actual.id == 'new-source' and actual.ical_uid == original.ical_uid
    for identity, payload in actual.recurrence_overrides.items():
        assert payload['id'] == occurrence_id(actual.id, identity)
        assert payload['series_id'] == actual.id
        assert original.recurrence_overrides[identity]['series_id'] == original.id


@pytest.mark.parametrize('explicit_uid', [False, True])
def test_baseline_uid_projection_does_not_restore_unrelated_schedule_payloads(explicit_uid):
    original = canonical_family()
    payload = original.to_dict()
    if not explicit_uid:
        payload.pop('ical_uid')
    state = {'event_id': original.id, 'calendar_id': original.calendar_id,
             'base_payload': {'event': payload}}
    with patch.object(Event, 'from_dict', side_effect=AssertionError('UID inventory does not need a model')):
        assert VdirsyncerBackend._state_uid(state) == original.ical_uid


def test_incomplete_or_inconsistent_baseline_identity_retains_normalization_fallback():
    original = source()
    state = {'event_id': original.id, 'calendar_id': 'old-calendar',
             'base_payload': {'event': original.to_dict()}}
    with patch.object(Event, 'from_dict', wraps=Event.from_dict) as restored:
        assert VdirsyncerBackend._state_uid(state) == original.ical_uid
        assert restored.call_count == 1
    assert VdirsyncerBackend._state_uid({'base_payload': {}}) is None


def test_identical_remote_source_skips_revalidation_and_restoration(store):
    original = canonical_family()
    store.apply_remote([original], calendar_id='remote')
    revision = store.events_revision()
    with patch.object(Event, 'from_dict', side_effect=AssertionError('Decoded identical source')), \
         patch.object(store, '_restore', side_effect=AssertionError('Restored identical source')), \
         patch.object(store, '_assert_remote_owners', wraps=store._assert_remote_owners) as ownership:
        store.apply_remote([original], calendar_id='remote')
    assert ownership.call_count == 1
    assert store.events_revision() == revision


def test_raw_transport_changes_are_persisted_even_when_visible_content_is_identical(store):
    original = source()
    store.apply_remote([original])
    replacement = replace(original, raw_ics='New transport fields from the server')
    store.apply_remote([replacement], calendar_id='remote')
    assert store.get_event(original.id).to_dict() == replacement.to_dict()


@pytest.mark.parametrize('column,value', [('start', START.timestamp() + 7200),
                                         ('end', START.timestamp() + 10800),
                                         ('recurring', 1), ('group_id', 'old-group'),
                                         ('candidate', 1), ('search_text', 'stale search text')])
def test_identical_source_repairs_outdated_derived_event_columns(store, column, value):
    original = source()
    store.apply_remote([original])
    store.connection.execute(f'UPDATE events SET {column}=? WHERE id=?', (value, original.id))
    store.connection.commit()
    with patch.object(store, '_restore', wraps=store._restore) as restored:
        store.apply_remote([original])
    assert restored.call_count == 1
    row = store.connection.execute('SELECT * FROM events WHERE id=?', (original.id,)).fetchone()
    expected = {'start': original.start.timestamp(), 'end': original.end.timestamp(),
                'recurring': original.recurring, 'group_id': original.group_id,
                'candidate': original.candidate, 'search_text': store._search_projection(original)}
    assert row[column] == expected[column]


def test_identical_remote_source_still_respects_pending_local_edits(store):
    original = source()
    store.apply('Create local event', [original])
    with patch.object(store, '_restore', side_effect=AssertionError('Bypassed pending edit guard')):
        with pytest.raises(HistoryConflict, match='pending'):
            store.apply_remote([original], calendar_id='remote')
    assert store.pending_changes('remote')


def test_identical_remote_source_still_requires_the_synchronized_calendar(store):
    original = source()
    store.apply_remote([original])
    with pytest.raises(EventOwnershipConflict, match='synchronized calendar'):
        store.apply_remote([original], calendar_id='other-calendar')


def test_mixed_remote_batch_restores_only_changed_sources(store):
    first, second = source(), source(id='second', ical_uid='second')
    store.apply_remote([first, second])
    edited = replace(second, title='Changed remotely')
    with patch.object(store, '_restore', wraps=store._restore) as restored:
        store.apply_remote([first, edited])
    assert list(restored.call_args.args[0]) == [second.id]
    assert store.get_event(first.id) == first
    assert store.get_event(second.id) == edited


@pytest.mark.parametrize('missing', [False, True], ids=['failed-projection', 'missing-projection'])
def test_failed_derived_projection_is_retried_for_an_identical_canonical_source(store, missing):
    original = source()
    store.apply_remote([original])
    if missing:
        store.connection.execute('DELETE FROM event_query_projection WHERE event_id=?', (original.id,))
    else:
        store.connection.execute('UPDATE event_query_projection SET data=NULL WHERE event_id=?', (original.id,))
    store.connection.commit()
    revision = store.events_revision()
    with patch.object(store, '_restore', wraps=store._restore) as restored:
        store.apply_remote([original])
    assert restored.call_count == 1
    assert store.events_revision() > revision
    row = store.connection.execute('SELECT data FROM event_query_projection WHERE event_id=?', (original.id,)).fetchone()
    assert row[0] is not None


def test_identical_sync_metadata_skips_writes_after_ownership_checks(store):
    original = canonical_family()
    store.apply_remote([original])
    VdirsyncerBackend._record(store, original.calendar_id, original, 'Synthetic server ICS')
    changes = store.connection.total_changes
    with patch.object(store, 'set_sync_state', side_effect=AssertionError('Rewrote identical metadata')), \
         patch.object(store, '_assert_identity_owner', wraps=store._assert_identity_owner) as ownership:
        VdirsyncerBackend._record(store, original.calendar_id, original, 'Synthetic server ICS')
    assert ownership.call_count == 1
    assert store.connection.total_changes == changes


def test_identical_sync_metadata_cannot_bypass_a_new_ownership_conflict(store):
    original = source()
    store.apply_remote([original])
    VdirsyncerBackend._record(store, original.calendar_id, original, 'Synthetic server ICS')
    with patch.object(store, '_assert_identity_owner', side_effect=EventOwnershipConflict('New owner')):
        with pytest.raises(EventOwnershipConflict, match='New owner'):
            VdirsyncerBackend._record(store, original.calendar_id, original, 'Synthetic server ICS')


def test_changed_transport_baseline_and_stale_semantic_hash_are_repaired(store):
    original = source()
    store.apply_remote([original])
    VdirsyncerBackend._record(store, original.calendar_id, original, 'Old server ICS')
    stale = store.get_sync_state(original.calendar_id, original.id)
    stale['etag'] = 'stale semantic fingerprint'
    store.set_sync_metadata(original.calendar_id, original.id, stale)
    VdirsyncerBackend._record(store, original.calendar_id, original, 'New server ICS')
    actual = store.get_sync_state(original.calendar_id, original.id)
    assert actual['base_payload']['ics'] == 'New server ICS'
    assert actual['etag'] == semantic(original)
