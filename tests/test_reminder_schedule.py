from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alarm_support import BASE, alarm_event
from debacal.alarms import Alarm, alarm_trigger
from debacal.event_range_index import EventRangeIndex
from debacal.reminder_preferences import CalendarReminderPolicy
from debacal.reminder_sources import SourceChange
from debacal.reminder_schedule import AlarmSchedule, compile_schedule, next_triggers

UTC = timezone.utc
DAY = timedelta(days=1)


def schedule(event, *, zone=UTC, prepared=None, revision=1, policy=None):
    change = SourceChange(revision, event.id, event, prepared or EventRangeIndex.prepare(event))
    result = compile_schedule(change, policy or CalendarReminderPolicy(), zone=zone)
    return AlarmSchedule.loads(result.dumps())


def test_sparse_far_offset_and_absolute_are_not_agenda_queries():
    later = BASE.replace(year=2046)
    advance = Alarm('advance', 'DISPLAY', f'-P{(later-BASE).days}D')
    absolute = Alarm('whole', 'DISPLAY', '20261121T090000Z')
    event = alarm_event(recurrence_rule='FREQ=YEARLY;INTERVAL=20', alarms=(advance, absolute))
    data = schedule(event, prepared=EventRangeIndex.prepare(replace(event, alarms=None)))
    due = next_triggers(data, BASE, BASE + DAY)
    assert [(d.key.alarm_key, d.key.occurrence_key, d.key.trigger_utc) for d in due] == [
        ('advance', later.isoformat(), BASE), ('whole', None, BASE)]


def test_cut_end_and_detached_actual_ranges_sharing_start():
    begin = BASE.replace(day=29)
    end = begin + 5 * DAY
    alarm = Alarm('end', 'DISPLAY', 'PT1H', related='END')
    moved_key = begin.replace(month=12).isoformat()
    moved = alarm_event(start=begin, end=begin+DAY, alarms=(alarm,), title='Detached')
    event = alarm_event(start=begin, end=end, recurrence_rule='FREQ=MONTHLY',
                        recurrence_overrides={moved_key: moved.to_dict()}, range_policy='cut', alarms=(alarm,))
    due = next_triggers(schedule(event), begin, begin + 7 * DAY)
    assert [(d.title, d.end, d.key.trigger_utc) for d in due] == [
        ('Detached', begin+DAY, begin+DAY+timedelta(hours=1)),
        ('Example', begin.replace(month=12, day=1, hour=0), begin.replace(month=12, day=1, hour=1))]


def test_fixed_repeat_arithmetic_and_nominal_dst():
    repeat = Alarm('dense', 'DISPLAY', 'PT0S', repeat=2_000_000_000, repeat_interval='PT1S')
    then = BASE + timedelta(seconds=1_700_000_000)
    due = next_triggers(schedule(alarm_event(alarms=(repeat,))), then, then+DAY, limit=3)
    assert [d.key.repeat_index for d in due] == list(range(1_700_000_000, 1_700_000_003))
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2025, 1, 1, 2, 30, tzinfo=zone)
    mixed = replace(repeat, repeat=500, repeat_interval='P1DT1H')
    event = alarm_event(start=start, end=start+DAY, floating_time=True, alarms=(mixed,))
    expected = alarm_trigger(mixed, start, event.end, repeat_index=400)
    item, = next_triggers(schedule(event, zone=zone), expected, expected+timedelta(seconds=1))
    assert item.key.repeat_index == 400 and item.key.trigger_utc == expected


def test_acknowledged_and_snooze_sibling():
    parent = Alarm('original', 'DISPLAY', 'PT0S', uid='original', acknowledged=BASE)
    child = Alarm('child', 'DISPLAY', '20261121T090500Z', uid='child',
                  raw_ics='BEGIN:VALARM\nRELATED-TO;RELTYPE=SNOOZE:original\nEND:VALARM')
    event = alarm_event(alarms=(parent, child, Alarm('other', 'EMAIL', 'PT0S')))
    due = next_triggers(schedule(event), BASE, BASE+DAY)
    assert [(d.key.alarm_key, d.key.trigger_utc) for d in due] == [('child', BASE+timedelta(minutes=5))]
    changed = replace(event, alarms=(replace(parent, acknowledged=BASE+DAY), child))
    assert not next_triggers(schedule(changed), BASE, BASE+DAY)


def test_native_repeat_pair_seek_matches_exhaustive_grid():
    from debacal.reminder_progression import first_pair, next_pair
    for d in (1, 2, 7):
        for s in (1, 3, 7):
            for n, r in ((2, 7), (7, 2), (4, 4)):
                expected = sorted((i*d+k*s, i, k) for i in range(n+1) for k in range(r+1))
                for floor in range(0, expected[-1][0]+2):
                    found = []
                    pair = first_pair(d, s, n, r, floor)
                    while pair is not None:
                        found.append(pair)
                        pair = next_pair(d, s, n, r, pair)
                    assert found == [p for p in expected if p[0] >= floor]


def test_dense_native_and_elapsed_repeat_seek_to_equal_time_page(monkeypatch):
    from debacal.prepared_recurrence import PreparedSeries
    start = BASE.replace(year=1800)
    repeat = Alarm('dense', 'DISPLAY', 'PT0S', repeat=2_000_000_000, repeat_interval='PT15S')
    event = alarm_event(start=start, end=start+DAY, recurrence_rule='FREQ=DAILY', alarms=(repeat,))
    data = schedule(event)
    def no_enumeration(*args):
        raise AssertionError('Constant-stride pair seek enumerated rule starts')
    monkeypatch.setattr(PreparedSeries, '_regular', no_enumeration)
    due = next_triggers(data, BASE, BASE+timedelta(seconds=1), limit=4)
    assert [d.start for d in due] == [start+i*DAY for i in range(4)]
    assert all(d.key.trigger_utc == BASE for d in due)


def test_custom_zone_restart_and_canonical_end_on_old_preparation():
    from debacal.timezones import resolve_timezone
    identifier = 'Resource/Test'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\nDTSTART:19700101T000000\n'
                  'TZOFFSETFROM:+0330\nTZOFFSETTO:+0330\nEND:STANDARD\nEND:VTIMEZONE\n')
    custom = resolve_timezone(identifier, {identifier: definition})
    start = datetime(2026, 3, 1, 9, tzinfo=custom)
    berlin = ZoneInfo('Europe/Berlin')
    end = (start+timedelta(hours=1)).astimezone(berlin)
    alarm = Alarm('end', 'DISPLAY', '-P1D', related='END')
    event = alarm_event(start=start, end=end, timezone_id=identifier, timezone_definitions={identifier: definition},
                        recurrence_rule='FREQ=DAILY', alarms=(alarm,))
    old = replace(event, end=end.astimezone(custom), alarms=None)
    prepared = EventRangeIndex.prepare(old)
    due = next_triggers(schedule(event, prepared=prepared), start.replace(day=28), start.replace(day=31))
    target = [d for d in due if d.start.date().day == 29][0]
    assert target.end.tzinfo == berlin
    assert target.start.utcoffset() == timedelta(hours=3, minutes=30)
    assert target.key.trigger_utc == alarm_trigger(alarm, target.start, target.end)


def test_actual_civil_midnight_for_both_all_day_bounds():
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 29, tzinfo=UTC)
    alarms = (Alarm('begin','DISPLAY','PT0S'), Alarm('end','DISPLAY','PT0S', related='END'))
    event = alarm_event(start=start, end=start+DAY, all_day=True, alarms=alarms)
    due = next_triggers(schedule(event, zone=zone), start-DAY, start+2*DAY)
    assert [d.key.trigger_utc for d in due] == [start-timedelta(hours=1), start+timedelta(hours=22)]


def test_generation_context_recovered_from_canonical_changes_dst_rule_starts():
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 1, 9, tzinfo=zone)
    event = alarm_event(start=start, end=start+timedelta(hours=1), timezone_id=zone.key,
                        recurrence_rule='FREQ=DAILY', alarms=(Alarm('begin','DISPLAY','PT0S'),))
    old = replace(event, start=start.astimezone(UTC), end=event.end.astimezone(UTC), timezone_id='', alarms=None)
    due, = next_triggers(schedule(event, prepared=EventRangeIndex.prepare(old)),
                         start.replace(month=4), start.replace(month=4, day=2))
    assert due.start.hour == 9 and due.key.trigger_utc.hour == 7


def test_dense_clock_guard_seeks_selected_rank_without_dropping_trigger():
    numbers = lambda n: ','.join(map(str, range(n)))
    rule = f'FREQ=DAILY;BYHOUR={numbers(24)};BYMINUTE={numbers(60)};BYSECOND={numbers(42)}'
    event = alarm_event(recurrence_rule=rule, alarms=(Alarm('now', 'DISPLAY', 'PT0S'),))
    change = SourceChange(1, event.id, event, EventRangeIndex.prepare(event))
    data = compile_schedule(change, CalendarReminderPolicy(), zone=UTC)
    when = BASE.replace(year=2050, hour=23, minute=55, second=40)
    found = next_triggers(data, when, when+timedelta(minutes=1), limit=4)
    assert [d.key.trigger_utc for d in found] == [when+timedelta(seconds=s) for s in (0,1,20,21)]


def test_rank_fallback_matches_native_sparse_dst_gap_and_setpos(monkeypatch):
    from debacal.prepared_recurrence import PreparedSeries
    from debacal.recurrence import RecurrenceRangeError
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 1, 1, 30, tzinfo=zone)
    rules = ('FREQ=DAILY;BYHOUR=1,2,3;BYMINUTE=0,30;COUNT=350',
             'FREQ=WEEKLY;BYDAY=SU,MO;BYHOUR=2,3;BYSETPOS=1,-1;COUNT=350',
             'FREQ=HOURLY;BYMINUTE=0,30;BYMONTH=3;COUNT=3000',
             'FREQ=MONTHLY;BYDAY=SU;BYHOUR=2,3;BYSETPOS=-2,-1;COUNT=350')
    queries = []
    for rule in rules:
        event = alarm_event(start=start, end=start+timedelta(hours=1), timezone_id=zone.key,
                            recurrence_rule=rule, alarms=(Alarm('a','DISPLAY','PT0S'),))
        data = schedule(event)
        lower, upper = start.replace(day=28), start.replace(day=31)
        expected = next_triggers(data, lower, upper)
        assert expected
        queries.append((data, lower, upper, expected))
    def guarded(*args):
        raise RecurrenceRangeError('Synthetic agenda budget')
    monkeypatch.setattr(PreparedSeries, '_regular', guarded)
    for data, lower, upper, expected in queries:
        assert next_triggers(data, lower, upper) == expected


def test_redundant_hourly_clock_selectors_use_fixed_pair_seek(monkeypatch):
    from debacal.prepared_recurrence import PreparedSeries
    start = BASE.replace(year=1980)
    alarm = Alarm('a', 'DISPLAY', 'PT0S', repeat=2_000_000_000, repeat_interval='PT1S')
    event = alarm_event(start=start, end=start+DAY, recurrence_rule='BYMINUTE=0;BYSECOND=0;FREQ=HOURLY',
                        alarms=(alarm,))
    data = schedule(event)
    def forbidden(*_):
        raise AssertionError('Enumerated historic hourly starts')
    monkeypatch.setattr(PreparedSeries, '_regular', forbidden)
    actual = next_triggers(data, BASE, BASE+timedelta(seconds=1), limit=2)
    assert [d.start for d in actual] == [start, start+timedelta(hours=1)]


def test_master_absolute_uses_bound_civil_range():
    zone = ZoneInfo('America/New_York')
    alarm = Alarm('abs','DISPLAY','20261121T090000Z')
    for civil, start, end in (
            ({'floating_time': True}, BASE, BASE+timedelta(hours=1)),
            ({'all_day': True}, BASE.replace(hour=0), BASE.replace(hour=0)+DAY)):
        event = alarm_event(start=start, end=end, alarms=(alarm,), **civil)
        due, = next_triggers(schedule(event, zone=zone), BASE, BASE+DAY)
        assert due.start == start.replace(tzinfo=zone) and due.end == end.replace(tzinfo=zone)
        assert due.key.trigger_utc == BASE and due.key.occurrence_key is None

def test_single_effective_hourly_clock_can_differ_from_dtstart(monkeypatch):
    from debacal.prepared_recurrence import PreparedSeries
    start = BASE.replace(year=1980)
    alarm = Alarm('a','DISPLAY','PT0S',repeat=2_000_000_000,repeat_interval='PT1S')
    event = alarm_event(start=start, end=start+DAY,
                        recurrence_rule='BYMINUTE=7,12;BYSETPOS=-1;FREQ=HOURLY', alarms=(alarm,))
    data = schedule(event)
    def forbidden(*_):
        raise AssertionError('Expanded a singleton-clock native stream')
    monkeypatch.setattr(PreparedSeries, '_regular', forbidden)
    due = next_triggers(data, BASE, BASE+timedelta(seconds=1), limit=3)
    assert [d.start for d in due] == [start, start+timedelta(minutes=12), start+timedelta(minutes=72)]
