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
from .schedule_replacement import ScheduleReplacement


class HistoryConflict(ValueError):
    """The operation's events have changed since it was performed."""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_sequence = 0
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
                PRIMARY KEY(calendar_id, resource_id)
            );
        """)
        with self._transaction():
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
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_groups ON events(calendar_id,group_id,start,end,id) WHERE group_id IS NOT NULL")
            self.connection.execute("CREATE INDEX IF NOT EXISTS events_candidates ON events(calendar_id,group_id) WHERE candidate=1")
            self.connection.execute("CREATE INDEX IF NOT EXISTS outbox_calendar ON outbox(calendar_id,id)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS event_revisions (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
            self.connection.execute("INSERT OR IGNORE INTO event_revisions(id,version) VALUES (1,0)")
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
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS event_revision_{action.lower()} AFTER {action} ON events"
                    f"{condition} BEGIN UPDATE event_revisions SET version=version+1 WHERE id=1; END"
                )
            if not self.connection.execute("SELECT 1 FROM calendars LIMIT 1").fetchone():
                self._write_calendar(Calendar("local", "Personal", "#4f7865"))

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
            try:
                self.connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
                yield
                if nested:
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.connection.commit()
            except BaseException:
                if nested:
                    self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self.connection.close()

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
        return [Calendar.from_dict(json.loads(row[0])) for row in self.connection.execute(
            "SELECT data FROM calendars ORDER BY rowid"
        )]

    def set_calendar_order(self, identifiers: Iterable[str]) -> None:
        """Save this profile's display order without touching calendar sources."""
        if isinstance(identifiers, (str, bytes)):
            raise ValueError('Calendar order must be a sequence of calendar IDs.')
        requested = list(identifiers)
        if any(not isinstance(identity, str) for identity in requested) or len(set(requested)) != len(requested):
            raise ValueError('Calendar order must contain unique calendar IDs.')
        with self._transaction():
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
            return Calendar.from_dict(json.loads(row[0])) if row else None

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
        with self._transaction():
            calendar = self.get_calendar(calendar_id)
            if calendar is None:
                raise ValueError("Calendar not found.")
            self._write_calendar(replace(calendar, visible=visible))

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
        with self._transaction():
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
        with self._transaction():
            if self.get_calendar(calendar_id) is None:
                raise ValueError('Calendar not found.')
            self.connection.execute(
                """INSERT INTO quarantined_resources(calendar_id,resource_id,raw_ics,reason,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(calendar_id,resource_id) DO UPDATE SET
                   raw_ics=excluded.raw_ics,reason=excluded.reason,updated_at=excluded.updated_at""",
                (calendar_id, resource_id, raw_ics, reason, datetime.now(timezone.utc).isoformat()),
            )

    def list_quarantined_resources(self, calendar_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query, params = 'SELECT * FROM quarantined_resources', ()
            if calendar_id is not None:
                query, params = query + ' WHERE calendar_id=?', (calendar_id,)
            return [dict(row) for row in self.connection.execute(query + ' ORDER BY calendar_id,resource_id', params)]

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

    def list_events(
        self, start: datetime | None = None, end: datetime | None = None,
        calendar_ids: Iterable[str] | None = None,
    ) -> list[Event]:
        clauses, params = [], []
        if start is not None:
            self._validate_datetime(start)
            clauses.append("end >= ? AND (end > ? OR end = start)")
            params.extend((start.timestamp(), start.timestamp()))
        if end is not None:
            self._validate_datetime(end)
            clauses.append("start < ?")
            params.append(end.timestamp())
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

    def list_group_events(self, calendar_id: str, group_id: str) -> list[Event]:
        """Load a legacy group without decoding unrelated calendar resources."""
        if not group_id:
            return []
        with self._lock:
            return [Event.from_dict(json.loads(row[0])) for row in self.connection.execute(
                "SELECT data FROM events WHERE calendar_id=? AND group_id=? ORDER BY start,end,id",
                (calendar_id, group_id),
            )]

    def query_occurrences(self, start: datetime, end: datetime, calendar_ids: Iterable[str] | None = None) -> list[Event]:
        """Query a view without materializing a series into stored events."""
        from .recurrence import expand_events_safe
        self._validate_datetime(start)
        self._validate_datetime(end)
        if end.timestamp() <= start.timestamp():
            raise ValueError("Occurrence range end must follow its start.")
        ids = list(calendar_ids) if calendar_ids is not None else None
        if ids == []:
            return []
        ordinary, masters = self._occurrence_sources(start, end, ids)
        events, failures = expand_events_safe(ordinary + masters, start, end)
        self._quarantine_expansion_failures(failures)
        return events

    def _occurrence_sources(self, start: datetime, end: datetime, ids: list[str] | None,
                            ordinary_limit: int | None = None) -> tuple[list[Event], list[Event]]:
        ordinary_query = "SELECT data FROM events WHERE recurring=0 AND end>=? AND (end>? OR end=start) AND start<?"
        ordinary_params = [start.timestamp(), start.timestamp(), end.timestamp()]
        query, params = "SELECT data FROM events WHERE recurring=1", []
        if ids is not None:
            condition = " AND calendar_id IN (" + ",".join("?" for _ in ids) + ")"
            ordinary_query += condition
            ordinary_params.extend(ids)
            query += condition
            params.extend(ids)
        if ordinary_limit is not None:
            ordinary_query += " ORDER BY start,end,id LIMIT ?"
            ordinary_params.append(ordinary_limit)
        with self._lock:
            own_transaction = not self.connection.in_transaction
            if own_transaction:
                self.connection.execute("BEGIN")
            try:
                ordinary = [Event.from_dict(json.loads(row[0])) for row in self.connection.execute(ordinary_query, ordinary_params)]
                masters = [Event.from_dict(json.loads(row[0])) for row in self.connection.execute(query, params)]
            finally:
                if own_transaction:
                    self.connection.rollback()
        return ordinary, masters

    def _quarantine_expansion_failures(self, failures) -> None:
        for master, error in failures:
            resource_id = hashlib.sha256((master.ical_uid or master.id).encode()).hexdigest()[:12]
            self.set_quarantined_resource(master.calendar_id, resource_id, master.raw_ics or '', str(error))

    def upcoming_occurrences(self, start: datetime, end: datetime, calendar_ids: Iterable[str] | None = None,
                             *, limit: int = 10) -> list[Event]:
        """Return only the earliest overlapping events, without expanding a full view."""
        from .recurrence import RecurrenceError, expand_series
        self._validate_datetime(start)
        self._validate_datetime(end)
        if end.timestamp() <= start.timestamp():
            raise ValueError("Occurrence range end must follow its start.")
        if type(limit) is not int or limit < 0:
            raise ValueError("The upcoming event limit must be a nonnegative integer.")
        ids = list(calendar_ids) if calendar_ids is not None else None
        if not limit or ids == []:
            return []
        result, masters = self._occurrence_sources(start, end, ids, ordinary_limit=limit)
        key = lambda event: (event.start.timestamp(), event.end.timestamp(), event.id)
        failures = []
        for master in masters:
            try:
                upper = end
                if len(result) >= limit:
                    # Later starts cannot enter the current top N. Keep a tiny
                    # exclusive interval for equal-start ties and ongoing events.
                    cutoff = min(end.timestamp(), max(start.timestamp(), result[-1].start.timestamp()) + 0.000001)
                    if cutoff > start.timestamp():
                        upper = datetime.fromtimestamp(cutoff, end.tzinfo)
                result.extend(expand_series(master, start, upper, limit=limit, truncate=True))
                result = sorted(result, key=key)[:limit]
            except (ValueError, TypeError, OverflowError, KeyError):
                failures.append((master, RecurrenceError("Unsupported or invalid recurrence rule.")))
        self._quarantine_expansion_failures(failures)
        return sorted(result, key=key)

    def get_occurrence(self, event_id: str) -> Event | None:
        from .recurrence import split_occurrence_id, key_datetime, recurrence_set, occurrence, recurrence_key
        parts = split_occurrence_id(event_id)
        if parts is None:
            return self.get_event(event_id)
        master_id, key = parts
        with self._lock:
            master = self.get_event(master_id)
            if master is None or not master.recurring or key in master.excluded_dates:
                return None
            try:
                start = key_datetime(key, master)
                if key in master.recurrence_overrides:
                    return occurrence(master, start, key)
                previous = recurrence_set(master, after=start).before(start, inc=True)
                if previous is None or recurrence_key(previous, master.all_day) != key:
                    return None
                return occurrence(master, start, key)
            except ValueError:
                return None

    def _reviewed_master(self, reference: Event) -> Event:
        master = self.get_event(reference.series_id or reference.id)
        current = self.get_occurrence(reference.id)
        if master is None or current is None or self._semantic_event(current.to_dict()) != self._semantic_event(reference.to_dict()):
            raise HistoryConflict("This event changed while it was being edited. Open it again before saving.")
        return master

    def edit_occurrence(self, instance: Event, edited: Event, label: str = "Edit occurrence") -> int | None:
        from .recurrence import occurrence_id
        if edited.calendar_id != instance.calendar_id:
            return self.edit_selection([instance], "single", instance, edited, label=label)
        with self._transaction():
            master = self._reviewed_master(instance)
            if not instance.series_id:
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
        from .recurrence import event_zone, key_datetime, recurrence_key
        zone = event_zone(master)
        wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
        shift = wall(edited.start) - wall(reference.start)
        reference_duration = wall(reference.end) - wall(reference.start)
        edited_duration = wall(edited.end) - wall(edited.start)
        resized = reference_duration != edited_duration
        if not resized and reference_duration.total_seconds() <= 0:
            resized = (edited.end.timestamp() - edited.start.timestamp()
                       != reference.end.timestamp() - reference.start.timestamp())

        def end_for(start, source):
            duration = wall(source.end) - wall(source.start)
            if duration.total_seconds() > 0:
                return (wall(start) + duration).replace(tzinfo=zone)
            return datetime.fromtimestamp(start.timestamp() + source.end.timestamp() - source.start.timestamp(), zone)

        duration_source = edited if resized else master
        overrides = dict(projected.recurrence_overrides)
        for key, payload in master.recurrence_overrides.items():
            moved_key = recurrence_key((wall(key_datetime(key, master)) + shift).replace(tzinfo=zone), projected.all_day)
            if moved_key not in overrides:
                continue  # A following edit leaves the earlier exceptions alone.
            original = Event.from_dict(payload)
            moved = Event.from_dict(overrides[moved_key])
            overrides[moved_key] = replace(
                original, id=moved.id, calendar_id=moved.calendar_id,
                ical_uid=moved.ical_uid, series_id=moved.series_id,
                recurrence_id=moved.recurrence_id, start=moved.start,
                end=(moved.end if moved_key in authoritative_keys else
                     end_for(moved.start, edited if resized else original)),
            ).to_dict()
        return replace(projected, end=end_for(projected.start, duration_source), recurrence_overrides=overrides)

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
        from .event_clipboard import _moved_times
        from .recurrence import event_zone, occurrence_id, updated_series, _valid_wall_time
        from .series_operations import clone_event, detach_event, partition_series, reidentify_series, updated_future
        from .selection import is_whole_root
        common_changes, schedule_changes = common_changes or {}, schedule_changes or {}
        deleted = delete_ids if delete_ids is not None else set()
        used_uids = set()
        if calendar_id is not None:
            self._assert_writable([{"calendar_id": calendar_id}])
            used_uids = {event.ical_uid for event in self.list_events(calendar_ids=[calendar_id])}
            # A remotely stored resource pending deletion still owns its UID.
            # Reusing it could make the sync bridge identify the new resource
            # as the old deletion instead of an independent insertion.
            for state in self.list_sync_state(calendar_id):
                payload = (state.get("base_payload") or {}).get("event") or {}
                if payload.get("ical_uid"):
                    used_uids.add(payload["ical_uid"])

        def transferred(value, *, keep_id=False):
            identifier = value.id if keep_id else str(uuid4())
            uid = value.ical_uid if value.ical_uid not in used_uids else str(uuid4())
            used_uids.add(uid)
            return clone_event(value, new_id=identifier, new_uid=uid,
                               calendar_id=calendar_id, group_id=value.group_id)

        reference_zone = event_zone(reference)
        reference_wall = lambda value: value.astimezone(reference_zone).replace(tzinfo=None)
        old_duration = reference_wall(reference.end) - reference_wall(reference.start)
        new_duration = reference_wall(edited.end) - reference_wall(edited.start)
        resized = old_duration != new_duration
        if old_duration <= timedelta(0) and not resized:
            resized = reference.end.timestamp() - reference.start.timestamp() != edited.end.timestamp() - edited.start.timestamp()
        changed = {}
        for root in roots:
            root_scope = "group" if is_whole_root(root) else scope
            master = self.get_event(root.series_id or root.id)
            source = root if root_scope == "single" or root_scope == "future" else master
            zone = event_zone(master)
            wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
            shift = wall(edited.start) - wall(reference.start)
            start, end = _moved_times(source, shift, zone)
            if resized:
                duration = wall(edited.end) - wall(edited.start)
                end = (start.replace(tzinfo=None) + duration).replace(tzinfo=zone, fold=edited.end.astimezone(zone).fold)
                if duration <= timedelta(0):
                    end = datetime.fromtimestamp(start.timestamp() + edited.end.timestamp() - edited.start.timestamp(), zone)
            if common_changes.get("all_day") is True:
                from datetime import time
                days = (edited.start.astimezone(zone).date() - reference.start.astimezone(zone).date()).days
                start = datetime.combine(source.start.astimezone(zone).date() + timedelta(days=days), time.min, zone)
                end = start + timedelta(days=max(1, (edited.end.astimezone(zone).date() - edited.start.astimezone(zone).date()).days))
            if not all(_valid_wall_time(value) for value in (start, end)):
                raise ValueError("A selected event falls in a daylight-saving clock change. Choose another time.")
            if root_scope == "single":
                value = replace(root, start=start, end=end, **common_changes)
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
            submission = replace(base, start=start, end=end, **(schedule_changes | common_changes))
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
            if resized:
                span = wall(edited.end) - wall(edited.start)
                def resized_end(value):
                    return ((wall(value) + span).replace(tzinfo=zone) if span > timedelta(0) else
                            datetime.fromtimestamp(value.timestamp() + edited.end.timestamp() - edited.start.timestamp(), zone))
                projected = replace(projected, end=resized_end(projected.start), recurrence_overrides={
                    key: replace(Event.from_dict(payload), end=resized_end(Event.from_dict(payload).start)).to_dict()
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
        from .recurrence import event_zone
        with self._transaction():
            refs, roots = self._review_selection(refs, scope, reference, expected_masters)
            edited = replace(reference, start=start, end=end)
            if copy:
                from .selection import capture_selection
                selection = capture_selection(self, refs, scope)
                zone = event_zone(selection.reference)
                wall = lambda value: value.astimezone(zone).replace(tzinfo=None)
                target = (wall(selection.reference.start) + (wall(start) - wall(reference.start))).replace(tzinfo=zone)
                calendar = (self.get_calendar(reference.calendar_id) if preserve_calendars else
                            self.get_calendar(calendar_id) if calendar_id else self.get_default_calendar())
                if calendar is None or (not preserve_calendars and (calendar.read_only or calendar.kind == "ics")):
                    raise ValueError("Choose a writable calendar for the copied events.")
                return selection.instantiate("group", target, calendar.id, preserve_calendars=preserve_calendars)
            return self.apply(label, self._transform_selection(roots, scope, reference, edited))

    def edit_selection(self, refs, scope, reference, edited, *, replace_schedule: bool | ScheduleReplacement = False,
                       label="Edit selected events", expected_masters=(), calendar_id=None) -> int | None:
        """Edit an exact selected union, including an optional calendar transfer.

        An unchanged editor calendar retains each selected event's source.
        An explicit calendar_id applies that target even when it matches the
        reference, allowing a mixed-calendar selection to be gathered together.
        """
        from .series_operations import partition_series
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
                     "schedule_expression", "expression_anchor", "schedule_syntax")
            schedule = {name: getattr(edited, name) for name in names if getattr(edited, name) != getattr(base, name)}
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
        with self._transaction():
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
        self.connection.execute(
            """INSERT INTO events(id,calendar_id,start,end,data,recurring,group_id,candidate,search_text) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET calendar_id=excluded.calendar_id,
               start=excluded.start,end=excluded.end,data=excluded.data,recurring=excluded.recurring,
               group_id=excluded.group_id,candidate=excluded.candidate,search_text=excluded.search_text""",
            (event.id, event.calendar_id, event.start.timestamp(), event.end.timestamp(), self._json(event.to_dict()),
             event.recurring, event.group_id, event.candidate, self._search_projection(event)),
        )

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
            self._assert_writable(before.values())
            self._assert_writable(after.values())
            # Ignore absent deletions and unchanged updates.
            changed = [key for key in ids if before[key] != after[key]]
            if not changed:
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

    def apply_remote(self, upserts: Iterable[Event] = (), delete_ids: Iterable[str] = ()) -> None:
        upserts, delete_ids = list(upserts), list(delete_ids)
        ids = [event.id for event in upserts] + delete_ids
        if len(ids) != len(set(ids)):
            raise ValueError("An event may appear only once in a remote update.")
        with self._transaction():
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
                self.connection.execute(
                    "INSERT INTO sync_metadata(calendar_id,event_id,data) VALUES (?,?,?) ON CONFLICT(calendar_id,event_id) DO UPDATE SET data=excluded.data",
                    (calendar_id, event_id, self._json(data)),
                )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._transaction():
            self.connection.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, self._json(value)))
