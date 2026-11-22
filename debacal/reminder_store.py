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
from .reminder_schedule import BACKFILL, FRONTIER_SQL, READY, RULE, head, initial_jobs, pop, step
from .reminder_sources import SourceChange

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY, calendar TEXT NOT NULL, revision INTEGER NOT NULL,
    generation INTEGER NOT NULL, changed INTEGER NOT NULL, zone TEXT, schedule BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS source_civil ON source(id,zone) WHERE zone IS NOT NULL;
CREATE INDEX IF NOT EXISTS source_calendar ON source(calendar,id);
CREATE TABLE IF NOT EXISTS catchup_group (
    source TEXT, component TEXT, alarm TEXT, occurrence TEXT, fingerprint TEXT, through INTEGER,
    PRIMARY KEY(source,component,alarm,occurrence,fingerprint));
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class ReminderStore:
    def __init__(self, database: str):
        target = str(Path(database).expanduser().resolve())
        path = ':memory:' if database == ':memory:' else target + '.reminders.sqlite3'
        self._db = sqlite3.connect(path, timeout=0)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(_SCHEMA + FRONTIER_SQL + ledger.LEDGER_SQL)
        self._partition = Partition(database, target, self._db)
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
        self._partition.check_target()
        self._db.execute('BEGIN IMMEDIATE')
        with self._db:
            yield
            self._partition.check_target()

    def _refresh(self):
        self._partition.check_owner()
        self._partition.refresh(self._now)

    def _check_partition(self, expected):
        if expected is not None and self.partition_id != expected:
            raise sqlite3.OperationalError('The reminder work page belongs to a previous database incarnation.')

    def read_sources(self, method, *args, partition=None, **kwargs):
        if method not in ('scan', 'changes', 'revision', 'calendar_states', 'calendar_name', 'source_stamp', 'selected'):
            raise ValueError('Unknown reminder source operation.')
        self._refresh()
        self._check_partition(partition)
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

    def rebind_sources(self, zone, *, after_id='', limit=128, partition=None):
        """Page civil schedules bound to an earlier local timezone."""
        if limit < 0:
            raise ValueError('Reminder batch size must be nonnegative.')
        self._refresh()
        rows = self._db.execute('SELECT id FROM source WHERE zone IS NOT NULL AND zone!=? AND id>? '
                                'ORDER BY id LIMIT ?', (StateWriter().zone(zone), after_id, limit))
        return self.read_sources('selected', tuple(row[0] for row in rows), partition=partition)

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
            previous = AlarmSchedule.loads(previous[0]) if previous and schedule else None
            if schedule and previous:
                ledger.align(self._db, schedule, previous, self._now)
            self._db.execute('DELETE FROM frontier WHERE source=?', (change.event_id,))
            self._db.execute('DELETE FROM source WHERE id=?', (change.event_id,))
            ledger.reconcile(self._db, change.event_id, schedule, previous, self._now)
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
        self._partition.check_target()
        times = [self._db.execute(sql).fetchone()[0] for sql in (
            'SELECT min(wake) FROM frontier', "SELECT min(until) FROM ledger WHERE status='snoozed'")]
        return instant(min(t for t in times if t is not None)) if any(t is not None for t in times) else None

    def claim_due(self, now: datetime, *, limit: int = 128) -> tuple[DueReminder, ...]:
        return tuple(due for due, _ in self.claim_due_with_fences(now, limit=limit))

    def claim_due_with_fences(self, now: datetime, *, limit: int = 128) -> tuple[tuple[DueReminder, str], ...]:
        """Return each committed claim with the original fence from its transaction."""
        if limit < 0:
            raise ValueError('Reminder claim limit must be nonnegative.')
        self._now = micros(now)
        self._refresh()
        due_items = []
        try:
            with self._write(), self._partition.guarded() as reader:
                # Bound both accepted deliveries and suppressed/refill progress.
                # A still-due next_wakeup asks the worker for another slice.
                loaded, deferred, refills = {}, {}, {}
                for _ in range(max(128, limit * 4)):
                    if len(due_items) >= limit:
                        break
                    result = self._claim_one(reader, loaded, due_items, deferred, refills)
                    if not result:
                        break
                if reader and due_items:
                    reader.incarnation()
        except (sqlite3.DatabaseError, OSError) as error:
            self._partition.latch(error)
            raise
        return tuple(due_items)

    def _claim_one(self, reader, loaded, items, deferred, refills):
        row = head(self._db, deferred)
        order = (row[1], *row[3:8]) if row else None
        bound = min(deferred.values()) if deferred else None
        snooze = self._db.execute(
            "SELECT until,source,component,alarm,occurrence,rank,key,payload FROM ledger WHERE status='snoozed' "
            'ORDER BY until,source,component,alarm,occurrence,rank LIMIT 1').fetchone()
        if (snooze and snooze[0] <= self._now and (order is None or snooze[:6] <= order)
                and (bound is None or snooze[:6] < bound)):
            return self._claim_snooze(snooze, reader, items)
        if not row or row[1] > self._now:
            return False
        if row[2] == READY and bound is not None and bound <= order:
            # Keep the row intact. Later streams can still advance their work,
            # but this ready key cannot be published ahead of the unknown key.
            deferred[row[0]] = order
            return True
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
        stream = job.source, job.component, job.alarm
        if job.kind == RULE:
            refills[stream] = refills.get(stream, 0) + 1
        for successor in following:
            row_id = successor.save(self._db)
            dense_refill = successor.kind == RULE and len(successor.state) > 4 and successor.state[-1] is not None
            yield_rule = successor.kind == RULE and refills[stream] >= 8
            if (successor.kind == BACKFILL or dense_refill or yield_rule) and row_id is not None:
                deferred[row_id] = (successor.wake, successor.source, successor.component, successor.alarm,
                                    successor.occurrence, successor.rank)
        if due:
            fence = self._new_claim()
            if ledger.claimed(self._db, due, changed, fence):
                items.append((due, fence))
        return True

    def _claim_snooze(self, row, reader, items):
        encoded, payload = row[6:]
        due = ledger.current_due(self._db, encoded, payload)
        source = self._db.execute('SELECT revision,generation FROM source WHERE id=?', (due.key.source_id,)).fetchone()
        context = self._db.execute('SELECT fingerprint FROM delivery_context WHERE source=? AND component=? '
                                   'AND alarm=? AND occurrence=?', ledger.group(due.key)).fetchone()
        valid = (source and source[0] == due.source_revision and context and context[0] == due.anchor_fingerprint
                 and (not due.alarm.acknowledged or due.alarm.acknowledged < due.key.trigger_utc))
        if valid and reader:
            state = reader.calendar_state(due.calendar_id)
            valid = state and state[0] and state[1] == source[1]
            if valid and reader.source_stamp(due.key.source_id) != source[0]:
                # Let journal reconciliation decide whether this anchor survived.
                return False
        if valid and due.key.occurrence_key and not due.key.component_key:
            blob, = self._db.execute('SELECT schedule FROM source WHERE id=?', (due.key.source_id,)).fetchone()
            valid = ledger.verify_snooze(self._db, AlarmSchedule.loads(blob), due.key, encoded)
            if valid is None:
                return False
        fence = self._new_claim() if valid else None
        self._db.execute('UPDATE ledger SET status=?,until=?,claim=?,unread=0 WHERE key=?',
                         ('pending' if valid else 'dismissed', row[0] if valid else None, fence, encoded))
        if valid:
            items.append((due, fence))
        return True

    def _new_claim(self):
        return f'{self.partition_id}/{self._partition.epoch}/{self._generation}/{uuid4().hex}'

    def claim_fence(self, key):
        row = self._db.execute("SELECT claim FROM ledger WHERE key=? AND status='pending'",
                               (ledger.encode_key(key),)).fetchone()
        return row[0] if row else None

    def pending_reminder(self, key, fence):
        """Refresh an unsent claim after descriptive edits, retaining its original fence."""
        self._partition.check_owner()
        encoded = ledger.encode_key(key)
        row = self._db.execute("SELECT payload FROM ledger WHERE key=? AND status='pending' AND claim=?",
                               (encoded, fence)).fetchone()
        return ledger.current_due(self._db, encoded, row[0]) if row else None

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

    def record_delivery(self, key: DeliveryKey, *, desktop_id: int | None, unread: bool | None = False,
                        uncertain: bool = False, fence=None) -> None:
        self._partition.check_owner()
        with self._write():
            self._db.execute("UPDATE ledger SET status=?,desktop_id=?,unread=coalesce(?,unread),until=NULL,claim=NULL WHERE key=? "
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

    def mute(self):
        """A deliberate global disable retires pending sends and cancels local snoozes."""
        with self._write():
            self._partition.check_owner()
            self._db.execute("UPDATE ledger SET status='uncertain',claim=NULL WHERE status='pending'")
            self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,closed=0,unread=0 WHERE status='snoozed'")

    def entry(self, key):
        encoded = ledger.encode_key(key)
        self._partition.check_target()
        row = self._db.execute("SELECT payload,status,unread,until FROM ledger WHERE key=? AND status!='suppressed'",
                               (encoded,)).fetchone()
        if row:
            return InboxEntry(ledger.current_due(self._db, encoded, row[0]), row[1], bool(row[2]),
                              instant(row[3]) if row[1] == 'snoozed' else None)

    def actionable(self, due):
        self._refresh()
        row = self._db.execute('SELECT revision,generation FROM source WHERE id=?', (due.key.source_id,)).fetchone()
        context = self._db.execute('SELECT fingerprint FROM delivery_context WHERE source=? AND component=? '
                                   'AND alarm=? AND occurrence=?', ledger.group(due.key)).fetchone()
        if not row or row[0] != due.source_revision or not context or context[0] != due.anchor_fingerprint:
            return False
        with self._partition.guarded() as reader:
            return self._valid_source(reader, due.key.source_id, row[0], due.calendar_id, row[1])

    def prepare_unsent(self, due, fence, *, add_unread=False):
        """Renew only a caller-held, still-unsent claim after a policy change."""
        prefix = f'{self.partition_id}/{self._partition.epoch}/'
        if not fence or not fence.startswith(prefix) or not self.actionable(due):
            return None
        if fence.startswith(f'{prefix}{self._generation}/'):
            return fence
        with self._write():
            self._partition.check_owner()
            renewed = self._new_claim()
            changed = self._db.execute("UPDATE ledger SET claim=?,unread=max(unread,?) WHERE key=? AND status='pending' AND claim=?",
                                       (renewed, add_unread, ledger.encode_key(due.key), fence))
            return renewed if changed.rowcount else None

    def pending_unread(self, key, fence):
        with self._write():
            self._db.execute("UPDATE ledger SET unread=1 WHERE key=? AND status='pending' AND claim=?",
                             (ledger.encode_key(key), fence))

    def snooze_deadline(self, key, fence):
        row = self._db.execute("SELECT until FROM ledger WHERE key=? AND claim=? AND status='pending'",
                               (ledger.encode_key(key), fence)).fetchone()
        return instant(row[0]) if row and row[0] is not None else None

    def suppress(self, key, fence):
        with self._write():
            # A missed local wake already has a historical inbox entry.
            self._db.execute("UPDATE ledger SET status=CASE WHEN until IS NULL THEN 'suppressed' ELSE 'dismissed' END,"
                             "until=NULL,claim=NULL,unread=0 WHERE key=? AND claim=?", (ledger.encode_key(key), fence))

    def begin_catchup(self, due, now):
        """Only the first overdue rank represents this logical alarm through now."""
        identity = (*ledger.group(due.key), due.anchor_fingerprint)
        with self._write():
            row = self._db.execute('SELECT through FROM catchup_group WHERE source=? AND component=? AND alarm=? '
                                   'AND occurrence=? AND fingerprint=?', identity).fetchone()
            if row and micros(due.key.trigger_utc) <= row[0]:
                return False
            self._db.execute('INSERT OR REPLACE INTO catchup_group VALUES(?,?,?,?,?,?)', (*identity, micros(now)))
            return True

    def controller_state(self):
        return self._partition.get('controller', {})

    def checkpoint(self, state, *, cancel_snoozes=False, calendars=(), partition=None):
        """Commit scheduling policy changes and their unfinished calendar scans together."""
        with self._write():
            self._partition.check_owner()
            self._check_partition(partition)
            cancel = "UPDATE ledger SET status='dismissed',unread=0,closed=0,until=NULL WHERE status='snoozed'"
            if cancel_snoozes:
                self._db.execute(cancel)
            else:
                self._db.executemany(cancel + ' AND source IN (SELECT id FROM source WHERE calendar=?)',
                                     ((cal,) for cal in calendars))
            self._partition.set('controller', state)

    def inbox(self, *, limit: int = 100, offset: int = 0) -> tuple[InboxEntry, ...]:
        self._partition.check_target()
        if min(limit, offset) < 0:
            raise ValueError('Reminder inbox limits must be nonnegative.')
        rows = self._db.execute(
            'SELECT l.key,l.payload,l.status,l.unread,l.until,m.first_revision,m.revision,m.title '
            'FROM ledger l LEFT JOIN source_label m ON m.source=l.source AND m.component=l.component '
            "WHERE l.status!='suppressed' "
            'ORDER BY l.stamp DESC,l.key LIMIT ? OFFSET ?', (limit, offset))
        reader = ledger.inbox_reader(self._db)
        return tuple(InboxEntry(ledger.decode_due(reader, key, payload, label), status, bool(unread),
                                instant(until) if status == 'snoozed' else None)
                     for key, payload, status, unread, until, *label in rows)

    def inbox_page(self, *, limit=100, offset=0):
        """Read the partition and every inbox payload from one WAL snapshot."""
        self._db.execute('BEGIN')
        with self._db:
            token = self.partition_id
            entries = self.inbox(limit=limit, offset=offset)
            self._partition.check_target()
            return token, entries

    def mark_read(self, keys: Iterable[DeliveryKey], *, partition=None) -> None:
        with self._write():
            if partition is not None and self.partition_id != partition:
                return
            self._db.executemany('UPDATE ledger SET unread=0 WHERE key=?', ((ledger.encode_key(k),) for k in keys))

    def unread_count(self) -> int:
        self._partition.check_target()
        return self._db.execute('SELECT count(*) FROM ledger WHERE unread=1').fetchone()[0]

    def close(self) -> None:
        self._partition.close()
        self._db.close()
