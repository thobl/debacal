"""Local-only sustained desktop benchmarks with synthetic calendars.

This is deliberately not a pytest test and is not run by release CI. Each
version builds its own database through that version's Store. The same seeded
interaction plan is then executed in a fresh process, with isolated XDG paths,
no account connections and no warm-up queries. Startup readiness is measured
from process creation, independently from Store, Qt and workspace preparation.

Example: python scripts/benchmark_extended.py --output /tmp/extended.json
Use --source-root to benchmark an immutable historical checkout with this exact
script. QT_QPA_PLATFORM defaults to offscreen, but may be set to xcb for Xvfb.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import resource
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
BLOCKS = ('navigation', 'preferences', 'creation', 'gestures', 'editing', 'series', 'heavy_selection', 'clipboard',
          'calendars', 'search', 'import_export', 'dialogs', 'notifications')


@dataclass(frozen=True)
class Config:
    events: int = 20000
    series: int = 2000
    finite: int = 120
    active_series: int = 240
    heavy_overrides: int = 400
    candidate_dates: int = 500
    calendars: int = 8
    payload_bytes: int = 2048
    rounds: int = 8
    startup_runs: int = 5
    import_events: int = 300
    seed: int = 81423
    width: int = 1400
    height: int = 900
    timeout_seconds: int = 3600


def summary(values):
    """Retain the maximum; percentiles never replace the individual samples."""
    ordered = sorted(values)
    if not ordered:
        return {'samples': 0, 'mean_ms': None, 'p95_ms': None, 'max_ms': None}
    return {'samples': len(ordered), 'mean_ms': round(statistics.fmean(ordered), 3),
            'p95_ms': round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 3),
            'max_ms': round(ordered[-1], 3), 'min_ms': round(ordered[0], 3)}


def interaction_plan(rounds, seed):
    rng = random.Random(seed)
    result = []
    for index in range(rounds):
        blocks = list(BLOCKS)
        rng.shuffle(blocks)
        views = ['day', 'week', 'month', 'year']
        rng.shuffle(views)
        # These intervals are unique and deliberately non-monotonic. Editing
        # existing events is separately timed at its actual source date.
        offsets = rng.sample(range(-730, 1461), 4)
        result.append({'round': index, 'blocks': blocks, 'views': views, 'offsets': offsets})
    return result


def expected_fixture_selection(visible, reference, scope):
    """Independent highlight oracle for the fixture's timed daily family.

    Detached dates retain their nominal recurrence identity. Comparing their
    moved clocks instead would incorrectly change which side of a cutoff they
    belong to. The fixture has no coincident or all-day identities.
    """
    family = reference.series_id or reference.id
    cutoff = datetime.fromisoformat(reference.recurrence_id or reference.start.isoformat())
    return {event.id for event in visible
            if (event.series_id or event.id) == family
            and (scope == 'group' or datetime.fromisoformat(
                event.recurrence_id or event.start.isoformat()) >= cutoff)}


def source_metadata(root, revision=''):
    paths = sorted((root / 'debacal').glob('*.py'))
    if not paths:
        raise ValueError('The source root must contain the debacal package.')
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return {'revision': revision, 'application_sha256': digest.hexdigest(),
            'application_files': len(paths)}


def verify_source(root):
    """A copied benchmark must never silently import the editable current app."""
    import debacal
    expected = (root / 'debacal').resolve()
    loaded = Path(debacal.__file__).resolve().parent
    if loaded != expected:
        raise RuntimeError(f'Wrong application imported: {loaded}; expected {expected}.')
    for name, module in tuple(sys.modules.items()):
        if name.startswith('debacal.') and getattr(module, '__file__', None):
            path = Path(module.__file__).resolve()
            if not path.is_relative_to(expected):
                raise RuntimeError('Application modules from different source roots were mixed.')


def fixture_events(config):
    # Reuse the maintained deterministic data generator, but instantiate its
    # Event classes only after the requested source root has been installed.
    if __package__:
        from .benchmark_ranges import Fixture, generated_events
    else:
        from benchmark_ranges import Fixture, generated_events
    from debacal.models import Event
    generated = list(generated_events(Fixture(config.events, config.series, config.finite,
                                             config.calendars, config.payload_bytes)))
    for index in range(config.active_series):
        first = ANCHOR.replace(hour=8 + index % 10) - timedelta(days=1200 + index % 31)
        generated.append(Event(f'active-{index}', f'cal-{index % config.calendars}',
            f'Synthetic active recurring appointment {index}', first, first + timedelta(minutes=30),
            recurrence_rule='FREQ=DAILY' if index % 3 == 0 else 'FREQ=WEEKLY',
            description='Synthetic payload ' * (config.payload_bytes // 18)))
    heavy_start = ANCHOR.replace(hour=14) - timedelta(days=90)
    overrides = {}
    for index in range(config.heavy_overrides):
        nominal = heavy_start + timedelta(days=index)
        moved = nominal + timedelta(hours=1, minutes=index % 6 * 5)
        overrides[nominal.isoformat()] = {
            'id': f'heavy-exception-{index}', 'calendar_id': 'cal-0',
            'title': f'Synthetic detached occurrence {index}', 'start': moved.isoformat(),
            'end': (moved + timedelta(minutes=45)).isoformat(),
            'description': 'Synthetic exception payload ' * 12,
        }
    generated.extend([
        Event('bench-editable', 'cal-0', 'Benchmark editable appointment', ANCHOR,
              ANCHOR + timedelta(hours=1)),
        Event('bench-series', 'cal-0', 'Benchmark weekly series', ANCHOR + timedelta(hours=3),
              ANCHOR + timedelta(hours=4), recurrence_rule='FREQ=WEEKLY;COUNT=30'),
        Event('bench-candidate', 'cal-0', 'Benchmark alternative slots', ANCHOR + timedelta(hours=7),
              ANCHOR + timedelta(hours=8), candidate=True,
              recurrence_dates=tuple((ANCHOR + timedelta(days=n * 3, hours=7)).isoformat()
                                     for n in range(1, config.candidate_dates))),
        Event('bench-heavy', 'cal-0', 'Benchmark exception-heavy series', heavy_start,
              heavy_start + timedelta(minutes=45), recurrence_rule='FREQ=DAILY;COUNT=1200',
              recurrence_overrides=overrides),
        Event('bench-all-day', 'cal-0', 'Benchmark all-day span', ANCHOR.replace(hour=0),
              ANCHOR.replace(hour=0) + timedelta(days=3), all_day=True),
    ])
    return generated


def build_fixture(database, config):
    from debacal.models import Calendar
    from debacal.store import Store
    from debacal.preferences import Preferences
    colors = ('#587c67', '#b4994f', '#697ab1', '#bd775a', '#30978c', '#934970', '#438ed0', '#8eaa39')
    started = time.perf_counter()
    events = fixture_events(config)
    generated_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    with Store(database) as store:
        for index in range(config.calendars):
            store.save_calendar(Calendar(f'cal-{index}', f'Synthetic calendar {index + 1}', colors[index % len(colors)]))
        store.save_calendar(Calendar('benchmark-import', 'Synthetic imports', '#587c67', visible=False))
        store.set_default_calendar('cal-0')
        Preferences(auto_sync_enabled=False, font_family='DejaVu Sans', locale_name='en_GB').save(store)
        store.apply_remote(events)
    return {'events': len(events), 'generation_ms': round(generated_ms, 3),
            'write_ms': round((time.perf_counter() - started) * 1000, 3),
            'database_bytes': database.stat().st_size}


def isolated_environment(directory):
    env = os.environ.copy()
    for name, subdir in (('XDG_DATA_HOME', 'data'), ('XDG_CONFIG_HOME', 'config'),
                         ('XDG_CACHE_HOME', 'cache'), ('XDG_RUNTIME_DIR', 'runtime')):
        path = directory / subdir
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[name] = str(path)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    env['TZ'] = 'UTC'
    env['PYTHONHASHSEED'] = '0'
    # The benchmark never needs an existing user's desktop bus/keyring.
    env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + str(directory / 'runtime/no-session-bus')
    env.pop('GNOME_KEYRING_CONTROL', None)
    return env


def run_worker(root, config, mode, database, directory, *, profile=None, progress=None):
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', mode,
               '--source-root', str(root), '--database', str(database),
               '--configuration', json.dumps(asdict(config))]
    if profile:
        command.extend(['--profile', str(profile)])
    started = time.perf_counter()
    with tempfile.TemporaryFile(mode='w+t') as errors:
        environment = isolated_environment(directory)
        if progress:
            environment['DEBACAL_BENCHMARK_PROGRESS'] = str(progress)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=errors, text=True, env=environment)
        try:
            # Readiness is sent before cleanup. The parent's clock includes
            # Python execution, imports and the first real Qt paint.
            ready, _, _ = select.select([child.stdout], [], [], config.timeout_seconds)
            if not ready:
                raise TimeoutError(f'{mode} worker did not report readiness.')
            line = child.stdout.readline()
            ready_ms = (time.perf_counter() - started) * 1000
            result = json.loads(line) if line else None
            child.wait(timeout=config.timeout_seconds)
            errors.seek(0)
            stderr = errors.read()
            if child.returncode or result is None:
                raise RuntimeError(f'{mode} worker failed ({child.returncode}): {stderr[-8000:]}')
            result['process_to_report_ms'] = round(ready_ms, 3)
            return result
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def desktop_worker(database, config, mode, profile_path=None):
    process_started = WORKER_STARTED
    imports_started = time.perf_counter()
    from PySide6 import __version__ as qt_version
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QFont, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
    from shiboken6 import isValid
    from debacal.conflict_dialog import ConflictDialog
    from debacal.date_time_picker import DateTimeRangePicker
    from debacal.dialogs import CalendarDialog, HistoryDialog
    from debacal.event_dialog import EventDialog
    from debacal.ics import export_ics, parse_ics_report
    from debacal.import_dialog import ImportDialog
    from debacal.import_sources import ImportSource
    from debacal.models import Calendar, Event
    from debacal.preferences import Preferences
    from debacal.preferences_dialog import PreferencesDialog
    from debacal.store import Store
    from debacal.sync import SyncConflict
    from debacal.ui import MainWindow
    imports_ms = (time.perf_counter() - imports_started) * 1000
    started = time.perf_counter()
    app = QApplication([])
    app.setStyle('Fusion')
    app.setFont(QFont('DejaVu Sans', 10))
    qt_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    store = Store(database)
    open_ms = (time.perf_counter() - started) * 1000
    errors, dialogs, records, usage_samples = [], [], [], []
    scenario_context = {}
    window = None
    active = None
    last_tick = time.perf_counter()
    max_gap = 0.0
    paint_count = 0

    class PaintCounter(QObject):
        def eventFilter(self, obj, event):
            nonlocal paint_count
            if event.type() == QEvent.Type.Paint:
                paint_count += 1
            return False

    counter = PaintCounter()
    app.installEventFilter(counter)
    def tick():
        nonlocal last_tick, max_gap
        current = time.perf_counter()
        if active is not None:
            max_gap = max(max_gap, (current - last_tick) * 1000)
        last_tick = current
        modal = QApplication.activeModalWidget()
        if modal is not None and not any(isValid(dialog) and modal is dialog for dialog in dialogs):
            errors.append(RuntimeError('Unexpected modal dialog during ' + str(active)
                                       + ': ' + modal.metaObject().className()))
            modal.reject()
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(tick)
    heartbeat.start()
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.setInterval(min(config.timeout_seconds, 180) * 1000)
    def operation_timeout():
        errors.append(TimeoutError('Operation timed out: ' + str(active)))
        modal = QApplication.activeModalWidget()
        if modal is not None:
            modal.reject()
    watchdog.timeout.connect(operation_timeout)

    def pump():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # Holding dead Qt wrappers keeps their Python attributes and closure
        # graphs alive, which would manufacture a benchmark-only memory leak.
        dialogs[:] = [dialog for dialog in dialogs if isValid(dialog)]
        if errors:
            raise RuntimeError('Unhandled Qt callback: ' + repr(errors[0])) from errors[0]

    def pending():
        if window is None:
            return False
        commands = getattr(window, 'database_commands', None)
        return bool((commands is not None and commands.pending)
                    or getattr(window, '_editor_future', None) is not None
                    or getattr(window, '_import_jobs', ())
                    or getattr(window, '_import_requests', ())
                    or getattr(window, '_import_dialog_active', False)
                    or not window.tasks.empty())

    def settle(predicate=None, timeout=120):
        begin = time.perf_counter()
        barrier = window.executor.submit(lambda: None) if window is not None else None
        quiet = 0
        while quiet < 2:
            pump()
            ready = not pending() and (barrier is None or barrier.done()) and (predicate is None or predicate())
            quiet = quiet + 1 if ready else 0
            if time.perf_counter() - begin > timeout:
                raise TimeoutError('UI operation did not complete; active=' + str(active))
            if quiet < 2:
                time.sleep(.001)
        if window is not None and getattr(window, 'local_issues', ()):
            raise RuntimeError('Operation produced app issues: ' + repr(window.local_issues))

    def measure(name, action, *, predicate=None, context=None):
        nonlocal active, last_tick, max_gap
        before_context = {'view_before': window.view, 'selection_scope': window.operation_scope()} if window is not None else {}
        progress = os.environ.get('DEBACAL_BENCHMARK_PROGRESS')
        if progress:
            with open(progress, 'a') as stream:
                stream.write(json.dumps({'stage': 'begin', 'operation': name, 'completed': len(records)}) + '\n')
        active = name
        last_tick, max_gap = time.perf_counter(), 0.0
        started, before_paints = last_tick, paint_count
        watchdog.start()
        try:
            value = action()
            dispatch = (time.perf_counter() - started) * 1000
            settle(predicate)
            elapsed = (time.perf_counter() - started) * 1000
            tick()
            records.append({'operation': name, 'elapsed_ms': round(elapsed, 3),
                            'dispatch_ms': round(dispatch, 3), 'max_event_loop_gap_ms': round(max_gap, 3),
                            'paint_events': paint_count - before_paints, **scenario_context,
                            **before_context, **(context or {})})
            if progress:
                with open(progress, 'a') as stream:
                    stream.write(json.dumps({'stage': 'complete', **records[-1]}) + '\n')
            return value
        finally:
            watchdog.stop()
            active = None

    def display(dialog):
        dialogs.append(dialog)
        dialog.show()
        dialog.activateWindow()
        return dialog

    def dispose(dialog):
        dialog.close()
        dialog.deleteLater()
        dialogs[:] = [tracked for tracked in dialogs if tracked is not dialog]
        pump()

    def accept_editor(editor, title, configure=None):
        display(editor)
        settle()
        editor.title.setText(title)
        if configure:
            configure(editor)
        editor.save()
        if editor.result() != QDialog.DialogCode.Accepted or not editor.result_events:
            raise RuntimeError('Editor did not save: ' + editor.error.text())
        return QDialog.DialogCode.Accepted

    def focus_source(identity='bench-editable', *, day_offset=0):
        source = store.get_event(identity)
        if source is None:
            raise RuntimeError('Fixture event is missing: ' + identity)
        target_day = source.start.date() + timedelta(days=day_offset)
        measure('focus_source_date', lambda: window.go_date(target_day))
        measure('focus_week_view', lambda: window.set_view('week'))
        if not store.get_calendar('cal-0').visible:
            measure('restore_primary_calendar', lambda: window.toggle_calendar('cal-0', True))
        reference = next((item for item in window.visible_events if item.id == identity or item.series_id == identity), None)
        if reference is None:
            raise RuntimeError('Fixture source did not appear in its date window: ' + identity)
        measure('select_event', lambda: window.select_event(reference.id))
        return reference

    def preferences_block(round_index):
        dialog = measure('preferences_open', lambda: display(PreferencesDialog(
            window.preferences, window, store=store, bindings=window.bindings, allow_remote=False)))
        dialog.applied.connect(window.apply_preferences)
        dialog.calendars_changed.connect(window.preferences_sources_changed)
        dialog.credential_requested.connect(window.save_source_credentials)
        for category in ('Calendar', 'Appearance', 'Calendar sources', 'Sync', 'Keyboard shortcuts'):
            measure('preferences_category_' + category.lower().replace(' ', '_'),
                    lambda category=category: dialog.set_category(category))
        measure('preferences_edit_appearance', lambda: dialog.set_category('Appearance'))
        changes = (
            ('theme', dialog.color_scheme, ('warm', 'neutral')[round_index % 2]),
            ('mode', dialog.appearance_mode, ('dark', 'light')[round_index % 2]),
            ('year_style', dialog.year_view_style, ('tracks', 'tints')[round_index % 2]),
            ('buttons', dialog.control_style, ('filled', 'underlined')[round_index % 2]),
        )
        for label, field, value in changes:
            measure('preferences_scroll_' + label,
                    lambda field=field: dialog.appearance_editor.controls_scroll.ensureWidgetVisible(field))
            index = field.findData(value)
            if index < 0:
                # Enumerated choices may have gained labels, but use the same
                # ordinal alternatives across snapshots rather than skipping.
                index = (field.findData(field.currentData()) + 1) % field.count()
            measure('preferences_change_' + label, lambda field=field, index=index: field.setCurrentIndex(index))
            measure('preferences_apply_' + label, dialog.apply,
                    predicate=lambda: not getattr(dialog, '_applying', False))
        measure('preferences_scroll_font', lambda: dialog.appearance_editor.controls_scroll.ensureWidgetVisible(dialog.font_scale))
        measure('preferences_font_size', lambda: dialog.font_scale.setValue(110 if round_index % 2 == 0 else 100))
        measure('preferences_apply_font_size', dialog.apply, predicate=lambda: not getattr(dialog, '_applying', False))
        measure('preferences_edit_calendar', lambda: dialog.set_category('Calendar'))
        measure('preferences_week_start', lambda: dialog.week_start.setCurrentIndex((dialog.week_start.currentIndex() + 1) % dialog.week_start.count()))
        measure('preferences_week_numbers', dialog.week_numbers.toggle)
        measure('preferences_visible_hours', lambda: (dialog.visible_start.setValue(7), dialog.visible_end.setValue(22)))
        measure('preferences_default_duration', lambda: dialog.default_duration.setText('45 min' if round_index % 2 == 0 else '1 h'))
        measure('preferences_edit_sync', lambda: dialog.set_category('Sync'))
        measure('preferences_auto_sync_enable', lambda: dialog.auto_sync.setChecked(True))
        measure('preferences_sync_interval', lambda: dialog.sync_interval.setValue(5 + round_index % 3))
        measure('preferences_auto_sync_disable', lambda: dialog.auto_sync.setChecked(False))
        if dialog.source_editor is not None:
            measure('preferences_edit_sources', lambda: dialog.set_category('Calendar sources'))
            measure('preferences_select_source', lambda: dialog.source_editor.sources.setCurrentItem(dialog.source_editor._items['cal-1']))
            measure('preferences_source_color', lambda: dialog.source_editor.set_color('cal-1', '#30978c' if round_index % 2 == 0 else '#697ab1'))
        measure('preferences_save', dialog.save, predicate=lambda: not getattr(dialog, '_applying', False))
        if dialog.result() != QDialog.DialogCode.Accepted:
            raise RuntimeError('Preferences did not accept: ' + dialog.error.text())
        if Preferences.load(store) != window.preferences:
            raise RuntimeError('Applied settings did not persist.')
        dispose(dialog)

    def navigation_block(round_index, entry):
        for view, offset in zip(entry['views'], entry['offsets']):
            target = (ANCHOR + timedelta(days=offset + round_index * 2203)).date()
            measure('jump_date', lambda target=target: window.go_date(target), context={'target': target.isoformat(), 'view': window.view})
            measure('view_' + view, lambda view=view: window.set_view(view), context={'target': target.isoformat()})
            measure('navigate_next_' + view, lambda: window.navigate(1))
            measure('navigate_previous_' + view, lambda: window.navigate(-1))
            calendar = f'cal-{1 + round_index % (config.calendars - 1)}'
            measure('hide_calendar_' + view, lambda: window.toggle_calendar(calendar, False))
            measure('show_calendar_' + view, lambda: window.toggle_calendar(calendar, True))
        measure('full_day_toggle', window.toggle_full_day)
        measure('full_day_restore', window.toggle_full_day)
        measure('go_today', window.today)

    def creation_block(round_index):
        before = len(store.event_ids())
        def run_editor(title, configure=None):
            return lambda editor: accept_editor(editor, title, configure)
        for kind in ('timed', 'all_day', 'weekly', 'custom', 'alternatives'):
            title = f'Benchmark created {kind} {round_index}'
            def configure(editor, kind=kind):
                if kind != 'timed' and kind != 'all_day':
                    editor.schedule_mode.setCurrentData('alternatives' if kind == 'alternatives' else 'repeat')
                    if kind in ('custom', 'alternatives'):
                        editor.repeat.setCurrentData('custom')
                        editor.pattern.setText('+0d 4*+2w +3w 3*+2w')
                    else:
                        editor.repeat.setCurrentData('weekly')
                        editor.repeat_count.setValue(7)
            with patch.object(EventDialog, 'exec', run_editor(title, configure)):
                if kind == 'all_day':
                    measure('create_all_day_event', lambda: window.create_all_day_event(ANCHOR + timedelta(days=round_index + 2)))
                else:
                    measure('create_' + kind + '_event', lambda: window.create_event(ANCHOR + timedelta(days=round_index + 2)))
        if len(store.event_ids()) != before + 5:
            raise RuntimeError('Creating five events did not persist five source resources.')
        # Exercise the actual event-creation signals wired from each surface.
        for view, surface in (('week', 'canvas'), ('month', 'month'), ('day', 'header')):
            measure('creation_view_' + view, lambda view=view: window.set_view(view))
            with patch.object(EventDialog, 'exec', run_editor(f'Benchmark surface {view} {round_index}')):
                target = ANCHOR + timedelta(days=10 + round_index)
                widget = getattr(window, surface)
                signal = widget.create_all_day_requested if view == 'day' else widget.create_requested
                measure('create_from_' + view + '_surface', lambda: signal.emit(target, target + timedelta(hours=1) if view != 'day' else target + timedelta(days=1)))

    def editing_block(round_index):
        reference = focus_source()
        title = f'Benchmark edited {round_index}'
        with patch.object(EventDialog, 'exec', lambda editor: accept_editor(editor, title)):
            measure('edit_event_save', lambda: window.edit_event(reference.id))
        if store.get_event('bench-editable').title != title:
            raise RuntimeError('Edited title did not persist.')
        measure('undo_edit', window.undo)
        measure('redo_edit', window.redo)
        measure('undo_edit_restore', window.undo)
        reference = focus_source()
        measure('move_event', lambda: window.move_event(reference.id, reference.start + timedelta(minutes=5), reference.end + timedelta(minutes=5)))
        measure('undo_move', window.undo)
        reference = focus_source()
        measure('copy_by_drag', lambda: window.move_event(reference.id, reference.start + timedelta(days=1), reference.end + timedelta(days=1), copy=True))
        measure('undo_drag_copy', window.undo)
        reference = focus_source()
        measure('delete_event', lambda: window.delete_event(reference.id))
        if store.get_event('bench-editable') is not None:
            raise RuntimeError('Deletion did not remove the event.')
        measure('undo_delete', window.undo)

    def gestures_block(round_index):
        focus_source()
        canvas = window.canvas
        x = canvas.gutter + .5 * (canvas.width() - canvas.gutter) / len(canvas.days)
        y = canvas.time_top + (21 - canvas.start_hour) * canvas.hour_height
        begin = QPoint(round(x), round(y))
        finish = begin + QPoint(0, max(16, round(canvas.hour_height / 2)))
        measure('scroll_to_evening', lambda: window.scroll.ensureVisible(begin.x(), begin.y(), 32, 32))
        for offset in range(3):
            measure('hover_time_indicator', lambda offset=offset: QTest.mouseMove(canvas, begin + QPoint(0, offset * 3)))
        count = len(store.event_ids())
        def drag_new():
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=begin)
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(finish), QPointF(canvas.mapToGlobal(finish)),
                              Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(canvas, move)
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=finish)
        with patch.object(EventDialog, 'exec', lambda editor: accept_editor(editor, f'Benchmark drag-created {round_index}')):
            measure('create_by_mouse_drag', drag_new)
        if len(store.event_ids()) != count + 1:
            raise RuntimeError('Dragging an empty time range did not create one event.')
        measure('undo_mouse_creation', window.undo)
        measure('mouse_creation_month_view', lambda: window.set_view('month'))
        month = window.month
        point = QPoint(round(month.week_gutter + .5 * (month.width() - month.week_gutter) / 7),
                       round(month.header_height + 10))
        with patch.object(EventDialog, 'exec', lambda editor: accept_editor(editor, f'Benchmark double-click {round_index}')):
            measure('create_by_month_doubleclick', lambda: QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=point))
        if len(store.event_ids()) != count + 1:
            raise RuntimeError('Double-clicking an empty month header did not create one event.')
        measure('undo_doubleclick_creation', window.undo)

    def series_block(round_index):
        for scope in ('single', 'group', 'future'):
            reference = focus_source('bench-series')
            if scope == 'future':
                cutoff = store.get_event('bench-series').start + timedelta(weeks=3)
                measure('series_following_cutoff_date', lambda: window.go_date(cutoff.date()))
                reference = next(item for item in window.visible_events
                                 if item.series_id == 'bench-series' and item.start.date() == cutoff.date())
                measure('series_following_cutoff_select', lambda: window.select_event(reference.id))
            measure('selection_' + scope, lambda scope=scope: window.set_selection_mode(scope))
            measure('move_series_' + scope, lambda reference=reference, scope=scope: window.move_event(
                reference.id, reference.start + timedelta(days=1), reference.end + timedelta(days=1), scope=scope))
            measure('undo_series_' + scope, window.undo)
        measure('selection_restore_single', lambda: window.set_selection_mode('single'))
        reference = focus_source('bench-candidate')
        measure('confirm_alternative_slot', lambda: window.confirm_candidate(reference.id))
        if store.get_event('bench-candidate').candidate:
            raise RuntimeError('Alternative-slot confirmation did not commit.')
        measure('undo_slot_confirmation', window.undo)

    def heavy_selection_block(round_index):
        from dataclasses import replace
        def check(reference, scope):
            family = [event for event in window.visible_events
                      if event.series_id == 'bench-heavy' or event.id == 'bench-heavy']
            expected = expected_fixture_selection(family, reference, scope)
            if len(family) < 2 or window.selected_ids != expected:
                raise RuntimeError('Heavy-family selection mismatch: ' + json.dumps({
                    'round': round_index, 'scope': scope, 'view': window.view,
                    'anchor': str(window.anchor), 'primary': reference.id,
                    'search': window.search.text(), 'visible_family_count': len(family),
                    'selected': sorted(window.selected_ids), 'expected': sorted(expected),
                    'mouse_buttons': QApplication.mouseButtons().value,
                }))
        for scope in ('group', 'future'):
            # Selecting the primary can legitimately expose only one family
            # member when its date is the final day of the configured week.
            # Choose an interior week so every weekday preference exercises a
            # populated selection, then compare exact affected identities.
            reference = focus_source('bench-heavy', day_offset=21)
            measure('heavy_selection_' + scope, lambda scope=scope: window.set_selection_mode(scope))
            check(reference, scope)
            measure('heavy_selected_navigate_' + scope, lambda: window.navigate(1))
            if window.selected_ids or window.selection.refs:
                raise RuntimeError('Date navigation did not clear the previous event selection.')
            visible_family = [event for event in window.visible_events if event.series_id == 'bench-heavy']
            if len(visible_family) < 2:
                raise RuntimeError('The interior heavy-family week did not contain several occurrences.')
            reference = visible_family[len(visible_family) // 2]
            measure('heavy_selected_reselect_' + scope, lambda: window.select_event(reference.id))
            check(reference, scope)
            measure('heavy_selected_hide_calendar_' + scope, lambda: window.toggle_calendar('cal-1', False))
            check(reference, scope)
            measure('heavy_selected_show_calendar_' + scope, lambda: window.toggle_calendar('cal-1', True))
            check(reference, scope)
            measure('heavy_selected_apply_preferences_' + scope, lambda: window.apply_preferences(
                replace(window.preferences, show_week_numbers=not window.preferences.show_week_numbers), window.bindings))
            check(reference, scope)
        measure('heavy_selection_restore_single', lambda: window.set_selection_mode('single'))

    def clipboard_block(round_index):
        reference = focus_source()
        measure('copy_event', lambda: window.copy_event(reference.id))
        measure('paste_preview', window.paste_events)
        if window.pending_paste is None:
            raise RuntimeError('Paste did not create a preview.')
        def move_preview():
            window.pending_paste.update(reference.start + timedelta(days=2 + round_index))
            window.render_placement()
        measure('paste_move_preview', move_preview)
        measure('paste_confirm', window.confirm_paste)
        measure('undo_paste', window.undo)
        measure('redo_paste', window.redo)
        measure('undo_paste_restore', window.undo)
        measure('paste_preview_cancel', window.paste_events)
        measure('paste_cancel', window.cancel_paste)

    def calendars_block(round_index):
        before = {calendar.id for calendar in store.list_calendars()}
        def add(dialog):
            display(dialog)
            settle()
            dialog.name.setText(f'Benchmark added calendar {round_index}')
            dialog.color.setCurrentIndex(round_index % dialog.color.count())
            dialog.save()
            return QDialog.DialogCode.Accepted
        with patch.object(CalendarDialog, 'exec', add):
            measure('calendar_add', window.add_calendar)
        added = next(calendar for calendar in store.list_calendars() if calendar.id not in before)
        def edit(dialog):
            display(dialog)
            settle()
            dialog.name.setText(f'Benchmark renamed calendar {round_index}')
            dialog.color.setCurrentIndex((round_index + 2) % dialog.color.count())
            dialog.save()
            return QDialog.DialogCode.Accepted
        with patch.object(CalendarDialog, 'exec', edit):
            measure('calendar_edit', lambda: window.add_calendar(added))
        measure('calendar_select_creation', lambda: window.set_creation_calendar(added.id))
        measure('calendar_select_primary', lambda: window.set_creation_calendar('cal-0'))
        identifiers = [calendar.id for calendar in store.list_calendars()]
        measure('calendar_reorder', lambda: window.reorder_calendars(list(reversed(identifiers))))
        measure('calendar_hide_added', lambda: window.toggle_calendar(added.id, False))
        measure('calendar_show_added', lambda: window.toggle_calendar(added.id, True))

    def search_block(round_index):
        measure('search_open', window.focus_search)
        for label, query in (('ordinary', f'Synthetic ordinary-{round_index + 12}'),
                             ('series', 'Benchmark weekly series'),
                             ('many_series', 'Synthetic active recurring appointment'),
                             ('empty', 'NoMatchingSyntheticPhrase')):
            def type_query(query=query):
                window.search.setFocus()
                window.search.selectAll()
                QTest.keyClicks(window.search, query)
            measure('search_type_' + label, type_query, predicate=lambda: not window.search_timer.isActive())
            if label != 'empty':
                if not window.results.count():
                    raise RuntimeError('Expected search result was not shown.')
                measure('search_next_' + label, lambda: window.cycle_search_result(1))
                measure('search_previous_' + label, lambda: window.cycle_search_result(-1))
                if label in ('series', 'many_series'):
                    # Exercise expanded series paging through the same selected
                    # result API; its representation is asserted by app tests.
                    item = window.results.item(0)
                    if item is not None:
                        measure('search_activate_series', lambda: window.open_search_result(item))
                        measure('search_expand_' + label, lambda: window.results.expandItem(item))
                        if item.childCount() < 1:
                            raise RuntimeError('Expanding a recurring search result did not expose dates.')
                        if label == 'many_series':
                            footer = next((item.child(index) for index in range(item.childCount())
                                           if window.results.is_paging_action(item.child(index))), None)
                            if footer is None:
                                raise RuntimeError('An infinite series did not provide a paging action.')
                            measure('search_more_dates', lambda: window.results.load_more(footer))
                        measure('search_collapse_' + label, lambda: window.results.collapseItem(item))
            measure('search_filter_' + label, window.filter_search.click)
            measure('search_filter_restore_' + label, window.filter_search.click)
        measure('search_close', window.exit_search)

    def import_export_block(round_index):
        imported_events = []
        for index in range(config.import_events):
            start = ANCHOR + timedelta(days=index * 3 + round_index, hours=index % 6)
            imported_events.append(Event(f'import-{round_index}-{index}', 'benchmark-import',
                f'Benchmark import {round_index} {index}', start, start + timedelta(minutes=45),
                recurrence_rule='FREQ=WEEKLY;COUNT=4' if index % 20 == 0 else ''))
        payload = measure('export_import_fixture', lambda: export_ics(imported_events))
        dialog = measure('import_preview_open', lambda: display(ImportDialog(store.list_calendars(),
            sources=[ImportSource.from_data(payload, 'Synthetic benchmark import')],
            calendar_id='benchmark-import', preferences=window.preferences, parent=window)))
        measure('import_preview_ready', lambda: None, predicate=lambda: dialog.preview_ready)
        if dialog.prepared_import is None or not dialog.prepared_import.events:
            raise RuntimeError('Synthetic import did not prepare events.')
        for name, button in (('next_occurrence', dialog.preview.next_occurrence_button),
                             ('previous_occurrence', dialog.preview.previous_occurrence_button),
                             ('last_occurrence', dialog.preview.last_button),
                             ('first_occurrence', dialog.preview.first_button),
                             ('next_month', dialog.preview.next_button),
                             ('previous_month', dialog.preview.previous_button)):
            measure('import_' + name, button.click,
                predicate=lambda: all(getattr(dialog, field, None) is None for field in ('_page_future', '_navigation_future')))
        measure('import_deselect_all', lambda: dialog._select_all(False),
                predicate=lambda: getattr(dialog, '_selection_future', None) is None)
        measure('import_select_all', lambda: dialog._select_all(True),
                predicate=lambda: getattr(dialog, '_selection_future', None) is None)
        # Hand the reviewed dialog to the real workspace import queue. This
        # preserves asynchronous remapping, polling, database writes and repaint.
        dialog.save()
        if dialog.result() != QDialog.DialogCode.Accepted:
            raise RuntimeError('Reviewed import could not be accepted.')
        def reviewed(_dialog):
            _dialog.selected_prepared = dialog.selected_prepared
            _dialog.selected_sources = dialog.selected_sources
            _dialog.selected_calendar_id = dialog.selected_calendar_id
            return QDialog.DialogCode.Accepted
        with patch.object(ImportDialog, 'exec', reviewed):
            measure('import_commit', lambda: window.import_data(payload, calendar_id='benchmark-import'))
        if store.get_event(imported_events[0].id) is None:
            # Imported identities may be remapped from their UID into the new
            # calendar; count persisted resources rather than assume an ID.
            if len(store.list_events(calendar_ids=['benchmark-import'])) < config.import_events:
                raise RuntimeError('Accepted import did not persist its events.')
        dispose(dialog)
        exported = database.parent / f'calendar-{round_index}.ics'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(exported), 'Calendar files (*.ics)')):
            measure('export_calendar_file', lambda: window.export_ics('cal-0'))
        if not exported.exists() or not exported.stat().st_size:
            raise RuntimeError('Calendar export did not produce a file.')
        reference = focus_source()
        selected = database.parent / f'selection-{round_index}.ics'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(selected), 'Calendar files (*.ics)')):
            measure('export_selection_file', window.export_selection)
        if not selected.exists() or not parse_ics_report(selected.read_bytes(), 'check').events:
            raise RuntimeError('Selection export was not an importable calendar.')

    def notifications_block(round_index):
        from dataclasses import replace
        from PySide6.QtGui import QCursor
        from debacal.alarms import Alarm
        from debacal.recurrence import occurrence
        from debacal.reminder_controller import ReminderController
        from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
        from debacal.sequence_document import document_expression
        from benchmark_reminders import Clock, fake_transport

        # A separate small live workspace permits real enable/build and delivery
        # in every round while the large common workspace stays a demo.
        path = database.parent / f'notification-controls-{round_index}.sqlite'
        clock = Clock()
        transport = fake_transport()
        def factory(db, **opts):
            return ReminderController(db, clock=clock, transport=transport, **opts)
        with Store(path) as local:
            clock.time = ANCHOR - timedelta(minutes=1)
            due_alarm = Alarm('due', 'DISPLAY', 'PT0S', uid='due')
            due = Event('due', 'local', 'Due reminder', ANCHOR, ANCHOR+timedelta(hours=1), alarms=(due_alarm,))
            # Keep the draft and gesture workload well above a one-row editor.
            alarms = tuple(Alarm(f'a{i}', 'DISPLAY', f'-PT{i+1}M', uid=f'a{i}') for i in range(24))
            master = Event('bench-alarms', 'local', 'Reminder series', ANCHOR+timedelta(hours=2),
                           ANCHOR+timedelta(hours=3), alarms=alarms,
                           recurrence_rule='FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1', schedule_syntax=2)
            children = [occurrence(master, master.start+timedelta(days=i+1)) for i in range(12)]
            master = replace(master, recurrence_dates=tuple(child.recurrence_id for child in children),
                             recurrence_overrides={child.recurrence_id: child.to_dict() for child in children})
            master = replace(master, schedule_expression=document_expression(master))
            local.apply_remote([master, due])
            live = MainWindow(local, enable_sync=False, delivery_allowed=True, reminder_factory=factory)
            live.resize(config.width, config.height)
            live.show()
            live.open_day(ANCHOR.date())
            controller = live.reminder_controller
            pages = {}
            controller.inboxPageReady.connect(lambda request, rows: pages.update({request: rows}))
            try:
                prefs = measure('reminder_preferences_open', lambda: display(PreferencesDialog(
                    live.preferences, live, store=local, reminder_clock=clock.now)))
                prefs.remindersApplied.connect(live.apply_reminder_preferences)
                measure('reminder_preferences_category', lambda: prefs.set_category('Notifications'))
                page = prefs.notification_page
                measure('reminder_preferences_enable', page.enabled.click)
                page.delivery.setCurrentIndex(page.delivery.findData('in_app'))
                measure('reminder_preferences_save', prefs.save, predicate=lambda: not prefs._applying)
                dispose(prefs)
                # Request a FIFO worker barrier and verify durable index readiness.
                def request():
                    return controller.request_inbox(limit=1)
                def ready():
                    if not Path(str(path)+'.reminders.sqlite3').is_file():
                        return False
                    import sqlite3
                    from contextlib import closing
                    with closing(sqlite3.connect(str(path)+'.reminders.sqlite3')) as reader:
                        rows = dict(reader.execute('SELECT key,value FROM metadata'))
                        return rows.get('bootstrap') == 'null' and bool(rows.get('cursor'))
                measure('reminder_index_ready', controller.reload_settings, predicate=ready)
                requests = []
                measure('reminder_idle_tick', lambda: requests.append(request()), predicate=lambda: requests[-1] in pages)
                def advance():
                    clock.time = ANCHOR
                    controller.reload_settings()
                measure('reminder_due_batch', advance, predicate=lambda: controller.unread_count == 1)
                measure('reminder_inbox_open', live.show_reminders,
                        predicate=lambda: live._reminder_dialog.rows.topLevelItemCount() == 1)
                live._reminder_dialog.close()
                ref = occurrence(master, master.start)
                live.select_event(ref.id)
                live.set_selection_mode('group')
                measure('reminder_series_copy', lambda: live.copy_event(ref.id))
                measure('reminder_series_paste_start', live.paste_events)
                live._paste_mouse_origin = QCursor.pos()+QPoint(3,3)
                target = ref.start+timedelta(minutes=15)
                measure('reminder_series_paste_hover', lambda: live.move_paste_preview(target))
                assert live.pending_paste.events[0].start == target
                measure('reminder_series_paste_confirm', live.confirm_paste,
                        predicate=lambda: not live.database_commands.pending)
                assert len(local.list_events()) == 3
                live.select_event(ref.id)
                measure('reminder_drag_start', lambda: live.preview_drag(ref.id, ref.start, ref.end, Qt.KeyboardModifier.NoModifier))
                measure('reminder_drag_hover', lambda: live.preview_drag(ref.id, target, ref.end+timedelta(minutes=15),
                                                                          Qt.KeyboardModifier.ControlModifier))
                assert live._drag_preview_events
                live.clear_drag_preview()
                editor = measure('reminder_editor_open', lambda: display(EventDialog(local.list_calendars(), ref.start,
                    event=ref, series=master, edit_scope='group', preferences=live.preferences, parent=live)))
                measure('reminder_editor_list', lambda: editor.notes_choice.setCurrentData('reminders'))
                measure('reminder_editor_timing', lambda: editor.start.set_value(target))
                measure('reminder_editor_remove', editor.reminders.remove_buttons[-1].click)
                measure('reminder_editor_save', editor.save)
                assert editor.result() == QDialog.DialogCode.Accepted
                draft = editor.result_events[0]
                replacement, alarm_edit = editor.schedule_replacement, editor.alarm_edit()
                def commit_editor(writer):
                    operation = writer.edit_selection((ref,), 'group', ref, draft, replace_schedule=replacement,
                                                      expected_masters=(master,), alarm_edit=alarm_edit)
                    return writer.created_event_ids(operation)
                measure('reminder_editor_commit', lambda: live.write_database(commit_editor, force_async=True),
                        predicate=lambda: not live.database_commands.pending)
                assert len(local.get_event(master.id).alarms) == len(alarms)-1
                dispose(editor)
                wire = export_ics([replace(due, id='incoming', ical_uid='incoming')])
                dialog = measure('reminder_import_open', lambda: display(ImportDialog(local.list_calendars(),
                    sources=[ImportSource.from_data(wire)], calendar_id='local', parent=live)))
                measure('reminder_import_ready', lambda: None, predicate=lambda: dialog.preview_ready)
                measure('reminder_import_remove', lambda: dialog.reminder_mode.setCurrentIndex(dialog.reminder_mode.findData('remove')))
                dialog.save()
                def reviewed(actual):
                    actual.selected_prepared = dialog.selected_prepared
                    actual.selected_calendar_id = dialog.selected_calendar_id
                    actual.selected_sources = dialog.selected_sources
                    return QDialog.DialogCode.Accepted
                with patch.object(ImportDialog, 'exec', reviewed):
                    measure('reminder_import_commit', lambda: live.import_data(wire, calendar_id='local'),
                            predicate=lambda: any(e.ical_uid == 'incoming' for e in local.list_events()))
                assert next(e for e in local.list_events() if e.ical_uid == 'incoming').alarms == ()
                dispose(dialog)
                measure('reminder_single_source_update', lambda: live.apply_events('Rename', [replace(due,title='Revised')]),
                        predicate=lambda: not live.database_commands.pending and local.get_event('due').title == 'Revised')
                live.apply_reminder_preferences(replace(live.reminder_preferences, close_behavior='tray'))
                with patch.object(live.tray, 'available', return_value=True):
                    live.close()
                    assert not live.isVisible()
                    measure('reminder_tray_restore', live.tray._menu.actions()[0].trigger, predicate=live.isVisible)
            finally:
                live.request_quit(force=True)
                if controller._retiring:
                    controller._retiring.join(10)
                live.deleteLater()
                pump()

    def dialogs_block(round_index):
        editor = measure('editor_open', lambda: display(EventDialog(store.list_calendars(), ANCHOR,
            ANCHOR + timedelta(hours=1), preferences=window.preferences, parent=window)))
        editor.title.setText('Benchmark dialog draft')
        measure('editor_all_day_on', lambda: editor.all_day.setChecked(True))
        measure('editor_all_day_off', lambda: editor.all_day.setChecked(False))
        measure('editor_repeat_mode', lambda: editor.schedule_mode.setCurrentData('repeat'))
        for frequency in ('daily', 'weekly', 'monthly', 'yearly', 'custom'):
            measure('editor_frequency_' + frequency, lambda frequency=frequency: editor.repeat.setCurrentData(frequency))
        def type_sequence():
            editor.pattern.setFocus()
            editor.pattern.selectAll()
            QTest.keyClicks(editor.pattern, '+0d 4*+2w +3w 3*+2w')
        measure('editor_sequence_type', type_sequence)
        measure('editor_preview_next', editor.preview.next_occurrence_button.click)
        measure('editor_preview_last', editor.preview.last_button.click)
        measure('editor_preview_first', editor.preview.first_button.click)
        measure('editor_toggle_date', lambda: editor.toggle_date(ANCHOR.date() + timedelta(days=3)))
        measure('editor_sequence_build', editor.build_events)
        measure('editor_close', lambda: dispose(editor))
        picker = measure('picker_open', lambda: display(DateTimeRangePicker(ANCHOR,
            ANCHOR + timedelta(hours=1), preferences=window.preferences, parent=window)))
        measure('picker_start', lambda: picker.set_start(ANCHOR + timedelta(minutes=5)))
        measure('picker_end', lambda: picker.set_end(ANCHOR + timedelta(hours=3)))
        measure('picker_date', lambda: picker.choose_date(ANCHOR.date() + timedelta(days=3)))
        measure('picker_full_day', lambda: picker.set_full_day(True))
        measure('picker_close', lambda: dispose(picker))
        history = measure('history_open', lambda: display(HistoryDialog(store, window.refresh, window)))
        measure('history_close', lambda: dispose(history))
        source = store.get_event('bench-editable')
        from dataclasses import replace
        remote = replace(source, title='Synthetic server alternative')
        conflict = SyncConflict('benchmark-conflict', source.calendar_id, source.id, source, remote,
                                'Synthetic conflict', 'synthetic', (remote,))
        conflict_dialog = measure('conflict_open', lambda: display(ConflictDialog(conflict, window.preferences, window)))
        measure('conflict_next_month', lambda: conflict_dialog.navigate_month(1))
        measure('conflict_close', lambda: dispose(conflict_dialog))

    profile = None
    if profile_path:
        import cProfile
        profile = cProfile.Profile()
        profile.enable()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch('debacal.ui.now', return_value=ANCHOR))
            stack.enter_context(patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)))
            def unexpected_message(*_args, **_kwargs):
                raise RuntimeError('Unexpected message box during ' + str(active))
            for method in ('warning', 'critical', 'question', 'information'):
                stack.enter_context(patch.object(QMessageBox, method, unexpected_message))
            def opened():
                nonlocal window
                window = MainWindow(store, enable_sync=False, demo_mode=True)
                window.resize(config.width, config.height)
                window.show()
                return window
            measure('startup_workspace', opened)
            if paint_count == 0:
                raise RuntimeError('Workspace did not render its first frame.')
            startup = {'imports_ms': round(imports_ms, 3), 'qt_application_ms': round(qt_ms, 3),
                       'store_open_ms': round(open_ms, 3),
                       'worker_to_first_paint_ms': round((time.perf_counter() - process_started) * 1000, 3),
                       'workspace_ms': records[0]['elapsed_ms']}
            if mode == 'startup':
                report = {'startup': startup}
                verify_source(SOURCE_ROOT)
                print(json.dumps(report), flush=True)
                return
            blocks = {'navigation': navigation_block, 'preferences': preferences_block,
                      'creation': creation_block, 'gestures': gestures_block, 'editing': editing_block, 'series': series_block,
                      'heavy_selection': heavy_selection_block,
                      'clipboard': clipboard_block, 'calendars': calendars_block, 'search': search_block,
                      'import_export': import_export_block, 'dialogs': dialogs_block, 'notifications': notifications_block}
            plan = interaction_plan(config.rounds, config.seed)
            for entry in plan:
                for block in entry['blocks']:
                    scenario_context.update(round=entry['round'], block=block)
                    if block == 'navigation':
                        blocks[block](entry['round'], entry)
                    else:
                        blocks[block](entry['round'])
                import threading
                pages = int(Path('/proc/self/statm').read_text().split()[1])
                usage_samples.append({'round': entry['round'],
                    'resident_mib': round(pages * os.sysconf('SC_PAGE_SIZE') / 1024**2, 3),
                    'threads': threading.active_count(), 'open_fds': len(list(Path('/proc/self/fd').iterdir())),
                    'sources': len(store.event_ids()), 'completed_interactions': len(records)})
            totals = defaultdict(list)
            dispatch = defaultdict(list)
            gaps = defaultdict(list)
            for record in records:
                totals[record['operation']].append(record['elapsed_ms'])
                dispatch[record['operation']].append(record['dispatch_ms'])
                gaps[record['operation']].append(record['max_event_loop_gap_ms'])
            operations = {name: {**summary(values), 'dispatch': summary(dispatch[name]),
                                'worst_event_loop_gap_ms': max(gaps[name])}
                          for name, values in totals.items()}
            repeatable = {name: item for name, item in operations.items() if name != 'startup_workspace'}
            report = {'startup': startup, 'operations': operations, 'records': records, 'plan': plan,
                      'usage_samples': usage_samples,
                      'coverage': {'operation_types': len(repeatable), 'actions': len(records),
                                   'expected_minimum_samples': config.rounds,
                                   'minimum_samples': min(item['samples'] for item in repeatable.values()),
                                   'under_sampled': [name for name, item in repeatable.items() if item['samples'] < config.rounds]},
                      'environment': {'python': platform.python_version(), 'pyside6': qt_version,
                                      'backend': app.platformName(), 'qt_style': app.style().objectName(),
                                      'font': app.font().family(), 'window_size': [window.width(), window.height()],
                                      'screen_size': [app.primaryScreen().size().width(), app.primaryScreen().size().height()],
                                      'cpus_available': len(os.sched_getaffinity(0)),
                                      'platform': platform.platform(),
                                      'dependency_versions': {name: importlib.metadata.version(name)
                                          for name in ('icalendar', 'python-dateutil', 'PySide6')},
                                      'profiled': bool(profile)},
                      'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)}
            verify_source(SOURCE_ROOT)
            print(json.dumps(report), flush=True)
    finally:
        if profile:
            profile.disable()
            profile.dump_stats(str(profile_path))
        heartbeat.stop()
        watchdog.stop()
        for dialog in reversed(dialogs):
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
        pump()
        if window is not None:
            settle()
            window.close()
            window.executor.shutdown(wait=True, cancel_futures=True)
            commands = getattr(window, 'database_commands', None)
            if commands is not None:
                commands.executor.shutdown(wait=True)
            window.deleteLater()
        app.clipboard().clear()
        pump()
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--revision', default='', help='Revision label for archived source snapshots.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path, help='Optional cProfile output; do not compare profiled timings.')
    parser.add_argument('--progress', type=Path, help='Append progress JSON lines, including completed individual timings.')
    parser.add_argument('--quick', action='store_true', help='Small local smoke run; not a performance comparison.')
    for name in ('events', 'series', 'finite', 'active_series', 'heavy_overrides', 'candidate_dates', 'calendars', 'payload_bytes', 'rounds', 'startup_runs',
                 'import_events', 'seed', 'width', 'height', 'timeout_seconds'):
        parser.add_argument('--' + name.replace('_', '-'), type=int)
    parser.add_argument('--worker', choices=('fixture', 'startup', 'usage'), help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--configuration', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        global WORKER_STARTED, SOURCE_ROOT
        WORKER_STARTED = time.perf_counter()
        SOURCE_ROOT = args.source_root.resolve()
        sys.path.insert(0, str(SOURCE_ROOT))
        config = Config(**json.loads(args.configuration))
        verify_source(SOURCE_ROOT)
        if args.worker == 'fixture':
            print(json.dumps(build_fixture(args.database, config)), flush=True)
        else:
            desktop_worker(args.database, config, args.worker, args.profile)
        return 0
    values = asdict(Config())
    if args.quick:
        values.update(events=80, series=20, finite=3, active_series=6, heavy_overrides=20, candidate_dates=30, rounds=2, startup_runs=1,
                      import_events=12, payload_bytes=128)
    for name in values:
        value = getattr(args, name)
        if value is not None:
            values[name] = value
    config = Config(**values)
    if min(config.events, config.series, config.finite, config.active_series, config.heavy_overrides, config.payload_bytes) < 0:
        parser.error('Fixture sizes must be nonnegative.')
    if config.calendars < 2 or min(config.rounds, config.startup_runs, config.import_events,
                                  config.width, config.height, config.timeout_seconds) < 1:
        parser.error('At least two calendars and positive rounds, startup runs, imports and dimensions are required.')
    root = args.source_root.resolve()
    metadata = source_metadata(root, args.revision)
    with tempfile.TemporaryDirectory(prefix='debacal-extended-benchmark-') as temporary:
        directory = Path(temporary)
        database = directory / 'fixture.sqlite3'
        fixture = run_worker(root, config, 'fixture', database, directory / 'fixture-env')
        startups = []
        for index in range(config.startup_runs):
            target = directory / f'startup-{index}.sqlite3'
            shutil.copy2(database, target)
            result = run_worker(root, config, 'startup', target, directory / f'startup-env-{index}')
            startups.append({**result['startup'], 'process_to_first_paint_ms': result['process_to_report_ms']})
        target = directory / 'usage.sqlite3'
        shutil.copy2(database, target)
        usage = run_worker(root, config, 'usage', target, directory / 'usage-env', profile=args.profile, progress=args.progress)
    report = {'schema_version': 1, 'source': metadata, 'configuration': asdict(config),
              'harness': {'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'generator_sha256': hashlib.sha256(Path(__file__).with_name('benchmark_ranges.py').read_bytes()).hexdigest()},
              'fixture': fixture, 'startup_samples': startups,
              'startup_summary': {name: summary([sample[name] for sample in startups]) for name in startups[0]},
              **usage}
    report['methodology'] = {'storage': 'file-backed SQLite, each version builds its native schema',
                             'startup': 'fresh processes and database copies; OS filesystem cache is not flushed',
                             'warmup_queries': 0, 'timing': 'dispatch through queued work, event processing and paint',
                             'network': 'remote/synchronization actions disabled; no real accounts or user profiles',
                             'input_driver': 'QTest pointer/typing and shared UI command handlers; forms auto-filled without human think time',
                             'interaction_blocks': list(BLOCKS), 'local_only': True}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'output': str(args.output), 'operations': len(usage['operations']),
                          'interactions': len(usage['records']), 'worst_ms': max(
                              item['max_ms'] for item in usage['operations'].values())}, indent=2))
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
