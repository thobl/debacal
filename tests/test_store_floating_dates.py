"""Imported DATE values stay on their calendar date across display-zone changes."""
from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import parse_ics
from debacal.models import Event
from debacal.store import Store


def payload(rule=''):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:floating-date\r\n'
            'DTSTART;VALUE=DATE:20261109\r\nDTEND;VALUE=DATE:20261110\r\n'
            'SUMMARY:Floating date\r\n' + (f'RRULE:{rule}\r\n' if rule else '') +
            'END:VEVENT\r\nEND:VCALENDAR\r\n')


@pytest.mark.parametrize('source_zone,view_zone', [('Pacific/Kiritimati', 'Pacific/Honolulu'),
                                                ('Pacific/Honolulu', 'Pacific/Kiritimati')])
@pytest.mark.parametrize('rule', ['', 'FREQ=DAILY;COUNT=3'])
@pytest.mark.parametrize('partial_day', [False, True])
def test_persisted_floating_dates_survive_a_timezone_change(tmp_path, monkeypatch, source_zone, view_zone, rule, partial_day):
    monkeypatch.setenv('TZ', source_zone)
    source, = parse_ics(payload(rule), 'local')
    path = tmp_path / 'calendar.sqlite'
    with Store(path) as store:
        store.apply_remote([source])
    monkeypatch.setenv('TZ', view_zone)
    zone = ZoneInfo(view_zone)
    begin = datetime(2026, 11, 9, 10 if partial_day else 0, tzinfo=zone)
    end = datetime(2026, 11, 9, 11, tzinfo=zone) if partial_day else datetime(2026, 11, 10, tzinfo=zone)
    with Store(path) as store:
        if not rule:
            assert store.list_events(begin, end) == [source]
        found = store.query_occurrences(begin, end)
        assert len(found) == 1
        assert found[0].start.date().isoformat() == '2026-11-09'
        assert found[0].end.date().isoformat() == '2026-11-10'
        assert found[0].all_day
        upcoming = store.upcoming_occurrences(begin, end, limit=1)
        assert len(upcoming) == 1 and upcoming[0].start.date() == begin.date()
        assert not store.query_occurrences(datetime(2026, 11, 8, tzinfo=zone),
                                           datetime(2026, 11, 9, tzinfo=zone))


@pytest.mark.parametrize('rule', ['', 'FREQ=DAILY;COUNT=3'])
def test_upcoming_limits_order_floating_dates_in_the_view_timezone(tmp_path, monkeypatch, rule):
    monkeypatch.setenv('TZ', 'Pacific/Kiritimati')
    source, = parse_ics(payload(rule), 'local')
    zone = ZoneInfo('Pacific/Honolulu')
    start = datetime(2026, 11, 8, 12, tzinfo=zone)
    earlier = Event('earlier', 'local', 'Previous date', start, start + timedelta(hours=1))
    with Store(tmp_path / 'calendar.sqlite') as store:
        store.apply_remote([source, earlier])
        found = store.upcoming_occurrences(datetime(2026, 11, 8, tzinfo=zone),
                                           datetime(2026, 11, 12, tzinfo=zone), limit=1)
        assert [item.id for item in found] == [earlier.id]


def test_a_wholly_skipped_floating_date_is_queryable_without_inventing_a_timed_interval():
    zone = ZoneInfo('Pacific/Apia')
    start, end = datetime(2011, 12, 30, tzinfo=zone), datetime(2011, 12, 31, tzinfo=zone)
    assert start.timestamp() == end.timestamp()
    source = Event('floating', 'local', 'Skipped date', start, end, all_day=True)
    point = Event('point', 'local', 'Actual instant', end, end)
    with Store(':memory:') as store:
        store.apply_remote([source, point])
        assert store.list_events(start, end) == [source]
        assert store.query_occurrences(start, end) == [source]
        assert store.upcoming_occurrences(start, end, limit=1) == [source]


def test_floating_candidates_keep_offscreen_timed_queries_indexed():
    zone = ZoneInfo('UTC')
    start = datetime(2026, 11, 9, tzinfo=zone)
    source = Event('visible', 'local', 'Visible', start, start + timedelta(hours=1))
    archived = [replace(source, id=f'old-{i}', ical_uid=f'old-{i}',
                        start=start - timedelta(days=i + 2),
                        end=start - timedelta(days=i + 2) + timedelta(hours=1)) for i in range(3000)]
    with Store(':memory:') as store:
        store.apply_remote([source, *archived])
        ticks = []
        store.connection.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        try:
            assert store.query_occurrences(start, start + timedelta(days=1)) == [source]
            assert store.upcoming_occurrences(start, start + timedelta(days=7), limit=10) == [source]
        finally:
            store.connection.set_progress_handler(None, 0)
        # A JSON/ordinary-row scan takes tens of thousands of instructions;
        # indexed date/timed candidates should do bounded work on this fixture.
        assert len(ticks) < 50
