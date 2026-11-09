"""Toggle diagnostics retain each first-window operation and its sub-stages."""
import json
from pathlib import Path
import subprocess
import sys

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
