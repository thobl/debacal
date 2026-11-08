"""Independent anchors and occurrence identities survive every series edit."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from debacal.event_clipboard import EventClipboard
from debacal.models import Event
from debacal.patterns import compile_sequence
from debacal.recurrence import expand_event, occurrence, recurrence_key, relative_reanchor_map, updated_series
from debacal.schedule_authoring import expression_matches, reconstruct_authoring
from debacal.series_operations import partition_series
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
ANCHOR = datetime(2027, 1, 31, 9, tzinfo=ZONE)


def authored(text, anchor=ANCHOR):
    return Event('series', 'local', 'Meeting', anchor, anchor + timedelta(hours=1),
                 timezone_id=ZONE.key, ical_uid='series@example.test',
                 schedule_expression=text, schedule_syntax=2,
                 expression_anchor=anchor.date().isoformat(),
                 **compile_sequence(text, anchor).fields)


def visible(master):
    return expand_event(master, ANCHOR - timedelta(days=100), ANCHOR + timedelta(days=700))


def move(master, target, operation):
    reference = visible(master)[0]
    delta = target - reference.start
    if operation == 'projection':
        return updated_series(master, reference,
                              replace(reference, start=target, end=reference.end + delta,
                                      recurrence_rule=master.recurrence_rule,
                                      recurrence_dates=master.recurrence_dates,
                                      excluded_dates=master.excluded_dates,
                                      recurrence_overrides=master.recurrence_overrides,
                                      schedule_expression=master.schedule_expression,
                                      expression_anchor=master.expression_anchor,
                                      schedule_syntax=master.schedule_syntax))
    if operation == 'clipboard':
        result, = EventClipboard.capture([master], reference, 'group').instantiate('group', target, 'local')
        return result
    with Store(':memory:') as store:
        store.apply('Create', [master])
        store.move_selection([reference], 'group', reference, target, reference.end + delta)
        result = store.get_event(master.id)
        store.undo()
        assert store.get_event(master.id) == master
        store.redo()
        assert store.get_event(master.id) == result
        return result


@pytest.mark.parametrize('operation', ['projection', 'drag', 'clipboard'])
@pytest.mark.parametrize(('text', 'expected'), [
    ('+0d 3*+1m', [(1, 30), (3, 30), (4, 30), (5, 30)]),
    ('+0d 3*+1m!', [(1, 30), (2, 28), (3, 30), (4, 30)]),
])
def test_whole_transport_reevaluates_skip_or_clamp_program_without_changing_it(operation, text, expected):
    master = authored(text)
    last = visible(master)[-1]
    special = replace(last, title='Individual', description='Keep me', end=last.end + timedelta(hours=2))
    master = replace(master, recurrence_overrides={last.recurrence_id: special.to_dict()})
    moved = move(master, ANCHOR - timedelta(days=1), operation)
    items = visible(moved)
    assert [(item.start.month, item.start.day) for item in items] == expected
    assert moved.schedule_expression == text
    assert expression_matches(moved, text)
    assert (items[-1].title, items[-1].description) == ('Individual', 'Keep me')
    assert items[-1].end - items[-1].start == timedelta(hours=3)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'clipboard'])
def test_transport_does_not_materialize_an_omitted_anchor(operation):
    master = authored('+1d +1d')
    moved = move(master, visible(master)[0].start + timedelta(days=2), operation)
    assert len(visible(moved)) == 2
    assert moved.start == ANCHOR + timedelta(days=2)
    assert recurrence_key(moved.start) in moved.excluded_dates
    assert moved.schedule_expression == master.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'clipboard'])
def test_nonlinear_reanchor_maps_moved_exception_identities_by_their_actual_instruction(operation):
    master = authored('+0d 3*+1m!')
    actual = visible(master)
    nominal_master = replace(master, recurrence_rule='FREQ=MONTHLY;COUNT=4',
                             recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
    nominal = visible(nominal_master)
    overrides = {item.recurrence_id: replace(item, start=actual[index].start,
        end=actual[index].end, title=f'Instruction {index}').to_dict()
        for index, item in enumerate(nominal) if index}
    master = replace(nominal_master, recurrence_overrides=overrides)
    assert expression_matches(master, master.schedule_expression)
    result = move(master, ANCHOR - timedelta(days=1), operation)
    items = visible(result)
    assert [(item.start.month, item.start.day) for item in items] == [(1, 30), (2, 28), (3, 30), (4, 30)]
    assert [item.title for item in items] == ['Meeting', 'Instruction 1', 'Instruction 2', 'Instruction 3']
    assert result.schedule_expression == master.schedule_expression
    assert expression_matches(result, result.schedule_expression)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'clipboard'])
def test_relative_prefix_and_monthly_tail_reanchor_by_instruction_and_tail_ordinal(operation):
    master = authored('+0d +1m every(1m)')
    third = visible(master)[2]
    master = replace(master, recurrence_overrides={third.recurrence_id:
        replace(third, title='Third logical occurrence', description='Keep tail identity').to_dict()})
    moved = move(master, ANCHOR - timedelta(days=1), operation)
    items = visible(moved)
    assert [(item.start.month, item.start.day) for item in items[:5]] == [(1, 30), (3, 30), (4, 30), (5, 30), (6, 30)]
    assert items[2].title == 'Third logical occurrence'
    assert items[2].description == 'Keep tail identity'
    assert moved.schedule_expression == master.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


@pytest.mark.parametrize('operation', ['projection', 'drag', 'clipboard'])
def test_relative_tail_reanchor_recompiles_nonlinear_prefix_exceptions(operation):
    master = authored('+0d +1m! +1d every(1m)')
    moved = move(master, ANCHOR - timedelta(days=1), operation)
    expected = authored(master.schedule_expression, ANCHOR - timedelta(days=1))
    assert [item.start for item in visible(moved)] == [item.start for item in visible(expected)]
    assert moved.schedule_expression == master.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


def test_all_day_tail_identity_lookup_normalizes_a_date_until_boundary():
    anchor = ANCHOR.replace(hour=0)
    text = 'every(1d, until=4.2.2027)'
    master = Event('series', 'local', 'Dates', anchor, anchor + timedelta(days=1),
                   all_day=True, timezone_id=ZONE.key, schedule_expression=text,
                   schedule_syntax=2, expression_anchor=anchor.date().isoformat(),
                   **compile_sequence(text, anchor, all_day=True).fields)
    mapping = relative_reanchor_map(master, anchor + timedelta(days=2))
    assert '2027-02-02' in mapping
    assert mapping['2027-02-02'] == '2027-02-04'


@pytest.mark.parametrize('unit,days', [('d', 1), ('w', 7)])
def test_far_future_linear_tail_identity_does_not_enumerate_recurrence_history(unit, days):
    master = authored(f'every(1{unit})')
    target = ANCHOR + timedelta(days=days * 20000)
    mapping = relative_reanchor_map(master, ANCHOR + timedelta(days=2))
    with patch('debacal.recurrence._recurrence_set', side_effect=AssertionError('must not enumerate')):
        assert mapping[recurrence_key(target)] == recurrence_key(target + timedelta(days=2))


def test_linear_tail_identity_uses_nominal_clock_before_normalizing_a_dst_gap():
    anchor = datetime(2027, 3, 27, 2, 30, tzinfo=ZONE)
    master = authored('every(1d)', anchor)
    mapping = relative_reanchor_map(master, anchor - timedelta(days=1))
    generated = datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    assert mapping[recurrence_key(generated)] == recurrence_key(anchor)


def test_split_clamped_program_retains_dates_even_when_suffix_needs_a_document():
    master = authored('+0d 3*+1m!')
    original = visible(master)
    head, tail = partition_series(master, original[1])
    assert [item.start for item in visible(head) + visible(tail)] == [item.start for item in original]
    for part in (head, tail):
        assert part.schedule_expression
        assert expression_matches(part, part.schedule_expression)


@pytest.mark.parametrize('excluded_anchor', [False, True])
def test_coincident_actual_starts_keep_both_nominal_identities_and_total_text(excluded_anchor):
    master = authored('every(1d, count=4)')
    original = visible(master)
    moved_occurrence = replace(original[1], start=original[2].start,
                               end=original[2].end, title='Same clock, separate occurrence')
    master = replace(master, recurrence_overrides={original[1].recurrence_id: moved_occurrence.to_dict()},
                     excluded_dates=(recurrence_key(master.start),) if excluded_anchor else ())
    master = replace(master, schedule_expression=reconstruct_authoring(master).expression)
    expected = visible(master)
    moved = updated_series(master, master,
                           replace(master, start=master.start + timedelta(hours=1),
                                   end=master.end + timedelta(hours=1)))
    items = visible(moved)
    assert len(items) == len(expected)
    assert len({item.id for item in items}) == len(items)
    assert len({item.start for item in items}) < len(items)
    assert [item.start for item in items] == [item.start + timedelta(hours=1) for item in expected]
    assert moved.schedule_expression
    assert expression_matches(moved, moved.schedule_expression)


def test_excluded_anchor_and_detached_prototype_remain_representable_after_split():
    master = authored('every(1d, count=6)')
    first = occurrence(master, master.start)
    master = replace(master, recurrence_overrides={first.recurrence_id: replace(first,
        start=first.start + timedelta(hours=3), end=first.end + timedelta(hours=3)).to_dict()})
    master = replace(master, schedule_expression=reconstruct_authoring(master).expression)
    original = visible(master)
    head, tail = partition_series(master, original[3])
    assert [item.start for item in visible(head) + visible(tail)] == [item.start for item in original]
    for part in (head, tail):
        assert part.schedule_expression
        assert expression_matches(part, part.schedule_expression)
