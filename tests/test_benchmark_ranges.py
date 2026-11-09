"""The performance workload must check first requests and complete answers."""
from datetime import datetime, timedelta, timezone

from debacal.models import Event
from scripts.benchmark_ranges import (
    Fixture, ROOT, build_database, expected_rows, generated_events, main, requests, worker,
)


def test_random_range_workload_has_no_repeated_intervals_and_is_reproducible():
    workload = requests(100, 6)
    intervals = [(value['start'], value['end']) for value in workload]
    assert len(set(intervals)) == len(intervals)
    assert workload == requests(100, 6)
    assert intervals != sorted(intervals)
    assert {value['kind'] for value in workload} == {'day', 'week', 'month'}
    assert all(0 < len(value['calendars']) < 6 for value in workload)


def test_generated_fixture_contains_remote_payloads_and_series_outside_view():
    events = list(generated_events(Fixture(20, 20, 3, 3, 2048)))
    assert all(event.raw_ics.startswith('BEGIN:VCALENDAR') for event in events)
    assert any(event.timezone_definitions for event in events)
    assert any(event.recurrence_overrides for event in events)
    assert any(event.recurrence_dates for event in events)
    assert any(event.start.year < 2020 for event in events)
    assert any(event.start.year > 2040 for event in events)


def test_reference_oracle_handles_exclusions_and_detached_occurrence():
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    moved = start + timedelta(days=10)
    series = Event('series', 'cal-0', 'Example', start, start + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=3',
                   excluded_dates=((start + timedelta(days=1)).isoformat(),),
                   recurrence_overrides={(start + timedelta(days=2)).isoformat(): {
                       'start': moved.isoformat(), 'end': (moved + timedelta(hours=1)).isoformat()}})
    query = {'start': start.isoformat(), 'end': (start + timedelta(days=20)).isoformat(), 'calendars': ['cal-0']}
    assert expected_rows([series], query) == [
        ('series', start.isoformat(), (start + timedelta(hours=1)).isoformat()),
        ('series', moved.isoformat(), (moved + timedelta(hours=1)).isoformat()),
    ]


def test_isolated_query_worker_records_every_first_request_and_checks_complete_data(tmp_path):
    import json
    fixture = Fixture(30, 20, 3, 3, 128)
    path, request_file = tmp_path / 'calendar.sqlite3', tmp_path / 'requests.json'
    build_database(path, fixture)
    workload = requests(3, fixture.calendars)
    request_file.write_text(json.dumps(workload))
    result = worker(ROOT, path, request_file, 'query')
    assert result['preparation_ms'] >= 0
    assert len(result['records']) == len(workload)
    events = list(generated_events(fixture))
    for request, record in zip(workload, result['records']):
        assert record['elapsed_ms'] >= 0
        assert record['rows'] == expected_rows(events, request)
        assert record['occurrences'] == len(record['rows'])
        assert 'decoded_models' in record and 'expanded_sources' in record


def test_query_latency_limit_fails_on_an_individual_slow_operation(tmp_path, capsys):
    import json
    report_path = tmp_path / 'latency.json'
    # A deliberately impossible positive threshold checks the real executable
    # path, workers and exit status, rather than an average-summary helper.
    status = main(['--events', '0', '--series', '0', '--finite', '0', '--queries', '3',
                   '--max-query-ms', '0.000001', '--output', str(report_path)])
    capsys.readouterr()
    report = json.loads(report_path.read_text())
    assert status == 1
    assert max(row['elapsed_ms'] for row in report['phases'][1]['records']) > 0.000001
