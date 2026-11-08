"""Recurrence controls retain real calendar schedules and precise exceptions."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import (
    RegularSchedule, ScheduleDraft, count_to_until, detect_regular_schedule, until_to_count,
)
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.recurrence import (
    RecurrenceError, base_schedule_starts, expand_event, occurrence,
    occurrence_id, recurrence_key, schedule_fields, updated_series,
)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def master(**values):
    defaults = dict(id='master', calendar_id='local', title='Original', start=START,
                    end=START + timedelta(minutes=45), recurrence_rule='FREQ=DAILY;COUNT=6')
    return Event(**(defaults | values))


def instants(values):
    return [value.astimezone(timezone.utc) for value in values]


def visible(event):
    return expand_event(event, START - timedelta(days=60), START + timedelta(days=100))


@pytest.mark.parametrize('kind,interval,count,until', [
    ('DAILY', 2, 5, date(2026, 11, 12)),
    ('WEEKLY', 3, 4, date(2027, 1, 6)),
    ('MONTHLY', 1, 6, date(2027, 4, 4)),
    ('YEARLY', 2, 3, date(2030, 11, 4)),
])
def test_count_and_inclusive_date_are_exact_inverses(kind, interval, count, until):
    assert count_to_until(START, kind, interval, count) == until
    assert until_to_count(START, kind, interval, until) == count


def test_month_and_leap_year_rules_skip_missing_dates_instead_of_clipping():
    january = START.replace(year=2027, month=1, day=31)
    assert count_to_until(january, 'MONTHLY', 1, 3) == date(2027, 5, 31)
    assert until_to_count(january, 'MONTHLY', 1, date(2027, 4, 30)) == 2
    leap = START.replace(year=2028, month=2, day=29)
    assert count_to_until(leap, 'YEARLY', 1, 3) == date(2036, 2, 29)
    assert until_to_count(leap, 'YEARLY', 1, date(2035, 12, 31)) == 2


def test_large_common_counts_use_constant_work_and_supported_years_are_checked():
    with patch('debacal.event_schedule.rrulestr', side_effect=AssertionError('should use date arithmetic')):
        end = count_to_until(START, 'DAILY', 1, 100000)
        assert until_to_count(START, 'DAILY', 1, end) == 100000
        assert count_to_until(START, 'WEEKLY', 1, 10000) == START.date() + timedelta(weeks=9999)
    with pytest.raises(RecurrenceError, match='calendar years'):
        count_to_until(START, 'YEARLY', 1, 100000)
    with pytest.raises(RecurrenceError, match='exceeds'):
        until_to_count(START, 'DAILY', 1, START.date() + timedelta(days=100001))


def test_regular_controls_keep_multi_weekdays_and_emit_date_until_for_all_day():
    regular = RegularSchedule.from_rule('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=5', START)
    assert regular is not None and regular.weekdays == (0, 2)
    assert regular.until == count_to_until(START, 'WEEKLY', 2, 5, (0, 2))
    all_day = RegularSchedule('DAILY', count=4, until=date(2026, 11, 7), ending='until')
    assert all_day.rule(START.replace(hour=0), all_day=True).endswith('UNTIL=20261107')
    timed = all_day.rule(START)
    assert timed.endswith('UNTIL=20261107T225959Z')


def test_unsupported_constraints_remain_advanced_and_source_is_not_rewritten():
    source = master(recurrence_rule='FREQ=MONTHLY;BYDAY=1WE;COUNT=8',
                    recurrence_dates=((START + timedelta(days=3)).isoformat(),),
                    excluded_dates=((START + timedelta(days=28)).isoformat(),))
    draft = ScheduleDraft.from_event(source)
    assert draft.kind == 'advanced' and not draft.dirty
    fields = draft.fields(source.start)
    for name in ('recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides', 'timezone_id'):
        assert fields[name] == getattr(source, name)


def test_authored_regular_literal_survives_classification_and_plain_edits():
    literal = '  4*(+2)  '
    starts = expand_sequence(literal, START)
    source = master(**schedule_fields(starts), schedule_expression=literal,
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    draft = ScheduleDraft.from_event(source)
    assert draft.kind == 'WEEKLY' and draft.interval == 2 and draft.count == 5
    assert draft.expression == literal and not draft.configuration_changed
    fields = draft.fields(source.start)
    assert fields['schedule_expression'] == literal and fields['recurrence_rule'] == source.recurrence_rule
    draft.set_expression(' 3*+1d ', START)
    assert draft.kind == 'DAILY' and draft.count == 4 and draft.expression == ' 3*+1d '
    assert draft.fields(START)['schedule_expression'] == ' 3*+1d '


def test_out_of_order_authored_dates_keep_their_literal_on_load():
    literal = ' +2  1.1.2025 +1 '
    starts = expand_sequence(literal, START)
    source = master(**schedule_fields(starts), schedule_expression=literal,
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    draft = ScheduleDraft.from_event(source)
    assert draft.expression == literal and not draft.configuration_changed
    assert draft.fields(START)['schedule_expression'] == literal


def test_loaded_custom_primary_move_shifts_concrete_dates_and_rewrites_absolute_text_once():
    literal = '10:30-13. +1'
    starts = expand_sequence(literal, START)
    source = master(**schedule_fields(starts), schedule_expression=literal,
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    draft = ScheduleDraft.from_event(source)
    shift = timedelta(days=1, hours=2)
    moved_start, moved_end = source.start + shift, source.end + shift
    expected = [value + shift for value in starts]
    assert instants(draft.starts(moved_start)) == instants(expected)
    fields = draft.fields(moved_start)
    edited = replace(source, start=moved_start, end=moved_end, **fields)
    projected = updated_series(source, source, edited)
    assert instants(base_schedule_starts(projected)) == instants(expected)
    assert instants(expand_sequence(projected.schedule_expression, projected.start)) == instants(expected)


def test_new_custom_authoring_uses_explicit_display_zone_and_keeps_untouched_timezone():
    utc_start = datetime(2026, 11, 4, 23, tzinfo=timezone.utc)
    source = master(start=utc_start, end=utc_start + timedelta(hours=1), recurrence_rule='')
    draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    draft.set_expression('+1d 15:00', utc_start)
    values = draft.starts(utc_start)
    assert values[0].date() == date(2026, 11, 5) and values[0].hour == 0
    assert all(value.tzinfo == ZONE for value in values)
    assert draft.fields(utc_start)['timezone_id'] == ZONE.key
    loaded = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    assert loaded.fields(utc_start)['timezone_id'] == source.timezone_id


def test_graphical_additions_to_utc_source_use_the_clicked_local_date_and_clock():
    utc_start = datetime(2026, 11, 4, 23, tzinfo=timezone.utc)
    source = master(start=utc_start, end=utc_start + timedelta(hours=1), recurrence_rule='')
    draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    changed = draft.toggle_date(source, date(2026, 11, 7))
    starts = base_schedule_starts(changed)
    assert [(value.astimezone(ZONE).date(), value.astimezone(ZONE).hour) for value in starts] == [
        (date(2026, 11, 5), 0), (date(2026, 11, 7), 0)]
    assert changed.timezone_id == ZONE.key
    assert instants(expand_sequence(changed.schedule_expression, utc_start.astimezone(ZONE))) == instants(starts)


def test_floating_all_day_dates_do_not_shift_when_display_timezone_differs():
    distant = ZoneInfo('Pacific/Auckland')
    initial = datetime(2026, 11, 4, 0, tzinfo=distant)
    source = master(start=initial, end=initial + timedelta(days=1), all_day=True,
                    recurrence_rule='', recurrence_dates=('2026-11-06',))
    draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    values = draft.starts(datetime(2026, 11, 4, tzinfo=ZONE), all_day=True)
    assert [value.date() for value in values] == [date(2026, 11, 4), date(2026, 11, 6)]
    assert draft.fields(initial, all_day=True)['recurrence_dates'] == ('2026-11-06',)


def test_graphical_dates_classify_regular_then_custom_and_can_return_to_regular():
    source = master(recurrence_rule='')
    draft = ScheduleDraft.from_event(source)
    current = source
    for offset in range(1, 5):
        current = draft.toggle_date(current, START.date() + timedelta(days=offset))
    assert draft.kind == 'DAILY' and draft.count == 5
    assert current.recurrence_rule == 'FREQ=DAILY;INTERVAL=1;COUNT=5'
    current = draft.toggle_date(current, START.date() + timedelta(days=2))
    assert draft.kind == 'custom' and len(visible(current)) == 4
    assert not any(event.start.date() == START.date() + timedelta(days=2) for event in visible(current))
    current = draft.toggle_date(current, START.date() + timedelta(days=2))
    assert draft.kind == 'DAILY' and draft.count == 5 and len(visible(current)) == 5


def test_primary_remains_editable_after_graphical_schedule_creation():
    draft = ScheduleDraft.from_event()
    source = master(recurrence_rule='')
    current = draft.toggle_date(source, START.date() + timedelta(days=2))
    current = draft.toggle_date(current, START.date() + timedelta(days=5))
    shift = timedelta(days=2, hours=1)
    fields = draft.fields(START + shift, current.end + shift)
    changed = replace(current, start=START + shift, end=current.end + shift, **fields)
    assert instants(base_schedule_starts(changed)) == instants([value + shift for value in base_schedule_starts(current)])
    assert instants(expand_sequence(changed.schedule_expression, changed.start)) == instants(base_schedule_starts(changed))


def test_unbounded_calendar_edit_keeps_rule_and_uses_standard_exclusions_and_rdates():
    source = master(recurrence_rule='FREQ=WEEKLY;BYDAY=WE')
    draft = ScheduleDraft.from_event(source)
    removed = draft.toggle_date(source, (START + timedelta(weeks=2)).date())
    assert removed.recurrence_rule == source.recurrence_rule
    assert recurrence_key(START + timedelta(weeks=2)) in removed.excluded_dates
    restored = draft.toggle_date(removed, (START + timedelta(weeks=2)).date())
    assert restored.recurrence_rule == source.recurrence_rule and not restored.excluded_dates
    added_start = START + timedelta(days=2, hours=3)
    added = draft.add_occurrence(restored, added_start)
    assert added.recurrence_rule == source.recurrence_rule
    assert recurrence_key(added_start) in added.recurrence_dates
    moved = replace(added, start=added.start + timedelta(days=1), end=added.end + timedelta(days=1),
                    **draft.fields(added.start + timedelta(days=1)))
    assert recurrence_key(added_start + timedelta(days=1)) in moved.recurrence_dates
    assert 'BYDAY=TH' in moved.recurrence_rule


def test_graph_edits_keep_primary_exclusion_and_detached_exception_identity():
    source = master(excluded_dates=(recurrence_key(START),))
    draft = ScheduleDraft.from_event(source)
    changed = draft.toggle_date(source, START.date() + timedelta(days=3))
    assert recurrence_key(START) in changed.excluded_dates
    assert not any(event.start == START for event in visible(changed))
    nominal = START + timedelta(days=2)
    detached = replace(occurrence(source, nominal), start=nominal + timedelta(hours=2), end=nominal + timedelta(hours=3))
    source = replace(source, recurrence_overrides={recurrence_key(nominal): detached.to_dict()})
    draft = ScheduleDraft.from_event(source)
    changed = draft.toggle_date(source, START.date() + timedelta(days=4))
    assert changed.recurrence_rule == source.recurrence_rule
    assert changed.recurrence_overrides == source.recurrence_overrides
    assert recurrence_key(START + timedelta(days=4)) in changed.excluded_dates


def test_precise_and_irregular_clocks_are_not_misclassified_as_calendar_frequency():
    assert detect_regular_schedule([START, START + timedelta(hours=2)]) is None
    assert detect_regular_schedule([START, START + timedelta(days=2), START + timedelta(days=5)]) is None
    precise = START.replace(microsecond=123)
    assert detect_regular_schedule([precise, precise + timedelta(days=1)]) is None


def test_explicit_replacement_keeps_new_custom_duration_and_canonical_resource_identity():
    source = master()
    start = START + timedelta(days=9, hours=2)
    draft = ScheduleDraft.from_event(source)
    edited = draft.add_occurrence(source, start, start + timedelta(hours=2))
    projected = updated_series(source, source, edited, replace_schedule=True)
    key = recurrence_key(start)
    assert key in projected.recurrence_overrides
    override = Event.from_dict(projected.recurrence_overrides[key])
    assert override.start == start and override.end == start + timedelta(hours=2)
    assert override.id == occurrence_id(source.id, key)
    assert override.calendar_id == source.calendar_id and override.ical_uid == source.ical_uid


def test_replacement_projects_unchanged_exceptions_but_preserves_changed_ranges():
    source = master()
    first, second = START + timedelta(days=1), START + timedelta(days=2)
    old_first = replace(occurrence(source, first), start=first + timedelta(hours=2), end=first + timedelta(hours=3))
    old_second = replace(occurrence(source, second), end=second + timedelta(hours=2))
    source = replace(source, recurrence_overrides={recurrence_key(first): old_first.to_dict(), recurrence_key(second): old_second.to_dict()})
    changed_second = replace(old_second, end=second + timedelta(hours=4))
    edited = replace(source, title='Shared title', end=source.start + timedelta(minutes=90),
                     recurrence_overrides=source.recurrence_overrides | {recurrence_key(second): changed_second.to_dict()})
    projected = updated_series(source, source, edited, replace_schedule=True)
    kept = Event.from_dict(projected.recurrence_overrides[recurrence_key(first)])
    changed = Event.from_dict(projected.recurrence_overrides[recurrence_key(second)])
    assert kept.end - kept.start == timedelta(minutes=90) and kept.title == 'Shared title'
    assert changed.end == changed_second.end
    removed = replace(edited, excluded_dates=(recurrence_key(second),))
    projected = updated_series(source, source, removed, replace_schedule=True)
    assert recurrence_key(second) not in projected.recurrence_overrides


def test_removing_last_precise_date_never_reloads_the_unchanged_blank_expression():
    start = START.replace(second=30, microsecond=123456)
    extra = (start + timedelta(days=1)).replace(second=59, microsecond=234567)
    source = master(start=start, end=start + timedelta(minutes=45), recurrence_rule='',
                    recurrence_dates=(extra.isoformat(),))
    draft = ScheduleDraft.from_event(source)
    assert draft.expression == ''
    changed = draft.toggle_date(source, extra.date())
    assert draft.starts(start) == [start]
    assert not changed.recurrence_dates
    assert changed.start == start and changed.end == source.end


def test_unrelated_graphical_edit_retains_exdate_and_a_skipped_date_can_be_restored():
    source = master()
    skipped = START + timedelta(days=2)
    source = replace(source, excluded_dates=(recurrence_key(skipped),))
    draft = ScheduleDraft.from_event(source)
    changed = draft.toggle_date(source, (START + timedelta(days=9)).date())
    assert changed.excluded_dates == source.excluded_dates
    assert skipped not in [value.start for value in visible(changed)]
    restored = draft.toggle_date(changed, skipped.date())
    assert not restored.excluded_dates
    assert skipped in [value.start for value in visible(restored)]


def test_calendar_toggle_uses_display_date_but_retains_authoring_clock_across_dst():
    primary = datetime(2027, 3, 25, 1, 30, tzinfo=ZONE)
    source = master(start=primary, end=primary + timedelta(hours=1), recurrence_rule='')
    draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
    changed = draft.toggle_date(source, date(2027, 3, 29), display_zone=timezone.utc)
    added = datetime(2027, 3, 30, 1, 30, tzinfo=ZONE)
    assert base_schedule_starts(changed) == [primary, added]
    restored = draft.toggle_date(changed, date(2027, 3, 29), display_zone=timezone.utc)
    assert [value.start for value in expand_event(restored, primary, primary + timedelta(days=10))] == [primary]
    initial = draft.toggle_date(restored, date(2027, 3, 25), display_zone=timezone.utc)
    assert [value.start for value in expand_event(initial, primary, primary + timedelta(days=10))] == [primary]


@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=6', 'FREQ=DAILY'])
def test_edit_occurrence_keeps_its_nominal_identity_and_custom_duration(rule):
    source = master(recurrence_rule=rule)
    nominal = START + timedelta(days=2)
    selected = occurrence(source, nominal)
    moved = nominal + timedelta(days=1, hours=2)
    draft = ScheduleDraft.from_event(source)
    changed = draft.edit_occurrence(source, selected, moved, moved + timedelta(hours=2))
    key = recurrence_key(nominal)
    assert changed.recurrence_rule == source.recurrence_rule
    assert not changed.recurrence_dates and not changed.excluded_dates
    assert set(changed.recurrence_overrides) == {key}
    projected = updated_series(source, source, changed, replace_schedule=True)
    visible_override = next(value for value in visible(projected) if value.recurrence_id == key)
    assert visible_override.start == moved and visible_override.end == moved + timedelta(hours=2)
    assert visible_override.id == selected.id
    changed_again = draft.edit_occurrence(projected, visible_override, moved + timedelta(hours=1), moved + timedelta(hours=4))
    assert set(changed_again.recurrence_overrides) == {key}
    assert not changed_again.excluded_dates and not changed_again.recurrence_dates


def test_primary_move_migrates_legacy_authoring_metadata_without_changing_dates():
    source = master(recurrence_rule='FREQ=WEEKLY;INTERVAL=2;COUNT=3',
                    schedule_expression='3(We X)', expression_anchor='2026-11-04', schedule_syntax=1)
    draft = ScheduleDraft.from_event(source)
    delta = timedelta(days=1)
    changed = replace(source, start=source.start + delta, end=source.end + delta,
                      **draft.fields(source.start + delta, source.end + delta))
    assert changed.schedule_syntax == 2 and 'We' not in changed.schedule_expression
    assert instants(expand_sequence(changed.schedule_expression, changed.start)) == instants(base_schedule_starts(changed))


def test_pinned_primary_click_preserves_authored_literal_and_clean_state():
    text = '  4*(+2)  '
    source = master(**schedule_fields(expand_sequence(text, START)), schedule_expression=text,
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    draft = ScheduleDraft.from_event(source)
    changed = draft.toggle_date(source, source.start.date())
    assert changed is source and draft.expression == text and not draft.configuration_changed


def test_custom_occurrence_range_keeps_literal_and_receives_later_common_field_changes():
    text = '  4*(+2)  '
    source = master(**schedule_fields(expand_sequence(text, START)), schedule_expression=text,
                    expression_anchor=START.date().isoformat(), schedule_syntax=2)
    selected = visible(source)[1]
    draft = ScheduleDraft.from_event(source)
    changed = draft.edit_occurrence(source, selected, selected.start + timedelta(hours=2),
                                    selected.end + timedelta(hours=3))
    assert changed.schedule_expression == text
    assert changed.recurrence_rule == source.recurrence_rule
    edited = replace(changed, title='New shared title', location='New room')
    projected = updated_series(source, source, edited, replace_schedule=True)
    detached = next(value for value in visible(projected) if value.recurrence_id == selected.recurrence_id)
    assert detached.title == edited.title and detached.location == edited.location
    assert detached.start == selected.start + timedelta(hours=2)
    assert detached.end == selected.end + timedelta(hours=3)


def test_common_title_edit_preserves_base_and_detached_durations_until_explicitly_changed():
    source = master()
    nominal = START + timedelta(days=2)
    exception = replace(occurrence(source, nominal), start=nominal + timedelta(hours=2),
                        end=nominal + timedelta(hours=5))
    source = replace(source, recurrence_overrides={recurrence_key(nominal): exception.to_dict()})
    edited = replace(source, title='Shared title')
    for absolute in (False, True):
        projected = updated_series(source, source, edited, replace_schedule=absolute)
        assert projected.end - projected.start == timedelta(minutes=45)
        assert Event.from_dict(projected.recurrence_overrides[recurrence_key(nominal)]).end == exception.end
    changed = updated_series(source, source, replace(edited, end=START + timedelta(minutes=90)))
    detached = Event.from_dict(changed.recurrence_overrides[recurrence_key(nominal)])
    assert detached.end - detached.start == timedelta(minutes=90)


def test_bound_detached_reference_maps_graph_edits_and_later_regular_controls_once():
    source = master(start=START + timedelta(days=2), end=START + timedelta(days=2, minutes=45),
                    recurrence_rule='FREQ=DAILY;COUNT=3')
    selected = occurrence(source, source.start)
    selected = replace(selected, start=selected.start + timedelta(days=10), end=selected.end + timedelta(days=10, hours=2))
    source = replace(source, recurrence_overrides={selected.recurrence_id: selected.to_dict()})
    draft = ScheduleDraft.from_event(source)
    draft.bind_primary_reference(selected.start, selected.end)
    assert draft.schedule_anchor(selected.start) == source.start
    changed = draft.toggle_date(source, (START + timedelta(days=20)).date(),
                                reference_start=selected.start, reference_end=selected.end)
    assert draft.primary_range(selected.start, selected.end) == (source.start, source.end)
    ui_start, ui_end = selected.start + timedelta(days=1), selected.end + timedelta(days=1)
    mapped_start, mapped_end = draft.primary_range(ui_start, ui_end)
    changed = replace(changed, start=mapped_start, end=mapped_end, **draft.fields(ui_start, ui_end))
    assert changed.start == source.start + timedelta(days=1)
    changed = draft.toggle_date(changed, (START + timedelta(days=22)).date(),
                                reference_start=ui_start, reference_end=ui_end)
    assert draft.primary_range(ui_start, ui_end) == (changed.start, changed.end)
    draft.set_regular('DAILY', count=4)
    assert draft.primary_range(ui_start, ui_end) == (changed.start, changed.end)
    assert draft.fields(ui_start, ui_end)['recurrence_overrides'] == changed.recurrence_overrides
