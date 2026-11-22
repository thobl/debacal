"""Single-threaded reminder owner, bounded scheduling slices and inbox commands."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
import sqlite3
from threading import Event, Thread
from time import monotonic
from typing import Generator
from uuid import uuid4

from .alarms import Alarm
from .recurrence import RecurrenceRangeError, recurrence_key
from .reminder_compilation import DAY_US, MAX_US, DeliveryKey, DueReminder, instant, micros
from .reminder_rule_seek import seek_slice
from .reminder_owner import ReminderOwner
from .reminder_preferences import CalendarReminderPolicy, ReminderPolicySnapshot, read_reminder_policy
from .reminder_schedule import compile_schedule
from .reminder_store import ReminderStore
from .timeutil import local_zone

PAGE = 12
POLL = 1.0
JITTER = timedelta(seconds=2)
DESKTOP_ISSUE = 'Desktop notifications are unavailable. Check the desktop notification service or use in-app delivery.'
DATA_ERRORS = (ValueError, OverflowError, TypeError, LookupError, AttributeError, RecursionError)


@dataclass
class Qualification:
    """Keep the completed decision while its durable disposition awaits a write lock."""
    due: DueReminder
    fence: str
    now: datetime
    policy: ReminderPolicySnapshot
    group_repeats: bool = True
    result: bool | None = None
    probe: Generator[None, None, bool] | None = None

    def resolve(self, worker):
        if self.result is None and self.probe is None:
            worker.assess(self)
        if self.probe is not None:
            # Only the bounded, in-memory recurrence computation lives in the
            # generator. A busy database operation cannot close its continuation.
            try:
                next(self.probe)
                return None
            except StopIteration as done:
                self.result = done.value
            except DATA_ERRORS:
                self.result = False
            self.probe = None
        if self.group_repeats and self.result:
            self.result = worker.store.begin_catchup(self.due, self.now)
            self.group_repeats = False
        return self.result


def upcoming(schedule, now):
    if any(micros(point.end) > micros(now) for point in schedule.explicit.values()):
        return True
    series = schedule.prepared
    if series is None:
        return False
    lower = series._lower(now.astimezone(schedule.zone), schedule.zone)
    cursor = max(micros(lower) if lower else 0, micros(series.rule._dtstart))
    bound = series.rule_bounds[1]
    upper = micros(bound) if bound else MAX_US
    checkpoint = None
    ranked = False
    while cursor <= upper:
        if series.seek and series.seek.seek(instant(cursor)) is None:
            return False
        stop = min(upper, cursor + 28 * DAY_US)
        if not ranked:
            try:
                value = next(series._regular(instant(cursor), instant(stop)), None)
            except RecurrenceRangeError:
                ranked = True
        if ranked:
            value, checkpoint = seek_slice(series, cursor, stop, checkpoint)
            if checkpoint is not None:
                yield
                continue
        if value is None:
            if not ranked and not series.calendar_periods:
                return False
            cursor = stop + 1
        else:
            master = schedule.master
            key = recurrence_key(value, master.all_day, floating_time=master.floating_time)
            if key not in series.excluded and key not in series.override_keys and key not in series.added_keys:
                point, _ = schedule.point(value, key)
                if micros(point.end) > micros(now):
                    return True
            cursor = micros(value) + 1_000_000
        yield
    return False


class ReminderWorker(Thread):
    def __init__(self, database, clock, transport, allowed, profile, notify, *, previous=None):
        super().__init__(name='reminder-scheduler', daemon=True)
        self.database, self.clock, self.transport = database, clock, transport
        self.allowed, self.profile, self.notify = allowed, profile, notify
        self.previous = previous
        self.commands = Queue()
        self.stopped = Event()
        self.store = self.owner = self.policy = self.partition = None
        self.armed = self.zone = self.issue = self.test = None
        self.journal_target = None
        self.checked_zone = False
        self.armed_revision = -1
        self.last_time = None
        self.native = False
        self.unsent = deque()
        self.qualifying = deque()
        self.review = None
        self.submitted = {}
        self.native_calendars = {}
        self.data_issue = self.desktop_issue = None
        self.count = None
        self.retry_probe = 0

    def post(self, kind, *args):
        if not self.stopped.is_set():
            self.notify(self, kind, args)

    def enqueue(self, kind, *args):
        self.commands.put((kind, args))

    def stop(self):
        self.stopped.set()
        self.enqueue('wake')

    def release(self):
        if self.native:
            self.transport.stop()
        self.native = False
        self.unsent.clear()
        self.qualifying.clear()
        self.review = None
        self.submitted.clear()
        self.native_calendars.clear()
        if self.store:
            self.store.close()
        if self.owner:
            self.owner.release()
        self.store = self.owner = self.partition = self.armed = None
        self.journal_target = None
        self.checked_zone = False

    def run(self):
        if self.previous is not None:
            self.previous.join()
            self.previous = None
        delay = 0
        pending = None
        try:
            while not self.stopped.is_set():
                if pending is None:
                    try:
                        pending = self.commands.get(timeout=delay)
                    except Empty:
                        pass
                if self.stopped.is_set():
                    break
                try:
                    delay = self.tick()
                    if pending:
                        self.command(*pending)
                        pending = None
                    self.report(None, data=True)
                except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                    busy = getattr(error, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    if busy:
                        self.stopped.wait(.05)
                        delay = 0
                        continue
                    self.release()
                    self.report(str(error), data=True)
                    delay = POLL
                    if pending and pending[0] == 'page':
                        self.post('page', pending[1][0], (), None)
                    pending = None
        finally:
            self.release()

    def report(self, issue, *, data=False):
        if data:
            self.data_issue = issue
        else:
            self.desktop_issue = issue
        current = self.data_issue or self.desktop_issue
        if current != self.issue:
            self.issue = current
            self.post('issue', current)

    def count_unread(self):
        count = self.store.unread_count() if self.store else 0
        if count != self.count:
            self.count = count
            self.post('count', count)

    def changed(self):
        self.count_unread()
        self.post('inbox', self.partition)

    def inbox_store(self):
        if not self.store and Path(str(Path(self.database).resolve()) + '.reminders.sqlite3').is_file():
            self.store = ReminderStore(self.database)
            self.partition = self.store.partition_id
        return self.store

    def command(self, kind, args):
        if kind == 'wake':
            return
        if kind == 'preview':
            if self.allowed:
                self.preview(args[0])
            return
        if kind == 'retired':
            self.native_calendars.pop(args[0], None)
            return
        if kind == 'availability':
            if self.native:
                self.post('availability', args[0])
                self.report(None if args[0] else DESKTOP_ISSUE)
            return
        if kind in ('delivered', 'failed'):
            self.result(kind, *args)
            return
        if kind == 'page':
            request, limit, offset = args
            token, entries = self.store.inbox_page(limit=limit, offset=offset) if self.inbox_store() else (None, ())
            if not self.owner:
                self.partition = token
            self.post('page', request, entries, token)
            self.count_unread()
            return
        if not self.inbox_store():
            return
        partition, key, *extra = args
        if partition != self.store.partition_id:
            return
        if kind == 'read':
            self.store.mark_read(key, partition=partition)
        else:
            entry = self.store.entry(key)
            if not entry:
                return
            native_action = kind == 'native'
            if native_action:
                if (key not in self.native_calendars or not self.owner or not self.native
                        or entry.status != 'delivered'
                        or not self.store.actionable(entry.reminder)):
                    self.native_calendars.pop(key, None)
                    return
                kind = extra[0]
                extra = [timedelta(minutes=5)]
            if kind == 'open':
                self.store.mark_read((key,), partition=partition)
                self.post('activate', entry.reminder)
            elif kind in ('dismiss', 'snooze'):
                if kind == 'dismiss':
                    self.store.dismiss(key, partition=partition)
                elif extra[0] > timedelta() and self.policy.preferences.enabled:
                    self.store.snooze(key, self.clock.now() + extra[0], partition=partition)
                self.transport.forget(key)
                self.submitted.pop(key, None)
            if native_action or kind in ('dismiss', 'snooze'):
                self.native_calendars.pop(key, None)
        self.changed()

    def ensure_owner(self, now):
        if self.owner is None:
            owner = ReminderOwner(self.database)
            if not owner.acquire():
                return False
            self.owner = owner
            try:
                if self.store is None:
                    self.store = ReminderStore(self.database)
                self.store.attach_owner(owner, now=now)
            except Exception:
                self.release()
                raise
        self.store.set_context(now=now, generation=self.policy.generation)
        return True

    def set_native(self, enabled):
        if enabled != self.native:
            self.native = enabled
            if enabled and not self.stopped.is_set():
                self.transport.start()
            else:
                self.transport.stop()
                for key, (fence, unread) in tuple(self.submitted.items()):
                    self.store.record_delivery(key, desktop_id=None, uncertain=True, unread=unread, fence=fence)
                self.submitted.clear()
                self.native_calendars.clear()
                self.changed()
                self.report(None)

    def settings(self, states):
        store, policy = self.store, self.policy
        state = store.controller_state()
        old = state.get('calendars', {})
        calendars = {}
        for cal in set(states) | set(policy.preferences.calendars) | set(policy.calendar_eligible_since):
            canonical = states.get(cal)
            local = policy.preferences.calendars.get(cal)
            rule = None
            if local and local.mode != 'event':
                rule = {'mode': local.mode}
                if local.mode == 'replace':
                    rule['alarms'] = [alarm.to_dict() for alarm in local.alarms]
            since = policy.calendar_eligible_since.get(cal)
            calendars[cal] = [rule, since.isoformat() if since else None,
                              list(canonical[:2]) if canonical else None]
        changed = {cal for cal in old.keys() | calendars.keys() if old.get(cal) != calendars.get(cal)}
        boundary = policy.eligible_since.isoformat() if policy.eligible_since else None
        if 'calendars' not in state or changed or state['boundary'] != boundary:
            scans = state.get('scans', {})
            cancelled = []
            for cal in sorted(changed):
                if 'calendars' in state:
                    scans[cal] = None
                previous, current = old.get(cal), calendars.get(cal)
                if previous and (not current or previous[1:] != current[1:]
                                 or current[0] and current[0]['mode'] == 'mute'):
                    cancelled.append(cal)
            updated = dict(calendars=calendars, boundary=boundary, scans=scans)
            global_mute = 'boundary' in state and state['boundary'] != boundary
            for key, cal in tuple(self.native_calendars.items()):
                if global_mute or cal in changed:
                    if key in self.submitted:
                        fence, unread = self.submitted[key]
                        store.record_delivery(key, desktop_id=None, unread=unread, uncertain=True, fence=fence)
                        del self.submitted[key]
                    self.transport.forget(key)
                    del self.native_calendars[key]
            store.checkpoint(updated, cancel_snoozes=global_mute, calendars=cancelled, partition=self.partition)
            state = updated
        return state

    def compile(self, page, *, scanned=False, journal=False):
        for change in page:
            if self.stopped.is_set():
                return
            schedule = boundary = None
            if change.event:
                cal = change.event.calendar_id
                policy = self.policy.preferences.calendars.get(cal, CalendarReminderPolicy())
                boundaries = (self.policy.eligible_since, self.policy.calendar_eligible_since.get(cal))
                boundary = max((x for x in boundaries if x), default=None)
                try:
                    schedule = compile_schedule(change, policy, zone=self.zone)
                except DATA_ERRORS:
                    pass
            cursor = (change.version, change.event_id) if journal else None
            try:
                self.store.put_source(change, schedule, eligible_since=boundary,
                                      scanned=scanned, cursor=cursor)
            except DATA_ERRORS:
                self.store.put_source(change, None, scanned=scanned, cursor=cursor)

    def sources(self, state):
        store = self.store
        if store.bootstrap is not None:
            page = store.read_sources('scan', after_id=store.bootstrap[1], limit=PAGE, partition=self.partition)
            if page:
                self.compile(page, scanned=True)
                return True
            store.finish_bootstrap()
        if state['scans']:
            cal, after = next(iter(state['scans'].items()))
            page = store.read_sources('scan', calendar_id=cal, after_id=after, limit=PAGE, partition=self.partition)
            self.compile(page)
            if page:
                state['scans'][cal] = page[-1].event_id
            else:
                del state['scans'][cal]
            store.checkpoint(state, partition=self.partition)
            return True
        if not self.checked_zone:
            page = store.rebind_sources(self.zone, limit=PAGE, partition=self.partition)
            if page:
                self.compile(page)
                return True
            self.checked_zone = True
        if self.journal_target is None:
            self.journal_target = store.read_sources('revision', partition=self.partition)
        page = store.read_sources('changes', store.cursor, limit=PAGE, partition=self.partition)
        self.compile(page, journal=True)
        if len(page) == PAGE and store.cursor[0] <= self.journal_target:
            # Include all IDs tied at the captured high-water revision. A
            # continuously advancing writer cannot indefinitely delay claims.
            return True
        self.journal_target = None
        if any(item.event is None for item in page):
            self.checked_zone = False
            return True
        return False

    def tick(self):
        now = self.clock.now()
        self.policy = read_reminder_policy(self.database)
        prefs = self.policy.preferences
        if self.last_time and not timedelta() <= now - self.last_time <= JITTER:
            self.armed = None
        self.last_time = now
        if not self.allowed or not prefs.enabled:
            if self.owner:
                self.store.mute()
                self.release()
                self.inbox_store()
                self.changed()
                self.report(None)
            self.test_tick()
            return POLL
        if not self.ensure_owner(now):
            self.test_tick()
            return POLL
        states = self.store.read_sources('calendar_states')
        if self.partition != self.store.partition_id:
            self.unsent.clear()
            self.qualifying.clear()
            self.review = None
            self.submitted.clear()
            self.native_calendars.clear()
            self.journal_target = None
            self.checked_zone = False
            if self.native:
                self.transport.stop()
                self.native = False
            self.partition = self.store.partition_id
            self.changed()
        zone = local_zone()
        if self.zone != zone:
            self.zone = zone
            self.checked_zone = False
        state = self.settings(states)
        more = self.sources(state)
        self.set_native(prefs.delivery != 'in_app' or self.test is not None)
        self.test_tick()
        if self.native:
            available = self.transport.available
            if available is False and monotonic() >= self.retry_probe:
                self.retry_probe = monotonic() + 30
                self.transport.probe()
            if available is False:
                self.report(DESKTOP_ISSUE)
        if more:
            return .001
        wake = self.store.next_wakeup()
        if wake and wake <= now and not self.qualifying and len(self.unsent) < PAGE:
            for due, fence in self.store.claim_due_with_fences(now, limit=PAGE):
                self.qualifying.append(Qualification(due, fence, now, self.policy))
        resolved = False
        while self.qualifying:
            claim = self.qualifying[0]
            accepted = claim.resolve(self)
            if accepted is None:
                break
            key = claim.due.key
            if accepted:
                if claim.policy.preferences.delivery != 'desktop':
                    self.store.pending_unread(key, claim.fence)
                self.unsent.append((key, claim.fence, claim.policy))
            else:
                self.store.suppress(key, claim.fence)
            self.qualifying.popleft()
            resolved = True
        if resolved:
            self.changed()
        self.dispatch()
        if self.qualifying or self.review:
            return .01
        wake = self.store.next_wakeup()
        if wake and wake > now:
            self.armed = wake
            self.armed_revision = self.store.cursor[0]
            return min(POLL, (wake - now).total_seconds())
        if wake and len(self.unsent) < PAGE:
            return .01
        return POLL

    def assess(self, claim):
        due, policy, now = claim.due, claim.policy, claim.now
        snooze_wake = self.store.snooze_deadline(due.key, claim.fence)
        trigger = snooze_wake or due.key.trigger_utc
        boundary = policy.calendar_eligible_since.get(due.calendar_id)
        if any(value and trigger <= value for value in (policy.eligible_since, boundary)):
            claim.result = False
            return
        if trigger == now or (self.armed and self.armed <= trigger and now - self.armed <= JITTER
                              and (snooze_wake or due.source_revision <= self.armed_revision)):
            claim.result = True
            claim.group_repeats = False
            return
        missed = policy.preferences.missed
        if missed == 'none' or now-trigger > timedelta(days=1):
            claim.result = False
            return
        if snooze_wake:
            claim.group_repeats = False
        if missed == 'upcoming' and due.key.occurrence_key is None:
            changes = self.store.read_sources('selected', (due.key.source_id,), partition=self.partition)
            if changes and changes[0].event is not None:
                try:
                    schedule = compile_schedule(changes[0], CalendarReminderPolicy(), zone=self.zone)
                    claim.probe = upcoming(schedule, now)
                    return
                except DATA_ERRORS:
                    pass
            claim.result = False
        else:
            claim.result = missed == 'all' or (due.end is not None and micros(due.end) > micros(now))

    def dispatch(self):
        for _ in range(len(self.unsent)):
            key, fence, original = self.unsent[0]
            prefs = self.policy.preferences
            if prefs.delivery != 'in_app' and (self.transport.available is None or len(self.submitted) >= 32):
                break
            if read_reminder_policy(self.database) != self.policy or self.stopped.is_set():
                break
            self.store.set_context(now=self.clock.now(), generation=self.policy.generation)
            due = self.store.pending_reminder(key, fence)
            if not due:
                self.unsent.popleft()
                self.review = None
                continue
            if original.generation != self.policy.generation:
                cal = due.calendar_id
                if (not prefs.enabled or prefs.calendars.get(cal, CalendarReminderPolicy()).mode == 'mute'
                        or original.eligible_since != self.policy.eligible_since
                        or original.calendar_eligible_since.get(cal) != self.policy.calendar_eligible_since.get(cal)):
                    self.store.suppress(key, fence)
                    self.unsent.popleft()
                    self.review = None
                    continue
                if original.preferences.missed != prefs.missed:
                    if not self.review or self.review.policy.generation != self.policy.generation:
                        self.review = Qualification(due, fence, self.clock.now(), self.policy, group_repeats=False)
                    accepted = self.review.resolve(self)
                    if accepted is None:
                        return
                    if not accepted:
                        self.store.suppress(key, fence)
                        self.review = None
                        self.unsent.popleft()
                        continue
                add_unread = original.preferences.delivery == 'desktop' and prefs.delivery != 'desktop'
                fence = self.store.prepare_unsent(due, fence, add_unread=add_unread)
                if not fence:
                    # A canonical edit can outrun its journal page. Reconcile
                    # it before retiring or refreshing the original claim.
                    return
                self.unsent[0] = key, fence, self.policy
                self.review = None
            calendar = self.store.read_sources('calendar_name', due.calendar_id, partition=self.partition)
            if not self.store.can_emit(due, fence):
                return
            if self.stopped.is_set() or read_reminder_policy(self.database) != self.policy:
                return
            unread = False if prefs.delivery == 'desktop' else None
            if prefs.delivery == 'in_app':
                self.store.record_delivery(key, desktop_id=None, unread=unread, fence=fence)
            elif self.transport.available:
                self.submitted[key] = fence, unread
                self.native_calendars[key] = due.calendar_id
                self.transport.send(due, calendar_name=calendar, profile_label=self.profile, silent=prefs.sound == 'silent')
            else:
                self.store.record_delivery(key, desktop_id=None, unread=unread, uncertain=True, fence=fence)
            self.unsent.popleft()
            self.changed()

    def result(self, kind, key, value):
        if self.test and key == self.test[0].key:
            self.test = None
            if not self.owner:
                self.set_native(False)
            self.report(None if kind == 'delivered' else DESKTOP_ISSUE)
            return
        if key not in self.submitted or not self.store:
            return
        fence, unread = self.submitted[key]
        failed = kind == 'failed'
        self.store.record_delivery(key, desktop_id=None if failed else value, unread=unread, uncertain=failed, fence=fence)
        del self.submitted[key]
        if failed:
            self.native_calendars.pop(key, None)
        self.report(DESKTOP_ISSUE if failed else None)
        self.changed()

    def preview(self, prefs):
        now = self.clock.now()
        key = DeliveryKey('test-' + uuid4().hex, '', 'preview', '', now, 0)
        due = DueReminder(key, '', 0, '', 'Test notification', now, now, Alarm('preview','DISPLAY','PT0S'))
        if prefs.delivery != 'desktop':
            self.post('preview', due)
        if prefs.delivery != 'in_app':
            self.test = due, prefs, False
            self.set_native(True)
            self.transport.probe()
            self.test_tick()

    def test_tick(self):
        if self.test and not self.test[2] and self.transport.available is not None:
            due, prefs, _ = self.test
            self.test = due, prefs, True
            self.transport.send(due, calendar_name='Debacal', profile_label=self.profile, silent=prefs.sound == 'silent')
