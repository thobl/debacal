"""Every canonical schedule has an editable and identity-preserving definition."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from random import Random
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event
from debacal.recurrence import expand_event, occurrence, recurrence_key
from debacal.schedule_authoring import expression_event, expression_matches, reconstruct_authoring
from debacal.sequence_document import (
    document_event, document_expression, document_matches, is_sequence_document,
    parse_document,
)


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def source(**changes):
    event = Event('series', 'calendar', 'Prototype', START, START + timedelta(hours=1),
                  recurrence_rule='FREQ=DAILY;COUNT=6', description='Prototype notes')
    return replace(event, **changes)


def schedule_fields(event):
    return tuple(getattr(event, name) for name in (
        'recurrence_rule', 'recurrence_dates', 'excluded_dates', 'recurrence_overrides'))


def visible(event):
    return [(item.recurrence_id, item.start, item.end, item.all_day)
            for item in expand_event(event, START - timedelta(days=15), START + timedelta(days=100))]


@pytest.mark.parametrize('rule,compact', [
    ('FREQ=WEEKLY;BYDAY=MO,WE', True),
    ('FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1', False),
    ('FREQ=YEARLY;BYMONTH=1,3,9;BYMONTHDAY=3,14,28', False),
    ('FREQ=HOURLY;INTERVAL=2', False),
    ('FREQ=MINUTELY;INTERVAL=35;COUNT=8000', False),
])
def test_arbitrary_standard_rules_have_total_definitions_without_expanding_them(rule, compact):
    event = source(recurrence_rule=rule)
    view = reconstruct_authoring(event)
    assert view.expression and view.structured is not compact
    assert is_sequence_document(view.expression) is not compact
    assert expression_matches(event, view.expression)
    restored = expression_event(event, view.expression)
    if compact:
        assert 'on=MO+WE' in view.expression
        assert visible(restored) == visible(event)
    else:
        assert schedule_fields(restored) == schedule_fields(event)


@pytest.mark.parametrize('exclude_reference,move_reference', [(True, False), (False, True), (True, True)])
def test_reference_is_not_required_to_be_an_actual_occurrence(exclude_reference, move_reference):
    event = source()
    key = recurrence_key(START)
    overrides = {}
    if move_reference:
        item = replace(occurrence(event, START), start=START - timedelta(days=2),
                       end=START - timedelta(days=2) + timedelta(hours=2), title='Moved reference')
        overrides[key] = item.to_dict()
    event = replace(event, excluded_dates=(key,) if exclude_reference else (), recurrence_overrides=overrides)
    view = reconstruct_authoring(event)
    assert view.expression
    assert expression_matches(event, view.expression)
    # The canonical document always retains exact nominal IDs and payloads,
    # even where a short actual-start expression is also available.
    restored = document_event(event, document_expression(event))
    assert schedule_fields(restored) == schedule_fields(event)
    assert visible(restored) == visible(event)


def test_excluding_all_occurrences_has_the_explicit_empty_sequence():
    event = source(recurrence_rule='', excluded_dates=(recurrence_key(START),))
    view = reconstruct_authoring(event)
    assert view.expression == 'none'
    assert expression_matches(event, view.expression)
    assert visible(expression_event(event, view.expression)) == []


def test_distinct_nominal_identities_with_coincident_starts_have_compact_text():
    event = source()
    one = occurrence(event, START + timedelta(days=1))
    two = occurrence(event, START + timedelta(days=2))
    shared = START + timedelta(days=4, hours=2)
    one = replace(one, start=shared, end=shared + timedelta(hours=2), title='First', description='First notes')
    two = replace(two, start=shared, end=shared + timedelta(hours=3), title='Second', location='Second room')
    event = replace(event, recurrence_overrides={one.recurrence_id: one.to_dict(), two.recurrence_id: two.to_dict()})
    before = deepcopy(event.recurrence_overrides)
    view = reconstruct_authoring(event)
    assert not view.structured and expression_matches(event, view.expression)
    restored = expression_event(event, view.expression)
    assert restored.recurrence_overrides == before
    assert visible(restored) == visible(event)
    assert len([item for item in visible(restored) if item[1] == shared]) == 2


def test_mixed_all_day_detached_range_is_readable_and_lossless():
    event = source()
    item = occurrence(event, START + timedelta(days=2))
    day = START.replace(hour=0) + timedelta(days=3)
    item = replace(item, start=day, end=day + timedelta(days=2), all_day=True,
                   description='Keep this payload')
    event = replace(event, recurrence_overrides={item.recurrence_id: item.to_dict()})
    view = reconstruct_authoring(event)
    assert view.structured and '"all_day": true' in view.expression
    assert expression_matches(event, view.expression)
    assert schedule_fields(expression_event(event, view.expression)) == schedule_fields(event)


def test_accepted_all_day_exception_with_non_midnight_clocks_remains_exactly_representable():
    event = source()
    item = replace(occurrence(event, START + timedelta(days=1)), all_day=True)
    event = replace(event, recurrence_overrides={item.recurrence_id: item.to_dict()})
    view = reconstruct_authoring(event)
    assert view.structured and item.start.isoformat() in view.expression
    assert expression_matches(event, view.expression)
    assert schedule_fields(expression_event(event, view.expression)) == schedule_fields(event)


def test_large_finite_sets_are_not_limited_by_the_short_program_budget():
    # Irregular RDATEs prevent conversion into a simple COUNT rule. The text is
    # deliberately larger than the compact parser's work/character budgets.
    dates = tuple(recurrence_key(START + timedelta(days=index * 2, minutes=index % 29))
                  for index in range(1, 1400))
    event = source(recurrence_rule='', recurrence_dates=dates)
    view = reconstruct_authoring(event)
    assert view.structured and len(view.expression) > 8192
    assert expression_matches(event, view.expression)
    assert schedule_fields(expression_event(event, view.expression)) == schedule_fields(event)


def test_editing_one_document_range_preserves_all_detached_content_and_identity():
    event = source()
    item = occurrence(event, START + timedelta(days=1))
    item = replace(item, title='Personal title', description='Personal notes', location='Personal room')
    event = replace(event, recurrence_overrides={item.recurrence_id: item.to_dict()})
    document = parse_document(document_expression(event))
    requested = START + timedelta(days=4, hours=3)
    document['move'][item.recurrence_id]['start'] = requested.isoformat()
    document['move'][item.recurrence_id]['end'] = (requested + timedelta(minutes=35)).isoformat()
    text = 'schedule' + json.dumps(document)
    assert not document_matches(event, text)
    restored = expression_event(event, text)
    changed = Event.from_dict(restored.recurrence_overrides[item.recurrence_id])
    assert (changed.start, changed.end) == (requested, requested + timedelta(minutes=35))
    assert (changed.id, changed.recurrence_id, changed.title, changed.description, changed.location) == (
        item.id, item.recurrence_id, item.title, item.description, item.location)
    assert event.recurrence_overrides[item.recurrence_id] == item.to_dict()


def test_a_new_move_inherits_prototype_content_and_a_removed_move_restores_nominal_slot():
    event = source()
    key = recurrence_key(START + timedelta(days=2))
    requested = START + timedelta(days=5, hours=4)
    text = 'schedule' + json.dumps({'rule': event.recurrence_rule, 'move': {
        key: {'start': requested.isoformat(), 'end': (requested + timedelta(hours=2)).isoformat()}}})
    changed = document_event(event, text)
    item = Event.from_dict(changed.recurrence_overrides[key])
    assert item.description == event.description and item.start == requested
    assert item.recurrence_id == key
    restored = document_event(changed, 'schedule' + json.dumps({'rule': event.recurrence_rule}))
    assert not restored.recurrence_overrides
    assert any(row[0] == key and row[1] == START + timedelta(days=2) for row in visible(restored))


@pytest.mark.parametrize('text', [
    'schedule[]', 'schedule{"unknown": 2}', 'schedule{"rule": 3}',
    'schedule{"add": "2027-01-01"}', 'schedule{"omit": [true]}',
    'schedule{"move": []}', 'schedule{"move": {"2027-01-01": {"start": "2027-01-01"}}}',
    'schedule{"rule": "", "rule": "FREQ=DAILY"}', 'schedule{"add": [NaN]}',
    'schedule{"add": ["not a date"]}',
])
def test_invalid_documents_leave_source_untouched(text):
    event = source()
    before = event.to_dict()
    assert not expression_matches(event, text)
    with pytest.raises(ValueError):
        expression_event(event, text)
    assert event.to_dict() == before


@pytest.mark.parametrize('rule,compact', [
    ('FREQ=WEEKLY;BYDAY=MO,WE', True),
    ('FREQ=YEARLY;BYMONTH=1,3,9;BYMONTHDAY=3,14,28', False),
])
def test_existing_document_upgrades_when_compact_and_preserves_whitespace_otherwise(rule, compact):
    event = source(recurrence_rule=rule)
    literal = '  schedule {\n  "rule": "' + rule + '"\n}\n'
    event = replace(event, schedule_expression=literal, schedule_syntax=2)
    view = reconstruct_authoring(event)
    assert view.structured is not compact
    assert expression_matches(event, view.expression)
    if compact:
        assert view.expression == 'every(1w, on=MO+WE)'
    else:
        assert view.expression == literal


def test_even_a_malformed_imported_rule_keeps_an_editable_lossless_document():
    event = source(recurrence_rule='FREQ=UNRECOGNIZED;COUNT=several')
    view = reconstruct_authoring(event)
    assert view.structured
    assert expression_matches(event, view.expression)
    assert schedule_fields(expression_event(event, view.expression)) == schedule_fields(event)


def test_seeded_combinations_of_reference_exclusion_detachment_and_collisions_round_trip():
    rng = Random(20261108)
    for index in range(60):
        event = source(recurrence_rule=rng.choice((
            'FREQ=DAILY;COUNT=8', 'FREQ=WEEKLY;BYDAY=MO,WE',
            'FREQ=MONTHLY;BYDAY=TU;BYSETPOS=2', '')))
        keys = [recurrence_key(START + timedelta(days=day)) for day in range(8)]
        excluded = tuple(rng.sample(keys, rng.randrange(5)))
        overrides = {}
        for key in rng.sample(keys, rng.randrange(5)):
            item = occurrence(event, datetime.fromisoformat(key), key)
            start = START + timedelta(days=rng.randrange(-3, 6), hours=rng.choice((0, 2)))
            all_day = bool(rng.randrange(3) == 0)
            if all_day:
                start = start.replace(hour=0)
            item = replace(item, start=start,
                           end=start + (timedelta(days=1) if all_day else timedelta(minutes=rng.choice((0, 25, 90)))),
                           all_day=all_day, title=f'Exception {index} {key}',
                           description='Individual content must survive')
            overrides[key] = item.to_dict()
        event = replace(event, excluded_dates=excluded, recurrence_overrides=overrides,
                        recurrence_dates=tuple(rng.sample(keys, rng.randrange(5))))
        document = document_expression(event)
        restored = document_event(event, document)
        assert schedule_fields(restored) == schedule_fields(event)
        assert document_matches(event, document)
        view = reconstruct_authoring(event)
        assert view.expression and expression_matches(event, view.expression)
