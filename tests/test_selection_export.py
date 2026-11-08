"""Selection export is scoped, independent, metadata-preserving and read-only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import parse_ics
from debacal.models import Calendar, Event
from debacal.recurrence import expand_events, occurrence, recurrence_key
from debacal.selection import capture_selection
from debacal.selection_export import export_selection, selection_events
from debacal.store import EventOwnershipConflict, Store, HistoryConflict

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 9, tzinfo=ZONE)
LOWER, UPPER = START - timedelta(days=60), START + timedelta(days=120)


def series(**changes):
    return replace(Event('master', 'local', 'Synthetic series', START, START + timedelta(hours=1),
                         ical_uid='synthetic-series@example.invalid', recurrence_rule='FREQ=WEEKLY;COUNT=5'), **changes)


def signature(events):
    return [(event.start.astimezone(timezone.utc), event.end.astimezone(timezone.utc), event.title,
             event.description, event.location, event.all_day, event.candidate)
            for event in expand_events(events, LOWER, UPPER)]


def database_state(store):
    return (store.events_revision(), store.connection.total_changes,
            store.history(), store.pending_changes(), [event.to_dict() for event in store.list_events()])


def test_two_selected_occurrences_roundtrip_as_two_independent_events_with_stable_uids():
    master = series()
    first, second = occurrence(master, START), occurrence(master, START + timedelta(weeks=2))
    with Store(':memory:') as store:
        store.apply_remote([master])
        before = database_state(store)
        exported = export_selection(store, [first, second], 'single')
        parsed = parse_ics(exported, 'imported')
        repeated = parse_ics(export_selection(store, [first, second], 'single'), 'imported')
        # DTSTAMP may advance between exports; resource identity and content
        # must remain stable even across a wall-clock second boundary.
        assert [event.ical_uid for event in repeated] == [event.ical_uid for event in parsed]
        assert signature(repeated) == signature(parsed)
        assert len(parsed) == 2 and all(not event.recurring for event in parsed)
        assert len({event.ical_uid for event in parsed}) == 2
        assert master.ical_uid not in {event.ical_uid for event in parsed}
        assert signature(parsed) == signature([replace(first, series_id=''), replace(second, series_id='')])
        reverse = parse_ics(export_selection(store, [second, first], 'single'), 'imported')
        assert {event.ical_uid for event in reverse} == {event.ical_uid for event in parsed}
        assert database_state(store) == before


def test_all_scope_keeps_standard_master_uid_and_complete_exception_schedule_once():
    master = series(recurrence_dates=(recurrence_key(START + timedelta(days=3)),),
                    excluded_dates=(recurrence_key(START + timedelta(weeks=1)),))
    nominal = START + timedelta(weeks=3)
    moved = replace(occurrence(master, nominal), start=nominal + timedelta(days=1),
                    end=nominal + timedelta(days=1, hours=2), title='Synthetic override', location='Elsewhere')
    master = replace(master, recurrence_overrides={moved.recurrence_id: moved.to_dict()})
    with Store(':memory:') as store:
        store.apply_remote([master])
        refs = [store.get_occurrence(occurrence(master, START).id), store.get_occurrence(moved.id)]
        snapshots = selection_events(store, refs, 'group')
        assert snapshots == (master,)
        parsed = parse_ics(export_selection(store, refs, 'group'), 'imported')
        assert len(parsed) == 1 and parsed[0].ical_uid == master.ical_uid
        assert parsed[0].recurrence_rule == master.recurrence_rule
        assert parsed[0].recurrence_dates == master.recurrence_dates
        assert parsed[0].excluded_dates == master.excluded_dates
        assert signature(parsed) == signature([master])


def test_future_scope_splits_at_nominal_boundary_and_keeps_rules_and_moved_exceptions():
    master = series()
    nominal = START + timedelta(weeks=2)
    moved = replace(occurrence(master, nominal), start=START - timedelta(days=1), end=START - timedelta(days=1, hours=-2))
    master = replace(master, recurrence_overrides={moved.recurrence_id: moved.to_dict()},
                     excluded_dates=(recurrence_key(START + timedelta(weeks=3)),))
    with Store(':memory:') as store:
        store.apply_remote([master])
        reference = store.get_occurrence(moved.id)
        before = database_state(store)
        parsed = parse_ics(export_selection(store, [reference], 'future'), 'imported')
        tail = selection_events(store, [reference], 'future')[0]
        assert len(parsed) == 1 and parsed[0].ical_uid != master.ical_uid
        assert 'COUNT=3' in parsed[0].recurrence_rule
        assert parsed[0].ical_uid == tail.ical_uid
        assert signature(parsed) == signature([tail])
        actual = expand_events(parsed, LOWER, UPPER)
        assert len(actual) == 2 and any(event.start == reference.start for event in actual)
        assert not any(event.start == START for event in actual)
        assert database_state(store) == before


def test_last_future_occurrence_gets_an_independent_uid_even_when_tail_is_ordinary():
    master = series(recurrence_rule='', recurrence_dates=(recurrence_key(START + timedelta(days=3)),))
    with Store(':memory:') as store:
        store.apply_remote([master])
        selected = occurrence(master, START + timedelta(days=3))
        result = selection_events(store, [selected], 'future')
        assert len(result) == 1 and not result[0].recurring
        assert result[0].ical_uid != master.ical_uid
        assert result[0].start == selected.start
        captured = capture_selection(store, [selected], 'future')
        assert captured.reference == captured.masters[0]
        copied = captured.instantiate('future', selected.start + timedelta(days=7), 'local')
        assert len(copied) == 1 and copied[0].start == selected.start + timedelta(days=7)
        assert not copied[0].recurring


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_explicit_whole_root_deduplicates_its_children_and_keeps_source_uid(scope):
    master = series()
    with Store(':memory:') as store:
        store.apply_remote([master])
        refs = [master, occurrence(master, START + timedelta(weeks=2))]
        snapshots = selection_events(store, refs, scope)
        assert snapshots == (master,)
        parsed = parse_ics(export_selection(store, refs, scope), 'imported')
        assert len(parsed) == 1 and parsed[0].ical_uid == master.ical_uid
        assert signature(parsed) == signature([master])


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_readonly_and_mixed_calendar_sources_export_without_history_or_source_changes(scope):
    master = series(calendar_id='readonly', read_only=True)
    ordinary = replace(series(), id='ordinary', ical_uid='ordinary@example.invalid', recurrence_rule='',
                       start=START + timedelta(hours=4), end=START + timedelta(hours=5))
    with Store(':memory:') as store:
        store.save_calendar(Calendar('readonly', 'Synthetic subscription', '#6f81ad', kind='ics', read_only=True))
        store.apply_remote([master, ordinary])
        before = database_state(store)
        refs = [occurrence(master, START + timedelta(weeks=2)), ordinary]
        parsed = parse_ics(export_selection(store, refs, scope), 'imported')
        assert len(parsed) == 2
        assert ordinary.ical_uid in {event.ical_uid for event in parsed}
        assert database_state(store) == before


RAW = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
X-SERVER-HEADER:retained header\r
BEGIN:VEVENT\r
UID:metadata@example.invalid\r
DTSTART:20261102T080000Z\r
DTEND:20261102T090000Z\r
RRULE:FREQ=WEEKLY;COUNT=4\r
SUMMARY:Synthetic parent\r
X-SERVER-PARENT:retained parent\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
TRIGGER:-PT30M\r
DESCRIPTION:Synthetic inherited reminder\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:metadata@example.invalid\r
RECURRENCE-ID:20261109T080000Z\r
DTSTART:20261109T100000Z\r
DTEND:20261109T110000Z\r
SUMMARY:Synthetic detached\r
X-SERVER-DETACHED:retained detached\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
TRIGGER:-PT5M\r
DESCRIPTION:Synthetic specific reminder\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:metadata@example.invalid\r
RECURRENCE-ID:20261116T080000Z\r
DTSTART:20261116T080000Z\r
DTEND:20261116T090000Z\r
SUMMARY:Synthetic cancelled\r
STATUS:CANCELLED\r
X-SERVER-CANCELLATION:retained cancellation\r
END:VEVENT\r
END:VCALENDAR\r
'''


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_export_keeps_unknown_resource_metadata_and_effective_alarms(scope):
    master = parse_ics(RAW, 'local')[0]
    with Store(':memory:') as store:
        store.apply_remote([master])
        reference = occurrence(master, datetime(2026, 11, 9, 8, tzinfo=timezone.utc))
        before = database_state(store)
        exported = export_selection(store, [reference], scope)
        assert b'X-SERVER-HEADER:retained header' in exported
        assert b'X-SERVER-PARENT:retained parent' in exported
        assert b'X-SERVER-DETACHED:retained detached' in exported
        assert b'DESCRIPTION:Synthetic specific reminder' in exported
        if scope == 'single':
            assert b'DESCRIPTION:Synthetic inherited reminder' not in exported
            assert b'RECURRENCE-ID' not in exported
            assert b'X-SERVER-CANCELLATION' not in exported
        else:
            assert b'DESCRIPTION:Synthetic inherited reminder' in exported
            assert b'RECURRENCE-ID' in exported
            assert b'X-SERVER-CANCELLATION:retained cancellation' in exported
        parsed = parse_ics(exported, 'imported')
        assert len(parsed) == 1
        if scope == 'group':
            assert parsed[0].ical_uid == master.ical_uid
        else:
            assert parsed[0].ical_uid != master.ical_uid
        assert database_state(store) == before


def test_one_occurrence_authored_whole_root_preserves_uid_and_authoring_metadata():
    master = series(recurrence_rule='', schedule_expression='2.11.2026',
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    assert master.recurring
    with Store(':memory:') as store:
        store.apply_remote([master])
        before = database_state(store)
        for scope in ('single', 'group', 'future'):
            snapshots = selection_events(store, [master], scope)
            assert snapshots == (master,)
            parsed = parse_ics(export_selection(store, [master], scope), 'imported')
            assert parsed[0].ical_uid == master.ical_uid
            assert parsed[0].schedule_expression == master.schedule_expression
            assert parsed[0].schedule_syntax == 2
        captured = capture_selection(store, [master], 'future')
        assert captured.masters == (master,)
        assert captured.reference.series_id == master.id
        assert database_state(store) == before


def test_all_day_occurrences_keep_calendar_dates_and_duration_when_detached():
    initial = START.replace(hour=0)
    master = series(start=initial, end=initial + timedelta(days=2), all_day=True)
    selected = [occurrence(master, initial + timedelta(weeks=index)) for index in (1, 3)]
    with Store(':memory:') as store:
        store.apply_remote([master])
        parsed = parse_ics(export_selection(store, selected, 'single'), 'imported')
        assert len(parsed) == 2 and len({value.ical_uid for value in parsed}) == 2
        assert all(value.all_day and not value.recurring for value in parsed)
        assert sorted((value.start.date(), (value.end.date() - value.start.date()).days) for value in parsed) == [
            (value.start.date(), 2) for value in selected]


def test_empty_stale_and_duplicate_uid_sources_fail_without_writes():
    master = series(recurrence_rule='')
    with Store(':memory:') as store:
        store.apply_remote([master])
        before = database_state(store)
        with pytest.raises(ValueError, match='Select'):
            export_selection(store, [], 'single')
        with pytest.raises(HistoryConflict):
            export_selection(store, [replace(master, title='Stale')], 'single')
        assert database_state(store) == before
        other = replace(master, id='another', title='Different resource')
        with pytest.raises(EventOwnershipConflict, match='UID'):
            store.apply_remote([other])
        assert database_state(store) == before
        store.apply_remote([replace(other, ical_uid='another@example.invalid')])
        # Simulate a legacy/corrupt row predating ownership enforcement. The
        # normal write API must refuse this state, and export must still catch
        # it when reading an older database rather than merging the resources.
        with store._transaction():
            store.connection.execute('UPDATE events SET data=? WHERE id=?',
                                     (json.dumps(other.to_dict()), other.id))
        before = database_state(store)
        with pytest.raises(ValueError, match='share an iCalendar UID'):
            export_selection(store, [master, other], 'group')
        assert database_state(store) == before
