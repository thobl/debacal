"""Global slash TZIDs resolve exact database names without guessing suffixes.

Synthetic regressions for icalendar's public issue-466 producer fixture:
https://github.com/collective/icalendar/issues/466
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics
from debacal.ics_resources import parse_report
from debacal.models import Calendar
from debacal.recurrence import expand_events
from debacal.store import Store
from debacal.timezones import TimezoneError, resolve_timezone
from debacal.vdirsyncer_backend import semantic
from test_calendar_standards_interop import document, fixed_zone, vevent


UTC = timezone.utc


def accepted(raw, full):
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.issues and not report.rejected, [issue.reason for issue in report.issues]
    return list(report.events)


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
@pytest.mark.parametrize('name', ['Europe/Stockholm', 'America/Argentina/Buenos_Aires', 'Asia/Kathmandu', 'UTC'])
def test_global_database_name_survives_import_edit_and_caldav_export(name, full):
    alias = '/' + name
    raw = document(vevent('global-clock', '20261023T110000', '20261023T120000', tzid=alias,
                          extra=['RRULE:FREQ=DAILY;COUNT=5']))
    original, = accepted(raw, full)
    assert alias in original.raw_ics
    assert original.timezone_id == ('' if name == 'UTC' else name)
    expected = [datetime(2026, 10, day, 11, tzinfo=ZoneInfo(name)).astimezone(UTC)
                for day in range(23, 28)]
    with Store(':memory:') as store:
        store.save_calendar(Calendar('synthetic', 'Synthetic', '#123456'))
        store.apply('Import synthetic global timezone', [original])
        edited = replace(store.list_events()[0], title='Edited global appointment')
        store.apply('Rename imported appointment', [edited])
        exported = export_ics(store.list_events())
    restored, = accepted(exported, full)
    assert semantic(restored) == semantic(edited)
    occurrences = expand_events([restored], expected[0] - timedelta(days=1), expected[-1] + timedelta(days=1))
    assert [item.start.astimezone(UTC) for item in occurrences] == expected


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_explicit_global_definition_overrides_database_rules(full):
    alias = '/Europe/Stockholm'
    raw = document(vevent('global-explicit', '20260115T110000', '20260115T120000', tzid=alias),
                   zones=[fixed_zone(alias, '+0300')])
    original, = accepted(raw, full)
    assert original.timezone_id == alias
    assert original.start.astimezone(UTC) == datetime(2026, 1, 15, 8, tzinfo=UTC)
    restored, = accepted(export_ics([replace(original, title='Edited explicit global timezone')]), full)
    assert restored.timezone_id == alias and restored.timezone_definitions == original.timezone_definitions
    assert restored.start.timestamp() == original.start.timestamp()


@pytest.mark.parametrize('full', [False, True], ids=['lexical', 'icalendar'])
def test_invalid_explicit_global_definition_is_not_replaced_by_database_rules(full):
    alias = '/Europe/Stockholm'
    raw = document(vevent('global-invalid', '20260115T110000', '20260115T120000', tzid=alias),
                   zones=[fixed_zone(alias, '+0300').replace('TZOFFSETTO:+0300', 'TZOFFSETTO:invalid')])
    report = parse_report(raw, 'synthetic', use_full=full)
    assert not report.events and len(report.rejected) == 1
    assert alias in report.issues[0].reason


@pytest.mark.parametrize('alias', [
    '//Europe/Stockholm', '/Europe/../Europe/Stockholm', '/Europe/Stockholm/',
    '/unknown.example/Europe/Stockholm', '/Unknown/Stockholm', '/',
])
def test_malformed_or_unrecognized_global_names_remain_recoverable(alias):
    with pytest.raises(TimezoneError):
        resolve_timezone(alias)
    raw = document(vevent('unrecognized-global', '20260115T110000', '20260115T120000', tzid=alias),
                   vevent('healthy-global-peer', '20260115T110000Z', '20260115T120000Z'))
    report = parse_report(raw, 'synthetic')
    assert len(report.events) == len(report.rejected) == 1
    assert report.events[0].ical_uid == 'healthy-global-peer@example.invalid'
    assert alias in report.rejected[0].raw_ics
