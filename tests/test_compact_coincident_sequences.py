"""Compact occurrence multiplicities retain standard calendar identities."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import PatternError, compile_sequence, expand_sequence, reconstruct_sequence
from debacal.recurrence import expand_event, occurrence, recurrence_key, updated_series
from debacal.schedule_authoring import expression_event, expression_matches, reconstruct_authoring
from debacal.sequence_document import document_expression
from debacal.series_operations import partition_series
from debacal.selection import EventSelection, affected_visible
from debacal.store import Store


UTC = timezone.utc
START = datetime(2027, 1, 31, 9, tzinfo=UTC)


def source(start=START, **changes):
    duration = timedelta(days=1) if changes.get('all_day') else timedelta(hours=1)
    return Event('series', 'local', 'Original title', start, start + duration, **changes)


def authored(text, start=START, **changes):
    event = expression_event(source(start, **changes), text)
    return replace(event, schedule_expression=text, schedule_syntax=2,
                   expression_anchor=start.date().isoformat())


def visible(event, days=100):
    return expand_event(event, event.start - timedelta(days=5), event.start + timedelta(days=days))


def counts(event, days=100):
    return Counter(item.start.date() if item.all_day else item.start.timestamp() for item in visible(event, days))


@pytest.mark.parametrize('text', ['+0d +0d', '2*+0d', '31.1.2027 31.1.2027'])
def test_each_explicit_emission_can_be_a_distinct_same_time_occurrence(text):
    assert expand_sequence(text, START) == [START, START]
    compiled = compile_sequence(text, START)
    assert compiled.duplicate_starts == (START,)
    with pytest.raises(PatternError, match='identit'):
        _ = compiled.fields
    event = authored(text)
    actual = visible(event)
    assert [item.start for item in actual] == [START, START]
    assert len({item.recurrence_id for item in actual}) == 2
    assert {item.title for item in actual} == {'Original title'}
    assert expression_matches(event, text)


def test_normalizer_retains_repeated_starts_with_zero_steps():
    starts = [START, START, START + timedelta(days=3), START + timedelta(days=3)]
    text = reconstruct_sequence(starts, anchor=START)
    assert '+0d' in text and not text.startswith('schedule')
    assert expand_sequence(text, START) == starts


@pytest.mark.parametrize('all_day', [False, True])
def test_standard_ics_roundtrip_keeps_all_same_time_occurrences(all_day):
    start = START.replace(hour=0) if all_day else START
    original = authored('3*+0d +1d', start, all_day=all_day)
    restored = parse_ics(export_ics([original]), original.calendar_id)[0]
    assert counts(restored) == counts(original)
    assert len(visible(restored)) == 4
    assert len({item.recurrence_id for item in visible(restored)}) == 4
    assert expression_matches(restored, restored.schedule_expression)
    assert b'RECURRENCE-ID' in export_ics([original])


def test_reducing_same_time_copies_keeps_the_original_reference_first():
    original = authored('2*+0d')
    key = next(iter(original.recurrence_overrides))
    payloads = deepcopy(original.recurrence_overrides)
    payloads[key]['title'] = 'The extra copy'
    original = replace(original, recurrence_overrides=payloads)
    changed = expression_event(original, '+0d')
    assert len(visible(changed)) == 1
    assert visible(changed)[0].title == original.title
    assert not changed.recurrence_overrides


def test_existing_coincident_payloads_keep_nominal_ids_ranges_and_content():
    original = source(recurrence_rule='FREQ=DAILY;COUNT=3')
    one = occurrence(original, START + timedelta(days=1))
    two = occurrence(original, START + timedelta(days=2))
    shared = START + timedelta(days=4, hours=2)
    one = replace(one, start=shared, end=shared + timedelta(hours=2), title='One', description='One notes')
    two = replace(two, start=shared, end=shared + timedelta(hours=3), title='Two', location='Two room')
    original = replace(original, recurrence_overrides={one.recurrence_id: one.to_dict(), two.recurrence_id: two.to_dict()})
    view = reconstruct_authoring(original)
    assert not view.structured and '+0d' in view.expression
    restored = expression_event(original, ' ' + view.expression + ' ')
    assert restored.recurrence_overrides == original.recurrence_overrides
    assert counts(restored) == counts(original)
    assert expression_matches(restored, view.expression)


def test_retaining_a_move_does_not_overwrite_a_new_event_at_its_nominal_slot():
    original = source(recurrence_rule='FREQ=DAILY;COUNT=3')
    nominal = recurrence_key(START + timedelta(days=1))
    moved = replace(occurrence(original, START + timedelta(days=1)),
                    start=START + timedelta(days=2), end=START + timedelta(days=2, hours=2), title='Moved')
    original = replace(original, recurrence_overrides={nominal: moved.to_dict()})
    changed = expression_event(original, '+0d +1d +1d +0d')
    assert [item.start for item in visible(changed)] == [START, START + timedelta(days=1),
                                                       START + timedelta(days=2), START + timedelta(days=2)]
    assert changed.recurrence_overrides[nominal]['title'] == 'Moved'
    assert expression_matches(changed, '+0d +1d +1d +0d')


@pytest.mark.parametrize('text, expected', [
    ('+0d +0d every(1d, count=3)', [START, START, START + timedelta(days=1), START + timedelta(days=2)]),
    ('+1d 31.1.2027 every(1d, count=3)', [START + timedelta(days=1), START,
                                        START + timedelta(days=1), START + timedelta(days=2)]),
])
def test_tail_shares_only_its_final_prefix_cursor(text, expected):
    assert expand_sequence(text, START) == expected
    event = authored(text)
    assert counts(event) == Counter(value.timestamp() for value in expected)
    assert expression_matches(event, text)


def test_ongoing_collision_reconstructs_compactly_without_losing_future_dates():
    original = source(recurrence_rule='FREQ=DAILY')
    selected = occurrence(original, START + timedelta(days=1))
    draft = ScheduleDraft.from_event(original)
    changed = draft.edit_occurrence(original, selected, START + timedelta(days=2),
                                     START + timedelta(days=2, hours=2))
    assert not draft.expression.startswith('schedule')
    assert expression_matches(changed, draft.expression)
    assert counts(changed)[(START + timedelta(days=2)).timestamp()] == 2
    assert len(visible(changed, 10)) == 10
    reparsed = expression_event(changed, draft.expression)
    assert reparsed.recurrence_overrides == changed.recurrence_overrides
    assert counts(reparsed) == counts(changed)


@pytest.mark.parametrize('tail', ['', ' every(1m!)'])
def test_nonlinear_reanchor_keeps_separate_ids_and_individual_content(tail):
    text = '+0d +0d +1m!' + tail
    original = authored(text)
    key = next(iter(original.recurrence_overrides))
    payloads = deepcopy(original.recurrence_overrides)
    payloads[key]['title'] = 'Specific copy'
    payloads[key]['end'] = (START + timedelta(hours=3)).isoformat()
    original = replace(original, recurrence_overrides=payloads)
    start = START - timedelta(days=1)
    changed = updated_series(original, original, replace(original, start=start, end=start + timedelta(hours=1)))
    assert changed.schedule_expression == text
    assert expression_matches(changed, text)
    seen = visible(changed)
    assert [item.start for item in seen[:3]] == [start, start, datetime(2027, 2, 28, 9, tzinfo=UTC)]
    assert len({item.recurrence_id for item in seen}) == len(seen)
    special = next(item for item in seen if item.title == 'Specific copy')
    assert special.end - special.start == timedelta(hours=3)


def test_add_and_remove_an_individual_coincident_range_keep_the_other_payload():
    original = authored('2*+0d')
    draft = ScheduleDraft.from_event(original)
    added = draft.add_occurrence(original, START, START + timedelta(hours=4))
    assert len(visible(added)) == 3
    selected = next(item for item in visible(added) if item.end - item.start == timedelta(hours=4))
    assert selected.recurrence_id not in original.recurrence_overrides
    removed = draft.remove_occurrence(added, selected)
    assert len(visible(removed)) == 2
    assert original.recurrence_overrides == {key: value for key, value in removed.recurrence_overrides.items()
                                           if key in original.recurrence_overrides}
    assert expression_matches(removed, draft.expression)


def test_old_document_for_a_small_coincident_series_now_opens_as_compact_text():
    original = authored('2*+0d +1d')
    original = replace(original, schedule_expression=document_expression(original))
    view = reconstruct_authoring(original)
    assert not view.structured and '+0d' in view.expression
    assert expression_matches(original, view.expression)


def test_point_and_fold_only_durations_survive_copy_materialization():
    point = source()
    point = replace(point, end=point.start)
    copies = expression_event(point, '2*+0d')
    assert all(item.start == item.end for item in visible(copies))
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    end = start.replace(fold=1)
    original = replace(source(start), end=end)
    copies = expression_event(original, '2*+0d')
    assert all(item.end.timestamp() - item.start.timestamp() == 3600 for item in visible(copies))


def test_duplicate_identity_can_use_a_future_slot_at_the_start_of_the_calendar():
    start = datetime(1, 1, 1, tzinfo=UTC)
    copied = expression_event(source(start), '2*+0d')
    assert len(copied.recurrence_overrides) == 1
    assert expression_matches(copied, '2*+0d')


@pytest.mark.parametrize('all_day', [False, True])
def test_following_scope_and_highlight_group_same_time_copies_at_their_actual_position(all_day):
    start = START.replace(hour=0) if all_day else START
    original = authored('2*+0d +1w +0d +1w', start, all_day=all_day)
    events = visible(original)
    for selected in [item for item in events if item.start.date() == (start + timedelta(weeks=1)).date()]:
        head, tail = partition_series(original, selected)
        assert len(visible(head)) == 2 and len(visible(tail)) == 3
        assert all(item.start.date() < selected.start.date() for item in visible(head))
        assert all(item.start.date() >= selected.start.date() for item in visible(tail))
        with Store(':memory:') as store:
            store.apply('Create coincident series', [original])
            affected = affected_visible(store, [selected], 'future', events)
        assert affected == {item.id for item in events if item.start.date() >= selected.start.date()}
    selection = EventSelection('future')
    selection.select(events[-1], master=original)
    selected = next(item for item in events if item.start.date() == (start + timedelta(weeks=1)).date())
    selection.select(selected, additive=True, master=original)
    assert selection.primary == selected


def test_native_rule_exceptions_keep_nominal_following_order_after_a_move():
    original = source(recurrence_rule='FREQ=DAILY;COUNT=4')
    early = occurrence(original, START + timedelta(days=1))
    moved = replace(early, start=START + timedelta(days=3), end=START + timedelta(days=3, hours=1))
    original = replace(original, recurrence_overrides={early.recurrence_id: moved.to_dict()})
    head, tail = partition_series(original, moved)
    assert len(visible(head)) == 1 and len(visible(tail)) == 3
