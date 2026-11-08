"""Actual-time authoring preserves detached standard occurrence identities."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from random import Random
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import expand_sequence, reconstruct_sequence
from debacal.recurrence import (
    RecurrenceError, expand_event, occurrence, recurrence_key, recurrence_set,
)
from debacal.schedule_authoring import (
    displayed_starts, expression_event, expression_matches, reconstruct_authoring,
)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def source(rule='FREQ=WEEKLY'):
    return Event('series', 'local', 'Meeting', START, START + timedelta(hours=1),
                 recurrence_rule=rule, timezone_id='Europe/Berlin', ical_uid='actual-sequence')


def instances(event):
    return expand_event(event, START - timedelta(days=60), START + timedelta(days=740))


def starts(event):
    return [item.start.astimezone(timezone.utc) for item in instances(event)]


def assert_actual_definition(event):
    assert event.schedule_expression
    assert expression_matches(event, event.schedule_expression)
    assert starts(expression_event(event, event.schedule_expression)) == starts(event)


@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=6', 'FREQ=WEEKLY'])
@pytest.mark.parametrize('delta', [timedelta(hours=2), timedelta(days=1, hours=2),
                                  timedelta(days=-40, hours=2)])
def test_graphical_time_changes_update_text_and_keep_nominal_key_and_details(rule, delta):
    master = source(rule)
    nominal = list(recurrence_set(master).xafter(START, count=3, inc=True))[2]
    selected = replace(occurrence(master, nominal), title='Individual title',
                       description='Individual notes', location='Individual room')
    key = selected.recurrence_id
    master = replace(master, recurrence_overrides={key: selected.to_dict()})
    draft = ScheduleDraft.from_event(master)
    moved = nominal + delta
    changed = draft.edit_occurrence(master, selected, moved, moved + timedelta(hours=2, minutes=5))
    assert '11:00' in changed.schedule_expression
    assert changed.recurrence_rule == master.recurrence_rule
    assert changed.recurrence_dates == master.recurrence_dates
    assert changed.excluded_dates == master.excluded_dates
    assert set(changed.recurrence_overrides) == {key}
    assert_actual_definition(changed)
    item = next(item for item in instances(changed) if item.recurrence_id == key)
    assert (item.start, item.end) == (moved, moved + timedelta(hours=2, minutes=5))
    assert (item.title, item.description, item.location) == ('Individual title', 'Individual notes', 'Individual room')
    # Both no-op parsing and an unrelated real text edit retain the complete
    # detached payload rather than replacing it with an anonymous RDATE.
    before = deepcopy(changed.recurrence_overrides)
    draft.set_expression(' ' + changed.schedule_expression + ' ', START)
    reread = replace(changed, **draft.fields(START, master.end))
    assert reread.recurrence_overrides == before
    extra = recurrence_key(START + timedelta(days=80, hours=5))
    requested = replace(reread, recurrence_dates=tuple(set(reread.recurrence_dates) | {extra}))
    new_text = reconstruct_authoring(requested, prefer_literal=False).expression
    draft.set_expression(new_text, START)
    edited = replace(reread, **draft.fields(START, master.end))
    assert edited.recurrence_overrides == before
    assert_actual_definition(edited)
    assert starts(edited) == starts(requested)
    data = export_ics([edited])
    assert b'RECURRENCE-ID' in data
    loaded = parse_ics(data, 'local')[0]
    assert set(loaded.recurrence_overrides) == {key}
    assert starts(loaded) == starts(edited)
    loaded_item = next(item for item in instances(loaded) if item.recurrence_id == key)
    assert (loaded_item.start, loaded_item.end, loaded_item.title, loaded_item.description, loaded_item.location) == (
        item.start, item.end, item.title, item.description, item.location)
    assert_actual_definition(loaded)
    # Editing the already-moved event again retains the same original slot.
    draft = ScheduleDraft.from_event(loaded)
    changed_again = draft.edit_occurrence(loaded, loaded_item, moved + timedelta(minutes=35),
                                           moved + timedelta(hours=3))
    assert '11:35' in changed_again.schedule_expression
    assert set(changed_again.recurrence_overrides) == {key}
    assert_actual_definition(changed_again)


def test_multiple_moved_overrides_before_and_after_primary_keep_distinct_identities():
    master = source()
    draft = ScheduleDraft.from_event(master)
    for week, day, hour in [(1, -2, 11), (2, 18, 14), (4, 27, 15)]:
        selected = occurrence(master, START + timedelta(weeks=week))
        start = START.replace(hour=hour) + timedelta(days=day)
        master = draft.edit_occurrence(master, selected, start, start + timedelta(minutes=40 + week))
    before = deepcopy(master.recurrence_overrides)
    assert len(before) == 3
    assert_actual_definition(master)
    requested = replace(master, recurrence_dates=(recurrence_key(START + timedelta(days=19, hours=3)),))
    text = reconstruct_authoring(requested, prefer_literal=False).expression
    draft.set_expression(text, START)
    edited = replace(master, **draft.fields(START, master.end))
    assert edited.recurrence_overrides == before
    assert starts(edited) == starts(requested)
    assert_actual_definition(edited)


def test_duration_only_graphical_edit_keeps_literal_and_whitespace_parsing_preserves_payload():
    text = '  4*(+2)  '
    master = replace(expression_event(source(), text), schedule_expression=text,
                     expression_anchor=START.date().isoformat(), schedule_syntax=2)
    selected = instances(master)[1]
    draft = ScheduleDraft.from_event(master)
    changed = draft.edit_occurrence(master, selected, selected.start, selected.end + timedelta(hours=2))
    assert changed.schedule_expression == text
    draft.set_expression(text + ' ', START)
    edited = replace(changed, **draft.fields(START, master.end))
    assert edited.recurrence_overrides == changed.recurrence_overrides
    assert displayed_starts(edited) == tuple([START, *[item.start for item in instances(edited) if item.start != START]])


def test_duplicate_displayed_starts_stay_canonical_with_an_explanation():
    master = source('FREQ=DAILY;COUNT=4')
    selected = instances(master)[1]
    draft = ScheduleDraft.from_event(master)
    # Moving onto the next independent occurrence must not collapse the pair.
    start = selected.start + timedelta(days=1)
    changed = draft.edit_occurrence(master, selected, start, start + timedelta(hours=2))
    assert draft.kind == 'advanced' and 'same time' in draft.authoring_error
    assert len(instances(changed)) == 4
    assert changed.recurrence_overrides
    before = deepcopy(draft.__dict__)
    with pytest.raises(RecurrenceError, match='same time'):
        draft.use_sequence(changed)
    assert draft.__dict__ == before
    reopened = ScheduleDraft.from_event(changed)
    assert reopened.kind == 'advanced' and 'same time' in reopened.authoring_error
    assert reopened.fields(changed.start)['recurrence_overrides'] == changed.recurrence_overrides


def test_irregular_dates_prefer_absolute_entries_and_patterns_stay_compact():
    values = [START, START + timedelta(days=8), START + timedelta(days=11)]
    text = reconstruct_sequence(values)
    assert text == '12. 15.'
    assert expand_sequence(text, START) == values
    weekly = [START + timedelta(weeks=index) for index in range(8)]
    assert reconstruct_sequence(weekly) == '7*+1'
    motif = expand_sequence('4*(+8d +3d)', START)
    assert reconstruct_sequence(motif) == '4*(+8d +3d)'


def test_seeded_actual_time_edits_and_text_reentry_are_lossless():
    rng = Random(20261108)
    for rule in ('FREQ=DAILY;COUNT=12', 'FREQ=WEEKLY'):
        for _ in range(12):
            master = source(rule)
            nominal = list(recurrence_set(master).xafter(START, count=9, inc=True))
            draft = ScheduleDraft.from_event(master)
            for index in rng.sample(range(1, 9), 3):
                selected = occurrence(master, nominal[index])
                start = selected.start.replace(hour=rng.choice((10, 11, 13, 14)), minute=rng.choice((5, 20, 35, 50)))
                master = draft.edit_occurrence(master, selected, start, start + timedelta(minutes=rng.randrange(20, 180)))
            assert_actual_definition(master)
            before = deepcopy(master.recurrence_overrides)
            draft.set_expression(' ' + master.schedule_expression, START)
            reread = replace(master, **draft.fields(START, master.end))
            assert reread.recurrence_overrides == before
            assert starts(reread) == starts(master)
