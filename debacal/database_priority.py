"""Give queued user writes a turn between background sync transactions."""
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Lock, local
from weakref import WeakValueDictionary
import mmap
import os
import time


_gates = WeakValueDictionary()
_gate_lock = Lock()
_thread = local()


@contextmanager
def foreground_work():
    previous = getattr(_thread, 'foreground', False)
    _thread.foreground = True
    try:
        yield
    finally:
        _thread.foreground = previous


class WriteGate:
    def __init__(self, inherited_fd=None):
        self.condition = Condition()
        self.pending = 0
        self._descriptor = None
        self._shared = None
        self._inherited = inherited_fd is not None
        if inherited_fd is not None:
            self._descriptor = os.dup(inherited_fd)
            self._shared = mmap.mmap(self._descriptor, 1, access=mmap.ACCESS_READ)

    def descriptor(self):
        """Create one anonymous shared priority byte only when a child needs it."""
        with self.condition:
            if self._descriptor is None:
                descriptor = os.memfd_create('debacal-write-priority', os.MFD_CLOEXEC)
                try:
                    os.ftruncate(descriptor, 1)
                    shared = mmap.mmap(descriptor, 1, access=mmap.ACCESS_WRITE)
                except BaseException:
                    os.close(descriptor)
                    raise
                self._descriptor, self._shared = descriptor, shared
                self._shared[0] = bool(self.pending)
            return self._descriptor

    def _publish(self):
        if self._shared is not None and not self._inherited:
            self._shared[0] = bool(self.pending)

    def __del__(self):
        if self._shared is not None:
            self._shared.close()
        if self._descriptor is not None:
            os.close(self._descriptor)

    def request_foreground(self):
        with self.condition:
            self.pending += 1
            self._publish()
        released = False
        def release():
            nonlocal released
            with self.condition:
                if not released:
                    released = True
                    self.pending -= 1
                    self._publish()
                    self.condition.notify_all()
        return release

    def wait_background(self):
        if getattr(_thread, 'foreground', False):
            return
        if self._inherited:
            # Memory-only polling: never acquire a SQLite write lock while a
            # user command is queued in the parent process.
            while self._shared[0]:
                time.sleep(.01)
            return
        with self.condition:
            self.condition.wait_for(lambda: not self.pending)


def get_write_gate(path):
    if str(path) == ':memory:':
        return WriteGate()
    key = str(Path(path).expanduser().resolve())
    with _gate_lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = WriteGate()
        return gate


def inherit_write_gate(path, descriptor):
    """Register the parent's read-only shared view before opening a child Store."""
    key = str(Path(path).expanduser().resolve())
    with _gate_lock:
        gate = WriteGate(descriptor)
        _gates[key] = gate
        return gate
