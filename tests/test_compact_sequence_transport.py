"""Compact weekday rules retain their phase and identities across operations."""
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event
from debacal.event_clipboard import EventClipboard
from debacal.ics import export_ics, parse_ics
from debacal.recurrence import expand_event, occurrence, recurrence_key, relative_reanchor_map, updated_series
from debacal.schedule_authoring import expression_event, expression_matches
from debacal.series_operations import partition_series
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 2, 9, tzinfo=ZONE)  # Monday


def source(text, *, start=START, all_day=False):
    if all_day:
        start = start.replace(hour=0)
    event = Event('series', 'local', 'Appointment', start,
                  start + (timedelta(days=1) if all_day else timedelta(hours=1)), all_day=all_day)
    event = expression_event(event, text)
    return replace(event, schedule_expression=text, expression_anchor=start.date().isoformat(), schedule_syntax=2)


def values(event, start=START - timedelta(days=10), end=START + timedelta(weeks=30)):
    return expand_event(event, start, end)


@pytest.mark.parametrize('interval', [1, 2, 3])
@pytest.mark.parametrize('delta', [-1, 1, 6, 7])
@pytest.mark.parametrize('all_day', [False, True])
def test_weekday_move_rotates_week_boundaries_without_rephasing(interval, delta, all_day):
    original = source(f'every({interval}w, on=MO+WE, count=12)', all_day=all_day)
    shift = timedelta(days=delta)
    moved = updated_series(original, original, replace(original, start=original.start + shift,
                                                       end=original.end + shift))
    assert [item.start for item in values(moved)] == [item.start + shift for item in values(original)]
    assert not moved.schedule_expression.startswith('schedule{')
    assert expression_matches(moved, moved.schedule_expression)


def test_unannotated_multiweek_rule_moves_week_start_together_with_weekdays():
    original = replace(source('every(2w, on=MO+WE, count=8)'), schedule_expression='', expression_anchor='', schedule_syntax=1)
    shift = timedelta(days=-1)
    moved = updated_series(original, original, replace(original, start=original.start + shift,
                                                       end=original.end + shift))
    assert [item.start for item in values(moved)] == [item.start + shift for item in values(original)]
    assert 'WKST=SU' in moved.recurrence_rule


def test_far_future_weekday_identity_lookup_does_not_walk_history():
    original = source('every(2w, on=MO+WE)')
    target = START + timedelta(weeks=40000, days=2)
    with patch('debacal.recurrence._recurrence_set', side_effect=AssertionError('History was enumerated')):
        mapping = relative_reanchor_map(original, START - timedelta(days=1))
        assert mapping[recurrence_key(target)] == recurrence_key(target - timedelta(days=1))


def test_weekday_split_then_move_keeps_detached_payload_and_both_compact_definitions(tmp_path):
    original = source('every(2w, on=MO+WE, count=16)')
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [original])
        items = values(original)
        chosen = items[5]
        later = items[9]
        store.edit_occurrence(later, replace(later, title='Retain this title', description='Original exception notes'))
        original = store.get_event(original.id)
        head, tail = partition_series(original, chosen)
        assert len(values(head)) == 5
        assert len(values(tail)) == 11
        assert all(expression_matches(part, part.schedule_expression) for part in (head, tail))
        assert all(not part.schedule_expression.startswith('schedule{') for part in (head, tail))
        shift = timedelta(days=-1)
        reference = occurrence(tail, chosen.start, chosen.recurrence_id)
        edited = replace(reference, start=reference.start + shift, end=reference.end + shift,
                         recurrence_rule=tail.recurrence_rule, recurrence_dates=tail.recurrence_dates,
                         excluded_dates=tail.excluded_dates, recurrence_overrides=tail.recurrence_overrides,
                         schedule_expression=tail.schedule_expression, schedule_syntax=2)
        moved = updated_series(tail, reference, edited)
        retained = next(item for item in values(moved) if item.title == 'Retain this title')
        assert retained.description == 'Original exception notes'
        assert retained.start == later.start + shift
        assert expression_matches(moved, moved.schedule_expression)


def test_monthly_ordinal_move_keeps_ordinal_pattern_when_new_reference_matches():
    original = source('every(1m, on=-1FR, count=6)', start=datetime(2026, 4, 24, 9, tzinfo=ZONE))
    shift = timedelta(days=1)
    moved = updated_series(original, original, replace(original, start=original.start + shift,
                                                       end=original.end + shift))
    assert moved.start == original.start + shift
    assert 'on=-1SA' in moved.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


def test_monthly_ordinal_move_cannot_silently_replace_dragged_occurrence():
    original = source('every(1m, on=-1FR)', start=datetime(2027, 4, 30, 9, tzinfo=ZONE))
    shift = timedelta(days=1)  # First Saturday of May, not its last Saturday.
    with pytest.raises(ValueError):
        updated_series(original, original, replace(original, start=original.start + shift,
                                                    end=original.end + shift))


@pytest.mark.parametrize('tail', ['', ' every(1m!)'])
@pytest.mark.parametrize('operation', ['projection', 'drag', 'dialog', 'clipboard'])
def test_coincident_month_program_moves_consistently_through_every_entrypoint(tmp_path, tail, operation):
    anchor = datetime(2027, 1, 31, 9, tzinfo=ZONE)
    text = '+0d +0d +1m!' + tail
    original = source(text, start=anchor)
    beginning, ending = anchor - timedelta(days=5), anchor + timedelta(days=100)
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [original])
        items = expand_event(original, beginning, ending)
        duplicate = next(item for item in items if item.recurrence_id != recurrence_key(anchor))
        store.edit_occurrence(duplicate, replace(duplicate, title='Extra appointment', description='Retain me',
                                                end=duplicate.start + timedelta(minutes=30)))
        original = store.get_event(original.id)
        chosen = next(item for item in expand_event(original, beginning, ending)
                      if item.recurrence_id == recurrence_key(anchor))
        target = anchor - timedelta(days=1)
        if operation == 'projection':
            moved = updated_series(original, original, replace(original, start=target, end=target + timedelta(hours=1)))
        elif operation == 'drag':
            store.move_selection([chosen], 'group', chosen, target, target + timedelta(hours=1))
            moved = store.get_event(original.id)
        elif operation == 'dialog':
            store.edit_selection([chosen], 'group', original,
                                 replace(original, start=target, end=target + timedelta(hours=1)))
            moved = store.get_event(original.id)
        else:
            moved, = EventClipboard.capture([original], chosen, 'group').instantiate('group', target, 'local')
            assert moved.id != original.id
        actual = expand_event(moved, beginning, ending)
        assert moved.schedule_expression == text
        assert [item.start.date() for item in actual[:3]] == [target.date(), target.date(), datetime(2027, 2, 28).date()]
        extra = next(item for item in actual if item.title == 'Extra appointment')
        assert extra.description == 'Retain me' and extra.end - extra.start == timedelta(minutes=30)
        assert len({item.recurrence_id for item in actual}) == len(actual)
        assert expression_matches(moved, moved.schedule_expression)
        loaded, = parse_ics(export_ics([moved]), 'local')
        assert [(item.start, item.end, item.title) for item in expand_event(loaded, beginning, ending)] == [
            (item.start, item.end, item.title) for item in actual]
        if operation in ('drag', 'dialog'):
            store.undo()
            assert store.get_event(original.id) == original


def test_coincident_following_partition_and_copy_keep_every_individual_identity():
    original = source('+0d +0d +1w +0d +1w')
    actual = values(original)
    selected = next(item for item in actual if item.start == START + timedelta(weeks=1))
    head, tail = partition_series(original, selected)
    assert len(values(head)) == 2 and len(values(tail)) == 3
    assert all(item.start < selected.start for item in values(head))
    assert all(item.start >= selected.start for item in values(tail))
    assert all(expression_matches(part, part.schedule_expression) for part in (head, tail) if part)
    assert all(not part.schedule_expression.startswith('schedule{') for part in (head, tail) if part)
    copied, = EventClipboard.capture([original], selected, 'future').instantiate('group', selected.start, 'local')
    assert [(item.start, item.end) for item in values(copied)] == [(item.start, item.end) for item in values(tail)]
    assert len({item.id for item in values(copied)}) == len(values(copied))
