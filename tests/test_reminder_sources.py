"""Read scheduling inputs in bounded, consistent snapshots of the canonical store."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from alarm_support import BASE, alarm_event
from debacal import alarm_ics
from debacal.event_range_index import EventRangeIndex
from debacal.models import Calendar
from debacal.query_projection import ProjectionStorage, pack_state, unpack_state
from debacal.reminder_sources import ReminderSources
from debacal.store import Store


def event(identity, **changes):
    return alarm_event(id=identity, ical_uid=identity, calendar_id='local', **changes)


def test_changes_include_tombstones_and_interleaved_scan_writes(tmp_path):
    path = tmp_path / 'example #?.sqlite3'
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        writer.save_calendar(Calendar('other', 'Other', '#abc'))
        writer.apply_remote([event('b'), event('c'), event('e')])
        high_water = reader.revision()
        first, = reader.scan(limit=1)
        assert first.event_id == 'b'
        found = {first.event_id: first.event.title}
        writer.apply_remote([event('a'), event('b', title='Intermediate'), event('f')], ['c'], calendar_id='local')
        writer.apply_remote([event('b', title='Edited'), replace(event('f'), calendar_id='other', id='g', ical_uid='g')])
        # A direct ID change journals both keys at the same revision.
        writer.connection.execute(
            "UPDATE events SET id='d',data=json_set(data,'$.id','d') WHERE id='e'")
        writer.connection.commit()
        remaining = reader.scan(after_id='b')
        assert [item.event_id for item in remaining] == ['d', 'f', 'g']
        found.update((item.event_id, item.event.title) for item in remaining)
        replayed = []
        cursor = high_water, ''
        while page := reader.changes(cursor, limit=1):
            item, = page
            assert (item.version, item.event_id) > cursor
            cursor = item.version, item.event_id
            replayed.append(cursor)
            if item.event is None:
                assert item.indexed is None and reader.source_stamp(item.event_id) is None
                found.pop(item.event_id, None)
            else:
                assert item.version == reader.source_stamp(item.event_id)
                found[item.event_id] = item.event.title
        assert replayed[-2:] == [(reader.revision(), 'd'), (reader.revision(), 'e')]
        assert len([key for _, key in replayed if key == 'b']) == 1
        assert {key for _, key in replayed} >= {'a', 'b', 'c', 'd', 'e', 'f', 'g'}
        assert found == {'a': 'Example', 'b': 'Edited', 'd': 'Example', 'f': 'Example', 'g': 'Example'}
        assert [row.event_id for row in reader.scan(calendar_id='other')] == ['g']
        assert [row.event_id for row in reader.scan(calendar_id='local', after_id='b', limit=1)] == ['d']
        assert reader.scan(limit=0) == ()
        with pytest.raises(ValueError):
            reader.changes((0, ''), limit=-1)


@pytest.mark.parametrize('damage', ['none', 'stale', 'missing', 'bad blob', 'wrong source', 'signature'])
def test_only_selected_source_is_hydrated_and_prepared_when_needed(tmp_path, damage):
    path = tmp_path / 'profile'
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        writer.apply_remote([event('a'), event('b'), event('c')])
        high = reader.revision()
        writer.apply_remote([event('b', title='New title', recurrence_rule='FREQ=DAILY;COUNT=3')])
        if damage == 'stale':
            writer.connection.execute("UPDATE events SET data=json_set(data,'$.title','Newer title') WHERE id='b'")
        elif damage == 'missing':
            writer.connection.execute("DELETE FROM event_query_projection WHERE event_id='b'")
        elif damage == 'bad blob':
            writer.connection.execute("UPDATE event_query_projection SET data=X'00' WHERE event_id='b'")
        elif damage == 'wrong source':
            writer.connection.execute("UPDATE event_query_projection SET data=(SELECT data FROM event_query_projection WHERE event_id='a') WHERE event_id='b'")
        elif damage == 'signature':
            writer.connection.execute("UPDATE event_query_state SET signature='obsolete'")
        # The full projection can be behind, while b's individual record remains current.
        writer.connection.execute("UPDATE events SET data=json_set(data,'$.title','Later') WHERE id='c'")
        writer.connection.commit()
        with patch.object(alarm_ics, 'hydrate_alarms', wraps=alarm_ics.hydrate_alarms) as hydrated, \
                patch.object(EventRangeIndex, 'prepare', wraps=EventRangeIndex.prepare) as prepared, \
                patch.object(ProjectionStorage, 'load_catalog', side_effect=AssertionError('global catalog read')):
            item, = reader.changes((high, 'z'), limit=1)
        assert item.event_id == 'b'
        assert item.event.title == item.indexed.master.title == ('Newer title' if damage == 'stale' else 'New title')
        assert len(item.indexed.occurrences) == 3
        assert [call.args[0].id for call in hydrated.call_args_list] == ['b']
        assert [call.args[0].id for call in prepared.call_args_list] == ([] if damage == 'none' else ['b'])


def test_legacy_canonical_alarms_are_hydrated_beside_existing_preparation(tmp_path):
    path = tmp_path / 'profile'
    raw = ('BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:older\nDTSTART:20261121T090000Z\n'
           'DTEND:20261121T100000Z\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT5M\n'
           'END:VALARM\nEND:VEVENT\nEND:VCALENDAR')
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        writer.apply_remote([event('older', raw_ics=raw)])
        with writer._transaction():
            writer.connection.execute("UPDATE events SET data=json_remove(data,'$.alarms')")
        blob, = writer.connection.execute('SELECT data FROM event_query_projection').fetchone()
        state = unpack_state(blob)
        del state['master']['f']['alarms']
        writer.connection.execute('UPDATE event_query_projection SET data=?', (pack_state(state),))
        writer.connection.commit()
        with patch.object(EventRangeIndex, 'prepare', side_effect=AssertionError('valid old preparation')):
            change, = reader.scan()
        assert change.event.raw_ics == raw and [alarm.trigger for alarm in change.event.alarms] == ['-PT5M']
        assert change.indexed.master.alarms is None and change.indexed.master.raw_ics == ''


def test_batch_pins_projection_and_shared_rows_until_return(tmp_path):
    path = tmp_path / 'profile'
    zone = 'Synthetic/Example'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{zone}\nBEGIN:STANDARD\nDTSTART:19700101T000000\n'
                  'TZOFFSETFROM:+0100\nTZOFFSETTO:+0100\nEND:STANDARD\nEND:VTIMEZONE\n')
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        writer.apply_remote([event('a', timezone_id=zone, timezone_definitions={zone: definition})])
        hydrate = alarm_ics.hydrate_alarms

        def commit_while_reading(source):
            writer.apply_remote([], ['a'], calendar_id='local')
            return hydrate(source)

        with patch.object(alarm_ics, 'hydrate_alarms', side_effect=commit_while_reading), \
                patch.object(EventRangeIndex, 'prepare', side_effect=AssertionError('lost snapshot')):
            previous, = reader.scan()
        assert previous.event_id == 'a' and previous.indexed.master.timezone_definitions == {zone: definition}
        assert reader.scan() == () and reader.source_stamp('a') is None


def test_reader_progresses_during_sync_writer(tmp_path):
    path = tmp_path / 'profile'
    with Store(path, query_index=False) as writer:
        writer.apply_remote([event('a')])
        revision = writer.events_revision()
        writer.connection.execute('BEGIN IMMEDIATE')
        writer.connection.execute("DELETE FROM events WHERE id='a'")
        with closing(ReminderSources(str(path))) as reader:
            assert reader.revision() == revision
            assert reader.scan()[0].event.id == 'a'
            assert reader.calendar_states()['local'][0] is True
            assert reader.incarnation()
            writer.connection.commit()
            assert reader.scan() == ()


def test_calendar_enable_transitions_are_atomic_durable_and_ignore_layout(tmp_path):
    path = tmp_path / 'profile'
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        original = reader.calendar_states()['local']
        assert original[0] is True
        revision = reader.revision()
        writer.set_calendar_visible('local', False)
        writer.set_calendar_in_main_list('local', False)
        calendar = writer.get_calendar('local')
        writer.save_calendar(replace(calendar, name='Edited'))
        assert reader.calendar_states()['local'] == original

        # Override the built-in clock on this writer connection only.
        fake_time = BASE + timedelta(milliseconds=123)
        with pytest.raises(RuntimeError):
            with writer._transaction():
                writer.save_calendar(replace(calendar, enabled=False))
                assert reader.calendar_states()['local'] == original
                raise RuntimeError('rollback')
        assert reader.calendar_states()['local'] == original
        with closing(sqlite3.connect(path)) as external:
            external.create_function('strftime', 2, lambda *_: fake_time.isoformat(timespec='milliseconds'))
            for enabled in ('false', 'true'):
                external.execute("UPDATE calendars SET data=json_set(data,'$.enabled',json(?)) WHERE id='local'",
                                 (enabled,))
                external.commit()
        expected = (True, original[1] + 2, fake_time)
        assert reader.calendar_states()['local'] == expected
        assert reader.revision() == revision
        assert writer.pending_changes() == [] and writer.history() == []
    with Store(path, query_index=False) as reopened, closing(ReminderSources(str(path))) as reader:
        assert reader.calendar_states()['local'] == expected
        reopened.connection.execute("DELETE FROM calendars WHERE id='local'")
        reopened.connection.commit()
        assert 'local' not in reader.calendar_states()
        reopened.save_calendar(calendar)
        state = reader.calendar_states()['local']
        assert state[0] and state[1] > expected[1]


def test_legacy_calendar_stamps_backfill_and_invalid_event_does_not_block_page(tmp_path):
    path = tmp_path / 'profile'
    with closing(sqlite3.connect(path)) as old:
        old.execute('CREATE TABLE calendars(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        old.executemany('INSERT INTO calendars VALUES (?,?)', [
            ('local', '{"id":"local","name":"Legacy","color":"#aaa","visible":false}'),
            ('off', '{"id":"off","name":"Off","color":"#bbb","enabled":false}'),
        ])
        old.commit()
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        states = reader.calendar_states()
        assert states['local'][0] and not states['off'][0]
        writer.apply_remote([event('a'), event('b')])
        writer.connection.execute("DELETE FROM event_query_changes WHERE event_id='b'")
        writer.connection.execute("UPDATE events SET data='{}' WHERE id='a'")
        writer.connection.commit()
        bad, good = reader.scan()
        assert bad.event_id == 'a' and bad.event is None and bad.indexed is None
        assert bad.version == reader.revision()
        assert good.event.id == 'b' and good.version == reader.source_stamp('b') == 0
        assert reader.source_stamp('absent') is None


@pytest.mark.parametrize('new_count', [1, 5])
def test_open_reader_detects_same_path_recreation_regardless_of_revision(tmp_path, new_count):
    profile = tmp_path / 'active'
    path = profile / 'profile'
    with Store(path, query_index=False) as old:
        old.apply_remote([event('old-1'), event('old-2')])
    with closing(ReminderSources(str(path))) as reader:
        identity = reader.incarnation()
        high = reader.revision()
        assert [row.event_id for row in reader.scan()] == ['old-1', 'old-2']
        profile.rename(tmp_path / 'retired')
        with Store(path, query_index=False) as new:
            new.apply_remote([event('new-' + str(i)) for i in range(new_count)])
        assert reader.incarnation() != identity
        assert (reader.revision() > high) == (new_count == 5)
        assert reader.source_stamp('old-1') is None
        assert [row.event_id for row in reader.scan()] == ['new-' + str(i) for i in range(new_count)]
        with closing(ReminderSources(str(path))) as reopened:
            assert reopened.incarnation() == reader.incarnation()


def test_reader_follows_a_replaced_profile_symlink(tmp_path):
    alias = tmp_path / 'active'
    for name in ('first', 'second'):
        with Store(tmp_path / name, query_index=False) as store:
            store.apply_remote([event(name)])
    alias.symlink_to(tmp_path / 'first')
    with closing(ReminderSources(str(alias))) as reader:
        identity = reader.incarnation()
        replacement = tmp_path / 'new-link'
        replacement.symlink_to(tmp_path / 'second')
        replacement.replace(alias)
        assert reader.incarnation() != identity
        assert [item.event_id for item in reader.scan()] == ['second']


def test_symlink_switch_between_stat_and_resolve_cannot_bind_wrong_source(tmp_path):
    alias = tmp_path / 'active'
    markers = {}
    for name in ('one', 'two', 'three'):
        with Store(tmp_path / name, query_index=False) as store:
            store.apply_remote([event(name)])
            markers[name] = store.connection.execute('SELECT token FROM database_incarnation').fetchone()[0]

    def point(name):
        link = tmp_path / 'new-link'
        link.symlink_to(tmp_path / name)
        link.replace(alias)

    point('one')
    resolve = Path.resolve

    def unstable_resolve(path, *args, **kwargs):
        if path != alias:
            return resolve(path, *args, **kwargs)
        point('three')
        resolved = resolve(path, *args, **kwargs)
        point('two')
        return resolved

    with closing(ReminderSources(str(alias))) as reader:
        assert reader.incarnation() == markers['one']
        point('two')
        with patch.object(Path, 'resolve', unstable_resolve):
            try:
                observed = reader.incarnation()
            except sqlite3.OperationalError:
                pass  # An unstable profile can be temporarily unavailable.
            else:
                assert observed == markers['two']
        assert reader.incarnation() == markers['two']
        assert [row.event_id for row in reader.scan()] == ['two']


def test_replaced_main_with_retained_wal_suspends_all_reads_until_coherent(tmp_path):
    profile = tmp_path / 'active'
    path = profile / 'main'
    with Store(path, query_index=False) as original:
        original.apply_remote([event('old')])
        reader = ReminderSources(str(path))
        old_marker = reader.incarnation()
    with closing(reader):
        new_path = tmp_path / 'replacement'
        with Store(new_path, query_index=False) as new:
            new.apply_remote([event('new')])
            new_marker = new.connection.execute('SELECT token FROM database_incarnation').fetchone()[0]
        assert new_marker != old_marker
        wal = profile / 'main-wal'
        old_wal = wal.stat()
        new_path.replace(path)
        for read in (reader.incarnation, reader.revision, reader.scan, reader.calendar_states,
                     lambda: reader.changes((0, '')), lambda: reader.source_stamp('old')):
            with pytest.raises(sqlite3.OperationalError, match='WAL'):
                read()
            assert wal.stat().st_ino == old_wal.st_ino
        # A complete independent file set at the canonical path allows a retry.
        profile.rename(tmp_path / 'retired')
        with Store(path, query_index=False) as restored:
            restored.apply_remote([event('restored')])
            restored_marker = restored.connection.execute('SELECT token FROM database_incarnation').fetchone()[0]
        assert reader.incarnation() == restored_marker
        assert [source.event_id for source in reader.scan()] == ['restored']


@pytest.mark.parametrize('known_profile', [True, False])
@pytest.mark.parametrize('read', ['scan', 'incarnation'])
def test_directory_swap_while_pinning_cannot_misattribute_a_batch(tmp_path, known_profile, read):
    active, other, staged = (tmp_path / name for name in ('active', 'other', 'staged'))
    for directory in (active, other, staged):
        with Store(directory / 'main', query_index=False) as store:
            store.apply_remote([event(directory.name)])
    with closing(ReminderSources(str(active / 'main'))) as reader:
        if not known_profile:
            active.rename(tmp_path / 'initial')
            staged.rename(active)
        parked = tmp_path / 'parked'
        check = reader._checked_wal
        calls = 0

        def swap_only_while_sqlite_opens(target, main_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                result = check(target, main_id)
                active.rename(parked)
                other.rename(active)
                return result
            if calls == 2:
                active.rename(other)
                parked.rename(active)
            return check(target, main_id)

        with patch.object(reader, '_checked_wal', swap_only_while_sqlite_opens):
            with pytest.raises(sqlite3.OperationalError, match='incarnation'):
                getattr(reader, read)()
        with closing(ReminderSources(str(active / 'main'))) as independent:
            assert reader.incarnation() == independent.incarnation()
        assert [change.event_id for change in reader.scan()] == ['active' if known_profile else 'staged']


def test_expected_incarnation_guards_entire_batch_and_empty_scalar_reads(tmp_path):
    active, other = tmp_path / 'active', tmp_path / 'other'
    for directory in (active, other):
        with Store(directory / 'main', query_index=False) as store:
            store.apply_remote([event(directory.name)])
    with closing(ReminderSources(str(active / 'main'))) as reader:
        original = reader.incarnation()
        with reader.for_incarnation(original):
            assert reader.revision() == 1
            active.rename(tmp_path / 'original')
            other.rename(active)
            for read in (reader.scan, reader.incarnation, reader.revision, reader.calendar_states,
                         lambda: reader.changes((10, '')), lambda: reader.source_stamp('absent')):
                with pytest.raises(sqlite3.OperationalError, match='incarnation'):
                    read()
        assert reader.incarnation() != original
        assert reader.scan()[0].event_id == 'other'


def test_same_inode_token_change_can_rebootstrap_after_identity_refresh(tmp_path):
    path = tmp_path / 'main'
    with Store(path, query_index=False) as writer, closing(ReminderSources(str(path))) as reader:
        previous = reader.incarnation()
        writer.connection.execute('UPDATE database_incarnation SET token=lower(hex(randomblob(16)))')
        writer.connection.commit()
        with reader.for_incarnation(previous), pytest.raises(sqlite3.OperationalError, match='incarnation'):
            reader.scan()
        with pytest.raises(sqlite3.OperationalError, match='Refresh'):
            reader.scan()
        current = reader.incarnation()
        assert current != previous
        with reader.for_incarnation(current):
            assert reader.scan() == ()
