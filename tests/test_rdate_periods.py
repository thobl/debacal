"""RFC 5545 period additions keep individual ranges without flattening a UID."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from debacal.ics import export_ics
from debacal.ics_resources import parse_report
from debacal.models import Calendar, Event
from debacal.recurrence import expand_events, recurrence_key
from debacal.store import Store
from debacal.vdirsyncer_backend import semantic
from test_calendar_standards_interop import (WINDOWS_ID, WINDOWS_ZONE, document,
                                             fixed_zone, vevent, viewer_zone)


UTC = timezone.utc


def accepted(raw, use_full=True):
    report = parse_report(raw, 'synthetic', use_full=use_full)
    assert not report.issues, [issue.reason for issue in report.issues]
    assert not report.rejected
    return report.events[0]


def november_ranges(event):
    values = expand_events([event], datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    return [(item.start.astimezone(UTC), item.end.astimezone(UTC)) for item in values]


@pytest.mark.parametrize('use_full', [False, True])
@pytest.mark.parametrize('period', ['20261111T110000Z/20261111T130000Z', '20261111T110000Z/PT2H'])
def test_period_end_and_duration_keep_master_range_and_plain_additions(use_full, period):
    raw = document(vevent('period-ranges', '20261110T110000Z', '20261110T120000Z', extra=[
        'RDATE;VALUE=PERIOD:' + period + ',20261112T110000Z/PT3H',
        'RDATE:20261113T110000Z']))
    master = accepted(raw, use_full)
    assert master.end - master.start == timedelta(hours=1)
    assert len(master.recurrence_dates) == 3
    assert len(master.recurrence_overrides) == 2
    expected = [(datetime(2026, 11, day, 11, tzinfo=UTC), datetime(2026, 11, day, end_hour, tzinfo=UTC))
                for day, end_hour in [(10, 12), (11, 13), (12, 14), (13, 12)]]
    assert november_ranges(master) == expected
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.import_report(parse_report(raw, 'synthetic', use_full=use_full), calendar_id='synthetic')
        restored, = store.list_events()
        assert november_ranges(restored) == expected
        key = recurrence_key(datetime(2026, 11, 11, 11, tzinfo=UTC))
        child = Event.from_dict(restored.recurrence_overrides[key])
        assert store.get_occurrence(child.id).end.hour == 13
        edited = replace(restored, title='Edited master')
        store.apply('Edit master', [edited])
        exported = export_ics(store.list_events())
    roundtrip = accepted(exported, use_full)
    assert november_ranges(roundtrip) == expected
    assert semantic(roundtrip) == semantic(edited)


@pytest.mark.parametrize('use_full', [False, True])
def test_period_overrides_rrule_duration_but_exdate_and_detached_changes_win(use_full):
    raw = document(vevent('period-precedence', '20261110T110000Z', '20261110T120000Z', extra=[
        'RRULE:FREQ=DAILY;COUNT=5',
        'RDATE;VALUE=PERIOD:20261111T110000Z/PT2H,20261112T110000Z/PT3H,20261113T110000Z/PT4H',
        'EXDATE:20261112T110000Z']),
        vevent('period-precedence', '20261113T150000Z', '20261113T170000Z', extra=[
            'RECURRENCE-ID:20261113T110000Z']))
    master = accepted(raw, use_full)
    expected = [(datetime(2026, 11, day, start, tzinfo=UTC), datetime(2026, 11, day, end, tzinfo=UTC))
                for day, start, end in [(10, 11, 12), (11, 11, 13), (13, 15, 17), (14, 11, 12)]]
    assert november_ranges(master) == expected
    roundtrip = accepted(export_ics([master]), use_full)
    assert november_ranges(roundtrip) == expected
    assert semantic(roundtrip) == semantic(master)


@pytest.mark.parametrize('use_full', [False, True])
def test_partial_detached_details_inherit_the_period_range(use_full):
    raw = document(vevent('partial-period', '20261110T110000Z', '20261110T120000Z', extra=[
        'RDATE;VALUE=PERIOD:20261111T110000Z/PT3H']),
        '\n'.join(['BEGIN:VEVENT', 'UID:partial-period@example.invalid',
                   'RECURRENCE-ID:20261111T110000Z', 'SUMMARY:Changed details only', 'END:VEVENT']))
    master = accepted(raw, use_full)
    child, = expand_events([master], datetime(2026, 11, 11, tzinfo=UTC), datetime(2026, 11, 12, tzinfo=UTC))
    assert child.title == 'Changed details only'
    assert child.end - child.start == timedelta(hours=3)


@pytest.mark.parametrize('use_full', [False, True])
def test_cancelled_period_is_not_reintroduced_on_export(use_full):
    raw = document(vevent('cancelled-period', '20261110T110000Z', '20261110T120000Z', extra=[
        'RDATE;VALUE=PERIOD:20261111T110000Z/PT3H']),
        '\n'.join(['BEGIN:VEVENT', 'UID:cancelled-period@example.invalid',
                   'RECURRENCE-ID:20261111T110000Z', 'STATUS:CANCELLED', 'END:VEVENT']))
    master = accepted(raw, use_full)
    assert len(november_ranges(master)) == 1
    assert november_ranges(accepted(export_ics([master]), use_full)) == november_ranges(master)


@pytest.mark.parametrize('use_full', [False, True])
@pytest.mark.parametrize('duration,expected_hour,elapsed_hours', [('P1D', 11, 25), ('PT24H', 10, 24)])
def test_custom_timezone_period_duration_distinguishes_nominal_days(use_full, duration, expected_hour, elapsed_hours):
    raw = document(vevent('dst-period', '20261022T110000', '20261022T120000', tzid=WINDOWS_ID,
                          extra=[f'RDATE;TZID="{WINDOWS_ID}";VALUE=PERIOD:20261024T110000/{duration}']),
                   zones=[WINDOWS_ZONE])
    master = accepted(raw, use_full)
    child, = expand_events([master], datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC))
    assert child.end.hour == expected_hour
    assert child.end.timestamp() - child.start.timestamp() == elapsed_hours * 3600
    assert child.timezone_id == WINDOWS_ID
    assert child.timezone_definitions[WINDOWS_ID]
    assert master.end - master.start == timedelta(hours=1)
    roundtrip = accepted(export_ics([master]), use_full)
    copied, = expand_events([roundtrip], datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC))
    assert copied.start.timestamp() == child.start.timestamp()
    assert copied.end.timestamp() == child.end.timestamp()
    assert semantic(master) == semantic(roundtrip)


@pytest.mark.parametrize('use_full', [False, True])
def test_period_with_scoped_start_and_utc_endpoint_retains_its_definition(use_full):
    identifier = 'Synthetic/period-zone'
    raw = document(vevent('mixed-endpoint', '20261110T110000Z', '20261110T120000Z', extra=[
        'RRULE:FREQ=DAILY;COUNT=2',
        f'RDATE;TZID={identifier};VALUE=PERIOD:20261112T110000/20261112T090000Z']),
                   zones=[fixed_zone(identifier, '+0300')])
    master = accepted(raw, use_full)
    assert identifier in master.timezone_definitions
    assert november_ranges(master)[-1] == (datetime(2026, 11, 12, 8, tzinfo=UTC), datetime(2026, 11, 12, 9, tzinfo=UTC))
    restored = accepted(export_ics([master]), use_full)
    assert november_ranges(restored) == november_ranges(master)
    assert semantic(restored) == semantic(master)


@pytest.mark.parametrize('duration', ['P1D', 'PT24H', 'PT90M'])
def test_floating_periods_keep_wall_duration_across_display_dst(duration):
    raw = document(vevent('floating-period', '20260327T110000', '20260327T120000', extra=[
        'RDATE;VALUE=PERIOD:20260328T110000/' + duration]))
    with viewer_zone('Europe/Berlin') as zone:
        master = accepted(raw)
        child, = expand_events([master], datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 3, 30, tzinfo=zone))
        expected = timedelta(minutes=90) if duration == 'PT90M' else timedelta(days=1)
        assert child.floating_time
        assert child.end.replace(tzinfo=None) - child.start.replace(tzinfo=None) == expected
        payload = master.to_dict()
    with viewer_zone('Pacific/Honolulu') as zone:
        master = Event.from_dict(payload)
        child, = expand_events([master], datetime(2026, 3, 28, tzinfo=zone), datetime(2026, 3, 30, tzinfo=zone))
        assert child.end.replace(tzinfo=None) - child.start.replace(tzinfo=None) == expected
        assert semantic(accepted(export_ics([master]))) == semantic(master)


@pytest.mark.parametrize('period', ['20261111T110000Z/', '/PT1H', '20261111T110000Z/P0D',
                                  '20261111T110000Z/-PT1H', '20261111T110000Z/20261111T100000Z',
                                  '20261111/20261112', 'not-a-date/PT1H'])
@pytest.mark.parametrize('use_full', [False, True])
def test_invalid_period_is_isolated_and_has_a_specific_safe_reason(use_full, period):
    raw = document(vevent('invalid-period', '20261110T110000Z', '20261110T120000Z', extra=[
        'RDATE;VALUE=PERIOD:' + period]), vevent('healthy', '20261112T110000Z', '20261112T120000Z'))
    report = parse_report(raw, 'synthetic', use_full=use_full)
    assert [item.ical_uid for item in report.events] == ['healthy@example.invalid']
    assert len(report.rejected) == 1
    assert 'RDATE period' in report.issues[0].reason
    assert period not in report.issues[0].reason
    assert 'Unsupported timezone' not in report.issues[0].reason


def test_duplicate_periods_are_deduplicated_but_conflicting_ranges_require_review():
    base = vevent('duplicates', '20261110T110000Z', '20261110T120000Z', extra=[
        'RRULE:FREQ=DAILY;COUNT=2',
        'RDATE;VALUE=PERIOD:20261111T110000Z/PT2H,20261111T110000Z/20261111T130000Z'])
    master = accepted(document(base))
    assert len(master.recurrence_dates) == len(master.recurrence_overrides) == 1
    assert len(november_ranges(master)) == 2
    report = parse_report(document(base.replace('/20261111T130000Z', '/20261111T140000Z')), 'synthetic')
    assert not report.events
    assert 'Conflicting RDATE periods' in report.issues[0].reason


@pytest.mark.parametrize('start,end,period', [
    ('20261110T110000', '20261110T120000', '20261111T110000Z/PT2H'),
    ('20261110T110000Z', '20261110T120000Z', '20261111T110000/PT2H'),
])
def test_mixed_floating_and_fixed_periods_require_review_without_guessing_keys(start, end, period):
    raw = document(vevent('mixed-time-modes', start, end, extra=['RDATE;VALUE=PERIOD:' + period]))
    with viewer_zone('Europe/Berlin'):
        report = parse_report(raw, 'synthetic')
    assert not report.events and len(report.rejected) == 1
    assert 'same floating or fixed time mode' in report.issues[0].reason


def test_period_range_is_visible_when_only_its_far_endpoint_overlaps_the_query():
    raw = document(vevent('long-period', '20261101T110000Z', '20261101T120000Z', extra=[
        'RDATE;VALUE=PERIOD:20261110T110000Z/P10D']))
    master = accepted(raw)
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply_remote([master])
        found, = store.query_occurrences(datetime(2026, 11, 19, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
    assert found.start == datetime(2026, 11, 10, 11, tzinfo=UTC)
    assert found.end == datetime(2026, 11, 20, 11, tzinfo=UTC)


def test_period_pull_series_edit_push_and_repull_do_not_create_a_false_conflict(tmp_path):
    from test_vdirsyncer import setup, sync
    store, calendar, cli, backend = setup(tmp_path)
    try:
        uid = 'period-sync@example.invalid'
        cli.remote[uid] = document(vevent('period-sync', '20261110T110000Z', '20261110T120000Z', extra=[
            'RRULE:FREQ=DAILY;COUNT=3', 'RDATE;VALUE=PERIOD:20261111T110000Z/PT3H']))
        pulled = sync(store, calendar, backend)
        assert not pulled.issues and not pulled.conflicts
        master, = store.list_events()
        before = november_ranges(master)
        store.edit_selection([master], 'group', master, replace(master, title='Edited series'))
        edited, = store.list_events()
        assert november_ranges(edited) == before
        assert all(child.title == 'Edited series' for child in expand_events([edited],
                   datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)))
        pushed = sync(store, calendar, backend)
        assert pushed.sent == 1 and not pushed.issues and not pushed.conflicts
        assert not store.pending_changes()
        again = sync(store, calendar, backend)
        assert not again.conflicts and not again.issues
        restored, = store.list_events()
        assert semantic(restored) == semantic(edited)
        assert november_ranges(restored) == before
    finally:
        store.close()
