"""Worker-owned, read-only snapshots of incremental reminder inputs."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from . import alarm_ics
from .event_range_index import EventRangeIndex, IndexedSource
from .models import Event
from .query_projection import ProjectionStorage


@dataclass(frozen=True, slots=True)
class SourceChange:
    version: int
    event_id: str
    event: Event | None
    indexed: IndexedSource | None


class ReminderSources:
    def __init__(self, database: str):
        if database == ':memory:':
            raise ValueError('A reminder reader requires a file-backed canonical database.')
        self._path = Path(database).absolute()
        self._connection = None
        self._file_id = None
        self._connect()

    def _connect(self):
        stat = self._path.stat()
        identity = stat.st_dev, stat.st_ino
        if identity != self._file_id:
            # SQLite can continue reading an unlinked file. Open the replacement
            # before serving the next read, even when its revision is higher.
            connection = sqlite3.connect(self._path.resolve().as_uri() + '?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            if self._connection is not None:
                self._connection.close()
            self._connection = connection
            self._storage = ProjectionStorage(connection)
            self._file_id = identity
        return self._connection

    @contextmanager
    def _snapshot(self):
        db = self._connect()
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.rollback()

    def incarnation(self) -> str:
        with self._snapshot() as db:
            return db.execute('SELECT token FROM database_incarnation WHERE id=1').fetchone()[0]

    def revision(self) -> int:
        with self._snapshot() as db:
            return db.execute('SELECT version FROM event_revisions WHERE id=1').fetchone()[0]

    def scan(self, *, after_id: str | None = None, calendar_id: str | None = None,
             limit: int = 128) -> tuple[SourceChange, ...]:
        sql = ('SELECT e.id,e.calendar_id,e.data,COALESCE(c.version,0) AS version FROM events AS e '
               'LEFT JOIN event_query_changes AS c ON c.event_id=e.id')
        conditions, params = [], []
        if calendar_id is not None:
            conditions.append('e.calendar_id=?')
            params.append(calendar_id)
        if after_id is not None:
            conditions.append('e.id>?')
            params.append(after_id)
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        return self._batch(sql + ' ORDER BY e.id LIMIT ?', params, limit)

    def changes(self, after: tuple[int, str], *, limit: int = 128) -> tuple[SourceChange, ...]:
        return self._batch(
            'SELECT c.event_id AS id,e.calendar_id,e.data,c.version FROM event_query_changes AS c '
            'LEFT JOIN events AS e ON e.id=c.event_id WHERE (c.version,c.event_id)>(?,?) '
            'ORDER BY c.version,c.event_id LIMIT ?', after, limit)

    def _batch(self, sql, params, limit):
        if limit < 0:
            raise ValueError('A reminder source batch limit must be nonnegative.')
        with self._snapshot() as db:
            reader = self._storage.source_reader()
            return tuple(self._change(row, reader) for row in db.execute(sql, (*params, limit)))

    def _change(self, row, reader):
        stamp, identity = row['version'], row['id']
        if row['data'] is not None:
            try:
                event = Event.from_dict(json.loads(row['data']))
                if event.id == identity and event.calendar_id == row['calendar_id']:
                    event = alarm_ics.hydrate_alarms(event)
                    indexed = self._storage.load_source(reader, identity, event.calendar_id, stamp)
                    if indexed is None:
                        indexed = EventRangeIndex.prepare(event)
                    return SourceChange(stamp, identity, event, indexed)
            except (ValueError, TypeError, LookupError, OverflowError, AttributeError, RecursionError):
                pass  # Retire this source's schedule and keep paging past its stamp.
        return SourceChange(stamp, identity, None, None)

    def source_stamp(self, event_id: str) -> int | None:
        with self._snapshot() as db:
            row = db.execute('SELECT COALESCE(c.version,0) FROM events AS e '
                             'LEFT JOIN event_query_changes AS c ON c.event_id=e.id WHERE e.id=?',
                             (event_id,)).fetchone()
            return row[0] if row is not None else None

    def calendar_states(self) -> dict[str, tuple[bool, int, datetime]]:
        with self._snapshot() as db:
            return {identity: (bool(enabled), generation, datetime.fromisoformat(changed))
                    for identity, enabled, generation, changed in db.execute(
                        'SELECT calendar_id,enabled,generation,changed_at FROM calendar_enable_transitions '
                        'WHERE enabled IS NOT NULL')}

    def close(self) -> None:
        self._connection.close()
