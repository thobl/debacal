"""Local-only preferences/refresh latency during real synthetic ICS syncing.

Run the same script and options with --source-root BEFORE and AFTER. A separate
loopback HTTP process serves 1,500 synthetic events; the application uses its
normal subscription parser, Store, SyncEngine, and GUI callbacks. The optimized
application's own process isolation is exercised without injecting a backend.
No remote account, credentials, production profile, or external network is used.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import timedelta
import faulthandler
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import select
import statistics
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def fixed_ics(data):
    """Keep generated transport stamps identical across source revisions."""
    return re.sub(rb'(?m)^DTSTAMP:\d{8}T\d{6}Z', b'DTSTAMP:20261104T090000Z', data)


def serve(path):
    """One independent interpreter; an HTTP server thread would share Qt's GIL."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Handler(BaseHTTPRequestHandler):
        sequence = 0
        def do_GET(self):
            if self.path != '/calendar.ics':
                self.send_error(404)
                return
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/calendar; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            Handler.sequence += 1
            print(json.dumps({'served': Handler.sequence, 'bytes': len(payload)}), flush=True)
        def log_message(self, *_values):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    print(json.dumps({'port': server.server_port}), flush=True)
    server.serve_forever()


def statistics_for(records):
    grouped = defaultdict(lambda: defaultdict(list))
    for record in records:
        identity = '/'.join(record[key] for key in ('workload', 'view', 'category', 'action'))
        for name, value in record.items():
            if name.endswith('_ms') and isinstance(value, (int, float)):
                grouped[identity][name].append(value)
    return {identity: {name: {'samples': len(values), 'mean_ms': round(statistics.fmean(values), 3),
                              'max_ms': round(max(values), 3)} for name, values in timings.items()}
            for identity, timings in grouped.items()}


def worker(args):
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'scripts'))
    # The application's child interpreter must import this exact snapshot too.
    os.environ['PYTHONPATH'] = str(root)
    from benchmark_extended import ANCHOR, Config, build_fixture, source_metadata, verify_source
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from debacal.calendar_sources import CalendarSourcesEditor
    from debacal.ics import export_ics, parse_ics_report
    from debacal.models import Calendar, Event
    from debacal.preferences_dialog import PreferencesDialog
    from debacal.store import Store
    from debacal.ui import MainWindow

    directory = args.directory
    config = Config(events=args.events, series=args.series, finite=args.finite,
                    active_series=args.active_series, rounds=1, startup_runs=1)
    database, payload_path = directory / 'calendar.sqlite3', directory / 'subscription.ics'
    fixture = build_fixture(database, config)
    imported = []
    for index in range(args.subscription_events):
        start = ANCHOR + timedelta(days=index % 1000 - 500, minutes=index % 90)
        imported.append(Event(f'subscription-{index}', 'subscription', f'Synthetic remote event {index}',
                              start, start + timedelta(minutes=45), ical_uid=f'synthetic-subscription-{index}',
                              description='Synthetic payload ' * 120,
                              recurrence_rule='FREQ=WEEKLY;COUNT=20' if index % 20 == 0 else ''))
    payload = fixed_ics(export_ics(imported))
    payload_path.write_bytes(payload)
    server = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--server', str(payload_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not select.select([server.stdout], [], [], 15)[0]:
        server.terminate()
        raise RuntimeError('Synthetic loopback HTTP server did not start')
    first = server.stdout.readline()
    if not first:
        raise RuntimeError('Synthetic HTTP server failed: ' + server.stderr.read()[:2000])
    port = json.loads(first)['port']
    with Store(database) as seed:
        seed.save_calendar(Calendar('subscription', 'Synthetic subscription', '#508080', kind='ics',
                                    url=f'http://127.0.0.1:{port}/calendar.ics', read_only=True))
        report = parse_ics_report(payload, 'subscription')
        if report.rejected or len(report.events) != args.subscription_events:
            raise RuntimeError('Synthetic subscription is not a valid import')
        seed.refresh_subscription(report, 'subscription')
    app = QApplication([])
    app.setStyle('Fusion')
    app.setFont(QFont('DejaVu Sans', 10))
    errors, records = [], []
    current, dialog, window, cycle = None, None, None, None
    last_tick = time.perf_counter()

    def pump():
        app.processEvents()
        if errors:
            raise RuntimeError('Unhandled Qt callback: ' + str(errors[0])) from errors[0]

    def error_hook(kind, error, tb):
        if not errors:
            traceback.print_exception(kind, error, tb)
        errors.append(error)

    def tick():
        nonlocal last_tick
        moment = time.perf_counter()
        if current is not None:
            current['heartbeat_gap_ms'] = max(current.get('heartbeat_gap_ms', 0), (moment-last_tick)*1000)
            if current.get('closed_at'):
                current.setdefault('post_close_tick', moment)
        if cycle is not None:
            cycle['heartbeat_gap_ms'] = max(cycle['heartbeat_gap_ms'], (moment-cycle['last_tick'])*1000)
            cycle['last_tick'] = moment
        last_tick = moment

    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(tick)
    heartbeat.start()

    class PaintProbe(QObject):
        def eventFilter(self, watched, event):
            kind = event.type()
            if cycle is not None and kind == QEvent.Type.Paint and watched is window.active_canvas():
                cycle['last_paint'] = time.perf_counter()
            if current is not None and event.type() == QEvent.Type.Paint:
                if watched is dialog and 'first_paint' not in current:
                    current['first_paint'] = time.perf_counter()
                    current['open_cpu_ms'] = (time.thread_time()-current['cpu_started'])*1000
                    QTimer.singleShot(15, interact)
                elif current.get('closed_at') and watched is window.active_canvas():
                    current.setdefault('calendar_paint', time.perf_counter())
            return False
    probe = PaintProbe()

    def instrument(name, action):
        def wrapped(*values, **kwargs):
            begin, cpu = time.perf_counter(), time.thread_time()
            try:
                return action(*(values[:1] if name == 'source_finish' else values), **kwargs)
            finally:
                timing = {'wall_ms': round((time.perf_counter()-begin)*1000, 3),
                          'cpu_ms': round((time.thread_time()-cpu)*1000, 3)}
                if current is not None:
                    current.setdefault('phases', {}).setdefault(name, []).append(timing)
                if name == 'refresh' and cycle is not None:
                    cycle['refreshes'].append(timing)
        return wrapped

    original_init = PreferencesDialog.__init__
    def initialize_dialog(self, *values, **kwargs):
        nonlocal dialog
        original_init(self, *values, **kwargs)
        dialog = self
        self.installEventFilter(probe)
        def destroyed(*_values):
            if current is not None and 'returned_at' in current:
                current['delete_ms'] = (time.perf_counter()-current['returned_at'])*1000
                current['delete_cpu_ms'] = (time.thread_time()-current['return_cpu_tick'])*1000
                current['dialog_destroyed'] = True
        self.destroyed.connect(destroyed)

    def interact():
        if current is None or dialog is None or not isValid(dialog):
            return
        current['open_to_form_ready_ms'] = (time.perf_counter()-current['started'])*1000
        current['closed_at'] = time.perf_counter()
        current['close_cpu_started'] = time.thread_time()
        (dialog.reject if current['action'] == 'cancel' else dialog.save)()

    def await_condition(predicate, timeout=None):
        # A native event loop releases the GUI thread's GIL while idle.
        # Replaying qWait/processEvents in a tight Python loop can instead
        # starve the very CPU/SQLite worker being measured.
        if predicate():
            return
        timeout = args.await_timeout if timeout is None else timeout
        loop, poll, deadline = QEventLoop(), QTimer(), QTimer()
        failures = []
        def check():
            try:
                if errors:
                    raise RuntimeError('Unhandled Qt callback: ' + str(errors[0])) from errors[0]
                if predicate():
                    loop.quit()
            except Exception as error:
                failures.append(error)
                loop.quit()
        def expired():
            faulthandler.dump_traceback(file=sys.stderr)
            failures.append(TimeoutError('Desktop did not complete operation: ' + str({
                'records':len(records), 'queued':window.engine._queued, 'sync_running':window._sync_running,
                'engine_events':window.engine.events.qsize(), 'poll_active':window.poll_timer.isActive(),
                'pending_writes':window.database_commands.pending})))
            loop.quit()
        poll.setInterval(5)
        poll.timeout.connect(check)
        deadline.setSingleShot(True)
        deadline.timeout.connect(expired)
        poll.start()
        deadline.start(timeout*1000)
        loop.exec()
        poll.stop()
        deadline.stop()
        if failures:
            raise failures[0]

    def finish_sync(record):
        nonlocal cycle
        await_condition(lambda: not window.engine._queued and window.engine.events.empty()
                        and not window._sync_running)
        if window._sync_errors or window._sync_issues:
            raise RuntimeError('Synthetic subscription sync reported an issue')
        completed = time.perf_counter()
        window.active_canvas().update()
        await_condition(lambda: cycle.get('last_paint',0) >= completed)
        tick()
        record.update(sync_cycle_heartbeat_gap_ms=round(cycle['heartbeat_gap_ms'],3),
                      sync_cycle_elapsed_ms=round((time.perf_counter()-cycle['started'])*1000,3),
                      sync_cycle_refresh_max_ms=max((value['wall_ms'] for value in cycle['refreshes']), default=0),
                      sync_cycle_refresh_count=len(cycle['refreshes']))
        if args.progress:
            with args.progress.open('a') as stream:
                stream.write(json.dumps({'stage':'cycle_complete',**record})+'\n')
        cycle = None

    def start_sync():
        nonlocal cycle
        moment = time.perf_counter()
        cycle = {'started':moment,'last_tick':moment,'heartbeat_gap_ms':0,'refreshes':[]}
        window.engine.sync_now()
        # Read after the separate server has written the entire HTTP body.
        # The real application is then parsing/applying, rather than waiting
        # for an artificially held lock or an HTTP-server thread sharing Qt.
        await_condition(lambda: bool(select.select([server.stdout], [], [], 0)[0]))
        served = json.loads(server.stdout.readline())
        if not served.get('served'):
            raise RuntimeError('Unexpected synthetic HTTP server response')
        pause = QEventLoop()
        QTimer.singleShot(10, pause.quit)
        pause.exec()
        if not window.engine._queued:
            raise RuntimeError('Sync finished before the responsiveness sample; increase subscription events')

    def measure(workload, view, category, action, index):
        nonlocal current, dialog, last_tick
        if args.progress:
            with args.progress.open('a') as stream:
                stream.write(json.dumps({'stage':'begin','workload':workload,'view':view,'category':category,'action':action,'sample':index})+'\n')
        current = {'workload': workload, 'view': view, 'category': category, 'action': action,
                   'sample': index, 'started': time.perf_counter(), 'cpu_started': time.thread_time(),
                   'revision_before': store.events_revision(), 'sync_active_before': window.engine._queued}
        last_tick = time.perf_counter()
        window.show_preferences(category=category)
        returned = time.perf_counter()
        if 'first_paint' not in current or 'closed_at' not in current:
            raise RuntimeError('Preferences did not complete the expected open/close interaction')
        current['close_to_return_ms'] = (returned-current['closed_at'])*1000
        current['close_cpu_ms'] = (time.thread_time()-current['close_cpu_started'])*1000
        current['returned_at'], current['return_cpu_tick'] = returned, time.thread_time()
        current.pop('calendar_paint', None)
        current.pop('post_close_tick', None)
        window.active_canvas().update()
        await_condition(lambda: 'calendar_paint' in current and 'post_close_tick' in current
                        and current.get('dialog_destroyed',False))
        current['open_to_first_paint_ms'] = (current['first_paint']-current['started'])*1000
        current['close_to_responsive_ms'] = (max(current['calendar_paint'], current['post_close_tick'])-current['closed_at'])*1000
        current['revision_after'] = store.events_revision()
        current['sync_active_after'] = window.engine._queued
        record = {key: (round(value, 3) if isinstance(value, float) else value)
                  for key, value in current.items() if key not in {
                      'started', 'cpu_started', 'first_paint', 'closed_at', 'close_cpu_started',
                      'calendar_paint', 'post_close_tick', 'returned_at', 'return_cpu_tick', 'dialog_destroyed'}}
        records.append(record)
        if args.progress:
            with args.progress.open('a') as stream:
                stream.write(json.dumps({'stage':'complete',**record})+'\n')
        current, dialog = None, None
        return record

    try:
        with Store(database) as store, ExitStack() as stack:
            stack.enter_context(patch('debacal.ui.now', return_value=ANCHOR))
            stack.enter_context(patch('sys.excepthook', error_hook))
            window = MainWindow(store, enable_sync=True)
            window.resize(1400, 900)
            window.show()
            for _ in range(3): pump()
            stack.enter_context(patch.object(PreferencesDialog, '__init__', instrument('construct', initialize_dialog)))
            stack.enter_context(patch.object(PreferencesDialog, 'reject', instrument('reject', PreferencesDialog.reject)))
            stack.enter_context(patch.object(PreferencesDialog, 'save', instrument('save', PreferencesDialog.save)))
            stack.enter_context(patch.object(CalendarSourcesEditor, 'finish', instrument('source_finish', CalendarSourcesEditor.finish)))
            stack.enter_context(patch.object(MainWindow, 'refresh', instrument('refresh', MainWindow.refresh)))
            sequence = 0
            for workload in args.workloads.split(','):
                for view in args.views.split(','):
                    window.set_view(view)
                    window.active_canvas().installEventFilter(probe)
                    for category in args.categories.split(','):
                        for action in ('cancel', 'save'):
                            for index in range(args.samples):
                                if workload == 'sync_changed':
                                    sequence += 1
                                    payload_path.write_bytes(payload.replace(b'SUMMARY:Synthetic remote event',
                                        f'SUMMARY:Synthetic revision {sequence} event'.encode()))
                                elif workload == 'sync_unchanged':
                                    # Keep the currently served payload byte-identical.
                                    pass
                                if workload.startswith('sync_'):
                                    start_sync()
                                record = measure(workload, view, category, action, index)
                                if workload.startswith('sync_'):
                                    finish_sync(record)
                                elif window.engine._queued:
                                    raise RuntimeError('Idle sample overlapped an unrequested automatic sync')
            if args.history_events:
                history_events = []
                for index in range(args.history_events):
                    value = Event(f'history-{index}', 'cal-0', f'Synthetic imported event {index}',
                                  ANCHOR + timedelta(days=index % 365),
                                  ANCHOR + timedelta(days=index % 365, hours=1),
                                  ical_uid=f'synthetic-history-{index}',
                                  description='Synthetic import payload ' * 100)
                    history_events.append(replace(value, raw_ics=fixed_ics(export_ics([value])).decode()))
                with Store(database, query_index=False) as writer:
                    writer.apply('Import synthetic history', history_events)
                    unrelated = writer.get_event('bench-editable')
                    for view in args.views.split(','):
                        window.set_view(view)
                        window.active_canvas().installEventFilter(probe)
                        for index in range(args.samples):
                            writer.apply_remote([replace(unrelated, title=f'Unrelated server revision {view} {index}')])
                            current = {'workload': 'large_history', 'view': view, 'category': 'Calendar',
                                       'action': 'refresh', 'sample': index, 'closed_at': time.perf_counter()}
                            last_tick = current['closed_at']
                            begin, cpu = time.perf_counter(), time.thread_time()
                            window.refresh()
                            current['refresh_dispatch_ms'] = (time.perf_counter()-begin)*1000
                            current['refresh_cpu_ms'] = (time.thread_time()-cpu)*1000
                            window.active_canvas().update()
                            await_condition(lambda: 'calendar_paint' in current and 'post_close_tick' in current)
                            current['refresh_to_responsive_ms'] = (max(current['calendar_paint'],current['post_close_tick'])-begin)*1000
                            availability = getattr(window, 'history_availability', None)
                            current['history_pending_after'] = bool(availability and availability._future is not None)
                            records.append({key:round(value,3) if isinstance(value,float) else value
                                            for key,value in current.items() if key not in {'closed_at','calendar_paint','post_close_tick'}})
                            current = None
                            if availability is not None:
                                await_condition(lambda: availability._future is None)
                            if not window.undo_button.isEnabled():
                                raise RuntimeError('Undo availability did not recover after the reviewed history check')
            verify_source(root)
            processor = getattr(window.engine, '_processor', None)
            result = {'schema_version': 1, 'source': source_metadata(root, args.revision),
                      'configuration': {**asdict(config), 'subscription_events': args.subscription_events,
                                        'samples': args.samples, 'history_events': args.history_events, 'timezone': args.timezone, 'views': args.views.split(','),
                                        'categories': args.categories.split(','), 'workloads': args.workloads.split(',')},
                      'fixture': {**fixture, 'subscription_sha256': hashlib.sha256(payload).hexdigest()},
                      'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'records': records, 'summary': statistics_for(records),
                      'environment': {'python': sys.version.split()[0], 'pyside6': pyside_version,
                                      'backend': app.platformName(), 'font': app.font().family(),
                                      'window_size': [window.width(), window.height()],
                                      'background_processing': processor is not None,
                                      'screen_size': [app.primaryScreen().size().width(), app.primaryScreen().size().height()],
                                      'qt_style': app.style().objectName(),
                                      'cpus_available': len(os.sched_getaffinity(0)), 'platform': sys.platform,
                                      'timezone': args.timezone, 'sqlite': sqlite3.sqlite_version,
                                      'dependency_versions': {name:version(name) for name in ('PySide6','icalendar','python-dateutil')}},
                      'methodology': {'local_only': True, 'synthetic_only': True,
                          'network': 'loopback HTTP server in a separate interpreter; no accounts or external requests',
                          'paint_instrumentation': 'dialog and active canvas only, no application-wide event filter',
                          'open': 'call through real show_preferences to first dialog paint',
                          'form_ready': 'first queued form interaction, including a deliberate 15ms timer',
                          'close': 'real Cancel/Save through native event-loop deletion, next calendar paint and heartbeat',
                          'deletion': 'return to QObject.destroyed notification; following paint/heartbeat also awaited',
                          'sync_cycle': 'heartbeat and refresh observer remains active through publication and following calendar paint',
                          'sync_overlap': 'start after full HTTP body served; require real engine job still active',
                          'profiles': 'timings are unprofiled; no historical query pages or warmup loops'}}
    finally:
        heartbeat.stop()
        if window is not None:
            engine = window.engine
            window.close()
            if engine is not None: engine._thread.join(timeout=120)
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        server.terminate()
        server.wait(timeout=10)
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--revision', default='')
    parser.add_argument('--timezone', default='UTC')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--await-timeout', type=int, default=180)
    parser.add_argument('--progress', type=Path)
    parser.add_argument('--events', type=int, default=20000)
    parser.add_argument('--series', type=int, default=2000)
    parser.add_argument('--finite', type=int, default=120)
    parser.add_argument('--active-series', type=int, default=240)
    parser.add_argument('--subscription-events', type=int, default=1500)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--history-events', type=int, default=1500)
    parser.add_argument('--views', default='week,year')
    parser.add_argument('--categories', default='Calendar,Appearance,Calendar sources,Keyboard shortcuts')
    parser.add_argument('--workloads', default='idle,sync_unchanged,sync_changed')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--directory', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--server', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.server:
        serve(args.server)
        return
    if min(args.events, args.series, args.active_series, args.subscription_events, args.samples) < 1 or min(args.finite,args.history_events) < 0:
        parser.error('Fixture counts and samples must be positive')
    if not set(args.workloads.split(',')) <= {'idle', 'sync_unchanged', 'sync_changed'}:
        parser.error('Unknown workload')
    if not set(args.views.split(',')) <= {'day','week','month','year'}:
        parser.error('Unknown calendar view')
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(args.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        parser.error('Unknown viewer timezone')
    if args.worker:
        worker(args)
        return
    sys.path.insert(0, str(ROOT / 'scripts'))
    from benchmark_extended import isolated_environment
    with tempfile.TemporaryDirectory(prefix='debacal-sync-responsiveness-') as temporary:
        directory = Path(temporary)
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--directory', str(directory)]
        for name in ('source_root', 'revision', 'events', 'series', 'finite', 'active_series',
                     'subscription_events', 'samples', 'views', 'categories', 'workloads', 'history_events', 'await_timeout', 'timezone'):
            command.extend(['--' + name.replace('_', '-'), str(getattr(args, name))])
        if args.progress:
            args.progress.parent.mkdir(parents=True, exist_ok=True)
            args.progress.write_text('')
            command.extend(['--progress',str(args.progress.resolve())])
        environment = isolated_environment(directory)
        environment['PYTHONPATH'] = str(args.source_root.resolve())
        environment['TZ'] = args.timezone
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=environment, timeout=3600)
        if process.returncode:
            if args.output:
                args.output.with_suffix('.stderr').write_text(process.stderr)
            raise RuntimeError('Responsiveness worker failed: ' + process.stderr[:5000] + process.stderr[-5000:])
        report = json.loads(process.stdout)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'records': len(report['records']), 'output': str(args.output),
                          'background_processing': report['environment']['background_processing']}))
    else:
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
