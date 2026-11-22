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


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_recurring_nominal_duration_with_utc_endpoint_keeps_all_four_ranges(parser):
    start = datetime(2026, 10, 23, 9, tzinfo=ZONE)
    event = Event('nominal', 'local', 'Daily', start, datetime(2026, 10, 24, 7, tzinfo=UTC),
                  duration_spec='P1D', recurrence_rule='FREQ=DAILY;COUNT=4')
    window = (datetime(2026, 10, 22, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC))
    expected = [(datetime(2026, 10, day, begin, tzinfo=UTC), datetime(2026, 10, day + 1, finish, tzinfo=UTC))
                for day, begin, finish in [(23, 7, 7), (24, 7, 8), (25, 8, 8), (26, 8, 8)]]
    def ranges(item):
        return [(v.start.astimezone(UTC), v.end.astimezone(UTC)) for v in expand_event(item, *window)]
    assert ranges(event) == expected
    raw = export_ics([event])
    assert b'DURATION:P1D' in raw and b'DTEND:' not in raw and b'DTEND;' not in raw
    restored, = parser(strip_metadata(raw), 'local')
    assert ranges(restored) == expected


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_late_fold_rdate_master_with_separate_utc_end_keeps_exact_primary_and_rdate_range(parser):
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    event = Event('exact', 'local', 'Exact dates', start, datetime(2026, 10, 25, 3, 30, tzinfo=UTC),
                  recurrence_dates=('2026-10-27T01:30:00+00:00',))
    raw = export_ics([event])
    assert b'DTSTART:20261025T013000Z' in raw
    restored, = parser(strip_metadata(raw), 'local')
    values = expand_event(restored, datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC))
    assert [(v.start.astimezone(UTC), v.end.astimezone(UTC)) for v in values] == [
        (datetime(2026, 10, day, 1, 30, tzinfo=UTC), datetime(2026, 10, day, 3, 30, tzinfo=UTC))
        for day in (25, 27)]


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_standalone_nominal_range_with_independent_end_preserves_end_reminder(parser):
    from debacal.alarms import Alarm, alarm_trigger
    event = Event('mixed', 'local', 'Mixed endpoint', datetime(2026, 10, 24, 9, tzinfo=ZONE),
                  datetime(2026, 10, 25, 8, tzinfo=UTC), duration_spec='P1D',
                  alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END', description='End'),))
    raw = export_ics([event])
    for payload in (raw, strip_metadata(raw)):
        restored, = parser(payload, 'local')
        assert (restored.start.timestamp(), restored.end.timestamp()) == (event.start.timestamp(), event.end.timestamp())
        assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 8, tzinfo=UTC)


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_exact_rdates_apply_nominal_alarm_in_each_occurrence_timezone(parser):
    from debacal.alarms import Alarm, alarm_trigger
    event = Event('exact', 'local', 'Exact dates', datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1),
                  datetime(2026, 10, 25, 3, 30, tzinfo=UTC), recurrence_dates=('2026-10-27T01:30:00+00:00',),
                  alarms=(Alarm('start', 'DISPLAY', '-P1D', description='Start'),))
    expected = [(datetime(2026, 10, day, 1, 30, tzinfo=UTC), datetime(2026, 10, day, 3, 30, tzinfo=UTC),
                 datetime(2026, 10, day-1, firing_hour, 30, tzinfo=UTC)) for day, firing_hour in ((25, 0), (27, 1))]
    def ranges(item):
        values = expand_event(item, datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC))
        return [(v.start.astimezone(UTC), v.end.astimezone(UTC), alarm_trigger(v.alarms[0], v.start, v.end))
                for v in values]
    assert ranges(event) == expected
    wire = export_ics([event])
    for payload in (strip_metadata(wire), wire):
        restored, = parser(payload, 'local')
        assert ranges(restored) == expected


@pytest.mark.parametrize('explicit_dates', [True, False])
def test_exact_occurrence_end_bound_uses_its_own_timezone_in_both_projections(explicit_dates):
    from debacal.alarms import Alarm, alarm_trigger
    from debacal.prepared_recurrence import PreparedSeries
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1) if explicit_dates else datetime(2026, 10, 25, 4, tzinfo=ZONE)
    event = Event('exact', 'local', 'Elapsed range', start, datetime(2026, 10, 25, 3, 30, tzinfo=UTC),
                  recurrence_rule='' if explicit_dates else 'FREQ=DAILY;COUNT=2',
                  recurrence_dates=('2026-10-27T01:30:00+00:00',) if explicit_dates else (),
                  alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END', description='End'),))
    window = (datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC))
    wire = export_ics([event])
    for model in (event, parse_ics(wire, 'local')[0], parse_fallback(strip_metadata(wire), 'local')[0]):
        for values in (expand_event(model, *window), PreparedSeries(model).query(*window)):
            expected = [datetime(2026, 10, d, 3, 30, tzinfo=UTC) for d in (24, 26 if explicit_dates else 25)]
            assert [alarm_trigger(v.alarms[0], v.start, v.end) for v in values] == expected


@pytest.mark.parametrize('start_zone', [ZONE, UTC])
def test_late_fold_end_varies_by_rdate_with_named_or_utc_master_start(start_zone):
    from debacal.alarms import Alarm, alarm_trigger
    start = datetime(2026, 10, 25, 0, 10, tzinfo=UTC).astimezone(start_zone)
    event = Event('end-fold', 'local', 'Exact dates', start, datetime(2026, 10, 25, 2, 45, tzinfo=ZONE, fold=1),
                  recurrence_dates=('2026-10-27T00:10:00+00:00',),
                  alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END', description='End'),))
    raw = export_ics([event])
    window = (datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC))
    for parser in (parse_ics, parse_fallback):
        for data in (raw, strip_metadata(raw)):
            model, = parser(data, 'local')
            if data == raw:
                assert not model.recurrence_overrides
            values = expand_event(model, *window)
            assert [alarm_trigger(v.alarms[0], v.start, v.end) for v in values] == [
                datetime(2026, 10, 24, 0, 45, tzinfo=UTC), datetime(2026, 10, 26, 1, 45, tzinfo=UTC)]


def point_dates():
    from debacal.alarms import Alarm
    point = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    end_alarm = Alarm('end', 'DISPLAY', '-P2W', related='END', description='End')
    return Event('point', 'local', 'Exact points', point.astimezone(ZoneInfo('America/New_York')),
                  point.astimezone(ZONE), recurrence_dates=('2026-11-02T12:00:00+00:00', '2026-11-16T12:00:00+00:00'),
                  alarms=(end_alarm,))


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_point_rdates_use_start_for_the_end_reminder_on_standard_wire(parser):
    from debacal.alarms import alarm_trigger
    window = datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 11, 17, tzinfo=UTC)
    expected = [
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), datetime(2026, 10, 11, 0, 30, tzinfo=UTC)),
        (datetime(2026, 11, 2, 12, tzinfo=UTC), datetime(2026, 10, 19, 11, tzinfo=UTC)),
        (datetime(2026, 11, 16, 12, tzinfo=UTC), datetime(2026, 11, 2, 12, tzinfo=UTC))]
    wire = export_ics([point_dates()])
    for data in (strip_metadata(wire), wire):
        restored, = parser(data, 'local')
        values = expand_event(restored, *window)
        assert [(v.start.astimezone(UTC), v.end.astimezone(UTC), alarm_trigger(v.alarms[0], v.start, v.end))
                for v in values] == [(instant, instant, firing) for instant, firing in expected]
        if data == wire:
            assert not restored.recurrence_overrides
            assert (restored.alarms[0].related, restored.alarms[0].trigger) == ('END', '-P2W')
        else:
            assert restored.alarms[0].related == 'START'
            assert all(v.alarms[0].related == 'START' for v in values)
            assert all(not line.startswith(('DTEND', 'DURATION')) for line in unfolded(data))


@pytest.mark.parametrize('edit,trigger', [
    ('title', datetime(2026, 10, 19, 11, tzinfo=UTC)),
    ('remove', datetime(2026, 10, 19, 12, tzinfo=UTC)),
    ('duration', datetime(2026, 10, 19, 11, tzinfo=UTC)),
    ('trigger', datetime(2026, 11, 2, 11, 55, tzinfo=UTC))])
def test_edited_point_exception_keeps_standard_trigger_ownership_on_reexport(edit, trigger):
    from debacal.alarms import alarm_trigger
    from debacal.ics_resources import _blocks
    header, blocks = _blocks(export_ics([point_dates()]))
    child = next(block for block in blocks if 'RECURRENCE-ID:20261102T120000Z' in block)
    if edit == 'remove':
        blocks.remove(child)
    elif edit == 'duration':
        child.insert(-1, 'DURATION:PT1H')
    else:
        field, replacement = ('SUMMARY:', 'SUMMARY:Edited') if edit == 'title' else ('TRIGGER:', 'TRIGGER:-PT5M')
        child[next(i for i, line in enumerate(child) if line.startswith(field))] = replacement
    raw = folded(['BEGIN:VCALENDAR', *header, *(line for block in blocks for line in block), 'END:VCALENDAR'])
    for parser in (parse_ics, parse_fallback):
        changed, = parser(raw, 'local')
        standard, = parser(strip_metadata(export_ics([changed])), 'local')
        window = datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
        for model in (changed, standard):
            instance, = expand_event(model, *window)
            alarm, = instance.alarms
            assert alarm_trigger(alarm, instance.start, instance.end) == trigger
            assert instance.end.astimezone(UTC) == datetime(2026, 11, 2, 13 if edit == 'duration' else 12, tzinfo=UTC)
            if edit == 'title':
                assert instance.title == 'Edited'
            if model is standard or edit in ('duration', 'trigger'):
                assert alarm.related == 'START'


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_rrule_same_zone_late_end_keeps_exact_duration_and_nominal_end_alarm(parser):
    from debacal.alarms import Alarm, alarm_trigger
    event = Event('daily', 'local', 'Daily', datetime(2026, 10, 25, 2, 10, tzinfo=ZONE),
                  datetime(2026, 10, 25, 2, 45, tzinfo=ZONE, fold=1), recurrence_rule='FREQ=DAILY;COUNT=3',
                  alarms=(Alarm('end', 'DISPLAY', '-P1D', related='END', description='End'),))
    window = datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC)
    wire = export_ics([event])
    for payload in (strip_metadata(wire), wire):
        decoded, = parser(payload, 'local')
        assert decoded.duration_spec == ''
        values = expand_event(decoded, *window)
        assert [v.end.timestamp() - v.start.timestamp() for v in values] == [5700] * 3
        assert [alarm_trigger(v.alarms[0], v.start, v.end) for v in values] == [
            datetime(2026, 10, day, hour, 45, tzinfo=UTC) for day, hour in ((24, 0), (25, 2), (26, 2))]


@pytest.mark.parametrize('rule', ['FREQ=DAILY', 'FREQ=DAILY;COUNT=3'])
def test_independent_late_end_rule_reports_unrepresentable_nominal_alarm_without_modifying_event(rule):
    from debacal.alarms import Alarm
    start = datetime(2026, 10, 25, 0, 10, tzinfo=UTC).astimezone(ZoneInfo('America/New_York'))
    alarm = Alarm('end', 'DISPLAY', '-P1D', related='END', description='End')
    event = Event('independent-end', 'local', 'Independent end', start,
                  datetime(2026, 10, 25, 2, 45, tzinfo=ZONE, fold=1), recurrence_rule=rule, alarms=(alarm,))
    snapshot = event.to_dict()
    with pytest.raises(ValueError, match='independent-end.*elapsed-hour reminder'):
        export_ics([event])
    assert event.to_dict() == snapshot
    for trigger in ('-PT24H', '-P0DT24H'):
        saved = export_ics([replace(event, alarms=(replace(alarm, trigger=trigger),))])
        assert parse_ics(saved, 'local')[0].alarms[0].trigger == trigger


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
@pytest.mark.parametrize('private', [False, True])
def test_edited_rdate_period_uses_standard_start_reminder(parser, private):
    from debacal.alarms import alarm_trigger
    wire = export_ics([point_dates()]).replace(b'RDATE:20261116T120000Z',
                                             b'RDATE;VALUE=PERIOD:20261116T120000Z/PT1H')
    model, = parser(wire if private else strip_metadata(wire), 'local')
    instance, = expand_event(model, datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 17, tzinfo=UTC))
    assert instance.end.astimezone(UTC) == datetime(2026, 11, 16, 13, tzinfo=UTC)
    assert alarm_trigger(instance.alarms[0], instance.start, instance.end) == datetime(2026, 11, 2, 12, tzinfo=UTC)
