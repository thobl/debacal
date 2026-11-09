"""Startup scores include first queries and separate diagnostic overhead."""
import json
from pathlib import Path

import pytest

if not (Path(__file__).resolve().parents[1] / 'scripts/benchmark_startup.py').is_file():
    pytest.skip('Maintainer tooling is not included in the source archive', allow_module_level=True)

from scripts.benchmark_ranges import Fixture, build_database, expected_rows, generated_events, requests
from scripts.benchmark_startup import ROOT, function_group, main, open_store, worker


def test_startup_worker_verifies_first_query_answers_and_reports_open_separately(tmp_path):
    fixture = Fixture(20, 10, 2, 6, 128)
    database, request_file = tmp_path / 'calendar.sqlite3', tmp_path / 'requests.json'
    build_database(database, fixture)
    workload = requests(3, fixture.calendars)
    request_file.write_text(json.dumps(workload))
    report = worker(ROOT, database, request_file, 'latency')
    assert report['open_ms'] >= 0 and report['module_import_ms'] >= 0
    assert report['open_cpu_ms'] >= 0
    expected = [expected_rows(list(generated_events(fixture)), request) for request in workload]
    assert [value['rows'] for value in report['queries']] == expected
    assert all(value['elapsed_ms'] >= 0 for value in report['queries'])


def test_benchmark_writer_role_matches_old_and_new_store_interfaces():
    class LegacyStore:
        def __init__(self, database):
            self.database = database
    class RoleStore:
        def __init__(self, database, *, query_index=True):
            self.database, self.query_index = database, query_index
    old, supported = open_store(LegacyStore, 'example', writer=True)
    assert old.database == 'example' and not supported
    new, supported = open_store(RoleStore, 'example', writer=True)
    assert supported and new.database == 'example' and not new.query_index
    reader, _ = open_store(RoleStore, 'example')
    assert reader.query_index


def test_profiler_categories_are_exclusive_and_keep_sql_json_tree_work_distinct():
    assert function_group('/code/debacal/interval_index.py', 'add') == 'interval_tree'
    assert function_group('/python/json/decoder.py', 'raw_decode') == 'json_decode'
    assert function_group('~', "<method 'execute' of 'sqlite3.Connection' objects>") == 'sqlite'
    assert function_group('/code/debacal/prepared_recurrence.py', '_project') == 'occurrence_materialization'
    assert function_group('/code/debacal/prepared_recurrence.py', '__init__') == 'recurrence_preparation'


def test_startup_threshold_checks_worst_fresh_process_in_real_cli(tmp_path):
    output = tmp_path / 'startup.json'
    result = main(['--events', '0', '--series', '0', '--finite', '0', '--samples', '2', '--queries', '3',
                   '--skip-profile', '--skip-memory', '--max-startup-ms', '0.000001', '--output', str(output)])
    report = json.loads(output.read_text())
    assert result == 1
    assert report['environment']['fresh_process_per_sample']
    assert report['environment']['profile_and_memory_separate']
    assert len(report['latency_samples']) == 2
    assert report['max_open_ms'] == max(
        *(value['open_ms'] for value in report['latency_samples']),
        report['writer_sample']['open_ms'], report['duplicate_sample']['open_ms'],
        report['duplicate_sample']['second_store_ms'])
    assert report['max_query_ms'] == max(value['elapsed_ms'] for sample in report['latency_samples'] for value in sample['queries'])


def test_optional_desktop_probe_records_frontend_costs_separately(tmp_path):
    output = tmp_path / 'desktop.json'
    assert main(['--events', '0', '--series', '0', '--finite', '0', '--samples', '1', '--queries', '1',
                 '--skip-profile', '--skip-memory', '--desktop', '--max-ready-ms', '0.000001',
                 '--output', str(output)]) == 1
    report = json.loads(output.read_text())
    desktop = report['desktop_samples'][0]
    names = ('module_import_ms', 'qt_and_workspace_import_ms', 'qt_application_ms',
             'store_open_ms', 'workspace_first_paint_ms')
    assert all(desktop[name] >= 0 for name in names)
    assert desktop['ready_ms'] == round(sum(desktop[name] for name in names), 3)
    assert desktop['ready_ms'] > desktop['store_open_ms']
    assert report['max_ready_ms'] == desktop['ready_ms']


def test_upgrade_fixture_builder_and_each_worker_use_independent_database_files(tmp_path):
    output = tmp_path / 'upgrade.json'
    # The same checkout keeps this protocol test self-contained. Real upgrade
    # measurements pass the immutable pre-change checkout as the fixture root.
    assert main(['--events', '0', '--series', '0', '--finite', '0', '--samples', '2', '--queries', '1',
                 '--skip-profile', '--skip-memory', '--fixture-source-root', str(ROOT),
                 '--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['one_time_upgrade']
    assert report['environment']['fresh_database_copy_per_sample']
    for sample in report['latency_samples']:
        assert sample['database_bytes_before'] == report['template_database_bytes']
        assert sample['database_bytes_after'] > 0
        assert sample['database_growth_bytes'] == sample['database_bytes_after'] - sample['database_bytes_before']
