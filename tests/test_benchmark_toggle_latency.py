"""Toggle diagnostics retain each first-window operation and its sub-stages."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

if not (Path(__file__).resolve().parents[1] / 'scripts/benchmark_toggle_latency.py').is_file():
    pytest.skip('Maintainer tooling is not included in the source archive', allow_module_level=True)

from scripts.benchmark_toggle_latency import make_workload


def test_workload_has_unique_non_monotonic_week_month_windows():
    rows = make_workload(200)
    intervals = [(row['start'], row['end']) for row in rows]
    assert len(rows) == len(set(intervals)) == 200
    assert {row['kind'] for row in rows} == {'week', 'month'}
    assert rows == make_workload(200)
    assert intervals != sorted(intervals)


def test_cli_records_visibility_write_query_layout_and_collection_stages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'diagnostic.json'
    child = subprocess.run(
        [sys.executable, str(root / 'scripts/benchmark_toggle_latency.py'),
         '--events', '0', '--series', '0', '--finite', '0', '--iterations', '2', '--output', str(output)],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    result = json.loads(output.read_text())
    assert len(result['records']) == 6
    for row in result['records']:
        assert row['elapsed_ms'] >= 0
        assert row['stages']['range_query']['calls'] == 1
        assert row['stages']['upcoming_query']['calls'] == 1
        assert isinstance(row['gc'], list)
        if row['operation'] != 'jump':
            assert row['stages']['visibility_write']['calls'] == 1
    for name in ('jump', 'hide', 'show'):
        assert result['summary'][name]['max_ms'] == max(
            row['elapsed_ms'] for row in result['records'] if row['operation'] == name)


def test_source_archives_skip_benchmarks_without_maintainer_scripts(tmp_path):
    """The release includes tests, but deliberately omits the scripts tree."""
    tests = tmp_path / 'tests'
    tests.mkdir()
    source = Path(__file__).resolve().parent
    for name in ('test_benchmark_ranges.py', 'test_benchmark_startup.py', 'test_benchmark_toggle_latency.py'):
        (tests / name).write_bytes((source / name).read_bytes())
    (tests / 'test_smoke.py').write_text('def test_source_collection():\n    pass\n')
    child = subprocess.run([sys.executable, '-m', 'pytest', '-q', str(tests)],
                           cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
    assert '1 passed, 3 skipped' in child.stdout
