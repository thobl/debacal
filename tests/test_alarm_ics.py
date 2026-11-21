from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from alarm_support import BASE, alarm_event
from debacal.alarms import Alarm, alarm_semantic, alarm_trigger
from debacal.alarm_ics import hydrate_alarms, read_alarms, write_alarms
from debacal.ics import export_ics, parse_ics, patch_ics
from debacal.ics_resources import parse_report
from debacal.models import Event


def component(*lines):
    return '\r\n'.join(('BEGIN:VEVENT', 'UID:e', 'DTSTART:20261121T090000Z', *lines, 'END:VEVENT'))


def resource(*events):
    return '\r\n'.join(('BEGIN:VCALENDAR', 'VERSION:2.0', *events, 'END:VCALENDAR', ''))


def block(*lines):
    return '\r\n'.join(('BEGIN:VALARM', *lines, 'END:VALARM'))


DISPLAY = block('ACTION:DISPLAY', 'TRIGGER:-PT5M', 'DESCRIPTION:Reminder')


def test_explicit_empty_roundtrip():
    original = alarm_event(alarms=(Alarm('k', 'DISPLAY', '-PT5M', description='Reminder'),))
    restored, = parse_ics(export_ics([original]), 'c')
    assert restored.alarms[0].trigger == '-PT5M'
    removed, = parse_ics(patch_ics(restored.raw_ics, replace(restored, alarms=())), 'c')
    assert removed.alarms == ()
    assert Event.from_dict(original.to_dict()).alarms == original.alarms


@pytest.mark.parametrize('full', [True, False])
def test_malformed_neighbor_survives(full):
    bad = block('ACTION:DISPLAY', 'TRIGGER;BROKEN:unknown', 'MISSING-COLON')
    raw = resource(component(bad, DISPLAY))
    report = parse_report(raw, 'c', use_full=full)
    assert not report.rejected
    event, = report.events
    damaged, valid = event.alarms
    assert damaged.issue and 'MISSING-COLON' in damaged.raw_ics
    assert valid.issue is None and valid.trigger == '-PT5M'
    again, = parse_ics(export_ics([event]), 'c')
    assert again.alarms == event.alarms
    assert parse_report(raw.replace('END:VALARM', 'END:BAD', 1), 'c', use_full=full).rejected


@pytest.mark.parametrize('action', ['AUDIO', 'EMAIL', 'PROCEDURE', 'X-UNKNOWN'])
def test_metadata_survives_supported_field_edit(action):
    raw = component(block('UID:alarm-id', 'ACTION:' + action, 'DESCRIPTION;LANGUAGE=en:Old',
                          'TRIGGER;RELATED=END;X-P=one:-PT3M', 'ACKNOWLEDGED:20261121T090000Z',
                          'RELATED-TO;RELTYPE=SNOOZE:parent', 'ATTACH:https://example.invalid/sound',
                          'X-DETAIL:folded', '  metadata', 'REPEAT:2', 'DURATION:PT2M'))
    alarm, = read_alarms(raw, component_key='e')
    assert alarm.key == alarm.uid == 'alarm-id'
    assert alarm.acknowledged == BASE
    assert bool(alarm.issue) == (action == 'X-UNKNOWN')
    value = write_alarms(raw, (replace(alarm, description='New', trigger='-PT4M'),))
    for prop in ('DESCRIPTION;LANGUAGE=en:New', 'RELATED-TO;RELTYPE=SNOOZE:parent',
                 'ATTACH:https://example.invalid/sound', 'X-DETAIL:folded metadata', 'X-P=one'):
        assert prop in value
    updated, = read_alarms(value, component_key='e')
    assert (updated.trigger, updated.related, updated.repeat, updated.repeat_interval) == ('-PT4M', 'END', 2, 'PT2M')


def test_duplicate_keys_survive_event_title_changes_and_formatting():
    raw = component('SUMMARY:Old', DISPLAY, DISPLAY)
    original = read_alarms(raw, component_key='e')
    assert len({a.key for a in original}) == 2
    assert read_alarms(raw.replace('SUMMARY:Old', 'SUMMARY:New'), component_key='e') == original
    reformatted = read_alarms(raw.replace('TRIGGER:-PT5M', 'trigger;related=START:-PT5M'), component_key='e')
    assert alarm_semantic(original) == alarm_semantic(tuple(replace(a, key='other') for a in reformatted))
    extended = read_alarms(raw.replace('END:VALARM', 'X-DETAIL:one\r\nEND:VALARM', 1), component_key='e')
    assert alarm_semantic(original) != alarm_semantic(extended)


def test_absent_and_explicit_detached_alarms_hydrate_from_retained_family():
    child = component('RECURRENCE-ID:20261122T090000Z', block('ACTION:AUDIO', 'TRIGGER:-PT20M'))
    empty = component('RECURRENCE-ID:20261123T090000Z')
    event, = parse_ics(resource(component('RRULE:FREQ=DAILY;COUNT=3', DISPLAY), child, empty), 'c')
    payload = event.to_dict()
    del payload['alarms']
    for override in payload['recurrence_overrides'].values():
        del override['alarms']
        assert override['raw_ics'] == ''
    legacy = Event.from_dict(payload)
    assert legacy.alarms is None
    hydrated = hydrate_alarms(legacy)
    assert hydrated.alarms[0].trigger == '-PT5M'
    alarms = [Event.from_dict(value).alarms for value in hydrated.recurrence_overrides.values()]
    assert alarms[0][0].trigger == '-PT20M' and alarms[1] == ()
    assert hydrated == event
    assert hydrate_alarms(replace(legacy, alarms=())).alarms == ()


def test_nominal_alarm_on_standalone_event_retains_zone_after_export():
    local = datetime(2026, 3, 29, 9, tzinfo=ZoneInfo('Europe/Berlin'))
    event = alarm_event(start=local, end=local + timedelta(hours=1), timezone_id='Europe/Berlin',
                        alarms=(Alarm('k', 'DISPLAY', '-P1D', description='Example'),))
    again, = parse_ics(export_ics([event]), 'c')
    assert alarm_trigger(again.alarms[0], again.start, again.end) == datetime(2026, 3, 28, 8, tzinfo=timezone.utc)


def test_trigger_identity_is_stable_across_equivalent_duration_spellings():
    first, = read_alarms(component(DISPLAY), component_key='e')
    second, = read_alarms(component(DISPLAY.replace('-PT5M', '-PT300S')), component_key='e')
    assert second.key == first.key


def test_direct_components_exclude_nested_alarms_and_preserve_other_components():
    nested = '\r\n'.join(('BEGIN:X-CONTAINER', block('ACTION:AUDIO', 'TRIGGER:PT1S'), 'END:X-CONTAINER'))
    raw = component(nested, DISPLAY)
    found, = read_alarms(raw, component_key='e')
    assert found.action == 'DISPLAY'
    stripped = write_alarms(raw, ())
    assert nested in stripped and stripped.count('BEGIN:VALARM') == 1


def test_legacy_override_uses_its_own_retained_template_when_family_has_none():
    key = '2026-11-22T09:00:00+00:00'
    child = alarm_event(alarms=None, series_id='e', recurrence_id=key,
                        raw_ics=resource(component(block('ACTION:AUDIO', 'TRIGGER:-PT2M'))))
    source = alarm_event(alarms=(), recurrence_rule='FREQ=DAILY;COUNT=2',
                         recurrence_overrides={key: child.to_dict()})
    saved = hydrate_alarms(source)
    alarm, = Event.from_dict(saved.recurrence_overrides[key]).alarms
    assert alarm.trigger == '-PT2M'


def test_invalid_and_valid_trigger_extensions_remain_distinct():
    valid, = read_alarms(component(DISPLAY), component_key='e')
    invalid, = read_alarms(component(DISPLAY.replace('-PT5M', '(0, -300)')), component_key='e')
    assert invalid.issue
    assert alarm_semantic((valid,)) != alarm_semantic((invalid,))


def test_absolute_alarm_repeats_survive_export_and_import():
    alarm = Alarm('absolute', 'DISPLAY', '20261121T070000Z', repeat=2, repeat_interval='PT5M', uid='absolute')
    raw = export_ics([alarm_event(alarms=(alarm,))])
    assert b'TRIGGER;VALUE=DATE-TIME:20261121T070000Z' in raw
    event, = parse_ics(raw, 'c')
    actual, = event.alarms
    assert (actual.trigger, actual.repeat, actual.repeat_interval) == ('20261121T070000Z', 2, 'PT5M')
    assert alarm_trigger(actual, event.start, event.end, repeat_index=2) == BASE - timedelta(hours=1, minutes=50)


def test_legacy_child_resource_selects_detached_before_master_regardless_of_order():
    key = '2026-11-22T09:00:00+00:00'
    detached = component('RECURRENCE-ID:20261122T090000Z', block('ACTION:AUDIO', 'TRIGGER:-PT2M'))
    raw = resource(detached, component(DISPLAY))
    child = alarm_event(alarms=None, recurrence_id=key, series_id='e', raw_ics=raw)
    source = alarm_event(recurrence_rule='FREQ=DAILY;COUNT=2', recurrence_overrides={key: child.to_dict()})
    hydrated = hydrate_alarms(source)
    alarm, = Event.from_dict(hydrated.recurrence_overrides[key]).alarms
    assert alarm.trigger == '-PT2M'


def test_explicit_zero_repeat_count_keeps_initial_trigger():
    raw = component(block('ACTION:AUDIO', 'TRIGGER:-PT5M', 'REPEAT:0', 'DURATION:PT1M'))
    alarm, = read_alarms(raw, component_key='e')
    assert alarm.issue is None
    assert alarm_trigger(alarm, BASE, BASE) == BASE - timedelta(minutes=5)
    with pytest.raises(ValueError):
        alarm_trigger(alarm, BASE, BASE, repeat_index=1)
