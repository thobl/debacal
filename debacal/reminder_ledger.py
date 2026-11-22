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
from .reminder_rule_seek import membership_slice
from .reminder_compilation import (
    DeliveryKey, DueReminder, instant, micros, semantics,
)

LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS ledger_zone (id TEXT PRIMARY KEY, state BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, component TEXT NOT NULL, alarm TEXT NOT NULL,
    occurrence TEXT NOT NULL, rank INTEGER NOT NULL, stamp INTEGER NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, unread INTEGER NOT NULL DEFAULT 0, until INTEGER, closed INTEGER DEFAULT 0,
    desktop_id INTEGER, payload TEXT NOT NULL, claim TEXT);
CREATE INDEX IF NOT EXISTS ledger_group ON ledger(source,component,alarm,occurrence,rank);
CREATE INDEX IF NOT EXISTS ledger_snooze_order ON ledger(until,source,component,alarm,occurrence,rank)
    WHERE status='snoozed';
DROP INDEX IF EXISTS ledger_snoozes;
CREATE INDEX IF NOT EXISTS ledger_status ON ledger(source,status);
CREATE INDEX IF NOT EXISTS ledger_inbox ON ledger(stamp DESC,key);
CREATE INDEX IF NOT EXISTS ledger_unread ON ledger(unread);
CREATE TABLE IF NOT EXISTS delivery_context (
    source TEXT, component TEXT, alarm TEXT, occurrence TEXT, fingerprint TEXT, changed INTEGER,
    PRIMARY KEY(source,component,alarm,occurrence));
CREATE TABLE IF NOT EXISTS source_label (
    source TEXT, component TEXT, first_revision INTEGER NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
    PRIMARY KEY(source,component));
CREATE TABLE IF NOT EXISTS snooze_probe (key TEXT PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snooze_probe_source ON snooze_probe(source);
CREATE TRIGGER IF NOT EXISTS clear_snooze_probe AFTER UPDATE OF status,until ON ledger
WHEN OLD.status='snoozed' BEGIN DELETE FROM snooze_probe WHERE key=OLD.key; END;
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


def decode_due(reader, key, payload, label=None):
    source, component, alarm, occ, stamp, rank = json.loads(key)
    calendar, revision, fingerprint, title, bounds, metadata = json.loads(payload)
    if label and label[0] is not None and label[0] <= revision <= label[1]:
        revision, title = label[1:]
    return DueReminder(DeliveryKey(source, component, alarm, occ[1:] if occ else None, instant(stamp), rank),
                       calendar, revision, fingerprint, title, *(reader.datetime(bound) for bound in bounds), Alarm.from_dict(metadata))


def current_due(db, encoded, payload):
    source, component, *_ = json.loads(encoded)
    label = db.execute('SELECT first_revision,revision,title FROM source_label WHERE source=? AND component=?',
                       (source, component)).fetchone()
    return decode_due(inbox_reader(db), encoded, payload, label)


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


def reconcile(db, source, schedule, previous_schedule, now):
    """Refresh live logical groups and retain the date of each actual anchor edit."""
    if (previous_schedule and schedule and schedule.timing_key and previous_schedule.timing_key == schedule.timing_key
            and previous_schedule.components == schedule.components):
        # The current alarm/anchor proof survives. An in-progress snooze check
        # also retains its cursor across descriptive revisions and restart.
        db.executemany('INSERT INTO source_label VALUES(?,?,?,?,?) ON CONFLICT(source,component) '
                       'DO UPDATE SET revision=excluded.revision,title=excluded.title',
                       ((source, component, previous_schedule.revision, schedule.revision, title)
                        for component, title in schedule.titles.items()))
        return
    # Materialize the most recent displayed title once when a source retires
    # or changes its delivery semantics. Later inbox reads remain self-contained.
    labels = db.execute('SELECT component,first_revision,revision,title FROM source_label WHERE source=?', (source,))
    for component, first, revision, title in labels:
        db.execute("UPDATE ledger SET payload=json_set(payload,'$[1]',?,'$[3]',?) WHERE source=? AND component=? "
                   "AND json_extract(payload,'$[1]') BETWEEN ? AND ?", (revision, title, source, component, first, revision))
    db.execute('DELETE FROM source_label WHERE source=?', (source,))
    db.execute("UPDATE ledger SET status='uncertain',claim=NULL WHERE source=? AND status='pending'", (source,))
    db.execute('DELETE FROM snooze_probe WHERE source=?', (source,))
    groups = db.execute('SELECT component,alarm,occurrence,fingerprint FROM delivery_context WHERE source=?', (source,))
    for component, key, encoded, previous in groups:
        ids = source, component, key, encoded
        alarm = schedule.components.get(component, {}).get(key) if schedule else None
        point = None
        if alarm:
            occurrence = encoded[1:] if encoded else None
            point = schedule.master_point() if occurrence is None else schedule.explicit.get(occurrence)
            if occurrence and point is None and schedule.prepared and not component:
                start = schedule.native_coordinate(occurrence)
                if start is not None:
                    # A historic rank needs its anchor geometry. Exact native
                    # membership matters when an actual snooze is about to fire.
                    try:
                        point, _ = schedule.point(start, occurrence)
                    except (ValueError, OverflowError):
                        pass
        current = (schedule.due(point, alarm, instant(0), 0) if point and point.component == component else None)
        fingerprint = current.anchor_fingerprint if current else None
        if fingerprint != previous:
            db.execute('UPDATE delivery_context SET fingerprint=?,changed=? WHERE source=? AND component=? '
                       'AND alarm=? AND occurrence=?', (fingerprint, now, *ids))
        # Payload is shared by ranks of this alarm/occurrence. Refresh it even
        # before a later inbox action chooses to snooze an already delivered key.
        if current:
            db.execute('UPDATE ledger SET payload=? WHERE source=? AND component=? AND alarm=? AND occurrence=? '
                       "AND fingerprint=? AND status!='dismissed'", (encode_due(db, current), *ids, fingerprint))
        ack = micros(alarm.acknowledged) if alarm and alarm.acknowledged else -1
        db.execute("UPDATE ledger SET status='dismissed',unread=0,until=NULL,closed=0,claim=NULL "
                   "WHERE source=? AND component=? AND alarm=? AND occurrence=? AND status='snoozed' "
                   "AND (fingerprint IS NOT ? OR stamp<=?)", (*ids, fingerprint, ack))


def verify_snooze(db, schedule, key, encoded):
    target = schedule.native_coordinate(key.occurrence_key) if not key.component_key else None
    if target is None:
        return True
    row = db.execute('SELECT state FROM snooze_probe WHERE key=?', (encoded,)).fetchone()
    member, pending = membership_slice(schedule.prepared, target, json.loads(row[0]) if row else None)
    if pending is not None:
        db.execute('INSERT OR REPLACE INTO snooze_probe VALUES(?,?,?)', (encoded, key.source_id, json.dumps(pending)))
        return None
    return member


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
