"""One workspace per database, with local delivery of activation/import requests."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QLockFile, QStandardPaths, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .import_request import ImportRequest
from .import_sources import MAX_IMPORT_BYTES


MAX_REQUEST_BYTES = 64 * 1024
MAX_FILES = 128
MAX_CLIENTS = 16
MAX_PENDING_REQUESTS = 128
MAX_BUFFERED_BYTES = 3 * MAX_IMPORT_BYTES


class InstanceError(RuntimeError):
    """The existing workspace could not be contacted safely."""


def _private_directory(directory: Path) -> Path:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or hasattr(os, 'getuid') and info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise InstanceError('The local workspace runtime directory must be private to your user.')
    return directory


def _runtime_directory() -> Path:
    runtime = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    if runtime:
        directory = Path(runtime) / 'debacal'
        # Filesystem Unix sockets have a small path limit. Leave enough room
        # for a database digest even when XDG_RUNTIME_DIR is unusually long.
        if len(os.fsencode(directory)) < 65:
            try:
                return _private_directory(directory)
            except (OSError, InstanceError):
                pass
    user = str(os.getuid()) if hasattr(os, 'getuid') else hashlib.sha256(
        str(Path.home()).encode()).hexdigest()[:12]
    return _private_directory(Path(tempfile.gettempdir()) / ('debacal-' + user))


def _request_bytes(files, stdin_data=None) -> tuple[str, bytes]:
    # A file may live on a slow/unavailable mount. Do not resolve links or
    # inspect it before the user reviews the import and a worker reads it.
    paths = [str(Path(path).expanduser().absolute()) for path in files]
    if len(paths) > MAX_FILES or any('\0' in path or len(os.fsencode(path)) > 4096 for path in paths):
        raise InstanceError('Open at most 128 calendar files at a time, using ordinary local file paths.')
    request_id = uuid4().hex
    if stdin_data == b'':
        stdin_data = None
    if stdin_data is not None and (not isinstance(stdin_data, bytes) or len(stdin_data) > MAX_IMPORT_BYTES):
        raise InstanceError('Choose standard input smaller than 20 MB.')
    value = {'version': 1, 'request': request_id, 'files': paths}
    if stdin_data is not None:
        value.update(version=2, stdin_size=len(stdin_data))
    header = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    if len(header) > MAX_REQUEST_BYTES:
        raise InstanceError('The calendar file request is too large. Open fewer files at a time.')
    return request_id, header + (stdin_data or b'')


def _request_size(request):
    return len(request.stdin_data) if isinstance(request, ImportRequest) else 0


class InstanceCoordinator(QObject):
    """Claim or contact the canonical database's workspace before opening it.

    ``start(files, stdin_data=None)`` returns True to create a window or False after the running
    instance acknowledges delivery. Connect ``request_received`` and then call
    ``enable_requests`` once the window is ready. A list of absolute paths asks
    for an import; an empty list asks to show/activate the existing window.
    With standard input, the signal carries an ``ImportRequest`` instead.
    """
    request_received = Signal(object)

    def __init__(self, database_path, parent=None, *, runtime_dir=None):
        super().__init__(parent)
        identity = os.path.normcase(str(Path(database_path).expanduser().resolve()))
        digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:24]
        try:
            directory = _private_directory(Path(runtime_dir)) if runtime_dir is not None else _runtime_directory()
        except OSError as exc:
            raise InstanceError('Could not create the local workspace runtime directory.') from exc
        self.server_name = str(directory / ('d-' + digest))
        self.lock_path = str(directory / ('d-' + digest + '.lock'))
        self._lock = QLockFile(self.lock_path)
        # Never steal a lock just because a productive calendar stays open for
        # days. QLockFile still detects a dead owner after a crash.
        self._lock.setStaleLockTime(0)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept_connections)
        self._clients = {}
        self._buffered_bytes = 0
        self._queued_bytes = 0
        self._requests = deque()
        self._seen = set()
        self._seen_order = deque()
        self._enabled = False
        self._draining = False
        self._owned = False
        self._started = False
        # Optional application queue budget, combined with the requests that
        # have been accepted here but have not reached that queue yet.
        self.admission_check = None

    def start(self, files=(), *, stdin_data=None, timeout_ms=3000) -> bool:
        if self._started:
            raise InstanceError('The workspace instance coordinator has already started.')
        if stdin_data == b'':
            stdin_data = None
        request_id, payload = _request_bytes(files, stdin_data)
        self._started = True
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            if self._lock.tryLock(0):
                self._owned = True
                # Only the owner may clear a stale socket. Concurrent launches
                # cannot unlink a live primary's server during its startup.
                QLocalServer.removeServer(self.server_name)
                if not self._server.listen(self.server_name):
                    self.close()
                    raise InstanceError('Could not listen for calendar file requests: ' + self._server.errorString())
                initial_files = json.loads(payload[:payload.index(b'\n')])['files']
                initial = ImportRequest(tuple(initial_files), stdin_data) if stdin_data is not None else initial_files
                if initial_files or stdin_data is not None:
                    self._requests.append(initial)
                    self._queued_bytes += _request_size(initial)
                return True
            if self._lock.error() not in (QLockFile.LockError.NoError, QLockFile.LockError.LockFailedError):
                raise InstanceError('Could not lock the local workspace runtime directory. Check its write permissions.')
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise InstanceError('Debacal is already starting or is not responding. Please try again shortly.')
            if self._forward(request_id, payload, remaining if stdin_data is not None else min(remaining, 250)):
                return False
            # Retry the lock as well: a previous primary can exit between its
            # lock being observed and this connection attempt.
            time.sleep(min(.02, max(0, deadline - time.monotonic())))

    def _forward(self, request_id, payload, timeout_ms):
        socket = QLocalSocket()
        try:
            deadline = time.monotonic() + timeout_ms / 1000
            socket.connectToServer(self.server_name)
            if not socket.waitForConnected(timeout_ms):
                return False
            if socket.write(payload) != len(payload):
                return False
            while socket.bytesToWrite():
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0 or not socket.waitForBytesWritten(remaining):
                    return False
            response = bytearray()
            while len(response) <= 1024:
                response.extend(bytes(socket.readAll()))
                if b'\n' in response:
                    try:
                        value = json.loads(bytes(response).split(b'\n', 1)[0])
                    except (ValueError, UnicodeError):
                        return False
                    if (isinstance(value, dict) and value.get('request') == request_id and value.get('ok') is False):
                        raise InstanceError(value.get('error') or 'The running Debacal instance could not accept this import.')
                    return value == {'ok': True, 'request': request_id}
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0 or not socket.waitForReadyRead(remaining):
                    return False
            return False
        finally:
            socket.abort()

    def enable_requests(self):
        """Deliver initial/early requests after the application has a window."""
        self._enabled = True
        QTimer.singleShot(0, self._drain_requests)

    def _drain_requests(self):
        if not self._enabled or self._draining:
            return
        self._draining = True
        try:
            while self._enabled and self._requests:
                request = self._requests.popleft()
                self._queued_bytes -= _request_size(request)
                self.request_received.emit(request)
        finally:
            self._draining = False

    def _accept_connections(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if len(self._clients) >= MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            timer.start(10000)
            socket.setReadBufferSize(256 * 1024)
            self._clients[socket] = {'buffer': bytearray(), 'header': None, 'timer': timer}
            socket.readyRead.connect(lambda source=socket: self._read_request(source))
            socket.disconnected.connect(lambda source=socket: self._drop_client(source))
            self._read_request(socket)

    def _drop_client(self, socket):
        state = self._clients.pop(socket, None)
        if state is not None:
            self._buffered_bytes -= len(state['buffer'])
        socket.deleteLater()

    def _read_request(self, socket):
        state = self._clients.get(socket)
        if state is None:
            return
        buffer = state['buffer']
        incoming = bytes(socket.readAll())
        if self._buffered_bytes + self._queued_bytes + len(incoming) > MAX_BUFFERED_BYTES:
            socket.abort()
            return
        buffer.extend(incoming)
        self._buffered_bytes += len(incoming)
        state['timer'].start(10000)
        if state['header'] is None:
            boundary = buffer.find(b'\n')
            if boundary == -1:
                if len(buffer) >= MAX_REQUEST_BYTES:
                    socket.abort()
                return
            if boundary + 1 > MAX_REQUEST_BYTES:
                socket.abort()
                return
            try:
                value = json.loads(bytes(memoryview(buffer)[:boundary]))
                if not isinstance(value, dict) or type(value.get('version')) is not int:
                    raise ValueError
                version = value['version']
                fields = {'version', 'request', 'files'} | ({'stdin_size'} if version == 2 else set())
                if (version not in (1, 2) or set(value) != fields
                        or not isinstance(value['request'], str) or len(value['request']) != 32
                        or any(char not in '0123456789abcdef' for char in value['request'])
                        or not isinstance(value['files'], list) or len(value['files']) > MAX_FILES
                        or any(not isinstance(path, str) or not Path(path).is_absolute() or '\0' in path
                               or len(os.fsencode(path)) > 4096 for path in value['files'])
                        or version == 2 and (type(value['stdin_size']) is not int
                                             or not 1 <= value['stdin_size'] <= MAX_IMPORT_BYTES)):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, RecursionError):
                socket.abort()
                return
            state['header'] = value
            state['body_start'] = boundary + 1
            state['expected'] = boundary + 1 + value.get('stdin_size', 0)
        value = state['header']
        if len(buffer) > state['expected']:
            socket.abort()
            return
        if len(buffer) < state['expected']:
            return
        request_id = value['request']
        if request_id not in self._seen:
            incoming_size = value.get('stdin_size', 0)
            importing = bool(value['files']) or value['version'] == 2
            if (len(self._requests) >= MAX_PENDING_REQUESTS
                    or importing and self.admission_check is not None and not self.admission_check(
                        len(self._requests) + 1, self._queued_bytes + incoming_size)):
                self._respond(socket, request_id, error='Debacal has too many pending imports. Finish or cancel an import, then try again.')
                return
            request = (ImportRequest(tuple(value['files']), bytes(memoryview(buffer)[state['body_start']:]))
                       if value['version'] == 2 else value['files'])
            self._requests.append(request)
            self._queued_bytes += _request_size(request)
            self._seen.add(request_id)
            self._seen_order.append(request_id)
            if len(self._seen_order) > 1024:
                self._seen.discard(self._seen_order.popleft())
        self._respond(socket, request_id)
        if self._enabled:
            QTimer.singleShot(0, self._drain_requests)

    def _respond(self, socket, request_id, *, error=None):
        state = self._clients.pop(socket, None)
        if state is not None:
            self._buffered_bytes -= len(state['buffer'])
            state['buffer'].clear()
            state['timer'].stop()
        response = {'ok': error is None, 'request': request_id}
        if error is not None:
            response['error'] = error
        socket.write(json.dumps(response, separators=(',', ':')).encode() + b'\n')
        socket.flush()
        socket.disconnectFromServer()

    def close(self):
        self._enabled = False
        for socket in list(self._clients):
            socket.abort()
        self._clients.clear()
        self._buffered_bytes = 0
        self._requests.clear()
        self._queued_bytes = 0
        if self._owned:
            self._server.close()
            QLocalServer.removeServer(self.server_name)
            self._lock.unlock()
            self._owned = False
