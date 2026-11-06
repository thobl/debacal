from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from debacal.conflict_demo import DemoConflictEngine
from debacal.models import Event
from debacal.store import Store


@pytest.fixture
def demo(tmp_path):
    with Store(tmp_path / "demo.sqlite") as store:
        start = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
        local = Event("example", "local", "Demo meeting", start, start + timedelta(hours=1), location="Local room")
        store.apply("Seed demo", [local])
        engine = DemoConflictEngine(store)
        yield store, local, engine
        engine.close()


def test_initial_demo_conflict_has_same_uid_different_time_and_notification(demo):
    store, local, engine = demo
    conflict = engine.conflicts[0]
    assert conflict.local == local
    assert conflict.remote.ical_uid == local.ical_uid
    assert conflict.remote.start == local.start + timedelta(hours=2)
    assert conflict.remote.location != local.location
    message = engine.events.get_nowait()
    assert message.kind == "conflict" and message.conflict == conflict
    assert "Offline demo" in message.message
    assert engine.credentials is None


def test_local_choice_keeps_local_version_and_removes_review(demo):
    store, local, engine = demo
    conflict = engine.conflicts[0]
    engine.resolve_conflict(conflict.id, "local", expected=conflict)
    assert store.get_event(local.id) == local
    assert len(store.history()) == 1
    assert engine.conflicts == []
    assert engine.events.get_nowait().kind == "conflict"
    assert engine.events.get_nowait().kind == "updated"


def test_server_choice_is_one_undoable_local_operation(demo):
    store, local, engine = demo
    conflict = engine.conflicts[0]
    engine.resolve_conflict(conflict.id, "remote", expected=conflict)
    assert store.get_event(local.id) == conflict.remote
    assert len(store.history()) == 2
    assert engine.conflicts == []
    store.undo()
    assert store.get_event(local.id) == local


def test_postponing_and_sync_do_not_change_events_or_resolve_conflict(demo):
    store, local, engine = demo
    conflict = engine.conflicts[0]
    engine.sync_now()
    assert engine.conflicts == [conflict]
    assert store.get_event(local.id) == local
    assert len(store.history()) == 1
    engine.events.get_nowait()
    message = engine.events.get_nowait()
    assert message.kind == "finished" and "no server connection" in message.message


def test_stale_local_edit_requires_fresh_review_before_server_choice(demo):
    store, local, engine = demo
    displayed = engine.conflicts[0]
    latest = replace(local, title="Edited after comparison opened")
    store.apply("Latest local edit", [latest])
    engine.resolve_conflict(displayed.id, "remote", expected=displayed)
    assert store.get_event(local.id) == latest
    refreshed = engine.conflicts[0]
    assert refreshed.local == latest
    assert "changed" in refreshed.reason
    engine.resolve_conflict(displayed.id, "remote", expected=displayed)
    assert store.get_event(local.id) == latest
    engine.resolve_conflict(displayed.id, "remote", expected=engine.conflicts[0])
    assert store.get_event(local.id) == displayed.remote
    assert engine.conflicts == []


def test_demo_creates_only_a_local_example_when_given_empty_disposable_store(tmp_path):
    with Store(tmp_path / "empty-demo.sqlite") as store:
        engine = DemoConflictEngine(store)
        assert len(engine.conflicts) == len(store.list_events()) == 1
        assert store.list_events()[0].calendar_id == "local"
        engine.close()
