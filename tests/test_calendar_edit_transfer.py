"""Calendar edits are resource transfers, with one durable undo operation."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.series_operations import partition_series
from debacal.store import HistoryConflict, Store
from debacal.vdirsyncer_backend import VdirsyncerBackend


ZONE = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 9, 9, tzinfo=ZONE)
BOUNDS = START - timedelta(days=1), START + timedelta(days=90)


def event(identifier="meeting", **changes):
    return replace(Event(identifier, "source", "Meeting", START, START + timedelta(hours=1),
                         ical_uid=identifier + "@example.invalid"), **changes)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "calendar.sqlite") as value:
        for identifier in ("source", "target"):
            value.save_calendar(Calendar(identifier, identifier.title(), "#118899", kind="caldav",
                                         url=f"https://calendar.test/{identifier}/", username="test"))
        yield value


def pending(store):
    return {(change["calendar_id"], change["event_id"]): change["action"]
            for change in store.pending_changes()}


def target_event(store):
    return store.list_events(calendar_ids=["target"])[0]


def dated(store, master):
    return [value for value in store.query_occurrences(*BOUNDS) if value.series_id == master.id]


def test_single_resource_transfer_retains_uid_and_undo_reverses_both_outboxes(store):
    source = event(description="Notes", location="Room", group_id="choices", candidate=True)
    source = replace(source, raw_ics=export_ics([source]).decode())
    store.apply_remote([source])
    store.set_sync_state("source", source.id, "https://calendar.test/source/meeting.ics", "etag",
                         {"event": source.to_dict()})
    operation = store.edit_selection([source], "single", source,
                                     replace(source, calendar_id="target", title="Moved"))
    moved = target_event(store)
    assert store.get_event(source.id) is None
    assert moved.id != source.id and moved.ical_uid == source.ical_uid
    assert moved.title == "Moved" and moved.description == "Notes" and moved.location == "Room"
    assert moved.group_id == "choices" and moved.candidate
    assert pending(store) == {("source", source.id): "delete", ("target", moved.id): "upsert"}
    assert store.get_sync_state("source", source.id)["etag"] == "etag"
    assert len(store.history()) == 1
    store.undo(operation)
    assert store.get_event(source.id) == source and store.get_event(moved.id) is None
    assert pending(store) == {("source", source.id): "upsert", ("target", moved.id): "delete"}
    store.redo(operation)
    assert store.get_event(moved.id) == moved and store.get_event(source.id) is None


def test_transferring_detached_occurrence_excludes_original_and_preserves_actual_range(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5", candidate=True)
    store.apply_remote([source])
    selected = dated(store, source)[2]
    store.edit_occurrence(selected, replace(selected, title="Exception", start=selected.start + timedelta(hours=2),
                                            end=selected.end + timedelta(hours=4)))
    selected = store.get_occurrence(selected.id)
    operation = store.edit_selection([selected], "single", selected, replace(selected, calendar_id="target"))
    moved = target_event(store)
    kept = store.get_event(source.id)
    assert selected.recurrence_id in kept.excluded_dates
    assert selected.recurrence_id not in kept.recurrence_overrides
    assert len(dated(store, source)) == 4
    assert not moved.recurring and not moved.series_id and not moved.recurrence_id
    assert moved.start == selected.start and moved.end == selected.end and moved.title == "Exception"
    assert moved.candidate and len(store.history()) == 2
    assert pending(store) == {("source", source.id): "upsert", ("target", moved.id): "upsert"}
    store.undo(operation)
    assert store.get_occurrence(selected.id) == selected


def test_multiple_occurrences_move_once_each_and_merge_exdates_in_one_operation(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=6")
    store.apply_remote([source])
    dates = dated(store, source)
    selected = [dates[1], dates[4], dates[1]]
    operation = store.edit_selection(selected, "single", dates[1], replace(dates[1], calendar_id="target"))
    moved = store.list_events(calendar_ids=["target"])
    assert len(moved) == 2 and len({value.ical_uid for value in moved}) == 2
    assert {value.start for value in moved} == {dates[1].start, dates[4].start}
    assert set(store.get_event(source.id).excluded_dates) == {dates[1].recurrence_id, dates[4].recurrence_id}
    assert len(pending(store)) == 3 and len(store.history()) == 1
    store.undo(operation)
    assert store.get_event(source.id) == source
    assert store.list_events(calendar_ids=["target"]) == []


def test_single_occurrence_transfer_inherits_alarm_and_unknown_source_properties(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    raw = export_ics([source]).decode().replace("END:VEVENT", "\r\n".join([
        "X-SOURCE-MARKER:Retained", "BEGIN:VALARM", "UID:alarm-identity", "ACTION:DISPLAY",
        "TRIGGER:-PT15M", "DESCRIPTION:Reminder", "END:VALARM", "END:VEVENT"]))
    source = replace(source, raw_ics=raw)
    store.apply_remote([source])
    selected = dated(store, source)[2]
    store.edit_selection([selected], "single", selected, replace(selected, calendar_id="target"))
    moved = target_event(store)
    serialized = export_ics([moved]).decode()
    assert "X-SOURCE-MARKER:Retained" in serialized and "UID:alarm-identity" in serialized
    assert serialized.count("BEGIN:VEVENT") == 1 and serialized.count("BEGIN:VALARM") == 1
    assert "RRULE:" not in serialized and "RECURRENCE-ID" not in serialized
    assert parse_ics(serialized, "target")[0].start == selected.start


def test_late_source_metadata_failure_rolls_back_every_selected_transfer(store):
    a, b = event("a"), event("b", raw_ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    store.apply_remote([a, b])
    with pytest.raises(ValueError, match="source calendar metadata"):
        store.edit_selection([a, b], "single", a, replace(a, calendar_id="target"))
    assert store.list_events() == [a, b] and not pending(store) and not store.history()


def test_whole_custom_series_transfer_keeps_schedule_and_detached_metadata(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=6", candidate=True,
                   schedule_expression="5*+1", expression_anchor=START.date().isoformat(), schedule_syntax=2)
    store.apply_remote([source])
    dates = dated(store, source)
    store.edit_occurrence(dates[3], replace(dates[3], title="Exception", end=dates[3].end + timedelta(hours=2)))
    source = store.get_event(source.id)
    original = dated(store, source)
    selected = original[1]
    store.edit_selection([selected], "group", source, replace(source, calendar_id="target"))
    moved = target_event(store)
    assert store.get_event(source.id) is None
    assert moved.ical_uid == source.ical_uid and moved.id != source.id
    assert moved.schedule_expression == source.schedule_expression
    assert moved.expression_anchor == source.expression_anchor and moved.schedule_syntax == 2
    assert moved.recurrence_rule == source.recurrence_rule and moved.candidate
    assert [(v.start, v.end, v.title) for v in dated(store, moved)] == [(v.start, v.end, v.title) for v in original]
    override = Event.from_dict(next(iter(moved.recurrence_overrides.values())))
    assert override.calendar_id == "target" and override.series_id == moved.id and override.ical_uid == moved.ical_uid


@pytest.mark.parametrize("index", [0, 2])
def test_following_transfer_splits_or_removes_source_without_losing_future(store, index):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    store.apply_remote([source])
    dates = dated(store, source)
    selected = dates[index]
    _, tail = partition_series(source, selected)
    operation = store.edit_selection([selected], "future", selected,
                                     replace(tail, calendar_id="target", title="Future"))
    moved = target_event(store)
    assert [value.start for value in dated(store, moved)] == [value.start for value in dates[index:]]
    assert all(value.title == "Future" for value in dated(store, moved))
    if index:
        assert [value.start for value in dated(store, source)] == [value.start for value in dates[:index]]
        assert pending(store)[("source", source.id)] == "upsert"
    else:
        assert store.get_event(source.id) is None
        assert pending(store)[("source", source.id)] == "delete"
    assert pending(store)[("target", moved.id)] == "upsert" and len(store.history()) == 1
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_mixed_selection_retains_calendars_until_explicit_target_is_requested(store):
    a, b = event("a"), event("b", calendar_id="target", start=START + timedelta(days=2), end=START + timedelta(days=2, hours=1))
    store.apply_remote([a, b])
    store.edit_selection([a, b], "single", b, replace(b, title="Shared"))
    a, b = store.get_event(a.id), store.get_event(b.id)
    assert a.calendar_id == "source" and b.calendar_id == "target"
    operation = store.edit_selection([a, b], "single", b, replace(b, title="Together"), calendar_id="target")
    assert store.get_event(a.id) is None and store.get_event(b.id).id == b.id
    moved = store.list_events(calendar_ids=["target"])
    assert len(moved) == 2 and {value.title for value in moved} == {"Together"}
    assert store.list_events(calendar_ids=["source"]) == []
    store.undo(operation)
    assert store.get_event(a.id) == a and store.get_event(b.id) == b


@pytest.mark.parametrize("source_calendar,target_calendar", [("local", "target"), ("source", "local"), ("local", "local-target")])
def test_local_and_remote_transfers_queue_only_remote_resource_changes(store, source_calendar, target_calendar):
    store.save_calendar(Calendar("local-target", "Local target", "#668899"))
    source = event(calendar_id=source_calendar)
    store.apply_remote([source])
    store.edit_selection([source], "single", source, replace(source, calendar_id=target_calendar))
    moved = store.list_events(calendar_ids=[target_calendar])[0]
    expected = {}
    if source_calendar == "source":
        expected[(source_calendar, source.id)] = "delete"
    if target_calendar == "target":
        expected[(target_calendar, moved.id)] = "upsert"
    assert pending(store) == expected
    assert store.get_event(source.id) is None and moved.ical_uid == source.ical_uid


def test_transfer_resource_identities_and_undo_survive_reopening_database(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    store.apply_remote([source])
    operation = store.edit_selection([source], "group", source, replace(source, calendar_id="target"))
    moved = target_event(store)
    with Store(store.path) as reopened:
        assert pending(reopened) == {("source", source.id): "delete", ("target", moved.id): "upsert"}
        assert reopened.get_event(moved.id) == moved
        reopened.undo(operation)
        assert reopened.get_event(source.id) == source
        assert reopened.get_event(moved.id) is None
        assert pending(reopened) == {("source", source.id): "upsert", ("target", moved.id): "delete"}


@pytest.mark.parametrize("collision", ["live", "pending-delete"])
def test_target_uid_collision_uses_new_uid_and_preserves_existing_resource(store, collision):
    source = event()
    existing = replace(source, id="existing", calendar_id="target", title="Existing")
    store.apply_remote([source, existing])
    if collision == "pending-delete":
        store.set_sync_state("target", existing.id, "vdirsyncer:" + existing.ical_uid, "etag", {"event": existing.to_dict()})
        store.apply("Delete existing", delete_ids=[existing.id])
    store.edit_selection([source], "single", source, replace(source, calendar_id="target"))
    moved = next(value for value in store.list_events(calendar_ids=["target"]) if value.id != existing.id)
    assert moved.ical_uid != source.ical_uid
    if collision == "live":
        assert store.get_event(existing.id) == existing
    else:
        assert pending(store)[("target", existing.id)] == "delete"


@pytest.mark.parametrize("changes", [{"read_only": True}, {"enabled": False}, {"kind": "ics"}])
@pytest.mark.parametrize("calendar_id", ["source", "target"])
def test_transfer_permission_failure_is_atomic(store, changes, calendar_id):
    source = event()
    destination = "target"
    if changes.get("kind") == "ics":
        store.save_calendar(Calendar("ics", "Subscription", "#118899", kind="ics"))
        if calendar_id == "source":
            source = replace(source, calendar_id="ics")
        else:
            destination = "ics"
    else:
        store.save_calendar(replace(store.get_calendar(calendar_id), **changes))
    store.apply_remote([source])
    with pytest.raises(ValueError, match="read-only|disabled"):
        store.edit_selection([source], "single", source, replace(source, calendar_id=destination))
    assert store.list_events() == [source] and not pending(store) and not store.history()


def test_stale_group_review_rejects_transfer_and_leaves_all_resources_intact(store):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    store.apply_remote([source])
    selected = dated(store, source)[1]
    stale = store.list_events()
    store.apply_remote([replace(source, location="Changed elsewhere")])
    with pytest.raises(HistoryConflict):
        store.edit_selection([selected], "group", selected, replace(selected, calendar_id="target"), expected_masters=stale)
    assert len(store.list_events()) == 1 and not pending(store) and not store.history()


def test_later_read_only_selection_member_rolls_back_entire_transfer(store):
    a, b = event("a"), event("b", read_only=True)
    store.apply_remote([a, b])
    with pytest.raises(ValueError, match="read-only"):
        store.edit_selection([a, b], "single", a, replace(a, calendar_id="target"))
    assert store.list_events() == [a, b] and not pending(store) and not store.history()


@pytest.mark.parametrize("method,scope", [("edit_occurrence", "single"), ("edit_series", "group"), ("edit_future", "future")])
def test_legacy_edit_apis_also_support_calendar_transfer(store, method, scope):
    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    store.apply_remote([source])
    selected = dated(store, source)[2]
    base = source if scope == "group" else partition_series(source, selected)[1] if scope == "future" else selected
    edited = replace(base, start=selected.start, end=selected.end, calendar_id="target")
    getattr(store, method)(selected, edited)
    moved = target_event(store)
    assert moved.calendar_id == "target"
    assert len(dated(store, moved)) == {"single": 0, "group": 5, "future": 3}[scope]


def test_remote_to_remote_transfer_and_undo_sync_through_independent_bridges(store):
    from test_vdirsyncer import FakeCLI

    source = event(recurrence_rule="FREQ=WEEKLY;COUNT=5")
    store.apply_remote([source])
    calendars = {key: store.get_calendar(key) for key in ("source", "target")}
    transports = {key: FakeCLI() for key in calendars}
    transports["source"].remote[source.ical_uid] = export_ics([source]).decode()
    backends = {key: VdirsyncerBackend(store.path, runner=transports[key]) for key in calendars}
    for key, backend in backends.items():
        backend.sync(store, calendars[key], "not-a-real-password")
    selected = dated(store, source)[1]
    source = store.get_event(source.id)
    operation = store.edit_selection([selected], "group", source, replace(source, calendar_id="target"))
    moved = target_event(store)
    # Upload target first: the source delete must remain independently queued.
    backends["target"].sync(store, calendars["target"], "not-a-real-password")
    assert source.ical_uid in transports["source"].remote
    assert pending(store) == {("source", source.id): "delete"}
    backends["source"].sync(store, calendars["source"], "not-a-real-password")
    assert not transports["source"].remote and not pending(store)
    assert len(parse_ics(transports["target"].remote[moved.ical_uid], "target")) == 1
    assert len(dated(store, moved)) == 5
    store.undo(operation)
    for key in ("source", "target"):
        backends[key].sync(store, calendars[key], "not-a-real-password")
    assert source.ical_uid in transports["source"].remote and not transports["target"].remote
    assert not pending(store) and store.get_event(source.id).recurrence_rule == source.recurrence_rule
