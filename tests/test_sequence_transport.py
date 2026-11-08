"""Authoring text follows the same projection through drag, edit, copy and split."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

import pytest

from debacal.event_clipboard import EventClipboard
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import compile_sequence, expand_sequence
from debacal.recurrence import expand_event, occurrence, recurrence_key, schedule_fields, updated_series
from debacal.schedule_authoring import expression_matches, reconstruct_authoring
from debacal.series_operations import partition_series
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
BOUNDS = START - timedelta(days=60), START + timedelta(days=500)


def source(expression, start=START):
    compiled = compile_sequence(expression, start)
    fields = compiled.fields if compiled else schedule_fields(expand_sequence(expression, start))
    return Event('series', 'local', 'A meeting', start, start + timedelta(hours=1),
                 ical_uid='series@example.test', timezone_id=ZONE.key, **fields,
                 schedule_expression=expression, expression_anchor=start.date().isoformat(), schedule_syntax=2)


def occurrences(event):
    return expand_event(event, *BOUNDS)


def starts(event):
    return [item.start.astimezone(timezone.utc) for item in occurrences(event)]


def assert_roundtrip(event):
    assert expression_matches(event, event.schedule_expression)
    loaded, = parse_ics(export_ics([event]), event.calendar_id)
    assert loaded.schedule_expression == event.schedule_expression
    assert expression_matches(loaded, loaded.schedule_expression)
    assert starts(loaded) == starts(event)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'dialog', 'clipboard'])
@pytest.mark.parametrize('expression', [
    '  +0d 4*(+2w) +3w 3*+2w  ', '+0d 4*+1m!', '+0d 10:00 11:00 9:00 10:00 11:00',
])
def test_equivalent_authored_literal_survives_every_whole_series_transport(operation, expression):
    master = source(expression)
    selected = occurrences(master)[2]
    delta = timedelta(days=2)
    expected = [item.start + delta for item in occurrences(master)]
    with Store(':memory:') as store:
        store.apply('Create', [master])
        if operation == 'projection':
            moved = updated_series(master, master, replace(master, start=master.start + delta, end=master.end + delta))
        elif operation == 'drag':
            store.move_selection([selected], 'group', selected, selected.start + delta, selected.end + delta)
            moved = store.get_event(master.id)
        elif operation == 'dialog':
            store.edit_selection([selected], 'group', master,
                                 replace(master, start=master.start + delta, end=master.end + delta))
            moved = store.get_event(master.id)
        else:
            clipboard = EventClipboard.capture([master], selected, 'group')
            moved, = clipboard.instantiate('group', selected.start + delta, 'local')
        assert moved.schedule_expression == expression
        assert moved.expression_anchor == (START + delta).date().isoformat()
        assert starts(moved) == [value.astimezone(timezone.utc) for value in expected]
        assert_roundtrip(moved)
        if operation in ('drag', 'dialog'):
            assert len(store.history()) == 2
            store.undo()
            assert store.get_event(master.id) == master
            store.redo()
            assert store.get_event(master.id) == moved


@pytest.mark.parametrize('operation', ['drag', 'dialog', 'clipboard'])
def test_moved_absolute_calendar_pattern_is_reconstructed_as_a_short_pattern(operation):
    master = source('+0d 4*1.')
    selected = occurrences(master)[2]
    delta = timedelta(days=2)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        if operation == 'drag':
            store.move_selection([selected], 'group', selected, selected.start + delta, selected.end + delta)
            moved = store.get_event(master.id)
        elif operation == 'dialog':
            store.edit_selection([selected], 'group', master,
                                 replace(master, start=master.start + delta, end=master.end + delta))
            moved = store.get_event(master.id)
        else:
            moved, = EventClipboard.capture([master], selected, 'group').instantiate('group', selected.start + delta, 'local')
        assert moved.schedule_expression != master.schedule_expression
        assert not re.search(r'\.\d{4}\.', moved.schedule_expression)
        assert not re.search(r'\b20\d\d\b', moved.schedule_expression)
        assert len(moved.schedule_expression) < 20
        assert starts(moved) == [value.start.astimezone(timezone.utc) + delta for value in occurrences(master)]
        assert_roundtrip(moved)


@pytest.mark.parametrize('operation', ['drag', 'dialog', 'clipboard'])
def test_future_transport_after_indefinite_daily_fourth_occurrence_partitions_three_past_events(operation):
    master = source('every(1d)')
    selected = occurrences(master)[3]
    delta = timedelta(hours=2)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        before, captured = partition_series(master, selected)
        assert len(occurrences(before)) == 3
        assert expression_matches(before, before.schedule_expression)
        assert len(expand_sequence(before.schedule_expression, before.start)) == 3
        if operation == 'drag':
            store.move_selection([selected], 'future', selected, selected.start + delta, selected.end + delta)
            head = store.get_event(master.id)
            tail, = [value for value in store.list_events() if value.id != master.id]
        elif operation == 'dialog':
            submitted = replace(captured, start=captured.start + delta, end=captured.end + delta)
            store.edit_selection([selected], 'future', selected, submitted)
            head = store.get_event(master.id)
            tail, = [value for value in store.list_events() if value.id != master.id]
        else:
            tail, = EventClipboard.capture([master], selected, 'future').instantiate('group', selected.start + delta, 'local')
            head = before
        assert starts(head) == starts(before)
        assert [item.start.hour for item in occurrences(head)] == [9, 9, 9]
        assert all(item.start.hour == 11 for item in occurrences(tail))
        assert 'COUNT=' not in tail.recurrence_rule and 'UNTIL=' not in tail.recurrence_rule
        assert tail.start == selected.start + delta
        assert_roundtrip(head)
        assert_roundtrip(tail)
        if operation != 'clipboard':
            store.undo()
            assert store.list_events() == [master]
            store.redo()
            assert starts(store.get_event(master.id)) == starts(head)


def test_split_one_retained_occurrence_has_an_explicit_anchor_without_a_duplicate():
    master = source('+0d +1d +3d')
    selected = occurrences(master)[1]
    before, _ = partition_series(master, selected)
    assert len(occurrences(before)) == 1
    assert reconstruct_authoring(before).expression == '+0d'
    assert before.schedule_expression == '+0d'
    assert before.expression_anchor == before.start.date().isoformat()


def test_relative_month_parts_keep_instruction_intent_after_splitting_and_reanchoring():
    master = source('+0d 4*+1m!', START.replace(year=2027, month=1, day=31))
    original = occurrences(master)
    before, after = partition_series(master, original[2])
    assert before.schedule_expression == '+0d +1m!'
    assert after.schedule_expression == '+0d 2*+1m!'
    assert starts(before) + starts(after) == starts(master)
    moved_head = updated_series(before, before,
        replace(before, start=before.start - timedelta(days=1), end=before.end - timedelta(days=1)))
    assert [(item.start.month, item.start.day) for item in occurrences(moved_head)] == [(1, 30), (2, 28)]
    assert moved_head.schedule_expression == '+0d +1m!'
    assert_roundtrip(moved_head)
    moved_tail = updated_series(after, after,
        replace(after, start=after.start + timedelta(days=2), end=after.end + timedelta(days=2)))
    assert [(item.start.month, item.start.day) for item in occurrences(moved_tail)] == [(4, 2), (5, 2), (6, 2)]
    assert moved_tail.schedule_expression == '+0d 2*+1m!'
    assert_roundtrip(moved_tail)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'dialog', 'clipboard'])
def test_calendar_relative_program_is_reevaluated_on_reanchor_instead_of_translating_cached_dates(operation):
    start = START.replace(year=2027, month=1, day=31)
    master = source('  +0d 2*(+1m!)  ', start)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        last = occurrences(master)[-1]
        store.edit_occurrence(last, replace(last, title='Special', description='Retained', end=last.end + timedelta(hours=2)))
        original = store.get_event(master.id)
        reference = occurrences(original)[0]
        target = reference.start - timedelta(days=1)
        if operation == 'projection':
            moved = updated_series(original, original, replace(original, start=target, end=target + timedelta(hours=1)))
        elif operation == 'drag':
            store.move_selection([reference], 'group', reference, target, target + timedelta(hours=1))
            moved = store.get_event(master.id)
        elif operation == 'dialog':
            store.edit_selection([reference], 'group', original, replace(original, start=target, end=target + timedelta(hours=1)))
            moved = store.get_event(master.id)
        else:
            moved, = EventClipboard.capture([original], reference, 'group').instantiate('group', target, 'local')
        values = occurrences(moved)
        assert [(item.start.month, item.start.day) for item in values] == [(1, 30), (2, 28), (3, 30)]
        assert moved.schedule_expression == master.schedule_expression
        assert moved.expression_anchor == '2027-01-30'
        assert (values[-1].title, values[-1].description) == ('Special', 'Retained')
        assert values[-1].end - values[-1].start == timedelta(hours=3)
        assert set(moved.recurrence_overrides) == {recurrence_key(values[-1].start)}
        assert_roundtrip(moved)


@pytest.mark.parametrize('operation', ['drag', 'dialog', 'clipboard'])
def test_nonlinear_relative_reanchor_preserves_moved_exception_content_by_instruction(operation):
    start = START.replace(year=2027, month=1, day=31)
    master = source('+0d 2*+1m!', start)
    canonical = replace(master, recurrence_rule='FREQ=MONTHLY;COUNT=3', recurrence_dates=())
    actual = expand_sequence(master.schedule_expression, start)
    nominal = expand_event(canonical, start - timedelta(days=1), start + timedelta(days=140))
    overrides = {item.recurrence_id: replace(item, start=actual[index], end=actual[index] + timedelta(hours=1)).to_dict()
                 for index, item in enumerate(nominal) if index}
    master = replace(canonical, recurrence_overrides=overrides)
    assert expression_matches(master, master.schedule_expression)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        reference = occurrences(master)[0]
        target = reference.start - timedelta(days=1)
        if operation == 'drag':
            store.move_selection([reference], 'group', reference, target, target + timedelta(hours=1))
            moved = store.get_event(master.id)
        elif operation == 'dialog':
            store.edit_selection([reference], 'group', master, replace(master, start=target, end=target + timedelta(hours=1)))
            moved = store.get_event(master.id)
        else:
            moved, = EventClipboard.capture([master], reference, 'group').instantiate('group', target, 'local')
        assert [(item.start.month, item.start.day) for item in occurrences(moved)] == [(1, 30), (2, 28), (3, 30)]
        assert moved.schedule_expression == master.schedule_expression
        assert_roundtrip(moved)


@pytest.mark.parametrize('operation', ['drag', 'clipboard'])
def test_single_edit_then_whole_transport_and_future_split_preserve_exception_identity_and_content(operation):
    master = source('+0d 5*+1w', START.replace(month=10, day=19))
    delta = timedelta(days=9, hours=1)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        selected = occurrences(master)[2]
        store.edit_occurrence(selected, replace(selected, title='Individual title',
            description='Individual notes', location='Individual room',
            start=selected.start + timedelta(hours=2), end=selected.end + timedelta(hours=4)))
        original = store.get_event(master.id)
        original_items = occurrences(original)
        if operation == 'drag':
            reference = occurrences(original)[1]
            store.move_selection([reference], 'group', reference,
                                 reference.start + delta, reference.end + delta)
            moved = store.get_event(master.id)
        else:
            reference = original_items[0]
            copied, = EventClipboard.capture([original], reference, 'group').instantiate('group', reference.start + delta, 'local')
            store.apply('Paste', [copied])
            moved = store.get_event(copied.id)
        def translated(value):
            return (value.astimezone(ZONE).replace(tzinfo=None) + delta).replace(tzinfo=ZONE)
        assert [(item.start, item.end, item.title, item.description, item.location) for item in occurrences(moved)] == [
            (translated(item.start), translated(item.end), item.title, item.description, item.location) for item in original_items]
        moved_key = recurrence_key(translated(selected.start))
        assert set(moved.recurrence_overrides) == {moved_key}
        assert_roundtrip(moved)
        before, after = partition_series(moved, occurrences(moved)[3])
        assert starts(before) + starts(after) == starts(moved)
        assert set(before.recurrence_overrides) == {moved_key}
        assert not after.recurrence_overrides
        assert_roundtrip(before)
        assert_roundtrip(after)
        store.undo()
        assert store.get_event(original.id) == original
        if operation == 'clipboard':
            assert store.get_event(moved.id) is None
        store.redo()
        assert store.get_event(moved.id) == moved
