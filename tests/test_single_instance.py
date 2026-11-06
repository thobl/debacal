"""Real local-process forwarding, crash recovery, and bounded IPC requests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from contextlib import contextmanager
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLockFile
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from debacal.single_instance import InstanceCoordinator, InstanceError, MAX_REQUEST_BYTES, _request_bytes
from debacal.import_sources import MAX_IMPORT_BYTES


def test_file_handoff_does_not_inspect_paths_before_import_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('File paths must be inspected by the import worker only.')
    for operation in ('resolve', 'stat', 'read_bytes'):
        monkeypatch.setattr(Path, operation, forbidden)
    _request, payload = _request_bytes(['not-yet-readable.ics'])
    assert json.loads(payload)['files'] == [str(tmp_path / 'not-yet-readable.ics')]


PRIMARY = r'''
import base64
import json
from pathlib import Path
import sys
import time
from PySide6.QtCore import QCoreApplication, QTimer
from debacal.single_instance import InstanceCoordinator
from debacal.import_request import ImportRequest
import debacal.single_instance as ipc
app = QCoreApplication([])
database, runtime, ready, events, delay, start_at, initial, initial_stdin, byte_budget, admission_limit = sys.argv[1:]
if int(byte_budget):
    ipc.MAX_BUFFERED_BYTES = int(byte_budget)
while time.monotonic() < float(start_at):
    time.sleep(.005)
coordinator = InstanceCoordinator(database, runtime_dir=runtime)
if int(admission_limit):
    coordinator.admission_check = lambda _requests, size: size <= int(admission_limit)
primary = coordinator.start(json.loads(initial), stdin_data=base64.b64decode(initial_stdin) if initial_stdin else None)
Path(ready).write_text(json.dumps({'primary': primary, 'socket': coordinator.server_name}))
if not primary:
    raise SystemExit(0)
def received(files):
    if isinstance(files, ImportRequest):
        files = {'files': list(files.files), 'stdin': base64.b64encode(files.stdin_data).decode('ascii')}
    with Path(events).open('a') as output:
        output.write(json.dumps(files) + '\n')
coordinator.request_received.connect(received)
QTimer.singleShot(int(delay), coordinator.enable_requests)
QTimer.singleShot(15000, app.quit)
app.exec()
coordinator.close()
'''


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Timed out waiting for local workspace process.')


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


@pytest.fixture
def workspace():
    # Short paths are intentional: Unix socket names have a small byte limit.
    with tempfile.TemporaryDirectory(prefix='debacal-ipc-') as directory:
        root = Path(directory)
        yield root, root / 'calendar.sqlite3', root / 'runtime'


@contextmanager
def primary(workspace, *, delay=0, start_at=0, initial=(), initial_stdin=None, byte_budget=0, admission_limit=0, name='one'):
    root, database, runtime = workspace
    ready, events = root / (name + '.ready'), root / (name + '.events')
    process = subprocess.Popen([sys.executable, '-c', PRIMARY, str(database), str(runtime),
                                str(ready), str(events), str(delay), str(start_at), json.dumps(list(initial)),
                                base64.b64encode(initial_stdin).decode('ascii') if initial_stdin else '',
                                str(byte_budget), str(admission_limit)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        def started():
            if process.poll() is not None and not ready.exists():
                raise AssertionError(process.communicate()[1])
            return ready.exists()
        wait_until(started)
        yield process, json.loads(ready.read_text()), events
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')]


def raw_request(server, payload):
    socket = QLocalSocket()
    try:
        socket.connectToServer(server)
        assert socket.waitForConnected(1000)
        socket.write(payload)
        while socket.bytesToWrite():
            if not socket.waitForBytesWritten(1000):
                break
        socket.waitForReadyRead(1000)
        return bytes(socket.readAll())
    finally:
        socket.abort()


def test_stdin_requests_are_bounded_and_keep_raw_bytes_outside_the_json_header():
    body = b'BEGIN:VCALENDAR\r\nDESCRIPTION:Line one\nLine two\0\xff\r\nEND:VCALENDAR\r\n'
    request, payload = _request_bytes([], stdin_data=body)
    header, forwarded = payload.split(b'\n', 1)
    assert json.loads(header) == {'version': 2, 'request': request, 'files': [], 'stdin_size': len(body)}
    assert forwarded == body
    assert json.loads(_request_bytes([], stdin_data=b'')[1])['version'] == 1
    with pytest.raises(InstanceError, match='20 MB'):
        _request_bytes([], stdin_data=b'x' * (MAX_IMPORT_BYTES + 1))
    with pytest.raises(InstanceError, match='20 MB'):
        _request_bytes([], stdin_data='not byte data')


def test_initial_and_forwarded_stdin_preserve_contents_beyond_the_file_request_limit(app, workspace):
    root, database, runtime = workspace
    initial = 'BEGIN:VCALENDAR\r\nDESCRIPTION:Grüße\r\nEND:VCALENDAR\r\n'.encode()
    body = initial + b'DESCRIPTION:' + b'\r\nline\0\xff' * 30000
    assert len(body) > MAX_REQUEST_BYTES
    with primary(workspace, delay=500, initial_stdin=initial) as (_process, _info, events):
        secondary = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert secondary.start([root / 'other.ics'], stdin_data=body) is False
            assert read_events(events) == []
            wait_until(lambda: len(read_events(events)) == 2)
            first, second = read_events(events)
            assert first['files'] == [] and base64.b64decode(first['stdin']) == initial
            assert second['files'] == [str(root / 'other.ics')]
            assert base64.b64decode(second['stdin']) == body
            assert not database.exists() and not list(root.rglob('*.ics'))
        finally:
            secondary.close()


@pytest.mark.parametrize('size,body', [
    (-1, b''), (0, b''), (True, b''), (MAX_IMPORT_BYTES + 1, b''), (1, b'too long'), (100, b'truncated'),
])
def test_invalid_or_truncated_stdin_frames_do_not_disrupt_later_imports(app, workspace, size, body):
    _root, database, runtime = workspace
    header = {'version': 2, 'request': 'c' * 32, 'files': [], 'stdin_size': size}
    with primary(workspace) as (_process, info, events):
        assert raw_request(info['socket'], json.dumps(header).encode() + b'\n' + body) == b''
        secondary = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert secondary.start(stdin_data=b'valid subsequent input') is False
            wait_until(lambda: read_events(events))
            assert len(read_events(events)) == 1
            assert base64.b64decode(read_events(events)[0]['stdin']) == b'valid subsequent input'
        finally:
            secondary.close()


def test_acknowledged_stdin_retries_do_not_duplicate_import_or_keep_an_incoming_buffer(app, workspace):
    _root, _database, _runtime = workspace
    request, payload = _request_bytes([], stdin_data=b'calendar\r\n' * 30000)
    with primary(workspace) as (_process, info, events):
        for _ in range(2):
            assert json.loads(raw_request(info['socket'], payload)) == {'ok': True, 'request': request}
        wait_until(lambda: read_events(events))
        assert len(read_events(events)) == 1
        assert base64.b64decode(read_events(events)[0]['stdin']) == b'calendar\r\n' * 30000


def test_pending_stdin_content_has_an_aggregate_byte_budget_until_delivery(app, workspace):
    _root, database, runtime = workspace
    body = b'calendar data\r\n' * 10000
    with primary(workspace, delay=2000, byte_budget=len(body) * 2 + 1000) as (_process, _info, events):
        for _ in range(2):
            secondary = InstanceCoordinator(database, runtime_dir=runtime)
            assert secondary.start(stdin_data=body) is False
            secondary.close()
        overloaded = InstanceCoordinator(database, runtime_dir=runtime)
        with pytest.raises(InstanceError, match='not responding'):
            overloaded.start(stdin_data=body, timeout_ms=100)
        overloaded.close()
        assert not read_events(events)
        wait_until(lambda: len(read_events(events)) == 2)
        later = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert later.start(stdin_data=body) is False
            wait_until(lambda: len(read_events(events)) == 3)
        finally:
            later.close()


def test_full_application_import_queue_returns_a_negative_ack_instead_of_losing_input(app, workspace):
    _root, database, runtime = workspace
    with primary(workspace, admission_limit=32) as (_process, _info, events):
        rejected = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            with pytest.raises(InstanceError, match='Finish or cancel'):
                rejected.start(stdin_data=b'x' * 33)
            assert not read_events(events)
        finally:
            rejected.close()
        accepted = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert accepted.start(stdin_data=b'smaller input') is False
            wait_until(lambda: read_events(events))
            assert base64.b64decode(read_events(events)[0]['stdin']) == b'smaller input'
        finally:
            accepted.close()


def test_second_launch_forwards_files_and_activation_without_opening_database(app, workspace):
    root, database, runtime = workspace
    calendar = root / 'a calendar.ics'
    calendar.write_text('synthetic, read only by the import workflow')
    with primary(workspace) as (_process, info, events):
        assert info['primary']
        secondary = InstanceCoordinator(database, runtime_dir=runtime)
        assert secondary.start([calendar]) is False
        secondary.close()
        activation = InstanceCoordinator(database, runtime_dir=runtime)
        assert activation.start() is False
        activation.close()
        wait_until(lambda: len(read_events(events)) == 2)
        assert read_events(events) == [[str(calendar.resolve())], []]
        assert not database.exists()
        assert calendar.read_text() == 'synthetic, read only by the import workflow'


def test_initial_and_early_requests_wait_until_window_is_ready(app, workspace):
    root, database, runtime = workspace
    first, second = root / 'initial.ics', root / 'early.ics'
    with primary(workspace, delay=500, initial=[str(first)]) as (_process, _info, events):
        secondary = InstanceCoordinator(database, runtime_dir=runtime)
        assert secondary.start([second]) is False
        assert not read_events(events)
        wait_until(lambda: len(read_events(events)) == 2)
        assert read_events(events) == [[str(first)], [str(second)]]
        secondary.close()


def test_canonical_database_identity_survives_creation_and_directory_symlinks(app, workspace):
    root, database, runtime = workspace
    with primary(workspace) as (_process, info, events):
        database.touch()  # The primary claims before Store creates its database.
        alias = root / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        secondary = InstanceCoordinator(alias / database.name, runtime_dir=runtime)
        assert secondary.server_name == info['socket']
        assert secondary.start() is False
        wait_until(lambda: read_events(events))
        assert read_events(events) == [[]]
        secondary.close()


def test_different_database_profiles_can_run_independently_and_close_releases_lock(app, workspace):
    root, database, runtime = workspace
    first = InstanceCoordinator(database, runtime_dir=runtime)
    second = InstanceCoordinator(root / 'testing.sqlite3', runtime_dir=runtime)
    try:
        assert first.start() is True
        assert second.start() is True
        assert first.server_name != second.server_name
        first.close()
        replacement = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert replacement.start() is True
        finally:
            replacement.close()
    finally:
        first.close()
        second.close()


def test_dead_primary_lock_and_socket_are_recovered_without_manual_cleanup(app, workspace):
    _root, database, runtime = workspace
    with primary(workspace) as (process, info, _events):
        process.kill()
        process.wait(timeout=5)
        assert Path(info['socket']).exists()
        assert list(runtime.glob('*.lock'))
        replacement = InstanceCoordinator(database, runtime_dir=runtime)
        try:
            assert replacement.start() is True
        finally:
            replacement.close()


def test_live_startup_lock_is_not_stolen_and_failure_does_not_remove_its_files(app, workspace):
    _root, database, runtime = workspace
    coordinator = InstanceCoordinator(database, runtime_dir=runtime)
    lock = QLockFile(coordinator.lock_path)
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    Path(coordinator.server_name).write_bytes(b'Existing owner has not started its server yet.')
    try:
        with pytest.raises(InstanceError, match='already starting'):
            coordinator.start(timeout_ms=100)
        coordinator.close()
        assert Path(coordinator.lock_path).exists()
        assert Path(coordinator.server_name).read_bytes().startswith(b'Existing owner')
    finally:
        lock.unlock()


@pytest.mark.parametrize('payload', [
    b'not json\n',
    b'{' + b'x' * MAX_REQUEST_BYTES,
    json.dumps({'version': 1, 'request': 'a' * 32, 'files': ['relative.ics']}).encode() + b'\n',
    json.dumps({'version': True, 'request': 'a' * 32, 'files': []}).encode() + b'\n',
])
def test_bad_requests_are_rejected_without_disrupting_the_live_workspace(app, workspace, payload):
    root, database, runtime = workspace
    with primary(workspace) as (_process, info, events):
        assert raw_request(info['socket'], payload) == b''
        secondary = InstanceCoordinator(database, runtime_dir=runtime)
        assert secondary.start([root / 'valid.ics']) is False
        wait_until(lambda: read_events(events))
        assert read_events(events) == [[str(root / 'valid.ics')]]
        secondary.close()


def test_acknowledged_retries_do_not_queue_the_same_import_twice(app, workspace):
    root, _database, _runtime = workspace
    with primary(workspace) as (_process, info, events):
        payload = json.dumps({'version': 1, 'request': 'b' * 32, 'files': [str(root / 'file.ics')]}).encode() + b'\n'
        for _ in range(2):
            assert json.loads(raw_request(info['socket'], payload)) == {'ok': True, 'request': 'b' * 32}
        wait_until(lambda: read_events(events))
        assert read_events(events) == [[str(root / 'file.ics')]]


def test_pending_handlers_do_not_reenter_each_other_via_a_modal_event_loop(app, workspace):
    root, database, runtime = workspace
    coordinator = InstanceCoordinator(database, runtime_dir=runtime)
    assert coordinator.start([root / 'first.ics'])
    calls, inside = [], False
    def receive(files):
        nonlocal inside
        assert not inside
        inside = True
        calls.append(files)
        if len(calls) == 1:
            coordinator._requests.append([str(root / 'second.ics')])
            coordinator._drain_requests()
        inside = False
    coordinator.request_received.connect(receive)
    try:
        coordinator.enable_requests()
        app.processEvents()
        assert calls == [[str(root / 'first.ics')], [str(root / 'second.ics')]]
    finally:
        coordinator.close()


def test_runtime_directory_must_be_private_and_request_count_is_bounded(app, workspace):
    root, database, runtime = workspace
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    with pytest.raises(InstanceError, match='private'):
        InstanceCoordinator(database, runtime_dir=runtime)
    runtime.chmod(0o700)
    coordinator = InstanceCoordinator(database, runtime_dir=runtime)
    with pytest.raises(InstanceError, match='128'):
        coordinator.start([root / ('file' + str(index)) for index in range(129)])
