"""Synthetic RFC contracts against clocks independent of the importer.

This matrix varies the timezone data and encoding, rather than identifying
particular producers or private calendar exports. Expected instants come from
the operating system's ZoneInfo data, explicit offset arithmetic, or the RFC's
wall-clock/DATE semantics. All paths include persistence and edited export.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from unittest.mock import patch
from zoneinfo import ZoneInfo

from icalendar import Timezone
import pytest

from debacal.ics import export_ics
from debacal.ics_fallback import escaped
from debacal.ics_resources import parse_report
from debacal.models import Calendar
from debacal.recurrence import expand_events
from debacal.store import Store


UTC = timezone.utc


def calendar(*blocks):
    return ('\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0',
                        'PRODID:-//Synthetic RFC Matrix//EN', *blocks,
                        'END:VCALENDAR', ''])).replace('\r\n', '\n').replace('\n', '\r\n')


def event(uid, properties):
    return '\n'.join(['BEGIN:VEVENT', 'UID:' + uid + '@example.invalid',
                      'DTSTAMP:20260101T000000Z', 'SUMMARY:Synthetic appointment',
                      *properties, 'END:VEVENT'])


def accepted(raw, full):
    report = parse_report(raw, 'matrix', use_full=full)
    assert not report.rejected and not report.issues, [issue.reason for issue in report.issues]
    return report.events


def persist_edit_roundtrip(events, full):
    with Store(':memory:') as store:
        store.save_calendar(Calendar('matrix', 'Synthetic', '#456789'))
        store.apply('Import synthetic matrix', events)
        edited = [replace(item, location='Edited synthetic location') for item in store.list_events()]
        store.apply('Edit imported matrix', edited)
        payload = export_ics(store.list_events())
    return accepted(payload, full)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('provider', ['zoneinfo', 'pytz'])
def test_embedded_rules_are_independent_of_the_library_global_provider(provider, full):
    from icalendar.timezone import tzp
    if provider == 'pytz':
        pytest.importorskip('pytz')
    original = tzp.name
    try:
        tzp.use(provider)
        # A database-shaped identifier still belongs to these explicit rules.
        # Third-party clients may select a different global timezone provider.
        definition = '\n'.join([
            'BEGIN:VTIMEZONE', 'TZID:Europe/Berlin', 'BEGIN:STANDARD',
            'DTSTART:19700101T000000', 'TZOFFSETFROM:+0245', 'TZOFFSETTO:+0245',
            'END:STANDARD', 'END:VTIMEZONE',
        ])
        raw = calendar(definition, event('provider-independent', [
            'DTSTART;TZID=Europe/Berlin:20260115T110000',
            'DURATION:PT1H', 'RRULE:FREQ=WEEKLY;COUNT=2',
        ]))
        restored = persist_edit_roundtrip(accepted(raw, full), full)
        found = expand_events(restored, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
        assert [item.start.astimezone(UTC) for item in found] == [
            datetime(2026, 1, day, 8, 15, tzinfo=UTC) for day in (15, 22)
        ]
    finally:
        tzp.use(original)


@lru_cache(maxsize=16)
def explicit_zone(name, identifier):
    # The independent reference is ZoneInfo, not decoding this generated block.
    return Timezone.from_tzinfo(ZoneInfo(name), tzid=identifier,
                               first_date=date(2025, 1, 1), last_date=date(2028, 1, 1)).to_ical().decode()


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('explicit', [False, True], ids=['database-name', 'embedded-observances'])
@pytest.mark.parametrize('name', [
    'Australia/Lord_Howe', 'Pacific/Chatham', 'America/St_Johns',
    'Europe/Dublin', 'Africa/Casablanca', 'Asia/Kathmandu', 'Pacific/Apia',
])
def test_monthly_clock_matrix_matches_reference_through_store_edit_export(name, explicit, full):
    zone = ZoneInfo(name)
    identifier = 'urn:synthetic:matrix:' + name if explicit else name
    blocks = [explicit_zone(name, identifier)] if explicit else []
    expected = []
    for month in range(1, 13):
        start = datetime(2026, month, 15, 11, 17, 31, tzinfo=zone)
        end = start + timedelta(minutes=83)
        blocks.append(event('month-' + str(month), [
            f'DTSTART;TZID="{identifier}":{start:%Y%m%dT%H%M%S}',
            f'DTEND;TZID="{identifier}":{end:%Y%m%dT%H%M%S}',
        ]))
        expected.append((start.astimezone(UTC), end.astimezone(UTC)))
    imported = accepted(calendar(*blocks), full)
    restored = persist_edit_roundtrip(imported, full)
    actual = [(item.start.astimezone(UTC), item.end.astimezone(UTC)) for item in restored]
    assert actual == expected
    assert all(item.location == 'Edited synthetic location' for item in restored)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('name,first', [
    ('Australia/Lord_Howe', date(2026, 4, 3)),
    ('Pacific/Chatham', date(2026, 9, 25)),
    ('America/St_Johns', date(2026, 10, 30)),
    ('Europe/Dublin', date(2026, 10, 23)),
    ('Africa/Casablanca', date(2026, 2, 13)),
])
def test_non_hour_and_negative_dst_recurrence_exceptions_follow_reference(name, first, full):
    zone = ZoneInfo(name)
    identifier = 'urn:synthetic:transitions:' + name
    starts = [datetime.combine(first + timedelta(days=index), datetime.min.time(), zone).replace(hour=9, minute=15)
              for index in range(6)]
    properties = [f'DTSTART;TZID="{identifier}":{starts[0]:%Y%m%dT%H%M%S}',
                  'DURATION:PT90M', 'RRULE:FREQ=DAILY;COUNT=5',
                  f'EXDATE;TZID="{identifier}":{starts[1]:%Y%m%dT%H%M%S}',
                  f'RDATE;TZID="{identifier}":{starts[5]:%Y%m%dT%H%M%S}']
    moved = starts[3] + timedelta(hours=4)
    detached = event('transition-series', [
        f'RECURRENCE-ID;TZID="{identifier}":{starts[3]:%Y%m%dT%H%M%S}',
        f'DTSTART;TZID="{identifier}":{moved:%Y%m%dT%H%M%S}', 'DURATION:PT90M'])
    imported = accepted(calendar(explicit_zone(name, identifier), event('transition-series', properties), detached), full)
    expected = [(value.astimezone(UTC), value.astimezone(UTC) + timedelta(minutes=90))
                for value in [starts[0], starts[2], moved, starts[4], starts[5]]]
    begin, finish = starts[0] - timedelta(days=1), starts[-1] + timedelta(days=2)
    for masters in [imported, persist_edit_roundtrip(imported, full)]:
        actual = expand_events(masters, begin, finish)
        assert [(item.start.astimezone(UTC), item.end.astimezone(UTC)) for item in actual] == expected


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('offset,delta', [
    ('+054116', timedelta(hours=5, minutes=41, seconds=16)),
    ('-003044', -timedelta(minutes=30, seconds=44)),
    ('+1245', timedelta(hours=12, minutes=45)),
    ('-0930', -timedelta(hours=9, minutes=30)),
])
def test_explicit_offsets_retain_minutes_and_seconds(offset, delta, full):
    identifier = 'urn:synthetic:offset:' + offset
    block = '\n'.join(['BEGIN:VTIMEZONE', 'TZID:' + identifier, 'BEGIN:STANDARD',
                       'DTSTART:19700101T000000', 'TZOFFSETFROM:' + offset,
                       'TZOFFSETTO:' + offset, 'END:STANDARD', 'END:VTIMEZONE'])
    data = calendar(block, event('offset', [f'DTSTART;TZID="{identifier}":20261110T110000',
                                           f'DTEND;TZID="{identifier}":20261110T120000']))
    imported = accepted(data, full)
    for item in [imported[0], persist_edit_roundtrip(imported, full)[0]]:
        assert item.start.astimezone(UTC) == datetime(2026, 11, 10, 11, tzinfo=UTC) - delta
        assert item.end.astimezone(UTC) == datetime(2026, 11, 10, 12, tzinfo=UTC) - delta


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('standard,daylight,standard_delta,daylight_delta', [
    ('-0930', '-0845', -timedelta(hours=9, minutes=30), -timedelta(hours=8, minutes=45)),
    ('+1245', '+1345', timedelta(hours=12, minutes=45), timedelta(hours=13, minutes=45)),
    ('+0200', '+0130', timedelta(hours=2), timedelta(hours=1, minutes=30)),
])
@pytest.mark.parametrize('duration', ['P1D', 'PT24H'])
def test_annual_rule_offsets_and_duration_use_declared_positive_or_negative_shift(
        standard, daylight, standard_delta, daylight_delta, duration, full):
    identifier = 'urn:synthetic:annual:' + standard + ':' + daylight
    block = '\n'.join([
        'BEGIN:VTIMEZONE', 'TZID:' + identifier, 'BEGIN:DAYLIGHT',
        'DTSTART:20260308T020000', 'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU',
        'TZOFFSETFROM:' + standard, 'TZOFFSETTO:' + daylight, 'END:DAYLIGHT',
        'BEGIN:STANDARD', 'DTSTART:20261101T030000',
        'RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU', 'TZOFFSETFROM:' + daylight,
        'TZOFFSETTO:' + standard, 'END:STANDARD', 'END:VTIMEZONE'])
    raw = calendar(block, event('annual-shift', [f'DTSTART;TZID="{identifier}":20260307T110000',
                                                'DURATION:' + duration]))
    imported = accepted(raw, full)
    expected_start = datetime(2026, 3, 7, 11, tzinfo=UTC) - standard_delta
    expected_end = (datetime(2026, 3, 8, 11, tzinfo=UTC) - daylight_delta
                    if duration == 'P1D' else expected_start + timedelta(hours=24))
    for item in [imported[0], persist_edit_roundtrip(imported, full)[0]]:
        assert item.start.astimezone(UTC) == expected_start
        assert item.end.astimezone(UTC) == expected_end


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('folded', [False, True])
def test_quoted_identifier_and_caret_parameters_can_be_folded_inside_parameters(full, folded):
    identifier = 'urn:synthetic:clock;room,annex'
    block = '\n'.join(['BEGIN:VTIMEZONE', 'TZID:' + escaped(identifier), 'BEGIN:STANDARD',
                       'DTSTART:19700101T000000', 'TZOFFSETFROM:+0545', 'TZOFFSETTO:+0545',
                       'END:STANDARD', 'END:VTIMEZONE'])
    parameter = f'TZID="{identifier}"'
    if folded:
        parameter = parameter.replace('clock;', 'cl\n\tock;')
    data = calendar(block, event('folded-params', [
        f'DTSTART;X-SYNTHETIC="an ^\'encoded^\' label";{parameter}:20261110T110000',
        f'DTEND;{parameter}:20261110T120000']))
    imported = accepted(data, full)
    restored = persist_edit_roundtrip(imported, full)
    assert restored[0].timezone_id == identifier
    assert restored[0].start.astimezone(UTC) == datetime(2026, 11, 10, 5, 15, tzinfo=UTC)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('viewer', ['Europe/Berlin', 'Pacific/Honolulu'])
@pytest.mark.parametrize('duration', ['P1D', 'PT24H'])
def test_floating_duration_does_not_depend_on_viewer_zone_or_dst(viewer, duration, full):
    zone = ZoneInfo(viewer)
    data = calendar(event('floating-duration', ['DTSTART:20260328T110000', 'DURATION:' + duration]))
    with patch('debacal.ics.local_timezone', return_value=zone), patch('debacal.timeutil.local_zone', return_value=zone):
        imported = accepted(data, full)
        restored = persist_edit_roundtrip(imported, full)
        for item in [imported[0], restored[0]]:
            assert item.floating_time
            assert item.start.replace(tzinfo=None) == datetime(2026, 3, 28, 11)
            assert item.end.replace(tzinfo=None) == datetime(2026, 3, 29, 11)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_date_duration_and_exclusions_remain_dates_through_timezone_changes(full):
    data = calendar(event('date-kind', ['DTSTART;VALUE=DATE:20260328', 'DURATION:P2D',
                                       'RRULE:FREQ=DAILY;COUNT=4', 'EXDATE;VALUE=DATE:20260329']))
    with patch('debacal.ics.local_timezone', return_value=ZoneInfo('Pacific/Kiritimati')):
        imported = accepted(data, full)
    with patch('debacal.ics.local_timezone', return_value=ZoneInfo('Pacific/Honolulu')):
        restored = persist_edit_roundtrip(imported, full)
        occurrences = expand_events(restored, datetime(2026, 3, 27, tzinfo=UTC), datetime(2026, 4, 4, tzinfo=UTC))
    assert [(item.start.date(), item.end.date()) for item in occurrences] == [
        (date(2026, 3, 28), date(2026, 3, 30)),
        (date(2026, 3, 30), date(2026, 4, 1)),
        (date(2026, 3, 31), date(2026, 4, 2)),
    ]


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_rfc_positive_leap_second_uses_permitted_second_59_interpretation(full):
    # RFC 5545 §3.3.5 explicitly recommends this interpretation for clocks
    # without leap-second support; rejecting the entire resource is avoidable.
    imported = accepted(calendar(event('leap-second', ['DTSTART:19970630T235960Z',
                                                       'DTEND:19970701T000100Z'])), full)
    restored = persist_edit_roundtrip(imported, full)
    assert restored[0].start == datetime(1997, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert restored[0].end == datetime(1997, 7, 1, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('invalid', [
    ['DTSTART;VALUE=DATE:20260101', 'DTEND:20260102T010000Z'],
    ['DTSTART:20260101T110000Z', 'DTEND;VALUE=DATE:20260102'],
    ['DTSTART:20260101T110000Z', 'DTSTART:20260101T120000Z', 'DTEND:20260101T130000Z'],
])
def test_invalid_primary_endpoint_types_do_not_invent_a_schedule_or_block_neighbors(full, invalid):
    data = calendar(event('invalid-type', invalid),
                    event('healthy-type', ['DTSTART:20260101T090000Z', 'DTEND:20260101T100000Z']))
    report = parse_report(data, 'matrix', use_full=full)
    assert [item.ical_uid for item in report.events] == ['healthy-type@example.invalid']
    assert len(report.rejected) == 1
    assert report.rejected[0].uid == 'invalid-type@example.invalid'


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_whitespace_only_summary_uses_the_untitled_fallback(full):
    data = calendar(event('blank-title', ['DTSTART:20260101T110000Z', 'DTEND:20260101T120000Z'])
                    .replace('SUMMARY:Synthetic appointment', 'SUMMARY: \t '))
    imported = accepted(data, full)
    assert imported[0].title.strip()
    assert persist_edit_roundtrip(imported, full)[0].title == imported[0].title
