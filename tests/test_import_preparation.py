"""Import previews and confirmation use one bounded, read-only snapshot."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import parse_ics_report
from debacal.ics_resources import ParseReport
from debacal.import_preparation import (
    PreparedImport, PreparedSource, PREVIEW_CALENDAR_ID, prepare_sources, remap_calendar,
)
from debacal.import_sources import ImportSource
from debacal.models import Event
from debacal.preferences import Preferences


def calendar(*events):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + '\r\n'.join(events)
            + '\r\nEND:VCALENDAR\r\n').encode()


def event(uid='one', title='Meeting', dates='DTSTART:20261106T090000Z\r\nDTEND:20261106T100000Z', extra=''):
    return ('BEGIN:VEVENT\r\nUID:' + uid + '\r\nSUMMARY:' + title + '\r\n' + dates
            + ('\r\n' + extra if extra else '') + '\r\nEND:VEVENT')


def test_snapshot_is_read_once_and_confirming_does_not_reparse_changed_file(tmp_path):
    original = calendar(event())
    path = tmp_path / 'Invitation.ics'
    path.write_bytes(original)
    snapshot = prepare_sources([ImportSource.from_path(path)])
    path.write_bytes(calendar(event(title='Changed after preview')))
    with patch('debacal.import_preparation.parse_ics_report', side_effect=AssertionError('Parsed twice')), \
         patch.object(Path, 'open', side_effect=AssertionError('Read twice')):
        report = snapshot.items[0].remap('destination')
    assert report == parse_ics_report(original, 'destination')
    assert report.events[0].title == 'Meeting'
    assert snapshot.event_count == 1 and not snapshot.warnings


@pytest.mark.parametrize('dates,extra,detached', [
    ('DTSTART:20261106T090000Z\r\nDTEND:20261106T100000Z', 'RRULE:FREQ=DAILY;COUNT=4',
     'RECURRENCE-ID:20261107T090000Z\r\nDTSTART:20261107T130000Z\r\nDTEND:20261107T140000Z'),
    ('DTSTART;TZID=Europe/Berlin:20261106T090000\r\nDTEND;TZID=Europe/Berlin:20261106T100000',
     'RRULE:FREQ=WEEKLY;COUNT=4\r\nRDATE;TZID=Europe/Berlin:20261108T090000',
     'RECURRENCE-ID;TZID=Europe/Berlin:20261113T090000\r\nDTSTART;TZID=Europe/Berlin:20261113T130000\r\nDTEND;TZID=Europe/Berlin:20261113T140000'),
    ('DTSTART;VALUE=DATE:20261106\r\nDTEND;VALUE=DATE:20261109',
     'RRULE:FREQ=MONTHLY;COUNT=3\r\nEXDATE;VALUE=DATE:20261206',
     'RECURRENCE-ID;VALUE=DATE:20270106\r\nDTSTART;VALUE=DATE:20270107\r\nDTEND;VALUE=DATE:20270110'),
])
@pytest.mark.parametrize('destination', ['destination', 'original'])
def test_remapping_matches_direct_parse_and_preserves_full_resource(dates, extra, detached, destination):
    metadata = ('X-DEBACAL-ID:original-id\r\nX-DEBACAL-CALENDAR:original\r\n'
                'X-DEBACAL-GROUP:choices\r\nX-DEBACAL-CANDIDATE:TRUE\r\n'
                'X-PRIVATE-VALUE:retained\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n'
                'TRIGGER:-PT10M\r\nDESCRIPTION:Reminder\r\nEND:VALARM')
    raw = calendar(event(dates=dates, extra=extra + '\r\n' + metadata),
                   event(title='Different occurrence', dates=detached))
    snapshot = prepare_sources([ImportSource.from_data(raw)])
    actual = snapshot.items[0].remap(destination)
    expected = parse_ics_report(raw, destination)
    assert actual == expected
    assert actual.events[0].raw_ics == snapshot.events[0].raw_ics
    assert 'BEGIN:VALARM' in actual.events[0].raw_ics
    assert 'X-PRIVATE-VALUE:retained' in actual.events[0].raw_ics
    assert all(value['calendar_id'] == destination and value['series_id'] == actual.events[0].id
               for value in actual.events[0].recurrence_overrides.values())
    assert snapshot.events[0].calendar_id == PREVIEW_CALENDAR_ID


def test_failed_files_and_bad_resources_do_not_block_healthy_events(tmp_path):
    malformed = event('bad', dates='DTSTART:20261106T090000Z\r\nDTEND:20261105T100000Z')
    sources = [ImportSource.from_path(tmp_path / 'Missing.ics'),
               ImportSource.from_data(b'not a calendar', label='Invalid input'),
               ImportSource.from_data(calendar(event(), malformed), label='Mixed calendar')]
    snapshot = prepare_sources(sources)
    assert len(snapshot.items) == 3 and snapshot.event_count == 1
    assert snapshot.items[0].error and snapshot.items[1].error
    assert len(snapshot.items[2].report.rejected) == 1
    assert len(snapshot.warnings) == 3
    assert any('Mixed calendar' in warning for warning in snapshot.warnings)
    with pytest.raises(ValueError):
        snapshot.items[0].remap('destination')
    assert snapshot.items[2].remap('destination') == parse_ics_report(calendar(event(), malformed), 'destination')


def test_duplicate_uids_preview_the_last_source_but_keep_both_import_reports():
    snapshot = prepare_sources([ImportSource.from_data(calendar(event(title='First'))),
                                ImportSource.from_data(calendar(event(title='Last')))])
    assert len(snapshot.items) == 2 and snapshot.event_count == 1
    assert snapshot.events[0].title == 'Last'
    assert snapshot.items[0].report.events[0].title == 'First'
    assert snapshot.items[1].report.events[0].title == 'Last'
    assert 'duplicate' in snapshot.warnings[0]


def test_month_preview_expands_current_dates_without_materializing_imported_series():
    raw = calendar(event(dates='DTSTART:20000103T090000Z\r\nDTEND:20000103T100000Z',
                         extra='RRULE:FREQ=WEEKLY;BYDAY=MO'))
    snapshot = prepare_sources([ImportSource.from_data(raw)])
    preferences = Preferences(start_weekday=6)
    assert snapshot.initial_month(preferences, date(2026, 11, 7)) == date(2026, 11, 1)
    page = snapshot.month_page(date(2026, 11, 6), preferences)
    assert page.start == date(2026, 11, 1) and page.end == date(2026, 12, 13)
    assert page.events and all(event.series_id == snapshot.events[0].id for event in page.events)
    assert all(page.start <= event.start.astimezone(ZoneInfo('Europe/Berlin')).date() < page.end
               for event in page.events)
    assert not page.warnings
    assert len(snapshot.items[0].remap('destination').events) == 1
    assert snapshot.events[0].recurrence_rule == 'FREQ=WEEKLY;BYDAY=MO'


def test_preview_limits_dense_series_without_changing_its_import_snapshot():
    snapshot = prepare_sources([ImportSource.from_data(calendar(event(extra='RRULE:FREQ=HOURLY')))])
    page = snapshot.month_page(date(2026, 11, 1), Preferences(start_weekday=0), limit=3)
    assert len(page.events) == 3
    assert 'Preview limited to 3' in page.warnings[0]
    assert snapshot.events[0].recurrence_rule == 'FREQ=HOURLY'
    assert snapshot.event_count == 1


def test_preview_resource_error_is_isolated_from_other_events():
    valid = parse_ics_report(calendar(event()), PREVIEW_CALENDAR_ID).events[0]
    invalid = replace(valid, id='bad', title='Broken series', recurrence_rule='FREQ=BOGUS')
    snapshot = PreparedImport((PreparedSource(0, 'Mixed', ParseReport((invalid, valid))),))
    page = snapshot.month_page(date(2026, 11, 1), Preferences(start_weekday=0))
    assert len(page.events) == 1 and page.events[0].title == 'Meeting'
    assert page.warnings and 'Broken series' in page.warnings[0]


def test_initial_month_uses_actual_dates_and_date_events_ignore_display_zone():
    zone = ZoneInfo('Pacific/Kiritimati')
    start = datetime(2026, 11, 1, tzinfo=zone)
    all_day = Event('all-day', PREVIEW_CALENDAR_ID, 'All-day event', start, start + timedelta(days=1), all_day=True)
    snapshot = PreparedImport((PreparedSource(0, 'Dates', ParseReport((all_day,))),))
    with patch('debacal.import_preparation.local_zone', return_value=ZoneInfo('Etc/GMT+12')):
        assert snapshot.initial_month(Preferences(), date(2026, 11, 1)) == date(2026, 11, 1)
        page = snapshot.month_page(date(2026, 11, 1), Preferences(start_weekday=6))
    assert page.events == (all_day,)


def test_cumulative_preview_bounds_preserve_prior_healthy_sources(monkeypatch):
    first, second = calendar(event()), calendar(event('two'))
    monkeypatch.setattr('debacal.import_preparation.MAX_PENDING_IMPORT_BYTES', len(first) + 1)
    snapshot = prepare_sources([ImportSource.from_data(first), ImportSource.from_data(second)])
    assert snapshot.event_count == 1 and snapshot.items[0].report is not None
    assert 'in total' in snapshot.items[1].error
    monkeypatch.setattr('debacal.import_preparation.MAX_PENDING_IMPORT_BYTES', 60_000_000)
    monkeypatch.setattr('debacal.import_preparation.MAX_IMPORT_EVENTS', 1)
    snapshot = prepare_sources([ImportSource.from_data(first), ImportSource.from_data(second)])
    assert snapshot.event_count == 1 and '50,000' in snapshot.items[1].error


def test_empty_sources_produce_an_empty_calendar_page():
    snapshot = prepare_sources([])
    assert snapshot.event_count == 0 and not snapshot.warnings
    assert snapshot.initial_month(Preferences(), date(2026, 11, 6)) == date(2026, 11, 1)
    assert not snapshot.month_page(date(2026, 11, 1), Preferences()).events


def test_remapping_rejects_a_missing_destination():
    with pytest.raises(ValueError, match='Choose a calendar'):
        remap_calendar(ParseReport(()), '')
