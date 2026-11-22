"""Transactional, worker-owned reminder queue and profile-local inbox."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable
from uuid import uuid4

from . import reminder_ledger as ledger
from .reminder_compilation import DAY_US, AlarmSchedule, DeliveryKey, DueReminder, compile_schedule, instant, micros
from .reminder_ledger import InboxEntry
from .reminder_partition import Partition
from .models import copy_event
from .reminder_preferences import CalendarReminderPolicy, read_reminder_policy, scheduling_rule
from .timeutil import local_zone
from .prepared_storage import StateWriter
from .reminder_schedule import BACKFILL, FRONTIER_SQL, READY, RULE, head, initial_jobs, pop, step
from .reminder_activation import ActivationContext
from .reminder_sources import SourceChange, ReminderSources

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY, calendar TEXT NOT NULL, revision INTEGER NOT NULL,
    generation INTEGER NOT NULL, changed INTEGER NOT NULL, zone TEXT, schedule BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS source_civil ON source(id,zone) WHERE zone IS NOT NULL;
CREATE INDEX IF NOT EXISTS source_calendar ON source(calendar,id);
CREATE TABLE IF NOT EXISTS receipt_source (
    id TEXT PRIMARY KEY, calendar TEXT NOT NULL, revision INTEGER NOT NULL, schedule BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS catchup_group (
    source TEXT, component TEXT, alarm TEXT, occurrence TEXT, fingerprint TEXT, through INTEGER,
    PRIMARY KEY(source,component,alarm,occurrence,fingerprint));
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class StaleSourcePage(RuntimeError):
    """A source changed while its historical groups were being prepared."""


@dataclass(frozen=True, slots=True)
class _ReceiptSource:
    calendar: str
    generation: int
    revision: int
    observed: int
    fingerprint: str
    coordinate: str | None
    zone: str | None


def _alarm_intent(schedule):
    return frozenset((component, key, alarm.uid, alarm.action, alarm.related, alarm.trigger,
                      alarm.repeat, alarm.repeat_interval, alarm.acknowledged)
                     for component, alarms in schedule.components.items() for key, alarm in alarms.items())


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
        self._observations = {}
        self._observation_partition = None
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

    def _outdated_zone_ids(self, zone, after_id, limit):
        rows = self._db.execute('SELECT id FROM source WHERE zone IS NOT NULL AND zone!=? AND id>? '
                                'ORDER BY id LIMIT ?', (StateWriter().zone(zone), after_id, limit))
        return tuple(row[0] for row in rows)

    def bootstrap_zone_is_current(self, zone):
        """Check the retained civil projections once when resuming a partial first scan."""
        return not self._outdated_zone_ids(zone, '', 1)

    def rebind_sources(self, zone, *, after_id='', limit=128, partition=None):
        """Page civil schedules bound to an earlier local timezone."""
        if limit < 0:
            raise ValueError('Reminder batch size must be nonnegative.')
        self._refresh()
        return self.read_sources('selected', self._outdated_zone_ids(zone, after_id, limit), partition=partition)

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
        for _ in self.put_source_steps(change, schedule, cursor=cursor, eligible_since=eligible_since, scanned=scanned):
            pass

    def has_receipts(self, source):
        return self._db.execute('SELECT 1 FROM delivery_context WHERE source=? LIMIT 1', (source,)).fetchone() is not None

    def _receipt_timing(self, schedule):
        """Keep geometry and alignment inputs only for components with receipts."""
        source = schedule.master.id
        if not self.has_receipts(source):
            return None
        components = {}
        for component, alarms in schedule.components.items():
            components[component] = {}
            if self._db.execute('SELECT 1 FROM delivery_context WHERE source=? AND component=? LIMIT 1',
                                (source, component)).fetchone():
                components[component] = {key: replace(alarm, description='') for key, alarm in alarms.items()}
        master = copy_event(schedule.master, dict(title='Reminder source'))
        return replace(schedule, master=master, components=components, titles={}, observed_inventory=True).dumps(runtime=True)

    def put_source_steps(self, change, schedule, *, cursor=None, eligible_since=None, scanned=False, muted=False):
        """Yield between history pages. Publication and cursor advancement remain one transaction."""
        self._refresh()
        floor = max(self._partition.get('boundary', 0),
                    micros(eligible_since) if eligible_since else 0)
        with self._partition.guarded() as reader:
            calendar_state = None
            enabled, generation = True, 0
            if reader:
                page = self._page.get(id(change))
                if page is None or page[0] is not change or page[1] != self.partition_id:
                    raise sqlite3.OperationalError('The source batch requires the current reminder partition.')
                if schedule:
                    state = reader.calendar_state(schedule.master.calendar_id)
                    calendar_state = state[:2] if state else None
                    enabled, generation = calendar_state or (False, 0)
                    if enabled and generation > 1:
                        floor = max(floor, micros(state[2]))
                reader.incarnation()
        if schedule and (schedule.master.id != change.event_id or schedule.revision != change.version):
            raise ValueError('The schedule does not match its source change.')
        token = self.partition_id
        if token != self._observation_partition:
            self._observations.clear()
            self._observation_partition = token
        # Only interrupted or in-progress sources retain an entry. A replayed
        # page can publish earlier IDs without changing a later edit's cutoff.
        intent = (change.event.ical_uid if change.event else None, enabled, muted, generation, floor,
                  schedule.timing_key if schedule else None,
                  _alarm_intent(schedule) if schedule else None)
        pending = self._observations.get(change.event_id)
        observed = pending[1] if pending and pending[0] == intent else self._now
        self._observations[change.event_id] = intent, observed
        job_floor = max(observed - DAY_US, floor)
        row = self._db.execute('SELECT schedule,0,generation,changed FROM source WHERE id=? UNION ALL '
                               'SELECT schedule,1,NULL,NULL FROM receipt_source WHERE id=? LIMIT 1',
                               (change.event_id, change.event_id)).fetchone()
        previous = AlarmSchedule.loads(row[0]) if row else None
        # Legacy dormant projections sometimes retained deleted alarms. Their
        # timing and historical UID evidence survive, but they cannot supply
        # a before-list for aligning a newly added UID-less alarm.
        current_inventory = previous is not None and (not row[1] or previous.observed_inventory)
        ambiguous = ledger.align(schedule, previous) if schedule and current_inventory else ()
        stage, zones = yield from ledger.stage_groups(self._db, change.event_id, schedule, previous)
        active = schedule is not None and enabled and not muted and any(schedule.components.values())
        known_since = observed
        if active and row and row[2] == generation and row[3] >= floor and ledger.same_geometry(schedule, previous):
            # Replaying a descriptive revision rebuilds the same known frontier. Keep its
            # observation time without extending the new jobs' lookback or the edit cutoff.
            if stage == 'metadata' or _alarm_intent(schedule) == _alarm_intent(previous):
                known_since = min(known_since, row[3])
        history_blob = (self._receipt_timing(schedule)
                        if schedule and not active and stage != 'empty' else None)
        verify = stage == 'staged' or history_blob is not None
        if verify:
            self._refresh()
            if self.partition_id != token:
                raise StaleSourcePage('The reminder partition changed during source preparation.')
        with self._write():
            self._partition.check_owner()
            if verify:
                with self._partition.guarded() as reader:
                    stamp = change.version if change.event is not None else None
                    state = reader.calendar_state(schedule.master.calendar_id) if reader and schedule else None
                    if reader and (reader.source_stamp(change.event_id) != stamp
                                   or schedule and (state[:2] if state else None) != calendar_state):
                        raise StaleSourcePage('The source changed during reminder preparation.')
            self._db.execute('DELETE FROM frontier WHERE source=?', (change.event_id,))
            self._db.execute('DELETE FROM source WHERE id=?', (change.event_id,))
            if row and row[1] and history_blob is None:
                self._db.execute('DELETE FROM receipt_source WHERE id=?', (change.event_id,))
            ledger.reconcile(self._db, change.event_id, schedule, previous, observed, stage, zones, ambiguous)
            if active:
                civil = change.event.all_day or change.event.floating_time or any(
                    fields.get('all_day') or fields.get('floating_time')
                    for fields in change.event.recurrence_overrides.values())
                zone = StateWriter().zone(schedule.zone) if civil else None
                self._db.execute('INSERT INTO source VALUES(?,?,?,?,?,?,?)',
                                 (change.event_id, schedule.master.calendar_id, change.version,
                                  generation, known_since, zone, schedule.dumps(runtime=True)))
                for job in initial_jobs(schedule, job_floor):
                    job.save(self._db)
            elif history_blob is not None:
                # Ineligibility ends real send and snooze authorizations even
                # when the canonical geometry itself is a metadata-only match.
                self._db.execute("UPDATE ledger SET status='uncertain',claim=NULL WHERE source=? AND status='pending'",
                                 (change.event_id,))
                self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,closed=0,unread=0,claim=NULL "
                                 "WHERE source=? AND status='snoozed'", (change.event_id,))
                self._db.execute('INSERT OR REPLACE INTO receipt_source VALUES(?,?,?,?)',
                                 (change.event_id, schedule.master.calendar_id, change.version, history_blob))
            if cursor is not None:
                self._partition.set('cursor', cursor)
            if scanned and self.bootstrap is not None:
                self._partition.set('bootstrap', (self.bootstrap[0], change.event_id))

        del self._observations[change.event_id]

    def _valid_source(self, reader, source, revision, calendar, generation):
        if not reader:
            return True
        state = reader.calendar_state(calendar)
        return bool(state and state[0] and state[1] == generation and reader.source_stamp(source) == revision)

    def observed_before(self, due, trigger):
        """An indexed source first seen after its trigger is eligible only for catch-up."""
        return self._db.execute('SELECT 1 FROM source WHERE id=? AND revision=? AND changed<?',
                                (due.key.source_id, due.source_revision, micros(trigger))).fetchone() is not None

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

    def _receipt_source(self, key, *, require_live):
        row = self._db.execute(
            'SELECT s.calendar,s.generation,s.revision,s.changed,l.fingerprint,m.coordinate,s.zone FROM ledger l '
            'JOIN source s ON s.id=l.source JOIN delivery_context d USING(source,component,alarm,occurrence) '
            'LEFT JOIN native_movement m USING(source,component,alarm,occurrence) '
            'WHERE l.key=? AND l.fingerprint=d.fingerprint '
            "AND (?=0 OR (m.coordinate IS NOT '' AND NOT EXISTS (SELECT 1 FROM retired_native r WHERE r.key=l.key)))",
            (ledger.encode_key(key), require_live)).fetchone()
        return _ReceiptSource(*row) if row else None

    def _snooze_calendar(self, key, published, *, read_event):
        evidence = self._partition.get('evidence')
        if not evidence:
            return None, None
        with closing(ReminderSources(self._partition.target, evidence=evidence)) as reader:
            if reader.identity_evidence()[0] != tuple(evidence[0]):
                return None, None
            with reader.for_incarnation(evidence[2]):
                return reader.snooze_source(key.source_id, published.calendar, published.generation, read_event=read_event)

    def _policy_unpublished(self, key, calendar, policy):
        # A secondary can observe replacement preferences before the owner
        # reaches this source in its calendar scan.
        state = self.controller_state()
        recorded = state.get('calendars', {}).get(calendar)
        scans = state.get('scans', {})
        pending = calendar in scans and (scans[calendar] is None or key.source_id > scans[calendar])
        return not recorded or recorded[0] != scheduling_rule(policy) or pending

    def _published_schedule(self, source):
        row = self._db.execute('SELECT schedule FROM source WHERE id=? UNION ALL '
                               'SELECT schedule FROM receipt_source WHERE id=? LIMIT 1', (source, source)).fetchone()
        return AlarmSchedule.loads(row[0]) if row else None

    def _matches_alarm(self, key, selected, change, policy, zone):
        previous = self._published_schedule(key.source_id)
        if previous is None or previous.master.ical_uid != change.event.ical_uid:
            return False
        schedule = compile_schedule(change, policy, zone=zone)
        alarm = schedule.components.get(key.component_key, {}).get(key.alarm_key)
        entry = self.entry(key)
        if (not alarm or not entry or alarm.uid != entry.reminder.alarm.uid
                or alarm.acknowledged and alarm.acknowledged >= key.trigger_utc):
            return False
        member = ledger.selected_member(previous, schedule, selected)
        point = schedule.selected_point(member) if member else None
        return bool(point and schedule.fingerprint(point, alarm) == entry.reminder.anchor_fingerprint)

    def snooze(self, key: DeliveryKey, until: datetime, *, partition=None, inbox_policy=None) -> bool:
        """Arm a published alarm. Inbox actions also verify the current calendar and policy."""
        checked = verified_revision = None
        if inbox_policy is not None:
            checked = self._receipt_source(key, require_live=True)
            if checked is None:
                return False
            selected = ledger.mapped_key(key, checked.coordinate)
            eligibility = inbox_policy.snooze_eligibility(checked.calendar)
            enabled, policy, *_ = eligibility
            if not enabled or policy.mode == 'mute' or policy.mode == 'replace' and not policy.alarms:
                return False
            zone = local_zone()
            zone_changed = checked.zone is not None and checked.zone != StateWriter().zone(zone)
            needs_content = selected != key or zone_changed or self._policy_unpublished(key, checked.calendar, policy)
            verified_revision, change = self._snooze_calendar(key, checked, read_event=needs_content)
            if verified_revision is None:
                return False
            if not needs_content and verified_revision != checked.revision:
                # The journal can coalesce an alarm removal and restoration before the owner polls.
                content_revision, change = self._snooze_calendar(key, checked, read_event=True)
                if content_revision != verified_revision:
                    return False
            if change is not None and not self._matches_alarm(key, selected, change, policy, zone):
                return False
        with self._write():
            if self._partition.get('incoherent') or partition is not None and self.partition_id != partition:
                return False
            active = self._receipt_source(key, require_live=inbox_policy is not None)
            if active is None or checked is not None and active != checked:
                return False
            if inbox_policy is not None:
                revision, _ = self._snooze_calendar(key, active, read_event=False)
                if revision != verified_revision:
                    return False
                if active.zone is not None and StateWriter().zone(local_zone()) != StateWriter().zone(zone):
                    return False
                current = read_reminder_policy(self._partition.target)
                if current.snooze_eligibility(active.calendar) != eligibility:
                    return False
            self._db.execute("UPDATE ledger SET status='dismissed',until=NULL,unread=0,closed=1,claim=NULL "
                             "WHERE source=? AND component=? AND alarm=? AND occurrence=? AND status IN ('snoozed','pending') "
                             "AND fingerprint=(SELECT fingerprint FROM ledger WHERE key=?)",
                             (*ledger.group(key), ledger.encode_key(key)))
            self._db.execute("UPDATE ledger SET status='snoozed',until=?,closed=1,unread=0,claim=NULL WHERE key=?",
                             (micros(until), ledger.encode_key(key)))
            return True

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
        source = self._receipt_source(due.key, require_live=True)
        if (not source or source.revision != due.source_revision or source.calendar != due.calendar_id
                or source.fingerprint != due.anchor_fingerprint):
            return False
        with self._partition.guarded() as reader:
            return self._valid_source(reader, due.key.source_id, source.revision, source.calendar, source.generation)

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

    def inbox(self, *, limit: int = 100, offset: int = 0, active_only=False) -> tuple[InboxEntry, ...]:
        self._partition.check_target()
        if min(limit, offset) < 0:
            raise ValueError('Reminder inbox limits must be nonnegative.')
        rows = self._db.execute(
            'SELECT l.key,l.payload,l.status,l.unread,l.until,m.first_revision,m.revision,m.title '
            'FROM ledger l LEFT JOIN source_label m ON m.source=l.source AND m.component=l.component '
            "WHERE l.status!='suppressed' AND (?=0 OR l.status!='dismissed') "
            'ORDER BY l.stamp DESC,l.key LIMIT ? OFFSET ?', (active_only, limit, offset))
        reader = ledger.inbox_reader(self._db)
        return tuple(InboxEntry(ledger.decode_due(reader, key, payload, label), status, bool(unread),
                                instant(until) if status == 'snoozed' else None)
                     for key, payload, status, unread, until, *label in rows)

    def inbox_page(self, *, limit=100, offset=0, active_only=False):
        """Read the partition and every inbox payload from one WAL snapshot."""
        self._db.execute('BEGIN')
        with self._db:
            token = self.partition_id
            entries = self.inbox(limit=limit, offset=offset, active_only=active_only)
            self._partition.check_target()
            return token, entries

    def _open_selection(self, key, partition):
        if partition is None or self.partition_id != partition or self._partition.get('incoherent'):
            return None
        entry = self.entry(key)
        evidence = self._partition.get('evidence')
        if entry is None or not evidence:
            return None
        due = entry.reminder
        movement = self._db.execute(
            'SELECT coordinate,coalesce(s.calendar,h.calendar),coalesce(s.revision,h.revision) '
            'FROM native_movement m LEFT JOIN source s ON s.id=m.source LEFT JOIN receipt_source h ON h.id=m.source '
            'WHERE m.source=? AND component=? AND alarm=? AND occurrence=? AND (s.id IS NOT NULL OR h.id IS NOT NULL)',
            ledger.group(key)).fetchone()
        if self._db.execute('SELECT 1 FROM retired_native WHERE key=?', (ledger.encode_key(key),)).fetchone():
            return None
        if movement:
            coordinate, calendar, revision = movement
            selected = ledger.mapped_key(key, coordinate)
            if selected is None:
                return None
            due = replace(due, key=selected, calendar_id=calendar, source_revision=revision)
        return due, evidence

    def open_entry(self, key, *, partition):
        """Resolve one selected resource outside the writer, then fence its read receipt."""
        selected = self._open_selection(key, partition)
        if not selected:
            return None
        due, evidence = selected
        context = ActivationContext(partition, tuple(evidence[0]), evidence[2])
        with closing(ReminderSources(self._partition.target, evidence=evidence)) as reader:
            if reader.identity_evidence()[0] != context.main_file:
                return None
            with reader.for_incarnation(context.incarnation):
                changes = reader.selected((key.source_id,))
                if not changes or not changes[0].event or changes[0].event.calendar_id != due.calendar_id:
                    return None
                change = changes[0]
                previous = self._published_schedule(key.source_id)
                if not previous or previous.revision != due.source_revision or previous.master.ical_uid != change.event.ical_uid:
                    return None
                schedule = compile_schedule(change, CalendarReminderPolicy(), zone=local_zone())
                member = ledger.selected_member(previous, schedule, due.key)
                if member is None or schedule.selected_point(member) is None:
                    return None
                due = replace(due, key=member, source_revision=change.version, title=schedule.titles[member.component_key])
                with self._write():
                    if self._open_selection(key, partition) != selected:
                        return None
                    state = reader.calendar_state(due.calendar_id)
                    if not state or not state[0] or reader.source_stamp(key.source_id) != change.version:
                        return None
                    self._db.execute('UPDATE ledger SET unread=0 WHERE key=?', (ledger.encode_key(key),))
                    return due, context

    def mark_read(self, keys: Iterable[DeliveryKey], *, partition=None) -> None:
        with self._write():
            if partition is not None and self.partition_id != partition:
                return
            self._db.executemany('UPDATE ledger SET unread=0 WHERE key=?', ((ledger.encode_key(k),) for k in keys))

    def external_revision(self):
        """SQLite changes from other workspace connections, independent of local scheduling writes."""
        self._partition.check_target()
        return self._db.execute('PRAGMA data_version').fetchone()[0]

    def unread_count(self) -> int:
        self._partition.check_target()
        return self._db.execute('SELECT count(*) FROM ledger WHERE unread=1').fetchone()[0]

    def close(self) -> None:
        self._partition.close()
        self._db.close()
