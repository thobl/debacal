"""Protocol duration expectations computed independently of Debacal helpers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics, parse_ics_report
from debacal.ics_fallback import folded, parse as parse_fallback, unfolded
from debacal.ics_range_policy import signature
from debacal.models import Event
from debacal.recurrence import expand_event, occurrence, recurrence_key
from debacal.vdirsyncer_backend import semantic


ZONE = ZoneInfo('Europe/Berlin')
UTC = timezone.utc


def document(start, ending, *, rule='FREQ=DAILY;COUNT=3', extra=''):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:duration-test\r\nSUMMARY:Duration test\r\n'
            f'DTSTART;TZID=Europe/Berlin:{start}\r\n{ending}\r\n' +
            (f'RRULE:{rule}\r\n' if rule else '') + extra + 'END:VEVENT\r\nEND:VCALENDAR\r\n').encode()


def strip_metadata(payload):
    return folded(line for line in unfolded(payload) if not line.startswith('X-DEBACAL-'))


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
@pytest.mark.parametrize('start_text,ending,nominal_days,elapsed_seconds', [
    ('20270327T090000', 'DTEND;TZID=Europe/Berlin:20270328T090000', 0, 23 * 3600),
    ('20270327T090000', 'DURATION:PT24H', 0, 24 * 3600),
    ('20270327T090000', 'DURATION:P1D', 1, 0),
    ('20270327T090000', 'DURATION:P1DT2H', 1, 2 * 3600),
    ('20270326T090000', 'DURATION:P1DT24H', 1, 24 * 3600),
    ('20270326T090000', 'DURATION:P2D', 2, 0),
    ('20271030T090000', 'DURATION:P1D', 1, 0),
    ('20271030T090000', 'DTEND;TZID=Europe/Berlin:20271031T090000', 0, 25 * 3600),
])
def test_exact_and_nominal_recurrences_follow_independent_instant_and_calendar_math(
        parser, start_text, ending, nominal_days, elapsed_seconds):
    raw = document(start_text, ending)
    imported, = parser(raw, 'local')
    start = datetime.strptime(start_text, '%Y%m%dT%H%M%S').replace(tzinfo=ZONE)
    expected = []
    for index in range(3):
        occurrence_start = start + timedelta(days=index)
        if nominal_days:
            # Nominal days preserve the local date/clock, then the clock part
            # advances elapsed UTC seconds. No application helper is used.
            day_end = occurrence_start + timedelta(days=nominal_days)
            occurrence_end = day_end.astimezone(UTC) + timedelta(seconds=elapsed_seconds)
        else:
            occurrence_end = occurrence_start.astimezone(UTC) + timedelta(seconds=elapsed_seconds)
        expected.append((occurrence_start.timestamp(), occurrence_end.timestamp()))
    assert bool(imported.duration_spec) == bool(nominal_days)
    window = start - timedelta(days=1), start + timedelta(days=5)
    assert [(item.start.timestamp(), item.end.timestamp()) for item in expand_event(imported, *window)] == expected
    serialized = export_ics([imported])
    if nominal_days:
        assert b'DURATION:' in serialized and b'DTEND:' not in serialized and b'DTEND;' not in serialized
    else:
        assert b'DURATION:' not in serialized
    for payload in (serialized, strip_metadata(serialized)):
        restored, = parser(payload, 'local')
        assert restored.duration_spec == imported.duration_spec
        assert [(item.start.timestamp(), item.end.timestamp()) for item in expand_event(restored, *window)] == expected


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_week_duration_retains_seven_nominal_days_and_not_168_elapsed_hours(parser):
    imported, = parser(document('20270325T090000', 'DURATION:P1W', rule='FREQ=WEEKLY;COUNT=3'), 'local')
    assert imported.duration_spec == 'P7D'
    actual = expand_event(imported, datetime(2027, 3, 1, tzinfo=ZONE), datetime(2027, 5, 1, tzinfo=ZONE))
    assert [(item.start.date().isoformat(), item.end.date().isoformat(), item.end.hour) for item in actual] == [
        ('2027-03-25', '2027-04-01', 9), ('2027-04-01', '2027-04-08', 9), ('2027-04-08', '2027-04-15', 9)]
    assert actual[0].end.timestamp() - actual[0].start.timestamp() == 167 * 3600


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_all_day_duration_uses_floating_dates_and_no_timed_duration_spec(parser):
    raw = document('20270327T090000', 'DURATION:P1D').replace(
        b'DTSTART;TZID=Europe/Berlin:20270327T090000', b'DTSTART;VALUE=DATE:20270327')
    imported, = parser(raw, 'local')
    assert imported.all_day and imported.duration_spec == ''
    assert imported.end.date().isoformat() == '2027-03-28'
    payload = export_ics([imported])
    assert b'DTEND;VALUE=DATE:20270328' in payload and b'DURATION:' not in payload


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_detached_nominal_range_uses_fixed_utc_endpoints_without_false_sync_conflicts(parser):
    master, = parser(document('20270326T090000', 'DURATION:P1D'), 'local')
    when = datetime(2027, 3, 27, 9, tzinfo=ZONE)
    detached = occurrence(master, when)
    master = replace(master, recurrence_overrides={recurrence_key(when): detached.to_dict()})
    raw = export_ics([master])
    assert raw.count(b'DURATION:') == 1  # Only the repeating master retains nominal intent.
    assert b'DTSTART:20270327T080000Z' in raw and b'DTEND:20270328T070000Z' in raw
    restored, = parser(raw, 'local')
    override = Event.from_dict(restored.recurrence_overrides[recurrence_key(when)])
    assert override.duration_spec == ''
    assert override.start.timestamp() == when.timestamp()
    assert override.end.timestamp() == datetime(2027, 3, 28, 9, tzinfo=ZONE).timestamp()
    assert semantic(restored) == semantic(master)


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_single_nominal_event_keeps_named_days_unless_late_ambiguous_start_needs_exact_utc(parser):
    normal, = parser(document('20270327T090000', 'DURATION:P1D', rule=''), 'local')
    raw = export_ics([normal])
    assert b'DTSTART;TZID=Europe/Berlin:' in raw and b'DURATION:P1D' in raw
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    end = datetime(2026, 10, 26, 2, 30, tzinfo=ZONE)
    late = Event('late', 'local', 'Late day', start, end, duration_spec='P1D')
    raw = export_ics([late])
    assert b'DTSTART:20261025T013000Z' in raw and b'DURATION:' not in raw
    restored, = parser(raw, 'local')
    assert restored.start.timestamp() == start.timestamp() and restored.end.timestamp() == end.timestamp()
    assert semantic(restored) == semantic(late)
    annotated = replace(late, schedule_expression='+0d', expression_anchor=start.date().isoformat(), schedule_syntax=2)
    restored, = parser(export_ics([annotated]), 'local')
    assert semantic(restored) == semantic(annotated)


def test_range_policy_cache_signature_distinguishes_nominal_and_exact_duration():
    start = datetime(2027, 1, 28, 9, tzinfo=ZONE)
    nominal = Event('cut', 'local', 'Cut', start, start + timedelta(days=2),
                    recurrence_rule='FREQ=MONTHLY;COUNT=12', range_policy='cut', duration_spec='P2D')
    assert signature(nominal) != signature(replace(nominal, duration_spec=''))


@pytest.mark.parametrize('ending', ['DURATION:P1D', 'DURATION:PT24H',
                                  'DTEND;TZID=Europe/Berlin:20270328T090000'])
def test_sync_baseline_payloads_without_duration_field_normalize_through_retained_source(ending):
    master, = parse_ics(document('20270327T090000', ending), 'local')
    older = master.to_dict()
    older.pop('duration_spec')
    assert semantic(Event.from_dict(older)) == semantic(master)


def test_sync_keeps_nominal_and_exact_recurring_duration_modes_distinct_even_with_same_initial_range():
    start = datetime(2027, 1, 2, 9, tzinfo=ZONE)
    nominal = Event('nominal', 'local', 'One day', start, start + timedelta(days=1),
                    duration_spec='P1D', recurrence_rule='FREQ=DAILY;COUNT=100')
    assert semantic(nominal) != semantic(replace(nominal, duration_spec=''))


def test_standalone_nominal_duration_is_distinct_and_clearing_it_replaces_retained_raw_duration():
    nominal, = parse_ics(document('20270102T090000', 'DURATION:P1D', rule=''), 'local')
    exact = replace(nominal, duration_spec='')
    assert semantic(nominal) != semantic(exact)
    assert exact.raw_ics == nominal.raw_ics and 'DURATION:P1D' in exact.raw_ics
    raw = export_ics([exact])
    assert b'DURATION:' not in raw and b'DTEND:' in raw
    restored, = parse_ics(raw, 'local')
    assert restored.duration_spec == '' and semantic(restored) == semantic(exact)
    assert Event.from_dict(exact.to_dict()).duration_spec == ''


def test_bad_duration_resource_does_not_block_healthy_peers():
    invalid = document('20270327T090000', 'DURATION:PT24H\r\nDTEND:20270328T090000Z')
    healthy = document('20270327T090000', 'DURATION:P1D').replace(b'UID:duration-test', b'UID:healthy')
    combined = invalid[:invalid.index(b'END:VCALENDAR')] + healthy[healthy.index(b'BEGIN:VEVENT'):]
    report = parse_ics_report(combined, 'local')
    assert [item.ical_uid for item in report.events] == ['healthy']
    assert len(report.rejected) == 1
