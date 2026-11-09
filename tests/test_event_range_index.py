"""Range retrieval must be bounded by relevant data on every new window."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.models import Calendar, Event
from debacal.prepared_recurrence import PreparedSeries
from debacal.recurrence import expand_events_safe, occurrence, recurrence_key
from debacal.store import Store


START = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def event(identity='event', **changes):
    return replace(Event(identity, 'local', 'Synthetic', START, START + timedelta(hours=1)), **changes)


def test_new_windows_only_project_overlapping_long_rules_without_deserialization():
    sources = [event(f'past-{index}', start=START - timedelta(days=2000 + index),
                     end=START - timedelta(days=2000 + index, hours=-1), recurrence_rule='FREQ=DAILY;COUNT=10')
               for index in range(600)]
    sources += [event(f'future-{index}', start=START + timedelta(days=2000 + index),
                      end=START + timedelta(days=2000 + index, hours=1), recurrence_rule='FREQ=DAILY')
                for index in range(600)]
    active = event('active', recurrence_rule='FREQ=WEEKLY;BYDAY=MO,WE')
    sources.append(active)
    with Store(':memory:') as store:
        store.apply_remote(sources)
        projected = []
        query = PreparedSeries.query_rule
        def observed(prepared, *args, **kwargs):
            projected.append(prepared.master.id)
            return query(prepared, *args, **kwargs)
        with patch.object(Event, 'from_dict', side_effect=AssertionError('No query-time JSON/model reconstruction')), \
                patch('debacal.recurrence.rrulestr', side_effect=AssertionError('No query-time rule parsing')), \
                patch.object(PreparedSeries, 'query_rule', observed):
            for offset in (0, 21, 7, 42, 14):
                begin = START.replace(hour=0) + timedelta(days=offset)
                assert store.query_occurrences(begin, begin + timedelta(days=7))
        assert projected == ['active'] * 5


def test_sparse_explicit_dates_and_detached_exceptions_are_individually_indexed():
    original = event('sparse', recurrence_dates=(recurrence_key(START + timedelta(days=1000)),))
    key = recurrence_key(START + timedelta(days=1))
    moved = replace(occurrence(original, START + timedelta(days=1)),
                    start=START - timedelta(days=1000), end=START - timedelta(days=1000, hours=-1))
    master = replace(original, recurrence_overrides={key: moved.to_dict()})
    with Store(':memory:') as store:
        store.apply_remote([master])
        with patch.object(PreparedSeries, 'query_rule', side_effect=AssertionError('Finite dates need no rule expansion')):
            assert not store.query_occurrences(START + timedelta(days=50), START + timedelta(days=51))
            assert store.query_occurrences(moved.start, moved.end) == [occurrence(master, START + timedelta(days=1))]
            found = store.query_occurrences(START + timedelta(days=1000), START + timedelta(days=1001))
            assert len(found) == 1 and found[0].start == START + timedelta(days=1000)


def test_index_tracks_edits_undo_redo_remote_updates_and_deletes(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    first = event(recurrence_rule='FREQ=DAILY;COUNT=4')
    with Store(path) as store, Store(path) as worker:
        store.apply_remote([first])
        begin, end = START - timedelta(hours=1), START + timedelta(days=4)
        assert len(store.query_occurrences(begin, end)) == 4
        moved = replace(first, start=START + timedelta(days=20), end=START + timedelta(days=20, hours=1))
        operation = store.apply('Move', [moved])
        assert not store.query_occurrences(begin, end)
        store.undo(operation)
        assert len(store.query_occurrences(begin, end)) == 4
        store.redo(operation)
        assert not store.query_occurrences(begin, end)
        store.undo(operation)
        worker.apply_remote([replace(first, recurrence_rule='FREQ=DAILY;COUNT=2')])
        assert len(store.query_occurrences(begin, end)) == 2
        worker.apply_remote([], [first.id], calendar_id='local')
        assert not store.query_occurrences(begin, end)
    with Store(path) as reopened:
        assert not reopened.query_occurrences(begin, end)


def test_a_rolled_back_query_cannot_leave_uncommitted_intervals_in_the_index():
    first = event()
    with Store(':memory:') as store:
        store.apply_remote([first])
        with pytest.raises(RuntimeError):
            with store._transaction():
                store._write_event(replace(first, start=START + timedelta(days=20), end=START + timedelta(days=20, hours=1)))
                assert not store.query_occurrences(START, START + timedelta(days=1))
                raise RuntimeError('Rollback')
        assert store.query_occurrences(START, START + timedelta(days=1)) == [first]


def test_nested_rollback_restores_prepared_sources_without_reloading_unrelated_data():
    with Store(':memory:') as store:
        first = event('one', recurrence_rule='FREQ=DAILY')
        other = event('other')
        store.apply_remote([first, other, *[event(str(index)) for index in range(100)]])
        with patch.object(Event, 'from_dict', wraps=Event.from_dict) as decoded:
            with store._transaction():
                store._write_event(replace(first, title='Outer edit'))
                with pytest.raises(RuntimeError):
                    with store._transaction():
                        store._write_event(replace(other, title='Inner edit'))
                        assert {item.title for item in store.query_occurrences(START, START + timedelta(days=1))} >= {'Outer edit', 'Inner edit'}
                        raise RuntimeError('Rollback inner')
                assert store.get_occurrence(other.id).title == 'Synthetic'
                assert store.get_occurrence(first.id).title == 'Outer edit'
        # Before-images restore the changed sources. An unsynchronized outer
        # edit can be reread; unrelated resources must not be reconstructed.
        assert decoded.call_count <= 4
        assert store.get_occurrence(first.id).title == 'Outer edit'


def test_preparation_failure_rolls_back_before_durable_commit():
    with Store(':memory:') as store:
        first = event('first')
        store.apply_remote([first])
        constructor = PreparedSeries.__init__
        def fail(prepared, master):
            if master.id == 'broken':
                raise RuntimeError('Synthetic preparation failure')
            constructor(prepared, master)
        with patch.object(PreparedSeries, '__init__', fail):
            with pytest.raises(RuntimeError, match='preparation failure'):
                store.apply_remote([event('broken', recurrence_rule='FREQ=DAILY')])
        assert store.get_event('broken') is None
        assert store.query_occurrences(START, START + timedelta(days=1)) == [first]


def test_changes_prepare_only_the_changed_source_and_queries_never_prepare_again():
    with Store(':memory:') as store:
        store.apply_remote([event(str(index), recurrence_rule='FREQ=DAILY') for index in range(100)])
        with patch.object(Event, 'from_dict', wraps=Event.from_dict) as decoded:
            store.apply_remote([event('3', title='Changed', recurrence_rule='FREQ=DAILY')])
        # apply_remote may also decode its conflict/snapshot inputs; the index
        # change stream itself has exactly one entry after the current revision.
        assert decoded.call_count < 10
        with patch.object(Event, 'from_dict', side_effect=AssertionError('Prepared data is current')):
            for offset in (3, 8, 1):
                assert len(store.query_occurrences(START + timedelta(days=offset), START + timedelta(days=offset + 1))) == 100


def test_calendar_filtering_and_independent_occurrence_limit_remain_correct():
    with Store(':memory:') as store:
        store.save_calendar(Calendar('other', 'Other', '#123456'))
        sources = [event('a', recurrence_rule='FREQ=DAILY'),
                   event('b', calendar_id='other', recurrence_rule='FREQ=WEEKLY'),
                   event('point', end=START)]
        store.apply_remote(sources)
        for selected in (None, ['local'], ['other'], []):
            begin, end = START, START + timedelta(days=60)
            expected, failures = expand_events_safe([item for item in sources if selected is None or item.calendar_id in selected], begin, end)
            assert not failures
            assert store.query_occurrences(begin, end, selected) == expected
            assert store.upcoming_occurrences(begin, end, selected, limit=5) == expected[:5]


@pytest.mark.parametrize('year', [1, 9998])
def test_occurrence_membership_at_extreme_years_does_not_use_sub_float_precision_windows(year):
    first = datetime(year, 1, 1, 9, tzinfo=timezone.utc)
    master = event(start=first, end=first + timedelta(hours=1), recurrence_rule='FREQ=DAILY')
    chosen = occurrence(master, first + timedelta(days=5))
    with Store(':memory:') as store:
        store.apply_remote([master])
        assert store.get_occurrence(chosen.id) == chosen


def test_selecting_one_long_duration_occurrence_does_not_query_all_overlapping_instances():
    master = event(end=START + timedelta(days=1000), recurrence_rule='FREQ=HOURLY')
    chosen = occurrence(master, START + timedelta(days=2000))
    with Store(':memory:') as store:
        store.apply_remote([master])
        with patch.object(PreparedSeries, 'query_rule', side_effect=AssertionError('Membership is not a range query')):
            assert store.get_occurrence(chosen.id) == chosen
