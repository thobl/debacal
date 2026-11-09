"""Persistent search candidates stay aligned with every event write path."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from debacal.models import Calendar, Event
from debacal.recurrence import occurrence, recurrence_key
from debacal.store import Store


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def event(identifier="event", **changes):
    return replace(Event(identifier, "local", "Synthetic", START, START + timedelta(hours=1)), **changes)


def index(store):
    return {identifier: (calendar, text) for identifier, calendar, text in store.event_search_index()}


def test_projection_preserves_literal_unicode_substrings_and_excluded_overrides():
    master = event(title="Straße & Σίσυφος", description="100%_literal\nnotes", location="Zürich",
                   recurrence_rule="FREQ=DAILY;COUNT=3")
    visible_key = recurrence_key(START + timedelta(days=1))
    hidden_key = recurrence_key(START + timedelta(days=2))
    visible = replace(occurrence(master, START + timedelta(days=1)), title="Überraschung", location="東京")
    hidden = replace(occurrence(master, START + timedelta(days=2)), title="Excluded-only phrase")
    master = replace(master, recurrence_overrides={visible_key: visible.to_dict(), hidden_key: hidden.to_dict()},
                     excluded_dates=(hidden_key,))
    with Store(":memory:") as store:
        store.apply_remote([master])
        with patch("debacal.store.json.loads", side_effect=AssertionError("The index needs no JSON decoding")):
            assert store.event_search_index() == [(master.id, master.calendar_id,
                "straße & σίσυφοσ 100%_literal\nnotes zürich".casefold() + "\0" +
                "überraschung 100%_literal\nnotes 東京".casefold())]
        text = index(store)[master.id][1]
        for query in ("strasse", "σίσυφοσ", "100%_literal", "überraschung", "東京"):
            assert query.casefold() in text
        assert "excluded-only" not in text
        # No normalization/tokenization beyond the existing casefold substring.
        assert "zu\u0308rich" not in text and "100anythingliteral" not in text


def test_projection_is_ordered_like_resource_list_and_bulk_fetch_is_narrow():
    with Store(":memory:") as store:
        values = [event(str(number), start=START + timedelta(hours=number % 4),
                        end=START + timedelta(hours=number % 4 + 1)) for number in range(1000)]
        store.apply_remote(values)
        expected = sorted(values, key=lambda value: (value.start.timestamp(), value.end.timestamp(), value.id))
        with patch.object(Event, "from_dict", side_effect=AssertionError("Projection must not construct Event models")):
            assert [row[0] for row in store.event_search_index()] == [value.id for value in expected]
        identifiers = ["1", "7", "999", "missing", "1"]
        with patch.object(Event, "from_dict", wraps=Event.from_dict) as decode:
            fetched = store.get_events(identifiers)
            assert store.get_events([]) == []
        assert {value.id for value in fetched} == {"1", "7", "999"}
        assert decode.call_count == 3
        assert len(store.get_events(value.id for value in values)) == 1000


def test_projection_tracks_edits_undo_redo_delete_and_rollback():
    with Store(":memory:") as store:
        initial = event(title="Initial", location="Original place")
        creation = store.apply("Create", [initial])
        edited = replace(initial, title="Updated", description="Changed notes", location="New place")
        operation = store.apply("Edit", [edited])
        assert index(store)[initial.id][1] == "updated changed notes new place"
        store.undo(operation)
        assert index(store)[initial.id][1] == "initial  original place"
        store.redo(operation)
        assert index(store)[initial.id][1] == "updated changed notes new place"
        with pytest.raises(RuntimeError):
            with store._transaction():
                store._write_event(replace(edited, title="Uncommitted"))
                raise RuntimeError("Synthetic rollback")
        assert "uncommitted" not in index(store)[initial.id][1]
        deletion = store.apply("Delete", delete_ids=[initial.id])
        assert store.event_search_index() == []
        store.undo(deletion)
        assert index(store)[initial.id][1] == "updated changed notes new place"
        store.undo(operation)
        store.undo(creation)
        assert store.event_search_index() == []


def test_projection_tracks_remote_writes_conflict_acceptance_and_other_connections(tmp_path):
    path = tmp_path / "calendar.sqlite"
    with Store(path) as store, Store(path) as worker:
        store.save_calendar(Calendar("remote", "Synthetic remote", "#123456", kind="caldav"))
        initial = event(calendar_id="remote", title="Initial")
        worker.apply_remote([initial])
        first_revision = store.events_revision()
        assert index(store)[initial.id] == ("remote", "initial  ")
        edited = replace(initial, title="Local edit")
        store.apply("Edit", [edited])
        reviewed = [value["id"] for value in store.pending_changes("remote")]
        accepted = replace(initial, title="Server accepted", location="Shared place")
        worker.accept_remote("remote", initial.id, edited, reviewed, upserts=[accepted])
        assert index(store)[initial.id] == ("remote", "server accepted  shared place")
        assert store.events_revision() > first_revision
        worker.apply_remote(delete_ids=[initial.id], calendar_id='remote')
        assert store.event_search_index() == []
    with Store(path) as reopened:
        assert reopened.event_search_index() == []


def test_projection_reindexes_single_override_edits_and_candidate_confirmation():
    with Store(":memory:") as store:
        master = event(title="Alternatives", recurrence_rule="FREQ=DAILY;COUNT=3", candidate=True)
        store.apply_remote([master])
        selected = occurrence(master, START + timedelta(days=1))
        operation = store.edit_occurrence(selected, replace(selected, title="Specific choice", description="Override note"))
        assert "specific choice" in index(store)[master.id][1]
        current = store.get_occurrence(selected.id)
        confirmation = store.confirm_candidate(current)
        assert index(store)[master.id][1] == "specific choice override note "
        assert "alternatives" not in index(store)[master.id][1]
        store.undo(confirmation)
        assert "alternatives" in index(store)[master.id][1] and "specific choice" in index(store)[master.id][1]
        store.undo(operation)
        assert "specific choice" not in index(store)[master.id][1]


def test_existing_database_backfill_is_persistent_without_changing_event_revision(tmp_path):
    path = tmp_path / "calendar.sqlite"
    source = event(title="Straße", location="Backfill place")
    with Store(path) as store:
        store.apply_remote([source])
        revision = store.events_revision()
        # Emulate the pre-projection database's absent migration marker.
        with store._transaction():
            store.connection.execute("UPDATE events SET search_text=''")
            store.connection.execute("DELETE FROM settings WHERE key='event_search_index_v1'")
    with Store(path) as reopened:
        assert index(reopened)[source.id][1] == "strasse  backfill place"
        assert reopened.events_revision() == revision
        assert reopened.get_event(source.id) == source
    with patch("debacal.store.json.loads", wraps=json.loads) as decode:
        with Store(path):
            pass
    assert decode.call_count == 3  # Migration markers and one eager source decode.
