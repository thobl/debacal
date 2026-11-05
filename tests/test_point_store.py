"""Zero-duration timed events participate in half-open calendar ranges."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from lebacal.models import Event
from lebacal.store import Store


START = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)


def point(event_id, start=START, **changes):
    return Event(event_id, "local", "Point event", start, start, **changes)


def test_point_range_includes_lower_bound_and_excludes_upper_bound(tmp_path):
    with Store(tmp_path / "db") as store:
        events = [point("before", START - timedelta(minutes=1)), point("lower"),
                  point("inside", START + timedelta(minutes=30)), point("upper", START + timedelta(hours=1)),
                  Event("ends-at-lower", "local", "Previous interval", START - timedelta(hours=1), START),
                  Event("overlapping", "local", "Overlapping interval", START - timedelta(hours=1), START + timedelta(minutes=1))]
        store.apply("Create", events)
        assert {event.id for event in store.list_events(START, START + timedelta(hours=1))} == {"lower", "inside", "overlapping"}
        assert {event.id for event in store.query_occurrences(START, START + timedelta(hours=1))} == {"lower", "inside", "overlapping"}


def test_point_lower_bound_alone_includes_points_at_or_after_start(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create", [point("before", START - timedelta(seconds=1)), point("at"), point("after", START + timedelta(seconds=1))])
        assert {event.id for event in store.list_events(start=START)} == {"at", "after"}
        assert {event.id for event in store.list_events(end=START)} == {"before"}


def test_edit_and_undo_a_point_event_keeps_zero_duration(tmp_path):
    with Store(tmp_path / "db") as store:
        original = point("point")
        store.apply("Create", [original])
        moved = replace(original, title="Moved point", start=START + timedelta(hours=1), end=START + timedelta(hours=1))
        store.apply("Move", [moved])
        assert store.get_event(original.id) == moved
        store.undo()
        assert store.get_event(original.id) == original
        assert store.query_occurrences(START, START + timedelta(hours=1)) == [original]
        store.redo()
        assert store.get_event(original.id) == moved


def test_recurring_point_query_and_single_edit_undo(tmp_path):
    with Store(tmp_path / "db") as store:
        master = point("series", recurrence_rule="FREQ=DAILY;COUNT=3")
        store.apply("Create series", [master])
        lower = START + timedelta(days=1)
        instances = store.query_occurrences(lower, lower + timedelta(hours=1))
        assert len(instances) == 1
        selected = instances[0]
        assert selected.start == selected.end == lower
        moved = replace(selected, start=lower + timedelta(hours=2), end=lower + timedelta(hours=2))
        store.edit_occurrence(selected, moved)
        assert store.query_occurrences(lower, lower + timedelta(hours=1)) == []
        assert store.get_occurrence(selected.id).start == moved.start
        store.undo()
        assert store.query_occurrences(lower, lower + timedelta(hours=1)) == instances
        assert store.list_events() == [master]
