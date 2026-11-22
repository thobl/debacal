"""Compile canonical alarm intent and prepared recurrence timing into worker data."""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone, tzinfo
import hashlib
import json

from . import ics_fallback as ics
from .alarm_ics import direct_parts
from .alarms import Alarm, signed_duration, utc_stamp
from .event_duration import end_for, occurrence_end_zone
from .models import Event, copy_event
from .prepared_storage import StateReader, StateWriter
from .query_projection import pack_state, unpack_state
from .recurrence import normalize_recurrence_time
from .reminder_preferences import CalendarReminderPolicy, instantiate_reminder_templates
from .reminder_sources import SourceChange
from .timezones import event_timezone

UTC = timezone.utc
UNIT = timedelta(microseconds=1)
ORIGIN = datetime.min.replace(tzinfo=UTC)
MAX_US = (datetime.max.replace(tzinfo=UTC) - ORIGIN) // UNIT
DAY_US = 86_400_000_000


def micros(stamp):
    if stamp.utcoffset() is None:
        raise ValueError('Reminder times must be timezone-aware.')
    return (stamp.astimezone(UTC) - ORIGIN) // UNIT


def instant(us):
    return ORIGIN + UNIT * min(MAX_US, max(0, us))


def relative(alarm):
    return alarm.trigger.upper().startswith(('P', '+P', '-P'))


def semantics(alarm):
    trigger = signed_duration(alarm.trigger) if relative(alarm) else utc_stamp(alarm.trigger).isoformat()
    interval = signed_duration(alarm.repeat_interval) if alarm.repeat else None
    return alarm.action, alarm.related, trigger, alarm.repeat, interval


def _component_alarms(event, policy, component):
    if policy.mode == 'mute':
        return {}
    alarms = event.alarms or ()
    if policy.mode == 'replace':
        alarms = instantiate_reminder_templates(policy.alarms, calendar_id=event.calendar_id,
                                                event_uid=event.ical_uid or event.id, recurrence_id=component)
    acks = {a.uid: a.acknowledged for a in alarms if a.uid and a.acknowledged}
    result = {}
    for alarm in alarms:
        if alarm.issue or alarm.action not in ('DISPLAY', 'AUDIO') or not alarm.trigger:
            continue
        try:
            semantics(alarm)
            if alarm.repeat and (alarm.repeat < 0 or sum(signed_duration(alarm.repeat_interval)) <= 0):
                continue
            ack = alarm.acknowledged
            for part in direct_parts(ics.unfolded(alarm.raw_ics)):
                if len(part) == 1:
                    name, params, uid = ics.content_line(part[0])
                    if name == 'RELATED-TO' and params.get('RELTYPE', '').upper() == 'SNOOZE' and uid in acks:
                        ack = max(ack, acks[uid]) if ack else acks[uid]
        except (ValueError, OverflowError, TypeError):
            continue
        result[alarm.key] = replace(alarm, acknowledged=ack, raw_ics='')
    return result


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    source_id: str
    component_key: str
    alarm_key: str
    occurrence_key: str | None
    trigger_utc: datetime
    repeat_index: int


@dataclass(frozen=True, slots=True)
class DueReminder:
    key: DeliveryKey
    calendar_id: str
    source_revision: int
    anchor_fingerprint: str
    title: str
    start: datetime | None
    end: datetime | None
    alarm: Alarm


@dataclass(frozen=True, slots=True)
class Point:
    component: str
    occurrence: str | None
    start: datetime
    end: datetime


@dataclass(slots=True)
class AlarmSchedule:
    master: Event
    revision: int
    zone: tzinfo
    components: dict
    titles: dict
    explicit: dict
    prepared: object
    cut: bool
    writer: StateWriter = field(default_factory=StateWriter, repr=False)
    reader: StateReader = field(init=False, repr=False)

    def __post_init__(self):
        self.reader = StateReader(lambda ref: self.writer.zones[ref])

    def encode_bounds(self, start, end):
        return [self.writer.datetime(start), self.writer.datetime(end)]

    def decode_bounds(self, state):
        return tuple(self.reader.datetime(bound) for bound in state)

    def dumps(self, *, runtime=False) -> bytes:
        writer = self.writer
        if runtime:
            for point in self.explicit.values():
                writer.zone(point.start.tzinfo)
                writer.zone(point.end.tzinfo)
        points = [] if runtime else [[p.component, p.occurrence, writer.datetime(p.start), writer.datetime(p.end)]
                                     for p in self.explicit.values()]
        state = [writer.event(self.master), self.revision, writer.zone(self.zone),
                 {c: [a.to_dict() for a in alarms.values()] for c, alarms in self.components.items()},
                 self.titles, points, writer.series(self.prepared) if self.prepared else None, self.cut]
        return pack_state([state, writer.zones, writer.definitions])

    @classmethod
    def loads(cls, blob: bytes):
        state, zones, definitions = unpack_state(blob)
        reader = StateReader(zones, definitions)
        event, rev, zone, alarms, titles, points, series, cut = state
        master = reader.event(event)
        result = cls(master, rev, reader.zone(zone), {c: {a['key']: Alarm.from_dict(a) for a in items}
                                                     for c, items in alarms.items()}, titles,
                   {k: Point(c, k, reader.datetime(s), reader.datetime(e)) for c, k, s, e in points},
                   reader.series(master, series) if series else None, cut)
        result.writer.zones.update(zones)
        result.reader = reader
        return result

    def point(self, start, key):
        event = self.master
        start = _local_bound(event, start, self.zone)
        end = end_for(event, start)
        clipped = False
        added = self.prepared.added_keys if self.prepared else event.recurrence_dates
        if self.cut and key not in added and (start.year, start.month) < (9999, 12):
            bound = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=start.tzinfo)
            bound = normalize_recurrence_time(bound)
            if micros(end) > micros(bound):
                end, clipped = bound, True
        return Point('', key, start, occurrence_end_zone(event, end)), clipped

    def fingerprint(self, point, alarm):
        anchor = point.end if alarm.related == 'END' else point.start
        context = self.writer.datetime(anchor) if relative(alarm) else None
        return hashlib.sha256(json.dumps([semantics(alarm), context]).encode()).hexdigest()

    def due(self, point, alarm, stamp, rank):
        signature = self.fingerprint(point, alarm)
        key = DeliveryKey(self.master.id, point.component, alarm.key, point.occurrence, stamp, rank)
        return DueReminder(key, self.master.calendar_id, self.revision, signature, self.titles[point.component],
                           point.start, point.end, alarm)


def _local_bound(event, value, zone):
    if event.all_day:
        return datetime.combine(value.date(), time.min, zone)
    if event.floating_time:
        return value.replace(tzinfo=zone, fold=0)
    return value.astimezone(event_timezone(event))


def compile_schedule(change: SourceChange, policy: CalendarReminderPolicy, *, zone: tzinfo) -> AlarmSchedule:
    event, indexed = change.event, change.indexed
    if event is None or indexed is None:
        raise ValueError('A live canonical event and prepared timing are required.')
    if event.recurring and not event.floating_time and not event.all_day:
        # Legacy wire hydration can recover the DTSTART zone. A rule compiled
        # in a different civil clock has different native starts through DST.
        old_zone, new_zone = event_timezone(indexed.master), event_timezone(event)
        if old_zone is not new_zone and StateWriter().zone(old_zone) != StateWriter().zone(new_zone):
            from .event_range_index import EventRangeIndex
            indexed = EventRangeIndex.prepare(event)
    master = copy_event(event, dict(raw_ics='', alarms=(), recurrence_overrides={}, description='', location='',
                                    schedule_expression='', expression_anchor=''))
    components = {'': _component_alarms(event, policy, '')}
    titles = {'': event.title}
    native = indexed.error is None and any(key[2] for key, _ in indexed.entries)
    series = copy(indexed.prepared) if native else None
    if series:
        # Explicit points have their own durable queue. Retain only rule seek state.
        series.master = master
        series.excluded = series.excluded.union(item.recurrence_id for item in indexed.occurrences.values())
        series.explicit_occurrences = series._explicit_starts = series._explicit_ends = ()
    cut = event.range_policy == 'cut' and any(field in ('FREQ=MONTHLY', 'FREQ=YEARLY')
                                               for field in event.recurrence_rule.upper().split(';'))
    schedule = AlarmSchedule(master, change.version, zone, components, titles, {}, series, cut)
    for item in indexed.occurrences.values():
        key = item.recurrence_id
        if key in event.recurrence_overrides:
            detached = Event.from_dict(event.recurrence_overrides[key])
            components[key] = _component_alarms(detached, policy, key)
            titles[key] = detached.title
            start = _local_bound(detached, detached.start, zone)
            end = (_local_bound(detached, detached.end, zone) if detached.floating_time or detached.all_day
                   else detached.end)
            point = Point(key, key, start, end)
        elif not key:
            start = _local_bound(event, event.start, zone)
            end = (_local_bound(event, event.end, zone) if event.floating_time or event.all_day else event.end)
            point = Point('', '', start, end)
        else:
            point, _ = schedule.point(item.start, key)
        schedule.explicit[key] = point
    return schedule
