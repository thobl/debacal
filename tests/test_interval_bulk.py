"""Bulk loading preserves dynamic interval semantics without per-row insertion."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from random import Random
from unittest.mock import patch

import pytest

from debacal.event_range_index import EventRangeIndex, IndexedSource
from debacal.interval_index import Interval, IntervalIndex
from debacal.models import Event
from debacal.prepared_recurrence import PreparedSeries


def verify(node):
    if node is None:
        return 0, (float('-inf'), False), []
    left_height, left_maximum, left_keys = verify(node.left)
    right_height, right_maximum, right_keys = verify(node.right)
    assert abs(left_height - right_height) <= 1
    assert node.height == max(left_height, right_height) + 1
    maximum = max((node.interval.end, node.interval.start == node.interval.end), left_maximum, right_maximum)
    assert node.maximum == maximum
    keys = [*left_keys, node.interval.key, *right_keys]
    assert keys == sorted(keys)
    return node.height, maximum, keys


def test_bulk_construction_matches_incremental_queries_and_remains_mutable():
    random = Random(620)
    entries = [Interval(start := random.randrange(-500, 500), start + random.randrange(25), str(index), index)
               for index in range(1200)]
    incremental = IntervalIndex()
    for item in entries:
        incremental.add(item)
    with patch.object(IntervalIndex, 'add', side_effect=AssertionError('Bulk load cannot insert individually')):
        bulk = IntervalIndex.from_intervals(iter(entries))
    verify(bulk._root)
    for _ in range(100):
        start = random.randrange(-520, 520)
        end = start + random.randrange(1, 40)
        assert list(bulk.ordered_overlapping(start, end)) == list(incremental.ordered_overlapping(start, end))
    for item in entries[::3]:
        bulk.remove(item.key)
        incremental.remove(item.key)
    additional = Interval(0, 1000, 'extra', 'extra')
    bulk.add(additional)
    incremental.add(additional)
    verify(bulk._root)
    assert list(bulk.ordered_overlapping(-1000, 2000)) == list(incremental.ordered_overlapping(-1000, 2000))


def test_sorted_bulk_handles_points_duplicates_and_invalid_input():
    assert IntervalIndex.from_sorted(())._root is None
    first = Interval(1, 1, 'point', 'original')
    last = Interval(1, 1, 'point', 'replacement')
    tree = IntervalIndex.from_sorted((first, last, Interval(1, 2, 'span', 'span')))
    assert list(tree.ordered_overlapping(1, 2)) == ['replacement', 'span']
    assert set(tree.overlapping(2, 3)) == set()
    verify(tree._root)
    with pytest.raises(ValueError, match='ordered by key'):
        IntervalIndex.from_sorted((Interval(2, 3, 'later', 1), first))
    with pytest.raises(ValueError, match='ordered endpoints'):
        IntervalIndex.from_intervals((Interval(4, 1, 'invalid', 1),))


def event(identifier, **changes):
    start = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    return replace(Event(identifier, 'local', 'Synthetic', start, start + timedelta(hours=1)), **changes)


def test_prepared_source_bulk_load_avoids_repreparation_and_supports_updates():
    first = event('daily', recurrence_rule='FREQ=DAILY')
    finite = event('finite', recurrence_rule='FREQ=WEEKLY;COUNT=4')
    added = event('added', calendar_id='other', recurrence_dates=(
        (first.start + timedelta(days=3)).isoformat(),
        (first.start + timedelta(days=8)).isoformat()))
    original = EventRangeIndex()
    for master in (first, finite, added, event('ordinary')):
        original.upsert(master)
    loaded = EventRangeIndex()
    with patch.object(PreparedSeries, '__init__', side_effect=AssertionError('Already prepared')), \
            patch.object(IntervalIndex, 'add', side_effect=AssertionError('No initial insertions')):
        loaded.bulk_load(original.sources.values())
    for offset in (0, 3, 9, 21):
        begin, end = first.start + timedelta(days=offset), first.start + timedelta(days=offset + 7)
        for calendars in (None, ['local'], ['other'], []):
            assert loaded.query(begin, end, calendars) == original.query(begin, end, calendars)
    loaded.remove(finite.id)
    original.remove(finite.id)
    changed = replace(first, recurrence_rule='FREQ=DAILY;COUNT=2')
    loaded.upsert(changed)
    original.upsert(changed)
    assert loaded.query(first.start, first.start + timedelta(days=40)) == original.query(first.start, first.start + timedelta(days=40))
    for tree in loaded.trees.values():
        verify(tree._root)


def test_invalid_prepared_bulk_does_not_replace_existing_index():
    index = EventRangeIndex()
    original = event('original')
    index.upsert(original)
    bad = IndexedSource(event('bad'))
    bad.entries.append((('different-calendar', 'instant', False), (0, 1, 'bad')))
    with pytest.raises(ValueError, match='invalid calendar'):
        index.bulk_load([bad])
    assert index.query(original.start, original.end)[0] == [original]
