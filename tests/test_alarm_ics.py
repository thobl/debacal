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


@pytest.mark.parametrize('full', [True, False])
def test_relative_trigger_with_tzid_retains_issue_and_healthy_sibling(full):
    malformed = block('ACTION:AUDIO', 'TRIGGER;TZID=Europe/Berlin:-PT5M')
    report = parse_report(resource(component(malformed, DISPLAY)), 'c', use_full=full)
    event, = report.events
    bad, healthy = event.alarms
    assert bad.issue
    with pytest.raises(ValueError):
        alarm_trigger(bad, event.start, event.end)
    assert healthy.issue is None
    assert b'TRIGGER;TZID=Europe/Berlin:-PT5M' in export_ics([event])


def test_repeat_semantics_ignore_valid_integer_spelling_and_preserve_malformed_values():
    def read_count(count):
        source = component(block('ACTION:AUDIO', 'TRIGGER:-PT5M', 'REPEAT:' + count, 'DURATION:PT1M'))
        return read_alarms(source, component_key='e')
    decorated, = read_count('+02')
    assert alarm_semantic((decorated,)) == alarm_semantic(read_count('2'))
    assert 'REPEAT:+02' in write_alarms(component(), (decorated,))
    assert alarm_semantic(read_count('invalid')) != alarm_semantic(read_count('other'))


def test_late_fold_alarm_preserves_event_instant_and_nominal_trigger_in_standard_calendar():
    from icalendar import Calendar

    zone = ZoneInfo('Europe/Berlin')
    late = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    event = alarm_event(start=late, end=datetime(2026, 10, 25, 3, 30, tzinfo=zone), timezone_id=zone.key,
                        alarms=(Alarm('nominal', 'DISPLAY', '-P1D', description='Example'),))
    raw = export_ics([event])
    wire, = Calendar.from_ical(raw).walk('VEVENT')
    wire_alarm, = wire.walk('VALARM')
    assert wire.decoded('DTSTART').astimezone(timezone.utc) == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert wire.decoded('DTSTART') + wire_alarm.decoded('TRIGGER') == datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)
    restored, = parse_ics(raw, 'c')
    assert restored.start.timestamp() == late.timestamp()
    assert restored.alarms[0].trigger == '-P1D'
    assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)


def repeated_fold_event(**changes):
    zone = ZoneInfo('Europe/Berlin')
    late = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    source = component(block('ACTION:AUDIO', 'UID:fold-alarm', 'TRIGGER:-P3D', 'REPEAT:4', 'DURATION:P1D',
                             'ACKNOWLEDGED:20261020T050000Z', 'RELATED-TO;RELTYPE=SNOOZE:other',
                             'X-RETAIN:metadata'))
    alarms = read_alarms(source, component_key='e/')
    return replace(alarm_event(start=late, end=datetime(2026, 10, 25, 3, 30, tzinfo=zone), timezone_id=zone.key,
                               alarms=alarms), **changes)


def test_late_fold_wire_repeats_cover_every_actual_instant_and_restore_only_intact_group():
    from icalendar import Calendar
    from debacal.ics_fallback import unfolded

    event = repeated_fold_event()
    raw = export_ics([event])
    calendar = Calendar.from_ical(raw)
    wire, = calendar.walk('VEVENT')
    bounds = wire.decoded('DTSTART')
    alarms = wire.walk('VALARM')
    assert len(alarms) == 2
    assert str(alarms[0]['UID']) == 'fold-alarm' and alarms[0]['UID'] != alarms[1]['UID']
    actual = []
    for part in alarms:
        assert str(part['RELATED-TO']) == 'other' and part['RELATED-TO'].params['RELTYPE'] == 'SNOOZE'
        assert str(part['X-RETAIN']) == 'metadata'
        assert part.decoded('ACKNOWLEDGED') == datetime(2026, 10, 20, 5, tzinfo=timezone.utc)
        initial = bounds + part.decoded('TRIGGER')
        actual.append(initial)
        for index in range(1, int(part.get('REPEAT', 0)) + 1):
            actual.append(initial + part.decoded('DURATION') * index)
    assert actual == [datetime(2026, 10, day, utc_hour, 30, tzinfo=timezone.utc)
                      for day, utc_hour in [(22, 0), (23, 0), (24, 0), (25, 0), (26, 1)]]
    restored, = parse_ics(raw, 'c')
    assert alarm_semantic(restored.alarms) == alarm_semantic(event.alarms)

    # A phone changing a part, or removing it altogether, owns that edit.
    lines = unfolded(raw)
    second = [i for i, line in enumerate(lines) if line == 'BEGIN:VALARM'][1]
    changed = list(lines)
    changed.insert(second + 1, 'X-PHONE:Edited')
    edited, = parse_ics('\n'.join(changed), 'c')
    assert len(edited.alarms) == 2 and 'X-PHONE:Edited' in edited.alarms[1].raw_ics
    assert edited.alarms[0].trigger != '-P3D'
    del lines[second:lines.index('END:VALARM', second) + 1]
    removed, = parse_ics('\n'.join(lines), 'c')
    assert len(removed.alarms) == 1 and removed.alarms[0].trigger != '-P3D'


def test_nominal_fold_projection_limits_work_and_keeps_large_elapsed_repeat_compact():
    event = repeated_fold_event()
    alarm, = event.alarms
    with pytest.raises(ValueError, match='100,000'):
        export_ics([replace(event, alarms=(replace(alarm, repeat=100001),))])
    elapsed = replace(alarm, repeat=2**31 - 1, repeat_interval='PT1S')
    raw = export_ics([replace(event, alarms=(elapsed,))])
    assert raw.count(b'BEGIN:VALARM') == 1
    assert parse_ics(raw, 'c')[0].alarms[0].repeat == 2**31 - 1


def test_fold_generated_uids_stable_through_no_uid_roundtrip_and_legacy_hydration():
    event = repeated_fold_event()
    original, = event.alarms
    event = replace(event, alarms=(replace(original, uid=None, key='local-editor-key'),))
    raw = export_ics([event])
    restored, = parse_ics(raw, 'c')
    def wire_uids(data):
        from icalendar import Calendar
        return [str(alarm['UID']) for alarm in Calendar.from_ical(data).walk('VALARM')]
    assert wire_uids(export_ics([restored])) == wire_uids(raw)
    assert restored.alarms[0].uid is None
    assert hydrate_alarms(replace(restored, alarms=None)).alarms == restored.alarms


@pytest.mark.parametrize('full', [True, False])
def test_damaged_fold_extension_keeps_standard_firings(full):
    from debacal.ics_fallback import unfolded

    lines = unfolded(export_ics([repeated_fold_event()]))
    for index, line in enumerate(lines):
        if line.startswith('X-DEBACAL-ORIGINAL-ALARM;'):
            lines[index] = line.split(':', 1)[0] + ':%%%'
    report = parse_report('\n'.join(lines), 'c', use_full=full)
    event, = report.events
    assert len(event.alarms) == 2
    assert event.alarms[0].trigger != '-P3D'


def test_legacy_untouched_export_retains_fold_alarm_intent_for_later_hydration():
    source = repeated_fold_event()
    imported, = parse_ics(export_ics([source]), 'c')
    legacy = replace(imported, alarms=None)
    read_back, = parse_ics(export_ics([legacy]), 'c')
    assert alarm_semantic(read_back.alarms) == alarm_semantic(source.alarms)


def test_negative_zero_repeat_is_equivalent_to_zero_but_negative_count_is_invalid():
    def read_count(number):
        raw = component(block('ACTION:AUDIO', 'TRIGGER:PT0S', 'REPEAT:' + number, 'DURATION:PT1M'))
        return read_alarms(raw, component_key='e')
    negative_zero = read_count('-00')
    assert negative_zero[0].issue is None
    assert alarm_semantic(negative_zero) == alarm_semantic(read_count('0'))
    assert read_count('-1')[0].issue


def test_late_fold_end_alarm_uses_exact_wire_end_and_local_nominal_offset():
    from icalendar import Calendar

    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 1, 30, tzinfo=zone)
    end = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    source = alarm_event(start=start, end=end, timezone_id=zone.key,
                         alarms=(Alarm('k', 'DISPLAY', '-P1D', related='END'),))
    raw = export_ics([source])
    wire, = Calendar.from_ical(raw).walk('VEVENT')
    part, = wire.walk('VALARM')
    assert part['TRIGGER'].params['RELATED'] == 'END'
    assert wire.decoded('DTEND').timestamp() == end.timestamp()
    expected = datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)
    assert wire.decoded('DTEND') + part.decoded('TRIGGER') == expected
    restored, = parse_ics(raw, 'c')
    assert restored.end.fold == 1 and restored.alarms[0].trigger == '-P1D'
    assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == expected


@pytest.mark.parametrize('phoenix_clock,hour', [('20261024T213000', 4), ('20261024T193000', 2)])
def test_foreign_end_zone_and_nominal_alarm_survive_intact_start_group(phoenix_clock, hour):
    from debacal.ics_fallback import unfolded
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    end = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    event = alarm_event(start=start, end=end, timezone_id=zone.key,
                        alarms=(Alarm('start', 'DISPLAY', '-P1D', description='Before start'),))
    lines = unfolded(export_ics([event]))
    index = next(i for i, line in enumerate(lines) if line.startswith('DTEND:'))
    lines[index] = 'DTEND;TZID=America/Phoenix:' + phoenix_clock
    lines.insert(lines.index('END:VEVENT'), block('ACTION:DISPLAY', 'DESCRIPTION:Phone',
                                                 'TRIGGER;RELATED=END:-P1D'))
    edited, = parse_ics('\n'.join(lines), 'c')
    def verify(actual):
        assert actual.start.timestamp() == start.timestamp()
        assert actual.end.astimezone(timezone.utc) == datetime(2026, 10, 25, hour, 30, tzinfo=timezone.utc)
        assert actual.end.tzinfo.key == 'America/Phoenix'
        assert [alarm.trigger for alarm in actual.alarms] == ['-P1D', '-P1D']
        assert alarm_trigger(actual.alarms[0], actual.start, actual.end) == datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)
        assert alarm_trigger(actual.alarms[1], actual.start, actual.end) == datetime(2026, 10, 24, hour, 30, tzinfo=timezone.utc)
    verify(edited)
    verify(Event.from_dict(edited.to_dict()))
    exported = export_ics([edited])
    assert ('DTEND;TZID=America/Phoenix:' + phoenix_clock).encode() in exported
    assert b'TZID:America/Phoenix' in exported
    verify(parse_ics(exported, 'c')[0])
    before_end_alarm = replace(edited, alarms=(edited.alarms[0],))
    ordinary_end, = parse_ics(export_ics([before_end_alarm]), 'c')
    assert ordinary_end.end_timezone_id == 'America/Phoenix'



def test_changed_utc_end_with_same_elapsed_alarm_offset_restores_only_start_group():
    from debacal.ics_fallback import unfolded
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    end = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=1)
    alarms = (Alarm('start', 'DISPLAY', '-P1D'), Alarm('end', 'AUDIO', '-P1D', related='END'))
    raw = unfolded(export_ics([alarm_event(start=start, end=end, timezone_id=zone.key, alarms=alarms)]))
    raw[raw.index('DTEND:20261025T014500Z')] = 'DTEND:20261025T014600Z'
    updated, = parse_ics('\n'.join(raw), 'c')
    assert updated.alarms[0].trigger == '-P1D' and updated.alarms[1].trigger != '-P1D'
    assert updated.end_timezone_id == 'UTC'
    assert alarm_trigger(updated.alarms[1], updated.start, updated.end) == datetime(2026, 10, 24, 0, 46, tzinfo=timezone.utc)


def test_changed_named_start_zone_leaves_verified_end_group_intact():
    from debacal.ics_fallback import unfolded
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 1, 30, tzinfo=zone)
    end = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    source = alarm_event(start=start, end=end, timezone_id=zone.key,
                         alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END'),))
    raw = unfolded(export_ics([source]))
    raw[raw.index('DTSTART;TZID=Europe/Berlin:20261025T013000')] = 'DTSTART;TZID=America/Phoenix:20261024T163000'
    raw.insert(raw.index('END:VEVENT'), block('ACTION:AUDIO', 'TRIGGER:-P1D'))
    edited, = parse_ics('\n'.join(raw), 'c')
    restored, = parse_ics(export_ics([Event.from_dict(edited.to_dict())]), 'c')
    assert restored.start.timestamp() == start.timestamp() and restored.end.timestamp() == end.timestamp()
    assert restored.timezone_id == 'America/Phoenix' and restored.end_timezone_id == 'Europe/Berlin'
    assert [alarm.trigger for alarm in restored.alarms] == ['-P1D', '-P1D']
    assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)
    assert alarm_trigger(restored.alarms[1], restored.start, restored.end) == datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)


def exact_date_family():
    zone = ZoneInfo('Europe/Berlin')
    alarms = read_alarms(component(
        block('ACTION:DISPLAY', 'UID:nominal', 'TRIGGER:-P1D', 'DESCRIPTION:Nominal'),
        block('ACTION:DISPLAY', 'UID:linked', 'TRIGGER:-PT5M', 'DESCRIPTION:Linked',
              'RELATED-TO;RELTYPE=SNOOZE:nominal', 'X-COLOR:BLUE'),
        block('ACTION:DISPLAY', 'UID:absolute', 'TRIGGER;VALUE=DATE-TIME:20261020T090000Z', 'DESCRIPTION:Once')
    ), component_key='e/')
    return alarm_event(start=datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1),
                       end=datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc), timezone_id=zone.key,
                       recurrence_dates=('2026-10-25T00:30:00+00:00', '2026-10-27T01:30:00+00:00'), alarms=alarms)


@pytest.mark.parametrize('full', [True, False])
def test_exact_date_alarm_exception_preserves_uid_ownership_and_external_edits(full):
    from debacal.ics_fallback import folded, unfolded
    from debacal.ics_resources import _blocks
    from debacal.recurrence import expand_event, recurrence_key

    event = exact_date_family()
    wire = export_ics([event])
    header, blocks = _blocks(wire)
    master, child = [b for b in blocks if b[0] == 'BEGIN:VEVENT']
    assert 'DTSTART:20261025T013000Z' in master
    original = read_alarms('\n'.join(master), component_key='e/')
    relative = read_alarms('\n'.join(child), component_key='e/child')
    assert [a.uid for a in original] == ['nominal', 'linked', 'absolute']
    assert [a.description for a in relative] == ['Nominal', 'Linked']
    assert not {a.uid for a in relative} & {a.uid for a in original}
    assert f'RELATED-TO;RELTYPE=SNOOZE:{relative[0].uid}' in unfolded(relative[1].raw_ics)
    assert 'X-COLOR:BLUE' in relative[1].raw_ics

    def decode(parts):
        data = folded(['BEGIN:VCALENDAR', *header, *(line for b in parts for line in b), 'END:VCALENDAR'])
        result = parse_report(data, 'c', use_full=full)
        assert not result.rejected
        return result.events[0]

    intact = decode(blocks)
    assert intact.start.fold == 1 and intact.timezone_id == event.timezone_id
    assert not intact.recurrence_overrides
    assert alarm_semantic(intact.alarms) == alarm_semantic(event.alarms)
    _, again = _blocks(export_ics([intact]))
    repeated = next(b for b in again if 'RECURRENCE-ID:20261025T013000Z' in b)
    assert [a.uid for a in read_alarms('\n'.join(repeated), component_key='ignored')] == [a.uid for a in relative]

    key = recurrence_key(event.start)
    for change in (unfolded(write_alarms('\n'.join(child), ())), [*child[:-1], 'X-PHONE:keep', child[-1]]):
        edited = decode([change if b is child else b for b in blocks])
        assert key in edited.recurrence_overrides
        if 'X-PHONE:keep' in change:
            assert b'X-PHONE:keep' in export_ics([edited])
        else:
            assert Event.from_dict(edited.recurrence_overrides[key]).alarms == ()

    removed = decode([b for b in blocks if b is not child])
    assert not removed.recurrence_overrides
    values = expand_event(removed, datetime(2026, 10, 24, tzinfo=timezone.utc),
                          datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert [alarm_trigger(v.alarms[0], v.start, v.end) for v in values] == [
        datetime(2026, 10, day, hour, 30, tzinfo=timezone.utc) for day, hour in [(24, 0), (24, 1), (26, 1)]]


def test_finite_exact_dates_share_one_nominal_evaluation_budget(monkeypatch):
    from debacal import alarm_rdate_ics
    event = exact_date_family()
    monkeypatch.setattr(alarm_rdate_ics, 'MAX_GENERATED_RANGES', 2)
    with pytest.raises(ValueError, match='2 nominal evaluations'):
        export_ics([event])


@pytest.mark.parametrize('full', [True, False])
def test_standalone_duration_end_annotation_requires_unchanged_standard_wire(full):
    from debacal.ics_fallback import folded, unfolded
    zone = ZoneInfo('Europe/Berlin')
    mixed = alarm_event(start=datetime(2026, 10, 24, 9, tzinfo=zone), timezone_id=zone.key,
                        end=datetime(2026, 10, 25, 8, tzinfo=timezone.utc), duration_spec='P1D',
                        alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END', description='End'),))
    raw = export_ics([mixed])
    assert b'DURATION:P1D' in raw
    restored, = parse_report(raw, 'c', use_full=full).events
    assert (restored.duration_spec, restored.end_timezone_id, restored.alarms[0].trigger) == ('P1D', 'UTC', '-P1D')
    # Replacing DURATION with the exact same instant in a named zone changes the
    # owner of END arithmetic. Only the elapsed standard alarm remains valid.
    edited = folded('DTEND;TZID=Europe/Berlin:20261025T090000' if line == 'DURATION:P1D' else line
                    for line in unfolded(raw))
    changed, = parse_report(edited, 'c', use_full=full).events
    assert changed.duration_spec == '' and changed.end_timezone_id == ''
    assert changed.alarms[0].trigger == '-PT86400S'


def test_duplicate_detached_identity_cannot_discard_phone_edit_as_generated():
    from debacal.ics_fallback import folded, unfolded
    from debacal.ics_resources import _blocks
    from debacal.recurrence import recurrence_key
    event = exact_date_family()
    header, blocks = _blocks(export_ics([event]))
    child = next(b for b in blocks if 'RECURRENCE-ID:20261025T013000Z' in b)
    edited = unfolded(write_alarms('\n'.join(child), ()))
    data = folded(['BEGIN:VCALENDAR', *header, *(line for b in blocks for line in b), *edited, 'END:VCALENDAR'])
    stored, = parse_ics(data, 'c')
    assert Event.from_dict(stored.recurrence_overrides[recurrence_key(event.start)]).alarms == ()


def test_exact_date_generated_child_gives_uidless_relative_siblings_stable_distinct_uids():
    from debacal.ics_resources import _blocks
    event = exact_date_family()
    relative = Alarm('editor', 'DISPLAY', '-PT5M', description='Check')
    event = replace(event, alarms=(event.alarms[0], relative, replace(relative, key='another')))
    def child_ids(data):
        child = next(b for b in _blocks(data)[1] if 'RECURRENCE-ID:20261025T013000Z' in b)
        return [a.uid for a in read_alarms('\n'.join(child), component_key='e/child')]
    wire = export_ics([event])
    uids = child_ids(wire)
    assert all(uids) and len(set(uids)) == 3
    restored, = parse_ics(wire, 'c')
    assert not restored.recurrence_overrides
    assert child_ids(export_ics([restored])) == uids
