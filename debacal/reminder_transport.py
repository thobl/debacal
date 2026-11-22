"""Thread-safe nonblocking submission and Qt-thread delivery of native results."""
import asyncio
from threading import Event, RLock, Thread

from PySide6.QtCore import QObject, Signal, Slot, Qt


class _Session:
    def __init__(self, address, post, timeout):
        self.address, self.post, self.timeout = address, post, timeout
        self.stopped = Event()
        self.loop = self.bus = self.main = None

    def run(self):
        async def serve():
            from .reminder_bus import NativeBus
            self.main = asyncio.current_task()
            self.loop = asyncio.get_running_loop()
            self.bus = NativeBus(self.address, lambda kind, args: self.post(self, kind, args), self.timeout)
            if not self.stopped.is_set():
                await self.bus.run()
        try:
            asyncio.run(serve())
        except asyncio.CancelledError:
            pass
        except Exception:
            self.post(self, 'availability', False)

    def schedule(self, callback):
        if self.loop and not self.stopped.is_set():
            try:
                self.loop.call_soon_threadsafe(callback)
                return True
            except RuntimeError:
                pass
        return False

    def stop(self):
        self.stopped.set()
        if self.loop:
            try:
                self.loop.call_soon_threadsafe(self.main.cancel)
            except RuntimeError:
                pass


class NotificationTransport(QObject):
    delivered = Signal(object, 'uint')
    failed = Signal(object, str)
    action = Signal(object, str)
    availabilityChanged = Signal(bool)
    retired = Signal(object)
    _posted = Signal(object, str, object)

    def __init__(self, *, bus_address='SESSION', timeout=5):
        super().__init__()
        self._address, self._timeout = bus_address, timeout
        self._lock = RLock()
        self._session = None
        self._last_availability = self._available = None
        self._generation = 0
        self._tokens = {}
        self._posted.connect(self._receive, Qt.ConnectionType.QueuedConnection)

    @property
    def available(self):
        """None while discovery is pending, otherwise the last owner-bound result."""
        with self._lock:
            return self._available

    def start(self):
        with self._lock:
            if self._session is None:
                self._session = _Session(self._address, self._posted.emit, self._timeout)
                Thread(target=self._session.run, daemon=True, name='desktop-notifications').start()

    def probe(self):
        """Refresh a failed capability probe on an existing connection."""
        with self._lock:
            session = self._session
            if session and self._available is False:
                def refresh():
                    if session.bus.router:
                        session.bus.spawn(session.bus.probe(activate=True))
                session.schedule(refresh)

    def send(self, due, *, calendar_name, profile_label, silent):
        with self._lock:
            session = self._session
            token = self._tokens[due.key] = object()
            generation = self._generation
            if session and self._available:
                def current():
                    with self._lock:
                        return session is self._session and self._tokens.get(due.key) is token

                def submit():
                    session.bus.spawn(session.bus.notify(due, token, calendar_name, profile_label,
                                                         silent, generation, current))
                if session.schedule(submit):
                    return
            self._posted.emit(session, 'failed', (due.key, token, 'Desktop notifications are unavailable.'))

    def forget(self, key):
        with self._lock:
            token = self._tokens.pop(key, None)
            session = self._session
            if session and token:
                session.schedule(lambda: session.bus.spawn(session.bus.withdraw(key, token)))

    def stop(self):
        with self._lock:
            if self._session:
                self._session.stop()
            self._session = self._available = self._last_availability = None
            self._tokens.clear()

    @Slot(object, str, object)
    def _receive(self, session, kind, args):
        with self._lock:
            if session is not self._session:
                return
            if kind == 'owner':
                self._generation = args
                self._available = None
                return
            if kind == 'availability':
                self._available = args
                if args == self._last_availability:
                    return
                self._last_availability = args
                self.availabilityChanged.emit(args)
                return
            key, token, *rest = args
            if self._tokens.get(key) is not token:
                return
            if kind == 'retired':
                self._tokens.pop(key)
                self.retired.emit(key)
                return
            if kind == 'failed':
                self._tokens.pop(key)
                self.failed.emit(key, rest[0])
                return
            generation, value = rest
            if generation != self._generation:
                return
            if kind == 'action':
                self._tokens.pop(key)
                self.action.emit(key, value)
            else:
                self.delivered.emit(key, value)
