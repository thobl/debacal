from contextlib import closing
from dataclasses import replace
from datetime import timedelta, timezone

from alarm_support import BASE, alarm_event
from debacal.alarms import Alarm
from debacal.reminder_owner import ReminderOwner
from debacal.reminder_preferences import CalendarReminderPolicy
from debacal.reminder_schedule import compile_schedule
from debacal.reminder_store import ReminderStore
from debacal.store import Store


def put(store, item):
    store.put_source(item, compile_schedule(item, CalendarReminderPolicy(), zone=timezone.utc),
                     cursor=(item.version, item.event_id))


def test_restart_claim_and_owner_handoff(tmp_path):
    db = str(tmp_path / 'profile.sqlite3')
    source = alarm_event(calendar_id='local', alarms=tuple(
        Alarm(f'id{i:03}', 'DISPLAY', 'PT0S', uid=f'id{i:03}') for i in range(140)))
    owner = ReminderOwner(db)
    with Store(db) as canonical:
        canonical.apply_remote([source])
        assert owner.acquire()
        try:
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=BASE)
                (item,) = store.read_sources('scan')
                put(store, item)
                first = store.claim_due(BASE, limit=13)
                assert len(first) == 13
                for due in first[:-1]:
                    store.record_delivery(due.key, desktop_id=None, unread=True)
            with closing(ReminderStore(db)) as reader:
                assert reader.inbox(limit=1)[0].status in ('pending', 'delivered')
                assert reader.unread_count() == 12
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=BASE)
                assert sum(x.status == 'uncertain' for x in store.inbox()) == 1
                rest = []
                while store.next_wakeup() is not None:
                    rest.extend(store.claim_due(BASE, limit=11))
                assert [d.key.alarm_key for d in (*first, *rest)] == [f'id{i:03}' for i in range(140)]
                assert len(store.inbox(limit=80, offset=80)) == 60
        finally:
            owner.release()


def test_snooze_metadata_and_legacy_alarm_edit(tmp_path):
    db = str(tmp_path / 'profile')
    owner = ReminderOwner(db)
    trigger = Alarm('wire', 'DISPLAY', 'PT0S')
    source = alarm_event(calendar_id='local', alarms=(trigger,))
    with Store(db) as canonical, closing(ReminderStore(db)) as store:
        canonical.apply_remote([source])
        assert owner.acquire()
        try:
            store.attach_owner(owner, now=BASE-timedelta(hours=1))
            def update(event):
                canonical.apply_remote([event])
                put(store, store.read_sources('scan')[0])
            update(source)
            reminder, = store.claim_due(BASE)
            store.record_delivery(reminder.key, desktop_id=1, unread=True)
            later = BASE + timedelta(minutes=5)
            store.snooze(reminder.key, later)
            update(replace(source, title='Renamed', end=source.end+timedelta(hours=1),
                           alarms=(replace(trigger, key='reformatted', trigger='+PT0S'),)))
            snoozed, = store.claim_due(later)
            assert snoozed.title == 'Renamed' and snoozed.key == reminder.key
            store.record_delivery(snoozed.key, desktop_id=2)
            update(replace(source, alarms=(replace(trigger, key='shifted', trigger='-PT1M'),)))
            assert not store.claim_due(later)
            update(replace(source, alarms=(replace(trigger, key='future', trigger='PT10M'),)))
            future, = store.claim_due(BASE+timedelta(minutes=10))
            assert future.key.trigger_utc == BASE+timedelta(minutes=10)
        finally:
            owner.release()


def test_restart_with_known_old_wal_and_overlapping_revision(tmp_path):
    import sqlite3
    import pytest
    from pathlib import Path

    path = tmp_path / 'profile'
    db = str(path)
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        with Store(path) as canonical:
            source = alarm_event(calendar_id='local', alarms=(Alarm('a', 'DISPLAY', 'PT0S'),))
            canonical.apply_remote([source])
            new = tmp_path / 'new'
            with Store(new) as replacement:
                replacement.apply_remote([replace(source, id='new', ical_uid='new')])
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=BASE)
                put(store, store.read_sources('scan')[0])
                new.replace(path)
                with pytest.raises(sqlite3.OperationalError, match='WAL'):
                    store.claim_due(BASE)
            with closing(ReminderStore(db)) as store:
                with pytest.raises(sqlite3.OperationalError, match='WAL'):
                    store.attach_owner(owner, now=BASE)
                assert not store.inbox()
        # Supply a completely coherent set; never modify a live retained WAL.
        with Store(tmp_path / 'clean') as clean:
            clean.apply_remote([replace(source, id='fresh', ical_uid='fresh')])
        for suffix in ('', '-wal', '-shm'):
            old = Path(db + suffix)
            if old.exists():
                old.rename(tmp_path / ('retired' + suffix))
        (tmp_path / 'clean').rename(path)
        with closing(ReminderStore(db)) as store:
            store.attach_owner(owner, now=BASE+timedelta(hours=1))
            assert not store.claim_due(BASE+timedelta(hours=1))
            assert store.cursor is None
    finally:
        owner.release()


def test_zone_change_selects_only_civil_sources_and_rebinds(tmp_path):
    from zoneinfo import ZoneInfo
    db = str(tmp_path/'zones')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        floating = alarm_event(calendar_id='local', floating_time=True, alarms=(Alarm('a','DISPLAY','PT0S'),))
        fixed = replace(floating, id='fixed', ical_uid='fixed', floating_time=False)
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([fixed, floating])
            store.attach_owner(owner, now=BASE-timedelta(hours=12))
            for item in store.read_sources('scan'):
                put(store, item)
            zone = ZoneInfo('America/New_York')
            selected = store.rebind_sources(zone)
            assert [c.event_id for c in selected] == [floating.id]
            for item in selected:
                store.put_source(item, compile_schedule(item, CalendarReminderPolicy(), zone=zone))
            assert not store.rebind_sources(zone)
            assert [d.key.source_id for d in store.claim_due(BASE)] == ['fixed']
            due, = store.claim_due(BASE+timedelta(hours=5))
            assert due.start.hour == BASE.hour and due.start.tzinfo == zone
    finally:
        owner.release()


def test_ambiguous_legacy_and_new_distinct_alarm(tmp_path):
    db = str(tmp_path/'ambiguous')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        old = tuple(Alarm(k,'DISPLAY',offset) for k,offset in (('a','PT0S'),('b','-PT1M')))
        source = alarm_event(calendar_id='local', alarms=old)
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE-timedelta(hours=1))
            put(store, store.read_sources('scan')[0])
            for due in store.claim_due(BASE):
                store.record_delivery(due.key, desktop_id=None)
            changed = tuple(Alarm(k,'DISPLAY',offset) for k,offset in (('c','-PT3M'),('d','PT1M')))
            writer.apply_remote([replace(source, alarms=changed)])
            put(store, store.read_sources('scan')[0])
            assert not store.claim_due(BASE)
            assert [d.key.alarm_key for d in store.claim_due(BASE+timedelta(minutes=1))] == ['d']
            writer.apply_remote([replace(source, alarms=(*changed, Alarm('added','DISPLAY','-PT5M')))])
            put(store, store.read_sources('scan')[0])
            assert [d.key.alarm_key for d in store.claim_due(BASE+timedelta(minutes=1))] == ['added']
    finally:
        owner.release()


def test_stale_callbacks_and_pending_repeats_after_dismissal(tmp_path):
    db = str(tmp_path/'callback')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        source = alarm_event(calendar_id='local',
                             alarms=(Alarm('a','DISPLAY','PT0S', repeat=2, repeat_interval='PT1M'),))
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            batch = store.claim_due(BASE+timedelta(minutes=1))
            assert len(batch) == 2
            fences = [store.claim_fence(d.key) for d in batch]
            assert all(store.can_emit(d, f) for d, f in zip(batch, fences))
            store.dismiss(batch[0].key)
            assert not any(store.can_emit(d, f) for d, f in zip(batch, fences))
            store.record_delivery(batch[1].key, desktop_id=7, unread=True, fence=fences[1])
            assert not store.unread_count()
            assert not store.claim_due(BASE+timedelta(minutes=2))
    finally:
        owner.release()


def test_variable_nominal_backfill_yields_and_survives_restart(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from debacal import reminder_schedule
    zone = ZoneInfo('America/New_York')
    start = datetime(2000, 1, 1, 2, 30, tzinfo=zone)
    repeat = Alarm('a','DISPLAY','PT0S', repeat=12000, repeat_interval='P1DT1H')
    source = alarm_event(calendar_id='local', start=start, end=start+timedelta(hours=1), alarms=(repeat,))
    from debacal.alarms import alarm_trigger
    due_at = alarm_trigger(repeat, source.start, source.end, repeat_index=9000)
    db = str(tmp_path/'repeat')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        with Store(db) as writer:
            writer.apply_remote([source])
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=due_at)
                put(store, store.read_sources('scan')[0])
                assert not store.claim_due(due_at, limit=1)
                assert store.next_wakeup() == due_at
            calls = 0
            advance = reminder_schedule._advance
            def count_steps(*args):
                nonlocal calls
                calls += 1
                return advance(*args)
            monkeypatch.setattr(reminder_schedule, '_advance', count_steps)
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=due_at)
                for _ in range(150):
                    page = store.claim_due(due_at, limit=1)
                    if page:
                        break
                due, = page
                assert due.key.repeat_index == 9000
                assert 0 < calls < 9000
    finally:
        owner.release()


def test_abandoned_bootstrap_page_and_emit_are_fenced_by_partition(tmp_path):
    import sqlite3
    import pytest
    db = str(tmp_path/'bound')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        source = alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S'),))
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            due, = store.claim_due(BASE)
            fence = store.claim_fence(due.key)
            assert not store.read_sources('scan', after_id='z')
            writer.connection.execute("UPDATE database_incarnation SET token='new-profile'")
            writer.connection.commit()
            assert not store.can_emit(due, fence)
            with pytest.raises(sqlite3.OperationalError, match='bootstrap'):
                store.finish_bootstrap()
            assert store.bootstrap is not None and not store.inbox()
    finally:
        owner.release()


def test_resume_very_old_dense_train_seeks_last_day_without_enumeration(tmp_path):
    db = str(tmp_path/'resume')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        source = alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S',
                                                                repeat=2_000_000_000, repeat_interval='PT1S'),))
        later = BASE + timedelta(days=7000)
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            due, = store.claim_due(later, limit=1)
            assert due.key.trigger_utc == later-timedelta(days=1)
    finally:
        owner.release()


def test_slow_backfill_leaves_another_source_eligible_in_same_slice(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('America/New_York')
    old = datetime(1990, 1, 1, 9, tzinfo=zone)
    slow = alarm_event(id='a', ical_uid='a', calendar_id='local', start=old, end=old+timedelta(hours=1),
                       alarms=(Alarm('slow','DISPLAY','PT0S',repeat=20000,repeat_interval='P1DT1S'),))
    fast = alarm_event(id='b', ical_uid='b', calendar_id='local', alarms=(Alarm('now','DISPLAY','PT0S'),))
    db = str(tmp_path/'fair')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([slow, fast])
            store.attach_owner(owner, now=BASE)
            for item in store.read_sources('scan'):
                put(store, item)
            assert [due.key.source_id for due in store.claim_due(BASE, limit=1)] == ['b']
    finally:
        owner.release()


def test_same_marker_snapshot_replacement_and_in_place_revision_rollback(tmp_path):
    import sqlite3
    db = str(tmp_path/'identity')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        source = alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S'),))
        with Store(db) as writer:
            writer.apply_remote([source])
            with sqlite3.connect(tmp_path/'snapshot') as copy:
                writer.connection.backup(copy)
            copy.close()
            with closing(ReminderStore(db)) as store:
                store.attach_owner(owner, now=BASE)
                put(store, store.read_sources('scan')[0])
                due, = store.claim_due(BASE)
                store.record_delivery(due.key, desktop_id=4)
                writer.connection.execute('UPDATE event_revisions SET version=0')
                writer.connection.commit()
                assert not store.claim_due(BASE)
                assert not store.inbox()
                put(store, store.read_sources('scan')[0])
                assert store.claim_due(BASE)
        (tmp_path/'snapshot').replace(tmp_path/'identity')
        with closing(ReminderStore(db)) as store:
            store.attach_owner(owner, now=BASE+timedelta(days=1))
            assert not store.inbox() and not store.claim_due(BASE+timedelta(days=1))
    finally:
        owner.release()


def test_scoped_timezone_table_is_shared_by_repeat_frontier_and_inbox(tmp_path):
    import sqlite3
    from debacal.timezones import resolve_timezone
    identifier = 'Resource/Shared'
    definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\n'
                  'BEGIN:STANDARD\nDTSTART:16010101T030000\nTZOFFSETFROM:+0200\n'
                  'TZOFFSETTO:+0100\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\nEND:STANDARD\n'
                  'BEGIN:DAYLIGHT\nDTSTART:16010101T020000\nTZOFFSETFROM:+0100\n'
                  'TZOFFSETTO:+0200\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\nEND:DAYLIGHT\nEND:VTIMEZONE\n')
    zone = resolve_timezone(identifier, {identifier: definition})
    start = BASE.astimezone(zone)
    event = alarm_event(calendar_id='local', start=start, end=start+timedelta(hours=1), timezone_id=identifier,
                        timezone_definitions={identifier: definition}, alarms=(Alarm('a','DISPLAY','PT0S',
                                                                                   repeat=3, repeat_interval='PT1S'),))
    db = str(tmp_path/'shared')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([event])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            assert len(store.claim_due(BASE+timedelta(seconds=1))) == 2
            with sqlite3.connect(db+'.reminders.sqlite3') as read:
                assert read.execute('SELECT max(length(payload)) FROM ledger').fetchone()[0] < 2000
                assert read.execute('SELECT max(length(state)) FROM frontier').fetchone()[0] < 1000
            read.close()
        with closing(ReminderStore(db)) as store:
            assert all(entry.reminder.start.tzinfo.identifier == identifier for entry in store.inbox())
    finally:
        owner.release()


def test_read_only_inbox_and_actions_when_off_never_open_source(tmp_path, monkeypatch):
    from debacal import reminder_partition
    db = str(tmp_path/'off')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S'),))])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            due, = store.claim_due(BASE)
            store.record_delivery(due.key, desktop_id=None, unread=True)
            partition = store.partition_id
        def forbidden(*args, **kwargs):
            raise AssertionError('Disabled inbox opened the canonical database')
        monkeypatch.setattr(reminder_partition, 'ReminderSources', forbidden)
        with closing(ReminderStore(db)) as inbox:
            assert inbox.unread_count() == 1
            inbox.mark_read((due.key,))
            inbox.snooze(due.key, BASE+timedelta(hours=1), partition=partition)
            inbox.dismiss(due.key, partition='old queued UI action')
            assert inbox.inbox()[0].status == 'snoozed'
            inbox.dismiss(due.key, partition=partition)
            assert inbox.inbox()[0].status == 'dismissed'
    finally:
        owner.release()


def test_final_emit_rechecks_source_calendar_and_generation(tmp_path):
    db = str(tmp_path/'emission')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        source = alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S'),))
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            due, = store.claim_due(BASE)
            fence = store.claim_fence(due.key)
            assert store.can_emit(due, fence)
            store.set_context(now=BASE, generation=1)
            assert not store.can_emit(due, fence)
            store.set_context(now=BASE, generation=0)
            writer.connection.execute("UPDATE calendars SET data=json_set(data,'$.enabled',0) WHERE id='local'")
            writer.connection.commit()
            assert not store.can_emit(due, fence)
            writer.connection.execute("UPDATE calendars SET data=json_set(data,'$.enabled',1) WHERE id='local'")
            writer.connection.commit()
            assert not store.can_emit(due, fence)
            writer.apply_remote([replace(source, title='Changed')])
            assert not store.can_emit(due, fence)
    finally:
        owner.release()


def test_metadata_after_future_move_keeps_the_already_eligible_new_trigger(tmp_path):
    db = str(tmp_path/'move')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        alarm = Alarm('a','DISPLAY','PT0S',uid='a')
        source = alarm_event(calendar_id='local', alarms=(alarm,))
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([source])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            old, = store.claim_due(BASE)
            store.record_delivery(old.key, desktop_id=None)
            shifted = replace(source, alarms=(replace(alarm, trigger='PT5M'),))
            writer.apply_remote([shifted])
            put(store, store.read_sources('scan')[0])
            later = BASE+timedelta(minutes=6)
            store.set_context(now=later, generation=0)
            writer.apply_remote([replace(shifted, title='New title')])
            put(store, store.read_sources('scan')[0])
            due, = store.claim_due(later)
            assert due.key.trigger_utc == BASE+timedelta(minutes=5)
            fence = store.claim_fence(due.key)
            store.dismiss(old.key)
            assert store.can_emit(due, fence)
    finally:
        owner.release()


def test_converting_fired_standalone_to_series_keeps_journal_progress(tmp_path):
    db = str(tmp_path/'convert')
    owner = ReminderOwner(db)
    assert owner.acquire()
    try:
        event = alarm_event(calendar_id='local', alarms=(Alarm('a','DISPLAY','PT0S'),))
        with Store(db) as writer, closing(ReminderStore(db)) as store:
            writer.apply_remote([event])
            store.attach_owner(owner, now=BASE)
            put(store, store.read_sources('scan')[0])
            old, = store.claim_due(BASE)
            store.record_delivery(old.key, desktop_id=None)
            writer.apply_remote([replace(event, recurrence_rule='FREQ=DAILY')])
            item, = store.read_sources('changes', store.cursor)
            put(store, item)
            assert store.cursor == (item.version, item.event_id)
    finally:
        owner.release()
