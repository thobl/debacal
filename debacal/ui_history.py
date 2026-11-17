"""Keep large history validation out of Qt's event loop."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer


# Ordinary edits still update the toolbar immediately. Imports can have many
# megabytes of canonical before/after snapshots and need a separate interpreter.
INLINE_HISTORY_BYTES = 256 * 1024


class HistoryAvailability(QObject):
    def __init__(self, store, parent, on_result, on_error):
        super().__init__(parent)
        self.store = store
        self.on_result = on_result
        self.on_error = on_error
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)
        self._executor = self._processor = self._future = None
        self._signature = None
        self._closed = False

    def update(self):
        if self._closed:
            return
        signature, size, cached = self.store.history_availability_probe()
        if cached is not None:
            self.on_result(*cached)
        elif size <= INLINE_HISTORY_BYTES or self.store.path == ':memory:':
            self.on_result(*self.store.history_availability())
        else:
            # Never enable an action using an out-of-date background answer.
            self.on_result(False, False)
            if self._future is None:
                if self._executor is None:
                    from .sync_worker import SyncProcessor
                    self._processor = SyncProcessor(self.store.path)
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-history')
                self._signature = signature
                self._future = self._executor.submit(self._processor.run, 'history')
                self.timer.start()

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        future, signature = self._future, self._signature
        self._future = self._signature = None
        self.timer.stop()
        try:
            result = future.result()
        except Exception as error:
            self.on_error(str(error), title='Undo/Redo availability could not be checked')
            return
        if (result['revision'] == signature[0]
                and result['signature'] == self.store._json(signature)):
            self.store.remember_history_availability(signature, result['available'])
        self.update()

    def shutdown(self):
        self._closed = True
        self.timer.stop()
        if self._processor is not None:
            self._processor.shutdown()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
