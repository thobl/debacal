"""Exact-union selection operations, independent of Qt and real calendars."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_clipboard import EventClipboard
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.recurrence import expand_events
from debacal.selection import EventSelection, affected_visible, capture_selection, selection_roots
from debacal.store import HistoryConflict, Store


ZONE = ZoneInfo("Europe/Berlin")
START = datetime(2026, 10, 20, 9, tzinfo=ZONE)
BOUNDS = START - timedelta(days=30), START + timedelta(days=100)


def event(key="a", **changes):
    return replace(Event(key, "local", key.upper(), START, START + timedelta(hours=1)), **changes)


def repeating(key="series", **changes):
    return event(key, **({"recurrence_rule": "FREQ=WEEKLY;COUNT=6"} | changes))


@pytest.fixture
def store():
    with Store(":memory:") as value:
        yield value


def occurrences(store, master_id):
    return [value for value in store.query_occurrences(*BOUNDS) if value.series_id == master_id]


def test_selection_model_primary_and_exact_single_toggling():
    selection = EventSelection()
    a, b = event("a"), event("b")
    selection.select(a)
    selection.select(b, additive=True)
    assert selection.refs == (a, b) and selection.primary == b
    selection.select(a, additive=True)
    assert selection.refs == (b,)
    selection.select(a)
    assert selection.refs == (a,)
    selection.clear()
    assert selection.primary is None and not selection.ids


def test_group_toggle_uses_family_not_original_occurrence_identity(store):
    source, other = repeating(), event("other")
    store.apply("Create", [source, other])
    values = occurrences(store, source.id)
    selection = EventSelection("group")
    selection.select(values[1])
    selection.select(other, additive=True)
    selection.select(values[4], additive=True)
    assert selection.refs == (other,)
    selection.select(values[3], additive=True)
    assert selection.primary == values[3]


def test_future_toggle_moves_cutoff_earlier_or_removes_already_affected_family(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    selection = EventSelection("future")
    selection.select(values[3])
    selection.select(values[1], additive=True)
    assert selection.refs == (values[1],)
    selection.select(values[5], additive=True)
    assert selection.refs == ()


def test_legacy_group_toggle_is_scoped_to_its_calendar():
    a, b = event("a", group_id="same"), event("b", group_id="same")
    foreign = replace(b, id="foreign", calendar_id="other")
    selection = EventSelection("group")
    selection.select(a)
    selection.select(foreign, additive=True)
    selection.select(b, additive=True)
    assert selection.refs == (foreign,)


def test_overlapping_series_roots_deduplicate_group_and_take_earliest_future_key(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    assert selection_roots(store, [values[4], values[1], values[3]], "group") == [source]
    assert selection_roots(store, [values[4], values[1], values[3]], "future") == [values[1]]
    assert selection_roots(store, [values[4], values[1], values[4]], "single") == [values[4], values[1]]


def test_affected_visible_uses_original_recurrence_key_for_moved_exception(store):
    source = repeating()
    store.apply("Create", [source])
    original = occurrences(store, source.id)
    store.edit_occurrence(original[4], replace(original[4], start=START - timedelta(days=4), end=START - timedelta(days=4) + timedelta(hours=1)))
    moved = store.get_occurrence(original[4].id)
    visible = store.query_occurrences(*BOUNDS)
    affected = affected_visible(store, [original[2]], "future", visible)
    assert affected == {value.id for value in original[2:]}
    assert moved.id in affected and moved.start < START


def test_legacy_union_expansion_never_crosses_calendar_boundaries(store):
    store.save_calendar(Calendar("other", "Other", "#123456"))
    a = event("a", group_id="same")
    b = event("b", group_id="same", start=START + timedelta(days=2), end=START + timedelta(days=2, hours=1))
    foreign = replace(b, id="foreign", calendar_id="other")
    store.apply("Create", [a, b, foreign])
    assert {value.id for value in selection_roots(store, [a, b], "group")} == {a.id, b.id}
    assert affected_visible(store, [a], "group", store.query_occurrences(*BOUNDS)) == {a.id, b.id}
    assert selection_roots(store, [b], "future") == [b]


def test_multi_single_clipboard_is_exact_union_even_if_paste_mode_changes(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    clipboard = capture_selection(store, [values[1], values[4]], "single",
                                  view_origin=date(2026, 10, 19), view_kind="week")
    assert clipboard.exact_selection and clipboard.reference.start == values[4].start
    restored = EventClipboard.from_bytes(clipboard.to_bytes())
    assert restored == clipboard
    assert json.loads(clipboard.to_bytes())["version"] == 2
    for scope in ("single", "group", "future"):
        copies = restored.instantiate(scope, values[4].start + timedelta(days=7), "local")
        assert len(copies) == 2 and all(not value.recurring for value in copies)
        assert [value.start for value in copies] == [values[1].start + timedelta(days=7), values[4].start + timedelta(days=7)]
        assert len({value.ical_uid for value in copies}) == 2


def test_multi_future_clipboard_contains_each_remaining_schedule_once(store):
    a, b = repeating("a"), repeating("b", start=START + timedelta(hours=3), end=START + timedelta(hours=4))
    store.apply("Create", [a, b])
    av, bv = occurrences(store, a.id), occurrences(store, b.id)
    clip = capture_selection(store, [av[4], bv[3], av[2]], "future")
    assert len(clip.masters) == 2
    assert {value.id: len(expand_events([value], *BOUNDS)) for value in clip.masters} == {a.id: 4, b.id: 3}
    copied = clip.instantiate("single", clip.reference.start, "local")
    assert len(copied) == 2 and len(expand_events(copied, *BOUNDS)) == 7


def test_clipboard_v1_backward_compatibility_and_v2_context_validation():
    source = event()
    legacy = EventClipboard.capture([source], source)
    assert json.loads(legacy.to_bytes())["version"] == 1
    assert EventClipboard.from_bytes(legacy.to_bytes()) == legacy
    contextual = replace(legacy, view_origin=date(2026, 10, 19), view_kind="week", exact_selection=True)
    payload = json.loads(contextual.to_bytes())
    payload["view_origin"] = "invalid"
    with pytest.raises(ValueError):
        EventClipboard.from_bytes(json.dumps(payload).encode())


def test_delete_multiple_occurrences_merges_exclusions_in_one_resource_and_undo(store):
    source = repeating(calendar_id="remote")
    store.save_calendar(Calendar("remote", "Remote", "#123456", kind="caldav"))
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    operation = store.delete_selection([values[1], values[4]], "single")
    assert set(store.get_event(source.id).excluded_dates) == {values[1].recurrence_id, values[4].recurrence_id}
    assert len(store.history()) == 2 and len(store.pending_changes()) == 1
    assert len(occurrences(store, source.id)) == 4
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_delete_multiple_future_roots_splits_once_at_earliest_key_and_keeps_other_resources(store):
    a, b = repeating(), event("other")
    store.apply("Create", [a, b])
    values = occurrences(store, a.id)
    operation = store.delete_selection([values[4], values[2]], "future")
    assert len(occurrences(store, a.id)) == 2 and store.get_event(b.id) == b
    assert len(store.history()) == 2
    store.undo(operation)
    assert store.get_event(a.id) == a


def test_move_multiple_singles_keeps_individual_fields_durations_and_atomic_undo(store):
    a = event("a", description="A notes")
    b = event("b", start=START + timedelta(days=2, hours=3), end=START + timedelta(days=2, hours=3, minutes=20), description="B notes")
    store.apply("Create", [a, b])
    operation = store.move_selection([a, b], "single", a, a.start + timedelta(days=7), a.end + timedelta(days=7))
    aa, bb = store.get_event(a.id), store.get_event(b.id)
    assert aa.start == a.start + timedelta(days=7) and bb.start == b.start + timedelta(days=7)
    assert aa.description == "A notes" and bb.description == "B notes"
    assert bb.end - bb.start == timedelta(minutes=20)
    assert len(store.history()) == 2
    store.undo(operation)
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}


def test_move_multiple_single_occurrences_merges_overrides_without_touching_siblings(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    operation = store.move_selection([values[1], values[3]], "single", values[3],
                                     values[3].start + timedelta(hours=2), values[3].end + timedelta(hours=2))
    assert set(store.get_event(source.id).recurrence_overrides) == {values[1].recurrence_id, values[3].recurrence_id}
    assert store.get_occurrence(values[0].id) == values[0]
    assert store.get_occurrence(values[1].id).start == values[1].start + timedelta(hours=2)
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_group_drag_accepts_clicked_affected_occurrence_not_explicit_root(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    operation = store.move_selection([values[1]], "group", values[4],
                                     values[4].start + timedelta(days=1), values[4].end + timedelta(days=1))
    assert store.get_event(source.id).start == source.start + timedelta(days=1)
    assert len(occurrences(store, source.id)) == 6
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_group_move_deduplicates_overlapping_roots_and_preserves_exception_content(store):
    source, other = repeating(), repeating("other", description="Other notes")
    store.apply("Create", [source, other])
    values = occurrences(store, source.id)
    store.edit_occurrence(values[3], replace(values[3], title="Exception", description="Exception notes", end=values[3].end + timedelta(hours=1)))
    source = store.get_event(source.id)
    operation = store.move_selection([values[0], values[1], occurrences(store, other.id)[0]], "group", values[1],
                                     values[1].start + timedelta(hours=1), values[1].end + timedelta(hours=1))
    assert store.get_event(source.id).start.hour == 10 and store.get_event(other.id).start.hour == 10
    exception = next(value for value in occurrences(store, source.id) if value.title == "Exception")
    assert exception.description == "Exception notes" and exception.end - exception.start == timedelta(hours=2)
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_future_move_union_keeps_past_and_reidentifies_one_tail_with_standard_roundtrip(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    operation = store.move_selection([values[4], values[2]], "future", values[4],
                                     values[4].start + timedelta(days=1), values[4].end + timedelta(days=1))
    masters = store.list_events()
    assert len(masters) == 2 and len({value.ical_uid for value in masters}) == 2
    actual = store.query_occurrences(*BOUNDS)
    assert [value.start for value in actual] == [value.start for value in values[:2]] + [value.start + timedelta(days=1) for value in values[2:]]
    restored = expand_events(parse_ics(export_ics(masters), "local"), *BOUNDS)
    assert [value.start.timestamp() for value in restored] == [value.start.timestamp() for value in actual]
    store.undo(operation)
    assert store.list_events() == [source]


def test_copy_drag_uses_clicked_delta_prepares_fresh_exact_union_without_writes(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    clones = store.move_selection([values[1]], "group", values[4],
                                  values[4].start + timedelta(days=2), values[4].end + timedelta(days=2), copy=True)
    assert len(clones) == 1 and clones[0].start == source.start + timedelta(days=2)
    assert clones[0].id != source.id and clones[0].ical_uid != source.ical_uid
    assert store.list_events() == [source] and len(store.history()) == 1


def test_copy_read_only_source_to_default_writable_calendar_is_allowed(store):
    store.save_calendar(Calendar("locked", "Read only", "#123456", kind="ics"))
    source = event(calendar_id="locked", read_only=True)
    store.apply_remote([source])
    clones = store.move_selection([source], "single", source, source.start, source.end, copy=True)
    assert clones[0].calendar_id == "local" and not clones[0].read_only
    assert store.history() == [] and store.get_event(source.id) == source


def test_edit_union_applies_only_changed_common_fields_not_primary_notes_or_duration(store):
    a = event("a", description="Primary notes")
    b = event("b", description="Independent notes", end=START + timedelta(minutes=20))
    store.apply("Create", [a, b])
    operation = store.edit_selection([a, b], "single", a, replace(a, title="Shared new title"))
    assert store.get_event(a.id).title == store.get_event(b.id).title == "Shared new title"
    assert store.get_event(b.id).description == b.description
    assert store.get_event(b.id).end - store.get_event(b.id).start == timedelta(minutes=20)
    store.undo(operation)
    assert store.get_event(a.id) == a and store.get_event(b.id) == b


def test_edit_group_changes_title_but_preserves_other_exception_fields(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    store.edit_occurrence(values[3], replace(values[3], title="Detached", description="Keep this", location="Custom room", end=values[3].end + timedelta(hours=1)))
    source = store.get_event(source.id)
    operation = store.edit_selection([values[0]], "group", source, replace(source, title="Common title"))
    exception = Event.from_dict(next(iter(store.get_event(source.id).recurrence_overrides.values())))
    assert exception.title == "Common title" and exception.description == "Keep this" and exception.location == "Custom room"
    assert exception.end - exception.start == timedelta(hours=2)
    store.undo(operation)
    assert store.get_event(source.id) == source


def test_group_dialog_master_reference_does_not_move_nth_selected_occurrence(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    operation = store.edit_selection([values[4]], "group", source, replace(source, title="Title only"),
                                     expected_masters={source.id: source})
    assert [value.start for value in occurrences(store, source.id)] == [value.start for value in values]
    assert store.get_event(source.id).start == source.start
    store.undo(operation)
    assert store.get_event(source.id) == source


@pytest.mark.parametrize("operation", ["delete", "move", "edit"])
def test_any_stale_reference_aborts_entire_union(store, operation):
    a, b = event("a"), event("b")
    store.apply("Create", [a, b])
    store.apply("Other edit", [replace(b, title="Latest")])
    history = len(store.history())
    with pytest.raises(HistoryConflict):
        if operation == "delete":
            store.delete_selection([a, b], "single")
        elif operation == "move":
            store.move_selection([a, b], "single", a, a.start + timedelta(hours=1), a.end + timedelta(hours=1))
        else:
            store.edit_selection([a, b], "single", a, replace(a, title="Stale batch"))
    assert store.get_event(a.id) == a and store.get_event(b.id).title == "Latest"
    assert len(store.history()) == history


def test_read_only_member_aborts_whole_group_without_partial_changes(store):
    a, b = event("a", group_id="group"), event("b", group_id="group", read_only=True)
    store.apply_remote([a, b])
    with pytest.raises(ValueError, match="read-only"):
        store.delete_selection([a], "group")
    assert store.list_events() == [a, b] and store.history() == []


def test_expected_master_guard_rejects_unseen_sibling_change(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    store.delete_occurrence(values[5])
    with pytest.raises(HistoryConflict):
        store.delete_selection([values[1]], "group", expected_masters=[source])
    assert len(occurrences(store, source.id)) == 5


def test_invalid_dst_time_in_second_member_aborts_all_projections(store):
    start = datetime(2027, 3, 20, 1, 30, tzinfo=ZONE)
    a = event("a", start=start, end=start + timedelta(minutes=20))
    other = datetime(2027, 3, 21, 2, 30, tzinfo=ZONE)
    b = event("b", start=other, end=other + timedelta(minutes=20))
    store.apply("Create", [a, b])
    with pytest.raises(ValueError, match="daylight"):
        store.move_selection([a, b], "single", a, start + timedelta(days=7), a.end + timedelta(days=7))
    assert store.get_event(a.id) == a and store.get_event(b.id) == b and len(store.history()) == 1


def test_future_title_edit_preserves_each_independent_tail_count(store):
    a, b = repeating("a"), repeating("b", recurrence_rule="FREQ=WEEKLY;COUNT=8")
    store.apply("Create", [a, b])
    av, bv = occurrences(store, a.id), occurrences(store, b.id)
    primary = av[2]
    edited = replace(primary, title="Remaining", recurrence_rule="FREQ=WEEKLY;COUNT=4")
    operation = store.edit_selection([bv[3], primary], "future", primary, edited)
    tails = [value for value in store.list_events() if value.id not in {a.id, b.id}]
    rules = [dict(part.split('=', 1) for part in value.recurrence_rule.split(';')) for value in tails]
    assert {rule['COUNT'] for rule in rules} == {'4', '5'}
    assert all(rule['FREQ'] == 'WEEKLY' for rule in rules)
    assert all(value.title == "Remaining" for value in tails)
    store.undo(operation)
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}


def test_mixed_calendar_batch_outbox_and_history_survive_reopen(tmp_path):
    path = tmp_path / "calendar.sqlite"
    with Store(path) as store:
        for key in ("one", "two"):
            store.save_calendar(Calendar(key, key.upper(), "#123456", kind="caldav"))
        a, b = event("a", calendar_id="one"), event("b", calendar_id="two")
        store.apply("Create", [a, b])
        for change in store.pending_changes():
            store.acknowledge_change(change["id"])
        operation = store.move_selection([a, b], "single", a, a.start + timedelta(hours=2), a.end + timedelta(hours=2))
        assert len(store.history()) == 2
        assert {(value["calendar_id"], value["event_id"]) for value in store.pending_changes()} == {("one", "a"), ("two", "b")}
    with Store(path) as store:
        store.undo(operation)
        assert store.get_event(a.id) == a and store.get_event(b.id) == b
        assert len(store.pending_changes()) == 2


def test_modal_snapshot_guard_ignores_unrelated_updates_but_detects_group_membership_change(store):
    a, other = event("a", group_id="group"), event("other")
    store.apply("Create", [a, other])
    snapshot = store.list_events()
    store.apply("Unrelated background change", [replace(other, title="Updated elsewhere")])
    store.edit_selection([a], "group", a, replace(a, title="Allowed"), expected_masters=snapshot)
    selected = store.get_event(a.id)
    snapshot = store.list_events()
    store.apply("New group member", [event("new", group_id="group")])
    with pytest.raises(HistoryConflict, match="group changed"):
        store.delete_selection([selected], "group", expected_masters=snapshot)
    assert len(store.list_events()) == 3


@pytest.mark.parametrize("scope", ["single", "group", "future"])
def test_raw_master_root_dominates_dated_children_and_always_means_whole_family(store, scope):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    roots = selection_roots(store, [values[3], source, values[1]], scope)
    assert roots == [source]
    visible = [source, *values]
    assert affected_visible(store, [values[3], source], scope, visible) == {value.id for value in visible}


def test_mixed_raw_parent_and_other_single_child_capture_preserves_exact_union(store):
    a, b = repeating("a"), repeating("b")
    store.apply("Create", [a, b])
    av, bv = occurrences(store, a.id), occurrences(store, b.id)
    clip = capture_selection(store, [a, bv[2]], "single", reference=bv[2])
    assert clip.reference.start == bv[2].start
    assert len(clip.masters) == 2
    assert sum(value.recurring for value in clip.masters) == 1
    copies = clip.instantiate("single", clip.reference.start + timedelta(days=7), "local")
    assert len(expand_events(copies, *BOUNDS)) == 7
    assert len([value for value in copies if not value.recurring]) == 1
    anchor = capture_selection(store, [a, bv[2]], "single", reference=av[4])
    assert anchor.reference.id == av[4].id
    with pytest.raises(ValueError):
        capture_selection(store, [a, bv[2]], "single", reference=bv[4])


def test_mixed_raw_parent_and_other_child_delete_is_one_atomic_operation(store):
    a, b = repeating("a"), repeating("b")
    store.apply("Create", [a, b])
    bv = occurrences(store, b.id)
    operation = store.delete_selection([a, bv[2]], "single")
    assert store.get_event(a.id) is None
    assert len(occurrences(store, b.id)) == 5
    assert store.get_occurrence(bv[1].id) == bv[1]
    assert store.get_event(b.id).excluded_dates == (bv[2].recurrence_id,)
    assert len(store.history()) == 2
    store.undo(operation)
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}


def test_mixed_raw_parent_and_child_move_preserves_other_siblings_and_undo(store):
    a, b = repeating("a"), repeating("b")
    store.apply("Create", [a, b])
    av, bv = occurrences(store, a.id), occurrences(store, b.id)
    operation = store.move_selection([a, bv[2]], "single", bv[2],
                                     bv[2].start + timedelta(hours=1), bv[2].end + timedelta(hours=1))
    assert all(value.start.hour == 10 for value in occurrences(store, a.id))
    assert store.get_occurrence(bv[2].id).start.hour == 10
    assert store.get_occurrence(bv[1].id) == bv[1]
    assert len(store.history()) == 2
    store.undo(operation)
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}


def test_raw_master_future_delete_does_not_keep_dates_before_its_dtstart(store):
    a = repeating("a", recurrence_rule="", recurrence_dates=("2026-10-01T07:00:00+00:00",))
    store.apply("Create", [a])
    store.delete_selection([a], "future")
    assert store.list_events() == []
    store.undo()
    assert store.list_events() == [a]


def test_raw_unbounded_master_capture_does_not_materialize_occurrences(store):
    a = repeating("a", recurrence_rule="FREQ=WEEKLY")
    store.apply("Create", [a])
    selected = occurrences(store, a.id)[3]
    with patch("debacal.recurrence.expand_series", side_effect=AssertionError("Do not flatten")):
        clip = capture_selection(store, [a], "single", reference=selected)
    assert clip.masters == (a,) and clip.reference.id == selected.id
    assert clip.masters[0].recurrence_rule == "FREQ=WEEKLY"


def test_raw_parent_title_edit_from_dated_single_form_keeps_standard_recurrence(store):
    source = repeating()
    store.apply("Create", [source])
    selected = occurrences(store, source.id)[3]
    operation = store.edit_selection([source], "single", selected, replace(selected, title="Whole parent"))
    assert store.get_event(source.id).recurrence_rule == source.recurrence_rule
    assert all(value.title == "Whole parent" for value in occurrences(store, source.id))
    assert store.get_event(source.id).start == source.start
    store.undo(operation)
    assert store.list_events() == [source]


def test_ctrl_raw_parent_promotes_partial_family_then_toggles_it_off(store):
    source = repeating()
    store.apply("Create", [source])
    values = occurrences(store, source.id)
    selection = EventSelection("single")
    selection.select(values[2])
    selection.select(source, additive=True)
    assert selection.refs == (source,)
    selection.select(source, additive=True)
    assert selection.refs == ()


def test_mixed_authored_parent_and_dated_single_edit_keeps_both_custom_definitions(store):
    a = repeating("a", schedule_expression=' +0d 5*(+1w) ', expression_anchor=START.date().isoformat(), schedule_syntax=2)
    b = repeating("b", start=START + timedelta(hours=3), end=START + timedelta(hours=4),
                  schedule_expression='+0d 5*+1w', expression_anchor=START.date().isoformat(), schedule_syntax=2)
    store.apply("Create", [a, b])
    original = store.query_occurrences(*BOUNDS)
    selected = occurrences(store, b.id)[2]
    assert selected.schedule_expression == b.schedule_expression
    # The Single editor clears inherited authoring fields on its result event;
    # that implicit cleanup is not a requested recurrence edit for raw root A.
    edited = replace(selected, title="Shared title", schedule_expression="", expression_anchor="", schedule_syntax=1)
    operation = store.edit_selection([a, selected], "single", selected, edited)
    assert store.get_event(a.id) == replace(a, title="Shared title")
    bb = store.get_event(b.id)
    assert bb.title == b.title and bb.recurrence_rule == b.recurrence_rule
    assert (bb.schedule_expression, bb.expression_anchor, bb.schedule_syntax) == (b.schedule_expression, b.expression_anchor, b.schedule_syntax)
    assert store.get_occurrence(selected.id).title == "Shared title"
    assert [(value.start.timestamp(), value.end.timestamp()) for value in store.query_occurrences(*BOUNDS)] == [
        (value.start.timestamp(), value.end.timestamp()) for value in original]
    store.undo(operation)
    assert {value.id: value for value in store.list_events()} == {a.id: a, b.id: b}
