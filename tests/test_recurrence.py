from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lebacal.models import Calendar, Event
from lebacal.recurrence import expand_event, recurrence_key, RecurrenceError, updated_series, rules_equivalent
from lebacal.store import Store, HistoryConflict


BERLIN = ZoneInfo("Europe/Berlin")


def master(**changes):
    start = datetime(2026, 10, 20, 9, tzinfo=BERLIN)
    return replace(Event("series", "local", "Class", start, start + timedelta(hours=1),
                         recurrence_rule="FREQ=WEEKLY;COUNT=4", ical_uid="class@example"), **changes)


def period(year=2026, month=10):
    return datetime(year, month, 1, tzinfo=BERLIN), datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=BERLIN)


def test_one_master_stays_repeating_beyond_previous_import_window(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create weekly series", [master(recurrence_rule="FREQ=WEEKLY")])
        assert len(store.list_events()) == 1
        events = store.query_occurrences(*period(2040, 2))
        assert len(events) == 4
        assert all(event.series_id == "series" for event in events)
        assert all(event.start.hour == 9 for event in events)
        assert len(store.list_events()) == 1


def test_timezone_survives_storage_and_recurrence_crosses_dst(tmp_path):
    path = tmp_path / "db"
    with Store(path) as store:
        store.apply("Create", [master()])
    with Store(path) as store:
        stored = store.get_event("series")
        assert stored.timezone_id == "Europe/Berlin"
        events = store.query_occurrences(datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN))
        assert [event.start.hour for event in events] == [9, 9, 9, 9]
        assert [event.start.utcoffset() for event in events] == [timedelta(hours=2)] + [timedelta(hours=1)] * 3
        assert all(event.end.hour == 10 for event in events)
        assert store.get_occurrence(events[2].id) == events[2]


def test_exdate_and_detached_move_are_one_undoable_master_edit(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create", [master()])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        original = store.query_occurrences(*bounds)
        store.delete_occurrence(original[1])
        assert len(store.query_occurrences(*bounds)) == 3
        assert len(store.get_event("series").excluded_dates) == 1
        store.edit_occurrence(original[2], replace(original[2], title="Moved class", start=original[2].start + timedelta(hours=2), end=original[2].end + timedelta(hours=2)))
        events = store.query_occurrences(*bounds)
        assert [event.start.hour for event in events] == [9, 11, 9]
        assert events[1].recurrence_id == original[2].recurrence_id
        assert events[1].id == original[2].id
        assert len(store.list_events()) == 1
        store.undo()
        assert [event.start.hour for event in store.query_occurrences(*bounds)] == [9, 9, 9]
        store.undo()
        assert store.query_occurrences(*bounds) == original
        store.redo()
        store.redo()
        assert store.get_occurrence(original[2].id).title == "Moved class"


def test_moved_override_is_visible_even_outside_original_occurrence_range(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create", [master()])
        event = store.query_occurrences(*period(2026, 10))[0]
        start = datetime(2027, 2, 1, 12, tzinfo=BERLIN)
        store.edit_occurrence(event, replace(event, start=start, end=start + timedelta(hours=1)))
        february = store.query_occurrences(*period(2027, 2))
        assert len(february) == 1
        assert february[0].start == start
        assert february[0].recurrence_id == event.recurrence_id


def test_rdates_include_dtstart_and_exclusions():
    event = master(recurrence_rule="", recurrence_dates=("2026-11-04T08:00:00+00:00", "2027-01-02T08:00:00+00:00"),
                   excluded_dates=("2026-11-04T08:00:00+00:00",))
    events = expand_event(event, datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2028, 1, 1, tzinfo=BERLIN))
    assert len(events) == 2
    assert events[0].start == event.start
    assert events[1].start.month == 1


@pytest.mark.parametrize("rule,count", [("FREQ=DAILY;COUNT=10", 10),
                                       ("FREQ=MONTHLY;COUNT=4", 4),
                                       ("FREQ=YEARLY;COUNT=3", 3),
                                       ("FREQ=MONTHLY;BYDAY=1TU;COUNT=4", 5)])
def test_standard_rule_frequencies(rule, count):
    events = expand_event(master(recurrence_rule=rule), datetime(2026, 1, 1, tzinfo=BERLIN), datetime(2030, 1, 1, tzinfo=BERLIN))
    assert len(events) == count


def test_series_edit_uses_selected_instance_delta_and_preserves_exceptions(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master()
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        selected = store.query_occurrences(*bounds)[2]
        edited = replace(selected, title="New title", start=selected.start + timedelta(hours=1), end=selected.end + timedelta(hours=1),
                         recurrence_rule=source.recurrence_rule)
        store.edit_series(selected, edited)
        updated = store.get_event("series")
        assert updated.start.date() == source.start.date()
        assert updated.start.hour == 10
        assert len(store.query_occurrences(*bounds)) == 4
        assert all(event.title == "New title" for event in store.query_occurrences(*bounds))
        store.undo()
        assert store.get_event("series") == source


def test_removing_recurrence_leaves_the_edited_selected_event(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create", [master()])
        selected = store.query_occurrences(*period(2026, 11))[1]
        store.edit_series(selected, replace(selected, title="Only this day"))
        raw = store.list_events()
        assert len(raw) == 1
        assert not raw[0].recurring
        assert raw[0].start == selected.start
        assert raw[0].id == "series"


def test_single_edit_cannot_overwrite_changes_since_dialog_opened(tmp_path):
    with Store(tmp_path / "db") as store:
        store.apply("Create", [master()])
        selected = store.query_occurrences(*period(2026, 11))[0]
        store.edit_occurrence(selected, replace(selected, title="Latest"))
        with pytest.raises(HistoryConflict):
            store.edit_occurrence(selected, replace(selected, title="Stale"))
        assert store.get_occurrence(selected.id).title == "Latest"


def test_remote_outbox_contains_master_only_after_exception_edit(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Work", "#123456", kind="caldav"))
        store.apply("Create", [master(calendar_id="remote")])
        selected = store.query_occurrences(*period(2026, 11))[0]
        store.edit_occurrence(selected, replace(selected, title="Exception"))
        pending = store.pending_changes()
        assert len(pending) == 1
        assert pending[0]["event_id"] == "series"
        assert selected.recurrence_id in pending[0]["payload"]["recurrence_overrides"]


def test_invalid_or_excessive_recurrence_is_rejected():
    for rule in ("FREQ=DAILY;INTERVAL=0", "FREQ=WEEKLY;COUNT=-1", "FREQ=GARBAGE", "FREQ=MINUTELY"):
        with pytest.raises(RecurrenceError):
            expand_event(master(recurrence_rule=rule), *period(2026, 11))
    with pytest.raises(RecurrenceError, match="exceeds"):
        expand_event(master(recurrence_rule="FREQ=DAILY"), *period(2026, 11), limit=3)


def test_all_day_recurrence_uses_date_identity():
    start = datetime(2026, 10, 20, tzinfo=BERLIN)
    event = master(start=start, end=start + timedelta(days=1), all_day=True)
    events = expand_event(event, datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN))
    assert len(events) == 4
    assert events[0].recurrence_id == "2026-10-20"
    assert all(event.start.hour == event.end.hour == 0 for event in events)


def test_all_day_until_date_is_inclusive_and_remains_standard_date():
    start = datetime(2026, 10, 20, tzinfo=BERLIN)
    event = master(start=start, end=start + timedelta(days=1), all_day=True,
                   recurrence_rule="FREQ=DAILY;UNTIL=20261023")
    events = expand_event(event, *period(2026, 10))
    assert len(events) == 4
    assert events[-1].start.day == 23
    assert event.recurrence_rule == "FREQ=DAILY;UNTIL=20261023"


def test_json_roundtrip_and_noop_are_stable(tmp_path):
    event = master(recurrence_dates=("2026-11-04T08:00:00+00:00",))
    with Store(tmp_path / "db") as store:
        store.apply("Create", [event])
        assert store.apply("No change", [store.get_event(event.id)]) is None
        assert len(store.history()) == 1


def test_recurrence_dates_are_canonical_sets():
    event = master(recurrence_dates=("2026-11-04T09:00:00+01:00", "2026-11-04T08:00:00+00:00"))
    assert event.recurrence_dates == ("2026-11-04T08:00:00+00:00",)


def test_weekly_explicit_byday_and_until_follow_series_shift(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(recurrence_rule="FREQ=WEEKLY;BYDAY=TU;UNTIL=20261110T080000Z")
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        selected = store.query_occurrences(*bounds)[2]
        edited = replace(selected, start=selected.start + timedelta(days=2, hours=1),
                         end=selected.end + timedelta(days=2, hours=1), recurrence_rule=source.recurrence_rule)
        store.edit_series(selected, edited)
        updated = store.get_event(source.id)
        assert "BYDAY=TH" in updated.recurrence_rule
        assert "UNTIL=20261112T090000Z" in updated.recurrence_rule
        events = store.query_occurrences(*bounds)
        assert len(events) == 4
        assert all(event.start.weekday() == 3 and event.start.hour == 10 for event in events)


def test_complex_monthly_rule_date_move_requires_explicit_rule_change(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=20;COUNT=3")
        store.apply("Create", [source])
        selected = store.query_occurrences(*period(2026, 11))[0]
        edited = replace(selected, start=selected.start + timedelta(days=2), end=selected.end + timedelta(days=2),
                         recurrence_rule=source.recurrence_rule)
        with pytest.raises(RecurrenceError, match="explicitly"):
            store.edit_series(selected, edited)
        assert store.get_event(source.id) == source
        assert len(store.history()) == 1
        store.edit_series(selected, replace(edited, recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=22;COUNT=3"))
        assert store.get_event(source.id).start.day == 22


def test_group_title_edit_keeps_cancelled_occurrence_when_editor_supplies_exclusions(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(excluded_dates=("2026-10-27T08:00:00+00:00",))
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        selected = store.query_occurrences(*bounds)[1]
        edited = replace(selected, title="New class", recurrence_rule=source.recurrence_rule,
                         excluded_dates=source.excluded_dates)
        store.edit_series(selected, edited)
        assert store.get_event(source.id).excluded_dates == source.excluded_dates
        assert len(store.query_occurrences(*bounds)) == 3


def test_series_edit_applies_common_fields_and_duration_to_detached_exceptions(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master()
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        original = store.query_occurrences(*bounds)
        detached = replace(original[1], title="Special title", description="Special notes", location="Old room",
                           start=original[1].start + timedelta(hours=2), end=original[1].start + timedelta(hours=2, minutes=30))
        store.edit_occurrence(original[1], detached)
        previous_master = store.get_event(source.id)
        selected = store.query_occurrences(*bounds)[2]
        edited = replace(selected, title="Whole series title", description="Whole series notes", location="New room",
                         start=selected.start + timedelta(hours=1), end=selected.start + timedelta(hours=3),
                         recurrence_rule=source.recurrence_rule, candidate=True)
        store.edit_series(selected, edited)
        events = store.query_occurrences(*bounds)
        assert len(events) == 4
        assert [event.start.hour for event in events] == [10, 12, 10, 10]
        assert all(event.end - event.start == timedelta(hours=2) for event in events)
        assert all(event.title == "Whole series title" and event.description == "Whole series notes"
                   and event.location == "New room" and event.candidate for event in events)
        assert all(event.ical_uid == source.ical_uid for event in events)
        store.undo()
        assert store.get_event(source.id) == previous_master


@pytest.mark.parametrize("rule,dates", [("FREQ=WEEKLY", ()),
                                       ("", ("2026-11-04T08:00:00+00:00", "2026-11-14T08:00:00+00:00"))])
def test_candidate_recurrence_confirmation_keeps_one_actual_slot_and_undoes(tmp_path, rule, dates):
    with Store(tmp_path / "db") as store:
        source = master(candidate=True, recurrence_rule=rule, recurrence_dates=dates)
        store.apply("Create alternatives", [source])
        selected = store.query_occurrences(*period(2026, 11))[1]
        store.confirm_candidate(selected)
        events = store.list_events()
        assert len(events) == 1
        chosen = events[0]
        assert chosen.id == source.id
        assert chosen.ical_uid == source.ical_uid
        assert chosen.start == selected.start and chosen.end == selected.end
        assert not chosen.candidate and not chosen.recurring
        assert not chosen.series_id and not chosen.recurrence_id
        assert store.query_occurrences(*period(2027, 11)) == []
        assert len(store.history()) == 2
        store.undo()
        assert store.list_events() == [source]
        assert store.get_occurrence(selected.id) == selected
        store.redo()
        assert store.list_events() == [chosen]


def test_confirm_recurring_candidate_removes_group_choices_but_preserves_confirmed_siblings(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("other", "Other", "#123456"))
        source = master(candidate=True, group_id="options")
        second_series = replace(source, id="second-series", start=source.start + timedelta(hours=2),
                                end=source.end + timedelta(hours=2))
        ordinary = replace(source, id="ordinary", recurrence_rule="")
        confirmed = replace(ordinary, id="confirmed", candidate=False)
        other_calendar = replace(ordinary, id="other-calendar", calendar_id="other")
        original = [source, second_series, ordinary, confirmed, other_calendar]
        store.apply("Create choices", original)
        selected = next(event for event in store.query_occurrences(*period(2026, 11)) if event.series_id == source.id)
        store.confirm_candidate(selected)
        assert {event.id for event in store.list_events()} == {source.id, confirmed.id, other_calendar.id}
        store.undo()
        assert {event.id: event for event in store.list_events()} == {event.id: event for event in original}


def test_confirm_uses_detached_candidates_actual_moved_time_and_master_outbox(tmp_path):
    with Store(tmp_path / "db") as store:
        store.save_calendar(Calendar("remote", "Remote", "#123456", kind="caldav"))
        source = master(candidate=True, calendar_id="remote")
        store.apply("Create choices", [source])
        selected = store.query_occurrences(*period(2026, 11))[0]
        moved = replace(selected, title="Chosen meeting", start=selected.start + timedelta(days=1, hours=2),
                        end=selected.end + timedelta(days=1, hours=2))
        store.edit_occurrence(selected, moved)
        selected = store.get_occurrence(selected.id)
        store.confirm_candidate(selected)
        chosen = store.get_event(source.id)
        assert chosen.title == moved.title
        assert chosen.start == moved.start and chosen.end == moved.end
        pending = store.pending_changes()
        assert len(pending) == 1
        assert pending[0]["event_id"] == source.id
        assert pending[0]["payload"]["recurrence_rule"] == ""
        assert pending[0]["payload"]["recurrence_overrides"] == {}
        assert not pending[0]["payload"]["candidate"]


def test_confirm_ordinary_candidate_and_reject_stale_selection(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(candidate=True, recurrence_rule="")
        store.apply("Create", [source])
        store.confirm_candidate(source)
        assert store.get_event(source.id) == replace(source, candidate=False)
        with pytest.raises(HistoryConflict):
            store.confirm_candidate(source)


def test_confirm_one_tentative_exception_preserves_confirmed_series(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master()
        store.apply("Create", [source])
        selected = store.query_occurrences(*period(2026, 11))[0]
        store.edit_occurrence(selected, replace(selected, candidate=True))
        selected = store.get_occurrence(selected.id)
        store.confirm_candidate(selected)
        assert store.get_event(source.id).recurring
        events = store.query_occurrences(datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN))
        assert len(events) == 4
        assert all(not event.candidate for event in events)


def test_replacing_explicit_schedule_can_remove_old_dtstart_and_undo(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(recurrence_rule="", recurrence_dates=("2026-11-04T08:00:00+00:00", "2026-11-14T08:00:00+00:00"))
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        original = store.query_occurrences(*bounds)
        selected = original[1]
        edited = replace(selected, recurrence_dates=(source.recurrence_dates[1],))
        store.edit_series(selected, edited, replace_schedule=True)
        updated = store.get_event(source.id)
        assert updated.start == selected.start
        assert updated.id == source.id and updated.ical_uid == source.ical_uid
        assert [event.start for event in store.query_occurrences(*bounds)] == [original[1].start, original[2].start]
        store.undo()
        assert store.get_event(source.id) == source
        assert store.query_occurrences(*bounds) == original


def test_absolute_schedule_replacement_keeps_only_still_relevant_overrides(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master()
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        original = store.query_occurrences(*bounds)
        for selected in original[:2]:
            store.edit_occurrence(selected, replace(selected, title="Old exception", start=selected.start + timedelta(hours=2),
                                                   end=selected.end + timedelta(hours=2)))
        previous = store.get_event(source.id)
        reference = store.get_occurrence(original[2].id)
        edited = replace(reference, title="New schedule", start=original[1].start, end=original[1].start + timedelta(hours=2),
                         recurrence_dates=(original[2].recurrence_id,), recurrence_rule="", excluded_dates=())
        store.edit_series(reference, edited, replace_schedule=True)
        updated = store.get_event(source.id)
        assert set(updated.recurrence_overrides) == {original[1].recurrence_id}
        events = store.query_occurrences(*bounds)
        assert len(events) == 2
        assert [event.start.hour for event in events] == [11, 9]
        assert all(event.title == "New schedule" and event.end - event.start == timedelta(hours=2) for event in events)
        store.undo()
        assert store.get_event(source.id) == previous


def test_absolute_dates_do_not_restore_old_anchor_or_unselected_excluded_date(tmp_path):
    with Store(tmp_path / "db") as store:
        source = master(excluded_dates=("2026-10-27T08:00:00+00:00",))
        store.apply("Create", [source])
        selected = store.query_occurrences(*period(2026, 11))[0]
        edited = replace(selected, recurrence_rule="", recurrence_dates=("2026-11-14T08:00:00+00:00",), excluded_dates=())
        store.edit_series(selected, edited, replace_schedule=True)
        events = store.query_occurrences(datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN))
        assert [event.start.day for event in events] == [3, 14]
        assert all(event.start.month == 11 for event in events)


@pytest.mark.parametrize("ending", ["COUNT=3", "UNTIL=20261118T080000Z"])
def test_gui_rule_defaults_still_shift_implicit_weekday_and_until(tmp_path, ending):
    with Store(tmp_path / "db") as store:
        start = datetime(2026, 11, 4, 9, tzinfo=BERLIN)
        source = master(start=start, end=start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;" + ending)
        store.apply("Create", [source])
        selected = store.query_occurrences(*period(2026, 11))[1]
        form_rule = "BYDAY=WE;INTERVAL=1;WKST=MO;" + ending + ";FREQ=WEEKLY"
        assert rules_equivalent(source.recurrence_rule, form_rule, source)
        edited = replace(selected, start=selected.start + timedelta(days=1), end=selected.end + timedelta(days=1),
                         recurrence_rule=form_rule)
        store.edit_series(selected, edited)
        projected = store.get_event(source.id)
        assert "BYDAY=TH" in projected.recurrence_rule
        if ending.startswith("UNTIL"):
            assert "UNTIL=20261119T080000Z" in projected.recurrence_rule
        events = store.query_occurrences(*period(2026, 11))
        assert len(events) == 3
        assert all(event.start.weekday() == 3 for event in events)


def test_explicitly_changed_weekday_is_preserved_instead_of_shifted_again(tmp_path):
    with Store(tmp_path / "db") as store:
        start = datetime(2026, 11, 4, 9, tzinfo=BERLIN)
        source = master(start=start, end=start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;COUNT=3")
        store.apply("Create", [source])
        selected = store.query_occurrences(*period(2026, 11))[1]
        changed_rule = "FREQ=WEEKLY;INTERVAL=1;BYDAY=TH;COUNT=3"
        assert not rules_equivalent(source.recurrence_rule, changed_rule, source)
        edited = replace(selected, start=selected.start + timedelta(days=1), end=selected.end + timedelta(days=1),
                         recurrence_rule=changed_rule)
        store.edit_series(selected, edited)
        assert store.get_event(source.id).recurrence_rule == changed_rule
        assert all(event.start.weekday() == 3 for event in store.query_occurrences(*period(2026, 11)))


@pytest.mark.parametrize("replace_schedule", [False, True])
def test_pure_series_projection_matches_persisted_edit_and_leaves_input_untouched(tmp_path, replace_schedule):
    with Store(tmp_path / "db") as store:
        source = master(recurrence_rule="FREQ=WEEKLY;COUNT=4")
        store.apply("Create", [source])
        bounds = datetime(2026, 10, 1, tzinfo=BERLIN), datetime(2026, 12, 1, tzinfo=BERLIN)
        selected = store.query_occurrences(*bounds)[2]
        edited = replace(selected, title="Previewed title", start=selected.start + timedelta(hours=1),
                         end=selected.end + timedelta(hours=2), recurrence_rule=source.recurrence_rule)
        before = source.to_dict()
        projection = updated_series(source, selected, edited, replace_schedule=replace_schedule)
        assert source.to_dict() == before
        assert projection.start == (edited.start if replace_schedule else source.start + timedelta(hours=1))
        preview = expand_event(projection, *bounds)
        store.edit_series(selected, edited, replace_schedule=replace_schedule)
        assert store.get_event(source.id) == projection
        assert store.query_occurrences(*bounds) == preview
