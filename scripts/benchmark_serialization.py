"""Measure immutable event snapshots and JSON/model round trips in fresh processes.

This local microbenchmark complements the complete-workflow runner. It uses only
synthetic records, preserves per-call timings, and never opens a calendar profile.
The same script can inspect another checkout with --source-root. An optional
prototype module exposing install(Event) supports isolated implementation trials
without changing the application source being compared.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from time import perf_counter_ns


ROOT = Path(__file__).resolve().parents[1]


def summarize(values):
    ordered = sorted(values)
    return {
        'samples': len(values),
        'mean_ms': statistics.fmean(values),
        'median_ms': statistics.median(values),
        'p95_ms': ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        'max_ms': max(values),
    }


def fixtures(Event):
    start = datetime(2026, 11, 4, 9, 17, 31, 123456, tzinfo=timezone.utc)
    ordinary = Event('ordinary', 'synthetic', 'Synthetic appointment, café', start,
                     start + timedelta(minutes=50), location='Synthetic room',
                     description='Synthetic notes\n' * 8)
    large = replace(ordinary, id='large-dates', ical_uid='large-dates',
                    recurrence_dates=tuple((start + timedelta(days=index * 2)).isoformat()
                                           for index in range(1, 4097)))
    overrides = {}
    for index in range(1, 401):
        nominal = start + timedelta(days=index * 3)
        item = replace(ordinary, id=f'nested/{index}', ical_uid='nested',
                       series_id='nested', recurrence_id=nominal.isoformat(),
                       start=nominal + timedelta(hours=1), end=nominal + timedelta(hours=2),
                       description='Synthetic retained exception notes\n' * 8)
        overrides[nominal.isoformat()] = item.to_dict()
    nested = replace(ordinary, id='nested', ical_uid='nested',
                     recurrence_rule='FREQ=DAILY;COUNT=1500', recurrence_overrides=overrides)
    definition = ('BEGIN:VTIMEZONE\r\nTZID:Synthetic/Offset\r\nBEGIN:STANDARD\r\n'
                  'DTSTART:19700101T000000\r\nTZOFFSETFROM:+0130\r\nTZOFFSETTO:+0130\r\n'
                  'END:STANDARD\r\nEND:VTIMEZONE\r\n')
    scoped = replace(ordinary, id='scoped', ical_uid='scoped',
                     timezone_id='Synthetic/Offset',
                     timezone_definitions={'Synthetic/Offset': definition},
                     recurrence_rule='FREQ=WEEKLY;COUNT=12')
    return {'ordinary': ordinary, 'large_rdate_4096': large,
            'nested_overrides_400': nested, 'scoped_timezone': scoped}


def worker(args):
    source = args.source_root.resolve()
    sys.path.insert(0, str(source))
    import debacal.models as models
    from debacal.models import Event
    model_path = Path(models.__file__).resolve()
    if not model_path.is_relative_to(source):
        raise RuntimeError('The requested checkout did not supply the event model.')
    before = fixtures(Event)
    expected = {name: value.to_dict() for name, value in before.items()}
    metadata = {'python': platform.python_version(), 'architecture': platform.machine(),
                'available_cpus': len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count(),
                'models_sha256': hashlib.sha256(model_path.read_bytes()).hexdigest(),
                'prototype_sha256': None}
    if args.prototype is not None:
        path = args.prototype.resolve()
        specification = importlib.util.spec_from_file_location('debacal_serialization_prototype', path)
        if specification is None or specification.loader is None:
            raise RuntimeError('Could not load serialization prototype.')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        module.install(Event)
        metadata['prototype_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    records = {}
    for name, value in before.items():
        if value.to_dict() != expected[name]:
            raise RuntimeError('Serialization changed event semantics for ' + name)
        if list(value.to_dict()) != list(expected[name]):
            raise RuntimeError('Serialization changed snapshot field order for ' + name)
        if Event.from_dict(value.to_dict()).to_dict() != expected[name]:
            raise RuntimeError('Model round trip changed event semantics for ' + name)
        operations = {
            'snapshot': value.to_dict,
            'model_roundtrip': lambda value=value: Event.from_dict(value.to_dict()),
            'json_roundtrip': lambda value=value: Event.from_dict(json.loads(json.dumps(
                value.to_dict(), ensure_ascii=False, separators=(',', ':')))),
        }
        for operation, action in operations.items():
            timings = []
            for _ in range(args.iterations):
                started = perf_counter_ns()
                result = action()
                timings.append((perf_counter_ns() - started) / 1_000_000)
                if result is None:
                    raise RuntimeError('Measured snapshot operation returned no data.')
            records[f'{name}/{operation}'] = timings
    return {'environment': metadata, 'milliseconds': records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--prototype', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.samples < 1:
        parser.error('Iterations and samples must be positive.')
    if args.worker:
        print(json.dumps(worker(args)))
        return 0
    reports = []
    for _ in range(args.samples):
        command = [sys.executable, str(Path(__file__).resolve()), '--worker',
                   '--source-root', str(args.source_root.resolve()), '--iterations', str(args.iterations)]
        if args.prototype is not None:
            command.extend(('--prototype', str(args.prototype.resolve())))
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError('Serialization benchmark worker failed: ' + result.stderr.strip())
        reports.append(json.loads(result.stdout))
    timings = {name: [value for report in reports for value in report['milliseconds'][name]]
               for name in reports[0]['milliseconds']}
    report = {'environment': reports[0]['environment'], 'fresh_processes': args.samples,
              'iterations_per_process': args.iterations, 'milliseconds': timings,
              'summary': {name: summarize(values) for name, values in timings.items()}}
    rendered = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
        print(json.dumps({'output': str(args.output), 'summary': report['summary']}, indent=2))
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
