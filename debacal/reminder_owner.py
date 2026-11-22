"""The independent, nonblocking delivery lock for a local profile."""
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLockFile

from .single_instance import _runtime_directory


class ReminderOwner:
    def __init__(self, database: str):
        self.database = str(Path(database).expanduser().resolve())
        identity = os.path.normcase(self.database) if database != ':memory:' else uuid4().hex
        digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:24]
        self._lock = QLockFile(str(_runtime_directory() / f'reminder-{digest}.lock'))
        self._lock.setStaleLockTime(0)
        self.epoch = None

    def acquire(self) -> bool:
        if self.epoch is None and self._lock.tryLock(0):
            self.epoch = uuid4().hex
        return self.epoch is not None

    def release(self) -> None:
        if self.epoch is not None:
            self.epoch = None
            self._lock.unlock()
