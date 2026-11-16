"""Small, display-free contracts for the explicitly local annual benchmark."""
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.benchmark_year_presence import configuration, day_counts, issue_values, parser, query_presence, run_worker, summary, year_plan


UTC = timezone.utc


def event(start, end, *, calendar='a', all_day=False):
    return SimpleNamespace(start=start, end=end, calendar_id=calendar, all_day=all_day)


def test_default_fixture_and_plan_match_the_extended_benchmark():
    arguments = parser().parse_args([])
    config = configuration(arguments)
    assert (config.events, config.series, config.finite, config.active_series) == (20000, 2000, 120, 240)
    assert (config.heavy_overrides, config.candidate_dates, config.calendars) == (400, 500, 8)
    assert arguments.samples == 3
    assert arguments.viewer_timezone == 'UTC'
    plan = year_plan(config.seed)
    years = [item['year'] for item in plan]
    assert len(years) == len(set(years)) == 7
    assert years != sorted(years) and years != sorted(years, reverse=True)
    assert all(2019 <= year <= 2035 for year in years)
    assert plan == year_plan(config.seed)
    assert all(datetime.fromisoformat(item['end']).year == item['year'] + 1 for item in plan)


@pytest.mark.parametrize('arguments', [
    ['--samples', '0'], ['--events', '-1'], ['--calendars', '0'],
    ['--candidate-dates', '0'], ['--timeout-seconds', '0'],
    ['--viewer-timezone', 'Benchmark/Unavailable-Zone'],
])
def test_invalid_cli_sizes_fail_before_creating_a_fixture(arguments):
    with pytest.raises(ValueError):
        configuration(parser().parse_args(arguments))


@pytest.mark.parametrize('years', [[], [2026, 2026], [0], [9999]])
def test_invalid_or_repeated_years_cannot_warm_the_same_window(years):
    with pytest.raises(ValueError):
        year_plan(years=years)


def test_custom_year_order_is_preserved_and_statistics_keep_the_worst_case():
    assert [item['year'] for item in year_plan(years=[2031, 2019, 2026])] == [2031, 2019, 2026]
    values = summary([2.0, 40.0, 3.0])
    assert values['samples'] == 3 and values['mean_ms'] == 15.0 and values['max_ms'] == 40.0
    assert summary([])['max_ms'] is None


def test_viewer_timezone_changes_boundaries_without_changing_year_order():
    default = year_plan()
    berlin = year_plan(viewer_timezone='Europe/Berlin')
    assert [request['year'] for request in default] == [request['year'] for request in berlin]
    assert all(datetime.fromisoformat(request['start']).utcoffset() == timedelta(hours=1)
               and datetime.fromisoformat(request['end']).utcoffset() == timedelta(hours=1) for request in berlin)


@pytest.mark.parametrize('mode', ['query', 'fixture'])
def test_worker_receives_viewer_zone_but_fixture_generation_stays_in_utc(tmp_path, monkeypatch, mode):
    captured = {}
    first_line = '{"ready": true}\n' if mode == 'query' else '{"fixture": {}}\n'
    def launch(command, **options):
        captured.update(command=command, **options)
        return SimpleNamespace(stdout=StringIO(first_line), returncode=0,
                               communicate=lambda **_kwargs: ('{"records": [], "validated": true}', None),
                               poll=lambda: 0)
    monkeypatch.setattr('scripts.benchmark_year_presence.subprocess.Popen', launch)
    monkeypatch.setattr('scripts.benchmark_year_presence.select.select', lambda *args: ([args[0][0]], [], []))
    config = configuration(parser().parse_args(['--viewer-timezone', 'Europe/Berlin']))
    run_worker(tmp_path, tmp_path / 'calendar.sqlite3', config, year_plan(), mode,
               tmp_path / 'isolated', viewer_timezone='Europe/Berlin')
    command = captured['command']
    assert command[command.index('--viewer-timezone') + 1] == 'Europe/Berlin'
    assert captured['env']['TZ'] == ('Europe/Berlin' if mode == 'query' else 'UTC')


def test_difference_counter_tracks_dates_points_clipping_and_exclusive_ends():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    stop = first + timedelta(days=4)
    values = [
        event(first - timedelta(days=1), first + timedelta(days=2)),
        event(first + timedelta(days=1, hours=8), first + timedelta(days=1, hours=8)),
        event(first + timedelta(days=2), first + timedelta(days=4), calendar='b', all_day=True),
        event(first + timedelta(days=3, hours=20), first + timedelta(days=5)),
        event(stop, stop),  # A point exactly at the exclusive end is outside.
    ]
    assert day_counts(values, first, stop) == [
        {'a': (1, '')}, {'a': (2, '')}, {'b': (1, '')}, {'a': (1, ''), 'b': (1, '')},
    ]


def test_date_values_keep_civil_days_while_timed_ranges_use_the_viewer_zone():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    offset = timezone(timedelta(hours=14))
    day = datetime(2026, 1, 2, tzinfo=offset)
    values = [event(day, day + timedelta(days=1), all_day=True),
              event(day, day + timedelta(hours=1), calendar='b')]
    assert day_counts(values, first, first + timedelta(days=3)) == [
        {'b': (1, '')}, {'a': (1, '')}, {},
    ]


def test_repeated_hour_and_midnight_end_follow_elapsed_instants():
    zone = ZoneInfo('Europe/Berlin')
    first = datetime(2026, 10, 24, tzinfo=zone)
    event_start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    event_end = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    midnight = datetime(2026, 10, 26, tzinfo=zone)
    values = [event(event_start, event_end), event(event_start, midnight)]
    assert day_counts(values, first, midnight + timedelta(days=1), zone=zone) == [{}, {'a': (2, '')}, {}]


def test_old_sources_use_the_full_event_path_and_preserve_per_source_issues():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    events = [event(first, first + timedelta(hours=1))]
    problem = SimpleNamespace(id='budget-source')
    def query(start, end, calendars, *, issues):
        assert calendars == ['a'] and start == first
        issues.append((problem, ValueError('Synthetic budget issue')))
        return events
    counts, count, issues, implementation = query_presence(SimpleNamespace(query_occurrences=query), first,
                                                          first + timedelta(days=1), ['a'])
    assert counts == [{'a': (1, '')}] and count == 1
    assert implementation == 'full-events-and-counters'
    assert issue_values(issues) == [('budget-source', 'ValueError', 'Synthetic budget issue')]


def test_full_event_summary_uses_the_selected_viewer_zone():
    zone = ZoneInfo('Europe/Berlin')
    first = datetime(2026, 1, 1, tzinfo=zone)
    start = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    events = [event(start, start + timedelta(minutes=15))]
    store = SimpleNamespace(query_occurrences=lambda *args, **kwargs: events)
    counters, count, _issues, _implementation = query_presence(store, first, first + timedelta(days=2), ['a'], zone=zone)
    assert counters == [{}, {'a': (1, '')}] and count == 1


def test_compact_sources_use_counters_without_materializing_events():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    counters = [{'a': (20, '')}]
    class Presence:
        summaries = counters
        def __len__(self):
            return 20
        def __iter__(self):
            raise AssertionError('Compact benchmark must not materialize Events')
    def query(start, end, calendars, *, issues):
        assert not issues
        return Presence()
    store = SimpleNamespace(query_year_presence=query)
    actual, count, issues, implementation = query_presence(store, first, first + timedelta(days=1), ['a'])
    assert actual is counters and count == 20 and not issues
    assert implementation == 'compact-ranges'
