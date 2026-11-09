"""Measure complete desktop workflows in fresh processes, using disposable data.

Run with .venv/bin/python scripts/benchmark_workflows.py --output /tmp/ui.json.
Use the same script with --source-root OTHER_CHECKOUT for a fair comparison.
The default offscreen backend needs no display, account or persistent profile.
Set QT_QPA_PLATFORM=xcb explicitly to measure actual desktop painting instead.
Each sample uses its own file-backed workspace and process. Maximum latency and
all individual samples are reported; repeated warm visits are not a score.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=10000)
    parser.add_argument('--series', type=int, default=200)
    parser.add_argument('--iterations', type=int, default=3)
    parser.add_argument('--width', type=int, default=1400)
    parser.add_argument('--height', type=int, default=900)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--sample-index', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--max-ms', type=float, help='Fail if any measured workflow exceeds this latency.')
    args = parser.parse_args()
    if args.events < 1 or args.series < 1 or args.iterations < 1:
        parser.error('Event counts and iterations must be positive.')
    if args.width < 1 or args.height < 1:
        parser.error('Window dimensions must be positive.')
    if args.max_ms is not None and args.max_ms <= 0:
        parser.error('--max-ms must be positive.')
    if not args.worker:
        reports = []
        for sample in range(args.iterations):
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--iterations', '1',
                       '--sample-index', str(sample), '--events', str(args.events), '--series', str(args.series),
                       '--width', str(args.width), '--height', str(args.height),
                       '--source-root', str(args.source_root.resolve())]
            if args.profile:
                command.extend(['--profile', str(args.profile.with_name(args.profile.name + f'.{sample}'))])
            child = subprocess.run(command, capture_output=True, text=True, timeout=600)
            if child.returncode:
                raise RuntimeError('Workflow worker failed: ' + child.stderr.strip())
            reports.append(json.loads(child.stdout))
        report = reports[0]
        timings = {}
        for child_report in reports:
            for name, values in child_report['milliseconds'].items():
                timings.setdefault(name, []).extend(values)
        report.update(iterations=args.iterations, milliseconds=timings,
                      summary={name: {'max_ms': max(values), 'samples': len(values)}
                               for name, values in timings.items()},
                      max_event_loop_gap_ms={name: max(child['max_event_loop_gap_ms'].get(name, 0)
                                                       for child in reports)
                                             for child in reports for name in child['max_event_loop_gap_ms']},
                      peak_rss_mib=max(child['peak_rss_mib'] for child in reports),
                      preparation_ms=[child['preparation_ms'] for child in reports],
                      fixture_write_ms=[child['fixture_write_ms'] for child in reports],
                      fixture_generation_ms=[child['fixture_generation_ms'] for child in reports])
        report['environment'].update(fresh_process_per_sample=True, storage='file-backed SQLite', warmup_queries=0)
        rendered = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(rendered + '\n')
            print(json.dumps({'output': str(args.output), 'workflows': len(report['summary']),
                              'slowest_workflow_ms': max(item['max_ms'] for item in report['summary'].values())}, indent=2))
        else:
            print(rendered)
        return int(args.max_ms is not None and any(item['max_ms'] > args.max_ms for item in report['summary'].values()))
    sys.path.insert(0, str(args.source_root.resolve()))

    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog
    from debacal.conflict_dialog import ConflictDialog
    from debacal.date_time_picker import DateTimeRangePicker
    from debacal.event_dialog import EventDialog
    from debacal.ics import export_ics
    from debacal.import_dialog import ImportDialog
    from debacal.import_sources import ImportSource
    from debacal.models import Calendar, Event
    from debacal.preferences_dialog import PreferencesDialog
    from debacal.store import Store
    from debacal.sync import SyncConflict
    from debacal.ui import MainWindow

    app = QApplication([])
    app.setStyle('Fusion')
    start = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin')) + timedelta(days=args.sample_index * 61)
    workspace = TemporaryDirectory(prefix='debacal-workflow-benchmark-')
    store = Store(Path(workspace.name) / 'calendar.sqlite3')
    for index, color in enumerate(('#587c67', '#d5ad32', '#697ab1')):
        store.save_calendar(Calendar(f'cal-{index}', f'Synthetic {index + 1}', color))
    store.set_default_calendar('cal-0')
    from benchmark_ranges import Fixture, generated_events
    generation_started = perf_counter()
    events = list(generated_events(Fixture(args.events, args.series, finite=120, calendars=3)))
    fixture_generation_ms = round((perf_counter() - generation_started) * 1000, 3)
    # Always exercise individual editing, even with a minimal smoke dataset.
    ordinary = Event('editable', 'cal-0', 'Editable meeting', start, start + timedelta(hours=1))
    candidate_start = start.replace(hour=20)
    candidate = Event('choices', 'cal-0', 'Choose a slot', candidate_start,
        candidate_start + timedelta(hours=1), candidate=True,
        recurrence_dates=tuple((candidate_start + timedelta(weeks=n)).isoformat() for n in range(1, 500)))
    write_started = perf_counter()
    store.apply_remote([*events, ordinary, candidate])
    fixture_write_ms = round((perf_counter() - write_started) * 1000, 3)
    store.close()
    preparation_started = perf_counter()
    store = Store(Path(workspace.name) / 'calendar.sqlite3')
    preparation_ms = round((perf_counter() - preparation_started) * 1000, 3)
    timings, loop_gaps, errors = {}, {}, []
    active = None
    last_tick = perf_counter()

    def tick():
        nonlocal last_tick
        current = perf_counter()
        if active is not None:
            loop_gaps.setdefault(active, []).append((current - last_tick) * 1000)
        last_tick = current

    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(tick)
    heartbeat.start()

    def settle():
        for _ in range(3):
            app.processEvents()
        if errors:
            raise RuntimeError('Unhandled Qt callback error: ' + str(errors[0])) from errors[0]

    def record_error(_kind, error, _traceback):
        errors.append(error)

    def wait_for(predicate, timeout=30):
        begin = perf_counter()
        while not predicate():
            settle()
            QTest.qWait(1)
            if perf_counter() - begin > timeout:
                raise RuntimeError('Workflow did not complete within the timeout.')
        settle()

    def measure(name, action):
        nonlocal active, last_tick
        settle()
        active, last_tick = name, perf_counter()
        begin = last_tick
        try:
            value = action()
            settle()
            timings.setdefault(name, []).append(round((perf_counter() - begin) * 1000, 3))
            return value
        finally:
            active = None

    def dispose(dialog):
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        settle()

    def display(dialog):
        dialog.show()
        settle()
        if dialog.grab().isNull():
            raise RuntimeError('Dialog did not paint.')
        return dialog

    profile = None
    if args.profile:
        import cProfile
        profile = cProfile.Profile()
        profile.enable()
    window = None
    try:
        with patch('debacal.ui.now', return_value=start), patch('sys.excepthook', side_effect=record_error):
            def open_workspace():
                value = MainWindow(store, enable_sync=False)
                # Compare identical geometry even when initial screen fitting
                # differs between checkouts or offscreen platform defaults.
                value.resize(args.width, args.height)
                value.go_date(start.date())
                value.show()
                return value
            window = measure('open_workspace', open_workspace)
            initial_window_size = [window.width(), window.height()]
            calendars = store.list_calendars()
            for iteration in range(args.iterations):
                for view in ('day', 'month', 'week', 'year'):
                    measure(view + '_view', lambda view=view: window.set_view(view))
                measure('year_next', lambda: window.navigate(1))
                measure('year_previous', lambda: window.navigate(-1))
                for style in ('tints', 'tracks'):
                    measure('year_style_' + style, lambda style=style: window.apply_preferences(
                        replace(window.preferences, year_view_style=style), window.bindings))
                measure('year_hide_calendar', lambda: window.toggle_calendar('cal-1', False))
                measure('year_show_calendar', lambda: window.toggle_calendar('cal-1', True))
                window.set_view('month')
                measure('month_next', lambda: window.navigate(1))
                measure('month_previous', lambda: window.navigate(-1))
                window.set_view('week')
                measure('hide_calendar', lambda: window.toggle_calendar('cal-1', False))
                measure('show_calendar', lambda: window.toggle_calendar('cal-1', True))
                window.focus_search()
                measure('type_search', lambda: QTest.keyClicks(window.search, 'synthetic ordinary-123'))
                measure('finish_search', lambda: wait_for(lambda: not window.search_timer.isActive()))
                if window.results.count():
                    measure('search_follow', lambda: window.cycle_search_result(1))
                    measure('search_filter', window.filter_search.click)
                    window.filter_search.click()
                measure('clear_search', window.exit_search)
                window.go_date(start.date())
                window.select_event(ordinary.id)
                measure('selection_all', lambda: window.set_selection_mode('group'))
                measure('selection_single', lambda: window.set_selection_mode('single'))
                window.activateWindow()
                window.canvas.setFocus()
                settle()
                measure('copy', lambda: QTest.keyClick(window.canvas, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier))
                measure('paste_preview', lambda: QTest.keyClick(window.canvas, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier))
                if window.pending_paste is None:
                    raise RuntimeError('Paste did not enter placement mode.')
                def place_preview():
                    window.pending_paste.update(start + timedelta(days=1))
                    window.render_placement()
                measure('paste_move_preview', place_preview)
                measure('paste_confirm', window.confirm_paste)
                measure('undo', window.undo)
                measure('redo', window.redo)
                window.undo()
                window.go_date(start.date())
                window.select_event(ordinary.id)
                measure('move_event', lambda: window.move_event(ordinary.id,
                    start + timedelta(hours=1), start + timedelta(hours=2)))
                window.undo()
                window.select_event(ordinary.id)
                measure('delete_event', window.delete_event)
                window.undo()
                def edit(dialog):
                    dialog.title.setText('Edited meeting')
                    dialog.save()
                    return QDialog.DialogCode.Accepted
                with patch.object(EventDialog, 'exec', edit):
                    measure('edit_and_save', lambda: window.edit_event(ordinary.id))
                window.undo()

                dialog = measure('new_event_dialog', lambda: display(EventDialog(calendars,
                    start, start + timedelta(hours=1), preferences=window.preferences, parent=window)))
                dialog.title.setText('New meeting')
                measure('editor_regular_series', lambda: dialog.schedule_mode.setCurrentData('repeat'))
                measure('editor_custom_series', lambda: dialog.pattern.setText('+0d 4*+2w +3w 3*+2w'))
                measure('editor_preview_next', dialog.preview.next_occurrence_button.click)
                measure('editor_preview_last', dialog.preview.last_button.click)
                measure('editor_build_events', dialog.build_events)
                dispose(dialog)
                picker = measure('date_time_picker', lambda: display(DateTimeRangePicker(
                    start, start + timedelta(hours=1), preferences=window.preferences, parent=window)))
                measure('picker_change_start', lambda: picker.set_start(start + timedelta(minutes=5)))
                measure('picker_change_end', lambda: picker.set_end(start + timedelta(hours=2)))
                dispose(picker)

                prefs = measure('preferences_dialog', lambda: display(PreferencesDialog(
                    window.preferences, window, store=store, bindings=window.bindings, allow_remote=False)))
                prefs.applied.connect(window.apply_preferences)
                measure('preferences_appearance', lambda: prefs.set_category('Appearance'))
                measure('preferences_apply_unchanged', prefs.apply)
                dispose(prefs)
                for scheme in ('warm', 'neutral'):
                    measure('preferences_theme_' + scheme, lambda scheme=scheme: window.apply_preferences(
                        replace(window.preferences, color_scheme=scheme), window.bindings))
                for scale in (120, 100):
                    measure('preferences_font_' + str(scale), lambda scale=scale: window.apply_preferences(
                        replace(window.preferences, font_scale=scale), window.bindings))

            # Keep a single file below the parser's per-resource safety cap,
            # while including both ordinary events and recurring masters.
            import_events = [*events[:min(args.events, 4000)],
                             *events[args.events:args.events + min(args.series, 500)], ordinary]
            payload = measure('export_ics', lambda: export_ics(import_events))
            # A separate destination keeps the later confirmation workload
            # unchanged while exercising the complete import write path.
            import_calendar = Calendar('benchmark-import', 'Synthetic import', '#587c67', visible=False)
            store.save_calendar(import_calendar)
            imported = measure('import_dialog', lambda: display(ImportDialog([*calendars, import_calendar],
                sources=[ImportSource.from_data(payload, 'Synthetic calendar')], calendar_id=import_calendar.id,
                preferences=window.preferences, parent=window)))
            measure('import_ready', lambda: wait_for(lambda: imported.preview_ready, timeout=120))
            if imported.prepared_import is None or not imported.prepared_import.events:
                raise RuntimeError('Import preview did not prepare the synthetic events: '
                                   + repr(getattr(imported.prepared_import, 'warnings', imported.summary.text())))
            def step_import():
                imported.preview.next_occurrence_button.click()
                wait_for(lambda: imported._navigation_future is None and imported._page_future is None)
            for _ in range(3):
                measure('import_next_occurrence', step_import)
            def next_import_month():
                imported.preview.next_button.click()
                wait_for(lambda: imported._page_future is None)
            measure('import_next_month', next_import_month)
            measure('import_toggle_date', lambda: imported._toggle_date(imported._page.events[0].start.date())
                    if imported._page.events else None)
            measure('import_selection_ready', lambda: wait_for(lambda: imported._selection_future is None))
            measure('import_accept', imported.save)
            if imported.selected_prepared is None:
                raise RuntimeError('Import acceptance did not preserve the reviewed snapshot.')
            reports = measure('import_remap', lambda: [item.remap(import_calendar.id)
                for item in imported.selected_prepared.items if item.report is not None])
            def apply_import():
                for report in reports:
                    future = Future()
                    future.set_result(report)
                    future.calendar_id = import_calendar.id
                    window.tasks.put(('import', future))
                window.poll()
            measure('import_apply', apply_import)
            if not store.list_events(calendar_ids=[import_calendar.id]):
                raise RuntimeError('Accepted import did not persist any events.')
            dispose(imported)
            remote = replace(ordinary, title='Server version', start=start + timedelta(hours=2),
                             end=start + timedelta(hours=3))
            conflict = SyncConflict('synthetic-conflict', 'cal-0', ordinary.id, ordinary, remote,
                                    'Synthetic concurrent calendar edits', 'synthetic', (remote,))
            comparison = measure('conflict_dialog', lambda: display(ConflictDialog(conflict, window.preferences, window)))
            measure('conflict_next_month', lambda: comparison.navigate_month(1))
            dispose(comparison)
            window.go_date(start.date())
            window.set_view('week')
            chosen = next(value for value in window.visible_events if value.candidate)
            window.select_event(chosen.id)
            settle()
            hit = next(rect for rect, identifier in window.canvas.confirm_hits if identifier == chosen.id)
            def confirm():
                QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=hit.center().toPoint())
                wait_for(lambda: not store.get_event('choices').candidate)
            measure('confirm_slot', confirm)
            if window.local_issues:
                raise RuntimeError('Workflow produced app issues: ' + repr(window.local_issues))
    finally:
        with patch('sys.excepthook', side_effect=record_error):
            heartbeat.stop()
            if window is not None:
                window.close()
                window.executor.shutdown(wait=True, cancel_futures=True)
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.clipboard().clear()
            app.processEvents()
            store.close()
            workspace.cleanup()
            if profile:
                profile.disable()
                profile.dump_stats(str(args.profile))
    if errors:
        raise RuntimeError('Unhandled Qt callback error during cleanup: ' + str(errors[0])) from errors[0]

    summary = {name: {'max_ms': max(samples), 'samples': len(samples)} for name, samples in timings.items()}
    report = {'environment': {'python': platform.python_version(), 'qt': qt_version,
                             'backend': app.platformName(), 'profiled': bool(profile),
                             'initial_window_size': initial_window_size},
              'ordinary_events': args.events, 'recurring_series': args.series, 'finite_series': 120, 'candidate_dates': 500,
              'iterations': args.iterations, 'import_resources': len(import_events),
              'preparation_ms': preparation_ms, 'fixture_write_ms': fixture_write_ms,
              'fixture_generation_ms': fixture_generation_ms,
              'milliseconds': timings, 'summary': summary,
              'max_event_loop_gap_ms': {name: round(max(values), 3) for name, values in loop_gaps.items()},
              'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)}
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n')
        print(json.dumps({'output': str(args.output), 'workflows': len(summary),
                          'peak_rss_mib': report['peak_rss_mib']}, indent=2))
    else:
        print(rendered)


if __name__ == '__main__':
    raise SystemExit(main())
