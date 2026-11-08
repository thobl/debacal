"""Calendar overflow choices survive copy, moves and following partitions."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from debacal.event_clipboard import EventClipboard
from debacal.models import Event
from debacal.recurrence import RecurrenceError, expand_event, recurrence_key, updated_series
from debacal.schedule_authoring import expression_event, expression_matches
from debacal.series_operations import partition_series, updated_future
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2027, 1, 28, tzinfo=ZONE)
WINDOW = datetime(2027, 1, 1, tzinfo=ZONE), datetime(2027, 8, 1, tzinfo=ZONE)


def source(policy, *, bounded=True, count=4):
    bound = f', count={count}' if bounded else ''
    text = f'every(1m, overflow={policy}, span=2{bound})'
    prototype = Event('range', 'local', 'Monthly range', START, START + timedelta(days=2),
                      all_day=True, timezone_id=ZONE.key)
    result = expression_event(prototype, text)
    return replace(result, schedule_expression=text, schedule_syntax=2,
                   expression_anchor=START.date().isoformat())


def values(event):
    return expand_event(event, *WINDOW) if event else []


def dates(event):
    return [(item.start.date().isoformat(), item.end.date().isoformat()) for item in values(event)]


@pytest.mark.parametrize('policy, expected', [
    ('skip', [('2027-01-28', '2027-01-30'), ('2027-03-28', '2027-03-30'),
              ('2027-04-28', '2027-04-30'), ('2027-05-28', '2027-05-30')]),
    ('shift', [('2027-01-28', '2027-01-30'), ('2027-02-27', '2027-03-01'),
               ('2027-03-28', '2027-03-30'), ('2027-04-28', '2027-04-30')]),
    ('cut', [('2027-01-28', '2027-01-30'), ('2027-02-28', '2027-03-01'),
             ('2027-03-28', '2027-03-30'), ('2027-04-28', '2027-04-30')]),
])
def test_three_policies_have_exact_starts_ends_and_counts(policy, expected):
    event = source(policy)
    assert dates(event) == expected
    assert expression_matches(event, event.schedule_expression)


@pytest.mark.parametrize('policy', ['skip', 'shift', 'cut'])
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('operation', ['project', 'move', 'copy'])
def test_whole_series_transports_keep_the_chosen_policy(tmp_path, policy, bounded, operation):
    event = source(policy, bounded=bounded)
    reference = next(item for item in values(event) if item.recurrence_id == recurrence_key(START, True))
    target = START + timedelta(days=1)
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [event])
        if operation == 'project':
            moved = updated_series(event, event, replace(event, start=target, end=target + timedelta(days=2)))
        elif operation == 'move':
            store.move_selection([reference], 'group', reference, target, target + timedelta(days=2))
            moved = store.get_event(event.id)
        else:
            moved, = EventClipboard.capture([event], reference, 'group').instantiate('group', target, 'local')
            assert moved.id != event.id
        expected_months = [1, 3, 4, 5] if policy in ('skip', 'cut') else [1, 2, 3, 4]
        actual = values(moved)
        assert [item.start.month for item in actual[:4]] == expected_months
        assert all(item.start.day == 29 for item in actual if item.start.month != 2)
        february = next((item for item in actual if item.start.month == 2), None)
        if policy == 'shift':
            assert (february.start.day, february.end.date().isoformat()) == (27, '2027-03-01')
        elif policy == 'cut':
            assert february is None
        assert moved.range_policy == ('cut' if policy == 'cut' else '')
        assert expression_matches(moved, moved.schedule_expression)
        if operation == 'move':
            store.undo()
            assert store.get_event(event.id) == event


@pytest.mark.parametrize('bounded', [False, True])
def test_cut_partition_at_february_retains_full_march_duration(bounded):
    event = source('cut', bounded=bounded)
    february = next(item for item in values(event) if item.start.month == 2)
    head, tail = partition_series(event, february)
    assert len(values(head)) == 1
    assert tail.range_policy == 'cut'
    # The stored range stays two days, while February's displayed cut is one.
    assert tail.end - tail.start == timedelta(days=2)
    assert dates(tail)[:2] == [('2027-02-28', '2027-03-01'), ('2027-03-28', '2027-03-30')]
    assert expression_matches(tail, tail.schedule_expression)


@pytest.mark.parametrize('policy', ['skip', 'shift', 'cut'])
def test_following_partition_copy_and_title_edit_keep_policy_and_all_ranges(policy):
    event = source(policy)
    selected = values(event)[1]
    head, tail = partition_series(event, selected)
    assert dates(head) + dates(tail) == dates(event)
    assert expression_matches(head, head.schedule_expression)
    assert expression_matches(tail, tail.schedule_expression)
    copied, = EventClipboard.capture([event], selected, 'future').instantiate('group', selected.start, 'local')
    assert dates(copied) == dates(tail)
    assert copied.range_policy == tail.range_policy
    edited = replace(selected, title='Future title', recurrence_rule=event.recurrence_rule,
                     recurrence_dates=event.recurrence_dates, excluded_dates=event.excluded_dates,
                     recurrence_overrides=event.recurrence_overrides,
                     schedule_expression=event.schedule_expression, schedule_syntax=2,
                     range_policy=event.range_policy)
    _, changed = updated_future(event, selected, edited)
    assert dates(changed) == dates(tail)
    assert all(item.title == 'Future title' for item in values(changed))
    assert changed.range_policy == tail.range_policy


def test_cut_ordinary_copy_detaches_the_visible_short_range_without_future_policy():
    event = source('cut')
    february = next(item for item in values(event) if item.start.month == 2)
    copied, = EventClipboard.capture([event], february, 'single').instantiate('single', february.start, 'local')
    assert not copied.recurring
    assert copied.range_policy == ''
    assert copied.start == february.start and copied.end == february.end


@pytest.mark.parametrize('count', [1, 3, None])
@pytest.mark.parametrize('operation', ['project', 'move', 'copy'])
def test_shifted_first_period_before_virtual_reference_is_not_dropped_or_duplicated(tmp_path, count, operation):
    event = source('shift', bounded=count is not None, count=count or 4)
    reference = values(event)[0]
    target = datetime(2027, 2, 28, tzinfo=ZONE)
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [event])
        if operation == 'project':
            moved = updated_series(event, event, replace(event, start=target, end=target + timedelta(days=2)))
        elif operation == 'move':
            store.move_selection([reference], 'group', reference, target, target + timedelta(days=2))
            moved = store.get_event(event.id)
        else:
            moved, = EventClipboard.capture([event], reference, 'group').instantiate('group', target, 'local')
        actual = values(moved)
        assert moved.start == target
        assert (actual[0].start, actual[0].end) == (datetime(2027, 2, 27, tzinfo=ZONE), datetime(2027, 3, 1, tzinfo=ZONE))
        assert target not in [item.start for item in actual]
        assert recurrence_key(target, True) in moved.excluded_dates
        assert expression_matches(moved, moved.schedule_expression)
        if count is not None:
            assert len(actual) == count
        if count != 1:
            assert actual[1].start == datetime(2027, 3, 28, tzinfo=ZONE)
            assert actual[1].end == datetime(2027, 3, 30, tzinfo=ZONE)
        assert moved.schedule_expression == event.schedule_expression
        if operation == 'move':
            store.undo()
            assert store.get_event(event.id) == event


@pytest.mark.parametrize('count', [1, 3, None])
def test_shift_prelude_following_split_and_copy_preserve_every_start_and_range(count):
    event = source('shift', bounded=count is not None, count=count or 4)
    target = datetime(2027, 2, 28, tzinfo=ZONE)
    moved = updated_series(event, event, replace(event, start=target, end=target + timedelta(days=2)))
    original_values = values(moved)
    head, tail = partition_series(moved, original_values[0])
    assert head is None
    assert dates(tail) == dates(moved)
    assert expression_matches(tail, tail.schedule_expression)
    copied, = EventClipboard.capture([moved], original_values[0], 'future').instantiate('group', original_values[0].start, 'local')
    assert dates(copied) == dates(moved)
    if count != 1:
        head, tail = partition_series(moved, original_values[1])
        assert dates(head) + dates(tail) == dates(moved)
        assert expression_matches(head, head.schedule_expression)
        assert expression_matches(tail, tail.schedule_expression)


@pytest.mark.parametrize('policy', ['skip', 'shift', 'cut'])
@pytest.mark.parametrize('scope', ['group', 'future'])
def test_moving_policy_series_keeps_detached_range_at_its_original_identity(tmp_path, policy, scope):
    event = source(policy, bounded=False)
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [event])
        selected = values(event)[1]
        own_start = selected.start + timedelta(days=2)
        store.edit_occurrence(selected, replace(selected, start=own_start, end=own_start + timedelta(days=3),
                                                title='Moved range', description='Keep its own notes', location='Room 7'))
        original = store.get_event(event.id)
        selected = store.get_occurrence(selected.id)
        delta = timedelta(days=1)
        edited = replace(original, start=selected.start + delta, end=selected.end + delta)
        if scope == 'future' and policy == 'shift':
            # The clipped reference at Feb 27 has an explicit preferred day
            # 28 and a date prefix. Moving its whole two-day range to Feb 28
            # cannot fit while preserving that fixed tail cursor.
            history = store.history()
            with pytest.raises(RecurrenceError, match='move only this event|Update its rule explicitly'):
                store.edit_future(selected, edited, expected_master=original, preserve_exceptions=True)
            assert store.list_events() == [original]
            assert store.history() == history
            return
        operation = (store.edit_series(selected, edited, preserve_exceptions=True) if scope == 'group' else
                     store.edit_future(selected, edited, expected_master=original, preserve_exceptions=True))
        actual = store.query_occurrences(*WINDOW)
        preserved = next(item for item in actual if item.title == 'Moved range')
        assert preserved.start == own_start + delta
        assert preserved.end - preserved.start == timedelta(days=3)
        assert preserved.description == 'Keep its own notes' and preserved.location == 'Room 7'
        changed = store.get_event(preserved.series_id)
        assert changed.range_policy == ('cut' if policy == 'cut' else '')
        assert expression_matches(changed, changed.schedule_expression)
        store.undo(operation)
        assert store.list_events() == [original]
