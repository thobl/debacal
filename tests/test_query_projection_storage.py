"""Durable query data must stay exact across restarts, writes and recovery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.event_range_index import EventRangeIndex
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.prepared_recurrence import PreparedSeries
from debacal.recurrence import recurrence_key
from debacal.store import Store


START = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def event(identity='event', **changes):
    return replace(Event(identity, 'local', 'Synthetic', START, START + timedelta(hours=1)), **changes)


RAW = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Synthetic test//EN\r
BEGIN:VEVENT\r
UID:retained-properties@calendar.invalid\r
DTSTART:20261101T090000Z\r
DTEND:20261101T100000Z\r
SUMMARY:Synthetic event\r
X-SYNTHETIC-PROPERTY:retain this value\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
TRIGGER:-PT15M\r
DESCRIPTION:Synthetic reminder\r
END:VALARM\r
END:VEVENT\r
END:VCALENDAR\r
'''


def test_reopening_bulk_loads_prepared_data_without_canonical_decoding_or_generation(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as store:
        store.apply_remote([event('ordinary', raw_ics='synthetic retained payload' * 100),
                            event('finite', recurrence_rule='FREQ=DAILY;COUNT=8'),
                            event('long', recurrence_rule='FREQ=MONTHLY')])
        expected = [(item.id, item.start, item.end) for item in store.query_occurrences(START, START + timedelta(days=90))]
    with patch.object(Event, 'from_dict', side_effect=AssertionError('No canonical event decoding on restart')), \
            patch.object(EventRangeIndex, 'prepare', side_effect=AssertionError('No recurrence preparation on restart')), \
            patch.object(PreparedSeries, '__init__', side_effect=AssertionError('No rule construction on restart')):
        with Store(path) as reopened:
            actual = reopened.query_occurrences(START, START + timedelta(days=90))
            assert [(item.id, item.start, item.end) for item in actual] == expected
            assert all(item._query_projection and not item.raw_ics for item in actual)
            assert reopened._query_index.sources['finite'].prepared is None


def test_writer_prepares_only_changed_source_and_reader_consumes_delta_without_parsing(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as reader:
        reader.apply_remote([event(str(index), recurrence_rule='FREQ=DAILY') for index in range(100)])
        with Store(path, query_index=False) as writer:
            assert writer._query_index is None
            with patch.object(EventRangeIndex, 'prepare', wraps=EventRangeIndex.prepare) as prepared:
                writer.apply_remote([event('3', title='Changed', recurrence_rule='FREQ=DAILY')])
            assert [call.args[0].id for call in prepared.call_args_list] == ['3']
            with patch.object(Event, 'from_dict', side_effect=AssertionError('Read the prepared delta only')), \
                    patch.object(EventRangeIndex, 'prepare', side_effect=AssertionError('No reader-side compilation')):
                found = reader.query_occurrences(START, START + timedelta(days=1))
                assert len(found) == 100
                assert next(item for item in found if item.series_id == '3').title == 'Changed'


def test_rollback_restores_both_durable_and_in_memory_projection(tmp_path):
    with Store(tmp_path / 'db') as store:
        first = event(recurrence_rule='FREQ=DAILY;COUNT=5')
        store.apply_remote([first])
        before = bytes(store.connection.execute('SELECT data FROM event_query_projection WHERE event_id=?', (first.id,)).fetchone()[0])
        version = store.events_revision()
        with pytest.raises(RuntimeError):
            with store._transaction():
                store._write_event(replace(first, start=START + timedelta(days=20), end=START + timedelta(days=20, hours=1)))
                assert not store.query_occurrences(START, START + timedelta(days=5))
                raise RuntimeError('Rollback synthetic edit')
        assert bytes(store.connection.execute('SELECT data FROM event_query_projection WHERE event_id=?', (first.id,)).fetchone()[0]) == before
        assert store._projection_storage.version() == store.events_revision() == version
        assert len(store.query_occurrences(START, START + timedelta(days=5))) == 5


@pytest.mark.parametrize('damage', ['container', 'missing'])
def test_damaged_derived_row_rebuilds_only_its_canonical_source(tmp_path, damage):
    path = tmp_path / 'db'
    with Store(path) as store:
        store.apply_remote([event('damaged'), event('healthy')])
        if damage == 'container':
            store.connection.execute("UPDATE event_query_projection SET data=X'000102' WHERE event_id='damaged'")
        else:
            store.connection.execute("DELETE FROM event_query_projection WHERE event_id='damaged'")
        store.connection.commit()
    with patch.object(EventRangeIndex, 'prepare', wraps=EventRangeIndex.prepare) as prepared:
        with Store(path) as reopened:
            assert {item.id for item in reopened.query_occurrences(START, START + timedelta(days=1))} == {'damaged', 'healthy'}
    assert [call.args[0].id for call in prepared.call_args_list] == ['damaged']


def test_format_change_rebuilds_once_and_following_restart_is_ready(tmp_path):
    path = tmp_path / 'db'
    with Store(path) as store:
        store.apply_remote([event(recurrence_rule='FREQ=WEEKLY')])
        store.connection.execute("UPDATE event_query_state SET signature='older-format'")
        store.connection.commit()
    with patch.object(EventRangeIndex, 'prepare', wraps=EventRangeIndex.prepare) as prepared:
        with Store(path):
            pass
    assert prepared.call_count == 1
    with patch.object(EventRangeIndex, 'prepare', side_effect=AssertionError('Already rebuilt')):
        with Store(path) as reopened:
            assert reopened.query_occurrences(START, START + timedelta(days=8))


@pytest.mark.parametrize('operation', ['apply', 'selection', 'occurrence'])
def test_editing_lightweight_values_preserves_canonical_unknown_ics_properties_and_history(tmp_path, operation):
    original = parse_ics(RAW, 'local')[0]
    with Store(tmp_path / 'db') as store:
        store.apply_remote([original])
        shown = store.query_occurrences(START, START + timedelta(days=1))[0]
        assert shown._query_projection and shown.raw_ics == ''
        edited = replace(shown, title='Edited title')
        if operation == 'apply':
            change = store.apply('Edit', [edited])
        elif operation == 'selection':
            change = store.edit_selection([shown], 'single', shown, edited)
        else:
            # Even an editor that constructs a fresh canonical value retains
            # the source's transport data through the reviewed reference.
            edited = replace(edited, _query_projection=False)
            change = store.edit_occurrence(shown, edited)
        saved = store.get_event(original.id)
        assert saved.title == 'Edited title'
        assert saved.raw_ics == original.raw_ics
        assert not saved._query_projection
        exported = export_ics([saved])
        assert b'X-SYNTHETIC-PROPERTY:retain this value' in exported
        assert b'BEGIN:VALARM' in exported
        assert '_query_projection' not in saved.to_dict()
        store.undo(change)
        assert store.get_event(original.id).raw_ics == original.raw_ics
        store.redo(change)
        assert store.get_event(original.id).raw_ics == original.raw_ics


def test_authoring_reconciliation_keeps_transport_from_a_lightweight_series_root():
    master = event(recurrence_dates=(recurrence_key(START + timedelta(days=2)),),
                   schedule_expression='+0d +2d', expression_anchor=START.date().isoformat(),
                   schedule_syntax=2, raw_ics=RAW.decode())
    with Store(':memory:') as store:
        store.apply_remote([master])
        light = store.query_source_events([master.id])[0]
        store.apply('Change dates', [replace(light, recurrence_dates=(recurrence_key(START + timedelta(days=4)),))])
        saved = store.get_event(master.id)
        assert saved.schedule_expression != master.schedule_expression
        assert saved.raw_ics == master.raw_ics


def test_shared_preparations_survive_partial_deletion_and_are_retired_when_unused():
    identifier = 'Synthetic/Shared'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\n'
                  'DTSTART:19700101T000000\nTZOFFSETFROM:+0100\nTZOFFSETTO:+0100\n'
                  'END:STANDARD\nEND:VTIMEZONE\n')
    first = event('first', timezone_id=identifier, timezone_definitions={identifier: definition})
    second = replace(first, id='second', ical_uid='second')
    with Store(':memory:') as store:
        store.apply_remote([first, second])
        count = lambda: store.connection.execute('SELECT count(*) FROM event_query_shared').fetchone()[0]
        assert count() == 2  # One definition and one prepared timezone.
        store.apply_remote([], [first.id], calendar_id='local')
        assert count() == 2
        assert [item.id for item in store.query_occurrences(START, START + timedelta(days=1))] == ['second']
        store.apply_remote([], [second.id], calendar_id='local')
        assert count() == 0
        assert not store.query_occurrences(START, START + timedelta(days=1))


@pytest.mark.parametrize('remote', [False, True])
def test_identical_explicit_reimport_retries_previously_failed_preparation(tmp_path, remote):
    path = tmp_path / 'db'
    source = event()
    with Store(path) as store:
        store.apply_remote([source])
        store.connection.execute('UPDATE event_query_projection SET data=NULL')
        store.connection.commit()
    with Store(path) as reader, Store(path, query_index=False) as writer:
        assert not reader.query_occurrences(START, START + timedelta(days=1))
        revision = reader.events_revision()
        if remote:
            writer.apply_remote([source])
        else:
            assert writer.apply('Retry import', [source]) is None
        assert reader.events_revision() > revision
        assert [item.id for item in reader.query_occurrences(START, START + timedelta(days=1))] == [source.id]
