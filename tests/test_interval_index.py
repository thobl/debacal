"""Structural bounds and exhaustive overlap checks for the maintained tree."""

from math import ceil, log2
from random import Random

from debacal.interval_index import Interval, IntervalIndex


def check_tree(node):
    if node is None:
        return 0, (float('-inf'), False), []
    left_height, left_maximum, left = check_tree(node.left)
    right_height, right_maximum, right = check_tree(node.right)
    assert abs(left_height - right_height) <= 1
    assert node.height == 1 + max(left_height, right_height)
    assert node.maximum == max((node.interval.end, node.interval.start == node.interval.end), left_maximum, right_maximum)
    keys = left + [node.interval.key] + right
    assert keys == sorted(keys)
    return node.height, node.maximum, keys


def test_sorted_insertions_remain_balanced_and_deletions_maintain_endpoints():
    tree = IntervalIndex()
    entries = [Interval(index, index + index % 9, str(index), index) for index in range(2000)]
    for item in entries:
        tree.add(item)
    height, _, _ = check_tree(tree._root)
    assert height <= 2 * ceil(log2(len(entries) + 1))
    for item in entries[::2]:
        tree.remove(item.key)
    check_tree(tree._root)
    assert set(tree.overlapping(-1, 3000)) == set(range(1, 2000, 2))
    for item in entries[1::2]:
        tree.remove(item.key)
    assert tree._root is None


def test_random_overlaps_match_brute_force_for_points_and_ranges_after_updates():
    random = Random(731)
    tree, entries = IntervalIndex(), {}
    for step in range(600):
        if entries and random.randrange(4) == 0:
            item = entries.pop(random.choice(list(entries)))
            tree.remove(item.key)
        else:
            start = random.randrange(-100, 100)
            item = Interval(start, start + random.randrange(20), str(step), step)
            tree.add(item)
            entries[step] = item
        lower = random.randrange(-120, 120)
        upper = lower + random.randrange(1, 40)
        expected = [item for item in entries.values() if item.start < upper
                    and (item.end > lower or item.start == item.end == lower)]
        assert set(tree.overlapping(lower, upper)) == {item.value for item in expected}
        assert list(tree.ordered_overlapping(lower, upper)) == [item.value for item in sorted(expected, key=lambda item: item.key)]
    check_tree(tree._root)


def test_half_open_edges_and_equal_start_duration_ties():
    tree = IntervalIndex()
    for item in (Interval(1, 3, 'long', 'long'), Interval(1, 1, 'point', 'point'),
                 Interval(1, 2, 'short', 'short'), Interval(0, 1, 'ended', 'ended'),
                 Interval(2, 2, 'future', 'future')):
        tree.add(item)
    assert list(tree.ordered_overlapping(1, 2)) == ['point', 'short', 'long']
    assert list(tree.overlapping(1, 1)) == []
    assert set(tree.overlapping(2, 3)) == {'long', 'future'}
