"""Canonical write batches reuse private models without weakening source guards."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.event_range_index import EventRangeIndex
from debacal.models import Event
from debacal.store import Store


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def event(identifier='synthetic', **changes):
    return replace(Event(identifier, 'local', 'Synthetic appointment', START,
                         START + timedelta(hours=1)), **changes)


def titles(store):
    return [item.title for item in store.query_occurrences(START - timedelta(days=1),
                                                         START + timedelta(days=30))]


def test_current_write_model_is_reused_only_until_projection_maintenance_finishes():
    with Store(':memory:') as store:
        with store._transaction():
            store.apply('Create synthetic event', [event()])
            with patch.object(Event, 'from_dict', side_effect=AssertionError('Do not decode this write twice')):
                store._maintain_query_projections()
            assert all(not frame['written'] for frame in store._query_transactions)
        assert titles(store) == ['Synthetic appointment']
        assert not store._query_transactions


def test_later_direct_sql_change_must_not_reuse_an_earlier_model():
    with Store(':memory:') as store:
        source = event()
        with store._transaction():
            store.apply('Create synthetic event', [source])
            changed = replace(source, title='Changed through SQL').to_dict()
            store.connection.execute('UPDATE events SET data=? WHERE id=?',
                                     (store._json(changed), source.id))
            with patch.object(Event, 'from_dict', wraps=Event.from_dict) as decode:
                store._maintain_query_projections()
            assert decode.call_count == 1
        assert titles(store) == ['Changed through SQL']


def test_released_savepoint_uses_the_latest_private_model():
    with Store(':memory:') as store:
        with store._transaction():
            source = event()
            store.apply('Outer source', [source])
            with store._transaction():
                store.apply('Inner replacement', [replace(source, title='Latest value')])
            with patch.object(Event, 'from_dict', side_effect=AssertionError('Released model is available')):
                store._maintain_query_projections()
        assert titles(store) == ['Latest value']


def test_rolled_back_savepoint_discards_its_model_and_retains_parent_write():
    with Store(':memory:') as store:
        with store._transaction():
            source = event()
            store.apply('Outer source', [source])
            with pytest.raises(ValueError, match='Rollback inner'):
                with store._transaction():
                    store.apply('Inner replacement', [replace(source, title='Rolled back')])
                    raise ValueError('Rollback inner')
        assert titles(store) == ['Synthetic appointment']
        assert store.get_event(source.id).title == source.title


def test_rollback_after_inner_projection_maintenance_recovers_canonical_parent_source():
    with Store(':memory:') as store:
        source = event()
        with store._transaction():
            store.apply('Outer source', [source])
            with pytest.raises(ValueError, match='Rollback prepared child'):
                with store._transaction():
                    store.apply('Inner replacement', [replace(source, title='Rolled back')])
                    store._maintain_query_projections()
                    raise ValueError('Rollback prepared child')
        assert titles(store) == ['Synthetic appointment']


def test_outer_rollback_leaves_no_captured_models_or_changed_projection():
    with Store(':memory:') as store:
        source = event()
        store.apply('Original', [source])
        with pytest.raises(ValueError, match='Rollback outer'):
            with store._transaction():
                store.apply('Temporary', [replace(source, title='Rolled back')])
                store._maintain_query_projections()
                raise ValueError('Rollback outer')
        assert titles(store) == ['Synthetic appointment']
        assert not store._query_transactions


def test_caller_mutation_after_apply_cannot_change_the_private_handoff():
    key = (START + timedelta(days=7)).isoformat()
    detached = event('synthetic/second', series_id='synthetic', recurrence_id=key,
                     start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1))
    source = event(recurrence_dates=(key,), recurrence_overrides={key: detached.to_dict()})
    with Store(':memory:') as store:
        with store._transaction():
            store.apply('Capture independent source', [source])
            source.recurrence_overrides[key]['title'] = 'Changed only in caller'
        assert titles(store) == ['Synthetic appointment', 'Synthetic appointment']
        assert store.get_event(source.id).recurrence_overrides[key]['title'] == 'Synthetic appointment'


def test_remote_replace_never_decodes_an_unused_old_event():
    with Store(':memory:') as store:
        source = event()
        store.apply_remote([source])
        with patch.object(store, 'get_event', wraps=store.get_event) as read:
            store.apply_remote([replace(source, title='Remote replacement')])
        assert not read.called
        assert titles(store) == ['Remote replacement']


def test_local_apply_reuses_its_before_snapshot_for_enqueue_state():
    with Store(':memory:') as store:
        source = event()
        store.apply('Original', [source])
        with patch.object(store, 'get_event', wraps=store.get_event) as read:
            store.apply('Edited', [replace(source, title='Local replacement')])
        assert not read.called
        assert titles(store) == ['Local replacement']
        store.undo()
        assert titles(store) == ['Synthetic appointment']


def test_undo_keeps_the_latest_transport_template():
    first = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-SYNTHETIC:original\r\nEND:VCALENDAR\r\n'
    refreshed = first.replace('original', 'server refresh')
    with Store(':memory:') as store:
        source = event(raw_ics=first)
        store.apply_remote([source])
        changed = replace(source, title='Edited')
        store.apply('Local edit', [changed])
        store.apply_remote([replace(changed, raw_ics=refreshed)])
        store.undo()
        restored = store.get_event(source.id)
        assert restored.title == source.title
        assert restored.raw_ics == refreshed


def test_projection_failure_still_retains_the_canonical_transport_source():
    raw = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-SYNTHETIC:retained\r\nEND:VCALENDAR\r\n'
    with Store(':memory:') as store:
        with store._transaction():
            store.apply('Synthetic source', [event(raw_ics=raw)])
            with patch.object(EventRangeIndex, 'prepare', side_effect=ValueError('Synthetic preparation error')):
                store._maintain_query_projections()
        retained, = store.list_quarantined_resources()
        assert retained['raw_ics'] == raw
        assert 'Synthetic preparation error' in retained['reason']
