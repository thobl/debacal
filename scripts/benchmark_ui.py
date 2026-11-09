"""Measure first visits to unrelated desktop date ranges, reporting maxima.

Use benchmark_ranges.py to isolate retrieval from Qt layout/painting. This
companion runs one fresh process against the same generated, file-backed data,
visits unique non-monotonic intervals and times real calendar visibility writes.
It does not measure a repeated warm month/week loop.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from benchmark_ranges import Fixture, ROOT, build_database, requests

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def worker(source, database, workload):
    sys.path.insert(0, str(source))
    from PySide6.QtWidgets import QApplication
    from debacal.store import Store
    from debacal.ui import MainWindow
    app = QApplication([])
    app.setStyle('Fusion')
    begin = perf_counter()
    store = Store(database)
    preparation = (perf_counter() - begin) * 1000
    window = None
    records, errors = [], []

    def settle():
        for _ in range(3):
            app.processEvents()
        if errors:
            raise RuntimeError('Unhandled Qt callback error: ' + str(errors[0])) from errors[0]

    def measure(operation, action, request=None):
        begin = perf_counter()
        action()
        settle()
        records.append({'operation': operation, 'elapsed_ms': round((perf_counter() - begin) * 1000, 3),
                        **(request or {})})
        if window is not None and window.local_issues:
            raise RuntimeError('Navigation produced app issues: ' + repr(window.local_issues))

    try:
        with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
            def open_window():
                nonlocal window
                window = MainWindow(store, enable_sync=False)
                window.resize(1400, 900)
                window.show()
            measure('open_workspace', open_window)
            for index, request in enumerate(workload):
                def show_range():
                    # Equivalent state update to view/date navigation, with one
                    # refresh rather than an intermediate query for another date.
                    window.view = request['kind']
                    window.anchor = datetime.fromisoformat(request['start']).date()
                    window.selected_day = window.anchor
                    window.refresh()
                metadata = {key: value for key, value in request.items() if key != 'calendars'}
                metadata['visible_calendars'] = [calendar.id for calendar in store.list_calendars() if calendar.visible]
                measure('show_' + request['kind'], show_range, metadata)
                fixture_calendars = [calendar for calendar in store.list_calendars() if calendar.id.startswith('cal-')]
                calendar = fixture_calendars[index % len(fixture_calendars)]
                measure('hide_calendar' if calendar.visible else 'show_calendar',
                        lambda: window.toggle_calendar(calendar.id, not calendar.visible),
                        {'calendar': calendar.id, 'start': request['start'], 'view': request['kind']})
    finally:
        if window is not None:
            window.close()
            window.executor.shutdown(wait=True, cancel_futures=True)
        app.processEvents()
        store.close()
    names = sorted({record['operation'] for record in records})
    return {'storage': 'file-backed SQLite', 'fresh_process': True, 'warmup_queries': 0,
            'preparation_ms': round(preparation, 3), 'records': records,
            'summary': {name: {'max_ms': max(record['elapsed_ms'] for record in records if record['operation'] == name),
                               'samples': sum(record['operation'] == name for record in records)} for name in names}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=6000)
    parser.add_argument('--series', type=int, default=1200)
    parser.add_argument('--finite', type=int, default=120)
    parser.add_argument('--queries', type=int, default=24)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-ms', type=float, help='Fail if any measured desktop operation exceeds this latency.')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--requests', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(worker(args.source_root.resolve(), args.database, json.loads(args.requests.read_text()))))
        return 0
    if min(args.events, args.series, args.finite) < 0 or not 1 <= args.queries <= 2191:
        parser.error('Event counts must be nonnegative and queries must be between 1 and 2191.')
    if args.max_ms is not None and args.max_ms <= 0:
        parser.error('--max-ms must be positive.')
    sys.path.insert(0, str(args.source_root.resolve()))
    config = Fixture(args.events, args.series, args.finite)
    with TemporaryDirectory(prefix='debacal-ui-benchmark-') as directory:
        database, request_file = Path(directory) / 'calendar.sqlite3', Path(directory) / 'requests.json'
        build_database(database, config)
        request_file.write_text(json.dumps(requests(args.queries, config.calendars)))
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--source-root', str(args.source_root.resolve()),
                   '--database', str(database), '--requests', str(request_file)]
        child = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if child.returncode:
            raise RuntimeError('Desktop benchmark worker failed: ' + child.stderr.strip())
        report = json.loads(child.stdout)
    report['fixture'] = config.__dict__
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n')
        print(json.dumps({'output': str(args.output), 'summary': report['summary']}, indent=2))
    else:
        print(rendered)
    return int(args.max_ms is not None and any(record['elapsed_ms'] > args.max_ms for record in report['records']))


if __name__ == '__main__':
    raise SystemExit(main())
