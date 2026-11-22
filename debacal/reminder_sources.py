"""Worker-owned, read-only snapshots of incremental reminder inputs."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
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


def _file_id(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _incarnation(db) -> str:
    row = db.execute('SELECT token FROM database_incarnation WHERE id=1').fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise sqlite3.OperationalError('The calendar database incarnation is unavailable.')
    return row[0]


class ReminderSources:
    def __init__(self, database: str):
        if database == ':memory:':
            raise ValueError('A reminder reader requires a file-backed canonical database.')
        self._path = Path(database).absolute()
        self._connection = None
        self._main_id = self._wal_id = None
        self._marker = self._expected = None
        self._closed = False
        self.incarnation()

    def _checked_wal(self, target, main_id):
        try:
            wal_id = _file_id(Path(str(target) + '-wal'))
        except FileNotFoundError:
            wal_id = None
        if _file_id(target) != main_id or _file_id(self._path) != main_id:
            raise sqlite3.OperationalError('The calendar database path changed during a read. Retry the profile.')
        if self._main_id != main_id and wal_id is not None and wal_id == self._wal_id:
            raise sqlite3.OperationalError('The calendar database was replaced with its previous WAL still present. '
                                           'Suspend reminders until a consistent database and WAL are available.')
        return wal_id

    @contextmanager
    def for_incarnation(self, token: str):
        """Require this durable identity inside every SQLite snapshot in the scope."""
        if not isinstance(token, str) or not token:
            raise ValueError('A nonempty calendar database incarnation is required.')
        previous = self._expected
        if previous is not None and previous != token:
            raise ValueError('A nested reminder read requires the same incarnation.')
        self._expected = token
        try:
            yield self
        finally:
            self._expected = previous

    @contextmanager
    def _snapshot(self, *, refresh_identity=False):
        if self._closed:
            raise sqlite3.ProgrammingError('The reminder source reader is closed.')
        target = self._path.resolve(strict=True)
        main_id = _file_id(target)
        self._checked_wal(target, main_id)
        # Open the resolved target for this batch. A racing rename cannot leave
        # subsequent polls indefinitely attached to an incorrectly cached handle.
        db = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            marker = _incarnation(db)  # Pins the identity and all subsequent rows to one WAL snapshot.
            wal_id = self._checked_wal(target, main_id)
            if self._expected is not None and marker != self._expected:
                raise sqlite3.OperationalError('The calendar database incarnation differs from the expected profile.')
            if main_id != self._main_id or marker != self._marker:
                if main_id == self._main_id and not refresh_identity:
                    raise sqlite3.OperationalError('The calendar database incarnation changed. Refresh the profile identity.')
                # A directory can move away and back between two successful stat
                # checks. Confirm a new inode/token pairing through the selected
                # name after the primary connection has pinned its own marker.
                with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as selected:
                    if _incarnation(selected) != marker:
                        raise sqlite3.OperationalError('The pinned calendar incarnation differs from the selected profile.')
            yield db
            if self._checked_wal(target, main_id) != wal_id:
                raise sqlite3.OperationalError('The calendar WAL changed during a read. Retry the profile.')
            db.rollback()
        except BaseException:
            db.close()
            raise
        # Keep the last observed SQLite file set open between snapshots. Its live
        # inode numbers cannot be recycled and mistaken for a retained old WAL.
        if self._connection is not None:
            self._connection.close()
        self._connection = db
        self._main_id, self._wal_id, self._marker = main_id, wal_id, marker

    def incarnation(self) -> str:
        with self._snapshot(refresh_identity=True):
            pass
        return self._marker

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
            rows = db.execute(sql, (*params, limit))
            first = rows.fetchone()
            if first is None:
                return ()
            storage = ProjectionStorage(db)
            reader = storage.source_reader()
            return tuple(self._change(row, storage, reader) for row in chain((first,), rows))

    def _change(self, row, storage, reader):
        stamp, identity = row['version'], row['id']
        if row['data'] is not None:
            try:
                event = Event.from_dict(json.loads(row['data']))
                if event.id == identity and event.calendar_id == row['calendar_id']:
                    event = alarm_ics.hydrate_alarms(event)
                    indexed = storage.load_source(reader, identity, event.calendar_id, stamp)
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
        self._closed = True
        self._connection.close()
