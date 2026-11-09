"""Resource-scoped VTIMEZONE rules and floating wall-clock persistence."""
from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from debacal.event_duration import add_duration, elapsed_duration, end_for
from debacal.models import Event
from debacal.recurrence import (canonical_rule, expand_series, key_datetime,
                                occurrence, recurrence_key, recurrence_set,
                                schedule_fields, updated_series)
from debacal.store import Store
from debacal.timezones import TimezoneError, event_timezone, resolve_timezone


TZID = 'W. Europe Standard Time'
WINDOWS = '''BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
'''


def fixed_definition(identifier, offset='+0300'):
    return (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\n'
            f'DTSTART:16010101T000000\nTZOFFSETFROM:{offset}\nTZOFFSETTO:{offset}\n'
            'END:STANDARD\nEND:VTIMEZONE\n')


def custom_event(**changes):
    zone = resolve_timezone(TZID, {TZID: WINDOWS})
    start = datetime(2026, 9, 11, 11, tzinfo=zone)
    return replace(Event('custom', 'local', 'Meeting', start, start + timedelta(hours=1),
                         timezone_id=TZID, timezone_definitions={TZID: WINDOWS}), **changes)


def test_real_windows_definition_retains_its_own_rules_not_a_guessed_alias():
    zone = event_timezone(custom_event())
    assert datetime(2026, 9, 11, 11, tzinfo=zone).astimezone(timezone.utc).hour == 9
    # These explicitly supplied modern rules also apply before EU harmonization.
    historic = datetime(1995, 10, 1, 11, tzinfo=zone)
    assert historic.utcoffset() == timedelta(hours=2)
    assert historic.utcoffset() != historic.replace(tzinfo=ZoneInfo('Europe/Berlin')).utcoffset()


def test_redundant_tzid_text_parameter_is_valid_for_explicit_rules():
    text = WINDOWS.replace('TZID:', 'TZID;VALUE=TEXT:')
    zone = resolve_timezone(TZID, {TZID: text})
    assert datetime(2026, 9, 11, 11, tzinfo=zone).utcoffset() == timedelta(hours=2)


def test_same_identifier_in_different_resources_never_collides():
    winter = resolve_timezone(TZID, {TZID: WINDOWS})
    fixed = resolve_timezone(TZID, {TZID: fixed_definition(TZID)})
    assert winter is resolve_timezone(TZID, {TZID: WINDOWS})
    assert winter is not fixed
    assert datetime(2026, 1, 1, tzinfo=winter).utcoffset() == timedelta(hours=1)
    assert datetime(2026, 1, 1, tzinfo=fixed).utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize('identifier', ['Europe/Berlin', 'UTC'])
def test_explicit_definition_is_authoritative_even_for_a_database_identifier(identifier):
    definition = fixed_definition(identifier)
    zone = resolve_timezone(identifier, {identifier: definition})
    start = datetime(2026, 1, 1, 11, tzinfo=zone)
    source = Event('iana', 'local', 'Custom IANA rules', start, start + timedelta(hours=1),
                   timezone_id=identifier, timezone_definitions={identifier: definition})
    assert source.timezone_id == identifier
    assert source.start.utcoffset() == timedelta(hours=3)
    assert Event.from_dict(source.to_dict()).start.utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize('fold', [0, 1])
def test_json_roundtrip_restores_custom_rules_and_repeated_hour_identity(fold):
    source = custom_event()
    start = datetime(2026, 10, 25, 2, 30, tzinfo=event_timezone(source), fold=fold)
    source = replace(source, start=start, end=start + timedelta(hours=2))
    restored = Event.from_dict(json.loads(json.dumps(source.to_dict())))
    assert restored.start.fold == fold
    assert restored.start.timestamp() == source.start.timestamp()
    assert restored.timezone_definitions == {TZID: WINDOWS}
    assert restored == source


def test_timezone_context_is_owned_including_clones_and_json():
    definitions = {TZID: WINDOWS}
    source = custom_event(timezone_definitions=definitions)
    definitions.clear()
    payload = source.to_dict()
    payload['timezone_definitions'].clear()
    assert source.timezone_definitions == {TZID: WINDOWS}
    assert deepcopy(source) == source


def test_recurrence_and_sqlite_reopen_keep_custom_daylight_rules(tmp_path):
    source = custom_event()
    start = datetime(2026, 10, 20, 11, tzinfo=event_timezone(source))
    source = replace(source, start=start, end=start + timedelta(hours=1), recurrence_rule='FREQ=WEEKLY;COUNT=4')
    with Store(tmp_path / 'events.db') as store:
        store.apply('Create custom-zone series', [source])
    with Store(tmp_path / 'events.db') as store:
        saved = store.get_event(source.id)
        result = store.query_occurrences(datetime(2026, 10, 1, tzinfo=timezone.utc),
                                         datetime(2026, 12, 1, tzinfo=timezone.utc))
        assert saved.timezone_definitions == source.timezone_definitions
        assert [value.start.hour for value in result] == [11] * 4
        assert [value.start.utcoffset() for value in result] == [timedelta(hours=2)] + [timedelta(hours=1)] * 3
        assert all(value.timezone_definitions == source.timezone_definitions for value in result)


def test_custom_zone_nominal_day_and_elapsed_24_hours_remain_distinct():
    source = custom_event()
    start = datetime(2026, 3, 28, 12, tzinfo=event_timezone(source))
    nominal = replace(source, start=start, end=add_duration(start, 'P1D'), duration_spec='P1D')
    assert elapsed_duration(nominal.start, nominal.end) == timedelta(hours=23)
    assert nominal.end.hour == 12
    assert add_duration(start, 'PT24H').hour == 13
    restored = Event.from_dict(nominal.to_dict())
    assert restored.duration_spec == 'P1D'
    assert end_for(restored, start).hour == 12


def test_custom_zone_missing_clocks_use_the_rfc_offset_before_the_gap():
    source = custom_event()
    zone = event_timezone(source)
    missing = datetime(2026, 3, 29, 2, 30, tzinfo=zone)
    assert missing.utcoffset() == timedelta(hours=1)
    assert missing.astimezone(timezone.utc) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    source = replace(source, start=datetime(2026, 3, 28, 2, 30, tzinfo=zone),
                     end=datetime(2026, 3, 28, 3, 30, tzinfo=zone), recurrence_rule='FREQ=DAILY;COUNT=3')
    result = expand_series(source, datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 4, 1, tzinfo=zone))
    assert [(value.start.day, value.start.hour, value.start.minute) for value in result] == [
        (28, 2, 30), (29, 3, 30), (30, 2, 30)]


def test_custom_zone_whole_series_move_keeps_detached_context_and_exclusions():
    source = custom_event(recurrence_rule='FREQ=WEEKLY;COUNT=3')
    second_start = source.start + timedelta(weeks=1)
    second_key = recurrence_key(second_start)
    detached = occurrence(source, second_start, second_key)
    detached = replace(detached, start=detached.start + timedelta(hours=2),
                       end=detached.end + timedelta(hours=2))
    source = replace(source, recurrence_overrides={second_key: detached.to_dict()},
                     excluded_dates=(recurrence_key(source.start + timedelta(weeks=2)),))
    edited = replace(source, start=source.start + timedelta(days=1), end=source.end + timedelta(days=1))
    moved = updated_series(source, source, edited)
    result = expand_series(moved, source.start - timedelta(days=1), source.start + timedelta(weeks=4))
    assert [(value.start.day, value.start.hour) for value in result] == [(12, 11), (19, 13)]
    assert all(value.timezone_definitions == {TZID: WINDOWS} for value in result)


def test_all_day_custom_zone_is_a_civil_date_not_an_instant():
    source = custom_event()
    zone = event_timezone(source)
    source = replace(source, start=datetime(2026, 3, 29, tzinfo=zone),
                     end=datetime(2026, 3, 30, tzinfo=zone), all_day=True,
                     recurrence_rule='FREQ=DAILY;COUNT=2')
    restored = Event.from_dict(source.to_dict())
    result = expand_series(restored, datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 4, 1, tzinfo=zone))
    assert [(value.start.day, value.start.hour, value.end.day, value.end.hour) for value in result] == [
        (29, 0, 30, 0), (30, 0, 31, 0)]


@pytest.mark.parametrize('definitions', [
    {TZID: 'BEGIN:VTIMEZONE\nTZID:Different\nEND:VTIMEZONE'},
    {TZID: 'not a timezone'},
    {TZID: WINDOWS.replace('TZOFFSETTO:+0100', 'TZOFFSETTO:broken')},
])
def test_invalid_explicit_definitions_fail_clearly_without_falling_back(definitions):
    with pytest.raises(TimezoneError, match='Timezone'):
        resolve_timezone(TZID, definitions)


def test_missing_unknown_zone_has_an_actionable_error():
    with pytest.raises(TimezoneError, match='no VTIMEZONE definition'):
        resolve_timezone('Custom/Not-an-IANA-zone')


@pytest.mark.parametrize('identifier', [None, '', 'x' * 1025, 'bad\nzone', 'bad\x00zone', []],
                         ids=['none', 'empty', 'oversized', 'line-break', 'nul', 'wrong-type'])
def test_invalid_timezone_identifier_is_bounded_before_filesystem_lookup(identifier, monkeypatch):
    def unexpected_lookup(value):
        pytest.fail('Invalid identifiers must not reach filesystem timezone lookup.')
    monkeypatch.setattr('debacal.timezones.ZoneInfo', unexpected_lookup)
    with pytest.raises(TimezoneError) as error:
        resolve_timezone(identifier)
    assert len(str(error.value)) < 200


def test_timezone_lookup_oserror_becomes_an_actionable_bounded_diagnostic(monkeypatch):
    def failing_lookup(value):
        raise OSError('The timezone filename cannot be represented.')
    monkeypatch.setattr('debacal.timezones.ZoneInfo', failing_lookup)
    with pytest.raises(TimezoneError, match='no VTIMEZONE definition'):
        resolve_timezone('Unknown/Zone')


def test_scoped_timezone_parsing_does_not_populate_icalendar_global_cache():
    from icalendar.timezone import tzp
    identifier = 'Resource/Scoped-cache-probe'
    assert tzp.timezone(identifier) is None
    zone = resolve_timezone(identifier, {identifier: fixed_definition(identifier)})
    assert datetime(2026, 1, 1, tzinfo=zone).utcoffset() == timedelta(hours=3)
    assert tzp.timezone(identifier) is None


def test_oversized_definition_is_rejected_before_parsing():
    with pytest.raises(TimezoneError, match='256 KiB'):
        resolve_timezone(TZID, {TZID: WINDOWS + ' ' * (256 * 1024)})


def test_dense_ancient_transition_rule_has_a_computation_budget():
    definition = (fixed_definition('Dense').replace('TZOFFSETFROM:+0300', 'TZOFFSETFROM:+0200')
                  .replace('END:STANDARD', 'RRULE:FREQ=MINUTELY\nEND:STANDARD')
                  .replace('END:VTIMEZONE', 'BEGIN:DAYLIGHT\nDTSTART:16010601T000000\n'
                           'TZOFFSETFROM:+0300\nTZOFFSETTO:+0200\nRRULE:FREQ=YEARLY\n'
                           'END:DAYLIGHT\nEND:VTIMEZONE'))
    zone = resolve_timezone('Dense', {'Dense': definition})
    with pytest.raises(TimezoneError, match='computation limit'):
        datetime(2026, 1, 1, tzinfo=zone).utcoffset()
    # A partially consumed, failed generator must keep failing on retries.
    with pytest.raises(TimezoneError, match='computation limit'):
        datetime(2026, 1, 2, tzinfo=zone).utcoffset()


def test_lazy_transition_index_supports_future_then_past_queries_through_supported_years():
    zone = resolve_timezone(TZID, {TZID: WINDOWS})
    assert datetime(9999, 12, 31, 12, tzinfo=zone).utcoffset() == timedelta(hours=1)
    assert datetime(1995, 10, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=2)
    assert datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1).utcoffset() == timedelta(hours=1)
    assert datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0).utcoffset() == timedelta(hours=2)
    assert datetime(1, 1, 1, 0, tzinfo=zone).utcoffset() == timedelta(hours=1)


def floating_event(**changes):
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 28, 11, tzinfo=zone)
    return replace(Event('floating', 'local', 'Wall time', start, start + timedelta(hours=1), floating_time=True), **changes)


def test_floating_json_restores_wall_fields_in_the_current_viewer_zone(monkeypatch):
    source = floating_event()
    payload = source.to_dict()
    assert payload['start'] == '2026-03-28T11:00:00'
    assert source.timezone_id == ''
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: ZoneInfo('Pacific/Honolulu'))
    restored = Event.from_dict(payload)
    assert restored.start.hour == 11
    assert restored.start.tzinfo == ZoneInfo('Pacific/Honolulu')
    assert restored.to_dict() == payload


def test_floating_until_exclusions_additions_and_detached_ranges_survive_a_viewer_zone_change(monkeypatch):
    source = floating_event(recurrence_rule='FREQ=DAILY;UNTIL=20260331T110000',
                            excluded_dates=('2026-03-29T11:00:00',),
                            recurrence_dates=('2026-04-02T14:00:00',))
    original_key = '2026-03-30T11:00:00'
    override = occurrence(source, key_datetime(original_key, source), original_key)
    override = replace(override, start=override.start.replace(hour=15), end=override.end.replace(hour=16))
    source = replace(source, recurrence_overrides={original_key: override.to_dict()})
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: ZoneInfo('Pacific/Honolulu'))
    restored = Event.from_dict(source.to_dict())
    zone = event_timezone(restored)
    result = expand_series(restored, datetime(2026, 3, 27, tzinfo=zone), datetime(2026, 4, 4, tzinfo=zone))
    assert [(value.start.month, value.start.day, value.start.hour) for value in result] == [
        (3, 28, 11), (3, 30, 15), (3, 31, 11), (4, 2, 14)]
    assert [value.recurrence_id for value in result] == [
        '2026-03-28T11:00:00', original_key, '2026-03-31T11:00:00', '2026-04-02T14:00:00']
    assert canonical_rule(restored.recurrence_rule, restored) == restored.recurrence_rule


def test_floating_rule_also_keeps_nonexistent_clocks_and_nominal_duration():
    source = floating_event(start=datetime(2026, 3, 28, 2, 30, tzinfo=ZoneInfo('Europe/Berlin')),
                            end=datetime(2026, 3, 29, 3, 30, tzinfo=ZoneInfo('Europe/Berlin')),
                            recurrence_rule='FREQ=DAILY;COUNT=3', duration_spec='P1DT1H')
    assert source.duration_spec == 'P1DT1H'
    rules = list(recurrence_set(source))
    assert [(value.day, value.hour, value.minute) for value in rules] == [(28, 2, 30), (29, 2, 30), (30, 2, 30)]
    assert end_for(source, rules[1]).replace(tzinfo=None) == datetime(2026, 3, 30, 3, 30)


def test_floating_schedule_fields_use_wall_times_and_naive_identity_keys():
    source = floating_event()
    dates = [source.start, source.start.replace(day=29), source.start.replace(day=30)]
    assert schedule_fields(dates, floating_time=True) == {'recurrence_rule': 'FREQ=DAILY;INTERVAL=1;COUNT=3', 'recurrence_dates': ()}
    assert recurrence_key(source.start, floating_time=True) == '2026-03-28T11:00:00'


def test_floating_whole_series_move_shifts_wall_until_and_keeps_exception_identities():
    source = floating_event(recurrence_rule='FREQ=DAILY;UNTIL=20260331T110000',
                            excluded_dates=('2026-03-29T11:00:00',))
    edited = replace(source, start=source.start + timedelta(days=1), end=source.end + timedelta(days=1))
    moved = updated_series(source, source, edited)
    assert moved.recurrence_rule == 'FREQ=DAILY;UNTIL=20260401T110000'
    assert moved.excluded_dates == ('2026-03-30T11:00:00',)
    result = expand_series(moved, source.start - timedelta(days=1), source.start + timedelta(days=6))
    assert [(value.start.month, value.start.day, value.start.hour) for value in result] == [
        (3, 29, 11), (3, 31, 11), (4, 1, 11)]


@pytest.mark.parametrize('floating', [False, True])
def test_schedule_draft_regular_changes_preserve_timezone_semantics(floating):
    from debacal.event_schedule import ScheduleDraft
    source = floating_event() if floating else custom_event()
    source = replace(source, recurrence_rule='FREQ=DAILY;COUNT=3')
    draft = ScheduleDraft.from_event(source)
    draft.set_regular('WEEKLY', count=4, ending='until', until=source.start.date() + timedelta(weeks=3))
    candidate = replace(source, **draft.fields(source.start, source.end, prototype=source))
    assert candidate.timezone_definitions == source.timezone_definitions
    assert candidate.floating_time == source.floating_time
    assert candidate.timezone_id == source.timezone_id
    result = expand_series(candidate, source.start - timedelta(days=1), source.start + timedelta(weeks=4))
    assert len(result) == 4
    assert all(item.start.hour == 11 for item in result)


@pytest.mark.parametrize('floating', [False, True])
def test_partition_keeps_scoped_zones_and_floating_until(floating):
    from debacal.series_operations import partition_series
    source = floating_event() if floating else custom_event()
    source = replace(source, recurrence_rule='FREQ=DAILY')
    key = recurrence_key(source.start + timedelta(days=3), floating_time=floating)
    pivot = occurrence(source, key_datetime(key, source), key)
    head, tail = partition_series(source, pivot)
    assert head.timezone_definitions == tail.timezone_definitions == source.timezone_definitions
    assert head.floating_time == tail.floating_time == floating
    result = expand_series(head, source.start - timedelta(days=1), source.start + timedelta(days=6))
    assert len(result) == 3
    assert tail.start.replace(tzinfo=None) == source.start.replace(tzinfo=None) + timedelta(days=3)
    assert ('Z' not in head.recurrence_rule) if floating else head.recurrence_rule.endswith('Z')


def test_floating_compact_sequence_keeps_gap_wall_clocks():
    from debacal.schedule_authoring import expression_event
    source = floating_event(start=datetime(2026, 3, 28, 2, 30, tzinfo=ZoneInfo('Europe/Berlin')),
                            end=datetime(2026, 3, 28, 3, 30, tzinfo=ZoneInfo('Europe/Berlin')))
    candidate = expression_event(source, '+0d +1d +1d')
    result = expand_series(candidate, source.start - timedelta(days=1), source.start + timedelta(days=4))
    assert [(item.start.day, item.start.hour) for item in result] == [(28, 2), (29, 2), (30, 2)]


@pytest.fixture
def event_editors(monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from debacal.event_dialog import EventDialog
    app = QApplication.instance() or QApplication([])
    widgets = []
    monkeypatch.setattr('debacal.event_dialog.local_zone', lambda: ZoneInfo('Europe/Berlin'))
    with Store(':memory:') as store:
        def create(source):
            selected = occurrence(source, source.start) if source.recurring else source
            dialog = EventDialog(store.list_calendars(), selected.start, selected.end,
                                 event=selected, series=source if source.recurring else None, edit_scope='group')
            widgets.append(dialog)
            return dialog
        yield create
        for widget in widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize('floating', [False, True])
@pytest.mark.parametrize('recurring', [False, True])
def test_event_editor_title_edit_keeps_timezone_rules_and_float_semantics(event_editors, floating, recurring):
    source = floating_event() if floating else custom_event()
    if recurring:
        source = replace(source, recurrence_rule='FREQ=DAILY;COUNT=3')
    editor = event_editors(source)
    editor.title.setText('Renamed')
    result = editor.build_events()[0]
    assert result.title == 'Renamed'
    assert result.timezone_definitions == source.timezone_definitions
    assert result.timezone_id == source.timezone_id
    assert result.floating_time == source.floating_time
    assert result.start.replace(tzinfo=None) == source.start.replace(tzinfo=None)


@pytest.mark.parametrize('recurring', [False, True])
def test_floating_spring_gap_remains_editable_in_fields_picker_and_duration(event_editors, recurring):
    zone = ZoneInfo('Europe/Berlin')
    source = floating_event(start=datetime(2026, 3, 29, 2, 30, tzinfo=zone),
                            end=datetime(2026, 3, 29, 3, 30, tzinfo=zone))
    if recurring:
        source = replace(source, recurrence_rule='FREQ=DAILY;COUNT=3')
    editor = event_editors(source)
    editor.title.setText('Renamed floating clock')
    assert editor.value(editor.start).hour == 2
    assert editor.duration.text() == '1h'
    result = editor.build_events()[0]
    assert result.floating_time and result.start.hour == 2 and result.end.hour == 3
    editor.open_range_picker('start')
    picker = editor.range_picker
    assert picker.floating_time and picker.duration == timedelta(hours=1)
    assert picker.set_start(source.start.replace(minute=45))
    assert picker.draft_end.replace(tzinfo=None) == datetime(2026, 3, 29, 3, 45)
    picker.accept()
    editor.duration.setText('2h')
    editor._duration_changed()
    result = editor.build_events()[0]
    assert result.start.replace(tzinfo=None) == datetime(2026, 3, 29, 2, 45)
    assert result.end.replace(tzinfo=None) == datetime(2026, 3, 29, 4, 45)


def test_floating_typed_clock_change_with_same_instant_keeps_wall_duration(event_editors):
    zone = ZoneInfo('Europe/Berlin')
    source = floating_event(start=datetime(2026, 3, 29, 2, 30, tzinfo=zone),
                            end=datetime(2026, 3, 29, 3, 30, tzinfo=zone))
    editor = event_editors(source)
    # These two Berlin clocks have the same represented instant, but are
    # different floating starts; this must still move the one-hour range.
    editor.start.setText('29.3.2026 3:30')
    editor._commit_pending_range()
    result = editor.build_events()[0]
    assert result.start.replace(tzinfo=None) == datetime(2026, 3, 29, 3, 30)
    assert result.end.replace(tzinfo=None) == datetime(2026, 3, 29, 4, 30)


def test_zoned_gap_validation_stays_strict_while_floating_fields_accept_it(event_editors):
    from debacal.date_time_fields import DateTimeField, parse_datetime, validate_datetime
    from debacal.date_time_picker import DateTimeRangePicker
    zone = ZoneInfo('Europe/Berlin')
    missing = datetime(2026, 3, 29, 2, 30, tzinfo=zone)
    end = missing.replace(hour=3)
    # Create the application's Qt instance through the shared fixture.
    event_editors(floating_event())
    with pytest.raises(ValueError, match='does not exist'):
        validate_datetime(missing)
    with pytest.raises(ValueError, match='does not exist'):
        parse_datetime('29.3.2026 2:30', reference=missing)
    with pytest.raises(ValueError, match='does not exist'):
        DateTimeRangePicker(missing, end)
    field = DateTimeField(floating_time=True)
    field.set_value(missing)
    assert field.value().replace(tzinfo=None) == datetime(2026, 3, 29, 2, 30)
    field.setText('29.3.2026 2:35')
    assert field.value().replace(tzinfo=None) == datetime(2026, 3, 29, 2, 35)
    field.close()
    field.deleteLater()
