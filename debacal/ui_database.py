"""Nonblocking GUI mutation boundary with a separate, ordered writer.

The GUI connection never waits for a database writer. A free transaction keeps
the immediate workflow; contention moves the captured operation to a dedicated
connection. Callbacks run on the Qt thread, and queued user writes get a turn
between sync transactions. No nested Qt event loop or processEvents retry is
used while a transaction is pending.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from shiboken6 import isValid

from .database_priority import foreground_work, get_write_gate
from .store import Store


def database_busy(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, 'sqlite_errorcode', None)
    return (code is not None and code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            or code is None and ('database is locked' in str(error).lower()
                                 or 'database table is locked' in str(error).lower()))


class DatabaseCommands(QObject):
    completed = Signal(object)
    pending_changed = Signal(int)

    def __init__(self, store, parent=None, *, on_change=None, on_error=None):
        super().__init__(parent)
        self.store = store
        self.on_change = on_change or (lambda: None)
        self.on_error = on_error or (lambda error: None)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-ui-writes')
        self._writer = None
        self._jobs = {}
        self._next_id = 0
        self._close_callback = None
        self._shutdown = False
        self._maintenance = False
        self._maintenance_again = False
        self._maintenance_ids = set()
        self._maintenance_all = False
        self._background_keys = set()
        self._gate = getattr(store, '_write_gate', None) or get_write_gate(store.path)
        self.completed.connect(self._finished)
        configure = getattr(store, 'configure_ui_access', None)
        if configure is not None:
            configure(self._request_maintenance, self._request_background_write)
        else:
            store.connection.execute('PRAGMA busy_timeout=0')

    @property
    def pending(self):
        return len(self._jobs)

    def write(self, operation, on_success=None, on_error=None, *, force_async=False):
        """Run now or enqueue; return True when the callback already ran."""
        if self._shutdown:
            raise RuntimeError('The database command queue is closed.')
        if not self.pending and not force_async:
            try:
                # Acquire the writer before invoking the captured operation.
                # A batch must either commit once or be queued in its entirety;
                # retrying a partially committed callable could duplicate work.
                with foreground_work(), self.store._transaction():
                    value = operation(self.store)
            except Exception as error:
                if not database_busy(error):
                    self._failed(error, on_error)
                    return True
            else:
                self._succeeded(value, on_success)
                return True
        self._next_id += 1
        identity = self._next_id
        self._jobs[identity] = on_success, on_error
        release = self._gate.request_foreground()
        self.pending_changed.emit(self.pending)
        future = self.executor.submit(self._execute, operation, release)
        future.add_done_callback(lambda result, key=identity: self._publish(key, result))
        return False

    def _execute(self, operation, release):
        try:
            with foreground_work():
                if self._writer is None:
                    if self.store.path == ':memory:':
                        raise RuntimeError('A contended asynchronous write needs a file-backed workspace.')
                    self._writer = Store(self.store.path, query_index=False)
                with self._writer._transaction():
                    return operation(self._writer)
        finally:
            release()

    def _publish(self, identity, future):
        try:
            value, error = future.result(), None
        except Exception as failure:
            value, error = None, failure
        if isValid(self):
            try:
                self.completed.emit((identity, value, error))
            except RuntimeError:
                # The owner can be destroyed between isValid and emit. The
                # accepted write has already committed and released its gate.
                if isValid(self):
                    raise

    @Slot(object)
    def _finished(self, result):
        identity, value, error = result
        callbacks = self._jobs.pop(identity, None)
        if callbacks is None:
            return
        if self._shutdown:
            return
        success, failure = callbacks
        if error is None:
            self._succeeded(value, success)
        else:
            self._close_callback = None
            self._failed(error, failure)
        self.pending_changed.emit(self.pending)
        if not self.pending and self._close_callback is not None:
            callback, self._close_callback = self._close_callback, None
            QTimer.singleShot(0, callback)

    def _succeeded(self, value, callback):
        try:
            if callback is not None:
                callback(value)
            else:
                self.on_change()
        except Exception as error:
            self._close_callback = None
            self.on_error(error)

    def _failed(self, error, callback):
        if callback is not None:
            callback(error)
        else:
            self.on_error(error)

    def request_close(self, callback):
        """Retain the window until authorized writes finish or report failure."""
        if not self.pending:
            return False
        self._close_callback = callback
        return True

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        def close_writer():
            if self._writer is not None:
                self._writer.close()
                self._writer = None
        self.executor.submit(close_writer)
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _request_maintenance(self, identities=None):
        if self._shutdown:
            return
        if identities is None:
            self._maintenance_all = True
        else:
            self._maintenance_ids.update(identities)
        if self._maintenance:
            self._maintenance_again = True
            return
        self._maintenance = True
        selected = None if self._maintenance_all else set(self._maintenance_ids)
        self._maintenance_ids.clear()
        self._maintenance_all = False
        def done(_value):
            self._maintenance = False
            self._maintenance_again = False
            parent = self.parent()
            if parent is not None and isValid(parent):
                if hasattr(parent, '_surface_data_key'):
                    parent._surface_data_key = None
                if hasattr(parent, '_events_revision'):
                    parent._events_revision = None
            self.on_change()
            if self._maintenance_all or self._maintenance_ids:
                self._request_maintenance(None if self._maintenance_all else self._maintenance_ids)
        def failed(error):
            self._maintenance = False
            self._maintenance_again = False
            self.on_error(error)
        self.write(lambda writer: writer.maintain_queries(identities=selected), done, failed, force_async=True)

    def _request_background_write(self, operation, key=None):
        """Persist diagnostics discovered by a read without writing on Qt."""
        if self._shutdown or key is not None and key in self._background_keys:
            return
        if key is not None:
            self._background_keys.add(key)
        def done(changed):
            self._background_keys.discard(key)
            if changed:
                self.on_change()
        def failed(error):
            self._background_keys.discard(key)
            self.on_error(error)
        self.write(operation, done, failed, force_async=True)


def submit_write(widget, store, operation, on_success=None, on_error=None):
    """Use a window's command queue from a dialog, or a standalone queue."""
    if not hasattr(store, 'connection'):
        # Lightweight non-SQL stores used by embedded dialogs cannot contend
        # with a database writer and keep their synchronous protocol.
        try:
            value = operation(store)
        except Exception as error:
            if on_error is not None:
                on_error(error)
            else:
                raise
        else:
            if on_success is not None:
                on_success(value)
        return True
    owner = widget
    while owner is not None:
        if callable(getattr(owner, 'write_database', None)):
            def succeeded(value):
                if isValid(widget) and on_success is not None:
                    on_success(value)
                if isValid(owner) and not getattr(owner, '_closed', False):
                    owner.refresh()
            def failed(error):
                if isValid(widget) and on_error is not None:
                    on_error(error)
                elif isValid(owner):
                    owner.report_error(str(error))
            return owner.write_database(operation, succeeded, failed, preserve_context=False)
        owner = owner.parentWidget()
    commands = getattr(store, '_standalone_ui_commands', None)
    if commands is None or not isValid(commands):
        commands = store._standalone_ui_commands = DatabaseCommands(store)
    def succeeded(value):
        if isValid(widget) and on_success is not None:
            on_success(value)
    def failed(error):
        if isValid(widget) and on_error is not None:
            on_error(error)
    return commands.write(operation, succeeded, failed)
