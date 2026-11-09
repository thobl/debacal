"""Diagnose occasional calendar visibility stalls after unrelated date jumps.

Uses generated calendars only. Every measured operation records inclusive
stage timings, SQL-to-next-statement intervals, garbage collection and Qt
settling. No query warm-up or latency averaging can hide an outlier.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import gc
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event as ThreadEvent, Thread
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_ranges import Fixture, build_database, requests


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def make_workload(iterations, calendars=6):
    return [request for request in requests(min(2191, iterations * 2), calendars)
            if request['kind'] in ('week', 'month')][:iterations]


def worker(database, workload, *, slow_ms=200, writer_hold_ms=0, contention_every=20):
    sys.path.insert(0, str(ROOT))
    from PySide6.QtWidgets import QApplication
    from debacal.store import Store
    from debacal.ui import MainWindow

    app = QApplication([])
    app.setStyle('Fusion')
    store = Store(database)
    window = MainWindow(store, enable_sync=False)
    window.resize(1400, 900)
    window.show()
    errors, records = [], []
    old_hook = sys.excepthook
    sys.excepthook = lambda _kind, error, _trace: errors.append(error)
    active, sql_previous, gc_started = None, None, {}
    writer_jobs, writer_ready, writer_errors = Queue(), ThreadEvent(), []
    writer_finished = None

    def background_writer():
        try:
            with Store(database, query_index=False) as writer:
                writer_ready.set()
                while (job := writer_jobs.get()) is not None:
                    number, acquired, finished = job
                    try:
                        with writer._transaction():
                            # Write the canonical database directly. Preference
                            # APIs may deliberately use a different database.
                            writer.connection.execute(
                                "INSERT INTO settings(key,value) VALUES ('benchmark_writer',?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(number),))
                            acquired.set()
                            ThreadEvent().wait(writer_hold_ms / 1000)
                    finally:
                        finished.set()
        except BaseException as error:
            writer_errors.append(error)
            writer_ready.set()

    writer_thread = Thread(target=background_writer, daemon=True) if writer_hold_ms else None

    def stage(obj, name, label):
        original = getattr(obj, name)
        def observed(*args, **kwargs):
            begin = perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                if active is not None:
                    active['stages'][label]['ms'] += (perf_counter() - begin) * 1000
                    active['stages'][label]['calls'] += 1
        setattr(obj, name, observed)
        return lambda: setattr(obj, name, original)

    def sql_step(statement):
        nonlocal sql_previous
        now = perf_counter()
        if active is None:
            return
        if sql_previous is not None:
            label, begin = sql_previous
            elapsed = (now - begin) * 1000
            active['sql_intervals'][label]['ms'] += elapsed
            active['sql_intervals'][label]['max_ms'] = max(active['sql_intervals'][label]['max_ms'], elapsed)
            active['sql_intervals'][label]['calls'] += 1
        words = statement.lstrip().split(None, 2)
        label = 'BEGIN IMMEDIATE' if words[:2] == ['BEGIN', 'IMMEDIATE'] else words[0].upper() if words else 'OTHER'
        sql_previous = label, now

    def collection(phase, info):
        generation = info['generation']
        if phase == 'start':
            gc_started[generation] = perf_counter()
        elif active is not None and generation in gc_started:
            active['gc'].append({'generation': generation,
                                 'ms': (perf_counter() - gc_started.pop(generation)) * 1000,
                                 'collected': info.get('collected', 0)})

    def settle():
        for _ in range(3):
            app.processEvents()
        if errors:
            raise RuntimeError('Qt callback raised during toggle benchmark.') from errors[0]
        if window.local_issues:
            raise RuntimeError('Generated data produced application issues.')

    def measure(operation, action, metadata):
        nonlocal active, sql_previous
        active = {**metadata, 'operation': operation,
                  'stages': defaultdict(lambda: {'ms': 0.0, 'calls': 0}),
                  'sql_intervals': defaultdict(lambda: {'ms': 0.0, 'max_ms': 0.0, 'calls': 0}), 'gc': []}
        sql_previous = None
        begin = perf_counter()
        action()
        active['action_ms'] = (perf_counter() - begin) * 1000
        if writer_finished is not None:
            active['writer_active_after_action'] = not writer_finished.is_set()
        ready = perf_counter()
        settle()
        active['settle_ms'] = (perf_counter() - ready) * 1000
        active['elapsed_ms'] = (perf_counter() - begin) * 1000
        active['occurrences'] = len(window.visible_events)
        active['slow'] = active['elapsed_ms'] >= slow_ms
        for timing in active['stages'].values():
            timing['ms'] = round(timing['ms'], 3)
        for timing in active['sql_intervals'].values():
            timing['ms'], timing['max_ms'] = round(timing['ms'], 3), round(timing['max_ms'], 3)
        for timing in active['gc']:
            timing['ms'] = round(timing['ms'], 3)
        active['action_ms'] = round(active['action_ms'], 3)
        active['settle_ms'] = round(active['settle_ms'], 3)
        active['elapsed_ms'] = round(active['elapsed_ms'], 3)
        records.append(active)
        active, sql_previous = None, None

    cleanup = []
    collecting, writer_started = False, False
    try:
        settle()
        if writer_thread is not None:
            writer_thread.start()
            writer_started = True
            if not writer_ready.wait(10) or writer_errors:
                raise RuntimeError('Synthetic background writer could not start.') from (writer_errors[0] if writer_errors else None)
        for obj, name, label in (
                (store, 'set_calendar_visible', 'visibility_write'),
                (store, 'query_occurrences', 'range_query'),
                (store, 'upcoming_occurrences', 'upcoming_query'),
                (store, '_synchronize_query_index', 'synchronize_index'),
                (store, '_maintain_query_projections', 'maintain_projection'),
                (store, 'history_availability', 'history_buttons'),
                (window, 'refresh', 'refresh'),
                (window, 'update_surface_data', 'surface_data'),
                (window, 'run_search', 'sidebar'),
                (window, 'sync_selection', 'selection'),
                (window, 'fit_calendar', 'fit_calendar'),
                (window, 'refresh_calendar_list', 'calendar_rows'),
                (window, 'update_sync_status', 'status'),
                (window.canvas, 'set_data', 'week_data'),
                (window.month, 'set_data', 'month_data'),
                (window.all_day_canvas, 'set_data', 'all_day_data')):
            cleanup.append(stage(obj, name, label))
        store.connection.set_trace_callback(sql_step)
        gc.callbacks.append(collection)
        collecting = True
        calendars = [calendar.id for calendar in store.list_calendars() if calendar.id.startswith('cal-')]
        for index, request in enumerate(workload):
            def jump():
                window.view = request['kind']
                window.anchor = datetime.fromisoformat(request['start']).date()
                window.selected_day = window.anchor
                window.refresh()
            metadata = {'iteration': index, 'view': request['kind'], 'start': request['start']}
            measure('jump', jump, metadata)
            identifier = calendars[index % len(calendars)]
            contended = writer_thread is not None and index % contention_every == 0
            if contended:
                acquired, writer_finished = ThreadEvent(), ThreadEvent()
                writer_jobs.put((index, acquired, writer_finished))
                if not acquired.wait(10) or writer_errors:
                    raise RuntimeError('Synthetic background writer did not acquire its transaction.') from (writer_errors[0] if writer_errors else None)
                metadata = metadata | {'canonical_writer_hold_ms': writer_hold_ms}
            measure('hide', lambda: window.toggle_calendar(identifier, False), metadata | {'calendar': identifier})
            measure('show', lambda: window.toggle_calendar(identifier, True), metadata | {'calendar': identifier})
    finally:
        if collecting:
            gc.callbacks.remove(collection)
        store.connection.set_trace_callback(None)
        for restore in reversed(cleanup):
            restore()
        sys.excepthook = old_hook
        window.close()
        window.executor.shutdown(wait=True, cancel_futures=True)
        app.processEvents()
        store.close()
        if writer_started:
            writer_jobs.put(None)
            writer_thread.join(writer_hold_ms / 1000 + 10)
            if writer_thread.is_alive() or writer_errors:
                raise RuntimeError('Synthetic background writer did not finish cleanly.') from (writer_errors[0] if writer_errors else None)
    return {'records': records, 'stage_timings': 'inclusive; SQL intervals include Python gaps',
            'summary': {name: {'max_ms': max(row['elapsed_ms'] for row in records if row['operation'] == name),
                               'slow_count': sum(row['slow'] for row in records if row['operation'] == name)}
                        for name in ('jump', 'hide', 'show')}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=3000)
    parser.add_argument('--series', type=int, default=1200)
    parser.add_argument('--finite', type=int, default=120)
    parser.add_argument('--irrelevant-series', type=int, default=0)
    parser.add_argument('--iterations', type=int, default=200)
    parser.add_argument('--slow-ms', type=float, default=200)
    parser.add_argument('--writer-hold-ms', type=float, default=0,
                        help='Hold a canonical database write transaction during selected toggles.')
    parser.add_argument('--contention-every', type=int, default=20)
    parser.add_argument('--max-ms', type=float, help='Fail if any visibility toggle exceeds this maximum.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--requests', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(worker(args.database, json.loads(args.requests.read_text()), slow_ms=args.slow_ms,
                                writer_hold_ms=args.writer_hold_ms, contention_every=args.contention_every)))
        return 0
    if min(args.events, args.series, args.finite, args.irrelevant_series) < 0 or not 1 <= args.iterations <= 1400:
        parser.error('Counts must be nonnegative and iterations between 1 and 1400.')
    if not 0 <= args.writer_hold_ms <= 5000 or args.contention_every < 1 or args.slow_ms <= 0:
        parser.error('Writer hold must be 0–5000 ms, contention interval and slow threshold positive.')
    if args.max_ms is not None and args.max_ms <= 0:
        parser.error('--max-ms must be positive.')
    sys.path.insert(0, str(ROOT))
    fixture = Fixture(args.events, args.series, args.finite, irrelevant=args.irrelevant_series)
    workload = make_workload(args.iterations, fixture.calendars)
    with TemporaryDirectory(prefix='debacal-toggle-benchmark-') as directory:
        database, request_file = Path(directory) / 'calendar.sqlite3', Path(directory) / 'requests.json'
        build_database(database, fixture)
        request_file.write_text(json.dumps(workload))
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--database', str(database),
                   '--requests', str(request_file), '--slow-ms', str(args.slow_ms),
                   '--writer-hold-ms', str(args.writer_hold_ms), '--contention-every', str(args.contention_every)]
        child = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if child.returncode:
            raise RuntimeError('Toggle worker failed: ' + child.stderr.strip())
        report = json.loads(child.stdout)
    report['fixture'] = fixture.__dict__
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report['summary'], indent=2))
    else:
        print(json.dumps(report, indent=2))
    return int(args.max_ms is not None and any(row['elapsed_ms'] > args.max_ms
               for row in report['records'] if row['operation'] in ('hide', 'show')))


if __name__ == '__main__':
    raise SystemExit(main())
