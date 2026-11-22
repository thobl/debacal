"""Worker-side trigger frontier. Each row is one independently resumable stream."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .alarms import _advance, alarm_trigger, signed_duration
from .event_duration import elapsed_duration
from .prepared_recurrence import _simple_day_step
from .recurrence import RecurrenceRangeError, recurrence_key
from .reminder_rule_seek import seek_slice
from .reminder_progression import first_pair, next_pair
from .reminder_compilation import (
    DAY_US, MAX_US, UNIT, AlarmSchedule, DeliveryKey, DueReminder, Point, compile_schedule,
    instant, micros, relative,
)

__all__ = ['AlarmSchedule', 'DeliveryKey', 'DueReminder', 'compile_schedule', 'next_triggers']

# Rule refill and nominal-repeat catch-up precede ready triggers at the same time.
RULE, BACKFILL, READY = range(3)
REPEAT_STEPS = 64
FRONTIER_SQL = """
CREATE TABLE IF NOT EXISTS frontier (
    id INTEGER PRIMARY KEY, wake INTEGER NOT NULL, kind INTEGER NOT NULL, source TEXT NOT NULL,
    component TEXT NOT NULL, alarm TEXT NOT NULL, occurrence TEXT NOT NULL, rank INTEGER NOT NULL,
    state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS frontier_wake ON frontier(wake,kind,source,component,alarm,occurrence,rank);
CREATE INDEX IF NOT EXISTS frontier_source ON frontier(source);
"""


def _fixed(zone):
    return isinstance(zone, timezone) or getattr(zone, 'key', '') in ('UTC', 'Etc/UTC', 'GMT')


def _width(parts):
    days, seconds = parts
    return days * DAY_US + seconds * 1_000_000


@dataclass(slots=True)
class Job:
    wake: int
    kind: int
    source: str
    component: str
    alarm: str
    occurrence: str
    rank: int
    state: list

    def save(self, db):
        if self.wake <= MAX_US:
            row = db.execute(
                'INSERT INTO frontier(wake,kind,source,component,alarm,occurrence,rank,state) VALUES(?,?,?,?,?,?,?,?)',
                (max(0, self.wake), self.kind, self.source, self.component, self.alarm,
                 self.occurrence, self.rank, json.dumps(self.state, separators=(',', ':'))))
            return row.lastrowid


def head(db, deferred=()):
    where = 'WHERE id NOT IN (' + ','.join('?' for _ in deferred) + ') ' if deferred else ''
    sql = ('SELECT id,wake,kind,source,component,alarm,occurrence,rank,state FROM frontier ' + where +
           'ORDER BY wake,kind,source,component,alarm,occurrence,rank LIMIT 1')
    return db.execute(sql, tuple(deferred)).fetchone()


def pop(db, row):
    db.execute('DELETE FROM frontier WHERE id=?', (row[0],))
    return Job(*row[1:8], json.loads(row[8]))


def _train(schedule, alarm, point, floor):
    try:
        base = micros(alarm_trigger(alarm, point.start, point.end))
    except (ValueError, OverflowError):
        return
    if alarm.acknowledged:
        floor = max(floor, micros(alarm.acknowledged) + 1)
    occurrence = '' if point.occurrence is None else '1' + point.occurrence
    rank, stamp, kind = 0, base, READY
    if base < floor:
        if not alarm.repeat:
            return
        parts = signed_duration(alarm.repeat_interval)
        zone = _repeat_zone(alarm, point)
        if not parts[0] or _fixed(zone):
            step = _width(parts)
            rank = (floor - base + step - 1) // step
            if rank > alarm.repeat:
                return
            stamp += rank * step
        else:
            kind = BACKFILL
    return Job(max(stamp, floor), kind, schedule.master.id, point.component, alarm.key, occurrence, rank,
               [schedule.encode_bounds(point.start, point.end), stamp, floor])


def _repeat_zone(alarm, point):
    if not relative(alarm):
        return timezone.utc
    return (point.end if alarm.related == 'END' else point.start).tzinfo


def _offset_bounds(schedule, alarm, cut):
    event = schedule.master
    wall = event.floating_time or event.all_day
    zone = schedule.zone if wall else (event.end if alarm.related == 'END' else event.start).tzinfo
    days, seconds = signed_duration(alarm.trigger)
    q = _width((days, seconds))
    uncertainty = 2 * DAY_US if days and not _fixed(zone) else 0
    if wall:
        uncertainty += DAY_US
    if alarm.related == 'START':
        return q - uncertainty, q + uncertainty
    if cut:
        return q - uncertainty, q + 33 * DAY_US + uncertainty
    if wall:
        duration = (event.end.replace(tzinfo=None) - event.start.replace(tzinfo=None)) // UNIT
    elif event.duration_spec:
        duration = _width(signed_duration(event.duration_spec))
        if not _fixed(event.start.tzinfo):
            uncertainty += 2 * DAY_US
    else:
        duration = elapsed_duration(event.start, event.end) // UNIT
    return q + duration - uncertainty, q + duration + uncertainty


def _progression(schedule, alarm, floor):
    """Prove a native constant UTC stride before using exact pair arithmetic."""
    series, master = schedule.prepared, schedule.master
    if not alarm.repeat or series.floating or series.cut or not _fixed(series.zone):
        return None
    rule = series.rule
    days = _simple_day_step(rule)
    if days:
        stride = days * DAY_US
    elif (master.recurrence_rule.upper().split(';')[0] == 'FREQ=HOURLY' and
          all(p.split('=')[0] in ('FREQ', 'INTERVAL', 'UNTIL', 'COUNT')
              for p in master.recurrence_rule.upper().split(';'))):
        stride = rule._interval * 3_600_000_000
    else:
        return None
    if not _fixed(master.start.tzinfo) or (alarm.related == 'END' and not _fixed(master.end.tzinfo)):
        return None
    origin = micros(rule._dtstart)
    last = (MAX_US-origin) // stride
    if rule._until is not None:
        last = min(last, (micros(rule._until)-origin) // stride)
    if rule._count is not None:
        last = min(last, rule._count-1)
    offset, _ = _offset_bounds(schedule, alarm, cut=False)
    base = origin + offset
    width = _width(signed_duration(alarm.repeat_interval))
    if alarm.acknowledged:
        floor = max(floor, micros(alarm.acknowledged)+1)
    args = [stride, width, last, alarm.repeat]
    return origin, base, args, first_pair(*args, floor-base)


def _pair_job(source, alarm, origin, base, args, pair):
    delta, i, rank = pair
    key = instant(origin + i*args[0]).isoformat()
    return Job(base+delta, READY, source, '', alarm, '1'+key, rank,
               ['pair', origin, base, args, pair])


def _step_pair(job, schedule, alarm):
    _, origin, base, args, pair = job.state
    key = job.occurrence[1:]
    series = schedule.prepared
    due = None
    if key not in series.excluded and key not in series.override_keys and key not in series.added_keys:
        try:
            point, _ = schedule.point(instant(origin + pair[1]*args[0]), key)
            due = schedule.due(point, alarm, instant(job.wake), job.rank)
        except OverflowError:
            pass
    following = next_pair(*args, pair)
    jobs = (_pair_job(job.source, job.alarm, origin, base, args, following),) if following else ()
    return due, jobs


def initial_jobs(schedule, floor):
    for point in schedule.explicit.values():
        for alarm in schedule.components[point.component].values():
            if relative(alarm) or point.component:
                if job := _train(schedule, alarm, point, floor):
                    yield job
    master = schedule.master
    for alarm in schedule.components[''].values():
        if not relative(alarm):
            point = Point('', None, master.start, master.end)
            if job := _train(schedule, alarm, point, floor):
                yield job
        elif schedule.prepared:
            if progression := _progression(schedule, alarm, floor):
                origin, base, args, pair = progression
                if pair:
                    yield _pair_job(master.id, alarm.key, origin, base, args, pair)
                continue
            for cut in ([False, True] if schedule.cut and alarm.related == 'END' else [False]):
                lo, hi = _offset_bounds(schedule, alarm, cut)
                repeat_span = _repeat_span(schedule, alarm)
                cursor = max(micros(schedule.prepared.rule._dtstart), floor - hi - repeat_span, 0)
                if cursor <= MAX_US:
                    yield Job(max(floor, cursor + lo), RULE, master.id, '', alarm.key, '', 0,
                              [cursor, floor, lo, cut])


def _refill(job, schedule, alarm):
    cursor, floor, lo, cut, *ranked = job.state
    series = schedule.prepared
    # A complex rule may yield no dates in a bounded probe. Advance its proven
    # lower bound and retain the job until the rule's end can be established.
    stop = min(MAX_US, cursor + 28 * DAY_US)
    if series.rule._until is not None and cursor > micros(series.rule._until):
        return
    if series.seek is not None and series.seek.seek(instant(cursor)) is None:
        return
    if not ranked:
        try:
            value = next(series._regular(instant(cursor), instant(stop)), None)
        except RecurrenceRangeError:
            ranked = [None]
    if ranked:
        value, pending = seek_slice(series, cursor, stop, ranked[0])
        if pending is not None:
            yield replace(job, state=[cursor, floor, lo, cut, pending])
            return
    mode = [None] if ranked else []
    if value is None:
        if (series.calendar_periods or ranked) and stop < MAX_US:
            upper = series.rule_bounds[1] if series.rule_bounds else None
            if upper is None or stop < micros(upper):
                yield replace(job, wake=max(floor, stop + 1 + lo), state=[stop + 1, floor, lo, cut, *mode])
        return
    coordinate = micros(value)
    if coordinate + lo > job.wake:
        yield replace(job, wake=coordinate + lo, state=[coordinate, floor, lo, cut, *mode])
        return
    key = recurrence_key(value, schedule.master.all_day, floating_time=schedule.master.floating_time)
    if key not in series.excluded and key not in series.override_keys and key not in series.added_keys:
        try:
            point, clipped = schedule.point(value, key)
            if alarm.related != 'END' or cut == clipped:
                if train := _train(schedule, alarm, point, floor):
                    yield train
        except OverflowError:
            pass
    # RFC recurrence starts have second resolution. Use a full second for the
    # next seek since the prepared engine also uses floating-point timestamps.
    following = coordinate + 1_000_000
    if following <= MAX_US:
        yield replace(job, wake=max(floor, following + lo), state=[following, floor, lo, cut, *mode])


def _resume(job, schedule, alarm, floor):
    """Seek an interrupted stream to the rolling catch-up boundary."""
    if job.state[0] == 'pair':
        _, origin, base, args, _ = job.state
        pair = first_pair(*args, floor-base)
        return (_pair_job(job.source, job.alarm, origin, base, args, pair),) if pair else ()
    if job.kind == RULE:
        cursor, _, lo, cut, *_ = job.state
        _, hi = _offset_bounds(schedule, alarm, cut)
        span = _repeat_span(schedule, alarm)
        cursor = max(cursor, floor - hi - span)
        return (replace(job, wake=max(floor, cursor + lo), state=[cursor, floor, lo, cut]),)
    bounds, stamp, _ = job.state
    if not alarm.repeat:
        return ()
    start, end = schedule.decode_bounds(bounds)
    zone = _repeat_zone(alarm, Point(job.component, None, start, end))
    parts = signed_duration(alarm.repeat_interval)
    if parts[0] and not _fixed(zone):
        return (replace(job, kind=BACKFILL, wake=floor, state=[bounds, stamp, floor]),)
    step = _width(parts)
    skip = (floor-stamp+step-1) // step
    if job.rank + skip > alarm.repeat:
        return ()
    stamp += skip*step
    return (replace(job, wake=stamp, rank=job.rank+skip, state=[bounds, stamp, floor]),)


def _repeat_span(schedule, alarm):
    if not alarm.repeat:
        return 0
    parts = signed_duration(alarm.repeat_interval)
    master = schedule.master
    end_zone = (master.end if alarm.related == 'END' else master.start).tzinfo
    varying = parts[0] and not _fixed(schedule.zone if schedule.prepared.floating else end_zone)
    return alarm.repeat * (_width(parts) + (2 * DAY_US if varying else 0))


def step(job, schedule, *, floor=0):
    """Consume one job and return one ready trigger plus a bounded set of successors."""
    alarm = schedule.components[job.component][job.alarm]
    if job.wake < floor:
        return None, _resume(job, schedule, alarm, floor)
    if job.state[0] == 'pair':
        return _step_pair(job, schedule, alarm)
    if job.kind == RULE:
        return None, tuple(_refill(job, schedule, alarm))
    bounds, stamp, floor = job.state
    start, end = schedule.decode_bounds(bounds)
    point = Point(job.component, job.occurrence[1:] if job.occurrence else None, start, end)
    rank = job.rank
    due = schedule.due(point, alarm, instant(stamp), rank) if job.kind == READY else None
    if rank == alarm.repeat:
        return due, ()
    days, seconds = signed_duration(alarm.repeat_interval)
    zone = _repeat_zone(alarm, point)
    for _ in range(REPEAT_STEPS):
        try:
            stamp = micros(_advance(instant(stamp).astimezone(zone), days, seconds))
        except OverflowError:
            return due, ()
        rank += 1
        if stamp >= floor:
            return due, (replace(job, wake=stamp, kind=READY, rank=rank, state=[bounds, stamp, floor]),)
        if rank == alarm.repeat:
            return due, ()
    return due, (replace(job, rank=rank, state=[bounds, stamp, floor]),)


def next_triggers(schedule: AlarmSchedule, after: datetime, until: datetime, *, limit: int = 128) -> tuple[DueReminder, ...]:
    if limit < 0:
        raise ValueError('Reminder page size must be nonnegative.')
    floor, stop = micros(after), micros(until)
    if floor >= stop or not limit:
        return ()
    with closing(sqlite3.connect('')) as db:
        db.executescript(FRONTIER_SQL)
        for job in initial_jobs(schedule, floor):
            job.save(db)
        result = []
        while len(result) < limit and (row := head(db)) and row[1] < stop:
            due, following = step(pop(db, row), schedule)
            for job in following:
                job.save(db)
            if due:
                result.append(due)
        return tuple(result)
