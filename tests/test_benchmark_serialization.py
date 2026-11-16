"""Small correctness checks for the optional local snapshot benchmark."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/benchmark_serialization.py'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not in the source archive')


@pytest.fixture
def benchmark():
    specification = importlib.util.spec_from_file_location('snapshot_benchmark', SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_summary_keeps_every_observation_and_its_worst_case(benchmark):
    summary = benchmark.summarize([1, 2, 3, 1000])
    assert summary == {'samples': 4, 'mean_ms': 251.5, 'median_ms': 2.5,
                       'p95_ms': 1000, 'max_ms': 1000}


def test_worker_refuses_to_measure_another_imported_checkout(benchmark, tmp_path):
    args = SimpleNamespace(source_root=tmp_path, prototype=None, iterations=1)
    with pytest.raises(RuntimeError, match='requested checkout'):
        benchmark.worker(args)


def test_worker_checks_each_model_shape_and_preserves_each_raw_sample(benchmark):
    args = SimpleNamespace(source_root=SCRIPT.parents[1], prototype=None, iterations=1)
    report = benchmark.worker(args)
    expected = {f'{case}/{operation}'
                for case in ('ordinary', 'large_rdate_4096', 'nested_overrides_400', 'scoped_timezone')
                for operation in ('snapshot', 'model_roundtrip', 'json_roundtrip')}
    assert set(report['milliseconds']) == expected
    assert all(len(values) == 1 and values[0] >= 0 for values in report['milliseconds'].values())
    assert len(report['environment']['models_sha256']) == 64
    assert report['environment']['prototype_sha256'] is None
