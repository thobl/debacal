"""Local reminder workflow measurements using synthetic SQLite profiles and fake delivery."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event as ThreadEvent
from time import monotonic, sleep
from zoneinfo import ZoneInfo

if __package__:
    from .benchmark_extended import isolated_environment, source_metadata, summary
else:
    from benchmark_extended import isolated_environment, source_metadata, summary

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)


def build_profile(path, count):
    from debacal.alarms import Alarm
    from debacal.ics import export_ics
    from debacal.models import Calendar, Event
    from debacal.recurrence import occurrence
    from debacal.store import Store

    cohort = min(72, count // 3)
    counts = Counter(sources=count, due=cohort)
    with Store(path) as store:
        for identity in ('active', 'hidden', 'disabled'):
            store.save_calendar(Calendar(identity, identity.title(), '#426788', visible=identity != 'hidden',
                                         enabled=identity != 'disabled'))
        batch = []
        for i in range(count):
            identity = f'source-{i:06}'
            zone = ZoneInfo(('America/New_York', 'Europe/Berlin', 'Asia/Tokyo')[i % 3])
            calendar = 'active' if i <= cohort else ('disabled' if i % 17 == 0 else 'hidden' if i % 7 == 0 else 'active')
            when = BASE if i < cohort else (BASE + (1 + i*37 % 1600)*DAY * (-1 if i%4 == 1 else 1))
            start = when.astimezone(zone)
            alarms = ()
            rule = ''
            if i <= cohort:
                alarms = (Alarm('due', 'DISPLAY', 'PT0S'),)
            elif i % 5 == 0:
                mode = i % 6
                alarms = (Alarm('alarm', 'EMAIL' if mode == 1 else 'DISPLAY',
                                '20281121T090000Z' if mode == 2 else '-PT5M', related='END' if mode == 3 else 'START',
                                repeat=2 if mode == 4 else 0, repeat_interval='PT15M' if mode == 4 else None),)
                rule = 'FREQ=WEEKLY;COUNT=8' if mode == 5 else ''
            all_day = i > cohort and i % 23 == 0
            if all_day:
                start = start.replace(hour=0, minute=0)
            event = Event(identity, calendar, f'Synthetic appointment {i}', start,
                          start + (DAY if all_day else 60*MINUTE), alarms=alarms, all_day=all_day, recurrence_rule=rule)
            if i > cohort and i % 251 == 0:
                event = replace(event, recurrence_rule='FREQ=DAILY;COUNT=4', alarms=(Alarm('master', 'DISPLAY', '-PT6M'),))
                item = replace(occurrence(event, start+DAY), alarms=(Alarm('child', 'AUDIO', '-PT3M'),))
                event = replace(event, recurrence_overrides={item.recurrence_id: item.to_dict()})
                raw = export_ics([event]).decode()
                event = replace(event, raw_ics=raw, alarms=None, recurrence_overrides={
                    item.recurrence_id: {key: value for key, value in item.to_dict().items() if key != 'alarms'}})
                counts['legacy_families'] += 1
            counts['saved_alarm_components'] += (2 if event.alarms is None else bool(event.alarms))
            counts['disabled_sources'] += calendar == 'disabled'
            counts['hidden_sources'] += calendar == 'hidden'
            batch.append(event)
            if len(batch) == 250 or i+1 == count:
                store.apply_remote(batch)
                batch.clear()
    return dict(counts)


class Clock:
    time = BASE - MINUTE

    def now(self):
        return self.time


def fake_transport():
    from debacal.reminder_transport import NotificationTransport
    class SyntheticTransport(NotificationTransport):
        def start(self):
            pass

        @property
        def available(self):
            return True

        def send(self, due, **_):
            self.delivered.emit(due.key, 1)
    return SyntheticTransport()


def resident_mib():
    pages = int(Path('/proc/self/statm').read_text().split()[1])
    return round(pages * os.sysconf('SC_PAGE_SIZE') / 1024**2, 3)


class Measurements:
    def __init__(self, app):
        from PySide6.QtCore import QEvent, QObject, QTimer
        self.app = app
        self.records = []
        self.last = monotonic()
        self.gap = 0.
        self.paints = 0
        class CounterFilter(QObject):
            def eventFilter(_, obj, event):
                if event.type() == QEvent.Type.Paint:
                    self.paints += 1
                return False
        self.counter = CounterFilter(app)
        app.installEventFilter(self.counter)
        self.heartbeat = QTimer(app)
        self.heartbeat.setInterval(10)
        self.heartbeat.timeout.connect(self.tick)
        self.heartbeat.start()

    def tick(self):
        now = monotonic()
        self.gap = max(self.gap, now - self.last)
        self.last = now

    def measure(self, name, action, ready=lambda: True):
        self.last = start = monotonic()
        self.gap = 0.
        before = self.paints
        result = action()
        dispatch = monotonic() - start
        while True:
            self.app.processEvents()
            if ready():
                break
            if monotonic() - start > 600:
                raise TimeoutError(name)
            sleep(.005)
        self.tick()
        self.records.append(dict(operation=name, elapsed_ms=round((monotonic()-start)*1000, 3),
                                 dispatch_ms=round(dispatch*1000, 3), max_event_loop_gap_ms=round(self.gap*1000, 3),
                                 paint_events=self.paints-before))
        return result


def history_stress(path, meter, size=2048):
    """Use real claims and a second sidecar connection while rebuilding a dense rule."""
    from debacal.alarms import Alarm
    from debacal.models import Event
    from debacal.reminder_owner import ReminderOwner
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_schedule import compile_schedule
    from debacal.reminder_store import ReminderStore
    from debacal.store import Store

    zone = ZoneInfo('America/New_York')
    # A backward DST fold provides one civil HOURLY slot for two elapsed hours.
    first = (BASE - timedelta(hours=size + 1)).astimezone(zone)
    source = Event('history', 'local', 'Historical series', first, first + 20*MINUTE,
                   recurrence_rule='FREQ=HOURLY', alarms=(Alarm('clock', 'DISPLAY', 'PT0S', uid='clock'),))
    owner = ReminderOwner(str(path))
    assert owner.acquire()
    state = {}
    writer_held = ThreadEvent()
    durations = []
    def start():
        canonical = state['canonical'] = Store(path)
        canonical.apply_remote([source])
        store = state['store'] = ReminderStore(str(path))
        store.attach_owner(owner, now=first)
        def put(event):
            canonical.apply_remote([event])
            change, = store.read_sources('selected', [event.id])
            store.put_source(change, compile_schedule(change, CalendarReminderPolicy(), zone=zone),
                             cursor=(change.version, event.id))
        state['put'] = put
        put(source)
        issued = []
        clock = first.astimezone(timezone.utc)
        while clock <= BASE:
            high = min(clock + 12 * timedelta(hours=1), BASE)
            while store.next_wakeup() is not None and store.next_wakeup() <= high:
                for due in store.claim_due(high, limit=64):
                    store.record_delivery(due.key, desktop_id=None, unread=True)
                    issued.append(due.key)
            clock = high + MINUTE
        assert len(issued) >= size, len(issued)
        store.snooze(issued[-1], BASE + 5*MINUTE)
        state['issued'] = issued
        original = store._write
        @contextmanager
        def measured():
            with original():
                began = monotonic()
                writer_held.set()
                try:
                    yield
                finally:
                    durations.append(round((monotonic()-began)*1000, 3))
        store._write = measured

    def ownerless_action():
        side = state['observer']
        began = monotonic()
        retries = 0
        while True:
            try:
                side.mark_read([state['issued'][0]])
                return dict(elapsed_ms=round((monotonic()-began)*1000, 3), busy_retries=retries)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if monotonic()-began > 15:
                    raise TimeoutError('Ownerless history action')
                retries += 1
                sleep(.002)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool, closing(ReminderStore(str(path))) as observer:
            state['observer'] = observer
            job = pool.submit(start)
            while not job.done():
                meter.app.processEvents()
                sleep(.005)
            job.result()
            counts = list(range(60))
            dense = 'FREQ=DAILY;BYHOUR=' + ','.join(map(str,range(24))) + ';BYMINUTE=' + ','.join(map(str,counts)) + ';BYSECOND=0,15,30,45'
            actions = {}
            for name, changed in [('metadata', replace(source, title='Updated title')),
                                  ('rule', replace(source, title='Updated title', recurrence_rule=dense)),
                                  ('duration', replace(source, title='Updated title', recurrence_rule=dense, end=source.end+MINUTE))]:
                writer_held.clear()
                durations.clear()
                def submit():
                    nonlocal job
                    job = pool.submit(state['put'], changed)
                meter.measure('history_' + name + '_edit', submit, lambda: job.done())
                job.result()
                # Exercise an independent writer against the same profile.
                # Repeat under actual contention in a separate instrumented pass.
                actions[name] = dict(writer_sections_ms=list(durations))
            ready = ThreadEvent()
            def action():
                with closing(ReminderStore(str(path))) as other:
                    state['observer'] = other
                    ready.set()
                    if not writer_held.wait(10):
                        raise TimeoutError('History edit did not acquire its sidecar writer')
                    return ownerless_action()
            with ThreadPoolExecutor(max_workers=1) as secondary:
                writer_held.clear()
                other = secondary.submit(action)
                assert ready.wait(10)
                job = pool.submit(state['put'], replace(changed, end=changed.end+MINUTE))
                meter.measure('history_concurrent_action', lambda: None, lambda: other.done() and job.done())
                job.result()
                actions['concurrent'] = other.result()
            def finalize():
                store = state['store']
                status = store.entry(state['issued'][-1]).status
                store.close()
                state['canonical'].close()
                return status
            return dict(groups=len(state['issued']), active_snoozes_before=1, final_snooze=pool.submit(finalize).result(),
                        actions=actions)
    finally:
        owner.release()


def profile_worker(path, phase, round_no, count):
    from PySide6 import __version__ as qt_version
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from debacal.reminder_controller import ReminderController
    from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
    from debacal.store import Store
    from debacal.ui import MainWindow

    app = QApplication([])
    app.setStyle('Fusion')
    app.setFont(QFont('DejaVu Sans', 10))
    meter = Measurements(app)
    clock = Clock()
    transport = fake_transport()
    def factory(database, **opts):
        return ReminderController(database, clock=clock, transport=transport, **opts)

    side = Path(str(path)+'.reminders.sqlite3')
    db = None
    def sql(query, params=()):
        nonlocal db
        if db is None and side.is_file():
            db = sqlite3.connect(side.as_uri()+'?mode=ro', uri=True, timeout=0)
        if db is not None:
            try:
                return db.execute(query, params).fetchone()
            except sqlite3.OperationalError as error:
                if 'locked' not in str(error) and 'no such table' not in str(error):
                    raise
        return None

    def setting(name):
        row = sql('SELECT value FROM metadata WHERE key=?', (name,))
        return json.loads(row[0]) if row else None
    def built():
        cursor = setting('cursor')
        return bool(cursor and cursor[0] >= revision and setting('bootstrap') is None and not setting('controller').get('scans'))

    store = meter.measure('store_open_' + phase, lambda: Store(path))
    revision = store.events_revision()
    start_partition = setting('partition')
    if (phase == 'enabled') != bool(start_partition):
        raise RuntimeError('Unexpected initial reminder partition')
    def open_window():
        window = MainWindow(store, enable_sync=False, delivery_allowed=True, reminder_factory=factory)
        window.resize(1400, 900)
        window.show()
        return window
    window = meter.measure('workspace_' + phase, open_window, lambda: meter.paints > 0)
    spawn = os.environ.get('DEBACAL_BENCHMARK_SPAWN_MONOTONIC')
    if spawn is not None:
        paint = dict(meter.records[-1], operation='process_to_first_paint_' + phase,
                     elapsed_ms=round((monotonic()-float(spawn))*1000, 3))
        meter.records.append(paint)
    controller = window.reminder_controller
    pages = {}
    controller.inboxPageReady.connect(lambda request, entries: pages.update({request: entries}))
    request = None
    def page():
        nonlocal request
        request = controller.request_inbox(limit=1)
    index = {}
    try:
        if phase == 'disabled':
            meter.measure('disabled_worker_idle', page, lambda: request in pages)
            assert not side.exists()
            before = resident_mib()
            def enable():
                prefs = ReminderPreferences(enabled=True, delivery='in_app')
                save_reminder_preferences(store, prefs, now=clock.now())
                window.apply_reminder_preferences(prefs)
            meter.measure('enable_and_build', enable, built)
            index.update(rss_before_mib=before, rss_after_mib=resident_mib(),
                         source_rows=sql('SELECT count(*) FROM source')[0],
                         frontier_rows=sql('SELECT count(*) FROM frontier')[0],
                         allocated_bytes=sql('PRAGMA page_count')[0]*sql('PRAGMA page_size')[0],
                         database_bytes=side.stat().st_size,
                         wal_bytes=Path(str(side)+'-wal').stat().st_size)
        else:
            meter.measure('ready_index_restart', page, lambda: request in pages and built())
            assert setting('partition') == start_partition
            index['retained_partition'] = True
            selected = store.get_event(f'source-{min(72, count//3):06}')
            def edit():
                window.apply_events('Change source', [replace(selected, title='Synthetic revision')])
            def changed():
                if window.database_commands.pending:
                    return False
                row = sql('SELECT revision FROM source WHERE id=?', (selected.id,))
                return row and row[0] > revision
            def idle():
                meter.measure('idle_worker_tick', page, lambda: request in pages)
            def due():
                def advance():
                    clock.time = BASE
                    controller.reload_settings()
                meter.measure('drain_due_cohort', advance, lambda: controller.unread_count == min(72, count//3))
            def navigation():
                rng = random.Random(82351 + round_no)
                day = BASE.date() + timedelta(days=rng.randrange(-600, 600))
                meter.measure('cold_navigation', lambda: window.open_day(day))
            def update():
                meter.measure('single_source_commit_and_index', edit, changed)
            operations = [idle, due, navigation, update]
            random.Random(1842 + round_no).shuffle(operations)
            for operation in operations:
                operation()
            # Hold an independent sync-style canonical transaction through actual
            # navigation and inbox response. The queued edit must settle afterwards.
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                meter.measure('held_writer_dispatch', lambda: window.apply_events('Held edit', [
                    replace(selected, title='Edited while syncing')]))
                assert window.database_commands.pending
                meter.measure('held_writer_navigation', lambda: window.go_date(BASE.date()))
                meter.measure('held_writer_inbox', page, lambda: request in pages)
                meter.measure('held_writer_release_to_commit', writer.commit, lambda: not window.database_commands.pending)
            assert store.get_event(selected.id).title == 'Edited while syncing'
        history = history_stress(path.with_suffix('.history.sqlite'), meter, min(2048, max(48, count//5))) if phase == 'enabled' else {}
        return dict(records=meter.records, index=index, history=history, environment=dict(backend=app.platformName(),
                    python=platform.python_version(), pyside6=qt_version, sqlite=sqlite3.sqlite_version,
                    platform=platform.platform(), cpus=len(os.sched_getaffinity(0))))
    finally:
        if db:
            db.close()
        window.close()
        if controller._retiring:
            controller._retiring.join(15)
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=int, default=20000)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--output', type=Path, default=ROOT/'design/notification-performance.json')
    parser.add_argument('--worker', choices=('disabled', 'enabled'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--round-number', type=int, default=0)
    args = parser.parse_args()
    if args.sources < 6 or args.rounds < 1:
        parser.error('Use at least six sources and one round.')
    if args.worker:
        print(json.dumps(profile_worker(args.database, args.worker, args.round_number, args.sources)), flush=True)
        return
    rounds = []
    with tempfile.TemporaryDirectory(prefix='debacal-synthetic-reminders-') as folder:
        directory = Path(folder)
        fixture = directory/'fixture.sqlite'
        workload = build_profile(fixture, args.sources)
        for index in range(args.rounds):
            profile = directory/f'round-{index}.sqlite'
            shutil.copy2(fixture, profile)
            # Reopen the same physical profile for the enabled phase so its
            # derived index still belongs to the same canonical file identity.
            for phase in ('disabled', 'enabled'):
                command = [sys.executable, __file__, '--worker', phase, '--round-number', str(index),
                           '--sources', str(args.sources), '--database', str(profile)]
                print(f'Round {index+1}/{args.rounds}, {phase}', file=sys.stderr, flush=True)
                env = isolated_environment(directory/f'env-{index}-{phase}')
                env['DEBACAL_BENCHMARK_SPAWN_MONOTONIC'] = str(monotonic())
                output = subprocess.check_output(command, env=env, text=True, timeout=900)
                rounds.append(dict(round=index+1, phase=phase, **json.loads(output)))
    operations = defaultdict(list)
    for record in (row for result in rounds for row in result['records']):
        operations[record['operation']].append(record)
    source = source_metadata(ROOT, subprocess.check_output(['git','rev-parse','--short','HEAD'], cwd=ROOT,text=True).strip())
    report = dict(source=source, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  workload=workload, rounds=rounds, environment=rounds[0]['environment'],
                  methodology='Fresh process and synthetic profile per disabled run, original files reopened for ready-index startup. '
                              'Normal OS filesystem caching. Seeded mixed action order and random date windows. Fake delivery.',
                  operations={name: {**summary([r['elapsed_ms'] for r in rows]),
                              'dispatch': summary([r['dispatch_ms'] for r in rows]),
                              'worst_event_loop_gap_ms': max(r['max_event_loop_gap_ms'] for r in rows)}
                              for name, rows in operations.items()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
