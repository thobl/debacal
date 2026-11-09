"""Foreground queries and profile choices never compete for sync's writer."""
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import Mock, patch

import pytest

from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.selection import capture_selection
from debacal.store import HistoryConflict, Store
from debacal.ui_state import UI_SETTINGS, ui_state_path
from test_calendar_visibility_contention import START, synthetic, while_writer_waits


def test_all_profile_ui_choices_are_durable_without_acquiring_the_event_writer(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.save_calendar(Calendar('work', 'Work', '#527365'))
        source = synthetic()
        reader.apply_remote([source])
        changes = reader.connection.total_changes
        def choose():
            with reader.ui_transaction():
                for key in UI_SETTINGS:
                    reader.set_setting(key, {'synthetic': key})
                reader.set_default_calendar('work')
                reader.set_calendar_order(['work', 'local'])
            Preferences.load(reader).save(reader)
            assert reader.get_default_calendar().id == 'work'
            assert [value.id for value in reader.list_calendars()] == ['work', 'local']
            assert reader.connection.total_changes == changes
        while_writer_waits(writer, choose, source)
    with Store(path) as reopened:
        assert reopened.get_default_calendar().id == 'work'
        assert [value.id for value in reopened.list_calendars()] == ['work', 'local']
        assert reopened.get_setting('selection_mode') == {'synthetic': 'selection_mode'}


def test_ui_transactions_roll_back_all_choices_together(tmp_path):
    with Store(tmp_path / 'calendar.sqlite3') as store:
        with pytest.raises(ValueError, match='Abort synthetic choices'):
            with store.ui_transaction():
                store.set_setting('show_full_day', True)
                store.set_calendar_visible('local', False)
                raise ValueError('Abort synthetic choices')
        assert store.get_setting('show_full_day') is None
        assert store.get_calendar('local').visible


def test_old_profile_ui_choices_are_migrated_once_and_metadata_cannot_clobber_them(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as legacy:
        legacy.save_calendar(replace(legacy.get_calendar('local'), visible=False))
        legacy.connection.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', [
            ('selection_mode', json.dumps('future')),
            ('calendar_order', json.dumps(['local'])),
            ('event_query_indexes_v1', json.dumps(True)),
        ])
        legacy.connection.commit()
    # Simulate a profile written before the UI-state database was introduced.
    ui_path = tmp_path / 'calendar.sqlite3.ui.sqlite3'
    ui_path.unlink()
    with Store(path) as migrated:
        assert not migrated.get_calendar('local').visible
        assert migrated.get_setting('selection_mode') == 'future'
        assert migrated._ui_state.get('event_query_indexes_v1') == (False, None)
        migrated.save_calendar(replace(migrated.get_calendar('local'), visible=True))
        assert not migrated.get_calendar('local').visible
        migrated.set_setting('selection_mode', 'group')
        migrated.set_calendar_visible('local', True)
    with Store(path) as reopened:
        assert reopened.get_calendar('local').visible
        assert reopened.get_setting('selection_mode') == 'group'


def test_new_profiles_do_not_invent_visibility_overrides_for_calendar_descriptors(tmp_path):
    path = tmp_path / 'new.sqlite3'
    with Store(path) as store:
        store.save_calendar(replace(store.get_calendar('local'), visible=False))
        assert not store.get_calendar('local').visible
    with Store(path) as reopened:
        reopened.save_calendar(replace(reopened.get_calendar('local'), visible=True))
        assert reopened.get_calendar('local').visible


def test_ui_state_is_isolated_for_memory_profiles_and_full_database_filenames(tmp_path):
    assert ui_state_path(str(tmp_path / 'calendar.db')) != ui_state_path(str(tmp_path / 'calendar.sqlite3'))
    with Store(':memory:') as first, Store(':memory:') as second:
        first.set_setting('selection_mode', 'future')
        first.set_calendar_visible('local', False)
        assert second.get_setting('selection_mode') is None
        assert second.get_calendar('local').visible


def test_foreground_connection_fails_fast_while_normal_writer_keeps_busy_timeout(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.configure_ui_access(Mock())
        assert reader.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 0
        assert writer.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000


def test_foreground_query_defers_stale_preparation_without_claiming_the_writer(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    first = synthetic()
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.apply_remote([first])
        requests = []
        reader.configure_ui_access(requests.append)
        # An older client/direct SQL operation can commit without maintaining
        # derived preparations. Foreground reads retain their coherent index.
        updated = replace(first, title='Written by an older client')
        writer._write_event(updated)
        writer.connection.commit()
        before = reader.connection.total_changes
        def query():
            found = reader.query_occurrences(START, START + timedelta(days=1))
            assert [value.title for value in found] == [first.title]
            assert requests == [None]
            assert reader.connection.total_changes == before
        while_writer_waits(writer, query, updated)
        writer.maintain_queries()
        found = reader.query_occurrences(START, START + timedelta(days=1))
        assert [value.title for value in found] == ['Received during sync']


def test_damaged_foreground_delta_keeps_the_entire_previous_snapshot_until_repair(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    first, damaged, deleted = synthetic('first'), synthetic('damaged'), synthetic('deleted')
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.apply_remote([first, damaged, deleted])
        requests = []
        reader.configure_ui_access(requests.append)
        writer.apply_remote([replace(first, title='New first'), replace(damaged, title='New damaged')],
                            [deleted.id], calendar_id='local')
        writer.connection.execute("UPDATE event_query_projection SET data=X'000102' WHERE event_id='damaged'")
        writer.connection.commit()
        def query():
            found = reader.query_occurrences(START, START + timedelta(days=1))
            assert {value.id: value.title for value in found} == {
                first.id: first.title, damaged.id: damaged.title, deleted.id: deleted.title}
            assert requests == [{'damaged'}]
        while_writer_waits(writer, query, first)
        writer.maintain_queries({'damaged'})
        found = reader.query_occurrences(START, START + timedelta(days=1))
        assert {value.id: value.title for value in found} == {
            first.id: 'Received during sync', damaged.id: 'New damaged'}


def test_copy_and_selection_previews_use_read_snapshots_during_sync(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    master = replace(synthetic(), recurrence_rule='FREQ=DAILY;COUNT=4')
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.apply_remote([master])
        occurrences = reader.query_occurrences(START, START + timedelta(days=4))
        reader.configure_ui_access(Mock())
        def preview():
            clipboard = capture_selection(reader, [occurrences[1]], 'future')
            assert clipboard.masters
            tail = reader.future_snapshot(occurrences[1])
            assert tail.start == occurrences[1].start
            copied = reader.move_selection([occurrences[1]], 'future', occurrences[1],
                occurrences[1].start + timedelta(days=7), occurrences[1].end + timedelta(days=7), copy=True)
            assert copied and not reader.history()
        while_writer_waits(writer, preview, master)


@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('damage', ['unmaintained', 'damaged'])
def test_stale_presentation_cannot_validate_a_mutation_of_changed_canonical_data(tmp_path, recurring, damage):
    from debacal.event_range_index import EventRangeIndex
    path = tmp_path / 'calendar.sqlite3'
    source = replace(synthetic(), recurrence_rule='FREQ=DAILY;COUNT=4' if recurring else '')
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.apply_remote([source, synthetic('unrelated')])
        reviewed = next(value for value in reader.query_occurrences(START, START + timedelta(days=1))
                        if (value.series_id or value.id) == source.id)
        reader.configure_ui_access(Mock())
        changed = replace(source, title='Changed since this occurrence was reviewed')
        if damage == 'unmaintained':
            writer._write_event(changed)
        else:
            writer.apply_remote([changed])
            writer.connection.execute("UPDATE event_query_projection SET data=X'000102' WHERE event_id=?", (source.id,))
        writer.connection.commit()
        with patch.object(EventRangeIndex, 'prepare', wraps=EventRangeIndex.prepare) as prepared:
            with pytest.raises(HistoryConflict, match='changed while it was being edited'):
                reader.delete_selection([reviewed], 'single')
        assert reader.get_event(source.id).title == changed.title
        assert not reader.history()
        # Ordinary events need no preparation; recurring review must seek only
        # this master instead of rebuilding the profile or trusting stale data.
        assert all(call.args[0].id == source.id for call in prepared.call_args_list)
        assert prepared.call_count == int(recurring)
