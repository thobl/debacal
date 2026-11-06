from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from debacal.event_clipboard import EventClipboard, MAX_CLIPBOARD_BYTES
from debacal.models import Calendar, Event
from debacal.recurrence import expand_events, occurrence, recurrence_key
from debacal.store import Store


BERLIN = ZoneInfo("Europe/Berlin")
BOUNDS = datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2028, 1, 1, tzinfo=BERLIN)


def series(**changes):
    start = datetime(2026, 10, 20, 9, tzinfo=BERLIN)
    return replace(Event("source", "original", "Meeting", start, start + timedelta(minutes=45),
                         recurrence_rule="FREQ=WEEKLY;COUNT=5", group_id="source-group",
                         ical_uid="source@example.invalid"), **changes)


def test_single_copy_detaches_occurrence_and_cannot_restore_series_on_group_paste():
    master = series()
    selected = expand_events([master], *BOUNDS)[2]
    clipboard = EventClipboard.capture([master], selected)
    assert len(clipboard.masters) == 1 and not clipboard.masters[0].recurring
    restored = EventClipboard.from_bytes(clipboard.to_bytes())
    target = datetime(2026, 12, 1, 14, tzinfo=BERLIN)
    for scope in ("single", "group", "future"):
        pasted = restored.instantiate(scope, target, "destination")
        assert len(pasted) == 1
        assert not pasted[0].recurring and not pasted[0].series_id
        assert pasted[0].start == target and pasted[0].end == target + timedelta(minutes=45)
        assert pasted[0].id != master.id and pasted[0].ical_uid != master.ical_uid


def test_group_copy_paste_preserves_rule_exclusions_overrides_and_anchor_time():
    master = series()
    original = expand_events([master], *BOUNDS)
    override = replace(original[3], start=original[3].start + timedelta(hours=2),
                       end=original[3].end + timedelta(hours=2, minutes=45), title="Detached",
                       description="Exception notes", location="Other room", read_only=True)
    master = replace(master, read_only=True, excluded_dates=(original[1].recurrence_id,),
                     recurrence_overrides={original[3].recurrence_id: override.to_dict()})
    selected = occurrence(master, original[2].start)
    clipboard = EventClipboard.from_bytes(EventClipboard.capture([master], selected, "group").to_bytes())
    target = datetime(2026, 12, 1, 14, tzinfo=BERLIN)
    pasted = clipboard.instantiate("group", target, "destination")
    cloned = pasted[0]
    assert cloned.recurrence_rule == master.recurrence_rule
    assert cloned.calendar_id == "destination" and not cloned.read_only
    assert cloned.timezone_id == "Europe/Berlin"
    assert cloned.group_id != master.group_id
    events = expand_events(pasted, *BOUNDS)
    assert len(events) == 4
    assert next(event for event in events if event.title == "Detached").start.hour == 16
    assert next(event for event in events if event.start == target).end == target + timedelta(minutes=45)
    detached = Event.from_dict(next(iter(cloned.recurrence_overrides.values())))
    assert detached.series_id == cloned.id and detached.ical_uid == cloned.ical_uid
    assert detached.calendar_id == "destination" and not detached.read_only
    assert detached.group_id == cloned.group_id
    assert detached.end - detached.start == timedelta(minutes=90)
    assert detached.description == "Exception notes" and detached.location == "Other room"
    assert EventClipboard.from_bytes(clipboard.to_bytes()) == clipboard


def test_future_copy_never_restores_uncopied_past_and_preserves_remaining_count():
    master = series()
    selected = expand_events([master], *BOUNDS)[2]
    clipboard = EventClipboard.capture([master], selected, "future")
    assert "COUNT=3" in clipboard.masters[0].recurrence_rule
    assert clipboard.reference.start == selected.start
    target = selected.start + timedelta(days=28)
    for scope in ("group", "future"):
        pasted = clipboard.instantiate(scope, target, "destination")
        events = expand_events(pasted, *BOUNDS)
        assert len(events) == 3 and events[0].start == target


def test_group_clipboard_can_paste_only_its_following_partition():
    master = series()
    selected = expand_events([master], *BOUNDS)[2]
    clipboard = EventClipboard.capture([master], selected, "group")
    pasted = clipboard.instantiate("future", selected.start, "destination")
    assert len(expand_events(pasted, *BOUNDS)) == 3
    assert "COUNT=3" in pasted[0].recurrence_rule


def test_legacy_group_future_filter_keeps_gaps_and_fresh_group_on_every_paste():
    first = series(recurrence_rule="")
    values = [replace(first, id=str(index), ical_uid=f"{index}@example.invalid",
                      start=first.start + timedelta(days=days), end=first.end + timedelta(days=days))
              for index, days in enumerate((0, 7, 21))]
    clipboard = EventClipboard.capture(values, values[1], "future")
    target = datetime(2027, 1, 5, 11, tzinfo=BERLIN)
    pasted = clipboard.instantiate("group", target, "destination")
    again = clipboard.instantiate("group", target, "destination")
    assert [event.start for event in pasted] == [target, target + timedelta(days=14)]
    assert len({event.group_id for event in pasted}) == 1
    assert pasted[0].group_id not in {values[0].group_id, again[0].group_id}
    assert {event.id for event in pasted}.isdisjoint(event.id for event in again)
    assert {event.ical_uid for event in pasted}.isdisjoint(event.ical_uid for event in again)


def test_selected_moved_override_is_the_actual_paste_anchor():
    master = series()
    original = expand_events([master], *BOUNDS)[2]
    moved = replace(original, start=original.start + timedelta(days=1, hours=3),
                    end=original.end + timedelta(days=1, hours=3))
    master = replace(master, recurrence_overrides={original.recurrence_id: moved.to_dict()})
    selected = occurrence(master, original.start)
    clipboard = EventClipboard.capture([master], selected, "future")
    target = datetime(2026, 12, 2, 15, tzinfo=BERLIN)
    pasted = clipboard.instantiate("group", target, "destination")
    events = expand_events(pasted, *BOUNDS)
    assert any(event.start == target for event in events)
    assert len(events) == 3


def test_multiple_times_same_day_and_point_duration_are_preserved():
    start = datetime(2026, 11, 5, 8, tzinfo=BERLIN)
    master = series(start=start, end=start, recurrence_rule="", recurrence_dates=tuple(
        recurrence_key(start + timedelta(hours=hours)) for hours in (5, 7)))
    selected = expand_events([master], *BOUNDS)[1]
    clipboard = EventClipboard.capture([master], selected, "group")
    target = selected.start + timedelta(hours=4)
    events = expand_events(clipboard.instantiate("group", target, "destination"), *BOUNDS)
    assert [event.start.hour for event in events] == [12, 17, 19]
    assert all(event.start.timestamp() == event.end.timestamp() for event in events)


def test_all_day_copy_paste_keeps_midnight_despite_timed_target():
    start = datetime(2026, 10, 20, tzinfo=BERLIN)
    master = series(start=start, end=start + timedelta(days=1), all_day=True)
    selected = expand_events([master], *BOUNDS)[2]
    clipboard = EventClipboard.capture([master], selected, "group")
    target = datetime(2026, 12, 1, 14, tzinfo=BERLIN)
    events = expand_events(clipboard.instantiate("group", target, "destination"), *BOUNDS)
    assert all(event.start.hour == event.end.hour == 0 for event in events)
    assert any(event.start.date() == target.date() for event in events)


@pytest.mark.parametrize("change", ["wrong_uid", "wrong_reference_id", "bad_recurrence", "duplicate", "version"])
def test_clipboard_rejects_forged_or_inconsistent_payload(change):
    master = series()
    selected = expand_events([master], *BOUNDS)[2]
    payload = json.loads(EventClipboard.capture([master], selected, "group").to_bytes())
    if change == "wrong_uid":
        payload["reference"]["ical_uid"] = "unrelated@example.invalid"
    elif change == "wrong_reference_id":
        payload["reference"]["id"] = "unrelated"
    elif change == "bad_recurrence":
        payload["reference"]["recurrence_id"] = recurrence_key(selected.start + timedelta(days=1))
    elif change == "duplicate":
        payload["events"].append(payload["events"][0])
    else:
        payload["version"] = True
    with pytest.raises(ValueError):
        EventClipboard.from_bytes(json.dumps(payload).encode())


def test_invalid_oversized_and_incomplete_payloads_fail_without_partial_instantiation():
    for payload in (b"not json", b"[]", b'{}', b"x" * (MAX_CLIPBOARD_BYTES + 1)):
        with pytest.raises(ValueError):
            EventClipboard.from_bytes(payload)
    master = series()
    selected = expand_events([master], *BOUNDS)[0]
    clipboard = EventClipboard.capture([master], selected, "group")
    with pytest.raises(ValueError):
        clipboard.instantiate("group", datetime(2026, 12, 1), "destination")
    with pytest.raises(ValueError):
        clipboard.instantiate("unknown", datetime.now(timezone.utc), "destination")
    with pytest.raises(ValueError):
        clipboard.instantiate("single", datetime.now(timezone.utc), "")


def test_prepared_group_is_written_as_one_undoable_batch(tmp_path):
    master = series(recurrence_rule="")
    second = replace(master, id="second", ical_uid="second@example.invalid",
                     start=master.start + timedelta(days=3), end=master.end + timedelta(days=3))
    clipboard = EventClipboard.capture([master, second], master, "group")
    prepared = clipboard.instantiate("group", datetime(2026, 12, 1, 12, tzinfo=BERLIN), "remote")
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        operation = store.apply("Paste group", prepared)
        assert len(store.list_events()) == len(store.pending_changes()) == 2
        assert len(store.history()) == 1
        store.undo(operation)
        assert store.list_events() == []


def test_nested_override_is_validated_even_when_another_occurrence_is_selected():
    master = series()
    events = expand_events([master], *BOUNDS)
    master = replace(master, recurrence_overrides={events[3].recurrence_id: events[3].to_dict()})
    selected = occurrence(master, events[0].start)
    payload = json.loads(EventClipboard.capture([master], selected, "group").to_bytes())
    override = next(iter(payload["events"][0]["recurrence_overrides"].values()))
    override["ical_uid"] = "unrelated@example.invalid"
    with pytest.raises(ValueError):
        EventClipboard.from_bytes(json.dumps(payload).encode())


def test_zero_shift_duplicate_preserves_repeated_hour_endpoint_choices():
    start = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
    source = series(start=start, end=datetime(2026, 10, 25, 3, 30, tzinfo=BERLIN), recurrence_rule="")
    clipboard = EventClipboard.capture([source], source)
    pasted = clipboard.instantiate("single", source.start, "destination")[0]
    assert pasted.start.timestamp() == source.start.timestamp()
    assert pasted.end.timestamp() == source.end.timestamp()
    assert pasted.start.fold == 1 and pasted.end.fold == 0


@pytest.mark.parametrize("target_fold", [0, 1])
def test_explicit_repeated_hour_target_is_honored_with_elapsed_duration(target_fold):
    start = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
    source = series(start=start, end=datetime(2026, 10, 25, 3, 30, tzinfo=BERLIN), recurrence_rule="")
    clipboard = EventClipboard.capture([source], source)
    target = datetime(2027, 10, 31, 2, 30, tzinfo=BERLIN, fold=target_fold)
    pasted = clipboard.instantiate("single", target, "destination")[0]
    assert pasted.start.timestamp() == target.timestamp()
    assert pasted.end.timestamp() - pasted.start.timestamp() == 3600
