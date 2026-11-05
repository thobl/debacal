from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
from unittest.mock import patch

import pytest

from lebacal.models import Calendar, Event
from lebacal.store import HistoryConflict, Store


def event(event_id="a", calendar_id="local", **changes):
    start = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
    return replace(Event(event_id, calendar_id, "Study", start, start + timedelta(hours=1)), **changes)


def test_durable_undo_redo(tmp_path):
    path = tmp_path / "calendar.sqlite"
    with Store(path) as store:
        item = event()
        store.apply("Create study", [item])
        store.apply("Move study", [replace(item, start=item.start + timedelta(hours=2), end=item.end + timedelta(hours=2))])
        store.undo()
        assert store.get_event(item.id) == item
    with Store(path) as store:
        store.redo()
        assert store.get_event(item.id).start == item.start + timedelta(hours=2)
        assert len(store.history()) == 2


def test_selective_undo_preserves_independent_operations(tmp_path):
    with Store(tmp_path / "db") as store:
        a, b = event("a"), event("b")
        first = store.apply("A", [a])
        store.apply("B", [b])
        store.undo(first)
        assert store.get_event("a") is None
        assert store.get_event("b") == b
        store.redo(first)
        assert store.get_event("a") == a


def test_selective_undo_cannot_overwrite_later_edits(tmp_path):
    with Store(tmp_path / "db") as store:
        first = store.apply("A", [event()])
        second = store.apply("Edit A", [event(title="Changed")])
        assert not next(row for row in store.history() if row["id"] == first)["can_undo"]
        with pytest.raises(HistoryConflict):
            store.undo(first)
        assert store.get_event("a").title == "Changed"
        store.undo(second)
        store.undo(first)
        assert store.get_event("a") is None


def test_default_redo_reverses_order_of_undos(tmp_path):
    with Store(tmp_path / "db") as store:
        first = store.apply("A", [event()])
        second = store.apply("Edit A", [event(title="Changed")])
        assert store.undo() == second
        assert store.undo() == first
        assert store.redo() == first
        assert store.redo() == second
        assert store.get_event("a").title == "Changed"


def test_group_resolution_is_one_undoable_operation(tmp_path):
    with Store(tmp_path / "db") as store:
        candidates = [event(str(index), group_id="group", candidate=True) for index in range(3)]
        store.apply("Block candidates", candidates)
        chosen = replace(candidates[1], candidate=False)
        store.apply("Choose date", [chosen], [candidates[0].id, candidates[2].id])
        assert store.list_events() == [chosen]
        store.undo()
        assert len(store.list_events()) == 3
        assert all(item.candidate for item in store.list_events())


def test_outbox_is_transactional_and_stale_ack_is_safe(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav", url="https://example.invalid/calendar/"))
        item = event(calendar_id="remote")
        store.apply("Create", [item])
        pending = store.pending_changes()[0]
        store.apply("Edit", [replace(item, title="New")])
        assert len(store.pending_changes()) == 1
        store.acknowledge_change(pending["id"])
        assert store.pending_changes()[0]["payload"]["title"] == "New"
        store.apply("Delete", delete_ids=[item.id])
        assert store.pending_changes()[0]["action"] == "delete"
        store.undo()
        assert store.pending_changes()[0]["action"] == "upsert"


def test_transaction_rolls_back_on_invalid_batch(tmp_path):
    with Store(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            store.apply("Bad batch", [event("a"), event("b", calendar_id="missing")])
        assert store.list_events() == []
        assert store.history() == []
        assert store.pending_changes() == []


def test_read_only_imports_and_local_history_are_separate(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("feed", "Holidays", "#123456", kind="ics", url="https://example.invalid/calendar.ics"))
        item = event(calendar_id="feed")
        store.apply_remote([item])
        assert store.history() == []
        with pytest.raises(ValueError):
            store.apply("Delete", delete_ids=[item.id])
        recurring = event("recurring", read_only=True)
        store.apply_remote([recurring])
        with pytest.raises(ValueError):
            store.apply("Edit recurrence", [replace(recurring, title="Edited")])


def test_remote_update_refuses_pending_local_changes_atomically(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        item = event(calendar_id="remote")
        store.apply("Create", [item])
        with pytest.raises(HistoryConflict):
            store.apply_remote([replace(item, title="Remote"), event("other")])
        assert store.get_event(item.id) == item
        assert store.get_event("other") is None


def test_range_overlap_and_timezone_ordering(tmp_path):
    with Store(tmp_path / "db") as store:
        a = event("a")
        b = event("b", start=a.start.astimezone(timezone(timedelta(hours=7))), end=a.end.astimezone(timezone(timedelta(hours=7))))
        store.apply("Create", [a, b])
        assert len(store.list_events(start=a.start + timedelta(minutes=30), end=a.end)) == 2
        assert store.list_events(start=a.end) == []
        assert store.list_events(calendar_ids=[]) == []
        with pytest.raises(ValueError):
            store.list_events(start=datetime(2026, 1, 1))


def test_sync_metadata_and_settings_persist(tmp_path):
    path = tmp_path / "db"
    with Store(path) as store:
        store.set_sync_state("local", "a", "https://example.invalid/a.ics", '"v1"', {"ics": "DATA"})
        store.set_setting("shortcuts", {"new": "Ctrl+N"})
    with Store(path) as store:
        assert store.get_sync_state("local", "a")["etag"] == '"v1"'
        assert len(store.list_sync_state("local")) == 1
        assert store.get_setting("shortcuts")["new"] == "Ctrl+N"
        store.delete_sync_state("local", "a")
        assert store.list_sync_state("local") == []


def test_reject_naive_or_backwards_event_times():
    with pytest.raises(ValueError):
        event(start=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        event(end=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_history_reads_event_state_in_batches(tmp_path):
    with Store(tmp_path / "db") as store:
        for index in range(40):
            store.apply("Edit", [event(title=f"Title {index}")])
        statements = []
        store.connection.set_trace_callback(statements.append)
        rows = store.history()
        store.connection.set_trace_callback(None)
        assert len(rows) == 40
        assert sum(row["can_undo"] for row in rows) == 1
        assert len([sql for sql in statements if sql.startswith("SELECT")]) == 2
        assert len(store.history(limit=5)) == 5
        assert store.history(limit=0) == []


def test_calendar_source_cannot_change_after_last_event_deleted(tmp_path):
    with Store(tmp_path / "db") as store:
        calendar = Calendar("remote", "Work", "#123456", kind="caldav", url="https://one.invalid/")
        store.save_calendar(calendar)
        store.apply("Create", [event(calendar_id="remote")])
        store.apply("Delete", delete_ids=["a"])
        with pytest.raises(ValueError):
            store.save_calendar(replace(calendar, url="https://two.invalid/"))
        assert store.get_calendar("remote").url == "https://one.invalid/"


def test_cross_calendar_reassignment_rolls_back_entire_operation(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("second", "Other", "#abcdef"))
        a = event()
        store.apply("Create", [a])
        with pytest.raises(ValueError):
            store.apply("Reassign", [event("b"), replace(a, calendar_id="second")])
        assert store.get_event("a") == a
        assert store.get_event("b") is None
        assert len(store.history()) == 1


def test_separate_connections_can_initialize_and_write_concurrently(tmp_path):
    path = tmp_path / "db"
    barrier = threading.Barrier(2)

    def worker(prefix):
        barrier.wait(timeout=5)
        with Store(path) as store:
            for index in range(20):
                store.apply("Concurrent create", [event(f"{prefix}-{index}")])
                store.history(limit=3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, prefix) for prefix in ("ui", "sync")]
        for future in futures:
            future.result(timeout=10)
    with Store(path) as store:
        assert len(store.list_events()) == 40
        assert len(store.history()) == 40


def test_accept_remote_replaces_event_and_discards_reviewed_outbox_atomically(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        local = event(calendar_id="remote")
        remote = replace(local, title="Server version")
        store.apply("Local edit", [local])
        change = store.pending_changes()[0]
        store.accept_remote("remote", local.id, local, [change["id"]], upserts=[remote])
        assert store.get_event(local.id) == remote
        assert store.pending_changes() == []
        assert not store.history()[0]["can_undo"]


def test_accept_remote_refuses_changes_after_review(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        reviewed = event(calendar_id="remote")
        store.apply("Local edit", [reviewed])
        stale_id = store.pending_changes()[0]["id"]
        latest = replace(reviewed, title="New local edit")
        store.apply("Another edit", [latest])
        with pytest.raises(HistoryConflict):
            store.accept_remote("remote", reviewed.id, reviewed, [stale_id], delete_ids=[reviewed.id])
        assert store.get_event(reviewed.id) == latest
        assert len(store.pending_changes()) == 1
        assert store.pending_changes()[0]["id"] != stale_id


def test_accept_remote_rolls_back_ack_on_failed_replacement(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        local = event(calendar_id="remote")
        store.apply("Local edit", [local])
        change = store.pending_changes()[0]
        # Force a failure after reviewed outbox rows would have been removed.
        with pytest.raises(sqlite3.IntegrityError):
            with patch.object(store, "_write_event", side_effect=sqlite3.IntegrityError("write failed")):
                store.accept_remote("remote", local.id, local, [change["id"]], upserts=[replace(local, title="Server")])
        assert store.get_event(local.id) == local
        assert store.pending_changes()[0]["id"] == change["id"]


def test_source_read_only_calendar_rejects_local_edits_and_undo(tmp_path):
    with Store(tmp_path / "db") as store:
        calendar = Calendar("remote", "Work", "#123456", kind="caldav")
        store.save_calendar(calendar)
        item = event(calendar_id="remote")
        store.apply("Create", [item])
        store.save_calendar(replace(calendar, read_only=True))
        with pytest.raises(ValueError, match="read-only"):
            store.apply("Edit", [replace(item, title="Changed")])
        with pytest.raises(ValueError, match="read-only"):
            store.undo()
        assert store.get_event(item.id) == item


def test_metadata_only_sync_does_not_disable_undo_or_drop_source_properties(tmp_path):
    with Store(tmp_path / "db") as store:
        item = event(title="Original")
        store.apply("Create", [item])
        edit = replace(item, title="Edited")
        store.apply("Edit", [edit])
        store.apply_remote([replace(edit, raw_ics="LATEST SOURCE PROPERTIES")])
        assert store.history()[0]["can_undo"]
        store.undo()
        assert store.get_event(item.id).title == "Original"
        assert store.get_event(item.id).raw_ics == "LATEST SOURCE PROPERTIES"
        store.redo()
        assert store.get_event(item.id).title == "Edited"
        assert store.get_event(item.id).raw_ics == "LATEST SOURCE PROPERTIES"
