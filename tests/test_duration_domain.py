"""Exact DTEND and nominal DURATION stay distinct through domain operations."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from debacal.event_clipboard import EventClipboard
from debacal.event_duration import add_duration, add_elapsed, elapsed_duration, end_for
from debacal.event_schedule import ScheduleDraft
from debacal.models import Event
from debacal.range_policies import occupied_span, range_policy_options, static_range_policy_fits
from debacal.recurrence import expand_event, occurrence, updated_series
from debacal.sequence_document import document_event, document_expression, document_matches
from debacal.schedule_authoring import expression_event
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')


def master(start, *, spec='', hours=24, rule='FREQ=DAILY;COUNT=4', **changes):
    end = add_duration(start, spec) if spec else add_elapsed(start, timedelta(hours=hours))
    return Event('duration', 'local', 'Duration', start, end, recurrence_rule=rule,
                  duration_spec=spec, **changes)


@pytest.mark.parametrize('start,ending,fold', [
    (datetime(2027, 3, 27, 1, 30, tzinfo=ZONE), 4, 0),
    (datetime(2027, 10, 30, 1, 30, tzinfo=ZONE), 2, 1),
])
def test_dtend_recurring_duration_is_exact_across_both_clock_changes(start, ending, fold):
    source = master(start, hours=2)
    values = expand_event(source, start, start + timedelta(days=5))
    assert len(values) == 4
    assert all(elapsed_duration(item.start, item.end) == timedelta(hours=2) for item in values)
    assert values[1].end.hour == ending and values[1].end.fold == fold


def test_moving_detached_exact_duration_across_gap_keeps_its_elapsed_hour():
    start = datetime(2027, 3, 25, 1, 30, tzinfo=ZONE)
    source = master(start, hours=.5, rule='FREQ=WEEKLY;COUNT=2')
    scheduled = start + timedelta(weeks=1)
    override_start = datetime(2027, 3, 27, 1, 30, tzinfo=ZONE)
    detached = replace(occurrence(source, scheduled), start=override_start,
                       end=override_start + timedelta(hours=1))
    source = replace(source, recurrence_overrides={detached.recurrence_id: detached.to_dict()})
    moved = updated_series(source, source,
        replace(source, start=start + timedelta(days=1), end=source.end + timedelta(days=1)))
    actual, = [Event.from_dict(payload) for payload in moved.recurrence_overrides.values()]
    assert actual.start == datetime(2027, 3, 28, 1, 30, tzinfo=ZONE)
    assert actual.end == datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    assert elapsed_duration(actual.start, actual.end) == timedelta(hours=1)


@pytest.mark.parametrize('start,hours', [
    (datetime(2027, 3, 27, 9, tzinfo=ZONE), 23),
    (datetime(2027, 10, 30, 9, tzinfo=ZONE), 25),
])
def test_nominal_one_day_differs_from_exact_twenty_four_hours(start, hours):
    nominal = master(start, spec='P1D')
    exact = master(start, spec='PT24H')
    assert exact.duration_spec == '' and nominal.duration_spec == 'P1D'
    nominal_values = expand_event(nominal, start, start + timedelta(days=5))
    exact_values = expand_event(exact, start, start + timedelta(days=5))
    assert elapsed_duration(nominal_values[0].start, nominal_values[0].end) == timedelta(hours=hours)
    assert all(value.end.hour == 9 for value in nominal_values)
    assert all(elapsed_duration(value.start, value.end) == timedelta(hours=24) for value in exact_values)


def test_exact_ongoing_event_is_found_in_its_extra_wall_hour_after_spring_change():
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    source = master(start)
    begin = datetime(2027, 3, 28, 9, 30, tzinfo=ZONE)
    values = expand_event(source, begin, begin + timedelta(minutes=10))
    assert [value.start.date() for value in values] == [datetime(2027, 3, 27).date(), begin.date()]
    assert values[0].end == datetime(2027, 3, 28, 10, tzinfo=ZONE)


def test_nominal_ongoing_event_is_found_when_its_end_normalizes_through_gap():
    start = datetime(2027, 3, 26, 2, 30, tzinfo=ZONE)
    source = master(start, spec='P1D')
    begin = datetime(2027, 3, 28, 3, 15, tzinfo=ZONE)
    values = expand_event(source, begin, begin + timedelta(minutes=5))
    assert len(values) == 1
    assert values[0].start == datetime(2027, 3, 27, 2, 30, tzinfo=ZONE)
    assert values[0].end == datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)


@pytest.mark.parametrize('spec,ending_hour', [('', 10), ('P1D', 9)])
@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_copy_move_and_split_preserve_semantic_duration(spec, ending_hour, scope):
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    source = master(start, spec=spec)
    reference = occurrence(source, start + timedelta(days=1)) if scope == 'future' else occurrence(source, start)
    target = datetime(2027, 3, 27, 9, tzinfo=ZONE)
    end = end_for(reference, target)
    copied, = EventClipboard.capture([source], reference, scope).instantiate('group', target, 'local')
    copied_values = expand_event(copied, target, target + timedelta(days=5))
    assert copied_values[0].end.hour == ending_hour
    assert copied.duration_spec == spec
    with Store(':memory:') as store:
        store.apply('Create', [source])
        operation = store.move_selection([reference], scope, reference, target, end)
        values = store.query_occurrences(target, target + timedelta(days=5))
        moved = next(value for value in values if value.start.timestamp() == target.timestamp())
        assert moved.end.hour == ending_hour
        assert moved.duration_spec == spec
        if scope == 'future':
            assert len(store.list_events()) == 2
            assert occurrence(store.get_event(source.id), start).end == source.end
        store.undo(operation)
        restored = store.get_event(source.id)
        assert restored.start == source.start and restored.end == source.end
        assert restored.duration_spec == spec


def test_explicit_resize_can_replace_nominal_with_equal_current_exact_duration():
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    source = master(start, spec='P1D')
    selected = occurrence(source, start)
    with Store(':memory:') as store:
        store.apply('Create', [source])
        operation = store.edit_selection([selected], 'group', selected, replace(source, duration_spec=''))
        assert store.get_event(source.id).duration_spec == ''
        values = store.query_occurrences(start, start + timedelta(days=5))
        assert values[2].end.hour == 10
        store.undo(operation)
        assert store.get_event(source.id).duration_spec == 'P1D'
        assert store.query_occurrences(start, start + timedelta(days=5))[2].end.hour == 9


def test_schedule_document_retains_nominal_master_and_detached_duration_intent():
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    source = master(start, spec='P1D', rule='FREQ=DAILY;BYHOUR=9,13;COUNT=4')
    selected = occurrence(source, start + timedelta(days=1))
    detached = replace(selected, duration_spec='')
    source = replace(source, recurrence_overrides={selected.recurrence_id: detached.to_dict()})
    text = document_expression(source)
    assert '"duration_spec": "P1D"' in text
    restored = document_event(source, text)
    assert restored.duration_spec == 'P1D'
    assert restored.recurrence_overrides[selected.recurrence_id]['duration_spec'] == ''
    assert document_matches(source, text)
    assert not document_matches(replace(source, duration_spec=''), text)


def test_custom_occurrence_can_explicitly_clear_nominal_duration_without_changing_endpoints():
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    source = master(start, spec='P1D')
    selected = occurrence(source, start + timedelta(days=1))
    draft = ScheduleDraft.from_event(source)
    changed = draft.edit_occurrence(source, selected, selected.start, selected.end, duration_spec='')
    assert changed.recurrence_overrides[selected.recurrence_id]['duration_spec'] == ''
    assert changed.duration_spec == 'P1D'


def test_cut_uses_the_actual_exact_end_when_clock_change_crosses_the_month_boundary():
    start = datetime(2024, 1, 30, tzinfo=ZONE)
    source = master(start, hours=48, rule='FREQ=MONTHLY;COUNT=3', range_policy='cut')
    values = expand_event(source, start, datetime(2024, 5, 1, tzinfo=ZONE))
    march = next(value for value in values if value.start.month == 3)
    assert end_for(source, march.start) == datetime(2024, 4, 1, 1, tzinfo=ZONE)
    assert march.end == datetime(2024, 4, 1, tzinfo=ZONE)
    assert elapsed_duration(march.start, march.end) == timedelta(hours=47)


@pytest.mark.parametrize('policy', ['skip', 'shift'])
def test_static_month_range_policy_rejects_exact_hours_that_spill_during_clock_change(policy):
    start = datetime(2024, 1, 30, tzinfo=ZONE)
    source = master(start, hours=48, rule='')
    with pytest.raises(ValueError, match='calendar-day duration'):
        expression_event(source, f'every(1m, overflow={policy}, span=2, count=4)')
    assert not static_range_policy_fits(source, 'MONTHLY', policy=policy, span=2, count=4)
    # A bounded series before that transition is completely checked and remains valid.
    assert expression_event(source, f'every(1m, overflow={policy}, span=2, count=1)')
    assert expression_event(source, f'every(1m, overflow={policy}, span=2, until=29.3.2024)')


@pytest.mark.parametrize('policy', ['skip', 'shift', 'cut'])
def test_nominal_days_retain_calendar_month_range_policies(policy):
    start = datetime(2024, 1, 30, tzinfo=ZONE)
    source = master(start, spec='P2D', rule='')
    authored = expression_event(source, f'every(1m, overflow={policy}, span=2)')
    assert authored.duration_spec == 'P2D'
    march = next(value for value in expand_event(authored, start, datetime(2024, 5, 1, tzinfo=ZONE))
                 if value.start.month == 3)
    assert march.end == datetime(2024, 4, 1, tzinfo=ZONE)


@pytest.mark.parametrize('spec,day,hour,hours,span', [
    ('', 30, 9, 24, 2), ('P2D', 28, 9, 0, 3),
])
def test_safe_forever_near_month_end_is_not_rejected_by_a_sampling_budget(spec, day, hour, hours, span):
    source = master(datetime(2024, 1, day, hour, tzinfo=ZONE), spec=spec, hours=hours, rule='')
    assert static_range_policy_fits(source, 'MONTHLY', policy='skip', span=span)
    assert expression_event(source, f'every(1m, overflow=skip, span={span})')


def test_safety_check_reaches_historical_clock_changes_after_more_than_four_hundred_years():
    source = master(datetime(1, 1, 30, tzinfo=ZONE), hours=48, rule='')
    assert static_range_policy_fits(source, 'MONTHLY', policy='skip', span=2, until=date(1800, 12, 31))
    assert not static_range_policy_fits(source, 'MONTHLY', policy='skip', span=2, until=date(2050, 12, 31))


def test_safe_unbounded_ranges_are_not_rejected_at_the_last_supported_december():
    source = master(datetime(9998, 1, 30, 9, tzinfo=ZONE), hours=24, rule='')
    assert static_range_policy_fits(source, 'MONTHLY', policy='skip', span=2)
    terminal = master(datetime(9998, 1, 30, tzinfo=ZONE), hours=48, rule='')
    assert static_range_policy_fits(terminal, 'MONTHLY', policy='skip', span=2)
    assert not static_range_policy_fits(terminal, 'MONTHLY', policy='skip', span=2, count=14)
    nominal = master(datetime(9998, 1, 30, tzinfo=ZONE), spec='P2D', rule='')
    assert static_range_policy_fits(nominal, 'MONTHLY', policy='skip', span=2)
    assert not static_range_policy_fits(nominal, 'MONTHLY', policy='skip', span=2, count=14)
    all_day = replace(nominal, all_day=True)
    assert static_range_policy_fits(all_day, 'MONTHLY', policy='skip', span=2)
    assert not static_range_policy_fits(all_day, 'MONTHLY', policy='skip', span=2, count=14)


def test_imported_native_exact_range_and_its_canonical_document_are_not_rejected():
    start = datetime(2024, 1, 30, tzinfo=ZONE)
    source = master(start, hours=48, rule='FREQ=MONTHLY;COUNT=3')
    assert document_event(source, document_expression(source)) == source
    assert len(expand_event(source, start, datetime(2024, 5, 1, tzinfo=ZONE))) == 3


def test_repeated_midnight_counts_the_occupied_new_day_only_for_timed_events():
    zone = ZoneInfo('America/Havana')
    start = datetime(2020, 10, 30, tzinfo=zone)
    end = datetime(2020, 11, 1, tzinfo=zone, fold=1)
    assert occupied_span(start, end) == 3
    assert occupied_span(start, end, all_day=True) == 2
    assert range_policy_options(start, end, 'MONTHLY') == (True, False)
    assert range_policy_options(start, end, 'MONTHLY', all_day=True) == (True, True)


@pytest.mark.parametrize('nominal', [False, True])
def test_history_from_before_duration_field_upgrade_still_undoes_and_redoes(nominal):
    start = datetime(2027, 3, 25, 9, tzinfo=ZONE)
    raw = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:duration\r\n'
           'DTSTART;TZID=Europe/Berlin:20270325T090000\r\n'
           + ('DURATION:P1D\r\n' if nominal else 'DTEND;TZID=Europe/Berlin:20270326T090000\r\n')
           + 'SUMMARY:Duration\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    source = master(start, spec='P1D' if nominal else '', raw_ics=raw)
    changed = replace(source, title='Updated')
    with Store(':memory:') as store:
        store.apply('Create', [source])
        operation = store.apply('Rename', [changed])
        with store._transaction():
            for row in store.connection.execute('SELECT id,before_data,after_data FROM operations').fetchall():
                values = []
                for field in ('before_data', 'after_data'):
                    snapshot = json.loads(row[field])
                    for payload in snapshot.values():
                        if payload is not None:
                            payload.pop('duration_spec', None)
                    values.append(json.dumps(snapshot))
                store.connection.execute('UPDATE operations SET before_data=?,after_data=? WHERE id=?', [*values, row['id']])
        store.undo(operation)
        assert store.get_event(source.id).title == source.title
        assert store.get_event(source.id).duration_spec == source.duration_spec
        store.redo(operation)
        assert store.get_event(source.id).title == changed.title
        if nominal:
            assert Store._semantic_event(source.to_dict()) != Store._semantic_event(replace(source, duration_spec='').to_dict())
