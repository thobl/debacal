"""Resource-local and floating clocks survive authoring and Cut metadata."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

from debacal.ics import export_ics, parse_ics
from debacal.ics_range_policy import GENERATED, expected_ranges, matches_generated, signature
from debacal.models import Event
from debacal.recurrence import occurrence
from debacal.sequence_document import document_event, document_expression, document_matches, parse_document
from debacal.timezones import resolve_timezone


IDENTIFIER = 'Synthetic/DocumentClock'
STANDARD = '\n'.join([
    'BEGIN:STANDARD', 'DTSTART:19700101T000000', 'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0100', 'END:STANDARD'])


def custom_source(*, daylight=False):
    parts = ['BEGIN:VTIMEZONE', 'TZID:' + IDENTIFIER, STANDARD]
    if daylight:
        parts.extend(['BEGIN:DAYLIGHT', 'DTSTART:20270201T020000',
                      'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0200', 'END:DAYLIGHT'])
    parts.append('END:VTIMEZONE')
    definitions = {IDENTIFIER: '\n'.join(parts)}
    zone = resolve_timezone(IDENTIFIER, definitions)
    start = datetime(2027, 1, 28, 9, tzinfo=zone)
    return Event('custom-document', 'synthetic', 'Synthetic monthly appointment', start,
                 start + timedelta(days=2), timezone_id=IDENTIFIER, timezone_definitions=definitions,
                 recurrence_rule='FREQ=MONTHLY;COUNT=3', range_policy='cut')


def floating_source():
    start = datetime(2027, 1, 28, 9, tzinfo=ZoneInfo('Europe/Berlin'))
    return Event('floating-document', 'synthetic', 'Synthetic floating appointment', start,
                 start + timedelta(days=2), floating_time=True,
                 recurrence_rule='FREQ=MONTHLY;COUNT=3', range_policy='cut')


def test_document_detached_custom_timezone_keeps_resource_rules_and_content():
    source = custom_source()
    detached = occurrence(source, source.start + timedelta(days=2))
    detached = replace(detached, title='Synthetic detached title', start=detached.start + timedelta(hours=2),
                       end=detached.end + timedelta(hours=2))
    source = replace(source, recurrence_overrides={detached.recurrence_id: detached.to_dict()})
    expression = document_expression(source)
    assert document_matches(source, expression)
    restored = document_event(source, expression)
    assert restored.recurrence_overrides == source.recurrence_overrides
    document = parse_document(expression)
    document['move'][detached.recurrence_id]['start'] = '2027-02-04T14:00:00'
    document['move'][detached.recurrence_id]['end'] = '2027-02-04T15:00:00'
    edited = document_event(source, 'schedule' + json.dumps(document))
    changed = Event.from_dict(edited.recurrence_overrides[detached.recurrence_id])
    assert changed.timezone_id == IDENTIFIER
    assert changed.timezone_definitions == source.timezone_definitions
    assert changed.start.replace(tzinfo=None) == datetime(2027, 2, 4, 14)
    assert changed.title == detached.title


def test_floating_document_keeps_naive_detached_keys_and_authored_clocks():
    source = floating_source()
    detached = occurrence(source, source.start + timedelta(days=2))
    detached = replace(detached, start=detached.start + timedelta(hours=2), end=detached.end + timedelta(hours=2))
    source = replace(source, recurrence_overrides={detached.recurrence_id: detached.to_dict()})
    expression = document_expression(source)
    document = parse_document(expression)
    values = document['move'][detached.recurrence_id]
    assert values['floating_time'] is True
    assert datetime.fromisoformat(values['start']).tzinfo is None
    with patch('debacal.timeutil.local_zone', return_value=ZoneInfo('Pacific/Honolulu')):
        restored = document_event(source, expression)
        assert document_matches(source, expression)
        item = Event.from_dict(restored.recurrence_overrides[detached.recurrence_id])
    assert restored.recurrence_overrides == source.recurrence_overrides
    assert item.start.replace(tzinfo=None) == detached.start.replace(tzinfo=None)
    assert datetime.fromisoformat(item.recurrence_id).tzinfo is None


def test_editing_floating_document_writes_naive_payloads_without_converting_to_instants():
    source = floating_source()
    detached = occurrence(source, source.start + timedelta(days=2))
    source = replace(source, recurrence_overrides={detached.recurrence_id: detached.to_dict()})
    document = parse_document(document_expression(source))
    values = document['move'][detached.recurrence_id]
    values['start'], values['end'] = '2027-02-04T14:00:00', '2027-02-04T15:00:00'
    with patch('debacal.timeutil.local_zone', return_value=ZoneInfo('Pacific/Honolulu')):
        edited = document_event(source, 'schedule' + json.dumps(document))
    payload = edited.recurrence_overrides[detached.recurrence_id]
    assert payload['floating_time'] is True
    assert payload['start'] == '2027-02-04T14:00:00'
    assert payload['end'] == '2027-02-04T15:00:00'


def test_cut_signatures_and_cached_ranges_distinguish_scoped_definitions():
    before_transition, after_transition = custom_source(), custom_source(daylight=True)
    assert before_transition.start == after_transition.start
    assert before_transition.end == after_transition.end
    assert signature(before_transition) != signature(after_transition)
    first, second = expected_ranges(before_transition), expected_ranges(after_transition)
    first_range, = first.values()
    second_range, = second.values()
    assert first_range[0].replace(tzinfo=None) == second_range[0].replace(tzinfo=None)
    assert first_range[1].timestamp() != second_range[1].timestamp()


def test_cut_signature_ignores_physical_timezone_line_endings_and_folding():
    source = custom_source()
    definition = source.timezone_definitions[IDENTIFIER]
    equivalent = definition.replace('TZID:Synthetic/DocumentClock', 'TZID:Synthetic/\n DocumentClock').replace('\n', '\r\n')
    assert signature(source) == signature(replace(source, timezone_definitions={IDENTIFIER: equivalent}))


def test_floating_cut_signature_ignores_viewer_timezone_and_matches_unqualified_generated_ranges():
    source = floating_source()
    with patch('debacal.timeutil.local_zone', return_value=ZoneInfo('Pacific/Honolulu')):
        restored = Event.from_dict(source.to_dict())
    assert signature(restored) == signature(source)
    key = '2027-02-28T09:00:00'
    expected = {key: (source.start, source.end)}
    component = {GENERATED: [({}, signature(source))],
                 'DTSTART': [({}, '20270128T090000')], 'DTEND': [({}, '20270130T090000')]}
    naive = {'DTSTART': [source.start.replace(tzinfo=None)], 'DTEND': [source.end.replace(tzinfo=None)]}
    decode = lambda _component, name, _zone: naive[name]
    assert matches_generated(component, source, key, expected, decode=decode, tz=ZoneInfo('Pacific/Honolulu'))
    naive['DTSTART'] = [source.start]
    assert matches_generated(component, source, key, expected, decode=decode, tz=ZoneInfo('Europe/Berlin'))
    component['DTSTART'] = [({'TZID': 'Europe/Berlin'}, '20270128T090000')]
    assert not matches_generated(component, source, key, expected, decode=decode, tz=ZoneInfo('Europe/Berlin'))


def test_floating_cut_roundtrip_keeps_symbolic_policy_in_a_different_viewer_zone():
    source = floating_source()
    exported = export_ics([source])
    assert GENERATED.encode() in exported
    with patch('debacal.timeutil.local_zone', return_value=ZoneInfo('Pacific/Honolulu')):
        restored, = parse_ics(exported, source.calendar_id)
    assert restored.floating_time
    assert restored.range_policy == 'cut'
    assert not restored.recurrence_overrides
