"""Calendar processing uses real child processes without sharing the UI's GIL."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import subprocess
import sys
from threading import Thread, get_ident
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from debacal.database_priority import foreground_work, get_write_gate
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.sync import SyncEngine
from debacal.credentials import CredentialError
from debacal.sync_worker import ProcessingCancelled, SyncProcessor, _bind_parent, decode_bridge, encode_conflicts
from debacal.vdirsyncer_backend import BridgeConflict


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def database(path, *, url='https://calendar.test/calendar.ics'):
    with Store(path) as store:
        store.save_calendar(Calendar('subscription', 'Synthetic subscription', '#587c67', kind='ics', url=url))
    return path


def launch_replacement(monkeypatch, code):
    original = subprocess.Popen
    processes, calls = [], []
    def launch(command, **options):
        calls.append((command, options))
        process = original([sys.executable, '-c', code], **options)
        processes.append(process)
        return process
    monkeypatch.setattr('debacal.sync_worker.subprocess.Popen', launch)
    return processes, calls


def gate_descriptors():
    result = []
    for path in Path('/proc/self/fd').iterdir():
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            continue
        if 'memfd:debacal-write-priority' in target:
            result.append(path.name)
    return result


def test_real_subscription_child_publishes_many_events_while_qt_keeps_dispatching(tmp_path):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Debacal synthetic test//EN']
    for index in range(1500):
        lines.extend(['BEGIN:VEVENT', f'UID:synthetic-{index}', f'SUMMARY:Appointment {index}',
                      'DTSTART:20261104T090000Z', 'DTEND:20261104T100000Z', 'END:VEVENT'])
    content = ('\r\n'.join([*lines, 'END:VCALENDAR']) + '\r\n').encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/calendar')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    path = database(tmp_path / 'calendar.sqlite3', url=f'http://127.0.0.1:{server.server_port}/calendar.ics')
    processor = SyncProcessor(path, timeout=30)
    app = QApplication.instance() or QApplication([])
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(processor.run, 'ics', 'subscription')
            deadline = time.monotonic() + 30
            observed_pid = None
            while not future.done() and time.monotonic() < deadline:
                if processor._process is not None:
                    observed_pid = processor._process.pid
                app.processEvents()
                time.sleep(.002)
            result = future.result(timeout=2)
        assert result['received'] == 1500 and not result['issues']
        assert observed_pid is not None and observed_pid != os.getpid()
        assert len(ticks) > 5
        with Store(path, query_index=False) as store:
            assert store.count_events('subscription') == 1500
            assert not store.pending_changes('subscription')
    finally:
        timer.stop()
        processor.shutdown()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_password_only_travels_on_stdin_and_protocol_errors_do_not_echo_child_output(tmp_path, monkeypatch):
    path = database(tmp_path / 'calendar.sqlite3')
    password = 'synthetic-password-private'
    processes, calls = launch_replacement(monkeypatch,
        'import json,sys; request=json.load(sys.stdin); print(request["password"])')
    processor = SyncProcessor(path)
    try:
        with pytest.raises(ValueError, match='invalid result') as failure:
            processor.run('ics', 'subscription', password)
        assert password not in str(failure.value)
        assert password not in repr(calls[0][0])
        assert 'DEBACAL_VDIR_PASSWORD' not in calls[0][1]['env']
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        processor.shutdown()


def test_worker_error_messages_redact_passwords(tmp_path, monkeypatch):
    path = database(tmp_path / 'calendar.sqlite3')
    launch_replacement(monkeypatch, 'import json,sys; r=json.load(sys.stdin); '
        'print(json.dumps({"ok":False,"error":"Rejected " + r["password"]}))')
    processor = SyncProcessor(path)
    try:
        with pytest.raises(ValueError, match=r'Rejected \[redacted\]'):
            processor.run('ics', 'subscription', 'private-synthetic-password')
    finally:
        processor.shutdown()


def test_oversized_child_stdout_is_stopped_before_unbounded_buffering(tmp_path, monkeypatch):
    path = database(tmp_path / 'calendar.sqlite3')
    processes, _calls = launch_replacement(monkeypatch,
        'import os,time; os.write(1,b"x"*100000); time.sleep(60)')
    monkeypatch.setattr('debacal.sync_worker.MAX_RESULT_BYTES', 1024)
    processor = SyncProcessor(path)
    try:
        with pytest.raises(ValueError, match='too much data'):
            processor.run('ics', 'subscription')
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed
    finally:
        processor.shutdown()


def test_worker_uses_the_running_package_even_with_shadow_cwd_and_pythonpath(tmp_path, monkeypatch):
    shadow = tmp_path / 'shadow'
    package = shadow / 'debacal'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('raise RuntimeError("Wrong checkout was imported")\n')
    database(shadow / 'calendar.sqlite3')
    monkeypatch.chdir(shadow)
    monkeypatch.setenv('PYTHONPATH', str(shadow))
    processor = SyncProcessor('calendar.sqlite3')
    try:
        assert processor.run('history')['available'] == [False, False]
    finally:
        processor.shutdown()


@pytest.mark.parametrize('cancel', [False, True], ids=['timeout', 'close'])
def test_timeout_and_close_reap_child_and_keep_local_changes(tmp_path, monkeypatch, cancel):
    path = database(tmp_path / 'calendar.sqlite3')
    with Store(path) as store:
        local = Event('queued', 'local', 'Unsynced event', START, START + timedelta(hours=1))
        store.apply('Create', [local])
        pending = store.pending_changes()
    processes, _calls = launch_replacement(monkeypatch,
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)')
    processor = SyncProcessor(path, timeout=30 if cancel else .2)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(processor.run, 'ics', 'subscription')
            deadline = time.monotonic() + 3
            while not processes and time.monotonic() < deadline:
                time.sleep(.002)
            assert processes
            if cancel:
                processor.shutdown()
            with pytest.raises(ProcessingCancelled if cancel else ValueError, match='cancelled' if cancel else 'timed out'):
                future.result(timeout=5)
        assert processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
        with Store(path) as reopened:
            assert reopened.get_event('queued') == local
            assert reopened.pending_changes() == pending
    finally:
        processor.shutdown()


def test_shared_priority_flag_defers_child_transactions_and_nested_releases(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    gate = get_write_gate(path)
    first, second = gate.request_foreground(), gate.request_foreground()
    descriptor = gate.descriptor()
    code = ('import sys; from debacal.database_priority import inherit_write_gate; '
            'from debacal.store import Store; '
            'gate=inherit_write_gate(sys.argv[1],int(sys.argv[2])); print("ready",flush=True); '
            'store=Store(sys.argv[1],query_index=False); '
            'store.set_setting("child-written",True); print("done",flush=True); store.close()')
    child = subprocess.Popen([sys.executable, '-c', code, str(path), str(descriptor)],
                             pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        first()
        assert not select.select([child.stdout], [], [], .05)[0]
        with foreground_work(), Store(path, query_index=False) as store:
            store.set_setting('foreground-written', True)
        assert child.poll() is None
        second()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert stdout.strip() == 'done'
        with Store(path, query_index=False) as store:
            assert store.get_setting('foreground-written') and store.get_setting('child-written')
    finally:
        first(); second()
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_processor_releases_anonymous_gate_descriptors_when_closed(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    gc.collect()
    baseline = len(gate_descriptors())
    processor = SyncProcessor(path)
    result = processor.run('conflicts', 'missing-calendar')
    assert result['skipped']
    assert len(gate_descriptors()) > baseline
    processor.shutdown()
    gc.collect()
    assert len(gate_descriptors()) == baseline
    with pytest.raises(ProcessingCancelled):
        processor.run('ics', 'subscription')


def test_child_history_result_has_consistent_availability_and_source_revision(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    with Store(path) as store:
        store.apply('Create', [Event('created', 'local', 'Created', START, START + timedelta(hours=1))])
        expected, revision = store.history_availability(), store.events_revision()
        signature = store._json(store.history_availability_probe()[0])
    processor = SyncProcessor(path)
    try:
        result = processor.run('history')
        assert tuple(result['available']) == expected
        assert result['revision'] == revision
        assert result['signature'] == signature
    finally:
        processor.shutdown()


def test_authenticated_child_refuses_changed_account_before_using_password(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    calendar = Calendar('remote', 'Remote', '#587c67', kind='caldav',
                        url='https://calendar.test/original/', username='original-user')
    with Store(path) as store:
        store.save_calendar(calendar)
    processor = SyncProcessor(path)
    try:
        with pytest.raises(ValueError, match='calendar source changed'):
            processor.run('vdirsyncer', 'remote', 'synthetic-private-password',
                          expected_calendar=replace(calendar, url='https://different.test/', username='different-user'))
    finally:
        processor.shutdown()


def test_parent_refuses_password_if_account_changed_during_metadata_refresh():
    calendar = Calendar('remote', 'Remote', '#587c67', kind='caldav',
                        url='https://calendar.test/', username='original-user')
    engine = object.__new__(SyncEngine)
    engine._password = lambda _calendar: 'old-account-password'
    engine._refresh_metadata = lambda *_args: replace(calendar, username='different-user')
    with pytest.raises(CredentialError, match='account changed'):
        engine._sync_vdir(None, calendar)


def test_parent_death_cancels_worker_waiting_on_shared_foreground_priority(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    marker = tmp_path / 'worker-pid'
    code = ('import sys,time; from pathlib import Path; from threading import Thread; '
            'from debacal.database_priority import get_write_gate; '
            'from debacal.sync_worker import SyncProcessor; '
            'gate=get_write_gate(sys.argv[1]); release=gate.request_foreground(); '
            'processor=SyncProcessor(sys.argv[1]); '
            'thread=Thread(target=lambda:processor.run("history"),daemon=True); thread.start(); '
            '\nwhile processor._process is None: time.sleep(.002)\n'
            'Path(sys.argv[2]).write_text(str(processor._process.pid)); time.sleep(60)')
    launcher = subprocess.Popen([sys.executable, '-c', code, str(path), str(marker)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    worker_pid = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.002)
        assert marker.exists()
        worker_pid = int(marker.read_text())
        launcher.kill()
        launcher.communicate(timeout=3)
        def alive():
            try:
                return Path(f'/proc/{worker_pid}/stat').read_text().split()[2] != 'Z'
            except FileNotFoundError:
                return False
        while alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not alive(), 'A dead desktop must not orphan a priority-blocked calendar worker'
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.communicate()
        if worker_pid is not None:
            try:
                os.killpg(worker_pid, 9)
            except ProcessLookupError:
                pass


def test_manual_worker_cannot_install_group_termination_in_its_callers_shell(monkeypatch):
    monkeypatch.setattr('debacal.sync_worker.os.getpgrp', lambda: -1)
    with patch('debacal.sync_worker.signal.signal', side_effect=AssertionError('Do not modify caller signal handlers')):
        with pytest.raises(ValueError, match='private process group'):
            _bind_parent(os.getppid())


def test_cancelling_an_inflight_child_transaction_rolls_back_uncommitted_writes(tmp_path, monkeypatch):
    path = database(tmp_path / 'calendar.sqlite3')
    marker = tmp_path / 'transaction-open'
    code = ('import json,sys,sqlite3,time; from pathlib import Path; r=json.load(sys.stdin); '
            'connection=sqlite3.connect(r["path"]); connection.execute("BEGIN IMMEDIATE"); '
            'connection.execute("INSERT INTO settings(key,value) VALUES (?,?)",("uncommitted-child","true")); '
            f'Path({str(marker)!r}).touch(); time.sleep(60)')
    launch_replacement(monkeypatch, code)
    processor = SyncProcessor(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(processor.run, 'ics', 'subscription')
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.002)
            assert marker.exists()
            processor.shutdown()
            with pytest.raises(ProcessingCancelled):
                future.result(timeout=5)
        with Store(path, query_index=False) as store:
            assert store.get_setting('uncommitted-child') is None
    finally:
        processor.shutdown()


def test_cancellation_terminates_descendants_holding_the_calendar_lock(tmp_path, monkeypatch):
    path = database(tmp_path / 'calendar.sqlite3')
    lock_path, marker = tmp_path / 'calendar.lock', tmp_path / 'grandchild-ready'
    grandchild = ('import fcntl,time; from pathlib import Path; '
                  f'stream=open({str(lock_path)!r},"a"); fcntl.flock(stream.fileno(),fcntl.LOCK_EX); '
                  f'Path({str(marker)!r}).touch(); time.sleep(60)')
    code = ('import subprocess,sys,time; '
            f'subprocess.Popen([sys.executable,"-c",{grandchild!r}]); time.sleep(60)')
    launch_replacement(monkeypatch, code)
    processor = SyncProcessor(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(processor.run, 'ics', 'subscription')
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.002)
            assert marker.exists()
            processor.shutdown()
            with pytest.raises(ProcessingCancelled):
                future.result(timeout=5)
        with lock_path.open('a') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        processor.shutdown()


def test_conflict_protocol_preserves_full_events_and_issues():
    local = Event('local', 'calendar', 'Local', START, START + timedelta(hours=1))
    remote = Event('remote', 'calendar', 'Remote', START, START + timedelta(hours=2))
    conflicts = (BridgeConflict('conflict', 'local', local, remote, 'Both changed'),)
    result = decode_bridge({'received': 2, 'sent': 1, 'conflicts': encode_conflicts(conflicts),
                            'issues': [{'resource_id': 'resource', 'reason': 'Synthetic issue'}]})
    assert result.conflicts == conflicts and (result.received, result.sent) == (2, 1)
    assert result.issues[0].resource_id == 'resource'


def test_injected_backends_remain_in_process_even_when_processing_is_requested(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    class Backend:
        pass
    with patch('debacal.sync_worker.SyncProcessor', side_effect=AssertionError('Injected backend must remain compatible')):
        engine = SyncEngine(path, vdir_backend=Backend(), background_processing=True)
        engine.close()
        engine._thread.join(timeout=3)
        assert not engine._thread.is_alive()


def test_opt_in_engine_keeps_credentials_on_its_existing_sync_thread(tmp_path):
    path = database(tmp_path / 'calendar.sqlite3')
    with Store(path) as store:
        store.save_calendar(Calendar('remote', 'Remote', '#587c67', kind='caldav',
                                     url='https://calendar.test/', username='synthetic-user'))
    main_thread, credentials_calls, processing_calls = get_ident(), [], []
    class Credentials:
        def get(self, calendar):
            credentials_calls.append(get_ident())
            return 'synthetic-private-password'
        def clear_session(self):
            pass
    class Processor:
        def __init__(self, _path):
            pass
        def run(self, operation, calendar_id, password='', **_kwargs):
            processing_calls.append((operation, calendar_id, password, get_ident()))
            return {'received': 0, 'sent': 0, 'conflicts': [], 'issues': []}
        def shutdown(self):
            pass
    with patch('debacal.sync_worker.SyncProcessor', Processor):
        engine = SyncEngine(path, background_processing=True, credential_store=Credentials(),
                            metadata_discoverer=lambda *_args: [])
        try:
            engine.sync_now()
            while engine.events.get(timeout=5).kind != 'finished':
                pass
            assert credentials_calls and all(identity != main_thread for identity in credentials_calls)
            vdir = next(call for call in processing_calls if call[0] == 'vdirsyncer')
            assert vdir[1:3] == ('remote', 'synthetic-private-password')
            assert vdir[3] == credentials_calls[0]
        finally:
            engine.close()
            engine._thread.join(timeout=3)
            assert not engine._thread.is_alive()
