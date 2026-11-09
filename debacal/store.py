"""SQLite source of truth, durable history, and a transactional sync outbox."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable, Iterator
from uuid import uuid4

from .models import Calendar, Event
from .event_duration import add_duration, end_for, same_duration
from .schedule_replacement import ScheduleReplacement
from .ui_state import UiState, UI_SETTINGS


_EVENT_UID_SQL = "CASE WHEN json_valid(data) THEN COALESCE(NULLIF(json_extract(data,'$.ical_uid'),''),id) ELSE id END"
_SYNC_UID_SQL = "CASE WHEN json_valid(data) THEN COALESCE(NULLIF(json_extract(data,'$.base_payload.event.ical_uid'),''),json_extract(data,'$.base_payload.event.id')) END"
_EVENT_ALL_DAY_SQL = "CASE WHEN json_valid(data) THEN COALESCE(json_extract(data,'$.all_day'),0) ELSE 0 END"
_EVENT_FLOATING_SQL = "CASE WHEN json_valid(data) THEN COALESCE(json_extract(data,'$.floating_time'),0) ELSE 0 END"
_EVENT_START_WALL_SQL = "CASE WHEN json_valid(data) THEN json_extract(data,'$.start') END"
_EVENT_END_WALL_SQL = "CASE WHEN json_valid(data) THEN json_extract(data,'$.end') END"
_EVENT_START_DATE_SQL = "CASE WHEN json_valid(data) THEN substr(json_extract(data,'$.start'),1,10) END"
_EVENT_END_DATE_SQL = "CASE WHEN json_valid(data) THEN substr(json_extract(data,'$.end'),1,10) END"


class HistoryConflict(ValueError):
    """The operation's events have changed since it was performed."""


class EventOwnershipConflict(ValueError):
    """A resource claims an ID or UID already owned by another resource."""


class Store:
    def __init__(self, path: str | Path, *, query_index: bool = True):
        self.path = str(path)
        legacy_profile = self.path != ':memory:' and Path(self.path).is_file() and Path(self.path).stat().st_size > 0
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_sequence = 0
        self._query_index = None
        self._query_enabled = query_index
        self._projection_storage = None
        self._query_revision = -1
        self._query_transactions = []
        self._ui_state = None
        self._ui_access = False
        self._maintenance_callback = None
        from .database_priority import get_write_gate
        self._write_gate = get_write_gate(self.path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._enable_wal()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS calendars (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                calendar_id TEXT NOT NULL REFERENCES calendars(id),
                start REAL NOT NULL, end REAL NOT NULL, data TEXT NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS events_range ON events(start, end);
            CREATE INDEX IF NOT EXISTS events_calendar ON events(calendar_id);
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL, created_at TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 1,
                before_data TEXT NOT NULL, after_data TEXT NOT NULL,
                toggled INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS operations_applied_id ON operations(applied,id DESC);
            CREATE INDEX IF NOT EXISTS operations_redo_order ON operations(applied,toggled DESC,id DESC);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, calendar_id TEXT NOT NULL,
                action TEXT NOT NULL, payload TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_metadata (
                calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY(calendar_id, event_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quarantined_resources (
                calendar_id TEXT NOT NULL REFERENCES calendars(id),
                resource_id TEXT NOT NULL, raw_ics BLOB NOT NULL,
                reason TEXT NOT NULL, updated_at TEXT NOT NULL,
                diagnostics TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(calendar_id, resource_id)
            );
        """)
        self._ui_state = UiState(self.path)
        with self._transaction():
            quarantine_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(quarantined_resources)')}
            if 'diagnostics' not in quarantine_columns:
                self.connection.execute("ALTER TABLE quarantined_resources ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
            # Old retained resources get a small lexical projection once. Never
            # transfer full ICS blobs to the status dialog's refresh timer.
            from .resource_diagnostics import DIAGNOSTIC_PREFIX_LIMIT, resource_diagnostics
            for row in self.connection.execute(
                    "SELECT calendar_id,resource_id,substr(raw_ics,1,?) AS prefix,length(raw_ics) AS raw_size FROM quarantined_resources WHERE diagnostics='{}'",
                    (DIAGNOSTIC_PREFIX_LIMIT,)):
                diagnostics = resource_diagnostics(row['prefix'])
                diagnostics['truncated'] = row['raw_size'] > DIAGNOSTIC_PREFIX_LIMIT
                self.connection.execute('UPDATE quarantined_resources SET diagnostics=? WHERE calendar_id=? AND resource_id=?',
                                        (self._json(diagnostics), row['calendar_id'], row['resource_id']))
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(events)")}
            if "recurring" not in columns:
                self.connection.execute("ALTER TABLE events ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0")
            for name, declaration in (("group_id", "TEXT"), ("candidate", "INTEGER NOT NULL DEFAULT 0"),
                                      ("search_text", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    self.connection.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_recurring_end ON events(recurring,end,start)")
            # Earlier versions could save one-date authoring expressions as
            # ordinary events. They now need the same projection/edit path as
            # larger date sets; keep the indexed flag aligned with the model.
            # Run the JSON backfill once, rather than decoding the complete
            # ordinary-event table whenever a background sync connection opens.
            query_backfill = not self.get_setting("event_query_indexes_v1", False)
            search_backfill = not self.get_setting("event_search_index_v1", False)
            if query_backfill or search_backfill:
                for row in self.connection.execute("SELECT id,data,recurring FROM events").fetchall():
                    try:
                        value = json.loads(row["data"])
                        if not isinstance(value, dict):
                            continue
                    except (ValueError, TypeError):
                        continue
                    fields, values = [], []
                    if query_backfill:
                        fields.extend(("recurring=?", "group_id=?", "candidate=?"))
                        values.extend((not value.get("series_id") and bool(value.get("recurrence_rule") or value.get("recurrence_dates")
                                                                          or value.get("recurrence_overrides") or value.get("schedule_expression")),
                                       value.get("group_id"), bool(value.get("candidate"))))
                    if search_backfill:
                        fields.append("search_text=?")
                        values.append(self._search_projection(value))
                    self.connection.execute(f"UPDATE events SET {','.join(fields)} WHERE id=?", [*values, row["id"]])
                if query_backfill:
                    self.set_setting("event_query_indexes_v1", True)
                if search_backfill:
                    self.set_setting("event_search_index_v1", True)
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_calendar_recurring_end ON events(calendar_id,recurring,end,start)")
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS events_calendar_uid ON events(calendar_id,({_EVENT_UID_SQL}))")
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS events_all_day_dates ON events(({_EVENT_END_DATE_SQL}),({_EVENT_START_DATE_SQL})) WHERE ({_EVENT_ALL_DAY_SQL})=1")
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS events_all_day_recurring_dates ON events(recurring,({_EVENT_END_DATE_SQL}),({_EVENT_START_DATE_SQL})) WHERE ({_EVENT_ALL_DAY_SQL})=1")
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS events_floating_recurring_times ON events(recurring,({_EVENT_END_WALL_SQL}),({_EVENT_START_WALL_SQL})) WHERE ({_EVENT_FLOATING_SQL})=1")
            self.connection.execute("CREATE INDEX IF NOT EXISTS sync_metadata_event ON sync_metadata(event_id)")
            self.connection.execute(f"CREATE INDEX IF NOT EXISTS sync_metadata_calendar_uid ON sync_metadata(calendar_id,({_SYNC_UID_SQL}))")
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_groups ON events(calendar_id,group_id,start,end,id) WHERE group_id IS NOT NULL")
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_candidates ON events(calendar_id,group_id) WHERE candidate=1")
            self.connection.execute("CREATE INDEX IF NOT EXISTS outbox_calendar ON outbox(calendar_id,id)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS event_revisions (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
            self.connection.execute("INSERT OR IGNORE INTO event_revisions(id,version) VALUES (1,0)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS event_query_changes (event_id TEXT PRIMARY KEY, version INTEGER NOT NULL)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS event_query_changes_version ON event_query_changes(version)")
            # These counters live in the same transaction as each event write.
            # Unlike data_version/total_changes they ignore settings, visibility,
            # outbox acknowledgments and unrelated ingestion metadata.
            for action in ("INSERT", "UPDATE", "DELETE"):
                condition = ""
                if action == "UPDATE":
                    condition = " WHEN " + " OR ".join(
                        f"old.{name} IS NOT new.{name}"
                        for name in ("id", "calendar_id", "start", "end", "data", "recurring", "group_id", "candidate")
                    )
                trigger = f'event_revision_{action.lower()}'
                existing = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()
                if existing is None or 'event_query_changes' not in existing[0]:
                    self.connection.execute(f'DROP TRIGGER IF EXISTS {trigger}')
                    identities = ('old.id', 'new.id') if action == 'UPDATE' else (
                        'old.id' if action == 'DELETE' else 'new.id',)
                    changes = ''.join(
                        f'INSERT INTO event_query_changes(event_id,version) VALUES ({identity},(SELECT version FROM event_revisions WHERE id=1)) '
                        'ON CONFLICT(event_id) DO UPDATE SET version=excluded.version;'
                        for identity in identities)
                    self.connection.execute(
                        f"CREATE TRIGGER {trigger} AFTER {action} ON events"
                        f"{condition} BEGIN UPDATE event_revisions SET version=version+1 WHERE id=1; {changes} END"
                    )
            if not self.connection.execute("SELECT 1 FROM calendars LIMIT 1").fetchone():
                self._write_calendar(Calendar("local", "Personal", "#4f7865"))
        self._ui_state.import_legacy(self.connection, seed=legacy_profile)
        # Derived preparations are durable. Ordinary restarts read the compact
        # representation and bulk-build only the in-memory search trees.
        from .query_projection import ProjectionStorage
        with self._transaction():
            storage = ProjectionStorage(self.connection)
            missing = storage.initialize()
            self._projection_storage = storage
            if missing:
                self._maintain_query_projections(identities=missing)
        if query_index:
            from .event_range_index import EventRangeIndex
            self._query_index = EventRangeIndex()
            self._synchronize_query_index()

    def _enable_wal(self) -> None:
        # SQLite can return SQLITE_BUSY immediately for journal-mode changes,
        # even with busy_timeout, when two connections initialize a new file.
        deadline = time.monotonic() + 5
        while True:
            try:
                if self.connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    self.connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                    self.connection.close()
                    raise
                time.sleep(0.01)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            nested = self.connection.in_transaction
            self._transaction_sequence += 1
            savepoint = f"debacal_{self._transaction_sequence}"
            frame = {'revision': self._query_revision, 'before': {}}
            self._query_transactions.append(frame)
            try:
                if not nested and not self._ui_access:
                    self._write_gate.wait_background()
                self.connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
                yield
                if nested:
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._maintain_query_projections()
                    if self._query_index is not None:
                        self._synchronize_query_index()
                    self.connection.commit()
            except BaseException:
                if nested:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.connection.rollback()
                self._query_transactions.pop()
                if self._query_index is not None:
                    for identity, record in frame['before'].items():
                        self._query_index.restore(identity, record)
                    self._query_revision = frame['revision']
                    self._synchronize_query_index()
                if self._projection_storage is not None:
                    self._projection_storage.reader = None
                    self._projection_storage.shared.clear()
                raise
            else:
                self._query_transactions.pop()

    def _maintain_query_projections(self, identities=None):
        if self._projection_storage is None:
            return
        rejected = self._projection_storage.maintain(self.events_revision(), identities=identities)
        for calendar, identity, raw, reason in rejected:
            resource = hashlib.sha256(identity.encode()).hexdigest()[:12]
            self.set_quarantined_resource(calendar, resource, raw, reason)

    @contextmanager
    def ui_transaction(self):
        """Group local UI choices without acquiring the event writer lock."""
        with self._lock:
            with self._ui_state.transaction():
                yield

    @contextmanager
    def read_snapshot(self):
        """Read a consistent WAL snapshot without claiming the writer slot."""
        with self._lock:
            own = not self.connection.in_transaction
            if own:
                self.connection.execute('BEGIN')
            try:
                yield
            finally:
                if own:
                    self.connection.rollback()

    def configure_ui_access(self, maintenance_callback, mutation_callback=None):
        """Make foreground reads fail-fast and delegate derived-data repairs.

        Ordinary Store callers retain synchronous transaction semantics. The
        application attaches this after startup and uses a separate command
        writer for mutations; foreground queries never wait for that writer.
        """
        with self._lock:
            self._maintenance_callback = maintenance_callback
            self._ui_mutation_callback = mutation_callback
            self._ui_access = True
            self.connection.execute('PRAGMA busy_timeout=0')

    def maintain_queries(self, identities=None):
        """Run durable preparation/repair on a background command connection."""
        with self._transaction():
            self._maintain_query_projections(identities=identities)

    def close(self) -> None:
        commands = getattr(self, '_standalone_ui_commands', None)
        if commands is not None:
            commands.shutdown()
            self._standalone_ui_commands = None
        with self._lock:
            self.connection.close()
            if self._ui_state is not None:
                self._ui_state.close()
            self._query_index = None
            if self._projection_storage is not None:
                self._projection_storage.reader = None
                self._projection_storage.shared.clear()
            self._query_transactions.clear()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _write_calendar(self, calendar: Calendar) -> None:
        self.connection.execute(
            "INSERT INTO calendars(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (calendar.id, self._json(calendar.to_dict())),
        )

    def list_calendars(self, *, include_disabled: bool = False) -> list[Calendar]:
        with self._lock:
            calendars = self._calendars_by_creation_order()
            if not include_disabled:
                calendars = [calendar for calendar in calendars if calendar.enabled]
            saved = self.get_setting('calendar_order')
            if not isinstance(saved, list):
                return calendars
            by_id = {calendar.id: calendar for calendar in calendars}
            ordered = []
            for identity in saved:
                if isinstance(identity, str) and identity in by_id:
                    ordered.append(by_id.pop(identity))
            # Newly discovered calendars append in their established order;
            # deleted IDs and duplicates never leave holes or duplicate rows.
            return ordered + [calendar for calendar in calendars if calendar.id in by_id]

    def _calendars_by_creation_order(self) -> list[Calendar]:
        calendars = [Calendar.from_dict(json.loads(row[0])) for row in self.connection.execute(
            "SELECT data FROM calendars ORDER BY rowid"
        )]
        overrides = self._ui_state.visibility_overrides() if self._ui_state is not None else {}
        return [replace(calendar, visible=overrides[calendar.id]) if calendar.id in overrides
                and calendar.visible != overrides[calendar.id] else calendar for calendar in calendars]

    def set_calendar_order(self, identifiers: Iterable[str]) -> None:
        """Save this profile's display order without touching calendar sources."""
        if isinstance(identifiers, (str, bytes)):
            raise ValueError('Calendar order must be a sequence of calendar IDs.')
        requested = list(identifiers)
        if any(not isinstance(identity, str) for identity in requested) or len(set(requested)) != len(requested):
            raise ValueError('Calendar order must contain unique calendar IDs.')
        with self.ui_transaction():
            calendars = self.list_calendars(include_disabled=True)
            available = {calendar.id for calendar in calendars}
            if any(identity not in available for identity in requested):
                raise ValueError('Calendar not found.')
            requested_ids = set(requested)
            remaining = [calendar.id for calendar in calendars if calendar.id not in requested_ids]
            self.set_setting('calendar_order', requested + remaining)

    def get_calendar(self, calendar_id: str) -> Calendar | None:
        with self._lock:
            row = self.connection.execute("SELECT data FROM calendars WHERE id=?", (calendar_id,)).fetchone()
            if row is None:
                return None
            calendar = Calendar.from_dict(json.loads(row[0]))
            visible = self._ui_state.visibility(calendar_id) if self._ui_state is not None else None
            return replace(calendar, visible=visible) if visible is not None and visible != calendar.visible else calendar

    def save_calendar(self, calendar: Calendar) -> None:
        with self._transaction():
            previous = self.get_calendar(calendar.id)
            if previous and (previous.kind, previous.url, previous.username) != (calendar.kind, calendar.url, calendar.username):
                # Deleted events can still exist in history or the outbox. Reusing
                # an ID for another source could later send those events to the
                # wrong server when undoing or retrying a pending operation.
                raise ValueError("Create a new calendar to change an existing calendar's source.")
            self._write_calendar(calendar)

    def set_calendar_visible(self, calendar_id: str, visible: bool) -> None:
        with self._lock:
            calendar = self.get_calendar(calendar_id)
            if calendar is None:
                raise ValueError("Calendar not found.")
            self._ui_state.set_visible(calendar_id, visible)

    def apply_calendar_preferences(self, originals: dict[str, Calendar], calendars: Iterable[Calendar]) -> bool:
        """Apply source drafts atomically, rebasing onto the latest sync metadata.

        Disabled subscriptions retain their events, history, and pending writes.
        Only settings edited in this dialog replace concurrent calendar values.
        """
        drafts = list(calendars)
        if len({calendar.id for calendar in drafts}) != len(drafts):
            raise ValueError('Calendar source settings contain duplicate calendars.')
        changed = False
        with self._transaction():
            for draft in drafts:
                current = self.get_calendar(draft.id)
                original = originals.get(draft.id)
                if current is None:
                    if original is not None:
                        raise ValueError('A calendar changed while Preferences was open. Reopen Calendar sources.')
                    self.save_calendar(draft)
                    changed = True
                    continue
                if original is None:
                    raise ValueError('A calendar was added while Preferences was open. Reopen Calendar sources.')
                if (current.kind, current.url, current.username) != (draft.kind, draft.url, draft.username):
                    raise ValueError('Create a new source to change an existing calendar account.')
                values = {}
                for name in ('enabled', 'use_server_color', 'name_alias'):
                    if getattr(draft, name) != getattr(original, name):
                        values[name] = getattr(draft, name)
                for name in ('server_color', 'server_name', 'read_only'):
                    if (getattr(draft, name) != getattr(original, name)
                            and getattr(current, name) == getattr(original, name)):
                        values[name] = getattr(draft, name)
                server_name = values.get('server_name', current.server_name)
                alias = values.get('name_alias', current.name_alias)
                if draft.name != original.name or 'name_alias' in values or 'server_name' in values:
                    values['name'] = alias or (server_name if draft.kind == 'caldav' and server_name else draft.name)
                if not draft.use_server_color and (draft.color != original.color or draft.use_server_color != original.use_server_color):
                    values['color'] = draft.color
                updated = replace(current, **values)
                if updated != current:
                    self.save_calendar(updated)
                    changed = True
        return changed

    def get_default_calendar(self) -> Calendar | None:
        """Return the chosen writable calendar, with a stable local fallback."""
        with self._lock:
            # A display reorder must not change an implicit creation target.
            writable = [calendar for calendar in self._calendars_by_creation_order()
                        if calendar.enabled and calendar.kind != 'ics' and not calendar.read_only]
            selected = self.get_setting('default_calendar_id')
            configured = next((calendar for calendar in writable if calendar.id == selected), None)
            return configured or next((calendar for calendar in writable if calendar.kind == 'local'), None) or next(iter(writable), None)

    def set_default_calendar(self, calendar_id: str) -> None:
        with self.ui_transaction():
            calendar = self.get_calendar(calendar_id)
            if calendar is None:
                raise ValueError('Default calendar not found.')
            if not calendar.enabled or calendar.kind == 'ics' or calendar.read_only:
                raise ValueError('Choose a writable default calendar.')
            self.set_setting('default_calendar_id', calendar_id)

    def set_quarantined_resource(self, calendar_id: str, resource_id: str,
                                 raw_ics: str | bytes, reason: str) -> None:
        """Retain a rejected resource without replacing the last good event.

        Text and bytes are kept exactly as supplied so even malformed UTF-8 can
        be recovered. Quarantine is ingestion metadata, outside event history.
        """
        if not resource_id:
            raise ValueError('A quarantined resource needs an identifier.')
        if not isinstance(raw_ics, (str, bytes)):
            raise ValueError('A quarantined resource must contain calendar text or bytes.')
        from .resource_diagnostics import resource_diagnostics
        diagnostics = self._json(resource_diagnostics(raw_ics))
        with self._transaction():
            if self.get_calendar(calendar_id) is None:
                raise ValueError('Calendar not found.')
            self.connection.execute(
                """INSERT INTO quarantined_resources(calendar_id,resource_id,raw_ics,reason,updated_at,diagnostics)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(calendar_id,resource_id) DO UPDATE SET
                   raw_ics=excluded.raw_ics,reason=excluded.reason,updated_at=excluded.updated_at,diagnostics=excluded.diagnostics""",
                (calendar_id, resource_id, raw_ics, reason, datetime.now(timezone.utc).isoformat(), diagnostics),
            )

    def list_quarantined_resources(self, calendar_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query, params = 'SELECT * FROM quarantined_resources', ()
            if calendar_id is not None:
                query, params = query + ' WHERE calendar_id=?', (calendar_id,)
            return [dict(row) for row in self.connection.execute(query + ' ORDER BY calendar_id,resource_id', params)]

    def quarantined_resource_summaries(self) -> list[dict[str, Any]]:
        """Small persisted metadata for UI lists; never fetch raw calendar blobs."""
        with self._lock:
            result = []
            for row in self.connection.execute(
                    'SELECT calendar_id,resource_id,reason,updated_at,diagnostics FROM quarantined_resources ORDER BY calendar_id,resource_id'):
                item = dict(row)
                try:
                    item['diagnostics'] = json.loads(item['diagnostics'])
                except (ValueError, TypeError):
                    item['diagnostics'] = {}
                if not isinstance(item['diagnostics'], dict):
                    item['diagnostics'] = {}
                result.append(item)
            return result

    def get_quarantined_resource(self, calendar_id: str, resource_id: str) -> dict[str, Any] | None:
        """Read one original resource after an explicit view/export action."""
        with self._lock:
            row = self.connection.execute('SELECT * FROM quarantined_resources WHERE calendar_id=? AND resource_id=?',
                                          (calendar_id, resource_id)).fetchone()
            return dict(row) if row is not None else None

    def delete_quarantined_resource(self, calendar_id: str, resource_id: str) -> None:
        with self._transaction():
            self.connection.execute('DELETE FROM quarantined_resources WHERE calendar_id=? AND resource_id=?',
                                    (calendar_id, resource_id))

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            row = self.connection.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
            return Event.from_dict(json.loads(row[0])) if row else None

    def get_events(self, event_ids: Iterable[str]) -> list[Event]:
        """Fetch only requested resources; missing IDs are omitted."""
        keys = list(dict.fromkeys(event_ids))
        result = []
        with self._lock:
            for offset in range(0, len(keys), 500):
                chunk = keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                result.extend(Event.from_dict(json.loads(row[0])) for row in self.connection.execute(
                    f"SELECT data FROM events WHERE id IN ({placeholders})", chunk,
                ))
        return result

    def get_events_by_uid(self, calendar_id: str, uids: Iterable[str]) -> list[Event]:
        """Find matching resource owners without decoding a whole calendar.

        The expression index also covers older rows whose effective UID is the
        event ID. Its guarded JSON expression allows unrelated malformed rows
        to remain isolated rather than blocking index creation or lookups.
        """
        keys = list(dict.fromkeys(uids))
        result = []
        with self._lock:
            for offset in range(0, len(keys), 500):
                chunk = keys[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows = self.connection.execute(
                    f"SELECT data FROM events WHERE calendar_id=? AND ({_EVENT_UID_SQL}) IN ({placeholders})",
                    [calendar_id, *chunk])
                for row in rows:
                    try:
                        result.append(Event.from_dict(json.loads(row[0])))
                    except (ValueError, TypeError, KeyError):
                        continue
        return result

    def used_calendar_uids(self, calendar_id: str) -> set[str]:
        """Project reserved UIDs without loading events or sync templates.

        Deleted remote resources retain ownership until their deletion syncs.
        Event rows use the indexed effective UID, including the model's ID
        fallback for older missing/empty UID fields.
        """
        with self._lock:
            result = {row[0] for row in self.connection.execute(
                f'SELECT ({_EVENT_UID_SQL}) FROM events INDEXED BY events_calendar_uid WHERE calendar_id=?',
                (calendar_id,))}
            result.update(row[0] for row in self.connection.execute(
                "SELECT CASE WHEN json_valid(data) THEN json_extract(data,'$.base_payload.event.ical_uid') END "
                "FROM sync_metadata WHERE calendar_id=?", (calendar_id,)) if row[0])
            return result

    def import_report(self, report, *, calendar_id: str | None = None):
        """Reconcile reviewed imports and write them under one ownership lock.

        Background sync uses another SQLite connection. UID ownership and
        local-ID collision checks must share the write transaction, otherwise
        a resource arriving between the check and upsert could be overwritten.
        Quarantined resources and accepted edits commit or roll back together.
        """
        from .ics_resources import reject_duplicate_identities
        from .import_preparation import reidentify_imports

        destinations = {event.calendar_id for event in report.events}
        if calendar_id is not None and destinations - {calendar_id}:
            raise ValueError('The imported events do not belong to the chosen calendar.')
        if calendar_id is None:
            if len(destinations) > 1:
                raise ValueError('Choose one destination calendar for the import.')
            calendar_id = next(iter(destinations), None)
        if report.rejected and calendar_id is None:
            raise ValueError('Choose a calendar for the rejected import resources.')

        with self._transaction():
            owners = self.get_events_by_uid(calendar_id, (event.ical_uid for event in report.events)) if calendar_id else []
            accepted = reidentify_imports(report, existing_events=owners)
            accepted = reject_duplicate_identities(accepted,
                existing_events=self.get_events(event.id for event in accepted.events))
            for resource in accepted.rejected:
                self.set_quarantined_resource(calendar_id, resource.resource_id, resource.raw_ics, resource.reason)
            self.apply(f'Import {len(accepted.events)} event(s)', accepted.events)
            # A reviewed reimport can repair one previously rejected UID without
            # claiming that every other retained resource has been corrected.
            rejected_keys = {resource.resource_id for resource in accepted.rejected}
            retained_keys = {row[0] for row in self.connection.execute(
                'SELECT resource_id FROM quarantined_resources WHERE calendar_id=?', (calendar_id,))} if accepted.events else set()
            repairable_keys = retained_keys - rejected_keys
            if repairable_keys:
                for event in accepted.events:
                    resource_id = hashlib.sha256(event.ical_uid.encode()).hexdigest()[:12]
                    if resource_id in repairable_keys:
                        self.delete_quarantined_resource(event.calendar_id, resource_id)
        return accepted

    def refresh_subscription(self, report, calendar_id: str):
        """Apply a complete feed inventory without surrendering existing owners.

        The last ownership check, removal of vanished resources, quarantine,
        and writes share one SQLite transaction across all Store connections.
        An invalid resource protects its previously cached UID from deletion.
        """
        from .ics_resources import ParseReport, RejectedResource, ResourceIssue, reject_duplicate_identities
        from .import_preparation import reidentify_imports

        if any(event.calendar_id != calendar_id for event in report.events):
            raise EventOwnershipConflict('Subscription resources must belong to their calendar.')
        with self._transaction():
            previous = self.list_events(calendar_ids=[calendar_id])
            original_ids = {event.id for event in report.events}
            accepted = reidentify_imports(report, existing_events=previous)
            accepted = reject_duplicate_identities(accepted,
                existing_events=self.get_events(event.id for event in accepted.events))
            events, issues, rejected = [], list(accepted.issues), list(accepted.rejected)
            for event in accepted.events:
                try:
                    self._assert_identity_owner(event.id, calendar_id, event.ical_uid)
                except EventOwnershipConflict as error:
                    resource_id = hashlib.sha256(event.ical_uid.encode()).hexdigest()[:12]
                    issues.append(ResourceIssue(resource_id, str(error)))
                    rejected.append(RejectedResource(event.ical_uid, event.raw_ics, resource_id, str(error)))
                else:
                    events.append(event)
            accepted = ParseReport(tuple(events), tuple(issues), tuple(rejected))
            ids = {event.id for event in events}
            rejected_uids = {resource.uid for resource in rejected}
            rejected_ids = original_ids - ids
            protected = {event.id for event in previous
                         if event.ical_uid in rejected_uids or event.id in rejected_ids}
            delete_ids = {event.id for event in previous} - ids - protected if '' not in rejected_uids else set()
            self.apply_remote(events, delete_ids, calendar_id=calendar_id,
                              expected_uids={event.id: event.ical_uid for event in previous})
            for resource in rejected:
                self.set_quarantined_resource(calendar_id, resource.resource_id, resource.raw_ics, resource.reason)
            rejected_keys = {resource.resource_id for resource in rejected}
            for old in self.list_quarantined_resources(calendar_id):
                if old['resource_id'] not in rejected_keys:
                    self.delete_quarantined_resource(calendar_id, old['resource_id'])
        return accepted

    @staticmethod
    def _search_projection(value: Event | dict[str, Any]) -> str:
        if isinstance(value, Event):
            fields = (value.title, value.description, value.location)
            overrides, excluded = value.recurrence_overrides, value.excluded_dates
        else:
            fields = tuple(str(value.get(field, "") or "") for field in ("title", "description", "location"))
            overrides, excluded = value.get("recurrence_overrides", {}), value.get("excluded_dates", ())
        segments = [" ".join(fields).casefold()]
        excluded = set(excluded or ())
        if not isinstance(overrides, dict):
            overrides = {}
        segments.extend(" ".join(str(payload.get(field, "") or "") for field in
                                 ("title", "description", "location")).casefold()
                        for key, payload in overrides.items() if key not in excluded and isinstance(payload, dict))
        # The separator avoids normal text matches crossing from a master to
        # an exception. This is a candidate index; callers still exactly match
        # each loaded master/override before showing a result.
        return "\0".join(segments)

    def event_search_index(self) -> list[tuple[str, str, str]]:
        """Read lightweight literal-match candidates without decoding Events."""
        with self._lock:
            return [tuple(row) for row in self.connection.execute(
                "SELECT id,calendar_id,search_text FROM events ORDER BY start,end,id"
            )]

    def count_events(self, calendar_id: str) -> int:
        """Count cached masters without parsing potentially quarantined rows."""
        with self._lock:
            return self.connection.execute('SELECT COUNT(*) FROM events WHERE calendar_id=?', (calendar_id,)).fetchone()[0]

    def revision(self) -> tuple[int, int]:
        """A connection-local token covering both local and external writes."""
        with self._lock:
            return self.connection.execute("PRAGMA data_version").fetchone()[0], self.connection.total_changes

    def events_revision(self) -> int:
        """A persistent event-only revision, shared across Store connections."""
        with self._lock:
            return self.connection.execute("SELECT version FROM event_revisions WHERE id=1").fetchone()[0]

    def _synchronize_query_index(self) -> None:
        """Load durable preparations and maintain only changed source entries."""
        with self._lock:
            if not self._query_enabled:
                return
            from .query_projection import PreparedStateError, load_record, unpack_state
            storage = self._projection_storage
            if self._ui_access:
                if storage.version() != self.events_revision():
                    if self._maintenance_callback is not None:
                        self._maintenance_callback(None)
                    return
            elif self.connection.in_transaction:
                self._maintain_query_projections()
            elif storage.version() != self.events_revision():
                # Also repair writes from an older app or a direct SQL client.
                # Normal sync writers already maintain these rows at commit.
                with self._transaction():
                    pass
            def remember(identity):
                for frame in self._query_transactions:
                    frame['before'].setdefault(identity, self._query_index.sources.get(identity))
            own_transaction = not self.connection.in_transaction
            if own_transaction:
                self.connection.execute('BEGIN')
            invalid, records, removed = set(), [], []
            try:
                revision = self.events_revision()
                if revision == self._query_revision:
                    return
                reader = storage.load_catalog()
                full = self._query_revision < 0 or revision < self._query_revision
                if full:
                    rows = self.connection.execute('SELECT event_id AS id,data FROM event_query_projection')
                else:
                    rows = self.connection.execute(
                        'SELECT changes.event_id AS id,projection.data FROM event_query_changes AS changes '
                        'LEFT JOIN event_query_projection AS projection ON projection.event_id=changes.event_id '
                        'WHERE changes.version>?', (self._query_revision,))
                for row in rows:
                    identity = row['id']
                    remember(identity)
                    if row['data'] is None:
                        if not full:
                            if self._ui_access:
                                removed.append(identity)
                            else:
                                self._query_index.remove(identity)
                        continue
                    try:
                        record = load_record(unpack_state(row['data']), reader)
                        if record.master.id != identity:
                            raise PreparedStateError('A prepared source has an inconsistent identity.')
                    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                        invalid.add(identity)
                        continue
                    if full or self._ui_access:
                        records.append(record)
                    else:
                        self._query_index.restore(identity, record)
                if full and not invalid:
                    from .event_range_index import EventRangeIndex
                    index = EventRangeIndex()
                    for identity in self._query_index.sources:
                        remember(identity)
                    index.bulk_load(records)
                    self._query_index = index
                elif self._ui_access and not invalid:
                    # Publish the complete delta only after it is validated.
                    # A broken derived row must leave the previous snapshot
                    # coherent while a background connection repairs it.
                    for identity in removed:
                        self._query_index.remove(identity)
                    for record in records:
                        self._query_index.restore(record.master.id, record)
                if not invalid:
                    self._query_revision = revision
            except BaseException:
                # A partially prepared update must never masquerade as the
                # previous complete revision after a transaction rolls back.
                self._query_revision = -1
                raise
            finally:
                if own_transaction:
                    self.connection.rollback()
            if invalid:
                if self._ui_access:
                    if self._maintenance_callback is not None:
                        self._maintenance_callback(invalid)
                    return
                # A damaged derived row is rebuilt from its canonical source;
                # data corruption cannot silently hide an event or poison all
                # other calendars. This is unrelated to viewed date windows.
                with self._transaction():
                    self._maintain_query_projections(identities=invalid)
                    storage.reader = None
                    self._query_revision = -1
                self._synchronize_query_index()

    def query_source_events(self, event_ids: Iterable[str]) -> list[Event]:
        """Borrow prepared sources for internal read-only search/presentation.

        Event is frozen, but its nested transport dictionaries must also be
        treated as read-only. Editing uses replacements and durable snapshots.
        """
        with self._lock:
            if not self._query_enabled:
                return self.get_events(event_ids)
            self._synchronize_query_index()
            return [self._query_index.sources[identity].master for identity in event_ids
                    if identity in self._query_index.sources]

    def event_ids(self) -> set[str]:
        """Return stored resource identities without decoding event payloads."""
        with self._lock:
            return {row[0] for row in self.connection.execute("SELECT id FROM events")}

    def list_events(
        self, start: datetime | None = None, end: datetime | None = None,
        calendar_ids: Iterable[str] | None = None,
    ) -> list[Event]:
        clauses, params = [], []
        if start is not None or end is not None:
            clause, params = self._range_clause(start, end)
            clauses.append(clause)
        if calendar_ids is not None:
            ids = list(calendar_ids)
            if not ids:
                return []
            clauses.append("calendar_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        query = "SELECT data FROM events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY start,end,id"
        with self._lock:
            return [Event.from_dict(json.loads(row[0])) for row in self.connection.execute(query, params)]

    @classmethod
    def _range_clause(cls, start, end, *, all_day=None):
        """SQL candidate overlap: instants for timed events, dates for all-day."""
        timed, floating, timed_params, date_params = [], [], [], []
        if start is not None:
            cls._validate_datetime(start)
            timed.append('end>=? AND (end>? OR end=start)')
            timed_params.extend((start.timestamp(), start.timestamp()))
            floating.append(f'({_EVENT_END_DATE_SQL})>?')
            date_params.append(start.date().isoformat())
        if end is not None:
            cls._validate_datetime(end)
            timed.append('start<?')
            timed_params.append(end.timestamp())
            # A query ending partway through a date includes that date;
            # midnight is the exclusive boundary of the previous date.
            operator = '<=' if any((end.hour, end.minute, end.second, end.microsecond)) else '<'
            floating.append(f'({_EVENT_START_DATE_SQL}){operator}?')
            date_params.append(end.date().isoformat())
        if start is not None and end is not None and end.timestamp() <= start.timestamp():
            timed.append('0')
        wall, wall_params = [], []
        from .timeutil import local_zone
        if start is not None:
            wall_start = start.astimezone(local_zone()).replace(tzinfo=None).isoformat()
            wall.append(f'({_EVENT_END_WALL_SQL})>=? AND (({_EVENT_END_WALL_SQL})>? OR ({_EVENT_END_WALL_SQL})=({_EVENT_START_WALL_SQL}))')
            wall_params.extend((wall_start, wall_start))
        if end is not None:
            wall.append(f'({_EVENT_START_WALL_SQL})<?')
            wall_params.append(end.astimezone(local_zone()).replace(tzinfo=None).isoformat())
        timed_clause = f'(({_EVENT_ALL_DAY_SQL})=0 AND ({_EVENT_FLOATING_SQL})=0 AND {" AND ".join(timed)})'
        date_clause = f'(({_EVENT_ALL_DAY_SQL})=1 AND {" AND ".join(floating)})'
        wall_clause = f'(({_EVENT_FLOATING_SQL})=1 AND {" AND ".join(wall)})'
        if all_day == 'floating':
            return wall_clause, wall_params
        if all_day is not None:
            return (date_clause, date_params) if all_day else (timed_clause, timed_params)
        return f'({timed_clause} OR {date_clause} OR {wall_clause})', timed_params + date_params + wall_params

    @staticmethod
    def _floating_timestamp(value: str, zone) -> float:
        return datetime.fromisoformat(value[:10]).replace(tzinfo=zone).timestamp()

    @classmethod
    def _occurrence_order(cls, event, zone):
        if event.all_day:
            return (cls._floating_timestamp(event.start.date().isoformat(), zone),
                    cls._floating_timestamp(event.end.date().isoformat(), zone), event.id)
        return event.start.timestamp(), event.end.timestamp(), event.id

    def list_group_events(self, calendar_id: str, group_id: str) -> list[Event]:
        """Load a legacy group without decoding unrelated calendar resources."""
        if not group_id:
            return []
        with self._lock:
            return [Event.from_dict(json.loads(row[0])) for row in self.connection.execute(
                "SELECT data FROM events WHERE calendar_id=? AND group_id=? ORDER BY start,end,id",
                (calendar_id, group_id),
            )]

    def query_occurrences(self, start: datetime, end: datetime, calendar_ids: Iterable[str] | None = None,
                          *, issues: list | None = None) -> list[Event]:
        """Project a view; optionally isolate and report over-budget sources.

        Without ``issues`` a projection limit raises as before. With a list,
        append ``(master, error)`` for each omitted source while keeping all
        healthy output. A source is never silently or partially truncated.
        """
        if not self._query_enabled:
            raise RuntimeError('This Store was opened without a calendar query index.')
        self._validate_datetime(start)
        self._validate_datetime(end)
        if end.timestamp() <= start.timestamp() and end.date() <= start.date():
            raise ValueError("Occurrence range end must follow its start.")
        ids = list(calendar_ids) if calendar_ids is not None else None
        if ids == []:
            return []
        with self._lock:
            self._synchronize_query_index()
            events, failures = self._query_index.query(start, end, ids, issues=issues)
        self._quarantine_expansion_failures(failures)
        return events

    def _occurrence_sources(self, start: datetime, end: datetime, ids: list[str] | None,
                            ordinary_limit: int | None = None) -> tuple[list[Event], list[Event]]:
        """Select relevant source identities from the maintained interval trees."""
        with self._lock:
            self._synchronize_query_index()
            events, records = self._query_index.select(start, end, ids, ordinary_limit=ordinary_limit)
            masters = {record.master.id: record.master for record in records}
            ordinary = []
            for item in events:
                if item.series_id:
                    masters[item.series_id] = self._query_index.sources[item.series_id].master
                else:
                    ordinary.append(item)
            return ordinary, list(masters.values())

    def _quarantine_expansion_failures(self, failures) -> None:
        if self._ui_access and failures:
            callback = getattr(self, '_ui_mutation_callback', None)
            if callback is not None:
                captured = tuple((master, str(error)) for master, error in failures)
                def record_failures(writer):
                    changed = False
                    for expected, message in captured:
                        source = writer.get_event(expected.id)
                        if source is None or writer._semantic_event(source.to_dict()) != writer._semantic_event(expected.to_dict()):
                            continue
                        resource_id = hashlib.sha256((source.ical_uid or source.id).encode()).hexdigest()[:12]
                        previous = writer.get_quarantined_resource(source.calendar_id, resource_id)
                        if previous is not None and previous['reason'] == message and previous['raw_ics'] == source.raw_ics:
                            continue
                        writer.set_quarantined_resource(source.calendar_id, resource_id, source.raw_ics or '', message)
                        changed = True
                    return changed
                callback(record_failures, ('projection-errors', tuple((master.id, str(error)) for master, error in failures)))
            return
        for master, error in failures:
            resource_id = hashlib.sha256((master.ical_uid or master.id).encode()).hexdigest()[:12]
            source = self.get_event(master.id) if master._query_projection else master
            self.set_quarantined_resource(master.calendar_id, resource_id,
                                          source.raw_ics if source is not None else '', str(error))

    def upcoming_occurrences(self, start: datetime, end: datetime, calendar_ids: Iterable[str] | None = None,
                             *, limit: int = 10, issues: list | None = None) -> list[Event]:
        """Return only the earliest overlapping events, without expanding a full view."""
        if not self._query_enabled:
            raise RuntimeError('This Store was opened without a calendar query index.')
        self._validate_datetime(start)
        self._validate_datetime(end)
        if end.timestamp() <= start.timestamp() and end.date() <= start.date():
            raise ValueError("Occurrence range end must follow its start.")
        if type(limit) is not int or limit < 0:
            raise ValueError("The upcoming event limit must be a nonnegative integer.")
        ids = list(calendar_ids) if calendar_ids is not None else None
        if not limit or ids == []:
            return []
        with self._lock:
            self._synchronize_query_index()
            result, failures = self._query_index.query(start, end, ids, limit=limit, issues=issues)
        self._quarantine_expansion_failures(failures)
        return result

    def get_occurrence(self, event_id: str, *, canonical: bool = False) -> Event | None:
        """Resolve one identity, optionally reviewing its canonical source.

        Presentation can retain an older coherent index while maintenance is
        pending. Mutation guards must not mistake that snapshot for the latest
        canonical event; their fallback prepares only the named resource.
        """
        from .recurrence import split_occurrence_id
        parts = split_occurrence_id(event_id)
        if parts is None:
            if canonical or not self._query_enabled:
                return self.get_event(event_id)
            values = self.query_source_events([event_id])
            return values[0] if values else None
        master_id, key = parts
        with self._lock:
            if self._query_enabled and not canonical:
                self._synchronize_query_index()
                record = self._query_index.sources.get(master_id)
            else:
                # Writer-only stores never construct a complete UI index. An
                # explicit semantic edit may still review one named resource.
                from .event_range_index import EventRangeIndex
                master = self.get_event(master_id)
                record = EventRangeIndex.prepare(master) if master is not None else None
            if record is None or record.error is not None:
                return None
            item = record.occurrences.get(event_id)
            if item is not None:
                from .event_range_index import _local_floating
                return _local_floating(item)
            if record.prepared is None:
                return None  # A fully indexed finite schedule has no other IDs.
            master = record.master
            if not master.recurring or key in record.prepared.excluded or key in record.prepared.override_keys:
                return None
            try:
                # Selection refresh uses the same prepared seek plan. It must
                # not reparse a large master merely to confirm membership.
                from .timeutil import local_zone
                return record.prepared.occurrence_at(
                    key, floating_zone=local_zone() if master.floating_time else None)
            except (ValueError, TypeError, OverflowError):
                return None

    def _reviewed_master(self, reference: Event) -> Event:
        master = self.get_event(reference.series_id or reference.id)
        current = self.get_occurrence(reference.id)
        if self._ui_access and self._query_revision != self.events_revision():
            current = self.get_occurrence(reference.id, canonical=True)
        def content(event):
            if reference.series_id:
                # A sibling edit can refresh the master's derived authoring
                # annotation without changing this dated occurrence. Resource
                # snapshots still compare it for whole-series stale guards.
                event = replace(event, schedule_expression="", expression_anchor="", schedule_syntax=1)
            return self._semantic_event(event.to_dict())
        if master is None or current is None or content(current) != content(reference):
            raise HistoryConflict("This event changed while it was being edited. Open it again before saving.")
        return master

    def edit_occurrence(self, instance: Event, edited: Event, label: str = "Edit occurrence") -> int | None:
        from .recurrence import occurrence_id
        if edited.calendar_id != instance.calendar_id:
            return self.edit_selection([instance], "single", instance, edited, label=label)
        with self._transaction():
            master = self._reviewed_master(instance)
            if not instance.series_id:
                if instance._query_projection and not edited.raw_ics:
                    edited = replace(edited, raw_ics=master.raw_ics, _query_projection=False)
                return self.apply(label, [replace(edited, id=master.id, calendar_id=master.calendar_id)])
            override = replace(
                edited, id=occurrence_id(master.id, instance.recurrence_id),
                calendar_id=master.calendar_id, series_id=master.id, recurrence_id=instance.recurrence_id,
                recurrence_rule="", recurrence_dates=(), excluded_dates=(), recurrence_overrides={}, raw_ics="",
                schedule_expression="", expression_anchor="", schedule_syntax=1,
            )
            overrides = master.recurrence_overrides | {instance.recurrence_id: override.to_dict()}
            return self.apply(label, [replace(master, recurrence_overrides=overrides)])

    def delete_occurrence(self, instance: Event, label: str = "Delete occurrence") -> int | None:
        with self._transaction():
            master = self._reviewed_master(instance)
            if not instance.series_id:
                return self.apply(label, delete_ids=[master.id])
            overrides = {key: value for key, value in master.recurrence_overrides.items() if key != instance.recurrence_id}
            excluded = tuple(sorted(set(master.excluded_dates) | {instance.recurrence_id}))
            return self.apply(label, [replace(master, excluded_dates=excluded, recurrence_overrides=overrides)])

    def confirm_candidate(self, instance: Event, label: str = "Confirm candidate") -> int | None:
        """Keep the chosen slot and remove candidate alternatives atomically.

        A candidate series becomes one ordinary event at the chosen occurrence's
        actual time. Its resource ID and iCalendar UID remain stable. Confirmed
        siblings and choices in other calendars are never removed.
        """
        with self._transaction():
            master = self._reviewed_master(instance)
            if not instance.candidate:
                raise ValueError("Select an alternative time slot to confirm.")
            if master.recurring and not instance.series_id:
                raise ValueError("Select a dated occurrence to confirm this alternative series.")
            if master.recurring and not master.candidate:
                # A lone tentative exception in a confirmed series must not
                # destroy its other, already confirmed occurrences.
                confirmed = replace(instance, candidate=False, schedule_expression="", expression_anchor="", schedule_syntax=1)
                overrides = master.recurrence_overrides | {instance.recurrence_id: confirmed.to_dict()}
                chosen = replace(master, recurrence_overrides=overrides)
            else:
                chosen = replace(
                    instance, id=master.id, calendar_id=master.calendar_id,
                    candidate=False, series_id="", recurrence_id="", recurrence_rule="",
                    recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                    ical_uid=master.ical_uid, raw_ics=master.raw_ics, group_id=master.group_id,
                    read_only=master.read_only,
                    schedule_expression="", expression_anchor="", schedule_syntax=1,
                )
            alternatives = []
            if master.group_id:
                alternatives = [row[0] for row in self.connection.execute(
                    "SELECT id FROM events WHERE calendar_id=? AND group_id=? AND candidate=1 AND id<>?",
                    (master.calendar_id, master.group_id, master.id),
                )]
            return self.apply(label, [chosen], alternatives)

    @staticmethod
    def _preserve_mouse_exceptions(master: Event, reference: Event, edited: Event, projected: Event,
                                   *, authoritative_keys=()) -> Event:
        """Move exception snapshots without applying dialog common fields.

        Dragging an unusually long exception moves the regular one-hour series
        without making every occurrence longer. Resizing explicitly applies
        the new duration to the regular event and all detached exceptions.
        Explicit occurrence ranges from a dialog keep their submitted ends.
        """
        from .recurrence import event_zone, key_datetime, recurrence_key, relative_reanchor_map
        zone = event_zone(master)
        wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
        shift = wall(edited.start) - wall(reference.start)
        resized = not same_duration(reference, edited)

        base = master
        if (reference.series_id and projected.schedule_expression != master.schedule_expression
                and wall(projected.start) != wall(master.start) + shift):
            from .series_operations import partition_series
            _, tail = partition_series(master, reference)
            if tail is not None:
                base = tail
        key_map = (relative_reanchor_map(base, projected.start)
                   if projected.schedule_syntax == base.schedule_syntax == 2
                   and projected.all_day == base.all_day
                   and projected.schedule_expression == base.schedule_expression else None)
        duration_source = edited if resized else master
        overrides = dict(projected.recurrence_overrides)
        for key, payload in master.recurrence_overrides.items():
            moved_key = (key_map[key] if key_map is not None and key in key_map else
                         recurrence_key((wall(key_datetime(key, master)) + shift).replace(tzinfo=zone), projected.all_day,
                                        floating_time=projected.floating_time))
            if moved_key not in overrides:
                continue  # A following edit leaves the earlier exceptions alone.
            original = Event.from_dict(payload)
            moved = Event.from_dict(overrides[moved_key])
            overrides[moved_key] = replace(
                original, id=moved.id, calendar_id=moved.calendar_id,
                ical_uid=moved.ical_uid, series_id=moved.series_id,
                recurrence_id=moved.recurrence_id, start=moved.start,
                end=(moved.end if moved_key in authoritative_keys else
                     end_for(edited if resized else original, moved.start)),
                duration_spec=(moved.duration_spec if moved_key in authoritative_keys else
                               edited.duration_spec if resized else original.duration_spec),
            ).to_dict()
        return replace(projected, end=end_for(duration_source, projected.start),
                       duration_spec=duration_source.duration_spec, recurrence_overrides=overrides)

    def _review_selection(self, refs, scope, reference=None, expected_masters=()):
        from .selection import affected_visible, selection_roots, validate_scope
        validate_scope(scope)
        refs = list(refs)
        for value in refs:
            self._reviewed_master(value)
        roots = selection_roots(self, refs, scope)
        expected = list(expected_masters.values() if hasattr(expected_masters, "values") else expected_masters)
        if expected:
            snapshots = {value.id: value for value in expected}
            class Snapshot:
                def get_event(self, event_id):
                    return snapshots.get(event_id)
                def list_events(self, calendar_ids=None):
                    return [value for value in snapshots.values() if calendar_ids is None or value.calendar_id in calendar_ids]
            expected_roots = selection_roots(Snapshot(), refs, scope)
            master_ids = lambda values: {value.series_id or value.id for value in values}
            if master_ids(expected_roots) != master_ids(roots):
                raise HistoryConflict("The selected group changed while it was being edited. Select it again before saving.")
            for event_id in master_ids(roots):
                current = self.get_event(event_id)
                previous = snapshots.get(event_id)
                if previous is None or current is None or self._semantic_event(current.to_dict()) != self._semantic_event(previous.to_dict()):
                    raise HistoryConflict("This selection changed while it was being edited. Select it again before saving.")
        if reference is not None:
            self._reviewed_master(reference)
            if reference.id not in affected_visible(self, refs, scope, [reference]):
                raise ValueError("The operation's reference is outside the selected events.")
        return refs, roots

    def delete_selection(self, refs, scope="single", label="Delete selected events", *, expected_masters=()) -> int | None:
        """Delete the exact selected union as one history/outbox transaction."""
        from .series_operations import partition_series
        from .selection import is_whole_root
        with self._transaction():
            _, roots = self._review_selection(refs, scope, expected_masters=expected_masters)
            changed, deleted = {}, set()
            for root in roots:
                master = changed.get(root.series_id or root.id) or self.get_event(root.series_id or root.id)
                root_scope = "group" if is_whole_root(root) else scope
                if root_scope == "single" and root.series_id:
                    overrides = {key: value for key, value in master.recurrence_overrides.items() if key != root.recurrence_id}
                    changed[master.id] = replace(master, excluded_dates=tuple(set(master.excluded_dates) | {root.recurrence_id}),
                                                 recurrence_overrides=overrides)
                elif root_scope == "future" and master.recurring:
                    before, _ = partition_series(master, root)
                    if before is None:
                        deleted.add(master.id)
                    else:
                        changed[master.id] = before
                else:
                    deleted.add(master.id)
            return self.apply(label, [event for key, event in changed.items() if key not in deleted], deleted)

    def _transform_selection(self, roots, scope, reference, edited, *, common_changes=None,
                             schedule_changes=None, replace_schedule=False, calendar_id=None,
                             delete_ids=None):
        """Compile projections in memory; no write occurs until every root passes."""
        from datetime import timedelta
        from .event_clipboard import _moved_times, calendar_day_delta
        from .recurrence import event_zone, occurrence_id, updated_series, _valid_wall_time
        from .series_operations import clone_event, detach_event, partition_series, reidentify_series, updated_future
        from .selection import is_whole_root
        common_changes, schedule_changes = common_changes or {}, schedule_changes or {}
        deleted = delete_ids if delete_ids is not None else set()
        used_uids = set()
        if calendar_id is not None:
            self._assert_writable([{"calendar_id": calendar_id}])
            used_uids = self.used_calendar_uids(calendar_id)

        def transferred(value, *, keep_id=False):
            identifier = value.id if keep_id else str(uuid4())
            uid = value.ical_uid if value.ical_uid not in used_uids else str(uuid4())
            used_uids.add(uid)
            return clone_event(value, new_id=identifier, new_uid=uid,
                               calendar_id=calendar_id, group_id=value.group_id)

        resized = not same_duration(reference, edited)
        changed = {}
        for root in roots:
            root_scope = "group" if is_whole_root(root) else scope
            master = self.get_event(root.series_id or root.id)
            if root._query_projection and not root.series_id:
                root = replace(root, raw_ics=master.raw_ics, _query_projection=False)
            source = root if root_scope == "single" or root_scope == "future" else master
            zone = event_zone(master)
            wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
            shift = (timedelta(days=calendar_day_delta(reference, edited.start))
                     if (source.all_day and common_changes.get('all_day') is not False
                         or reference.all_day and edited.all_day) else
                     wall(edited.start) - wall(reference.start))
            exact_target = (edited.start if source.id == reference.id
                            and not common_changes.get('all_day', source.all_day) else None)
            start, end = _moved_times(source, shift, zone, target=exact_target)
            # A timed duration cannot describe the range of an unchanged
            # all-day companion (or vice versa). An explicit type conversion
            # does apply the new duration to every selected event.
            resize_source = resized and (source.all_day == reference.all_day or 'all_day' in common_changes)
            if resize_source:
                end = add_duration(start, edited.duration_spec) if edited.duration_spec else end_for(edited, start)
            duration_spec = edited.duration_spec if resize_source else source.duration_spec
            if common_changes.get("all_day") is True:
                from datetime import time
                days = calendar_day_delta(reference, edited.start)
                start = datetime.combine(source.start.astimezone(zone).date() + timedelta(days=days), time.min, zone)
                end = start + timedelta(days=max(1, (edited.end.astimezone(zone).date() - edited.start.astimezone(zone).date()).days))
            if not all(_valid_wall_time(value) for value in (start, end)):
                raise ValueError("A selected event falls in a daylight-saving clock change. Choose another time.")
            if root_scope == "single":
                value = replace(root, start=start, end=end, duration_spec=duration_spec, **common_changes)
                if root.series_id:
                    value = replace(value, recurrence_rule="", recurrence_dates=(), excluded_dates=(), recurrence_overrides={},
                                    schedule_expression="", expression_anchor="", schedule_syntax=1, raw_ics="")
                    previous = changed.get(master.id, master)
                    if calendar_id is not None and calendar_id != master.calendar_id:
                        changed[master.id] = replace(previous,
                            excluded_dates=tuple(sorted(set(previous.excluded_dates) | {root.recurrence_id})),
                            recurrence_overrides={key: payload for key, payload in previous.recurrence_overrides.items()
                                                  if key != root.recurrence_id})
                        value = transferred(detach_event(master, value))
                        changed[value.id] = value
                    else:
                        changed[master.id] = replace(previous, recurrence_overrides=previous.recurrence_overrides | {root.recurrence_id: value.to_dict()})
                else:
                    value = replace(value, **schedule_changes)
                    if calendar_id is not None and calendar_id != master.calendar_id:
                        deleted.add(master.id)
                        value = transferred(value)
                    changed[value.id] = value
                continue
            base = master
            if root_scope == "future" and master.recurring:
                _, base = partition_series(master, root)
            submission = replace(base, start=start, end=end, duration_spec=duration_spec,
                                 **(schedule_changes | common_changes))
            if root_scope == "future" and master.recurring:
                before, projected = updated_future(master, root, submission, replace_schedule)
            else:
                before, projected = None, updated_series(master, master, submission, replace_schedule)
            # Start with copy-style exception content preservation. A duration
            # edit then deliberately replaces each affected exception's end.
            if replace_schedule is not ScheduleReplacement.RESET:
                submitted_overrides = schedule_changes.get("recurrence_overrides", {})
                explicit_keys = {key for key, payload in submitted_overrides.items()
                                 if payload != master.recurrence_overrides.get(key)}
                projected = self._preserve_mouse_exceptions(master, source, submission, projected,
                                                            authoritative_keys=explicit_keys)
            if resize_source:
                def resized_end(value):
                    return add_duration(value, edited.duration_spec) if edited.duration_spec else end_for(edited, value)
                projected = replace(projected, end=resized_end(projected.start), duration_spec=edited.duration_spec,
                    recurrence_overrides={
                    key: replace(Event.from_dict(payload), end=resized_end(Event.from_dict(payload).start),
                                 duration_spec=edited.duration_spec).to_dict()
                    for key, payload in projected.recurrence_overrides.items()})
            if common_changes:
                projected = replace(projected, **common_changes, recurrence_overrides={
                    key: replace(Event.from_dict(payload), **common_changes).to_dict()
                    for key, payload in projected.recurrence_overrides.items()})
            if before is not None:
                changed[master.id] = before
                new_id = str(uuid4())
                projected = reidentify_series(projected, new_id, new_id)
            if calendar_id is not None and calendar_id != master.calendar_id:
                if before is None:
                    deleted.add(master.id)
                projected = transferred(projected, keep_id=before is not None)
            changed[projected.id] = projected
        return list(changed.values())

    def move_selection(self, refs, scope, reference, start, end, *, copy=False,
                       calendar_id=None, label="Move selected events", expected_masters=(), preserve_calendars=False):
        """Move one union atomically, or prepare fresh copies without writing."""
        with (self.read_snapshot() if copy else self._transaction()):
            refs, roots = self._review_selection(refs, scope, reference, expected_masters)
            edited = replace(reference, start=start, end=end)
            if copy:
                from .selection import capture_selection
                selection = capture_selection(self, refs, scope, reference=reference)
                calendar = (self.get_calendar(reference.calendar_id) if preserve_calendars else
                            self.get_calendar(calendar_id) if calendar_id else self.get_default_calendar())
                if calendar is None or (not preserve_calendars and (calendar.read_only or calendar.kind == "ics")):
                    raise ValueError("Choose a writable calendar for the copied events.")
                return selection.instantiate("group", start, calendar.id, preserve_calendars=preserve_calendars)
            return self.apply(label, self._transform_selection(roots, scope, reference, edited))

    def edit_selection(self, refs, scope, reference, edited, *, replace_schedule: bool | ScheduleReplacement = False,
                       label="Edit selected events", expected_masters=(), calendar_id=None) -> int | None:
        """Edit an exact selected union, including an optional calendar transfer.

        An unchanged editor calendar retains each selected event's source.
        An explicit calendar_id applies that target even when it matches the
        reference, allowing a mixed-calendar selection to be gathered together.
        """
        from .series_operations import partition_series
        from .recurrence import rules_equivalent
        from .selection import is_whole_root
        with self._transaction():
            _, roots = self._review_selection(refs, scope, reference, expected_masters)
            self._assert_writable(root.to_dict() for root in roots)
            if calendar_id is None and edited.calendar_id != reference.calendar_id:
                calendar_id = edited.calendar_id
            master = self.get_event(reference.series_id or reference.id)
            reference_root = next(root for root in roots if (root.series_id or root.id) == master.id)
            reference_scope = "group" if is_whole_root(reference_root) else scope
            base = master if reference_scope != "single" else reference
            if reference_scope == "future" and master.recurring:
                _, base = partition_series(master, reference)
            common = {name: getattr(edited, name) for name in ("title", "description", "location", "all_day", "candidate")
                      if getattr(edited, name) != getattr(reference, name)}
            names = ("recurrence_rule", "recurrence_dates", "excluded_dates", "recurrence_overrides",
                     "schedule_expression", "expression_anchor", "schedule_syntax", "range_policy")
            schedule = {name: getattr(edited, name) for name in names if getattr(edited, name) != getattr(base, name)}
            if rules_equivalent(edited.recurrence_rule, base.recurrence_rule, base):
                # Splits and ICS round trips may spell default BYDAY/INTERVAL
                # clauses explicitly. A title-only edit must not apply one
                # family's remaining COUNT to every independent selected tail.
                schedule.pop('recurrence_rule', None)
            if scope == "single" and reference.series_id:
                # A dated Single form never configures recurrence. Clearing its
                # inherited annotation must not clear another selected family.
                schedule = {}
            deleted = set()
            projections = self._transform_selection(roots, scope, reference, edited,
                                                    common_changes=common, schedule_changes=schedule,
                                                    replace_schedule=replace_schedule,
                                                    calendar_id=calendar_id, delete_ids=deleted)
            return self.apply(label, projections, deleted)

    def edit_series(
        self, reference: Event, edited: Event, label: str = "Edit series", *,
        replace_schedule: bool | ScheduleReplacement = False, preserve_exceptions: bool = False,
    ) -> int | None:
        """Persist the same pure series projection used by editor previews."""
        from .recurrence import updated_series
        if edited.calendar_id != reference.calendar_id:
            return self.edit_selection([reference], "group", reference, edited, label=label,
                                       replace_schedule=replace_schedule)
        with self._transaction():
            master = self._reviewed_master(reference)
            updated = updated_series(master, reference, edited, replace_schedule)
            if preserve_exceptions and replace_schedule is not ScheduleReplacement.RESET:
                updated = self._preserve_mouse_exceptions(master, reference, edited, updated)
            return self.apply(label, [updated])

    def _reviewed_future_master(self, reference: Event, expected_master: Event | None) -> Event:
        master = self._reviewed_master(reference)
        if expected_master is not None and (
            expected_master.id != master.id
            or self._semantic_event(expected_master.to_dict()) != self._semantic_event(master.to_dict())
        ):
            raise HistoryConflict("This series changed while it was being edited. Open it again before saving.")
        return master

    def future_snapshot(self, reference: Event, *, expected_master: Event | None = None) -> Event:
        """Return the selected occurrence's remaining schedule for a clipboard.

        The snapshot retains its source identity. Copy/paste must assign a new
        event ID and UID before storing it as another resource.
        """
        from .series_operations import partition_series
        with self.read_snapshot():
            master = self._reviewed_future_master(reference, expected_master)
            _, tail = partition_series(master, reference)
            if tail is None:
                raise ValueError("There are no events at or after this occurrence.")
            return tail

    def edit_future(
        self, reference: Event, edited: Event, label: str = "Edit this and following events", *,
        replace_schedule: bool | ScheduleReplacement = False, expected_master: Event | None = None,
        preserve_exceptions: bool = False,
    ) -> int | None:
        """Split and edit a series in one undoable, synchronizable operation.

        The past retains the existing iCalendar resource. A nonempty future
        becomes a new resource, while a split before the first occurrence
        updates the original resource without creating an empty past.
        """
        from .series_operations import reidentify_series, updated_future
        if edited.calendar_id != reference.calendar_id:
            return self.edit_selection([reference], "future", reference, edited, label=label,
                                       replace_schedule=replace_schedule,
                                       expected_masters=[expected_master] if expected_master is not None else ())
        with self._transaction():
            master = self._reviewed_future_master(reference, expected_master)
            before, after = updated_future(master, reference, edited, replace_schedule)
            if after is None:
                raise ValueError("There are no events at or after this occurrence.")
            if preserve_exceptions and replace_schedule is not ScheduleReplacement.RESET:
                after = self._preserve_mouse_exceptions(master, reference, edited, after)
            if before is None:
                after = replace(after, id=master.id, calendar_id=master.calendar_id,
                                ical_uid=master.ical_uid)
                return self.apply(label, [after])
            after_id = str(uuid4())
            after = reidentify_series(after, after_id, after_id)
            return self.apply(label, [before, after])

    def delete_future(
        self, reference: Event, label: str = "Delete this and following events", *,
        expected_master: Event | None = None,
    ) -> int | None:
        """Remove the original recurrence IDs at and after the selection."""
        from .series_operations import partition_series
        with self._transaction():
            master = self._reviewed_future_master(reference, expected_master)
            before, _ = partition_series(master, reference)
            if before is None:
                return self.apply(label, delete_ids=[master.id])
            return self.apply(label, [before])

    @staticmethod
    def _validate_datetime(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Range boundaries must include a timezone.")

    def _write_event(self, event: Event) -> None:
        if event.series_id:
            raise ValueError("Store recurring instances through edit_occurrence; keep the series as one master.")
        if event.recurring:
            from .recurrence import recurrence_set
            recurrence_set(event)
        self._assert_identity_owner(event.id, event.calendar_id, event.ical_uid)
        self._retry_incomplete_projection(event.id)
        self.connection.execute(
            """INSERT INTO events(id,calendar_id,start,end,data,recurring,group_id,candidate,search_text) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET calendar_id=excluded.calendar_id,
               start=excluded.start,end=excluded.end,data=excluded.data,recurring=excluded.recurring,
               group_id=excluded.group_id,candidate=excluded.candidate,search_text=excluded.search_text""",
            (event.id, event.calendar_id, event.start.timestamp(), event.end.timestamp(), self._json(event.to_dict()),
             event.recurring, event.group_id, event.candidate, self._search_projection(event)),
        )

    def _retry_incomplete_projection(self, event_id):
        """Let an explicit reimport retry a formerly unsupported source.

        Canonical bytes can be identical after a compatibility fix. Publish an
        availability revision so all readers receive the repaired preparation,
        without invalidating or recompiling unrelated sources.
        """
        if self._projection_storage is None:
            return
        failed = self.connection.execute('SELECT 1 FROM event_query_projection WHERE event_id=? AND data IS NULL',
                                         (event_id,)).fetchone()
        if failed is not None:
            self.connection.execute('UPDATE event_revisions SET version=version+1 WHERE id=1')
            self.connection.execute('INSERT INTO event_query_changes(event_id,version) '
                                    'VALUES (?,(SELECT version FROM event_revisions WHERE id=1)) '
                                    'ON CONFLICT(event_id) DO UPDATE SET version=excluded.version', (event_id,))

    def _assert_identity_owner(self, event_id: str, calendar_id: str, uid: str | None = None) -> None:
        """Check the global ID and calendar-scoped UID under the write lock.

        Native X-properties are untrusted transport metadata. Tombstones retain
        ownership until their queued deletion and sync state have been cleared.
        Callers must hold a transaction when checking before a mutation.
        """
        owner = self.connection.execute(
            f"SELECT calendar_id,({_EVENT_UID_SQL}) AS uid FROM events WHERE id=?", (event_id,)).fetchone()
        if owner and (owner['calendar_id'] != calendar_id or uid is not None and owner['uid'] != uid):
            raise EventOwnershipConflict('This X-DEBACAL-ID belongs to another calendar resource; the existing event was preserved.')
        pending = self.connection.execute('SELECT calendar_id,payload FROM outbox WHERE event_id=?', (event_id,)).fetchone()
        if pending:
            payload = json.loads(pending['payload']) if pending['payload'] else {}
            pending_uid = payload.get('ical_uid') or payload.get('id')
            if pending['calendar_id'] != calendar_id or uid is not None and pending_uid and pending_uid != uid:
                raise EventOwnershipConflict('This X-DEBACAL-ID belongs to another pending resource; its change was preserved.')
        for row in self.connection.execute('SELECT calendar_id,data FROM sync_metadata WHERE event_id=?', (event_id,)):
            state = json.loads(row['data'])
            original = (state.get('base_payload') or {}).get('event') or {}
            original_uid = original.get('ical_uid') or original.get('id')
            if row['calendar_id'] != calendar_id or uid is not None and original_uid and original_uid != uid:
                raise EventOwnershipConflict('This X-DEBACAL-ID belongs to another synchronized resource; its identity was preserved.')
        if uid is not None:
            other = self.connection.execute(
                f"SELECT id FROM events WHERE calendar_id=? AND ({_EVENT_UID_SQL})=? AND id<>? LIMIT 1",
                (calendar_id, uid, event_id)).fetchone()
            if other:
                raise EventOwnershipConflict('This event UID already belongs to another event in this calendar; the existing event was preserved.')
            reserved = self.connection.execute(
                f"SELECT event_id FROM sync_metadata WHERE calendar_id=? AND ({_SYNC_UID_SQL})=? AND event_id<>? LIMIT 1",
                (calendar_id, uid, event_id)).fetchone()
            if reserved:
                raise EventOwnershipConflict('This event UID belongs to another synchronized resource; its pending state was preserved.')

    def _assert_remote_owners(self, upserts, delete_ids, calendar_id=None, expected_uids=None):
        if calendar_id is not None and any(event.calendar_id != calendar_id for event in upserts):
            raise EventOwnershipConflict('Remote replacements must belong to the synchronized calendar.')
        for event in upserts:
            self._assert_identity_owner(event.id, event.calendar_id, event.ical_uid)
        for event_id in delete_ids:
            if calendar_id is None:
                raise ValueError('Remote deletions require their calendar identity.')
            self._assert_identity_owner(event_id, calendar_id, (expected_uids or {}).get(event_id))

    def _snapshot(self, ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(ids)
        keys = list(result)
        # Stay under SQLite's variable limit, including older SQLite builds.
        for offset in range(0, len(keys), 500):
            chunk = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.connection.execute(f"SELECT id,data FROM events WHERE id IN ({placeholders})", chunk):
                result[row["id"]] = Event.from_dict(json.loads(row["data"])).to_dict()
        return result

    @staticmethod
    def _normalized_snapshot(snapshot):
        return {key: Event.from_dict(value).to_dict() if value is not None else None
                for key, value in snapshot.items()}

    @staticmethod
    def _semantic_event(value):
        if value is None:
            return None
        result = {key: item for key, item in value.items() if key != "raw_ics"}
        if 'duration_spec' not in value and not value.get('all_day'):
            from .event_duration import retained_duration_spec
            if retained_duration_spec(value.get('raw_ics', '')):
                # Only legacy nominal DURATION rows need model decoding.
                # Ordinary event/history comparisons keep their cheap path.
                result['duration_spec'] = Event.from_dict(value).duration_spec
        if not result.get('duration_spec'):
            result.pop('duration_spec', None)
        if not result.get("schedule_expression"):
            result.pop("schedule_syntax", None)
        if result.get("recurrence_overrides"):
            result["recurrence_overrides"] = {key: Store._semantic_event(item)
                                              for key, item in result["recurrence_overrides"].items()}
        if result.get("recurrence_rule"):
            result["recurrence_rule"] = ";".join(sorted(result["recurrence_rule"].upper().removeprefix("RRULE:").split(";")))
        return result

    def _snapshot_matches(self, current, expected):
        return all(self._semantic_event(current[key]) == self._semantic_event(value)
                   for key, value in expected.items())

    def _assert_writable(self, values: Iterable[dict[str, Any] | None]) -> None:
        calendars = {}
        for value in values:
            if value is None:
                continue
            if value.get("read_only", False):
                raise ValueError("This recurring subscription event is read-only in this version.")
            calendar_id = value["calendar_id"]
            if calendar_id not in calendars:
                calendars[calendar_id] = self.get_calendar(calendar_id)
            calendar = calendars[calendar_id]
            if calendar is None:
                raise ValueError("Calendar not found.")
            if not calendar.enabled:
                raise ValueError("This calendar is disabled. Enable it in Calendar sources before editing its events.")
            if calendar.read_only:
                raise ValueError("This calendar is read-only on its server. Copy the event to a writable calendar to edit it.")
            if calendar.kind == "ics":
                raise ValueError("ICS subscriptions are read-only. Copy the event to a local calendar to edit it.")

    def _enqueue(self, event_id: str, previous: dict[str, Any] | None, current: dict[str, Any] | None) -> None:
        if previous and current and previous["calendar_id"] != current["calendar_id"]:
            # A cross-calendar move needs a deletion in one source and an insertion
            # in the other. Use a fresh event ID for the new calendar instead.
            raise ValueError("Moving across calendars requires creating a copy with a new event ID.")
        value = current or previous
        if value is None:
            return
        calendar = self.get_calendar(value["calendar_id"])
        if calendar is None or calendar.kind != "caldav" or calendar.read_only:
            return
        self.connection.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
        self.connection.execute(
            "INSERT INTO outbox(event_id,calendar_id,action,payload,created_at) VALUES (?,?,?,?,?)",
            (event_id, calendar.id, "upsert" if current else "delete",
             self._json(current) if current else None, datetime.now(timezone.utc).isoformat()),
        )

    def _restore(self, target: dict[str, dict[str, Any] | None], *, enqueue: bool = True, preserve_source: bool = False) -> None:
        for event_id, value in target.items():
            old = self.get_event(event_id)
            previous = old.to_dict() if old else None
            if value is None:
                self.connection.execute("DELETE FROM events WHERE id=?", (event_id,))
            else:
                if preserve_source and old is not None:
                    value = value | {"raw_ics": old.raw_ics, "ical_uid": old.ical_uid}
                self._write_event(Event.from_dict(value))
            if enqueue:
                self._enqueue(event_id, previous, value)

    @staticmethod
    def _reconcile_local_authoring(event: Event, previous: dict[str, Any] | None,
                                   current: dict[str, Any]) -> Event:
        """Keep locally authored annotations truthful before history/outbox writes.

        Remote updates deliberately bypass this path.
        Content-only edits preserve its original annotation and formatting.
        A local schedule edit refreshes a stale annotation with a complete
        compact program or an identity-preserving schedule document.
        """
        def authored(value):
            return bool(value and value.get("schedule_syntax") == 2
                        and (value.get("schedule_expression") or value.get("expression_anchor")))

        if event.schedule_syntax != 2 or not (authored(current) or authored(previous)):
            return event

        def schedule(value):
            if value is None:
                return None
            fields = tuple(value.get(name) for name in ("start", "all_day", "timezone_id", "timezone_definitions", "floating_time", "recurrence_rule",
                                                        "recurrence_dates", "excluded_dates", "range_policy", "duration_spec"))
            overrides = {key: (payload.get("start"), payload.get("end"),
                               payload.get("all_day", False), payload.get("timezone_id", ""),
                               payload.get('timezone_definitions', {}), payload.get('floating_time', False), payload.get('duration_spec', ''))
                         for key, payload in value.get("recurrence_overrides", {}).items()}
            return fields, overrides

        if schedule(current) == schedule(previous):
            return event
        from .schedule_authoring import expression_matches, reconstruct_authoring
        if expression_matches(event, event.schedule_expression):
            return event
        view = reconstruct_authoring(event)
        return replace(event, schedule_expression=view.expression,
                       expression_anchor=event.start.date().isoformat())

    def apply(self, label: str, upserts: Iterable[Event] = (), delete_ids: Iterable[str] = ()) -> int | None:
        upserts, delete_ids = list(upserts), list(delete_ids)
        ids = [event.id for event in upserts] + delete_ids
        if len(ids) != len(set(ids)):
            raise ValueError("An event may appear only once in an operation.")
        if not ids:
            return None
        with self._transaction():
            before = self._snapshot(ids)
            after = {event.id: event.to_dict() for event in upserts} | {event_id: None for event_id in delete_ids}
            def preserve_transport(event):
                original = before[event.id]
                if event._query_projection and original is not None and not event.raw_ics:
                    # Display values deliberately omit transport text. Keep
                    # canonical unknown ICS properties when editing them.
                    after[event.id]['raw_ics'] = original.get('raw_ics', '')
                    for key, payload in after[event.id].get('recurrence_overrides', {}).items():
                        prior = original.get('recurrence_overrides', {}).get(key, {})
                        if 'raw_ics' not in payload and 'raw_ics' in prior:
                            payload['raw_ics'] = prior['raw_ics']
            for event in upserts:
                preserve_transport(event)
            self._assert_writable(before.values())
            self._assert_writable(after.values())
            for event in upserts:
                reconciled = self._reconcile_local_authoring(event, before[event.id], after[event.id])
                if reconciled is not event:
                    after[event.id] = reconciled.to_dict()
                    preserve_transport(event)
            # Ignore absent deletions and unchanged updates.
            changed = [key for key in ids if before[key] != after[key]]
            if not changed:
                for event in upserts:
                    self._retry_incomplete_projection(event.id)
                return None
            before, after = ({key: value[key] for key in changed} for value in (before, after))
            self._restore(after)
            cursor = self.connection.execute(
                "INSERT INTO operations(label,created_at,before_data,after_data) VALUES (?,?,?,?)",
                (label, datetime.now(timezone.utc).isoformat(), self._json(before), self._json(after)),
            )
            return int(cursor.lastrowid)

    def _matches(self, snapshot: dict[str, dict[str, Any] | None]) -> bool:
        return self._snapshot_matches(self._snapshot(snapshot), snapshot)

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            # Take one read snapshot so a sync worker cannot change half of the
            # comparisons mid-read. Batch current events instead of issuing a
            # separate SQL query for every occurrence in every history record.
            own_transaction = not self.connection.in_transaction
            if own_transaction:
                self.connection.execute("BEGIN")
            try:
                query = "SELECT * FROM operations ORDER BY id DESC"
                params = ()
                if limit is not None:
                    if limit < 0:
                        raise ValueError("History limit cannot be negative.")
                    query += " LIMIT ?"
                    params = (limit,)
                operations = list(self.connection.execute(query, params))
                expected_states = [self._normalized_snapshot(json.loads(row["after_data"] if row["applied"] else row["before_data"])) for row in operations]
                current = self._snapshot(key for state in expected_states for key in state)
                records = []
                for row, expected in zip(operations, expected_states):
                    applied = bool(row["applied"])
                    allowed = self._snapshot_matches(current, expected)
                    records.append({
                        "id": row["id"], "label": row["label"], "created_at": row["created_at"],
                        "applied": applied, "can_undo": applied and allowed,
                        "can_redo": not applied and allowed,
                    })
                return records
            finally:
                if own_transaction:
                    self.connection.rollback()

    def _toggle(self, operation_id: int | None, *, applied: bool) -> int | None:
        with self._transaction():
            if operation_id is None:
                order = "id DESC" if not applied else "toggled DESC,id DESC"
                row = self.connection.execute(
                    f"SELECT * FROM operations WHERE applied=? ORDER BY {order} LIMIT 1", (not applied,)
                ).fetchone()
            else:
                row = self.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            if bool(row["applied"]) == applied:
                raise ValueError("This operation is already in the requested state.")
            expected = self._normalized_snapshot(json.loads(row["before_data"] if applied else row["after_data"]))
            target = self._normalized_snapshot(json.loads(row["after_data"] if applied else row["before_data"]))
            if not self._matches(expected):
                raise HistoryConflict("These events changed after this operation. Undo the dependent change first.")
            self._assert_writable(expected.values())
            self._assert_writable(target.values())
            self._restore(target, preserve_source=True)
            sequence = self.connection.execute("SELECT COALESCE(MAX(toggled),0)+1 FROM operations").fetchone()[0]
            self.connection.execute("UPDATE operations SET applied=?,toggled=? WHERE id=?", (applied, sequence, row["id"]))
            return int(row["id"])

    def history_availability(self) -> tuple[bool, bool]:
        """Check the two toolbar actions without decoding the whole history."""
        with self._lock:
            undo = self.connection.execute(
                "SELECT id,toggled FROM operations WHERE applied=1 ORDER BY id DESC LIMIT 1").fetchone()
            redo = self.connection.execute(
                "SELECT id,toggled FROM operations WHERE applied=0 ORDER BY toggled DESC,id DESC LIMIT 1").fetchone()
            permissions = tuple(sorted((calendar.id, calendar.kind, calendar.read_only)
                                       for calendar in self.list_calendars()))
            signature = (self.events_revision(), tuple(undo) if undo else None,
                         tuple(redo) if redo else None, permissions)
            cached = getattr(self, '_history_availability_cache', None)
            if cached is not None and cached[0] == signature:
                return cached[1]
            available = []
            for candidate, applied in ((undo, True), (redo, False)):
                allowed = False
                if candidate is not None:
                    row = self.connection.execute("SELECT before_data,after_data FROM operations WHERE id=?",
                                                  (candidate['id'],)).fetchone()
                    before, after = (self._normalized_snapshot(json.loads(row[field]))
                                     for field in ('before_data', 'after_data'))
                    if self._matches(after if applied else before):
                        try:
                            self._assert_writable(before.values())
                            self._assert_writable(after.values())
                        except ValueError:
                            pass
                        else:
                            allowed = True
                available.append(allowed)
            result = tuple(available)
            self._history_availability_cache = signature, result
            return result

    def undo(self, operation_id: int | None = None) -> int | None:
        return self._toggle(operation_id, applied=False)

    def redo(self, operation_id: int | None = None) -> int | None:
        return self._toggle(operation_id, applied=True)

    def pending_changes(self, calendar_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query, params = "SELECT * FROM outbox", ()
            if calendar_id is not None:
                query, params = query + " WHERE calendar_id=?", (calendar_id,)
            return [dict(row) | {"payload": json.loads(row["payload"]) if row["payload"] else None}
                    for row in self.connection.execute(query + " ORDER BY id", params)]

    def count_pending_changes(self, calendar_id: str | None = None) -> int:
        """Count queued resources without decoding their calendar payloads."""
        with self._lock:
            query, params = "SELECT COUNT(*) FROM outbox", ()
            if calendar_id is not None:
                query += " WHERE calendar_id=?"
                params = (calendar_id,)
            return self.connection.execute(query, params).fetchone()[0]

    def _pending_rows(self, ids: Iterable[str]) -> list[sqlite3.Row]:
        """Load only row identities needed for transactional conflict guards."""
        keys = list(set(ids))
        rows = []
        for offset in range(0, len(keys), 500):
            chunk = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(self.connection.execute(
                f"SELECT id,event_id,calendar_id FROM outbox WHERE event_id IN ({placeholders})", chunk,
            ))
        return rows

    def acknowledge_change(self, change_id: int) -> None:
        with self._transaction():
            self.connection.execute("DELETE FROM outbox WHERE id=?", (change_id,))

    def apply_remote(self, upserts: Iterable[Event] = (), delete_ids: Iterable[str] = (), *,
                     calendar_id: str | None = None, expected_uids: dict[str, str] | None = None) -> None:
        upserts, delete_ids = list(upserts), list(delete_ids)
        ids = [event.id for event in upserts] + delete_ids
        if len(ids) != len(set(ids)):
            raise ValueError("An event may appear only once in a remote update.")
        with self._transaction():
            self._assert_remote_owners(upserts, delete_ids, calendar_id, expected_uids)
            if self._pending_rows(ids):
                raise HistoryConflict("Remote changes conflict with pending local edits.")
            self._restore({event.id: event.to_dict() for event in upserts} | {event_id: None for event_id in delete_ids}, enqueue=False)

    def accept_remote(
        self, calendar_id: str, event_id: str, expected: Event | None,
        reviewed_change_ids: Iterable[int], upserts: Iterable[Event] = (),
        delete_ids: Iterable[str] = (),
    ) -> None:
        """Atomically discard reviewed edits and apply a chosen server version.

        Snapshot and outbox checks prevent a conflict dialog from discarding an
        edit made after the dialog opened. A crash commits either both the
        discard and replacement, or neither.
        """
        upserts, delete_ids = list(upserts), list(delete_ids)
        ids = [event.id for event in upserts] + delete_ids
        if len(ids) != len(set(ids)):
            raise ValueError("An event may appear only once in a remote update.")
        if expected is not None and (expected.id != event_id or expected.calendar_id != calendar_id):
            raise ValueError("The reviewed event must belong to this calendar.")
        if any(event.calendar_id != calendar_id for event in upserts):
            raise ValueError("Remote replacements must belong to this calendar.")
        with self._transaction():
            if self.get_event(event_id) != expected:
                raise HistoryConflict("This event changed after the conflict was reviewed. Review it again.")
            self._assert_remote_owners(upserts, delete_ids, calendar_id,
                                       {event_id: expected.ical_uid} if expected else None)
            pending = self._pending_rows([event_id, *ids])
            reviewed = set(reviewed_change_ids)
            current = {change["id"] for change in pending
                       if change["event_id"] == event_id and change["calendar_id"] == calendar_id}
            if current != reviewed:
                raise HistoryConflict("Pending edits changed after the conflict was reviewed. Review it again.")
            if any(change["event_id"] in ids and change["id"] not in reviewed for change in pending):
                raise HistoryConflict("Remote changes conflict with pending local edits.")
            for change_id in reviewed:
                self.connection.execute("DELETE FROM outbox WHERE id=?", (change_id,))
            self._restore({event.id: event.to_dict() for event in upserts} | {key: None for key in delete_ids}, enqueue=False)

    def get_sync_metadata(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT data FROM sync_metadata WHERE calendar_id=? AND event_id=?", (calendar_id, event_id)).fetchone()
            return json.loads(row[0]) if row else None

    def get_sync_state(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        return self.get_sync_metadata(calendar_id, event_id)

    def set_sync_state(self, calendar_id: str, event_id: str, href: str, etag: str | None, base_payload: dict[str, Any] | None = None) -> None:
        self.set_sync_metadata(calendar_id, event_id, {
            "event_id": event_id, "calendar_id": calendar_id,
            "href": href, "etag": etag, "base_payload": base_payload,
        })

    def delete_sync_state(self, calendar_id: str, event_id: str) -> None:
        self.set_sync_metadata(calendar_id, event_id, None)

    def list_sync_state(self, calendar_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row[0]) for row in self.connection.execute(
                "SELECT data FROM sync_metadata WHERE calendar_id=? ORDER BY event_id", (calendar_id,)
            )]

    def set_sync_metadata(self, calendar_id: str, event_id: str, data: dict[str, Any] | None) -> None:
        with self._transaction():
            if data is None:
                self.connection.execute("DELETE FROM sync_metadata WHERE calendar_id=? AND event_id=?", (calendar_id, event_id))
            else:
                source = (data.get('base_payload') or {}).get('event') or {}
                if source.get('id', event_id) != event_id or source.get('calendar_id', calendar_id) != calendar_id:
                    raise EventOwnershipConflict('The sync state does not belong to this calendar resource.')
                self._assert_identity_owner(event_id, calendar_id, source.get('ical_uid') or source.get('id'))
                self.connection.execute(
                    "INSERT INTO sync_metadata(calendar_id,event_id,data) VALUES (?,?,?) ON CONFLICT(calendar_id,event_id) DO UPDATE SET data=excluded.data",
                    (calendar_id, event_id, self._json(data)),
                )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in UI_SETTINGS and self._ui_state is not None:
                present, value = self._ui_state.get(key)
                if present:
                    return value
            row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if key in UI_SETTINGS and self._ui_state is not None:
            with self._lock:
                self._ui_state.set(key, value)
            return
        with self._transaction():
            self.connection.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, self._json(value)))
