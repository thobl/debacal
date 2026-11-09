"""Small profile-local UI choices, independent of calendar synchronization.

Calendar/event transactions never acquire this connection's write lock. Existing
profiles move their UI settings here once, leaving canonical calendar data and
history unchanged. The file is named <profile-database>.ui.sqlite3.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time


UI_SETTINGS = frozenset({
    'display_preferences', 'week_numbers_default_version',
    'keybindings', 'keybindings_version', 'calendar_order', 'default_calendar_id',
    'selection_mode', 'show_full_day', 'filter_calendar_by_search',
})


def ui_state_path(database: str) -> str:
    # Retain the complete database filename so alternative files with the same
    # stem (for example example.db and example.sqlite3) cannot share choices.
    return ':memory:' if database == ':memory:' else str(Path(database).with_name(Path(database).name + '.ui.sqlite3'))


class UiState:
    def __init__(self, database: str):
        self.path = ui_state_path(database)
        self._lock = RLock()
        self._sequence = 0
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            self._startup(self._initialize)
        except BaseException:
            self.connection.close()
            raise

    def _startup(self, operation):
        """Serialize first-use setup, then restore fail-fast runtime writes.

        Independent processes/connections may open a new profile together.
        Journal negotiation can ignore SQLite's busy handler, so retry it within
        the same bounded startup deadline. Normal UI changes never retry/wait.
        """
        with self._lock:
            deadline = time.monotonic() + 5
            try:
                while True:
                    remaining = max(0, round((deadline - time.monotonic()) * 1000))
                    self.connection.execute(f'PRAGMA busy_timeout={remaining}')
                    try:
                        return operation()
                    except sqlite3.OperationalError as error:
                        code = getattr(error, 'sqlite_errorcode', 0)
                        if code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
            finally:
                self.connection.execute('PRAGMA busy_timeout=0')

    def _initialize(self):
        if self.path != ':memory:' and self.connection.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
            self.connection.execute('PRAGMA journal_mode=WAL')
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'settings', 'calendar_visibility'}.issubset(tables):
            with self.transaction():
                self.connection.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                self.connection.execute('CREATE TABLE IF NOT EXISTS calendar_visibility ('
                                        'calendar_id TEXT PRIMARY KEY, visible INTEGER NOT NULL CHECK(visible IN (0,1)))')

    @contextmanager
    def transaction(self):
        with self._lock:
            nested = self.connection.in_transaction
            self._sequence += 1
            name = f'ui_state_{self._sequence}'
            try:
                self.connection.execute(f'SAVEPOINT {name}' if nested else 'BEGIN IMMEDIATE')
                yield
                self.connection.execute(f'RELEASE SAVEPOINT {name}' if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.connection.execute(f'ROLLBACK TO SAVEPOINT {name}')
                    self.connection.execute(f'RELEASE SAVEPOINT {name}')
                elif self.connection.in_transaction:
                    self.connection.execute('ROLLBACK')
                raise

    def get(self, key):
        """Return (present,value), distinguishing an explicit JSON null."""
        with self._lock:
            row = self.connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return (False, None) if row is None else (True, json.loads(row[0]))

    def set(self, key, value):
        payload = json.dumps(value, sort_keys=True, separators=(',', ':'))
        with self.transaction():
            self.connection.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, payload))

    def visibility(self, calendar_id):
        with self._lock:
            row = self.connection.execute('SELECT visible FROM calendar_visibility WHERE calendar_id=?',
                                          (calendar_id,)).fetchone()
            return None if row is None else bool(row[0])

    def visibility_overrides(self):
        with self._lock:
            return {row[0]: bool(row[1]) for row in self.connection.execute('SELECT calendar_id,visible FROM calendar_visibility')}

    def set_visible(self, calendar_id, visible):
        with self.transaction():
            self.connection.execute('INSERT INTO calendar_visibility(calendar_id,visible) VALUES (?,?) '
                                    'ON CONFLICT(calendar_id) DO UPDATE SET visible=excluded.visible',
                                    (calendar_id, bool(visible)))

    def import_legacy(self, source, *, seed):
        """Copy old UI choices once without modifying the event database.

        New databases mark this step complete without creating overrides for
        their initial calendar descriptors. INSERT OR IGNORE also preserves
        explicit choices if an interrupted upgrade already wrote some of them.
        """
        if self.get('_legacy_import_complete')[0]:
            return
        def migrate():
            with self.transaction():
                if self.get('_legacy_import_complete')[0]:
                    return
                if seed:
                    for key, value in source.execute('SELECT key,value FROM settings'):
                        if key in UI_SETTINGS:
                            self.connection.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',
                                                    (key, value))
                    for calendar_id, payload in source.execute('SELECT id,data FROM calendars'):
                        visible = json.loads(payload).get('visible', True)
                        self.connection.execute('INSERT OR IGNORE INTO calendar_visibility(calendar_id,visible) VALUES (?,?)',
                                                (calendar_id, bool(visible)))
                self.set('_legacy_import_complete', True)
        self._startup(migrate)

    def close(self):
        with self._lock:
            self.connection.close()
