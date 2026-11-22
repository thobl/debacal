"""Transactional, worker-owned reminder queue and profile-local inbox."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable
from uuid import uuid4

from . import reminder_ledger as ledger
from .reminder_compilation import DAY_US, AlarmSchedule, DeliveryKey, DueReminder, instant, micros
from .reminder_ledger import InboxEntry
from .reminder_partition import Partition
from .prepared_storage import StateWriter
from .reminder_schedule import BACKFILL, FRONTIER_SQL, RULE, head, initial_jobs, pop, step
from .reminder_sources import SourceChange

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY, calendar TEXT NOT NULL, revision INTEGER NOT NULL,
    generation INTEGER NOT NULL, changed INTEGER NOT NULL, zone TEXT, schedule BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS source_civil ON source(id,zone) WHERE zone IS NOT NULL;
CREATE INDEX IF NOT EXISTS source_calendar ON source(calendar,id);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class ReminderStore:
    def __init__(self, database: str):
        path = ':memory:' if database == ':memory:' else str(Path(database).expanduser().resolve()) + '.reminders.sqlite3'
        self._db = sqlite3.connect(path, timeout=0)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(_SCHEMA + FRONTIER_SQL + ledger.LEDGER_SQL)
        self._partition = Partition(database, self._db)
        self._now = micros(datetime.now(timezone.utc))
        self._page = {}
        self._scan_end = None
        self._generation = 0

    def attach_owner(self, owner, *, now: datetime):
        self._now = micros(now)
        self._partition.attach(owner, self._now)

    def set_context(self, *, now, generation):
        """Supply the worker clock and policy snapshot used to fence callbacks."""
        self._now = micros(now)
        self._generation = generation

    @property
    def cursor(self):
        value = self._partition.get('cursor')
        return tuple(value) if value else None

    @property
    def bootstrap(self):
        """(captured journal high-water, last scanned id), or None after replay begins."""
        return self._partition.get('bootstrap')

    @property
    def partition_id(self):
        return self._partition.get('partition')

    @contextmanager
    def _write(self):
        self._db.execute('BEGIN IMMEDIATE')
        with self._db:
            yield

    def _refresh(self):
        self._partition.check_owner()
        self._partition.refresh(self._now)

    def read_sources(self, method, *args, **kwargs):
        if method not in ('scan', 'changes', 'revision', 'calendar_states', 'source_stamp', 'selected'):
            raise ValueError('Unknown reminder source operation.')
        self._refresh()
        try:
            with self._partition.guarded() as reader:
                result = getattr(reader, method)(*args, **kwargs)
            if method == 'scan':
                self._scan_end = ((self.partition_id, kwargs.get('after_id')) if not result and
                                  not kwargs.get('calendar_id') and kwargs.get('limit', 128) > 0 else None)
            if method in ('scan', 'changes', 'selected'):
                self._page = {id(item): (item, self.partition_id) for item in result}
            return result
        except (sqlite3.DatabaseError, OSError) as error:
            self._partition.latch(error)
            raise

    def rebind_sources(self, zone, *, after_id='', limit=128):
        """Page civil schedules bound to an earlier local timezone."""
        if limit < 0:
            raise ValueError('Reminder batch size must be nonnegative.')
        self._refresh()
        rows = self._db.execute('SELECT id FROM source WHERE zone IS NOT NULL AND zone!=? AND id>? '
                                'ORDER BY id LIMIT ?', (StateWriter().zone(zone), after_id, limit))
        return self.read_sources('selected', tuple(row[0] for row in rows))

    def finish_bootstrap(self):
        self._refresh()
        with self._write():
            if self.bootstrap is not None:
                if self._scan_end != (self.partition_id, self.bootstrap[1]):
                    raise sqlite3.OperationalError('The empty bootstrap page must match the current partition.')
                self._partition.set('cursor', (self.bootstrap[0], ''))
                self._partition.set('bootstrap', None)

    def put_source(self, change: SourceChange, schedule: AlarmSchedule | None, *,
                   cursor: tuple[int, str] | None = None, eligible_since: datetime | None = None,
                   scanned=False) -> None:
        self._refresh()
        floor = max(self._now - DAY_US, self._partition.get('boundary', 0),
                    micros(eligible_since) if eligible_since else 0)
        with self._partition.guarded() as reader:
            generation = 0
            if reader:
                page = self._page.get(id(change))
                if page is None or page[0] is not change or page[1] != self.partition_id:
                    raise sqlite3.OperationalError('The source batch requires the current reminder partition.')
                if schedule:
                    state = reader.calendar_state(schedule.master.calendar_id)
                    if not state or not state[0]:
                        schedule = None
                    else:
                        generation = state[1]
                        if generation > 1:
                            floor = max(floor, micros(state[2]))
                reader.incarnation()
        with self._write():
            self._partition.check_owner()
            if schedule and (schedule.master.id != change.event_id or schedule.revision != change.version):
                raise ValueError('The schedule does not match its source change.')
            previous = self._db.execute('SELECT schedule FROM source WHERE id=?', (change.event_id,)).fetchone()
            if schedule and previous:
                ledger.align(self._db, schedule, AlarmSchedule.loads(previous[0]), self._now)
            self._db.execute('DELETE FROM frontier WHERE source=?', (change.event_id,))
            self._db.execute('DELETE FROM source WHERE id=?', (change.event_id,))
            ledger.refresh_contexts(self._db, change.event_id, schedule, self._now)
            ledger.update_snoozes(self._db, change.event_id, schedule)
            if schedule and any(schedule.components.values()):
                civil = change.event.all_day or change.event.floating_time or any(
                    fields.get('all_day') or fields.get('floating_time')
                    for fields in change.event.recurrence_overrides.values())
                zone = StateWriter().zone(schedule.zone) if civil else None
                self._db.execute('INSERT INTO source VALUES(?,?,?,?,?,?,?)',
                                 (change.event_id, schedule.master.calendar_id, change.version,
                                  generation, self._now, zone, schedule.dumps(runtime=True)))
                for job in initial_jobs(schedule, floor):
                    job.save(self._db)
            if cursor is not None:
                self._partition.set('cursor', cursor)
            if scanned and self.bootstrap is not None:
                self._partition.set('bootstrap', (self.bootstrap[0], change.event_id))

    def _valid_source(self, reader, source, revision, calendar, generation):
        if not reader:
            return True
        state = reader.calendar_state(calendar)
        return bool(state and state[0] and state[1] == generation and reader.source_stamp(source) == revision)

    def next_wakeup(self) -> datetime | None:
        times = [self._db.execute(sql).fetchone()[0] for sql in (
            'SELECT min(wake) FROM frontier', "SELECT min(until) FROM ledger WHERE status='snoozed'")]
        return instant(min(t for t in times if t is not None)) if any(t is not None for t in times) else None

    def claim_due(self, now: datetime, *, limit: int = 128) -> tuple[DueReminder, ...]:
        if limit < 0:
            raise ValueError('Reminder claim limit must be nonnegative.')
        self._now = micros(now)
        self._refresh()
        due_items = []
        try:
            with self._write(), self._partition.guarded() as reader:
                # Bound both accepted deliveries and suppressed/refill progress.
                # A still-due next_wakeup asks the worker for another slice.
                loaded, deferred = {}, set()
                for _ in range(max(128, limit * 4)):
                    if len(due_items) >= limit:
                        break
                    result = self._claim_one(reader, loaded, due_items, deferred)
                    if not result:
                        break
                if reader and due_items:
                    reader.incarnation()
        except (sqlite3.DatabaseError, OSError) as error:
            self._partition.latch(error)
            raise
        return tuple(due_items)

    def _claim_one(self, reader, loaded, items, deferred):
        row = head(self._db, deferred)
        snooze = self._db.execute("SELECT until,key,payload FROM ledger WHERE status='snoozed' "
                                  'ORDER BY until,key LIMIT 1').fetchone()
        if snooze and snooze[0] <= self._now and (not row or snooze[0] <= row[1]):
            due = ledger.decode_due(ledger.inbox_reader(self._db), *snooze[1:])
            source = self._db.execute('SELECT generation FROM source WHERE id=?', (due.key.source_id,)).fetchone()
            okay = source and self._valid_source(reader, due.key.source_id, due.source_revision,
                                                 due.calendar_id, source[0])
            self._db.execute('UPDATE ledger SET status=?,until=NULL,claim=?,unread=0 WHERE key=?',
                             ('pending' if okay else 'dismissed', self._new_claim(), snooze[1]))
            if okay:
                items.append(due)
            return True
        if not row or row[1] > self._now:
            return False
        job = pop(self._db, row)
        if job.source not in loaded:
            revision, calendar, generation, changed, blob = self._db.execute(
                'SELECT revision,calendar,generation,changed,schedule FROM source WHERE id=?', (job.source,)).fetchone()
            if not self._valid_source(reader, job.source, revision, calendar, generation):
                self._db.execute('DELETE FROM frontier WHERE source=?', (job.source,))
                return True
            loaded[job.source] = changed, AlarmSchedule.loads(blob)
        changed, schedule = loaded[job.source]
        due, following = step(job, schedule, floor=self._now - DAY_US)
        for successor in following:
            row_id = successor.save(self._db)
            dense_refill = successor.kind == RULE and len(successor.state) > 4 and successor.state[-1] is not None
            if (successor.kind == BACKFILL or dense_refill) and row_id is not None:
                deferred.add(row_id)
        if due and ledger.claimed(self._db, due, changed, self._new_claim()):
            items.append(due)
        return True

    def _new_claim(self):
        return f'{self.partition_id}/{self._partition.epoch}/{self._generation}/{uuid4().hex}'

    def claim_fence(self, key):
        row = self._db.execute("SELECT claim FROM ledger WHERE key=? AND status='pending'",
                               (ledger.encode_key(key),)).fetchone()
        return row[0] if row else None

    def can_emit(self, due, fence):
        """Final worker check immediately before handing a claim to transport."""
        self._refresh()
        if not fence or fence != self.claim_fence(due.key):
            return False
        if not fence.startswith(f'{self.partition_id}/{self._partition.epoch}/{self._generation}/'):
            return False
        row = self._db.execute('SELECT revision,generation FROM source WHERE id=?', (due.key.source_id,)).fetchone()
        if not row or row[0] != due.source_revision:
            return False
        with self._partition.guarded() as reader:
            return self._valid_source(reader, due.key.source_id, due.source_revision, due.calendar_id, row[1])

    def record_delivery(self, key: DeliveryKey, *, desktop_id: int | None, unread: bool = False,
                        uncertain: bool = False, fence=None) -> None:
        self._partition.check_owner()
        with self._write():
            self._db.execute("UPDATE ledger SET status=?,desktop_id=?,unread=?,claim=NULL WHERE key=? "
                             "AND status='pending' AND (? IS NULL OR claim=?)",
                             ('uncertain' if uncertain else 'delivered', desktop_id, unread,
                              ledger.encode_key(key), fence, fence))

    def dismiss(self, key: DeliveryKey, *, partition=None) -> None:
        with self._write():
            if partition is not None and self.partition_id != partition:
                return
            self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,unread=0,closed=1,claim=NULL "
                             'WHERE key=?', (ledger.encode_key(key),))
            self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,unread=0,closed=1,claim=NULL "
                             "WHERE source=? AND component=? AND alarm=? AND occurrence=? AND status IN ('snoozed','pending') "
                             "AND fingerprint=(SELECT fingerprint FROM ledger WHERE key=?)",
                             (*ledger.group(key), ledger.encode_key(key)))

    def snooze(self, key: DeliveryKey, until: datetime, *, partition=None) -> None:
        with self._write():
            if partition is not None and self.partition_id != partition:
                return
            self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,unread=0,closed=1,claim=NULL "
                             "WHERE source=? AND component=? AND alarm=? AND occurrence=? AND status IN ('snoozed','pending') "
                             "AND fingerprint=(SELECT fingerprint FROM ledger WHERE key=?)",
                             (*ledger.group(key), ledger.encode_key(key)))
            self._db.execute("UPDATE ledger SET status='snoozed',until=?,closed=1,unread=0,claim=NULL WHERE key=?",
                             (micros(until), ledger.encode_key(key)))

    def inbox(self, *, limit: int = 100, offset: int = 0) -> tuple[InboxEntry, ...]:
        if min(limit, offset) < 0:
            raise ValueError('Reminder inbox limits must be nonnegative.')
        rows = self._db.execute('SELECT key,payload,status,unread,until FROM ledger ORDER BY stamp DESC,key '
                                'LIMIT ? OFFSET ?', (limit, offset))
        reader = ledger.inbox_reader(self._db)
        return tuple(InboxEntry(ledger.decode_due(reader, key, payload), status, bool(unread),
                                instant(until) if until is not None else None)
                     for key, payload, status, unread, until in rows)

    def mark_read(self, keys: Iterable[DeliveryKey]) -> None:
        with self._write():
            self._db.executemany('UPDATE ledger SET unread=0 WHERE key=?', ((ledger.encode_key(k),) for k in keys))

    def unread_count(self) -> int:
        return self._db.execute('SELECT count(*) FROM ledger WHERE unread=1').fetchone()[0]

    def close(self) -> None:
        self._partition.close()
        self._db.close()
