"""Qt facade for the profile's background reminder scheduler and local inbox."""
from datetime import datetime, timedelta, timezone
from itertools import count

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .reminder_worker import ReminderWorker


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ReminderController(QObject):
    preferencesChanged = Signal(object)
    inboxPageSnapshot = Signal(int, object, object)
    inboxChanged = Signal()
    issueChanged = Signal(object)
    activate = Signal(object)
    inboxPageReady = Signal(int, object)
    unreadCountChanged = Signal(int)
    desktopAvailabilityChanged = Signal(bool)
    testNotificationShown = Signal(object)
    _notice = Signal(object, str, object)

    def __init__(self, database: str, *, clock: Clock, transport, delivery_allowed: bool, profile_label=''):
        super().__init__()
        self._database, self._clock, self._transport = database, clock, transport
        self._allowed, self._profile = delivery_allowed, profile_label
        self._retiring = self._worker = self._partition = None
        self.activation_context = None
        self.desktop_available = None
        self.unread_count = 0
        self._requests = count(1)
        self._notice.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        transport.delivered.connect(self._delivered)
        transport.failed.connect(self._failed)
        transport.retired.connect(self._retired)
        transport.action.connect(self._action)
        transport.availabilityChanged.connect(self._availability)

    def now(self):
        return self._clock.now()

    def start(self):
        if self._worker is None:
            self._worker = ReminderWorker(self._database, self._clock, self._transport, self._allowed,
                                          self._profile, self._notice.emit, previous=self._retiring)
            self._retiring = None
            self._worker.start()

    def stop(self):
        worker = self._worker
        self._worker = self._partition = None
        self.activation_context = None
        self.desktop_available = None
        if worker:
            self._retiring = worker
            worker.stop()
            self._transport.stop()

    def _queue(self, name, *args):
        if self._worker:
            self._worker.enqueue(name, *args)

    def reload_settings(self):
        self._queue('wake')

    def request_inbox(self, *, limit=100, offset=0, active_only=False):
        if limit < 0 or offset < 0:
            raise ValueError('Inbox page limits must be nonnegative.')
        request = next(self._requests)
        self._queue('page', request, limit, offset, active_only)
        return request

    @property
    def inbox_partition(self):
        """Bind actions on a displayed page to the database that supplied it."""
        return self._partition

    def mark_read(self, keys, *, partition=None):
        self._queue('read', partition or self._partition, tuple(keys))

    def dismiss(self, key, *, partition=None):
        self._queue('dismiss', partition or self._partition, key)

    def snooze(self, key, duration: timedelta, *, partition=None):
        if duration <= timedelta():
            raise ValueError('Choose a positive snooze duration.')
        if duration > datetime.max.replace(tzinfo=timezone.utc) - self._clock.now():
            raise ValueError('Choose a shorter snooze duration.')
        self._queue('snooze', partition or self._partition, key, duration)

    def open_reminder(self, key, *, partition=None):
        self._queue('open', partition or self._partition, key)

    def test_notification(self, draft):
        self._queue('preview', draft)

    @Slot(object, 'uint')
    def _delivered(self, key, remote_id):
        self._queue('delivered', key, remote_id)

    @Slot(object, str)
    def _failed(self, key, message):
        self._queue('failed', key, message)

    @Slot(object)
    def _retired(self, key):
        self._queue('retired', key)

    @Slot(object, str)
    def _action(self, key, action):
        self._queue('native', self._partition, key, action)

    @Slot(bool)
    def _availability(self, value):
        self._queue('availability', value)

    @Slot(object, str, object)
    def _receive(self, worker, name, args):
        if worker is not self._worker:
            return
        if name == 'preferences':
            self.preferencesChanged.emit(*args)
        elif name == 'inbox':
            self._partition, = args
            self.inboxChanged.emit()
        elif name == 'page':
            request, entries, token = args
            self._partition = token
            self.inboxPageReady.emit(request, entries)
            self.inboxPageSnapshot.emit(request, entries, token)
        elif name == 'count':
            self.unread_count, = args
            self.unreadCountChanged.emit(self.unread_count)
        elif name == 'availability':
            self.desktop_available, = args
            self.desktopAvailabilityChanged.emit(self.desktop_available)
        elif name == 'activate':
            due, context = args
            if context.current_partition(self._database):
                self.activation_context = context
                self.activate.emit(due)
        elif name == 'issue':
            self.issueChanged.emit(*args)
        elif name == 'preview':
            self.testNotificationShown.emit(*args)


def create_reminder_controller(database, *, delivery_allowed, profile_label):
    from .reminder_transport import NotificationTransport
    return ReminderController(database, clock=Clock(), transport=NotificationTransport(),
                              delivery_allowed=delivery_allowed, profile_label=profile_label)
