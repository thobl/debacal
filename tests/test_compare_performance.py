"""Performance comparisons reject changed workloads and retain regressions."""
import csv
import json
from pathlib import Path

import pytest

if not (Path(__file__).resolve().parents[1] / 'scripts/compare_performance.py').is_file():
    pytest.skip('Maintainer tooling is not included in the source archive', allow_module_level=True)

from scripts.compare_performance import LABELS, MANDATORY_OPERATIONS, compare_reports, main, markdown


def reports():
    result = {}
    for index, label in enumerate(LABELS):
        duration = (40.0, 20.0, 10.0)[index]
        operations = {name: {'samples': 1 if name == 'startup_workspace' else 2,
                            'mean_ms': duration, 'p95_ms': duration * 2, 'max_ms': duration * 2,
                            'worst_event_loop_gap_ms': duration / 2}
                      for name in MANDATORY_OPERATIONS}
        result[label] = {
            'configuration': {'rounds': 2, 'startup_runs': 2, 'seed': 42, 'events': 20000},
            'harness': {'script_sha256': 'a' * 64, 'generator_sha256': 'b' * 64},
            'plan': [{'blocks': ['navigation', 'preferences']}, {'blocks': ['preferences', 'navigation']}],
            'environment': {'backend': 'xcb', 'font': 'DejaVu Sans', 'window_size': [1400, 900],
                            'screen_size': [1600, 1000], 'qt_style': 'fusion', 'python': '3.14.10',
                            'pyside6': '6.12.0', 'dependency_versions': {'icalendar': '7.3.0'},
                            'cpus_available': 12, 'platform': 'synthetic-kernel', 'profiled': False},
            'source': {'revision': label, 'application_sha256': str(index + 1) * 64},
            'operations': operations,
            'records': [{'operation': name} for name, stats in operations.items() for _ in range(stats['samples'])],
            'startup_samples': [{'process_to_first_paint_ms': duration * 10}] * 2,
            'startup_summary': {'process_to_first_paint_ms': {'samples': 2, 'mean_ms': duration * 10,
                                                           'max_ms': duration * 12}},
            'peak_rss_mib': 300 - index * 30,
            'usage_samples': [{'resident_mib': 200, 'threads': 2, 'open_fds': 10},
                             {'resident_mib': 220 - index * 10, 'threads': 2, 'open_fds': 10}],
        }
    return result


def test_comparison_keeps_three_versions_means_and_maxima_and_memory():
    comparison = compare_reports(reports())
    row = next(row for row in comparison['operations'] if row['operation'] == 'view_year')
    assert row['before_mean_ms'] == 40
    assert row['current_max_ms'] == 40 and row['optimized_max_ms'] == 20
    assert row['current_to_optimized_mean_speedup'] == 2
    assert row['current_to_optimized_max_speedup'] == 2
    assert comparison['memory']['optimized']['peak_rss_mib'] == 240
    assert comparison['startup']['before']['process_to_first_paint_ms']['mean_ms'] == 400
    assert comparison['comparable'] and not comparison['under_sampled']


@pytest.mark.parametrize('field', ['configuration', 'harness', 'plan', 'backend', 'font',
                                  'window_size', 'dependency_versions', 'profiled'])
def test_changed_workload_or_environment_refuses_comparison(field):
    data = reports()
    if field in ('configuration', 'harness', 'plan'):
        data['optimized'][field] = {'changed': True}
    else:
        data['optimized']['environment'][field] = True if field == 'profiled' else 'different'
    with pytest.raises(ValueError):
        compare_reports(data)


def test_missing_interactions_and_record_count_disagreements_are_rejected():
    data = reports()
    del data['before']['operations']['view_year']
    with pytest.raises(ValueError, match='inventory'):
        compare_reports(data)
    data = reports()
    data['current']['operations']['view_year']['samples'] = 3
    with pytest.raises(ValueError, match='sample count'):
        compare_reports(data)


def test_under_sampled_interactions_are_flagged_and_regressions_have_speedup_below_one():
    data = reports()
    data['optimized']['operations']['view_year'].update(samples=1, mean_ms=50, max_ms=80)
    removed = False
    records = []
    for record in data['optimized']['records']:
        if record['operation'] == 'view_year' and not removed:
            removed = True
        else:
            records.append(record)
    data['optimized']['records'] = records
    comparison = compare_reports(data)
    assert not comparison['comparable']
    assert comparison['under_sampled'] == [{'version': 'optimized', 'operation': 'view_year', 'samples': 1, 'required': 2}]
    row = next(row for row in comparison['operations'] if row['operation'] == 'view_year')
    assert row['current_to_optimized_mean_speedup'] == .4
    assert row['current_to_optimized_max_speedup'] == .5


def test_cli_writes_all_rows_and_public_outputs_omit_input_paths(tmp_path):
    data = reports()
    paths = []
    for label in LABELS:
        path = tmp_path / (label + '.json')
        path.write_text(json.dumps(data[label]))
        paths.append(str(path))
    output = tmp_path / 'output'
    assert main([*paths, '--output-dir', str(output), '--markdown']) == 0
    with (output / 'operations.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(MANDATORY_OPERATIONS)
    assert {row['operation'] for row in rows} == MANDATORY_OPERATIONS
    assert 'view_year' in (output / 'all-operations.md').read_text()
    assert str(tmp_path) not in (output / 'comparison.json').read_text()


def test_private_source_labels_are_not_copied_into_public_results():
    data = reports()
    data['before']['source']['revision'] = '/home/private-user/private-checkout'
    with pytest.raises(ValueError, match='public identifiers'):
        compare_reports(data)
