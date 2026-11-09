"""Durable prepared source data; the interval trees themselves are rebuilt.

Only primitives are stored. The format and timezone-engine identity are checked
before use; canonical events remain available to rebuild derived state after an
upgrade or damage. No date window or query answer is represented here.
"""
from __future__ import annotations

import hashlib
import json
from math import isfinite
from pathlib import Path
import zlib
from zoneinfo import TZPATH

from .event_range_index import FINITE_RULE_LIMIT, IndexedSource
from .prepared_storage import ENGINE_VERSION, FORMAT_VERSION, PreparedStateError, StateReader, StateWriter


# Bump when preparation semantics or supported data change, even if the binary
# layout is unchanged: older failed preparations must be reconsidered too.
PROJECTION_VERSION = 1


def environment_signature() -> str:
    """Identify native timezone data without reading every timezone file."""
    native = []
    for root in TZPATH:
        path = Path(root)
        for name in ('tzdata.zi', '+VERSION', 'version'):
            candidate = path / name
            try:
                stat = candidate.stat()
                with candidate.open('rb') as source:
                    prefix = source.read(256).decode('ascii', errors='replace')
            except OSError:
                continue
            native.append((str(candidate), stat.st_size, stat.st_mtime_ns, prefix))
            break
        else:
            try:
                native.append((str(path), path.stat().st_mtime_ns))
            except OSError:
                pass
    try:
        import tzdata
        native.append(('tzdata', tzdata.__version__, tzdata.IANA_VERSION))
    except ImportError:
        pass
    value = [PROJECTION_VERSION, FORMAT_VERSION, ENGINE_VERSION, FINITE_RULE_LIMIT, native]
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def pack_state(value) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return b'QP1' + zlib.compress(raw, 1)


def unpack_state(value):
    if not isinstance(value, bytes) or value[:3] != b'QP1':
        raise PreparedStateError('Unsupported query projection container.')
    try:
        return json.loads(zlib.decompress(value[3:]))
    except (ValueError, zlib.error, UnicodeError) as error:
        raise PreparedStateError('The prepared query data needs rebuilding.') from error


def record_state(record: IndexedSource, writer: StateWriter):
    master = record.master
    # A completely unrolled schedule is represented by its individual points.
    # Keeping its compiled rule and a second set of finite dates would duplicate
    # both startup work and memory without serving any query.
    has_rule = any(tree_key[2] for tree_key, _key in record.entries)
    prepared = writer.series(record.prepared) if has_rule and record.prepared is not None and record.error is None else None
    explicit_ids = []
    if prepared is not None:
        explicit_ids = [item.id for item in record.prepared.explicit_occurrences]
        prepared.pop('explicit', None)
    return {
        'master': writer.event(master),
        'prepared': prepared,
        'explicit': explicit_ids,
        'error': ([type(record.error).__name__, str(record.error)] if record.error is not None else None),
        'occurrences': [None if item is master else writer.event(item, base=master)
                        for item in record.occurrences.values()],
        'entries': [[kind, rule, lower if isfinite(lower) else None,
                     upper if isfinite(upper) else None, identity]
                    for (_calendar, kind, rule), (lower, upper, identity) in record.entries],
    }


def load_record(state, reader: StateReader) -> IndexedSource:
    from .recurrence import RecurrenceError, RecurrenceRangeError

    master = reader.event(state['master'])
    record = IndexedSource(master)
    for value in state['occurrences']:
        item = master if value is None else reader.event(value, base=master)
        record.occurrences[item.id] = item
    for kind, rule, lower, upper, identity in state['entries']:
        # Reuse the occurrence's identity object rather than retaining a second
        # equal JSON string for every entry/removal handle.
        identity = master.id if rule else record.occurrences[identity].id
        record.entries.append(((master.calendar_id, kind, bool(rule)),
                               (float('-inf') if lower is None else lower,
                                float('inf') if upper is None else upper, identity)))
    if state['prepared'] is not None:
        explicit = tuple(record.occurrences[identity] for identity in state['explicit'])
        record.prepared = reader.series(master, state['prepared'], explicit_occurrences=explicit)
    if state['error'] is not None:
        name, message = state['error']
        errors = {'RecurrenceError': RecurrenceError, 'RecurrenceRangeError': RecurrenceRangeError}
        record.error = errors.get(name, RecurrenceError)(message)
    return record


class ProjectionStorage:
    """Transactional SQLite representation shared by readers and sync writers."""

    def __init__(self, connection):
        self.connection = connection
        self.signature = environment_signature()
        self.reader = None
        self.shared = {}

    def initialize(self):
        # execute(), rather than executescript(), retains the caller's enclosing
        # transaction during upgrades and source writes.
        for statement in (
            'CREATE TABLE IF NOT EXISTS event_query_state (id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT NOT NULL, version INTEGER NOT NULL)',
            # The change journal retires deleted sources explicitly, retaining
            # their reference rows until batch-final shared-data cleanup.
            'CREATE TABLE IF NOT EXISTS event_query_projection (event_id TEXT PRIMARY KEY, data BLOB)',
            'CREATE TABLE IF NOT EXISTS event_query_shared (reference TEXT PRIMARY KEY, kind TEXT NOT NULL, data BLOB NOT NULL)',
            'CREATE TABLE IF NOT EXISTS event_query_references (event_id TEXT NOT NULL REFERENCES event_query_projection(event_id) ON DELETE CASCADE, reference TEXT NOT NULL REFERENCES event_query_shared(reference), PRIMARY KEY(event_id,reference))',
            'CREATE INDEX IF NOT EXISTS event_query_references_ref ON event_query_references(reference)',
        ):
            self.connection.execute(statement)
        status = self.connection.execute('SELECT signature FROM event_query_state WHERE id=1').fetchone()
        if status is None or status[0] != self.signature:
            self.connection.execute('DELETE FROM event_query_references')
            self.connection.execute('DELETE FROM event_query_projection')
            self.connection.execute('DELETE FROM event_query_shared')
            self.connection.execute('INSERT INTO event_query_state(id,signature,version) VALUES (1,?,-1) '
                                    'ON CONFLICT(id) DO UPDATE SET signature=excluded.signature,version=-1',
                                    (self.signature,))
            return ()
        # Normal writes keep both counts aligned transactionally. A missing
        # derived row (for example after manual repair) must not hide a source.
        counts = self.connection.execute('SELECT (SELECT count(*) FROM events),'
                                         '(SELECT count(*) FROM event_query_projection)').fetchone()
        if counts[0] == counts[1]:
            return ()
        missing = [row[0] for row in self.connection.execute(
            'SELECT events.id FROM events LEFT JOIN event_query_projection AS projection '
            'ON projection.event_id=events.id WHERE projection.event_id IS NULL')]
        missing.extend(row[0] for row in self.connection.execute(
            'SELECT projection.event_id FROM event_query_projection AS projection LEFT JOIN events '
            'ON events.id=projection.event_id WHERE events.id IS NULL'))
        return missing

    def version(self):
        return self.connection.execute('SELECT version FROM event_query_state WHERE id=1').fetchone()[0]

    def _shared_sink(self, kind, reference, value):
        key = 'definition:' + reference if kind == 'definition' else reference
        data = value.encode('utf-8') if kind == 'definition' else pack_state(value)
        self.connection.execute('INSERT INTO event_query_shared(reference,kind,data) VALUES (?,?,?) '
                                'ON CONFLICT(reference) DO UPDATE SET kind=excluded.kind,data=excluded.data '
                                'WHERE kind IS NOT excluded.kind OR data IS NOT excluded.data',
                                (key, kind, data))

    def maintain(self, revision, *, identities=None):
        """Compile only modified canonical sources, in the same transaction."""
        indexed = self.version()
        if identities is None and indexed == revision:
            return []
        if identities is not None:
            rows = []
            ids = list(identities)
            for offset in range(0, len(ids), 500):
                part = ids[offset:offset + 500]
                rows.extend(self.connection.execute(
                    'SELECT id,calendar_id,data FROM events WHERE id IN (' + ','.join('?' for _ in part) + ')', part))
            found = {row['id'] for row in rows}
            rows.extend({'id': identity, 'calendar_id': None, 'data': None} for identity in ids if identity not in found)
        elif indexed < 0:
            rows = self.connection.execute('SELECT id,calendar_id,data FROM events')
        else:
            rows = self.connection.execute(
                'SELECT changes.event_id AS id,events.calendar_id,events.data FROM event_query_changes AS changes '
                'LEFT JOIN events ON events.id=changes.event_id WHERE changes.version>?', (indexed,))
        from .event_range_index import EventRangeIndex
        from .models import Event

        rejected, old_references = [], set()
        writer = StateWriter(shared_sink=self._shared_sink)
        for row in rows:
            identity = row['id']
            old_references.update(item[0] for item in self.connection.execute(
                'SELECT reference FROM event_query_references WHERE event_id=?', (identity,)))
            self.connection.execute('DELETE FROM event_query_references WHERE event_id=?', (identity,))
            if row['data'] is None:
                self.connection.execute('DELETE FROM event_query_projection WHERE event_id=?', (identity,))
                continue
            value = None
            writer.reset_references()
            try:
                value = json.loads(row['data'])
                event = Event.from_dict(value)
                record = EventRangeIndex.prepare(event)
                data = pack_state(record_state(record, writer))
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                # Invalid canonical data stays retained and visible in Issues;
                # a NULL projection prevents repeatedly rebuilding that source.
                raw = value.get('raw_ics', '') if isinstance(value, dict) else ''
                if not isinstance(raw, (str, bytes)):
                    raw = ''
                rejected.append((row['calendar_id'], identity, raw, f'The stored event could not be read: {error}'))
                data = None
            self.connection.execute('INSERT INTO event_query_projection(event_id,data) VALUES (?,?) '
                                    'ON CONFLICT(event_id) DO UPDATE SET data=excluded.data', (identity, data))
            if data is not None:
                for kind, reference in writer.references:
                    key = 'definition:' + reference if kind == 'definition' else reference
                    self.connection.execute('INSERT INTO event_query_references(event_id,reference) VALUES (?,?)',
                                            (identity, key))
        # Cleanup is batch-final: the shared writer can legitimately reuse a
        # definition first encountered by an earlier source in this operation.
        for reference in old_references:
            self.connection.execute('DELETE FROM event_query_shared WHERE reference=? AND NOT EXISTS '
                                    '(SELECT 1 FROM event_query_references WHERE reference=?)', (reference, reference))
        if identities is None:
            self.connection.execute('UPDATE event_query_state SET version=? WHERE id=1', (revision,))
        return rejected

    def load_catalog(self):
        # Keep only the small live-reference catalog. Large packed timezone
        # tables are read once on reconstruction, not copied on every unrelated
        # edit or retained beside their restored numeric representation.
        self.shared = {row['reference']: row['kind'] for row
                       in self.connection.execute('SELECT reference,kind FROM event_query_shared')}
        if self.reader is None:
            self.reader = StateReader(self._load_zone, self._load_definition)
        # Prune no-longer-referenced interned data without duplicating objects
        # still used by unchanged sources on incremental reloads.
        self.reader.retain_references(
            {reference for reference, kind in self.shared.items() if kind == 'zone'},
            {reference.removeprefix('definition:') for reference, kind in self.shared.items()
             if kind == 'definition'})
        return self.reader

    def _load_shared(self, reference, kind):
        row = self.connection.execute('SELECT data FROM event_query_shared WHERE reference=? AND kind=?',
                                      (reference, kind)).fetchone()
        if row is None:
            raise PreparedStateError('A shared prepared value is missing.')
        return bytes(row[0])

    def _load_zone(self, reference):
        return unpack_state(self._load_shared(reference, 'zone'))

    def _load_definition(self, reference):
        return self._load_shared('definition:' + reference, 'definition').decode('utf-8')
