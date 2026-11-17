"""Coordinate-preserving source edits replace values without reshaping trees."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.event_range_index import EventRangeIndex
from debacal.interval_index import Interval, IntervalIndex
from debacal.models import Event
from debacal.recurrence import occurrence, recurrence_key
from debacal.store import Store


START = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


def event(**changes):
    return replace(Event('source', 'local', 'Original', START, START + timedelta(hours=1)), **changes)


def shape(root):
    if root is None:
        return ()
    return (id(root), root.interval.key, root.height, root.maximum, shape(root.left), shape(root.right))


def forbid_structure_changes():
    stack = ExitStack()
    for name in ('add', 'remove'):
        stack.enter_context(patch.object(IntervalIndex, name, side_effect=AssertionError('Preserve unchanged nodes')))
    return stack


def test_value_replacement_preserves_nodes_heights_maxima_and_sorted_point_queries():
    values = [Interval(float(index), float(index + index % 4), str(index), ('old', index))
              for index in range(127)]
    tree = IntervalIndex.from_sorted(values)
    before = shape(tree._root)
    updated = {index: ('new', index) for index in (0, 1, 12, 63, 125, 126)}
    with ExitStack() as stack:
        for name in ('_balance', '_refresh', '_insert', '_remove'):
            stack.enter_context(patch('debacal.interval_index.' + name,
                                     side_effect=AssertionError('No balance or endpoint work for unchanged keys')))
        for index, payload in updated.items():
            tree.replace_value(values[index].key, payload)
    assert shape(tree._root) == before
    expected = [updated.get(index, ('old', index)) for index in range(127)]
    assert list(tree.ordered_overlapping(-1, 200)) == expected
    assert ('new', 0) in tree.overlapping(0, .5)
    assert values[0].value == ('old', 0), 'Previously retained immutable Intervals remain unchanged'


@pytest.mark.parametrize('populated', [False, True])
def test_missing_replacement_key_raises_without_inserting_or_mutating(populated):
    tree = IntervalIndex()
    if populated:
        tree.add(Interval(1, 2, 'present', 'original'))
    before = shape(tree._root)
    with pytest.raises(KeyError):
        tree.replace_value((1, 2, 'missing'), 'replacement')
    assert shape(tree._root) == before
    assert list(tree.overlapping(0, 3)) == (['original'] if populated else [])


def test_plain_source_title_notes_and_candidate_replace_current_payload_only():
    original = event()
    index = EventRangeIndex()
    index.upsert(original)
    old_record = index.sources[original.id]
    old_snapshot, _failures = index.query(START, START + timedelta(days=1))
    before = {key: shape(tree._root) for key, tree in index.trees.items()}
    changed = replace(original, title='New title', description='New notes', candidate=True)
    with forbid_structure_changes():
        index.upsert(changed)
    found, failures = index.query(START, START + timedelta(days=1))
    assert not failures and found == [changed]
    assert found[0] is changed
    assert old_snapshot == [original] and old_snapshot[0].title == 'Original'
    assert old_record.master is original
    assert {key: shape(tree._root) for key, tree in index.trees.items()} == before


def test_all_finite_explicit_and_detached_payloads_are_replaced():
    nominal = START + timedelta(days=1)
    original = event(recurrence_rule='FREQ=DAILY;COUNT=4',
                     recurrence_dates=(recurrence_key(START + timedelta(days=10)),))
    detached = replace(occurrence(original, nominal), title='Original detached',
                       start=nominal + timedelta(hours=2), end=nominal + timedelta(hours=3))
    original = replace(original, recurrence_overrides={recurrence_key(nominal): detached.to_dict()})
    index = EventRangeIndex()
    index.upsert(original)
    previous = index.sources[original.id]
    old_snapshot, _ = index.query(START, START + timedelta(days=12))
    detached = replace(detached, title='Changed detached', description='Detached notes', candidate=True)
    changed = replace(original, title='Changed family', description='Family notes', candidate=True,
                      recurrence_overrides={recurrence_key(nominal): detached.to_dict()})
    prepared = index.prepare(changed)
    assert prepared.entries == previous.entries
    with forbid_structure_changes():
        index.restore(changed.id, prepared)
    found, failures = index.query(START, START + timedelta(days=12))
    assert not failures and len(found) == 5
    assert all(item.candidate for item in found)
    assert {item.title for item in found} == {'Changed family', 'Changed detached'}
    assert {item.description for item in found} == {'Family notes', 'Detached notes'}
    assert all(item is prepared.occurrences[item.id] for item in found)
    assert {item.title for item in old_snapshot} == {'Original', 'Original detached'}
    assert previous.master is original


def test_rule_change_with_identical_envelope_replaces_native_rule_and_anchor():
    original = event(recurrence_rule='FREQ=DAILY')
    index = EventRangeIndex()
    index.upsert(original)
    previous = index.sources[original.id]
    changed = replace(original, recurrence_rule='FREQ=WEEKLY', title='Weekly', description='New rule')
    current = index.prepare(changed)
    assert previous.entries == current.entries
    with forbid_structure_changes():
        index.restore(changed.id, current)
    explicit, rules = index.select(START, START + timedelta(days=28))
    assert rules == [current] and rules[0] is current
    assert all(item.title == 'Weekly' for item in explicit)
    found, failures = index.query(START, START + timedelta(days=28))
    assert not failures and len(found) == 4
    assert all(item.title == 'Weekly' and item.description == 'New rule' for item in found)
    assert len(previous.prepared.query(START, START + timedelta(days=28))) == 28
    assert previous.master.recurrence_rule == 'FREQ=DAILY'


@pytest.mark.parametrize('changes', [
    {'calendar_id': 'different'},
    {'start': START + timedelta(days=10), 'end': START + timedelta(days=10, hours=1)},
])
def test_changed_coordinates_or_calendar_use_structural_update(changes):
    original = event()
    index = EventRangeIndex()
    index.upsert(original)
    changed = replace(original, **changes)
    with patch.object(index, '_remove_entry', wraps=index._remove_entry) as removals:
        index.upsert(changed)
        assert removals.call_count == 1
    assert index.sources[changed.id].master is changed
    found, failures = index.query(changed.start, changed.end, [changed.calendar_id])
    assert not failures and found == [changed]
    if 'calendar_id' in changes:
        assert not index.query(START, START + timedelta(days=1), ['local'])[0]
    else:
        assert not index.query(START, START + timedelta(days=1))[0]


def test_metadata_rollback_restores_prior_payloads_without_rebalancing():
    original = event(recurrence_rule='FREQ=DAILY;COUNT=3')
    with Store(':memory:') as store:
        store.apply_remote([original])
        before = store._query_index.sources[original.id]
        snapshot = store.query_occurrences(START, START + timedelta(days=3))
        with forbid_structure_changes():
            with pytest.raises(RuntimeError, match='Rollback'):
                with store._transaction():
                    store._write_event(replace(original, title='Uncommitted', description='Draft notes', candidate=True))
                    assert all(item.title == 'Uncommitted' for item in
                               store.query_occurrences(START, START + timedelta(days=3)))
                    raise RuntimeError('Rollback')
        assert store._query_index.sources[original.id] is before
        restored = store.query_occurrences(START, START + timedelta(days=3))
        assert restored == snapshot
        assert all(item.title == 'Original' and not item.candidate for item in restored)
