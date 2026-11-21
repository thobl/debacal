"""Sidebar grouping is a local choice, independent of visibility and syncing."""
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from debacal.models import Calendar
from debacal.store import Store
from test_calendar_visibility_contention import synthetic, while_writer_waits


def ids(store, **options):
    return [calendar.id for calendar in store.list_calendars(**options)]


def test_existing_descriptors_default_to_main_list_and_new_choices_persist(tmp_path):
    old = {'id': 'work', 'name': 'Work', 'color': '#527365'}
    assert Calendar.from_dict(old).in_main_list
    path = tmp_path / 'testing.sqlite3'
    with Store(path) as store:
        store.save_calendar(Calendar.from_dict(old))
        store.save_calendar(Calendar('occasional', 'Occasional', '#839272', in_main_list=False))
        assert store.get_setting('calendar_main_list') is None
        assert ids(store) == ['local', 'work', 'occasional']
        store.set_calendar_in_main_list('work', False)
        assert not store.get_calendar('work').in_main_list
    with Store(path) as store, Store(tmp_path / 'other.sqlite3') as other:
        assert not store.get_calendar('work').in_main_list
        assert not store.get_calendar('occasional').in_main_list
        assert other.get_calendar('local').in_main_list
    with Store(':memory:') as first, Store(':memory:') as second:
        first.set_calendar_in_main_list('local', False)
        assert second.get_calendar('local').in_main_list


@pytest.mark.parametrize('value', [0, 1, None, '', 'false', [], {}])
def test_main_list_flag_requires_a_boolean(value):
    with pytest.raises(ValueError, match='main-list'):
        Calendar('bad', 'Bad setting', '#527365', in_main_list=value)
    with Store(':memory:') as store:
        with pytest.raises(ValueError, match='main-list'):
            store.set_calendar_in_main_list('local', value)
        assert store.get_calendar('local').in_main_list


def test_grouping_is_independent_of_event_visibility_source_enabled_state_and_default():
    with Store(':memory:') as store:
        store.save_calendar(Calendar('work', 'Work', '#527365', visible=False))
        store.save_calendar(Calendar('off', 'Disconnected', '#839272', enabled=False, in_main_list=False))
        store.set_default_calendar('local')
        store.set_calendar_in_main_list('local', False)
        assert ids(store) == ['work', 'local']
        assert ids(store, include_disabled=True) == ['work', 'local', 'off']
        assert store.get_calendar('local').visible
        assert not store.get_calendar('work').visible
        assert not store.get_calendar('off').enabled
        assert store.get_default_calendar().id == 'local'
        store.set_calendar_visible('local', False)
        assert not store.get_calendar('local').in_main_list
        store.set_calendar_in_main_list('work', False)
        assert not store.get_calendar('work').visible


def test_layout_keeps_group_order_and_preserves_omitted_disabled_calendars():
    with Store(':memory:') as store:
        for identity in ('first', 'second', 'third'):
            store.save_calendar(Calendar(identity, identity, '#527365'))
        store.save_calendar(Calendar('off', 'Disconnected', '#527365', enabled=False, in_main_list=False))
        store.set_calendar_layout(['third', 'second', 'first', 'local'], ['second', 'first'])
        assert ids(store) == ['second', 'first', 'third', 'local']
        assert store.get_setting('calendar_order') == ['third', 'second', 'first', 'local', 'off']
        assert not store.get_calendar('off').in_main_list
        store.set_calendar_order(['local', 'third', 'first', 'second'])
        assert ids(store) == ['first', 'second', 'local', 'third']
        store.save_calendar(Calendar('new', 'New calendar', '#527365'))
        assert ids(store) == ['first', 'second', 'new', 'local', 'third']
        store.set_calendar_layout(['third'], ['third'])
        assert ids(store) == ['third', 'first', 'second', 'new', 'local']
        assert not store.get_calendar('local').in_main_list


def test_layout_supports_empty_groups_and_empty_requested_set():
    with Store(':memory:') as store:
        store.set_calendar_layout(['local'], [])
        assert not store.get_calendar('local').in_main_list
        store.set_calendar_layout([], [])
        assert not store.get_calendar('local').in_main_list
        store.set_calendar_layout(['local'], ['local'])
        assert store.get_calendar('local').in_main_list


@pytest.mark.parametrize('order,main', [
    ('local', []), (None, []), (['local', 'local'], []), ([None], []), ([[]], []),
    (['missing'], []), (['local'], 'local'), (['local'], None), (['local'], ['local', 'local']),
    (['local'], [None]), (['local'], [[]]), (['local'], ['other']),
])
def test_invalid_layout_never_partially_changes_order_or_membership(order, main):
    with Store(':memory:') as store:
        with pytest.raises(ValueError):
            store.set_calendar_layout(order, main)
        assert store.get_setting('calendar_order') is None
        assert store.get_setting('calendar_main_list') is None
        assert store.get_calendar('local').in_main_list


def test_missing_calendar_does_not_create_a_stale_membership_override():
    with Store(':memory:') as store:
        with pytest.raises(ValueError, match='not found'):
            store.set_calendar_in_main_list('missing', False)
        assert store.get_setting('calendar_main_list') is None


def test_layout_rolls_back_order_if_membership_persistence_fails():
    with Store(':memory:') as store:
        save = store.set_setting
        def fail_membership(key, value):
            if key == 'calendar_main_list':
                raise RuntimeError('Synthetic UI-state failure')
            save(key, value)
        with patch.object(store, 'set_setting', side_effect=fail_membership):
            with pytest.raises(RuntimeError, match='Synthetic'):
                store.set_calendar_layout(['local'], [])
        assert store.get_setting('calendar_order') is None
        assert store.get_setting('calendar_main_list') is None


def test_grouping_and_reordering_finish_while_sync_holds_the_event_writer(tmp_path):
    path = tmp_path / 'testing.sqlite3'
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.save_calendar(Calendar('work', 'Work', '#527365'))
        event = synthetic()
        reader.apply_remote([event])
        reader.configure_ui_access(Mock())
        revision, changes = reader.events_revision(), reader.connection.total_changes
        descriptor = reader.connection.execute('SELECT data FROM calendars WHERE id=?', ('local',)).fetchone()[0]
        def choose():
            reader.set_calendar_in_main_list('local', False)
            reader.set_calendar_layout(['local', 'work'], ['work'])
            assert ids(reader) == ['work', 'local']
            assert not reader.get_calendar('local').in_main_list
            assert reader.events_revision() == revision
            assert reader.connection.total_changes == changes
            assert reader.connection.execute('SELECT data FROM calendars WHERE id=?', ('local',)).fetchone()[0] == descriptor
        while_writer_waits(writer, choose, event)


def test_sync_descriptor_updates_cannot_undo_sidebar_placement():
    with Store(':memory:') as store:
        original = store.get_calendar('local')
        store.set_calendar_in_main_list('local', False)
        store.save_calendar(replace(original, name='Refreshed server name', color='#123456'))
        current = store.get_calendar('local')
        assert not current.in_main_list
        assert current.name == 'Refreshed server name' and current.color == '#123456'


def test_preferences_only_replace_explicit_group_changes_and_keep_latest_metadata():
    with Store(':memory:') as store:
        original = store.get_calendar('local')
        store.set_calendar_in_main_list('local', False)
        store.save_calendar(replace(original, color='#123456'))
        assert store.apply_calendar_preferences({'local': original}, [replace(original, name='Alias')])
        current = store.get_calendar('local')
        assert current.name == 'Alias' and current.color == '#123456' and not current.in_main_list
        assert store.apply_calendar_preferences({'local': current}, [replace(current, in_main_list=True)])
        updated = store.get_calendar('local')
        assert updated.in_main_list and updated.color == '#123456' and updated.name == 'Alias'


def test_rejected_preferences_never_leak_main_list_changes_or_partial_source_edits():
    with Store(':memory:') as store:
        original = store.get_calendar('local')
        invalid_original = Calendar('gone', 'Removed', '#527365')
        with pytest.raises(ValueError, match='changed while Preferences'):
            store.apply_calendar_preferences({'local': original, 'gone': invalid_original}, [
                replace(original, in_main_list=False, name='Pending alias'), invalid_original])
        assert store.get_calendar('local') == original
        assert store.get_setting('calendar_main_list') is None


def test_malformed_local_overrides_are_ignored_and_can_be_replaced():
    with Store(':memory:') as store:
        store.set_setting('calendar_main_list', {'local': 'false', 'unused': False})
        assert store.get_calendar('local').in_main_list
        assert store.list_calendars()[0].in_main_list
        store.set_setting('calendar_main_list', ['invalid map'])
        store.set_calendar_in_main_list('local', False)
        assert store.get_setting('calendar_main_list') == {'local': False}
