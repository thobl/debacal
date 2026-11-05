"""Persistence, history, and wire-format contracts for following-event edits."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lebacal.ics import export_ics, parse_ics
from lebacal.models import Calendar, Event
from lebacal.recurrence import expand_events, recurrence_key
from lebacal.store import HistoryConflict, Store


BERLIN = ZoneInfo("Europe/Berlin")
BOUNDS = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2027, 2, 1, tzinfo=BERLIN)


def series(**changes):
    start = datetime(2026, 10, 20, 9, tzinfo=BERLIN)
    return replace(Event("series", "local", "Meeting", start, start + timedelta(hours=1),
                         ical_uid="meeting@example.invalid", recurrence_rule="FREQ=WEEKLY;COUNT=5"), **changes)


def edit(reference, master, **changes):
    """An editor keeps the complete master recurrence metadata on submission."""
    return replace(reference, recurrence_rule=master.recurrence_rule,
                   recurrence_dates=master.recurrence_dates, excluded_dates=master.excluded_dates,
                   recurrence_overrides=master.recurrence_overrides,
                   schedule_expression=master.schedule_expression, expression_anchor=master.expression_anchor,
                   schedule_syntax=master.schedule_syntax, **changes)


def tail_of(store):
    return next(event for event in store.list_events() if event.id != "series")


def test_following_count_split_is_one_durable_operation_with_two_outbox_resources(tmp_path):
    path = tmp_path / "db"
    source = series(calendar_id="remote")
    with Store(path) as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        store.apply("Create", [source])
        for change in store.pending_changes():
            store.acknowledge_change(change["id"])
        original = store.query_occurrences(*BOUNDS)
        selected = original[2]
        operation = store.edit_future(selected, edit(selected, source, title="Future meeting"), expected_master=source)
        head, tail = store.get_event(source.id), tail_of(store)
        assert head.ical_uid == source.ical_uid
        assert tail.id != source.id and tail.ical_uid not in {source.ical_uid, ""}
        assert "COUNT=2" in head.recurrence_rule
        assert "COUNT=3" in tail.recurrence_rule
        assert [event.title for event in store.query_occurrences(*BOUNDS)] == ["Meeting"] * 2 + ["Future meeting"] * 3
        pending = store.pending_changes()
        assert {change["event_id"] for change in pending} == {head.id, tail.id}
        assert all(change["action"] == "upsert" and not change["payload"]["series_id"] for change in pending)
        assert len(store.history()) == 2
        tail_id = tail.id
    with Store(path) as store:
        store.undo(operation)
        assert store.get_event(source.id) == source
        assert store.get_event(tail_id) is None
        assert store.query_occurrences(*BOUNDS) == original
        assert {change["event_id"]: change["action"] for change in store.pending_changes()} == {source.id: "upsert", tail_id: "delete"}
        store.redo(operation)
        assert store.get_event(tail_id).title == "Future meeting"


def test_following_move_keeps_infinite_rule_and_local_hour_across_dst(tmp_path):
    source = series(start=datetime(2026, 10, 19, 9, tzinfo=BERLIN),
                    end=datetime(2026, 10, 19, 10, tzinfo=BERLIN),
                    recurrence_rule="FREQ=WEEKLY;BYDAY=MO")
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[1]
        moved = edit(selected, source, start=selected.start + timedelta(days=2), end=selected.end + timedelta(days=2))
        store.edit_future(selected, moved)
        tail = tail_of(store)
        assert "UNTIL=" in store.get_event(source.id).recurrence_rule
        assert "BYDAY=WE" in tail.recurrence_rule
        assert "COUNT=" not in tail.recurrence_rule and "UNTIL=" not in tail.recurrence_rule
        events = store.query_occurrences(*BOUNDS)
        assert events[0].start == source.start
        assert all(event.start.weekday() == 2 and event.start.hour == 9 for event in events[1:])
        assert events[0].start.utcoffset() == timedelta(hours=2)
        assert events[1].start.utcoffset() == timedelta(hours=1)


def test_first_occurrence_following_edit_and_delete_keep_single_identity(tmp_path):
    source = series()
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[0]
        operation = store.edit_future(selected, edit(selected, source, title="Renamed"))
        assert len(store.list_events()) == 1
        assert store.get_event(source.id).ical_uid == source.ical_uid
        assert len(store.query_occurrences(*BOUNDS)) == 5
        store.undo(operation)
        deleted = store.delete_future(store.query_occurrences(*BOUNDS)[0])
        assert store.list_events() == []
        store.undo(deleted)
        assert store.list_events() == [source]


def test_no_empty_head_when_all_prior_dates_are_excluded(tmp_path):
    source = series(excluded_dates=(recurrence_key(datetime(2026, 10, 20, 9, tzinfo=BERLIN)),))
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[0]
        store.delete_future(selected)
        assert store.list_events() == []
        store.undo()
        assert store.list_events() == [source]


def test_delete_following_partitions_by_original_key_of_moved_override(tmp_path):
    source = series(recurrence_rule="FREQ=WEEKLY;COUNT=6")
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        # A past occurrence moved into the future must remain; a future one
        # moved before the split must be removed with its original siblings.
        store.edit_occurrence(original[0], replace(original[0], start=original[4].start + timedelta(days=1), end=original[4].end + timedelta(days=1)))
        store.edit_occurrence(original[4], replace(original[4], start=original[0].start + timedelta(days=1), end=original[0].end + timedelta(days=1)))
        source = store.get_event(source.id)
        selected = store.get_occurrence(original[2].id)
        operation = store.delete_future(selected, expected_master=source)
        kept = store.query_occurrences(*BOUNDS)
        assert {event.recurrence_id for event in kept} == {original[0].recurrence_id, original[1].recurrence_id}
        assert original[0].recurrence_id in store.get_event(source.id).recurrence_overrides
        assert original[4].recurrence_id not in store.get_event(source.id).recurrence_overrides
        store.undo(operation)
        assert store.get_event(source.id) == source


def test_following_count_counts_excluded_occurrences_and_rekeys_overrides(tmp_path):
    source = series(recurrence_rule="FREQ=WEEKLY;COUNT=6")
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        store.delete_occurrence(original[1])
        store.delete_occurrence(original[3])
        store.edit_occurrence(original[4], replace(original[4], title="Exception", start=original[4].start + timedelta(hours=2), end=original[4].end + timedelta(hours=2)))
        source = store.get_event(source.id)
        selected = store.get_occurrence(original[2].id)
        store.edit_future(selected, edit(selected, source, title="New common title", end=selected.end + timedelta(hours=1)))
        tail = tail_of(store)
        assert "COUNT=2" in store.get_event(source.id).recurrence_rule
        assert "COUNT=4" in tail.recurrence_rule
        assert set(tail.excluded_dates) == {original[3].recurrence_id}
        assert set(store.get_event(source.id).excluded_dates) == {original[1].recurrence_id}
        override = Event.from_dict(tail.recurrence_overrides[original[4].recurrence_id])
        assert override.series_id == tail.id and override.ical_uid == tail.ical_uid
        assert override.id.startswith(tail.id + "::occurrence::")
        assert override.title == "New common title" and override.start.hour == 11 and override.end.hour == 13
        assert len(store.query_occurrences(*BOUNDS)) == 4


def test_rdate_schedule_replacement_removes_old_tail_anchor_and_undo_restores(tmp_path):
    source = series(recurrence_rule="", recurrence_dates=tuple(recurrence_key(datetime(2026, month, day, 9, tzinfo=BERLIN))
                                                              for month, day in [(10, 23), (11, 10), (12, 1)]))
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        selected = original[2]
        new_start = datetime(2026, 11, 12, 14, tzinfo=BERLIN)
        edited = replace(selected, start=new_start, end=new_start + timedelta(minutes=45),
                         recurrence_dates=(recurrence_key(new_start + timedelta(days=7)),))
        operation = store.edit_future(selected, edited, replace_schedule=True)
        events = store.query_occurrences(*BOUNDS)
        assert [event.start for event in events] == [original[0].start, original[1].start, new_start, new_start + timedelta(days=7)]
        assert all(event.end - event.start == timedelta(minutes=45) for event in events[2:])
        store.undo(operation)
        assert store.query_occurrences(*BOUNDS) == original


def test_following_snapshot_is_read_only_and_has_only_remaining_dates(tmp_path):
    source = series()
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[2]
        snapshot = store.future_snapshot(selected, expected_master=source)
        assert len(expand_events([snapshot], *BOUNDS)) == 3
        assert snapshot.id == source.id and snapshot.ical_uid == source.ical_uid
        assert store.list_events() == [source] and len(store.history()) == 1


@pytest.mark.parametrize("operation", ["edit", "delete", "snapshot"])
def test_stale_selected_occurrence_never_splits_or_deletes_current_series(tmp_path, operation):
    with Store(tmp_path / "db") as store:
        source = series()
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[2]
        store.edit_occurrence(selected, replace(selected, title="Latest"))
        with pytest.raises(HistoryConflict):
            if operation == "edit":
                store.edit_future(selected, edit(selected, source, title="Stale"))
            elif operation == "delete":
                store.delete_future(selected)
            else:
                store.future_snapshot(selected)
        assert len(store.list_events()) == 1
        assert store.get_occurrence(selected.id).title == "Latest"


def test_expected_master_protects_sibling_only_change_from_other_connection(tmp_path):
    path = tmp_path / "db"
    with Store(path) as store, Store(path) as other:
        source = series()
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        other.delete_occurrence(original[4])
        with pytest.raises(HistoryConflict, match="series changed"):
            store.edit_future(original[2], edit(original[2], source, title="Stale"), expected_master=source)
        with pytest.raises(HistoryConflict):
            store.delete_future(original[2], expected_master=source)
        assert len(store.list_events()) == 1
        assert store.get_event(source.id).excluded_dates == (original[4].recurrence_id,)


def test_following_edit_respects_read_only_calendar_without_partial_write(tmp_path):
    source = series(calendar_id="subscription")
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("subscription", "Read only", "#123456", kind="ics"))
        store.apply_remote([source])
        selected = store.query_occurrences(*BOUNDS)[2]
        with pytest.raises(ValueError, match="read-only"):
            store.edit_future(selected, edit(selected, source, title="Denied"))
        with pytest.raises(ValueError, match="read-only"):
            store.delete_future(selected)
        assert store.list_events() == [source] and store.history() == []


def test_split_resources_round_trip_standard_ics_without_materializing_virtual_rows(tmp_path):
    source = series()
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        selected = store.query_occurrences(*BOUNDS)[2]
        store.edit_future(selected, edit(selected, source, start=selected.start + timedelta(days=1), end=selected.end + timedelta(days=1)))
        expected = store.query_occurrences(*BOUNDS)
        resources = store.list_events()
        wire = export_ics(resources)
        assert wire.count(b"BEGIN:VEVENT") == 2
        assert wire.count(b"RRULE:") == 2
        restored = parse_ics(wire, "local")
        assert {event.ical_uid for event in restored} == {event.ical_uid for event in resources}
        actual = expand_events(restored, *BOUNDS)
        assert [(event.start.timestamp(), event.end.timestamp(), event.title) for event in actual] == [
            (event.start.timestamp(), event.end.timestamp(), event.title) for event in expected]
        assert all(not event.series_id for event in resources)


def test_off_rule_rdate_pivot_does_not_restart_the_rule_interval(tmp_path):
    extra = datetime(2026, 10, 23, 9, tzinfo=BERLIN)
    source = series(recurrence_rule="FREQ=WEEKLY;INTERVAL=2;COUNT=5",
                    recurrence_dates=(recurrence_key(extra),))
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        selected = next(event for event in original if event.start == extra)
        store.edit_future(selected, edit(selected, source, title="Remaining"))
        actual = store.query_occurrences(*BOUNDS)
        assert [event.start for event in actual] == [event.start for event in original]
        assert [event.title for event in actual] == ["Meeting"] + ["Remaining"] * 5
        tail = tail_of(store)
        assert tail.start == datetime(2026, 11, 3, 9, tzinfo=BERLIN)
        assert recurrence_key(extra) in tail.recurrence_dates
        wire = export_ics(store.list_events())
        restored = expand_events(parse_ics(wire, "local"), *BOUNDS)
        assert [event.start.timestamp() for event in restored] == [event.start.timestamp() for event in original]


def test_all_day_following_delete_keeps_date_identity_and_standard_until(tmp_path):
    start = datetime(2026, 10, 20, tzinfo=BERLIN)
    source = series(start=start, end=start + timedelta(days=1), all_day=True,
                    recurrence_rule="FREQ=DAILY;UNTIL=20261025")
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        original = store.query_occurrences(*BOUNDS)
        operation = store.delete_future(original[3])
        kept = store.get_event(source.id)
        assert "UNTIL=20261022" in kept.recurrence_rule
        assert [event.recurrence_id for event in store.query_occurrences(*BOUNDS)] == ["2026-10-20", "2026-10-21", "2026-10-22"]
        assert b"UNTIL=20261022" in export_ics([kept])
        store.undo(operation)
        assert store.query_occurrences(*BOUNDS) == original


def test_ordinary_event_following_scope_is_an_ordinary_undoable_edit(tmp_path):
    source = series(recurrence_rule="")
    with Store(tmp_path / "db") as store:
        store.apply("Create", [source])
        assert store.future_snapshot(source) == source
        operation = store.edit_future(source, replace(source, title="Ordinary edit"))
        assert len(store.list_events()) == 1 and store.get_event(source.id).title == "Ordinary edit"
        store.undo(operation)
        assert store.list_events() == [source]


def mouse_series(store):
    source = series(recurrence_rule="FREQ=WEEKLY")
    store.apply("Create", [source])
    original = store.query_occurrences(*BOUNDS)
    store.edit_occurrence(original[0], replace(original[0], title="Past exception", end=original[0].start + timedelta(minutes=15)))
    store.edit_occurrence(original[2], replace(original[2], title="Long exception", description="Custom notes", location="Other room",
                                             start=original[2].start + timedelta(hours=1), end=original[2].end + timedelta(hours=2)))
    store.edit_occurrence(original[3], replace(original[3], title="Short exception", end=original[3].start + timedelta(minutes=30)))
    return store.get_event(source.id), store.get_occurrence(original[2].id)


@pytest.mark.parametrize("scope", ["group", "future"])
def test_mouse_move_preserves_selected_exception_and_regular_master_durations(tmp_path, scope):
    with Store(tmp_path / "db") as store:
        source, selected = mouse_series(store)
        before_history = len(store.history())
        shift = timedelta(days=1, hours=1)
        # Mouse submission uses the master metadata with the actual selected
        # event's moved bounds, rather than the occurrence's empty RRULE.
        edited = replace(source, start=selected.start + shift, end=selected.end + shift)
        operation = (store.edit_series(selected, edited, preserve_exceptions=True) if scope == "group" else
                     store.edit_future(selected, edited, expected_master=source, preserve_exceptions=True))
        assert len(store.history()) == before_history + 1
        affected = store.get_event(source.id) if scope == "group" else tail_of(store)
        assert affected.end - affected.start == timedelta(hours=1)
        assert affected.start.hour == 10 and affected.start.weekday() == 2
        assert "COUNT=" not in affected.recurrence_rule and "UNTIL=" not in affected.recurrence_rule
        events = store.query_occurrences(*BOUNDS)
        chosen = next(event for event in events if event.title == "Long exception")
        sibling = next(event for event in events if event.title == "Short exception")
        assert chosen.start == selected.start + shift and chosen.end - chosen.start == timedelta(hours=2)
        assert chosen.description == "Custom notes" and chosen.location == "Other room"
        assert sibling.end - sibling.start == timedelta(minutes=30)
        if scope == "future":
            head = store.get_event(source.id)
            assert head.recurrence_overrides == {key: payload for key, payload in source.recurrence_overrides.items()
                                                 if key < selected.recurrence_id}
        store.undo(operation)
        assert store.list_events() == [source]


@pytest.mark.parametrize("scope", ["group", "future"])
def test_mouse_resize_applies_duration_globally_while_preserving_exception_content(tmp_path, scope):
    with Store(tmp_path / "db") as store:
        source, selected = mouse_series(store)
        edited = replace(source, start=selected.start, end=selected.start + timedelta(hours=3))
        operation = (store.edit_series(selected, edited, preserve_exceptions=True) if scope == "group" else
                     store.edit_future(selected, edited, preserve_exceptions=True))
        affected = store.get_event(source.id) if scope == "group" else tail_of(store)
        assert affected.end - affected.start == timedelta(hours=3)
        for payload in affected.recurrence_overrides.values():
            override = Event.from_dict(payload)
            assert override.end - override.start == timedelta(hours=3)
        events = store.query_occurrences(*BOUNDS)
        chosen = next(event for event in events if event.title == "Long exception")
        assert chosen.description == "Custom notes" and chosen.location == "Other room"
        assert next(event for event in events if event.title == "Short exception").title == "Short exception"
        store.undo(operation)
        assert store.list_events() == [source]
