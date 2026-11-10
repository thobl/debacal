"""Generated VTIMEZONE onsets must match independent native UTC transitions."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar, Timezone
import pytest

from debacal.ics import export_ics, parse_ics
from debacal.ics_fallback import folded
from debacal.ics_resources import _timezone_definition
from debacal.models import Event
from debacal.timezone_generation import normalize_generated_onsets
from debacal.timezones import resolve_timezone


UTC = timezone.utc


def transition_instants(zone, first, last):
    """Find UTC changes using ZoneInfo, independently of the ICS generator."""
    cursor, previous = first, first.astimezone(zone).utcoffset()
    while cursor < last:
        offset = cursor.astimezone(zone).utcoffset()
        if offset != previous:
            low, high = int((cursor - timedelta(hours=6)).timestamp()), int(cursor.timestamp())
            while high - low > 1:
                middle = (low + high) // 2
                if datetime.fromtimestamp(middle, UTC).astimezone(zone).utcoffset() == previous:
                    low = middle
                else:
                    high = middle
            yield datetime.fromtimestamp(high, UTC)
        cursor += timedelta(hours=6)
        previous = offset


@pytest.mark.parametrize('name', ['Europe/Berlin', 'Europe/Dublin', 'Africa/Casablanca',
                                  'Australia/Lord_Howe', 'Pacific/Chatham', 'Pacific/Apia'])
def test_generated_definitions_match_native_utc_clocks_offsets_and_folds(name):
    first, last = date(2025, 1, 1), date(2028, 1, 1)
    original = folded(_timezone_definition(name, first, last)).decode()
    identifier = 'Synthetic/Generated/' + name
    # Use an unrelated identifier so an independent native-name lookup cannot
    # hide wrong embedded rules. The supplied observances are authoritative.
    definition = original.replace('TZID:' + name, 'TZID:' + identifier)
    embedded = resolve_timezone(identifier, {identifier: definition})
    native = ZoneInfo(name)
    samples = [datetime(2026, month, 15, 9, 17, 31, 123456, tzinfo=UTC) for month in range(1, 13)]
    for onset in transition_instants(native, datetime(2025, 1, 2, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC)):
        samples.extend(onset + timedelta(seconds=seconds, microseconds=micros)
                       for seconds in (-3600, -1800, -1, 0, 1, 1800, 3600)
                       for micros in (0, 123456))
    for instant in samples:
        expected, actual = instant.astimezone(native), instant.astimezone(embedded)
        assert actual.replace(tzinfo=None) == expected.replace(tzinfo=None), instant
        assert actual.utcoffset() == expected.utcoffset(), instant
        assert actual.fold == expected.fold, instant
        assert actual.astimezone(UTC) == instant


def test_native_recurring_event_export_has_correct_spring_gap_onset_for_other_clients():
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 28, 3, 30, tzinfo=zone)
    event = Event('generated-onset', 'synthetic', 'Synthetic daily event', start, start + timedelta(hours=1),
                  timezone_id='Europe/Berlin', recurrence_rule='FREQ=DAILY;COUNT=3')
    raw = export_ics([event])
    calendar = ICalendar.from_ical(raw)
    definition = calendar.walk('VTIMEZONE')[0].to_ical().decode()
    identifier = 'Synthetic/ExportedBerlin'
    definition = definition.replace('TZID:Europe/Berlin', 'TZID:' + identifier)
    embedded = resolve_timezone(identifier, {identifier: definition})
    written_start = datetime(2026, 3, 29, 3, 30, tzinfo=embedded)
    assert written_start.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize('onset', ['20260329T020000', '20260329T030000'])
def test_generation_verifies_both_correct_and_post_gap_library_outputs(onset):
    component = Timezone.from_ical(('BEGIN:VTIMEZONE\r\nTZID:Synthetic/Generation\r\n'
        'BEGIN:DAYLIGHT\r\nDTSTART:' + onset + '\r\nTZOFFSETFROM:+0100\r\n'
        'TZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n').encode())
    normalize_generated_onsets(component, ZoneInfo('Europe/Berlin'))
    assert component.subcomponents[0].decoded('DTSTART') == datetime(2026, 3, 29, 2)
    again = component.to_ical()
    assert normalize_generated_onsets(component, ZoneInfo('Europe/Berlin')).to_ical() == again


def test_generation_rejects_an_onset_not_supported_by_the_native_zone():
    component = Timezone.from_ical(b'BEGIN:VTIMEZONE\r\nTZID:Synthetic/Generation\r\n'
        b'BEGIN:DAYLIGHT\r\nDTSTART:20260330T030000\r\nTZOFFSETFROM:+0100\r\n'
        b'TZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n')
    with pytest.raises(ValueError, match='disagrees with its timezone rules'):
        normalize_generated_onsets(component, ZoneInfo('Europe/Berlin'))


def test_imported_explicit_rules_are_not_repaired_to_match_a_database_name():
    # This resource deliberately defines a later transition than Berlin's
    # native rules. Its own DTSTART remains authoritative after an edit.
    raw = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic Import//EN\r\n' \
          b'BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:DAYLIGHT\r\n' \
          b'DTSTART:20260329T030000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\n' \
          b'END:DAYLIGHT\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT\r\nUID:synthetic-explicit\r\n' \
          b'DTSTAMP:20261101T120000Z\r\nDTSTART;TZID=Europe/Berlin:20260410T100000\r\n' \
          b'DTEND;TZID=Europe/Berlin:20260410T110000\r\nSUMMARY:Synthetic\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
    event, = parse_ics(raw, 'synthetic')
    exported = export_ics([replace(event, title='Edited synthetic event')])
    definition = ICalendar.from_ical(exported).walk('VTIMEZONE')[0]
    assert definition.subcomponents[0].decoded('DTSTART') == datetime(2026, 3, 29, 3)


def test_generated_casablanca_includes_short_historical_ramadan_observances():
    name = 'Africa/Casablanca'
    native = ZoneInfo(name)
    definition = folded(_timezone_definition(name, date(1970, 1, 1), date(2101, 1, 1))).decode()
    identifier = 'Synthetic/HistoricalCasablanca'
    definition = definition.replace('TZID:' + name, 'TZID:' + identifier)
    embedded = resolve_timezone(identifier, {identifier: definition})
    onsets = list(transition_instants(native, datetime(2012, 1, 1, tzinfo=UTC),
                                    datetime(2018, 1, 1, tzinfo=UTC)))
    # These years include both spring/autumn changes and Ramadan suspensions
    # shorter than the upstream generator's 64-day transition-search stride.
    assert len(onsets) == 24
    for onset in onsets:
        for delta in (-3600, -1, 0, 1, 3600):
            instant = onset + timedelta(seconds=delta)
            actual, expected = instant.astimezone(embedded), instant.astimezone(native)
            assert actual.replace(tzinfo=None) == expected.replace(tzinfo=None), instant
            assert actual.utcoffset() == expected.utcoffset(), instant
            assert actual.astimezone(UTC) == instant


def test_old_generated_source_keeps_native_model_and_reexports_verified_rules():
    name = 'Europe/Berlin'
    legacy = Timezone.from_tzinfo(ZoneInfo(name), tzid=name,
                                 first_date=date(1970, 1, 1), last_date=date(2101, 1, 1))
    raw = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic Legacy//EN\r\n'
           + legacy.to_ical() + b'\r\nBEGIN:VEVENT\r\nUID:synthetic-legacy\r\n'
           b'DTSTAMP:20261101T120000Z\r\nDTSTART;TZID=Europe/Berlin:20260329T033000\r\n'
           b'DTEND;TZID=Europe/Berlin:20260329T043000\r\n'
           b'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    event, = parse_ics(raw, 'synthetic')
    assert event.timezone_definitions == {}
    assert event.start.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    from debacal.store import Store
    store = Store(':memory:')
    try:
        event = replace(event, calendar_id=store.list_calendars()[0].id)
        store.apply('Import synthetic legacy source', [event])
        saved = store.get_event(event.id)
        assert saved.start.astimezone(UTC) == event.start.astimezone(UTC)
        occurrences = store.query_occurrences(datetime(2026, 3, 29, tzinfo=UTC),
                                              datetime(2026, 4, 1, tzinfo=UTC))
        assert len(occurrences) == 3
        assert all(item.start.astimezone(UTC).hour == 1 for item in occurrences)
        event = saved
    finally:
        store.close()
    calendar = ICalendar.from_ical(export_ics([replace(event, title='Edited synthetic')]))
    definition = calendar.walk('VTIMEZONE')[0].to_ical().decode()
    identifier = 'Synthetic/ReexportedLegacy'
    definition = definition.replace('TZID:' + name, 'TZID:' + identifier)
    embedded = resolve_timezone(identifier, {identifier: definition})
    assert datetime(2026, 3, 29, 3, 30, tzinfo=embedded).astimezone(UTC) == event.start.astimezone(UTC)


def test_a_modified_legacy_generated_definition_remains_explicit():
    name = 'Europe/Berlin'
    legacy = Timezone.from_tzinfo(ZoneInfo(name), tzid=name,
                                 first_date=date(1970, 1, 1), last_date=date(2101, 1, 1))
    definition = legacy.to_ical().decode().replace('TZOFFSETTO:+0200', 'TZOFFSETTO:+0300')
    from debacal.ics_resources import _generated_timezone_coverage
    from debacal.ics_fallback import unfolded
    assert _generated_timezone_coverage(tuple(unfolded(definition))) is None
