"""Correctness and bounded-work checks for large-calendar Store queries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Calendar, Event
from debacal.recurrence import (
    RecurrenceRangeError, expand_events, expand_series, occurrence, recurrence_key, recurrence_set,
)
from debacal.store import HistoryConflict, Store


START = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def event(identifier="event", **changes):
    return replace(Event(identifier, "local", "Synthetic", START, START + timedelta(hours=1)), **changes)


def remote_calendar(identifier="remote"):
    return Calendar(identifier, "Synthetic remote", "#123456", kind="caldav")


def test_event_revision_is_cross_connection_transactional_and_event_only(tmp_path):
    path = tmp_path / "calendar.sqlite"
    with Store(path) as store, Store(path) as worker:
        store.save_calendar(remote_calendar())
        initial = store.events_revision()
        store.set_setting("selected_day", "2026-11-02")
        worker.set_calendar_visible("remote", False)
        worker.set_sync_metadata("remote", "event", {"etag": "synthetic"})
        assert store.events_revision() == initial

        item = event(calendar_id="remote")
        operation = store.apply("Create", [item])
        created = store.events_revision()
        assert created > initial and worker.events_revision() == created
        store.acknowledge_change(store.pending_changes()[0]["id"])
        assert store.events_revision() == created

        worker.apply_remote([replace(item, title="Server changed")])
        changed = store.events_revision()
        assert changed > created
        worker.apply_remote([replace(item, title="Server changed")])
        assert store.events_revision() == changed  # Metadata-only passes need no invalidation.

        with pytest.raises(ValueError):
            with worker._transaction():
                worker._write_event(replace(item, title="Rolled back"))
                raise ValueError("Synthetic rollback")
        assert store.events_revision() == changed

        worker.apply_remote([item])
        store.undo(operation)
        undone = store.events_revision()
        store.redo(operation)
        assert store.events_revision() > undone
        final = store.events_revision()
    with Store(path) as reopened:
        assert reopened.events_revision() == final


def test_broad_revision_notices_separate_connection_commits(tmp_path):
    with Store(tmp_path / "db") as store, Store(tmp_path / "db") as worker:
        previous = store.revision()
        worker.set_setting("synthetic", True)
        assert store.revision() != previous


def test_resource_identity_snapshot_never_decodes_event_payloads():
    with Store(':memory:') as store:
        store.apply_remote([event('first'), event('second', recurrence_rule='FREQ=DAILY')])
        with patch('debacal.store.json.loads', side_effect=AssertionError('IDs do not need payloads')):
            assert store.event_ids() == {'first', 'second'}


def test_query_decodes_each_master_once_and_never_decodes_offscreen_ordinary_rows():
    with Store(":memory:") as store:
        ordinary = event("ordinary")
        active = event("active", recurrence_rule="FREQ=DAILY;COUNT=3")
        ancient = event("ancient", start=START - timedelta(days=100), end=START - timedelta(days=100, hours=-1),
                        recurrence_rule="FREQ=DAILY")
        offscreen = [event(f"old-{index}", start=START - timedelta(days=index + 1),
                           end=START - timedelta(days=index + 1, hours=-1)) for index in range(100)]
        store.apply_remote([ordinary, active, ancient, *offscreen])
        with patch.object(Event, "from_dict", wraps=Event.from_dict) as decode:
            actual = store.query_occurrences(START, START + timedelta(days=1))
        assert len(actual) == 3
        assert decode.call_count == 3


def test_candidate_confirmation_uses_group_index_and_remains_undoable():
    with Store(":memory:") as store:
        choices = [event(f"choice-{index}", candidate=True, group_id="choices") for index in range(3)]
        confirmed = event("confirmed", group_id="choices")
        unrelated = [event(f"ordinary-{index}") for index in range(1000)]
        store.apply_remote([*choices, confirmed, *unrelated])
        with patch.object(store, "list_events", side_effect=AssertionError("Unrelated resources must not be loaded")):
            operation = store.confirm_candidate(choices[1])
        assert store.get_event(choices[1].id) == replace(choices[1], candidate=False)
        assert store.get_event(choices[0].id) is None and store.get_event(choices[2].id) is None
        assert store.get_event(confirmed.id) == confirmed
        store.undo(operation)
        assert [store.get_event(item.id) for item in choices] == choices
        store.redo(operation)
        assert store.get_event(choices[0].id) is None


def test_candidate_index_tracks_remote_edits_and_history():
    with Store(":memory:") as store:
        original = event("other", group_id="old", candidate=False)
        choice = event("choice", group_id="new", candidate=True)
        store.apply_remote([original, choice])
        operation = store.apply("Make alternative", [replace(original, group_id="new", candidate=True)])
        store.undo(operation)
        store.redo(operation)
        confirmed = store.confirm_candidate(choice)
        assert store.get_event(original.id) is None
        store.undo(confirmed)
        assert store.get_event(original.id).candidate


def test_group_query_is_calendar_scoped_ordered_and_decodes_only_its_members():
    with Store(":memory:") as store:
        store.save_calendar(Calendar("other", "Synthetic other", "#123456"))
        first = event("first", group_id="shared")
        later = event("later", group_id="shared", start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1))
        foreign = event("foreign", calendar_id="other", group_id="shared")
        unrelated = [event(f"ordinary-{index}") for index in range(100)]
        store.apply_remote([later, first, foreign, *unrelated])
        with patch.object(Event, "from_dict", wraps=Event.from_dict) as decode:
            assert store.list_group_events("local", "shared") == [first, later]
            assert store.list_group_events("local", "") == []
        assert decode.call_count == 2


def test_pending_counts_and_remote_guards_do_not_decode_unrelated_payloads():
    with Store(":memory:") as store:
        store.save_calendar(remote_calendar())
        store.save_calendar(remote_calendar("other"))
        a, b = event("a", calendar_id="remote"), event("b", calendar_id="other")
        store.apply("Pending", [a, b])
        with patch("debacal.store.json.loads", side_effect=AssertionError("Count must not decode JSON")):
            assert store.count_pending_changes() == 2
            assert store.count_pending_changes("remote") == 1
            assert store.count_pending_changes("missing") == 0
        with patch.object(store, "pending_changes", side_effect=AssertionError("Guard must not load all pending changes")):
            store.apply_remote([event("independent")])
            with pytest.raises(HistoryConflict):
                store.apply_remote([replace(a, title="Remote")])
            reviewed = [row[0] for row in store.connection.execute("SELECT id FROM outbox WHERE event_id=?", (a.id,))]
            store.accept_remote("remote", a.id, a, reviewed, upserts=[replace(a, title="Reviewed server")])
        assert store.count_pending_changes() == 1
        assert store.get_event(a.id).title == "Reviewed server"


def test_legacy_index_backfill_happens_once(tmp_path):
    path = tmp_path / "legacy.sqlite"
    choice = event("choice", candidate=True, group_id="group")
    authored = event("authored", schedule_expression="1.11.2026", expression_anchor="2026-11-01")
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE calendars(id TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE events(id TEXT PRIMARY KEY,calendar_id TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,data TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO calendars VALUES (?,?)", ("local", json.dumps(Calendar("local", "Synthetic", "#123456").to_dict())))
        for item in (choice, authored):
            connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", (item.id, item.calendar_id, item.start.timestamp(), item.end.timestamp(), json.dumps(item.to_dict())))
    with Store(path) as store:
        assert store.get_event(choice.id) == choice
        assert dict(store.connection.execute("SELECT recurring,group_id,candidate FROM events WHERE id='choice'").fetchone()) == {
            "recurring": 0, "group_id": "group", "candidate": 1,
        }
        assert store.connection.execute("SELECT recurring FROM events WHERE id='authored'").fetchone()[0] == 1
    with patch("debacal.store.json.loads", wraps=json.loads) as decode:
        with Store(path):
            pass
    assert decode.call_count == 2  # The migration markers, no event JSON.


@pytest.mark.parametrize("rule", [
    "FREQ=DAILY", "FREQ=DAILY;INTERVAL=3", "FREQ=DAILY;COUNT=4000", "FREQ=DAILY;COUNT=5",
    "FREQ=DAILY;INTERVAL=2;BYDAY=MO,WE,FR", "FREQ=DAILY;BYDAY=MO,WE;COUNT=3000",
    "FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=3;COUNT=500", "FREQ=WEEKLY;BYDAY=SA;COUNT=500",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR;WKST=SU", "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=500",
    "FREQ=DAILY;UNTIL=20261029T090000Z", "FREQ=MONTHLY;BYDAY=1MO",
])
def test_seeking_preserves_dateutil_suffix_rules_and_dst(rule):
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2019, 1, 5, 9, tzinfo=zone)
    master = event(start=first, end=first + timedelta(hours=1), recurrence_rule=rule)
    lower, upper = datetime(2026, 10, 20, tzinfo=zone), datetime(2026, 11, 12, tzinfo=zone)
    expected = recurrence_set(master).between(lower, upper, inc=True)
    actual = recurrence_set(master, after=lower).between(lower, upper, inc=True)
    assert [item.timestamp() for item in actual] == [item.timestamp() for item in expected]


def test_seeking_ancient_daily_series_has_bounded_iteration_work():
    from dateutil.rrule import rrule

    first = datetime(1900, 1, 1, 9, tzinfo=timezone.utc)
    master = event(start=first, end=first + timedelta(hours=1), recurrence_rule="FREQ=DAILY")
    produced = 0
    original = rrule._iter

    def observed(rule):
        nonlocal produced
        for value in original(rule):
            produced += 1
            yield value

    with patch.object(rrule, "_iter", observed):
        actual = expand_series(master, START, START + timedelta(days=7))
    assert len(actual) == 7
    assert produced < 20  # Independent of the century of hidden earlier dates.


def test_seeking_keeps_date_until_explicit_earlier_dates_and_exhausted_count():
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2000, 1, 1, tzinfo=zone)
    lower = datetime(2026, 10, 20, tzinfo=zone)
    upper = datetime(2026, 11, 5, tzinfo=zone)
    for rule, all_day in (("FREQ=DAILY;UNTIL=20261031", True), ("FREQ=DAILY;COUNT=3", False)):
        master = event(start=first, end=first + timedelta(days=1), all_day=all_day,
                       recurrence_rule=rule, recurrence_dates=(recurrence_key(lower + timedelta(days=3), all_day),),
                       excluded_dates=(recurrence_key(lower + timedelta(days=4), all_day),))
        expected = recurrence_set(master).between(lower, upper, inc=True)
        actual = recurrence_set(master, after=lower).between(lower, upper, inc=True)
        assert [value.timestamp() for value in actual] == [value.timestamp() for value in expected]


def test_ancient_occurrence_review_and_confirmation_use_the_bounded_suffix():
    from dateutil.rrule import rrule

    first = datetime(1900, 1, 1, 9, tzinfo=timezone.utc)
    master = event("ancient", start=first, end=first + timedelta(hours=1), recurrence_rule="FREQ=DAILY", candidate=True)
    chosen = occurrence(master, START)
    produced = 0
    original = rrule._iter

    def observed(rule):
        nonlocal produced
        for value in original(rule):
            produced += 1
            yield value

    with Store(":memory:") as store:
        store.apply_remote([master])
        with patch.object(rrule, "_iter", observed):
            assert store.get_occurrence(chosen.id) == chosen
            operation = store.confirm_candidate(chosen)
        assert produced < 20
        assert store.get_event(master.id).start == START
        store.undo(operation)
        assert store.get_event(master.id) == master


def test_upcoming_matches_full_query_with_exceptions_exclusions_and_ongoing_events():
    with Store(":memory:") as store:
        ancient = event("daily", start=START - timedelta(days=10000), end=START - timedelta(days=10000, hours=-1),
                        recurrence_rule="FREQ=DAILY")
        moved_from = ancient.start + timedelta(days=10003)
        key = recurrence_key(moved_from)
        override = replace(occurrence(ancient, moved_from), start=START - timedelta(hours=2), end=START + timedelta(hours=2))
        ancient = replace(ancient, recurrence_overrides={key: override.to_dict()},
                          excluded_dates=(recurrence_key(START + timedelta(days=1)),))
        ordinary = [event(f"ordinary-{index}", start=START + timedelta(minutes=index * 5),
                          end=START + timedelta(minutes=index * 5 + 30)) for index in range(20)]
        ongoing = event("ongoing", start=START - timedelta(days=3), end=START + timedelta(hours=3))
        point = event("point", end=START)
        store.apply_remote([ancient, ongoing, point, *ordinary])
        expected = store.query_occurrences(START, START + timedelta(days=30))[:10]
        assert store.upcoming_occurrences(START, START + timedelta(days=30)) == expected
        assert store.upcoming_occurrences(START, START + timedelta(days=30), [], limit=10) == []
        assert store.upcoming_occurrences(START, START + timedelta(days=30), limit=0) == []


def test_upcoming_is_bounded_past_global_view_limit_and_orders_tied_detached_events():
    with Store(":memory:") as store:
        master = event("daily", recurrence_rule="FREQ=DAILY")
        long_key, short_key = recurrence_key(START + timedelta(days=1)), recurrence_key(START + timedelta(days=2))
        long_override = replace(occurrence(master, START + timedelta(days=1)), start=START, end=START + timedelta(hours=3))
        short_override = replace(occurrence(master, START + timedelta(days=2)), start=START, end=START + timedelta(minutes=15))
        master = replace(master, recurrence_overrides={long_key: long_override.to_dict(), short_key: short_override.to_dict()})
        store.apply_remote([master])
        with patch("debacal.recurrence.occurrence", wraps=occurrence) as project:
            actual = store.upcoming_occurrences(START, START + timedelta(days=20000), limit=2)
        assert [value.end for value in actual] == [START + timedelta(minutes=15), START + timedelta(hours=1)]
        assert project.call_count < 10


def test_upcoming_isolates_malformed_series_without_hiding_healthy_rows():
    with Store(":memory:") as store:
        healthy = event("healthy")
        malformed = event("malformed", recurrence_rule="FREQ=DAILY")
        store.apply_remote([healthy, malformed])
        data = malformed.to_dict() | {"recurrence_rule": "FREQ=UNSUPPORTED"}
        with store._transaction():
            store.connection.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(data), malformed.id))
        assert store.upcoming_occurrences(START, START + timedelta(days=1)) == [healthy]
        assert len(store.list_quarantined_resources()) == 1


@pytest.mark.parametrize("rule", ["", "FREQ=DAILY;COUNT=2"])
def test_upcoming_utc_order_survives_rdate_fold_and_narrowed_top_n_window(rule):
    zone = ZoneInfo("Europe/Berlin")
    initial = datetime(2026, 10, 25, 1, tzinfo=zone)
    lower = initial.replace(hour=0)
    upper = lower + timedelta(days=1)
    late_fold = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    early_fold = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    middle = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    master = event("fold-series", start=initial, end=initial, recurrence_rule=rule,
                   recurrence_dates=(recurrence_key(late_fold), recurrence_key(early_fold)),
                   excluded_dates=(recurrence_key(initial),))
    ordinary = event("ordinary", start=middle, end=middle)
    with Store(":memory:") as store:
        store.apply_remote([master, ordinary])
        expected = store.query_occurrences(lower, upper)
        assert [value.start.timestamp() for value in expected] == [early_fold.timestamp(), middle.timestamp(), late_fold.timestamp()]
        assert store.upcoming_occurrences(lower, upper, limit=1) == expected[:1]
        assert store.upcoming_occurrences(lower, upper, limit=2) == expected[:2]
        # A directly narrowed query uses the same safe exclusive UTC boundary.
        assert [value.start.timestamp() for value in store.query_occurrences(lower, middle)] == [early_fold.timestamp()]


def test_truncated_fold_dates_and_override_keep_the_same_earliest_instants():
    zone = ZoneInfo("Europe/Berlin")
    initial = datetime(2026, 10, 25, 1, tzinfo=zone)
    lower, upper = initial.replace(hour=0), initial.replace(hour=0) + timedelta(days=1)
    late_first = initial.replace(hour=2, minute=10, fold=1)
    late_second = initial.replace(hour=2, minute=15, fold=1)
    early = initial.replace(hour=2, minute=30, fold=0)
    master = event("fold-series", start=initial, end=initial,
                   recurrence_dates=tuple(recurrence_key(value) for value in (late_first, late_second, early)),
                   excluded_dates=(recurrence_key(initial),))
    assert expand_series(master, lower, upper, limit=1, truncate=True)[0].start.timestamp() == early.timestamp()
    overridden = replace(occurrence(master, late_first), start=initial.replace(hour=2, minute=20, fold=0),
                         end=initial.replace(hour=2, minute=20, fold=0))
    master = replace(master, recurrence_overrides={recurrence_key(late_first): overridden.to_dict()},
                     excluded_dates=(recurrence_key(initial), recurrence_key(late_second)))
    full = expand_series(master, lower, upper)
    assert [value.start.timestamp() for value in full] == [overridden.start.timestamp(), early.timestamp()]
    assert expand_series(master, lower, upper, limit=1, truncate=True) == full[:1]


def test_fold_rule_anchor_does_not_create_an_extra_count_occurrence():
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    lower = datetime(2026, 10, 25, tzinfo=zone)
    master = event(start=first, end=first, recurrence_rule="FREQ=DAILY;COUNT=1")
    expected = list(recurrence_set(master))
    actual = expand_series(master, lower, lower + timedelta(days=1))
    assert len(actual) == len(expected) == 1
    assert actual[0].start.timestamp() == expected[0].timestamp()


def test_annual_aggregate_does_not_exhaust_other_valid_series_or_quarantine_them():
    first = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    lower, upper = first.replace(hour=0), first.replace(year=2027, hour=0)
    with Store(":memory:") as store:
        masters = [event(f"daily-{number}", start=first, end=first + timedelta(hours=1), recurrence_rule="FREQ=DAILY")
                   for number in range(30)]
        store.apply_remote(masters)
        values = store.query_occurrences(lower, upper)
        assert len(values) == 30 * 365
        assert {value.series_id for value in values} == {master.id for master in masters}
        assert store.list_quarantined_resources() == []
        assert store.history() == [] and store.count_pending_changes() == 0
        # Clipboard/preview expansion keeps its explicit aggregate safety bound.
        with pytest.raises(RecurrenceRangeError, match="exceeds"):
            expand_events(masters, lower, upper)


def test_single_dense_resource_raises_range_error_without_quarantining_valid_data():
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    master = event(start=first, end=first + timedelta(hours=1), recurrence_rule="FREQ=DAILY;COUNT=10001")
    with Store(":memory:") as store:
        store.apply_remote([master])
        revision = store.events_revision()
        with pytest.raises(RecurrenceRangeError, match="smaller date range"):
            store.query_occurrences(first.replace(hour=0), first.replace(year=2028, hour=0))
        assert store.list_quarantined_resources() == []
        assert store.get_event(master.id) == master and store.events_revision() == revision
        assert store.history() == [] and store.count_pending_changes() == 0
