"""A maintained interval search tree, independent of previous queries.

Nodes are AVL balanced and carry the maximum endpoint of their subtree. Point
events use equal endpoints and belong to the half-open query containing them.
Updates take O(log n); queries prune both past endpoints and future starts.
There is no query-result storage or warm-up path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Iterator, TypeVar


T = TypeVar('T')


@dataclass(frozen=True, slots=True)
class Interval(Generic[T]):
    start: float
    end: float
    identity: str
    value: T

    @property
    def key(self) -> tuple[float, float, str]:
        return self.start, self.end, self.identity


@dataclass(slots=True)
class _Node(Generic[T]):
    interval: Interval[T]
    left: _Node[T] | None = None
    right: _Node[T] | None = None
    height: int = 1
    maximum: tuple[float, bool] = (float('-inf'), False)

    def __post_init__(self):
        self.maximum = self.interval.end, self.interval.start == self.interval.end


def _height(node):
    return node.height if node is not None else 0


def _refresh(node):
    node.height = 1 + max(_height(node.left), _height(node.right))
    node.maximum = max((node.interval.end, node.interval.start == node.interval.end),
                       node.left.maximum if node.left is not None else (float('-inf'), False),
                       node.right.maximum if node.right is not None else (float('-inf'), False))
    return node


def _right(node):
    top = node.left
    node.left = top.right
    top.right = _refresh(node)
    return _refresh(top)


def _left(node):
    top = node.right
    node.right = top.left
    top.left = _refresh(node)
    return _refresh(top)


def _balance(node):
    _refresh(node)
    skew = _height(node.left) - _height(node.right)
    if skew > 1:
        if _height(node.left.left) < _height(node.left.right):
            node.left = _left(node.left)
        return _right(node)
    if skew < -1:
        if _height(node.right.right) < _height(node.right.left):
            node.right = _right(node.right)
        return _left(node)
    return node


def _insert(node, interval):
    if node is None:
        return _Node(interval)
    if interval.key < node.interval.key:
        node.left = _insert(node.left, interval)
    elif interval.key > node.interval.key:
        node.right = _insert(node.right, interval)
    else:
        node.interval = interval
    return _balance(node)


def _remove(node, key):
    if node is None:
        return None
    if key < node.interval.key:
        node.left = _remove(node.left, key)
    elif key > node.interval.key:
        node.right = _remove(node.right, key)
    else:
        if node.left is None:
            return node.right
        if node.right is None:
            return node.left
        successor = node.right
        while successor.left is not None:
            successor = successor.left
        node.interval = successor.interval
        node.right = _remove(node.right, successor.interval.key)
    return _balance(node)


class IntervalIndex(Generic[T]):
    """Mutable AVL interval index. Callers retain keys for individual removal."""

    def __init__(self):
        self._root: _Node[T] | None = None

    @classmethod
    def from_sorted(cls, intervals) -> IntervalIndex[T]:
        """Build an AVL tree in linear time from intervals ordered by key.

        Equal keys retain the last value, just like repeated ``add`` calls.
        Construction retains no input array or auxiliary search structure.
        """
        values = []
        previous = None
        for item in intervals:
            if item.end < item.start:
                raise ValueError('An indexed interval must have ordered endpoints.')
            key = item.key
            if previous is not None and key < previous:
                raise ValueError('Bulk intervals must be ordered by key.')
            if previous == key:
                values[-1] = item
            else:
                values.append(item)
            previous = key

        def build(begin, end):
            if begin == end:
                return None
            middle = (begin + end) // 2
            node = _Node(values[middle], build(begin, middle), build(middle + 1, end))
            return _refresh(node)

        result = cls()
        result._root = build(0, len(values))
        return result

    @classmethod
    def from_intervals(cls, intervals) -> IntervalIndex[T]:
        """Sort arbitrary intervals once, then build without insertions/rotations."""
        return cls.from_sorted(sorted(intervals, key=lambda item: item.key))

    def add(self, interval: Interval[T]) -> None:
        if interval.end < interval.start:
            raise ValueError('An indexed interval must have ordered endpoints.')
        self._root = _insert(self._root, interval)

    def remove(self, key: tuple[float, float, str]) -> None:
        self._root = _remove(self._root, key)

    def overlapping(self, start: float, end: float) -> Iterator[T]:
        if end <= start:
            return
        stack = [self._root] if self._root is not None else []
        while stack:
            node = stack.pop()
            if node.maximum < (start, True):
                continue
            item = node.interval
            if node.left is not None:
                stack.append(node.left)
            if item.start >= end:
                continue
            if item.end > start or item.start == item.end == start:
                yield item.value
            if node.right is not None:
                stack.append(node.right)

    def ordered_overlapping(self, start: float, end: float) -> Iterator[T]:
        """Report overlaps in start order, allowing bounded next-event queries."""
        if end <= start:
            return

        def visit(node):
            if node is None or node.maximum < (start, True):
                return
            yield from visit(node.left)
            item = node.interval
            if item.start >= end:
                return
            if item.end > start or item.start == item.end == start:
                yield item.value
            yield from visit(node.right)

        yield from visit(self._root)
