"""Persistent preparation must preserve semantics without redoing source work."""

from array import array
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
import json
from unittest.mock import patch
import weakref
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event, copy_event
from debacal.prepared_recurrence import PreparedSeries
from debacal.prepared_storage import FORMAT_VERSION, PreparedStateError, StateReader, StateWriter
from debacal.recurrence import occurrence, recurrence_key
from debacal.recurrence_budget import BoundedRule
from debacal.timezones import (
    _cached_explicit_timezone, _prepared_definitions, _prepared_definitions_lock,
    resolve_timezone,
)


START = datetime(2010, 1, 1, 9, tzinfo=ZoneInfo('Europe/Berlin'))


def event(**changes):
    return replace(Event('source', 'local', 'Synthetic', START, START + timedelta(hours=1)), **changes)


def transported(value):
    """Exercise the same primitive JSON boundary used by the SQLite Store."""
    return json.loads(json.dumps(value))


def roundtrip(master):
    prepared = PreparedSeries(master)
    writer = StateWriter()
    source = writer.event(master)
    state = writer.series(prepared)
    reader = StateReader(transported(writer.zones), transported(writer.definitions))
    restored_master = reader.event(transported(source))
    restored = reader.series(restored_master, transported(state))
    return prepared, restored, writer, reader


@pytest.mark.parametrize('rule', [
    'FREQ=DAILY', 'FREQ=DAILY;COUNT=4', 'FREQ=DAILY;INTERVAL=3;BYDAY=MO,WE,FR;COUNT=5000',
    'FREQ=WEEKLY;BYDAY=MO,WE;COUNT=1000', 'FREQ=WEEKLY;BYMONTH=3,10;COUNT=1000',
    'FREQ=MONTHLY;BYMONTHDAY=31', 'FREQ=MONTHLY;BYDAY=1MO;COUNT=1000',
    'FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=1000',
    'FREQ=YEARLY;BYEASTER=0;COUNT=1000', 'FREQ=HOURLY;BYMINUTE=5,35;COUNT=1000000',
    'FREQ=HOURLY;BYMONTH=3;BYDAY=MO,WE;COUNT=500000',
    'FREQ=MINUTELY;INTERVAL=5;COUNT=1000',
])
def test_prepared_rule_numeric_state_preserves_arbitrary_suffix_queries(rule):
    master = event(recurrence_rule=rule)
    expected, actual, _writer, _reader = roundtrip(master)
    for year, month in [(2026, 3), (2010, 1), (2040, 10), (2019, 7)]:
        lower = datetime(year, month, 1, tzinfo=START.tzinfo)
        upper = lower + timedelta(days=8)
        assert actual.query(lower, upper) == expected.query(lower, upper)
    assert actual.rule_bounds == expected.rule_bounds
    if actual.rule is not None:
        assert str(actual.rule) == str(expected.rule)


def test_restore_does_not_parse_validate_generate_or_iterate_timezone_rules():
    identifier = 'Persistence/Scoped'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\n'
                  'BEGIN:STANDARD\nDTSTART:16010101T030000\nTZOFFSETFROM:+0200\n'
                  'TZOFFSETTO:+0100\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\nEND:STANDARD\n'
                  'BEGIN:DAYLIGHT\nDTSTART:16010101T020000\nTZOFFSETFROM:+0100\n'
                  'TZOFFSETTO:+0200\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\nEND:DAYLIGHT\nEND:VTIMEZONE\n')
    zone = resolve_timezone(identifier, {identifier: definition})
    first = datetime(2010, 1, 1, 9, tzinfo=zone)
    master = event(start=first, end=first + timedelta(hours=1), recurrence_rule='FREQ=MONTHLY;COUNT=1000',
                   timezone_id=identifier, timezone_definitions={identifier: definition})
    prepared = PreparedSeries(master)
    writer = StateWriter()
    event_state, series_state = writer.event(master), writer.series(prepared)
    reader = StateReader(transported(writer.zones), transported(writer.definitions))
    with patch.object(Event, '__post_init__', side_effect=AssertionError('Event validated on restore')), \
            patch.object(PreparedSeries, '__init__', side_effect=AssertionError('Series prepared on restore')), \
            patch('debacal.recurrence.rrulestr', side_effect=AssertionError('Rule parsed on restore')), \
            patch.object(BoundedRule, '__iter__', side_effect=AssertionError('Rule expanded on restore')):
        restored_master = reader.event(transported(event_state))
        restored = reader.series(restored_master, transported(series_state))
        restored_zone = restored.event_zone
        assert datetime(9999, 7, 1, tzinfo=restored_zone).utcoffset() == timedelta(hours=2)
        assert datetime(2026, 10, 25, 2, 30, tzinfo=restored_zone, fold=1).utcoffset() == timedelta(hours=1)
        assert datetime(2026, 10, 25, 2, 30, tzinfo=restored_zone, fold=0).utcoffset() == timedelta(hours=2)
    lower = datetime(2026, 1, 1, tzinfo=zone)
    assert restored.query(lower, lower + timedelta(days=60)) == prepared.query(lower, lower + timedelta(days=60))


def test_event_and_minimal_override_shape_roundtrip_and_transport_is_separate():
    master = event(raw_ics='Synthetic retained ICS', recurrence_rule='FREQ=DAILY',
                   recurrence_overrides={recurrence_key(START + timedelta(days=1)): {
                       'title': 'Changed', 'start': (START + timedelta(days=2)).isoformat(),
                       'end': (START + timedelta(days=2, hours=1)).isoformat(), 'raw_ics': 'Synthetic detached ICS'}})
    writer = StateWriter()
    state = writer.event(master)
    assert 'Synthetic retained ICS' not in json.dumps(state)
    assert 'Synthetic detached ICS' not in json.dumps(state)
    reader = StateReader(writer.zones, writer.definitions)
    restored = reader.event(transported(state))
    assert restored.raw_ics == ''
    assert restored._query_projection
    assert '_query_projection' not in restored.to_dict()
    assert restored.recurrence_overrides == {
        key: {name: value for name, value in payload.items() if name != 'raw_ics'}
        for key, payload in master.recurrence_overrides.items()}
    complete = reader.event(transported(writer.event(master, omit_transport=False)))
    assert complete == master
    assert not complete._query_projection
    assert reader.event(writer.event(restored, omit_transport=False))._query_projection


def test_occurrence_deltas_share_master_fields_and_can_reuse_indexed_events():
    master = event(recurrence_rule='FREQ=WEEKLY', description='Synthetic notes ' * 100)
    prepared = PreparedSeries(master)
    writer = StateWriter()
    master_state = writer.event(master)
    state = writer.series(prepared)
    assert 'description' not in state['explicit'][0]['f']
    reader = StateReader(writer.zones, writer.definitions)
    restored_master = reader.event(master_state)
    explicit = tuple(reader.event(item, base=restored_master) for item in state.pop('explicit'))
    restored = reader.series(restored_master, state, explicit_occurrences=explicit)
    assert restored.explicit_occurrences[0] is explicit[0]
    assert explicit[0].description is restored_master.description
    assert explicit[0] == occurrence(master, START)


@pytest.mark.parametrize('all_day,floating', [(True, False), (False, True)])
def test_civil_date_and_floating_prepared_ranges_survive_storage(all_day, floating, monkeypatch):
    first = START.replace(hour=0) if all_day else START
    master = event(start=first, end=first + timedelta(days=2) if all_day else first + timedelta(hours=1),
                   all_day=all_day, floating_time=floating, recurrence_rule='FREQ=MONTHLY;COUNT=400', range_policy='cut')
    expected, restored, _writer, _reader = roundtrip(master)
    if floating:
        monkeypatch.setattr('debacal.timeutil.local_zone', lambda: ZoneInfo('America/New_York'))
    lower = datetime(2026, 3, 1, tzinfo=ZoneInfo('America/New_York'))
    assert restored.query(lower, lower + timedelta(days=65)) == expected.query(lower, lower + timedelta(days=65))


def test_numeric_types_and_datetime_extremes_are_exact():
    writer = StateWriter()
    values = (array('Q', [0, 2 ** 63, 2 ** 64 - 1]), array('I', [0, 2 ** 32 - 1]),
              datetime(1, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc),
              datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=3))),
              timedelta(days=-2, microseconds=1), frozenset({'a', 'b'}))
    state = transported(writer.value(values))
    restored = StateReader(writer.zones, writer.definitions).value(state)
    assert restored == values


def test_version_mismatch_requires_rebuild_before_querying():
    master = event(recurrence_rule='FREQ=DAILY')
    writer = StateWriter()
    state = writer.series(PreparedSeries(master))
    state['v'] = FORMAT_VERSION + 1
    with pytest.raises(PreparedStateError, match='version'):
        StateReader(writer.zones, writer.definitions).series(master, state)


def test_restored_timezone_is_shared_with_first_edit_and_released_with_its_owners():
    identifier = 'Persistence/First-edit'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\n'
                  'BEGIN:STANDARD\nDTSTART:16010101T030000\nTZOFFSETFROM:+0200\n'
                  'TZOFFSETTO:+0100\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\nEND:STANDARD\n'
                  'BEGIN:DAYLIGHT\nDTSTART:16010101T020000\nTZOFFSETFROM:+0100\n'
                  'TZOFFSETTO:+0200\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\nEND:DAYLIGHT\nEND:VTIMEZONE\n')
    zone = resolve_timezone(identifier, {identifier: definition})
    first = datetime(2026, 1, 1, 9, tzinfo=zone)
    master = event(start=first, end=first + timedelta(hours=1),
                   timezone_id=identifier, timezone_definitions={identifier: definition})
    writer = StateWriter()
    state = transported(writer.event(master))
    zones, definitions = transported(writer.zones), transported(writer.definitions)
    _cached_explicit_timezone.cache_clear()
    with _prepared_definitions_lock:
        _prepared_definitions.pop((identifier, definition), None)
    first_reader, second_reader = StateReader(zones, definitions), StateReader(zones, definitions)
    with patch('debacal.timezones._explicit_timezone', side_effect=AssertionError('First edit recompiled timezone')), \
            patch.object(BoundedRule, '__iter__', side_effect=AssertionError('First edit regenerated transitions')):
        restored = first_reader.event(state)
        other = second_reader.event(state)
        shared = restored.start.tzinfo
        assert other.start.tzinfo is shared
        assert resolve_timezone(identifier, {identifier: definition}) is shared
        edited = replace(other, title='Edited')
        from_json = Event.from_dict(other.to_dict())
        assert edited.start.tzinfo is shared and from_json.start.tzinfo is shared
        fold_start = datetime(2026, 10, 25, 2, 30, tzinfo=shared, fold=1)
        folded = replace(other, start=fold_start, end=fold_start.replace(hour=3))
        assert folded.start.utcoffset() == timedelta(hours=1)
        assert folded.start.fold == 1
        # Closing the first Store leaves the second owner's prepared data live.
        del first_reader, restored
        assert replace(other, location='New location').start.tzinfo is shared
    reference = weakref.ref(shared)
    del second_reader, other, edited, from_json, folded, fold_start, shared
    gc.collect()
    assert reference() is None
    with _prepared_definitions_lock:
        assert (identifier, definition) not in _prepared_definitions


def test_same_tzid_with_different_resource_definitions_keeps_distinct_prepared_rules():
    identifier = 'Persistence/Distinct-definitions'
    writer = StateWriter()
    states, texts = [], []
    for offset in ('+0100', '+0300'):
        text = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\nDTSTART:19700101T000000\n'
                f'TZOFFSETFROM:{offset}\nTZOFFSETTO:{offset}\nEND:STANDARD\nEND:VTIMEZONE\n')
        texts.append(text)
        zone = resolve_timezone(identifier, {identifier: text})
        first = START.replace(tzinfo=zone)
        states.append(writer.event(event(start=first, end=first + timedelta(hours=1),
                                         timezone_id=identifier, timezone_definitions={identifier: text})))
    reader = StateReader(writer.zones, writer.definitions)
    values = [reader.event(state) for state in states]
    with patch('debacal.timezones._explicit_timezone', side_effect=AssertionError('Definition reparsed')):
        assert [replace(item, title='Changed').start.utcoffset() for item in values] == [timedelta(hours=1), timedelta(hours=3)]
        for item, text in zip(values, texts):
            assert resolve_timezone(identifier, {identifier: text}) is item.start.tzinfo


def test_shared_sink_fires_once_but_each_source_records_existing_references():
    written = []
    writer = StateWriter(shared_sink=lambda *value: written.append(value))
    zone = timezone(timedelta(hours=3), 'Synthetic offset')
    zone_ref = writer.zone(zone)
    definition_ref = writer.definition('Synthetic timezone text')
    assert len(written) == 2
    assert writer.references == {('zone', zone_ref), ('definition', definition_ref)}
    writer.reset_references()
    assert not writer.references
    assert writer.zone(zone) == zone_ref
    assert writer.definition('Synthetic timezone text') == definition_ref
    assert writer.references == {('zone', zone_ref), ('definition', definition_ref)}
    assert written == [('zone', zone_ref, writer.zones[zone_ref]),
                       ('definition', definition_ref, writer.definitions[definition_ref])]


def test_reader_releases_removed_catalog_data_and_can_register_it_again():
    identifier = 'Persistence/Reintroduced-definition'
    text = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\nDTSTART:19700101T000000\n'
            'TZOFFSETFROM:+0300\nTZOFFSETTO:+0300\nEND:STANDARD\nEND:VTIMEZONE\n')
    zone = resolve_timezone(identifier, {identifier: text})
    first = START.replace(tzinfo=zone)
    master = event(start=first, end=first + timedelta(hours=1),
                   timezone_id=identifier, timezone_definitions={identifier: text})
    writer = StateWriter()
    state = writer.event(master)
    zones, definitions = transported(writer.zones), transported(writer.definitions)
    reader = StateReader(zones, definitions)
    restored = reader.event(state)
    reference = weakref.ref(restored.start.tzinfo)
    reader.retain_references(set(), set())
    removed_zones, removed_definitions = dict(zones), dict(definitions)
    zones.clear()
    definitions.clear()
    with pytest.raises(KeyError):
        reader.zone(state['s'][2])
    with pytest.raises(KeyError):
        reader.definitions_state(state['d'])
    del restored
    gc.collect()
    assert reference() is None
    zones.update(removed_zones)
    definitions.update(removed_definitions)
    _cached_explicit_timezone.cache_clear()
    with patch('debacal.timezones._explicit_timezone', side_effect=AssertionError('Removed registration was not reset')):
        restored = reader.event(state)
        assert replace(restored, title='Reintroduced').start.tzinfo is restored.start.tzinfo


def test_floating_restore_uses_current_viewer_zone_without_loading_writer_zone(monkeypatch):
    berlin, new_york = ZoneInfo('Europe/Berlin'), ZoneInfo('America/New_York')
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: berlin)
    first = datetime(2026, 10, 25, 9, tzinfo=berlin)
    master = event(start=first, end=first + timedelta(hours=1), floating_time=True,
                   recurrence_rule='FREQ=DAILY')
    writer = StateWriter()
    state, series_state = writer.event(master), writer.series(PreparedSeries(master))
    assert state['s'][2] is None and state['e'][2] is None
    # Older prepared records may name their writer zone. Floating wall clocks
    # must remain portable even if that zone is unavailable to the new viewer.
    state['s'][2] = state['e'][2] = series_state['event_zone'] = 'iana:Unavailable/Writer-zone'
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: new_york)
    expected = Event.from_dict(master.to_dict())
    reader = StateReader(writer.zones, writer.definitions)
    with patch.object(Event, '__post_init__', side_effect=AssertionError('Floating restore revalidated Event')):
        actual = reader.event(state)
        restored = reader.series(actual, series_state)
    assert actual == expected
    assert actual.start.tzinfo is new_york and actual.end.tzinfo is new_york
    lower = first.replace(tzinfo=new_york, hour=0)
    assert restored.query(lower, lower + timedelta(days=3)) == PreparedSeries(expected).query(lower, lower + timedelta(days=3))


def test_fast_event_clone_preserves_all_fields_and_projection_provenance_without_pickle():
    master = event(description='Shared notes', raw_ics='Synthetic retained source',
                   recurrence_rule='FREQ=DAILY', recurrence_dates=(recurrence_key(START + timedelta(days=2)),),
                   _query_projection=True)
    with patch.object(Event, '__post_init__', side_effect=AssertionError('Clone validated Event')), \
            patch.object(Event, '__getstate__', side_effect=AssertionError('Clone used pickle protocol')), \
            patch.object(Event, '__setstate__', side_effect=AssertionError('Clone used pickle protocol')):
        cloned = copy_event(master)
    assert cloned == master and cloned is not master
    assert cloned._query_projection
    assert cloned.timezone_definitions is master.timezone_definitions
    assert cloned.recurrence_overrides is master.recurrence_overrides
    assert cloned.description is master.description
