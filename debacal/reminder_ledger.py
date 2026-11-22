"""Local delivery, disposition and edited logical alarm identities."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
import json
from typing import Literal
from uuid import uuid4

from .alarms import Alarm
from .prepared_storage import StateReader, StateWriter
from .query_projection import pack_state, unpack_state
from .recurrence import key_datetime
from .reminder_compilation import (
    DeliveryKey, DueReminder, Point, instant, micros, semantics,
)

LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS ledger_zone (id TEXT PRIMARY KEY, state BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, component TEXT NOT NULL, alarm TEXT NOT NULL,
    occurrence TEXT NOT NULL, rank INTEGER NOT NULL, stamp INTEGER NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, unread INTEGER NOT NULL DEFAULT 0, until INTEGER, closed INTEGER DEFAULT 0,
    desktop_id INTEGER, payload TEXT NOT NULL, claim TEXT);
CREATE INDEX IF NOT EXISTS ledger_group ON ledger(source,component,alarm,occurrence,rank);
CREATE INDEX IF NOT EXISTS ledger_snoozes ON ledger(until,key) WHERE status='snoozed';
CREATE INDEX IF NOT EXISTS ledger_status ON ledger(source,status);
CREATE INDEX IF NOT EXISTS ledger_inbox ON ledger(stamp DESC,key);
CREATE INDEX IF NOT EXISTS ledger_unread ON ledger(unread);
CREATE TABLE IF NOT EXISTS delivery_context (
    source TEXT, component TEXT, alarm TEXT, occurrence TEXT, fingerprint TEXT, changed INTEGER,
    PRIMARY KEY(source,component,alarm,occurrence));
CREATE TABLE IF NOT EXISTS suppressed (
    source TEXT, component TEXT, alarm TEXT, occurrence TEXT, rank INTEGER, through INTEGER,
    PRIMARY KEY(source,component,alarm,occurrence,rank));
"""


@dataclass(frozen=True, slots=True)
class InboxEntry:
    reminder: DueReminder
    status: Literal['pending', 'delivered', 'uncertain', 'snoozed', 'dismissed']
    unread: bool
    snoozed_until: datetime | None


def group(key):
    occurrence = '' if key.occurrence_key is None else '1' + key.occurrence_key
    return key.source_id, key.component_key, key.alarm_key, occurrence


def encode_key(key):
    return json.dumps([*group(key), micros(key.trigger_utc), key.repeat_index], separators=(',', ':'))


def encode_due(db, due):
    def save(_kind, ref, state):
        db.execute('INSERT OR IGNORE INTO ledger_zone VALUES(?,?)', (ref, pack_state(state)))
    writer = StateWriter(save)
    bounds = writer.datetime(due.start), writer.datetime(due.end)

    return json.dumps([due.calendar_id, due.source_revision, due.anchor_fingerprint, due.title,
                       bounds, due.alarm.to_dict()], separators=(',', ':'))


def inbox_reader(db):
    return StateReader(lambda ref: unpack_state(db.execute('SELECT state FROM ledger_zone WHERE id=?',
                                                           (ref,)).fetchone()[0]))


def decode_due(reader, key, payload):
    source, component, alarm, occ, stamp, rank = json.loads(key)
    calendar, revision, fingerprint, title, bounds, metadata = json.loads(payload)
    return DueReminder(DeliveryKey(source, component, alarm, occ[1:] if occ else None, instant(stamp), rank),
                       calendar, revision, fingerprint, title, *(reader.datetime(bound) for bound in bounds), Alarm.from_dict(metadata))


def align(db, schedule, previous, now):
    """Retain exact content/ordinal matches, then an unambiguous residual edit."""
    for component, alarms in schedule.components.items():
        old = {a.key: a for a in previous.components.get(component, {}).values() if not a.uid}
        new = [a for a in alarms.values() if not a.uid]
        matches = {}
        by_value = defaultdict(list)
        for a in old.values():
            by_value[semantics(a)].append(a.key)
        for a in new:
            candidates = by_value[semantics(a)]
            if candidates:
                match = a.key if a.key in candidates else candidates[0]
                candidates.remove(match)
                matches[a.key] = match
                del old[match]
        unmatched = [a for a in new if a.key not in matches]
        if len(old) == len(unmatched) == 1:
            matches[unmatched[0].key] = next(iter(old))
        elif old and unmatched:
            for a in unmatched:
                # Ambiguous edits suppress past replay only for an occurrence
                # and repeat rank which actually had a predecessor disposition.
                for predecessor in old:
                    db.execute('INSERT OR REPLACE INTO suppressed SELECT source,component,?,occurrence,rank,? '
                               'FROM ledger WHERE source=? AND component=? AND alarm=?',
                               (a.key, now, schedule.master.id, component, predecessor))
        used = set(matches.values()) | {a.key for a in alarms.values() if a.uid}
        updated = {}
        for a in alarms.values():
            key = matches.get(a.key, a.key)
            if not a.uid and a.key not in matches and key in used:
                key = 'local-' + uuid4().hex
            updated[key] = replace(a, key=key)
        schedule.components[component] = updated


def refresh_contexts(db, source, schedule, now):
    """Date an anchor edit once, even if more descriptive edits arrive before claiming."""
    if schedule is None:
        db.execute('UPDATE delivery_context SET fingerprint=NULL,changed=? WHERE source=?', (now, source))
        return
    groups = db.execute('SELECT component,alarm,occurrence,fingerprint FROM delivery_context WHERE source=?', (source,))
    for component, key, encoded, previous in groups:
        alarm = schedule.components.get(component, {}).get(key)
        fingerprint = None
        if alarm:
            occurrence = encoded[1:] if encoded else None
            point = schedule.explicit.get(occurrence)
            if occurrence is None:
                point = Point('', None, schedule.master.start, schedule.master.end)
            elif occurrence and point is None and schedule.prepared and not component:
                # Native identity already gives the original start. Membership
                # is checked by the frontier if that identity is ever due again.
                try:
                    point, _ = schedule.point(key_datetime(occurrence, schedule.master), occurrence)
                except (ValueError, OverflowError):
                    point = None
            if point and point.component == component:
                fingerprint = schedule.fingerprint(point, alarm)
        if fingerprint != previous:
            db.execute('UPDATE delivery_context SET fingerprint=?,changed=? WHERE source=? AND component=? '
                       'AND alarm=? AND occurrence=?', (fingerprint, now, source, component, key, encoded))


def claimed(db, due, changed, fence):
    key = due.key
    ids = group(key)
    if db.execute('SELECT 1 FROM ledger WHERE key=?', (encode_key(key),)).fetchone():
        return False
    context = db.execute('SELECT changed FROM delivery_context WHERE source=? AND component=? AND alarm=? '
                         'AND occurrence=?', ids).fetchone()
    if context:
        changed = context[0]
    if db.execute('SELECT 1 FROM ledger WHERE source=? AND component=? AND alarm=? AND occurrence=? '
                  'AND ((closed=1 AND fingerprint=?) OR (rank=? AND fingerprint!=? AND ?<=?)) LIMIT 1',
                  (*ids, due.anchor_fingerprint, key.repeat_index, due.anchor_fingerprint,
                   micros(key.trigger_utc), changed)).fetchone():
        return False
    if db.execute('SELECT 1 FROM suppressed WHERE source=? AND component=? AND alarm=? AND occurrence=? '
                  'AND rank=? AND through>=?', (*ids, key.repeat_index, micros(key.trigger_utc))).fetchone():
        return False
    db.execute('INSERT OR IGNORE INTO delivery_context VALUES(?,?,?,?,?,?)', (*ids, due.anchor_fingerprint, changed))
    db.execute('INSERT INTO ledger(key,source,component,alarm,occurrence,rank,stamp,fingerprint,status,payload,claim) '
               "VALUES(?,?,?,?,?,?,?,?,'pending',?,?)", (encode_key(key), *ids, key.repeat_index,
                micros(key.trigger_utc), due.anchor_fingerprint, encode_due(db, due), fence))
    return True


def update_snoozes(db, source, schedule):
    for key, payload in db.execute("SELECT key,payload FROM ledger WHERE source=? AND status='snoozed'", (source,)):
        old = decode_due(inbox_reader(db), key, payload)
        k = old.key
        alarm = schedule.components.get(k.component_key, {}).get(k.alarm_key) if schedule else None
        point = None
        if alarm:
            if k.occurrence_key is None:
                point = Point('', None, schedule.master.start, schedule.master.end)
            else:
                point = schedule.explicit.get(k.occurrence_key)
                if point is None and schedule.prepared:
                    item = schedule.prepared.occurrence_at(k.occurrence_key, floating_zone=schedule.zone)
                    if item:
                        point, _ = schedule.point(item.start, k.occurrence_key)
        if point and point.component == k.component_key:
            due = schedule.due(point, alarm, k.trigger_utc, k.repeat_index)
            if (due.anchor_fingerprint == old.anchor_fingerprint and
                    (not alarm.acknowledged or alarm.acknowledged < k.trigger_utc)):
                db.execute('UPDATE ledger SET payload=? WHERE key=?', (encode_due(db, due), key))
                continue
        db.execute("UPDATE ledger SET status='dismissed',unread=0,until=NULL,closed=0,claim=NULL WHERE key=?", (key,))
