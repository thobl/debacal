"""Local mutations certify sequence metadata before durable history and syncing."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.recurrence import expand_event
from debacal.schedule_authoring import expression_event, expression_matches
from debacal.store import HistoryConflict, Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 10, tzinfo=ZONE)
TEXT = '  5*+1d  '


@pytest.fixture
def store():
    with Store(':memory:') as value:
        value.save_calendar(Calendar('remote', 'Synthetic remote', '#587c67', kind='caldav'))
        yield value


def source(text=TEXT):
    prototype = Event('series', 'remote', 'A sequence', START, START + timedelta(hours=1),
                      timezone_id=ZONE.key, schedule_expression=text,
                      expression_anchor=START.date().isoformat(), schedule_syntax=2)
    return expression_event(prototype, text)


def occurrences(event):
    return expand_event(event, START - timedelta(days=10), START + timedelta(days=100))


def assert_current_annotation(store, master):
    assert expression_matches(master, master.schedule_expression)
    queued, = store.pending_changes(master.calendar_id)
    assert queued['payload']['schedule_expression'] == master.schedule_expression
    reread, = parse_ics(export_ics([master]), master.calendar_id)
    assert expression_matches(reread, reread.schedule_expression)
    assert [(item.start, item.end) for item in occurrences(reread)] == [
        (item.start, item.end) for item in occurrences(master)]


@pytest.mark.parametrize('operation', ['edit_occurrence', 'move_selection'])
def test_individual_time_changes_refresh_text_and_outbox_with_original_exception_identity(store, operation):
    master = source()
    store.apply('Create series', [master])
    selected = occurrences(master)[2]
    start, end = selected.start + timedelta(hours=2), selected.end + timedelta(hours=2)
    if operation == 'edit_occurrence':
        history = store.edit_occurrence(selected, replace(selected, start=start, end=end, description='Exception notes'))
    else:
        history = store.move_selection([selected], 'single', selected, start, end)
    changed = store.get_event(master.id)
    assert '12:00' in changed.schedule_expression
    assert set(changed.recurrence_overrides) == {selected.recurrence_id}
    assert store.get_occurrence(selected.id).start == start
    assert_current_annotation(store, changed)
    store.undo(history)
    assert store.get_event(master.id) == master
    store.redo(history)
    assert store.get_event(master.id) == changed
    assert_current_annotation(store, changed)


@pytest.mark.parametrize('operation', ['delete_occurrence', 'delete_selection'])
def test_deleting_an_occurrence_updates_annotated_schedule_without_reopening_editor(store, operation):
    master = source()
    store.apply('Create series', [master])
    selected = occurrences(master)[2]
    history = (store.delete_occurrence(selected) if operation == 'delete_occurrence'
               else store.delete_selection([selected], 'single'))
    changed = store.get_event(master.id)
    assert len(occurrences(changed)) == 5
    assert selected.recurrence_id in changed.excluded_dates
    assert_current_annotation(store, changed)
    store.undo(history)
    assert store.get_event(master.id) == master
    store.redo(history)
    assert_current_annotation(store, store.get_event(master.id))


@pytest.mark.parametrize('field,value', [('title', 'Renamed'), ('description', 'Different notes'),
                                        ('location', 'Another room'), ('end', START + timedelta(hours=3))])
def test_content_and_duration_changes_leave_literal_formatting_untouched(store, field, value):
    master = source()
    store.apply('Create series', [master])
    store.apply('Edit event', [replace(master, **{field: value})])
    changed = store.get_event(master.id)
    assert changed.schedule_expression == TEXT
    assert changed.schedule_syntax == 2 and changed.expression_anchor == master.expression_anchor


def test_remote_annotation_and_title_only_edit_are_preserved_until_a_local_schedule_change(store):
    master = source()
    selected = occurrences(master)[2]
    remote = replace(master, excluded_dates=(selected.recurrence_id,))
    assert not expression_matches(remote, remote.schedule_expression)
    store.apply_remote([remote])
    assert store.get_event(remote.id) == remote and not store.pending_changes()
    title = replace(remote, title='A title edit')
    store.apply('Change title', [title])
    assert store.get_event(remote.id) == title
    remaining = occurrences(title)[-1]
    store.delete_occurrence(remaining)
    updated = store.get_event(master.id)
    assert len(occurrences(updated)) == 4
    assert_current_annotation(store, updated)


def test_duplicate_actual_starts_clear_only_optional_annotation_and_remain_undoable(store):
    master = source()
    store.apply('Create series', [master])
    selected, neighbor = occurrences(master)[1:3]
    history = store.edit_occurrence(selected, replace(selected, start=neighbor.start, end=neighbor.end))
    changed = store.get_event(master.id)
    assert changed.schedule_expression == changed.expression_anchor == ''
    assert len(occurrences(changed)) == 6
    assert sum(item.start == neighbor.start for item in occurrences(changed)) == 2
    assert set(changed.recurrence_overrides) == {selected.recurrence_id}
    assert b'X-DEBACAL-PATTERN:' not in export_ics([changed])
    store.undo(history)
    assert store.get_event(master.id) == master
    store.redo(history)
    assert store.get_event(master.id) == changed


def test_unannotated_regular_and_legacy_text_are_not_silently_rewritten(store):
    regular = replace(source('every(1d)'), schedule_expression='', expression_anchor='')
    legacy = replace(source(), id='legacy', ical_uid='legacy', schedule_syntax=1,
                     schedule_expression='Old date-only authoring text')
    store.apply('Create sources', [regular, legacy])
    for master in (regular, legacy):
        selected = occurrences(master)[2]
        store.delete_occurrence(selected)
        changed = store.get_event(master.id)
        assert changed.schedule_expression == master.schedule_expression
        assert changed.schedule_syntax == master.schedule_syntax


def test_singleton_empty_text_is_valid_and_preserved(store):
    master = source('+1d')
    store.apply('Create two occurrences', [master])
    store.delete_occurrence(occurrences(master)[1])
    changed = store.get_event(master.id)
    assert changed.schedule_expression == ''
    assert expression_matches(changed, '')
    assert len(occurrences(changed)) == 1


def test_sibling_annotation_refresh_does_not_invalidate_an_unchanged_occurrence_reference(store):
    master = source()
    store.apply('Create series', [master])
    first, second = occurrences(master)[1:3]
    store.edit_occurrence(first, replace(first, start=first.start + timedelta(hours=2),
                                        end=first.end + timedelta(hours=2)))
    store.delete_occurrence(second)
    changed = store.get_event(master.id)
    assert_current_annotation(store, changed)
    assert store.get_occurrence(second.id) is None
    # A truly changed occurrence remains stale, even though its series text is
    # derived and therefore excluded from the dated-reference comparison.
    with pytest.raises(HistoryConflict):
        store.edit_occurrence(first, replace(first, title='An edit from an old range'))
    assert store.get_event(master.id) == changed
