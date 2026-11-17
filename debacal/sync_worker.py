"""Private, disposable calendar processing outside the desktop interpreter.

The worker receives credentials only over its anonymous stdin pipe. Its output
is a bounded JSON result, never a log; unexpected child output is not surfaced.
SQLite and the bridge's existing per-calendar lock retain recovery ownership.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
from threading import Lock
from time import monotonic

from .database_priority import get_write_gate, inherit_write_gate


MAX_RESULT_BYTES = 128 * 1024 * 1024
_BOOTSTRAP = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
              'from debacal.sync_worker import main; main()')


class ProcessingCancelled(RuntimeError):
    pass


def _terminate(process):
    # Its maintained vdirsyncer children share this private process group,
    # including inherited bridge lock descriptors. Cancel the whole job.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class SyncProcessor:
    def __init__(self, path, *, timeout=600):
        self.path = str(path)
        self.timeout = timeout
        self._gate = get_write_gate(path)
        self._lock = Lock()
        self._process = None
        self._closed = False

    def shutdown(self):
        """Cancel immediately; the existing sync thread reaps the child."""
        with self._lock:
            self._closed = True
            if self._process is not None:
                _terminate(self._process)
            else:
                self._gate = None

    def _exchange(self, process, request, deadline):
        """Pump private pipes without accumulating an unbounded child output."""
        output, written, ended = bytearray(), 0, False
        with selectors.DefaultSelector() as selector:
            for pipe, kind in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, kind)
            while True:
                with self._lock:
                    closed = self._closed
                if closed:
                    raise ProcessingCancelled('Calendar processing was cancelled. Local changes remain queued.')
                if monotonic() >= deadline:
                    raise ValueError('Calendar processing timed out. Local changes remain queued; retry synchronization.')
                for key, _mask in selector.select(.05):
                    pipe = key.fileobj
                    if pipe is process.stdin:
                        try:
                            written += os.write(pipe.fileno(), memoryview(request)[written:written + 65536])
                        except BrokenPipeError:
                            written = len(request)
                        if written == len(request):
                            selector.unregister(pipe)
                            pipe.close()
                    else:
                        data = os.read(pipe.fileno(), 65536)
                        if not data:
                            selector.unregister(pipe)
                            pipe.close()
                            ended = True
                        elif len(output) + len(data) > MAX_RESULT_BYTES:
                            raise ValueError('Calendar processing returned too much data. Local changes remain queued.')
                        else:
                            output.extend(data)
                if ended and process.poll() is not None:
                    return output

    def run(self, operation, calendar_id='', password='', *, expected_calendar=None):
        if operation == 'vdirsyncer' and expected_calendar is None:
            raise ValueError('The calendar account must be identified before synchronization.')
        request = json.dumps({'operation': operation, 'path': self.path,
                              'calendar_id': calendar_id, 'password': password,
                              'expected_source': ({name: getattr(expected_calendar, name)
                                                   for name in ('kind', 'url', 'username')}
                                                  if expected_calendar is not None else None)}).encode()
        environment = os.environ.copy()
        environment.pop('DEBACAL_VDIR_PASSWORD', None)
        with self._lock:
            if self._closed:
                raise ProcessingCancelled('Calendar processing was cancelled. Local changes remain queued.')
            if self._process is not None:
                raise RuntimeError('A calendar processing job is already running.')
            descriptor = self._gate.descriptor()
            command = [sys.executable, '-c', _BOOTSTRAP, str(Path(__file__).resolve().parents[1]),
                       '--priority-fd', str(descriptor), '--parent-pid', str(os.getpid())]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, pass_fds=(descriptor,),
                                       start_new_session=True, env=environment)
            self._process = process
        try:
            output = self._exchange(process, request, monotonic() + self.timeout)
            with self._lock:
                if self._closed:
                    raise ProcessingCancelled('Calendar processing was cancelled. Local changes remain queued.')
            if process.returncode or len(output) > MAX_RESULT_BYTES:
                raise ValueError('Calendar processing stopped unexpectedly. Local changes remain queued; retry synchronization.')
            try:
                result = json.loads(output)
                if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                    raise ValueError('Invalid result')
                if not result['ok']:
                    reason = result.get('error')
                    if not isinstance(reason, str):
                        raise ValueError('Invalid error')
                    raise WorkerError(reason)
                value = result['result']
                if not valid_result(operation, value):
                    raise ValueError('Invalid result')
                return value
            except WorkerError as error:
                reason = str(error).replace(password, '[redacted]') if password else str(error)
                raise ValueError(reason) from None
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError('Calendar processing returned an invalid result. Local changes remain queued.') from None
        finally:
            if process.poll() is None:
                _terminate(process)
            for pipe in (process.stdin, process.stdout):
                if not pipe.closed:
                    pipe.close()
            process.wait()
            with self._lock:
                self._process = None
                if self._closed:
                    self._gate = None


class WorkerError(Exception):
    pass


def valid_result(operation, value):
    if not isinstance(value, dict):
        return False
    if operation == 'history':
        available = value.get('available')
        return (isinstance(available, list) and len(available) == 2
                and all(type(item) is bool for item in available)
                and type(value.get('revision')) is int and value['revision'] >= 0
                and isinstance(value.get('signature'), str))
    if operation in {'ics', 'vdirsyncer'} and not (
            type(value.get('received')) is int and value['received'] >= 0):
        return False
    if operation == 'vdirsyncer' and not (type(value.get('sent')) is int and value['sent'] >= 0):
        return False
    issues = value.get('issues', [])
    if not isinstance(issues, list) or not all(
            isinstance(item, dict) and isinstance(item.get('resource_id'), str)
            and isinstance(item.get('reason'), str) for item in issues):
        return False
    conflicts = value.get('conflicts', [])
    return isinstance(conflicts, list) and all(
        isinstance(item, dict) and all(isinstance(item.get(name), str) for name in ('id', 'event_id', 'reason'))
        and all(item.get(name) is None or isinstance(item[name], dict) for name in ('local', 'remote'))
        for item in conflicts)


def encode_conflicts(conflicts):
    return [{'id': item.id, 'event_id': item.event_id,
             'local': item.local.to_dict() if item.local is not None else None,
             'remote': item.remote.to_dict() if item.remote is not None else None,
             'reason': item.reason} for item in conflicts]


def decode_bridge(result):
    from .ics_resources import ResourceIssue
    from .models import Event
    from .vdirsyncer_backend import BridgeConflict, BridgeResult
    conflicts = tuple(BridgeConflict(item['id'], item['event_id'],
                      Event.from_dict(item['local']) if item['local'] is not None else None,
                      Event.from_dict(item['remote']) if item['remote'] is not None else None,
                      item['reason']) for item in result.get('conflicts', ()))
    issues = tuple(ResourceIssue(item['resource_id'], item['reason']) for item in result.get('issues', ()))
    return BridgeResult(result.get('received', 0), result.get('sent', 0), conflicts, issues)


def execute(request):
    from .store import Store
    from .vdirsyncer_backend import VdirsyncerBackend
    operation = request.get('operation')
    if operation not in {'vdirsyncer', 'ics', 'conflicts', 'history'}:
        raise ValueError('Unsupported calendar processing operation.')
    with Store(request['path'], query_index=False) as store:
        if operation == 'history':
            # Read the availability and its source revision from one SQLite
            # snapshot. The UI can discard a response overtaken by sync.
            store.connection.execute('BEGIN')
            try:
                revision = store.events_revision()
                signature = store._json(store.history_availability_probe()[0])
                available = store.history_availability()
                return {'available': list(available), 'revision': revision, 'signature': signature}
            finally:
                store.connection.rollback()
        calendar = store.get_calendar(request['calendar_id'])
        if calendar is None or not calendar.enabled:
            return {'received': 0, 'sent': 0, 'conflicts': [], 'issues': [], 'skipped': True}
        expected = request.get('expected_source')
        if operation == 'vdirsyncer' and expected is None:
            raise ValueError('The calendar account must be identified before synchronization.')
        if expected is not None and expected != {name: getattr(calendar, name) for name in ('kind', 'url', 'username')}:
            raise ValueError('The calendar source changed during synchronization. Retry using its current account.')
        if operation == 'ics':
            if calendar.kind != 'ics':
                raise ValueError('The calendar source changed. Retry synchronization.')
            from .sync import refresh_ics_subscription
            report = refresh_ics_subscription(store, calendar)
            return {'received': len(report.events),
                    'issues': [{'resource_id': item.resource_id, 'reason': item.reason} for item in report.issues]}
        if calendar.kind != 'caldav':
            raise ValueError('The calendar source changed. Retry synchronization.')
        backend = VdirsyncerBackend(request['path'])
        if operation == 'conflicts':
            return {'conflicts': encode_conflicts(backend.saved_conflicts(calendar.id))}
        result = backend.sync(store, calendar, request.get('password', ''))
        return {'received': result.received, 'sent': result.sent,
                'conflicts': encode_conflicts(result.conflicts),
                'issues': [{'resource_id': item.resource_id, 'reason': item.reason} for item in result.issues]}


def _bind_parent(parent_pid):
    """A desktop crash must not leave a detached job holding its calendar lock."""
    if os.getpgrp() != os.getpid():
        raise ValueError('The calendar worker requires its own private process group.')
    import ctypes
    def parent_closed(_signal, _frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, parent_closed)
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # Linux PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'Could not bind the calendar worker lifetime.')
    if os.getppid() != parent_pid:
        parent_closed(None, None)


def main():
    parser = argparse.ArgumentParser(description='Private Debacal calendar worker')
    parser.add_argument('--priority-fd', required=True, type=int)
    parser.add_argument('--parent-pid', required=True, type=int)
    arguments = parser.parse_args()
    password = ''
    priority = None
    try:
        _bind_parent(arguments.parent_pid)
        data = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('Calendar processing request is too large.')
        request = json.loads(data)
        if not isinstance(request, dict) or not isinstance(request.get('password', ''), str):
            raise ValueError('Invalid calendar processing request.')
        password = request.get('password', '')
        priority = inherit_write_gate(request['path'], arguments.priority_fd)
        result = {'ok': True, 'result': execute(request)}
    except Exception as error:
        reason = str(error)
        if password:
            reason = reason.replace(password, '[redacted]')
        result = {'ok': False, 'error': reason[:4000] or 'Calendar processing failed. Local changes remain queued.'}
    encoded = json.dumps(result).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"ok":false,"error":"Calendar processing returned too much data. Local changes remain queued."}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
