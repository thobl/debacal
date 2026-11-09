"""Measure Store startup, its constituent work, memory, and first queries.

Only generated calendars in temporary files are opened. Uninstrumented latency,
exclusive CPU profiling, and allocation measurements run in separate fresh
processes so profiling cannot improve or distort the latency score.
"""
from __future__ import annotations

import argparse
import cProfile
import inspect
import json
import os
from pathlib import Path
import platform
import pstats
import resource
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
import tracemalloc


ROOT = Path(__file__).resolve().parents[1]
ANCIENT_TIMEZONE = (
    'BEGIN:VTIMEZONE\r\nTZID:Benchmark/UTC\r\nBEGIN:STANDARD\r\n'
    'DTSTART:16010101T030000\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\n'
    'RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\nTZNAME:STD\r\nEND:STANDARD\r\n'
    'BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\n'
    'TZOFFSETTO:+0200\r\nRRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\n'
    'TZNAME:DST\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n')


def resident_bytes():
    """Current resident set on Linux; other platforms retain peak RSS only."""
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def peak_resident_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == 'darwin' else value * 1024


def function_group(filename, name):
    """Exclusive profiler costs: each function belongs to exactly one group."""
    path = filename.replace('\\', '/')
    if path.endswith('/interval_index.py'):
        return 'interval_tree'
    if path.endswith('/prepared_storage.py'):
        return 'prepared_representation'
    if path.endswith('/query_projection.py'):
        return 'projection_storage'
    if path.endswith('/timezones.py') or '/dateutil/tz/' in path or '/icalendar/timezone/' in path:
        return 'timezone'
    if path.endswith('/models.py') or '/copy.py' in path:
        return 'model_and_validation'
    if path.endswith('/prepared_recurrence.py'):
        return 'occurrence_materialization' if name in {'_project', '_explicit', 'finite_rule_occurrences'} else 'recurrence_preparation'
    if path.endswith('/recurrence.py') or path.endswith('/range_policies.py') or path.endswith('/event_duration.py'):
        return 'occurrence_materialization' if name in {'occurrence', 'occurrence_end', 'uncut_occurrence_end'} else 'recurrence_preparation'
    if '/dateutil/rrule.py' in path or path.endswith('/recurrence_budget.py'):
        return 'recurrence_engine'
    if '/json/' in path:
        return 'json_decode'
    if 'sqlite3' in name or '/sqlite3/' in path:
        return 'sqlite'
    if path.endswith('/event_range_index.py'):
        return 'index_orchestration'
    if path.endswith('/store.py'):
        return 'store_iteration_and_bookkeeping'
    return 'other'


def profile_summary(profile):
    stats = pstats.Stats(profile)
    groups, functions, stages = {}, [], []
    for (filename, line, name), (primitive, calls, exclusive, cumulative, _callers) in stats.stats.items():
        group = function_group(filename, name)
        item = groups.setdefault(group, {'exclusive_ms': 0.0, 'calls': 0})
        item['exclusive_ms'] += exclusive * 1000
        item['calls'] += calls
        normalized = filename.replace('\\', '/')
        if '/dateutil/' in normalized:
            normalized = 'dateutil/' + normalized.rsplit('/dateutil/', 1)[1]
        elif '/icalendar/' in normalized:
            normalized = 'icalendar/' + normalized.rsplit('/icalendar/', 1)[1]
        elif '/debacal/' in normalized:
            normalized = 'debacal/' + normalized.rsplit('/debacal/', 1)[1]
        else:
            normalized = Path(filename).name
        function = {'function': f'{normalized}:{line}:{name}', 'calls': calls,
                    'exclusive_ms': round(exclusive * 1000, 3),
                    'inclusive_ms': round(cumulative * 1000, 3)}
        functions.append(function)
        if ((normalized.startswith('debacal/') and name in {
                '__init__', '_synchronize_query_index', 'from_dict', '__post_init__',
                'upsert', 'prepare', 'add', 'from_sorted', 'bulk_load', '_explicit_timezone',
                '_bound_transition_rules', '_explicit', 'finite_rule_occurrences', '_project',
                'event', 'series', 'zone', '_scoped_timezone', '_pack', '_unpack', 'from_records',
                'ensure', 'load_all', 'load_records', 'decode_record', 'load', '_restore'})
                or normalized == '__init__.py' and name == 'loads'):
            stages.append(function)
    for value in groups.values():
        value['exclusive_ms'] = round(value['exclusive_ms'], 3)
    return {'instrumented_ms': round(stats.total_tt * 1000, 3),
            'exclusive_groups': groups,
            'named_stages_inclusive_not_additive': sorted(stages, key=lambda item: item['inclusive_ms'], reverse=True),
            'top_functions': sorted(functions, key=lambda item: item['exclusive_ms'], reverse=True)[:30]}


def open_store(Store, database, *, writer=False, supported=None):
    # The baseline predates this explicit role. Its sync worker opens a full
    # Store, so a writer measurement must honestly retain that old behavior.
    if supported is None:
        supported = 'query_index' in inspect.signature(Store).parameters
    return Store(database, **({'query_index': False} if writer and supported else {})), supported


def worker(source_root, database, request_file, mode, fixture_config=None):
    sys.path.insert(0, str(source_root))
    started = perf_counter()
    from debacal.store import Store
    from scripts.benchmark_ranges import result_rows
    import_ms = (perf_counter() - started) * 1000
    if not Path(inspect.getfile(Store)).resolve().is_relative_to(source_root):
        raise RuntimeError('Requested source checkout was not imported.')
    if mode == 'fixture':
        import scripts.benchmark_ranges as fixture_builder
        data = json.loads(fixture_config.read_text())
        config = fixture_builder.Fixture(**data['fixture'])
        original_zone = fixture_builder.TIMEZONE
        try:
            if data['ancient_timezone']:
                fixture_builder.TIMEZONE = ANCIENT_TIMEZONE
            return {'fixture_write_ms': fixture_builder.build_database(database, config)}
        finally:
            fixture_builder.TIMEZONE = original_zone
    supported = 'query_index' in inspect.signature(Store).parameters
    report = {'mode': mode, 'module_import_ms': round(import_ms, 3),
              'database_bytes_before': database.stat().st_size}
    def finish():
        report['database_bytes_after'] = database.stat().st_size
        report['database_growth_bytes'] = report['database_bytes_after'] - report['database_bytes_before']
        return report
    before_rss = resident_bytes()
    if mode == 'desktop':
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        started = perf_counter()
        from PySide6.QtWidgets import QApplication
        from debacal.ui import MainWindow
        report['qt_and_workspace_import_ms'] = round((perf_counter() - started) * 1000, 3)
        started = perf_counter()
        app = QApplication([])
        app.setStyle('Fusion')
        report['qt_application_ms'] = round((perf_counter() - started) * 1000, 3)
        started = perf_counter()
        store, _ = open_store(Store, database, supported=supported)
        report['store_open_ms'] = round((perf_counter() - started) * 1000, 3)
        window, errors = None, []
        old_hook = sys.excepthook
        sys.excepthook = lambda _kind, error, _trace: errors.append(error)
        try:
            started = perf_counter()
            window = MainWindow(store, enable_sync=False)
            window.resize(1400, 900)
            window.show()
            for _ in range(3):
                app.processEvents()
            if errors:
                raise RuntimeError('Desktop startup raised a Qt callback error.') from errors[0]
            if window.local_issues:
                raise RuntimeError('Desktop startup reported local issues.')
            report['workspace_first_paint_ms'] = round((perf_counter() - started) * 1000, 3)
            report['ready_ms'] = round(sum(report[name] for name in (
                'module_import_ms', 'qt_and_workspace_import_ms', 'qt_application_ms',
                'store_open_ms', 'workspace_first_paint_ms')), 3)
            report['resident_ready_bytes'] = resident_bytes()
        finally:
            sys.excepthook = old_hook
            if window is not None:
                window.close()
                window.executor.shutdown(wait=True, cancel_futures=True)
            app.processEvents()
            store.close()
        return finish()
    if mode == 'profile':
        profile = cProfile.Profile()
        profile.enable()
        store, _ = open_store(Store, database, supported=supported)
        profile.disable()
        report.update(profile_summary(profile))
        store.close()
        return finish()
    if mode == 'memory':
        tracemalloc.start()
        store, _ = open_store(Store, database, supported=supported)
        current, peak = tracemalloc.get_traced_memory()
        report.update(retained_python_bytes=current, peak_python_bytes=peak,
                      resident_before_bytes=before_rss, resident_store_bytes=resident_bytes(),
                      peak_resident_bytes=peak_resident_bytes())
        store.close()
        import gc
        gc.collect()
        report['retained_after_close_python_bytes'] = tracemalloc.get_traced_memory()[0]
        report['resident_after_close_bytes'] = resident_bytes()
        tracemalloc.stop()
        return finish()
    cpu = process_time()
    started = perf_counter()
    store, supported = open_store(Store, database, writer=mode == 'writer', supported=supported)
    report.update(open_ms=round((perf_counter() - started) * 1000, 3),
                  open_cpu_ms=round((process_time() - cpu) * 1000, 3),
                  writer_role_supported=supported,
                  resident_before_bytes=before_rss, resident_store_bytes=resident_bytes())
    try:
        if mode == 'duplicate':
            started = perf_counter()
            writer, supported = open_store(Store, database, writer=True, supported=supported)
            report.update(second_store_ms=round((perf_counter() - started) * 1000, 3),
                          writer_role_supported=supported,
                          resident_two_stores_bytes=resident_bytes())
            writer.close()
        elif mode == 'latency':
            from datetime import datetime
            requests = json.loads(request_file.read_text())
            records = []
            for request in requests:
                first, end = map(datetime.fromisoformat, (request['start'], request['end']))
                started = perf_counter()
                found = store.query_occurrences(first, end, request['calendars'])
                elapsed = (perf_counter() - started) * 1000
                records.append({**request, 'elapsed_ms': round(elapsed, 3), 'rows': result_rows(found)})
            report['queries'] = records
        report['peak_resident_bytes'] = peak_resident_bytes()
    finally:
        store.close()
    return finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=3000)
    parser.add_argument('--series', type=int, default=1200)
    parser.add_argument('--finite', type=int, default=120)
    parser.add_argument('--irrelevant-series', type=int, default=0)
    parser.add_argument('--payload-bytes', type=int, default=2048)
    parser.add_argument('--ancient-timezone', action='store_true',
                        help='Use two annual DST observances from year 1601 instead of a fixed embedded zone.')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--queries', type=int, default=12)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--fixture-source-root', type=Path,
                        help='Build canonical data with another checkout to measure one-time upgrade preparation.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--skip-profile', action='store_true')
    parser.add_argument('--skip-memory', action='store_true')
    parser.add_argument('--desktop', action='store_true',
                        help='Also measure Qt imports, application creation, Store opening and first offscreen paint.')
    parser.add_argument('--max-startup-ms', type=float)
    parser.add_argument('--max-query-ms', type=float)
    parser.add_argument('--max-ready-ms', type=float,
                        help='With --desktop, fail if any measured launch-to-first-paint phase sum exceeds this limit.')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--worker', choices=('latency', 'profile', 'memory', 'writer', 'duplicate', 'desktop', 'fixture'), help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--requests', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--fixture-config', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    if args.worker:
        print(json.dumps(worker(source, args.database, args.requests, args.worker, args.fixture_config)))
        return 0
    if (min(args.events, args.series, args.finite, args.irrelevant_series, args.payload_bytes) < 0
            or args.samples < 1 or not 1 <= args.queries <= 2191 or args.timeout <= 0
            or any(value is not None and value <= 0 for value in (args.max_startup_ms, args.max_query_ms, args.max_ready_ms))):
        parser.error('Counts must be nonnegative; samples, timeout and limits positive; queries 1–2191.')
    if args.max_ready_ms is not None and not args.desktop:
        parser.error('--max-ready-ms requires --desktop.')
    sys.path.insert(0, str(source))
    import scripts.benchmark_ranges as fixture_builder
    from scripts.benchmark_ranges import Fixture, build_database, expected_rows, generated_events, requests
    config = Fixture(args.events, args.series, args.finite, 6, args.payload_bytes, args.irrelevant_series)
    workload = requests(args.queries, config.calendars)
    with TemporaryDirectory(prefix='debacal-startup-benchmark-') as directory:
        directory = Path(directory)
        database, request_file = directory / 'calendar.sqlite3', directory / 'requests.json'
        request_file.write_text(json.dumps(workload))
        original_zone = fixture_builder.TIMEZONE
        try:
            if args.ancient_timezone:
                fixture_builder.TIMEZONE = ANCIENT_TIMEZONE
            if args.fixture_source_root is not None:
                fixture_config = directory / 'fixture.json'
                fixture_config.write_text(json.dumps({'fixture': vars(config), 'ancient_timezone': args.ancient_timezone}))
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), '--worker', 'fixture',
                     '--source-root', str(args.fixture_source_root.resolve()), '--database', str(database),
                     '--fixture-config', str(fixture_config)], capture_output=True, text=True,
                    check=True, timeout=args.timeout)
                fixture_ms = json.loads(result.stdout)['fixture_write_ms']
            else:
                fixture_ms = build_database(database, config)
            events = list(generated_events(config))
            expected = [expected_rows(events, request) for request in workload]
        finally:
            fixture_builder.TIMEZONE = original_zone
        del events
        def run(mode):
            # Every measured process starts with identical durable data. This
            # matters for upgrades: the first worker must not prepare the next
            # worker's index, and no per-date persistent result can help later
            # samples. Copies and cleanup are outside measured startup time.
            with TemporaryDirectory(prefix='sample-', dir=directory) as sample:
                sample_database = Path(sample) / 'calendar.sqlite3'
                shutil.copyfile(database, sample_database)
                command = [sys.executable, str(Path(__file__).resolve()), '--worker', mode,
                           '--source-root', str(source), '--database', str(sample_database), '--requests', str(request_file)]
                result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=args.timeout)
                return json.loads(result.stdout)
        latency = [run('latency') for _ in range(args.samples)]
        for sample in latency:
            if [row['rows'] for row in sample['queries']] != [list(map(list, rows)) for rows in expected]:
                raise AssertionError('Prepared startup changed the complete range-query answer.')
            for row in sample['queries']:
                row['occurrences'] = len(row.pop('rows'))
        report = {'environment': {'python': sys.version.split()[0], 'platform': platform.system(),
                                  'storage': 'file-backed SQLite', 'fresh_process_per_sample': True,
                                  'fresh_database_copy_per_sample': True,
                                  'profile_and_memory_separate': True, 'physical_disk_cache_flushed': False},
                  'fixture': vars(config) | {'ancient_timezone': args.ancient_timezone},
                  'one_time_upgrade': args.fixture_source_root is not None,
                  'template_database_bytes': database.stat().st_size,
                  'fixture_write_ms': round(fixture_ms, 3),
                  'latency_samples': latency, 'writer_sample': run('writer'), 'duplicate_sample': run('duplicate')}
        if not args.skip_profile:
            report['profile'] = run('profile')
        if not args.skip_memory:
            report['memory'] = run('memory')
        if args.desktop:
            report['desktop_samples'] = [run('desktop') for _ in range(args.samples)]
    reader_times = [value['open_ms'] for value in report['latency_samples']]
    reader_times.append(report['duplicate_sample']['open_ms'])
    reader_times.extend(value['store_open_ms'] for value in report.get('desktop_samples', ()))
    report['max_reader_open_ms'] = max(reader_times)
    report['max_writer_open_ms'] = max(report['writer_sample']['open_ms'], report['duplicate_sample']['second_store_ms'])
    report['max_open_ms'] = max(report['max_reader_open_ms'], report['max_writer_open_ms'])
    report['latency_sample_max_open_ms'] = max(value['open_ms'] for value in report['latency_samples'])
    report['max_query_ms'] = max(row['elapsed_ms'] for value in report['latency_samples'] for row in value['queries'])
    report['max_database_growth_bytes'] = max(value['database_growth_bytes'] for value in report['latency_samples'])
    if args.desktop:
        report['max_ready_ms'] = max(value['ready_ms'] for value in report['desktop_samples'])
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + '\n')
    else:
        print(text)
    return int((args.max_startup_ms is not None and report['max_open_ms'] > args.max_startup_ms)
               or (args.max_query_ms is not None and report['max_query_ms'] > args.max_query_ms)
               or (args.max_ready_ms is not None and report['max_ready_ms'] > args.max_ready_ms))


if __name__ == '__main__':
    raise SystemExit(main())
