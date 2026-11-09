"""Give queued user writes a turn between background sync transactions."""
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Lock, local
from weakref import WeakValueDictionary


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
    def __init__(self):
        self.condition = Condition()
        self.pending = 0

    def request_foreground(self):
        with self.condition:
            self.pending += 1
        released = False
        def release():
            nonlocal released
            with self.condition:
                if not released:
                    released = True
                    self.pending -= 1
                    self.condition.notify_all()
        return release

    def wait_background(self):
        if getattr(_thread, 'foreground', False):
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
