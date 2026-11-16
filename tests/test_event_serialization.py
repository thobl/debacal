"""Durable snapshots isolate mutable payloads without copying immutable values."""
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event


START = datetime(2026, 11, 4, 9, 17, 31, 123456, tzinfo=ZoneInfo('Europe/Berlin'))


def sample(**changes):
    return replace(Event('synthetic', 'local', 'Synthetic café', START, START + timedelta(hours=1)), **changes)


def expected_snapshot(event):
    result = {item.name: deepcopy(getattr(event, item.name)) for item in fields(event)
              if item.name not in {'start', 'end', '_query_projection'}}
    result['start'] = (event.start.replace(tzinfo=None) if event.floating_time else event.start).isoformat()
    result['end'] = (event.end.replace(tzinfo=None) if event.floating_time else event.end).isoformat()
    result['recurrence_dates'] = list(event.recurrence_dates)
    result['excluded_dates'] = list(event.excluded_dates)
    return result


@pytest.mark.parametrize('floating,all_day', [(False, False), (True, False), (False, True)])
def test_snapshot_preserves_all_fields_order_json_and_endpoint_semantics(floating, all_day):
    source = sample(floating_time=floating, all_day=all_day,
                    recurrence_dates=((START + timedelta(days=7)).isoformat(),),
                    excluded_dates=((START + timedelta(days=14)).isoformat(),),
                    _query_projection=True)
    snapshot = source.to_dict()
    expected = expected_snapshot(source)
    assert snapshot == expected
    assert list(snapshot) == list(expected)
    assert '_query_projection' not in snapshot
    assert Event.from_dict(json.loads(json.dumps(snapshot))).to_dict() == expected


def test_nested_overrides_and_each_output_snapshot_have_independent_mutable_payloads():
    key = (START + timedelta(days=7)).isoformat()
    detached = sample(id='synthetic/second', series_id='synthetic', recurrence_id=key,
                      start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1))
    source = sample(recurrence_dates=(key,), recurrence_overrides={key: detached.to_dict()})
    canonical = next(iter(source.recurrence_overrides))
    first, second = source.to_dict(), source.to_dict()
    first['recurrence_dates'].append('2040-01-01T09:00:00+00:00')
    first['recurrence_overrides'][canonical]['title'] = 'Changed only in first snapshot'
    first['recurrence_overrides'][canonical]['recurrence_dates'].append('2040-01-02T09:00:00+00:00')
    first['recurrence_overrides'][canonical]['timezone_definitions']['Synthetic'] = 'Only first'
    assert second == expected_snapshot(source)
    assert source.recurrence_overrides[canonical]['title'] == detached.title
    assert not source.recurrence_overrides[canonical]['recurrence_dates']
    assert not source.recurrence_overrides[canonical]['timezone_definitions']


def test_scoped_timezone_definition_snapshot_is_independent_without_copying_tzinfo():
    definition = ('BEGIN:VTIMEZONE\r\nTZID:Synthetic/Offset\r\nBEGIN:STANDARD\r\n'
                  'DTSTART:19700101T000000\r\nTZOFFSETFROM:+0130\r\nTZOFFSETTO:+0130\r\n'
                  'END:STANDARD\r\nEND:VTIMEZONE\r\n')
    source = sample(timezone_id='Synthetic/Offset', timezone_definitions={'Synthetic/Offset': definition})
    snapshot = source.to_dict()
    restored = Event.from_dict(snapshot)
    assert restored.start.timestamp() == source.start.timestamp()
    assert restored.end.timestamp() == source.end.timestamp()
    snapshot['timezone_definitions']['Synthetic/Offset'] = 'Changed only in snapshot'
    assert source.timezone_definitions['Synthetic/Offset'] == definition
    assert restored.timezone_definitions['Synthetic/Offset'] == definition


def test_repeated_hour_folds_survive_snapshot_round_trip():
    zone = ZoneInfo('Europe/Berlin')
    for fold in (0, 1):
        start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=fold)
        end = (start.astimezone(timezone.utc) + timedelta(minutes=15)).astimezone(zone)
        source = sample(start=start, end=end)
        restored = Event.from_dict(source.to_dict())
        assert restored.start.timestamp() == source.start.timestamp()
        assert restored.end.timestamp() == source.end.timestamp()
        assert restored.start.fold == fold


def test_snapshot_preserves_extra_dataclass_fields_and_mutable_extension_isolation():
    @dataclass(frozen=True, slots=True)
    class ExtendedEvent(Event):
        extension: dict = field(default_factory=dict)

    source = ExtendedEvent('extended', 'local', 'Synthetic', START, START + timedelta(hours=1),
                           extension={'values': [{'label': 'Retain'}]})
    snapshot = source.to_dict()
    assert snapshot == expected_snapshot(source)
    snapshot['extension']['values'][0]['label'] = 'Changed only in snapshot'
    assert source.extension == {'values': [{'label': 'Retain'}]}


def test_snapshot_preserves_shared_nested_values_and_cycles_in_mutable_extensions():
    @dataclass(frozen=True, slots=True)
    class ExtendedEvent(Event):
        extension: dict = field(default_factory=dict)

    shared = [{'label': 'Retain'}]
    extension = {'left': shared, 'right': shared}
    extension['self'] = extension
    source = ExtendedEvent('extended', 'local', 'Synthetic', START, START + timedelta(hours=1),
                           extension=extension)
    copied = source.to_dict()['extension']
    assert copied is copied['self']
    assert copied['left'] is copied['right']
    assert copied['left'] is not shared
    copied['left'][0]['label'] = 'Changed only in snapshot'
    assert shared[0]['label'] == 'Retain'


def test_mutable_mapping_subclasses_and_opaque_values_keep_deepcopy_semantics():
    class CustomMapping(dict):
        pass

    class CustomKey:
        pass

    @dataclass(frozen=True, slots=True)
    class ExtendedEvent(Event):
        extension: dict = field(default_factory=dict)

    key = CustomKey()
    nested = CustomMapping(values=[{'label': 'Retain'}])
    source = ExtendedEvent('extended', 'local', 'Synthetic', START, START + timedelta(hours=1),
                           extension={'mapping': nested, 'opaque_keys': {key: ['Retain']}})
    copied = source.to_dict()['extension']
    assert type(copied['mapping']) is CustomMapping
    assert next(iter(copied['opaque_keys'])) is not key
    copied['mapping']['values'][0]['label'] = 'Changed only in snapshot'
    assert nested['values'][0]['label'] == 'Retain'


def test_constructor_isolates_each_override_payload_from_its_input_and_other_payloads():
    first = (START + timedelta(days=7)).isoformat()
    second = (START + timedelta(days=14)).isoformat()
    payload = sample().to_dict()
    source = sample(recurrence_overrides={first: payload, second: payload})
    payload['recurrence_dates'].append('2040-01-01T09:00:00+00:00')
    entries = list(source.recurrence_overrides.values())
    assert not entries[0]['recurrence_dates'] and not entries[1]['recurrence_dates']
    entries[0]['recurrence_dates'].append('2041-01-01T09:00:00+00:00')
    assert not entries[1]['recurrence_dates']
