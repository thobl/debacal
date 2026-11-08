"""Bulk calendar decoding shares zones while retaining resource isolation."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal import ics_resources as codec
from debacal.ics import export_ics
from debacal.models import Event
from debacal.recurrence import occurrence, recurrence_key


def mixed_calendar():
    from dataclasses import replace
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 24, 9, tzinfo=zone)
    series = Event('weekly', 'local', 'Shared timezone', start, start + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY;COUNT=3')
    moved_start = start + timedelta(weeks=1)
    detached = replace(occurrence(series, moved_start), start=moved_start + timedelta(hours=2),
                       end=moved_start + timedelta(hours=3), title='Detached occurrence')
    series = replace(series, recurrence_overrides={recurrence_key(moved_start): detached.to_dict()})
    midnight = start.replace(hour=0)
    return export_ics([series, Event('all-day', 'local', 'All day', midnight, midnight + timedelta(days=2), all_day=True),
                       Event('utc', 'local', 'UTC event', start.astimezone(timezone.utc),
                             (start + timedelta(hours=1)).astimezone(timezone.utc))])


@pytest.mark.parametrize('use_full', [False, True])
def test_bulk_decodes_shared_timezones_once_and_matches_isolated_resources(use_full):
    payload = mixed_calendar()
    original = codec._read
    with patch.object(codec, '_read', wraps=original) as decode:
        actual = codec.parse_report(payload, 'local', use_full=use_full)
    assert len(actual.events) == 3 and not actual.issues
    assert decode.call_count == 1

    def isolated(data, full):
        if data is payload:
            raise ValueError('Force the isolated decoder')
        return original(data, full)

    with patch.object(codec, '_read', side_effect=isolated):
        expected = codec.parse_report(payload, 'local', use_full=use_full)
    assert actual == expected
    assert actual.events[0].raw_ics and all('BEGIN:VEVENT' in event.raw_ics for event in actual.events)


@pytest.mark.parametrize('use_full', [False, True])
def test_missing_uid_repair_uses_isolated_decoding_without_redecoding_healthy_peers(use_full):
    payload = mixed_calendar().replace(b'UID:utc\r\n', b'')
    with patch.object(codec, '_read', wraps=codec._read) as decode:
        report = codec.parse_report(payload, 'local', use_full=use_full, repair_missing_uids=True)
    assert len(report.events) == 3 and not report.rejected
    assert len(report.issues) == 1 and 'Missing event UID' in report.issues[0].reason
    assert decode.call_count == 2


@pytest.mark.parametrize('use_full', [False, True])
def test_bad_component_does_not_block_other_resources_when_bulk_decode_fails(use_full):
    payload = mixed_calendar().replace(b'SUMMARY:UTC event\r\n',
        b'SUMMARY:UTC event\r\nRRULE:FREQ=BOGUS\r\n')
    original = codec._read

    def isolated(data, full):
        if data is payload:
            raise ValueError('A backend rejected bulk decoding')
        return original(data, full)

    expected = codec.parse_report(payload, 'local', use_full=use_full)
    with patch.object(codec, '_read', side_effect=isolated):
        actual = codec.parse_report(payload, 'local', use_full=use_full)
    assert actual == expected
    assert len(actual.events) == 2 and [item.uid for item in actual.rejected] == ['utc']
