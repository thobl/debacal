"""Cross-component reminder workflows with synthetic calendars and transports."""
from dataclasses import replace
from datetime import timedelta

import pytest
from PySide6.QtWidgets import QApplication

from alarm_support import BASE, FakeClock, wait_for
from debacal.ics import parse_ics
from debacal.import_dialog import ImportDialog
from debacal.models import Calendar
from debacal.recurrence import occurrence
from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
from debacal.store import Store
from debacal.ui import MainWindow
from test_import_reminders import WIRE
from test_import_requests import accept_request
from test_reminder_controller import MINUTE, event, running
from test_reminder_editor import ABS, CALS, EventDialog, alarm_event, release_widget
from test_sync import setup, synchronize


def test_manual_remove_history_remote_template_upload_and_redownload(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    store, transport, engine = setup(tmp_path)
    wire = WIRE[:WIRE.rindex(b'BEGIN:VEVENT')] + b'END:VCALENDAR'
    try:
        resource = transport.put(transport.new_href('family'), wire)
        assert not [message for message in synchronize(engine) if message.kind == 'error']
        before, = store.list_events(calendar_ids=['remote'])
        assert before.alarms[0].action == 'EMAIL'
        window = MainWindow(store, enable_sync=False, delivery_allowed=False)
        def review(dialog):
            wait_for(lambda: dialog.preview_ready)
            dialog.reminder_mode.setCurrentIndex(dialog.reminder_mode.findData('remove'))
            return accept_request(dialog)
        monkeypatch.setattr(ImportDialog, 'exec', review)
        try:
            assert window.import_data(wire, calendar_id='remote')
            window._process_import_request()
            wait_for(lambda: not window._import_jobs)
            assert store.get_event(before.id).alarms == ()
            operation = store.undo()
            assert operation and store.get_event(before.id).alarms == before.alarms
            assert store.redo(operation) == operation
            assert store.get_event(before.id).alarms == ()
            assert not [message for message in synchronize(engine) if message.kind == 'error']
            uploaded = transport.get(resource.href)
            assert uploaded.etag != resource.etag
            assert 'BEGIN:VALARM' not in uploaded.data and 'X-INVITATION:blue' in uploaded.data
            transport.put(uploaded.href, uploaded.data.replace('SUMMARY:Detached', 'SUMMARY:Revised'), etag=uploaded.etag)
            assert not [message for message in synchronize(engine) if message.kind == 'error']
            downloaded = store.get_event(before.id)
            assert downloaded.alarms == () and not store.pending_changes()
            assert next(iter(downloaded.recurrence_overrides.values()))['title'] == 'Revised'
        finally:
            release_widget(window)
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize('minutes', [-20, 30])
@pytest.mark.parametrize('rule,previous_days,next_days', [
    ('FREQ=DAILY;COUNT=4', 1, 1), ('FREQ=DAILY', 1, 1),
    ('FREQ=WEEKLY;BYDAY=FR,SA', 1, 6), ('FREQ=MONTHLY', 31, 30), ('FREQ=YEARLY', 365, 365)])
def test_actual_all_series_move_after_delivery_and_owner_restart(tmp_path, minutes, rule, previous_days, next_days):
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - 90*MINUTE)
    source = replace(event('series', BASE - timedelta(days=previous_days), recurrence_rule=rule), end=BASE - timedelta(days=previous_days) + timedelta(hours=2))
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with running(path, clock) as (owner, transport, page):
            assert not page()
            clock.time = BASE
            owner.reload_settings()
            wait_for(lambda: transport.sent)
            first = transport.sent[0][0]
            transport.delivered.emit(first.key, 1)
            wait_for(lambda: page()[0].status == 'delivered')
            root = occurrence(source, BASE)
            writer.move_selection([root], 'group', root, root.start + minutes*MINUTE, root.end + minutes*MINUTE)
            moved = writer.get_event(source.id)
            assert moved.start == source.start + minutes*MINUTE
            writer.apply('Change title', [replace(moved, title='Rescheduled')])
        with running(path, clock) as (next_owner, transport, page):
            wait_for(lambda: transport.started)
            assert len(page()) == 1
            clock.time = BASE + minutes*MINUTE + (timedelta() if minutes > 0 else timedelta(days=next_days))
            next_owner.reload_settings()
            wait_for(lambda: transport.sent)
            assert [(due.title, due.key.trigger_utc) for due, *_ in transport.sent] == [('Rescheduled', clock.time)]


def test_new_absolute_reminder_uses_current_event_date():
    dlg = EventDialog(CALS, BASE, event=alarm_event(alarms=(ABS,)))
    try:
        moved = dlg.start.value() + timedelta(days=30)
        dlg.start.set_value(moved)
        dlg.start.editingFinished.emit()
        dlg.reminders.add_button.click()
        form = dlg.reminders.alarm_dialog
        form.kind.setCurrentData('absolute')
        assert form.absolute.value() == moved
        form.reject()
        dlg.reminders.edit_buttons[0].click()
        dlg.reminders.alarm_dialog.save()
        assert dlg.reminders.alarms() == (ABS,) and dlg.alarm_edit() is None
    finally:
        release_widget(dlg)


def test_visible_navigation_and_inbox_while_source_edit_waits_for_sync_writer(tmp_path):
    from test_reminder_dialog import workspace
    from test_ui_database_actions import held_writer
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - MINUTE)
    source = event('series', BASE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with workspace(path, clock, writer) as (window, _, page):
            window.show()
            assert not page()
            with held_writer(path):
                window.apply_events('Edit reminder source', [replace(source, title='Revised')])
                assert window.database_commands.pending
                window.open_day(BASE.date())
                window.show_reminders()
                assert window._reminder_dialog.isVisible()
                assert window.selected_day == BASE.date() and not page()
            wait_for(lambda: not window.database_commands.pending)
            clock.time = BASE
            window.reminder_controller.reload_settings()
            wait_for(lambda: any(row.reminder.title == 'Revised' for row in page()))



def test_past_all_move_catches_up_only_previously_undisposed_alarm(tmp_path):
    from test_reminder_controller import ALARM
    path = tmp_path/'calendar'
    clock = FakeClock(BASE - 90*MINUTE)
    source = replace(event('series', BASE, ALARM, replace(ALARM, key='later', uid='later', trigger='PT10M'),
                           recurrence_rule='FREQ=DAILY'), end=BASE + 60*MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with running(path, clock) as (owner, transport, page):
            assert not page()
            clock.time = BASE
            owner.reload_settings()
            wait_for(lambda: transport.sent)
            first = transport.sent[0][0]
            transport.delivered.emit(first.key, 1)
            wait_for(lambda: page()[0].status == 'delivered')
            root = occurrence(source, BASE)
            writer.move_selection([root], 'group', root, root.start - 20*MINUTE, root.end - 20*MINUTE)
            owner.reload_settings()
            wait_for(lambda: len(transport.sent) == 2)
            assert [(item.key.alarm_key, item.key.trigger_utc) for item, *_ in transport.sent] == [
                ('alarm', BASE), ('later', BASE - 10*MINUTE)]


def test_following_split_and_confirmed_alternative_deliver_to_one_owner(tmp_path):
    from test_reminder_controller import ALARM
    path = tmp_path/'calendar'
    clock = FakeClock(BASE - MINUTE)
    absolute = replace(ALARM, key='once', uid='once', trigger='20261123T090000Z')
    source = event('series', BASE, ALARM, absolute, candidate=True, recurrence_rule='FREQ=DAILY;COUNT=4')
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with running(path, clock) as (owner, delivery, page), running(path, clock) as (peer, other, peer_page):
            assert not page() and not peer_page()
            clock.time = BASE
            owner.reload_settings()
            wait_for(lambda: delivery.sent)
            first = delivery.sent[0][0]
            delivery.delivered.emit(first.key, 1)
            wait_for(lambda: page()[0].status == 'delivered')
            pivot = occurrence(source, BASE + timedelta(days=1))
            writer.edit_future(pivot, replace(writer.future_snapshot(pivot), title='Following'))
            head, tail = sorted(writer.list_events(), key=lambda e: e.start)
            assert [a.uid for a in head.alarms] == ['alarm', 'once']
            assert [a.trigger for a in tail.alarms] == ['PT0S']
            selected, = writer.query_occurrences(BASE + timedelta(days=2), BASE + timedelta(days=3))
            writer.confirm_candidate(selected)
            assert not writer.get_event(tail.id).recurring
            clock.time = selected.start
            owner.reload_settings()
            peer.reload_settings()
            wait_for(lambda: len(delivery.sent) >= 3)
            assert sorted((d.key.source_id, d.key.alarm_key) for d, *_ in delivery.sent[1:]) == sorted([
                (head.id, 'once'), (tail.id, writer.get_event(tail.id).alarms[0].key)])
            assert len(peer_page()) == 3 and not other.sent


def test_successive_all_moves_preserve_the_same_delivered_occurrence(tmp_path):
    from debacal.reminder_worker import ReminderWorker
    from alarm_support import FakeTransport
    from debacal.reminder_reveal import reminder_target
    path = str(tmp_path/'calendar')
    clock = FakeClock(BASE - 90*MINUTE)
    worker = ReminderWorker(path, clock, FakeTransport(), True, 'Example', lambda *_: None)
    def drain():
        for _ in range(100):
            if worker.tick() >= 1:
                break
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        master = replace(event('series', BASE, recurrence_rule='FREQ=DAILY'), end=BASE+60*MINUTE)
        writer.apply_remote([master])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        try:
            drain()
            clock.time = BASE
            drain()
            assert len(worker.store.inbox()) == 1
            original = worker.store.inbox()[0].reminder.key
            for minutes in (-10, -10, 4, -5):
                master = writer.get_event(master.id)
                root = occurrence(master, master.start)
                writer.move_selection([root], 'group', root, root.start+minutes*MINUTE, root.end+minutes*MINUTE)
                drain()
                assert [row.reminder.key for row in worker.store.inbox()] == [original]
                moved = writer.get_event(master.id)
                writer.apply('Metadata', [replace(moved, title='Edited')])
                worker.release()
                worker = ReminderWorker(path, clock, FakeTransport(), True, 'Example', lambda *_: None)
                drain()
                opened = worker.store.open_entry(original, partition=worker.store.partition_id)
                assert opened is not None
                _, resolved, _ = reminder_target(path, *opened)
                assert resolved.start == moved.start
        finally:
            worker.release()

@pytest.mark.parametrize('rule', ['FREQ=MONTHLY;BYHOUR=8,9', 'FREQ=YEARLY;BYHOUR=8,9'])
def test_date_only_all_move_keeps_independent_hourly_slots(tmp_path, rule):
    from alarm_support import FakeTransport
    from debacal.reminder_reveal import reminder_target
    from debacal.reminder_worker import ReminderWorker
    from test_reminder_controller import ALARM

    path = str(tmp_path / 'calendar')
    first = BASE - 60*MINUTE
    clock = FakeClock(first - 2*timedelta(days=1))
    worker = ReminderWorker(path, clock, FakeTransport(), True, 'Example', lambda *_: None)

    def drain():
        for _ in range(100):
            if worker.tick() >= 1:
                return
        pytest.fail('Reminder worker did not settle')

    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        later = replace(ALARM, key='later', uid='later', trigger='PT2H')
        master = replace(event('hours', first, ALARM, later, recurrence_rule=rule), end=first + 2*timedelta(days=1))
        writer.apply_remote([master])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        try:
            drain()
            clock.time = BASE
            drain()
            receipts = [row.reminder.key for row in worker.store.inbox()]
            assert {key.trigger_utc for key in receipts} == {first, BASE}
            root = occurrence(master, master.start)
            shift = timedelta(days=1)
            writer.move_selection([root], 'group', root, root.start-shift, root.end-shift)
            drain()
            keys = {row.reminder.key for row in worker.store.inbox()}
            assert {key for key in keys if key.alarm_key == 'alarm'} == set(receipts)
            assert {key.trigger_utc for key in keys if key.alarm_key == 'later'} == {
                stamp-shift+120*MINUTE for stamp in (first, BASE)}
            for key in receipts:
                opened = worker.store.open_entry(key, partition=worker.store.partition_id)
                assert opened is not None
                _, current, _ = reminder_target(path, *opened)
                assert current.start == key.trigger_utc-shift
        finally:
            worker.release()


@pytest.fixture
def native_history(tmp_path):
    from alarm_support import FakeTransport
    from debacal.reminder_worker import ReminderWorker

    path = str(tmp_path / 'calendar')
    clock = FakeClock(BASE)
    worker = ReminderWorker(path, clock, FakeTransport(), True, 'Example', lambda *_: None)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))

        def enable(master, now):
            clock.time = now
            writer.apply_remote([master])
            save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=now)
            drain()

        def drain():
            for _ in range(1000):
                if worker.tick() >= 1:
                    return
            pytest.fail('Reminder worker did not settle')

        def move(master, target):
            root = occurrence(master, master.start)
            writer.move_selection([root], 'group', root, target, target + (root.end - root.start))

        try:
            yield writer, clock, worker, enable, drain, move
        finally:
            worker.release()


@pytest.mark.parametrize('frequency', ['daily', 'finite_daily', 'finite_minutely'])
@pytest.mark.parametrize('ambiguous', [False, True])
def test_native_spring_gap_move_uses_original_nominal_member(native_history, ambiguous, frequency):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from debacal.reminder_reveal import reminder_target
    writer, clock, worker, enable, drain, move = native_history
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 3, 26, 2, 30, tzinfo=zone)
    rule = 'FREQ=DAILY;BYHOUR=2,3' if ambiguous else 'FREQ=DAILY'
    if frequency == 'finite_daily':
        rule += ';COUNT=12'
    elif frequency == 'finite_minutely':
        rule = 'FREQ=MINUTELY;INTERVAL=60;COUNT=150' if ambiguous else 'FREQ=MINUTELY;INTERVAL=1440;COUNT=12'
    master = event('gap', start, recurrence_rule=rule, timezone_id=zone.key)
    due = datetime(2026, 3, 29, 3, 30, tzinfo=zone)
    enable(master, due - MINUTE)
    clock.time = due
    drain()
    receipt, = worker.store.inbox()
    move(master, start + timedelta(days=1))
    drain()
    opened = worker.store.open_entry(receipt.reminder.key, partition=worker.store.partition_id)
    if ambiguous:
        assert opened is None
    else:
        assert opened is not None
        target = reminder_target(writer.path, *opened)
        assert target is not None
        assert target[1].start == datetime(2026, 3, 30, 2, 30, tzinfo=zone)


@pytest.mark.parametrize('prior_move', ['none', 'native', 'legacy'])
def test_removed_native_receipt_cannot_claim_or_open_a_new_monthly_root(native_history, prior_move):
    from datetime import datetime, timezone
    from test_reminder_controller import ALARM
    writer, clock, worker, enable, drain, move = native_history
    start = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    master = replace(event('series', start, replace(ALARM, uid=''), recurrence_rule='FREQ=DAILY'), end=start+60*MINUTE)
    due = start+timedelta(days=1)
    enable(master, due-60*MINUTE)
    clock.time = due
    drain()
    original, = worker.store.inbox()
    if prior_move != 'none':
        move(master, start-10*MINUTE)
        drain()
    if prior_move == 'legacy':
        # Install the previous sidecar format, including an independent
        # ambiguous UID-less rank recorded at the original delivered point.
        with worker.store._db as db:
            db.execute("INSERT INTO suppressed SELECT source,component,alarm,occurrence,rank,through "
                       "FROM rank_suppression WHERE kind='native'")
            db.execute("DELETE FROM rank_suppression")
            db.execute("INSERT INTO suppressed SELECT source,component,'ambiguous',occurrence,rank,stamp FROM ledger")
    changed = replace(writer.get_event(master.id), recurrence_rule='FREQ=MONTHLY')
    writer.apply('Monthly', [changed])
    drain()
    assert not writer.query_occurrences(due, due+MINUTE)
    move(writer.get_event(master.id), due-10*MINUTE)
    drain()
    if prior_move == 'legacy':
        assert worker.store._db.execute("SELECT alarm,kind FROM rank_suppression").fetchall() == [
            ('ambiguous', 'ambiguous')]
    keys = {entry.reminder.key for entry in worker.store.inbox()}
    assert {key.trigger_utc for key in keys} == {due, due-10*MINUTE}
    assert worker.store.open_entry(original.reminder.key, partition=worker.store.partition_id) is None
    if prior_move == 'none':
        # A new receipt can earn a later alarm on the very same original
        # coordinate, without reviving the retired receipt that first used it.
        move(writer.get_event(master.id), due)
        drain()
        current = writer.get_event(master.id)
        writer.apply('Later alarm', [replace(current, alarms=(replace(current.alarms[0], trigger='PT10M'),))])
        drain()
        clock.time = due+10*MINUTE
        drain()
        latest, = [row.reminder.key for row in worker.store.inbox() if row.reminder.key.trigger_utc == clock.time]
        assert latest.occurrence_key == original.reminder.key.occurrence_key
        assert worker.store.open_entry(latest, partition=worker.store.partition_id) is not None
        assert worker.store.open_entry(original.reminder.key, partition=worker.store.partition_id) is None


@pytest.mark.parametrize('interruption', ['clock', 'busy', 'preferences', 'metadata', 'new_geometry'])
def test_future_at_observation_rearms_after_staging_pages(native_history, interruption):
    import sqlite3
    from test_reminder_controller import ALARM
    writer, clock, worker, enable, drain, move = native_history
    alarms = [replace(ALARM, key=str(i), uid=str(i)) for i in range(40)]
    master = event('series', BASE, *alarms, recurrence_rule='FREQ=DAILY')
    enable(master, BASE-MINUTE)
    clock.time = BASE
    drain()
    assert len(worker.store.inbox()) == 40
    move(master, BASE+MINUTE)
    assert worker.tick() < 1
    assert worker.store._db.execute('SELECT count(*) FROM temp.staged_group').fetchone() == (32,)
    clock.time = BASE+MINUTE+timedelta(seconds=1)
    if interruption == 'preferences':
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app', sound='silent'),
                                  now=clock.now())
    if interruption in ('metadata', 'new_geometry'):
        current = writer.get_event(master.id)
        if interruption == 'metadata':
            writer.apply('Rename', [replace(current, title='Later description')])
        else:
            move(current, BASE+timedelta(seconds=30))
    if interruption == 'busy':
        assert worker.tick() < 1
        database = worker.store._db.execute('PRAGMA database_list').fetchone()[2]
        peer = sqlite3.connect(database)
        try:
            peer.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.OperationalError, match='locked|busy'):
                worker.tick()
        finally:
            peer.close()
    drain()
    if interruption == 'new_geometry':
        assert len(worker.store.inbox()) == 40
    else:
        assert {entry.reminder.key.alarm_key for entry in worker.store.inbox()
                if entry.reminder.key.trigger_utc == BASE+MINUTE} == {alarm.key for alarm in alarms}


@pytest.mark.parametrize('after', ['ordinary', 'exclude', 'detach'])
def test_explicit_dtstart_outside_rrule_retains_its_own_move_identity(native_history, after):
    from datetime import datetime, timezone
    from debacal.recurrence import recurrence_key
    from debacal.reminder_reveal import reminder_target
    writer, clock, worker, enable, drain, move = native_history
    start = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
    source = event('root', start, recurrence_rule='FREQ=WEEKLY;BYDAY=TU')
    enable(source, start-5*MINUTE)
    clock.time = start
    drain()
    original, = worker.store.inbox()
    move(source, start-MINUTE)
    current = writer.get_event(source.id)
    root_key = recurrence_key(current.start)
    if after == 'exclude':
        writer.apply('Exclude root', [replace(current, excluded_dates=(root_key,))])
    elif after == 'detach':
        detached = replace(current, title='Detached', recurrence_rule='', alarms=())
        writer.apply('Detach root', [replace(current, recurrence_overrides={root_key: detached.to_dict()})])
    drain()
    opened = worker.store.open_entry(original.reminder.key, partition=worker.store.partition_id)
    if after == 'ordinary':
        assert [row.reminder.key for row in worker.store.inbox()] == [original.reminder.key]
        assert opened and reminder_target(writer.path, *opened)[1].start == start-MINUTE
    else:
        assert opened is None


@pytest.mark.parametrize('block', ['calendar', 'mute'])
@pytest.mark.parametrize('edit', ['unchanged', 'move', 'exclude'])
def test_inactive_receipts_follow_canonical_occurrences_without_delivery(native_history, block, edit):
    from alarm_support import FakeTransport
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_reveal import reminder_target
    from debacal.reminder_worker import ReminderWorker
    writer, clock, worker, enable, drain, move = native_history
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    calendar = Calendar('c', 'Calendar', '#123456')
    from debacal.reminder_compilation import AlarmSchedule
    from test_reminder_controller import ALARM
    source = replace(event('daily', BASE, replace(ALARM, description='Synthetic alarm details'),
                           recurrence_rule='FREQ=DAILY'), description='Synthetic private notes')
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    original, = worker.store.inbox()
    writer.apply_remote([event('never-delivered', BASE+5*MINUTE)])
    if block == 'calendar':
        writer.save_calendar(replace(calendar, enabled=False))
    else:
        save_reminder_preferences(writer, replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}), now=clock.now())
    drain()
    if block == 'calendar':
        assert worker.store.open_entry(original.reminder.key, partition=worker.store.partition_id) is None
    if edit == 'exclude':
        from debacal.recurrence import recurrence_key
        excluded = replace(source, excluded_dates=(recurrence_key(source.start),))
        if block == 'calendar':
            writer.apply_remote([excluded])
        else:
            writer.apply('Exclude occurrence', [excluded])
        drain()
    if edit == 'move':
        if block == 'calendar':
            # A sync write can change a locally disabled calendar.
            writer.apply_remote([replace(source, start=source.start-2*MINUTE, end=source.end-2*MINUTE)])
        else:
            move(source, BASE-2*MINUTE)
        drain()
    assert not worker.store._db.execute('SELECT 1 FROM frontier').fetchone()
    assert not worker.store._db.execute('SELECT 1 FROM source').fetchone()
    archived, = worker.store._db.execute('SELECT id,schedule FROM receipt_source').fetchall()
    assert archived[0] == source.id
    geometry = AlarmSchedule.loads(archived[1])
    assert not (geometry.master.description or geometry.master.raw_ics or geometry.titles)
    assert all(not alarm.description and not alarm.raw_ics for alarms in geometry.components.values() for alarm in alarms.values())
    clock.time = BASE+6*MINUTE
    drain()
    assert len(worker.store.inbox()) == 1
    worker.release()
    resumed = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        def settle():
            for _ in range(1000):
                if resumed.tick() >= 1:
                    return
            pytest.fail('Reminder worker did not settle')
        settle()
        if block == 'calendar':
            writer.save_calendar(calendar)
        else:
            save_reminder_preferences(writer, prefs, now=clock.now())
        settle()
        opened = resumed.store.open_entry(original.reminder.key, partition=resumed.store.partition_id)
        if edit == 'exclude':
            assert opened is None
        else:
            assert opened is not None
            assert reminder_target(writer.path, *opened)[1].start == (BASE-2*MINUTE if edit == 'move' else BASE)
        assert [row.reminder.key for row in resumed.store.inbox()] == [original.reminder.key]
    finally:
        resumed.release()


@pytest.mark.parametrize('legacy', [False, True])
def test_uid_bearing_moved_cutoff_overlaps_uidless_delivered_sibling(native_history, legacy):
    from datetime import datetime, timezone
    from debacal.recurrence import recurrence_key
    from test_reminder_controller import ALARM
    writer, clock, worker, enable, drain, move = native_history
    start = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    due = start+timedelta(days=1)
    early = replace(ALARM, key='early', uid='early', trigger='-PT10M')
    sibling = replace(ALARM, key='sibling', uid=None)
    master = replace(event('series', start, early, sibling, recurrence_rule='FREQ=DAILY',
                           recurrence_dates=(recurrence_key(due-10*MINUTE),)), end=start+60*MINUTE)
    enable(master, due-16*MINUTE)
    clock.time = due
    drain()
    move(master, start-10*MINUTE)
    drain()
    if legacy:
        with worker.store._db as db:
            db.execute("INSERT INTO suppressed SELECT source,component,alarm,occurrence,rank,through "
                       "FROM rank_suppression WHERE kind='native'")
            db.execute("DELETE FROM rank_suppression")
    changed = replace(writer.get_event(master.id), recurrence_rule='FREQ=MONTHLY', recurrence_dates=(),
                      alarms=(replace(early, trigger='-PT5M'), sibling))
    writer.apply('Monthly', [changed])
    drain()
    move(writer.get_event(master.id), due-10*MINUTE)
    drain()
    assert any(row.reminder.key.alarm_key == early.key and row.reminder.key.trigger_utc == due-15*MINUTE
               for row in worker.store.inbox())
    assert not worker.store._db.execute(
        "SELECT 1 FROM rank_suppression WHERE alarm=? AND kind='ambiguous'", (early.key,)).fetchone()


def test_title_only_during_mute_opens_latest_stationary_receipt_after_restart(native_history):
    from alarm_support import FakeTransport
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_reveal import reminder_target
    from debacal.reminder_worker import ReminderWorker
    writer, clock, worker, enable, drain, _ = native_history
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    source = event('daily', BASE, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    original, = worker.store.inbox()
    save_reminder_preferences(writer, replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}), now=clock.now())
    drain()
    writer.apply('Rename while muted', [replace(source, title='Renamed during mute')])
    drain()
    worker.release()
    resumed = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        save_reminder_preferences(writer, prefs, now=clock.now())
        for _ in range(100):
            if resumed.tick() >= 1:
                break
        opened = resumed.store.open_entry(original.reminder.key, partition=resumed.store.partition_id)
        assert opened is not None
        assert reminder_target(writer.path, *opened)[1].title == 'Renamed during mute'
        assert resumed.store.inbox()[0].reminder.title == 'Renamed during mute'
    finally:
        resumed.release()


@pytest.mark.parametrize('actor', ['owner', 'secondary'])
@pytest.mark.parametrize('block', ['calendar', 'mute', 'global'])
@pytest.mark.parametrize('published', [False, True])
def test_inbox_snooze_on_inactive_source_cannot_arm_after_reactivation(native_history, actor, block, published):
    from alarm_support import FakeTransport
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_worker import ReminderWorker
    writer, clock, owner, enable, drain, _ = native_history
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    calendar = Calendar('c', 'Calendar', '#123456')
    enable(event('daily', BASE, recurrence_rule='FREQ=DAILY'), BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = owner.store.inbox()
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        peer.tick()
        assert peer.owner is None
        action = owner if actor == 'owner' else peer
        token = owner.store.partition_id
        if block == 'calendar':
            writer.save_calendar(replace(calendar, enabled=False))
        else:
            muted = (replace(prefs, enabled=False) if block == 'global'
                     else replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}))
            save_reminder_preferences(writer, muted, now=clock.now())
        if published:
            drain()
            peer.tick()
        action.command('snooze', (token, entry.reminder.key, 5*MINUTE))
        assert action.store.entry(entry.reminder.key).snoozed_until is None
        if published and block != 'global':
            assert owner.store.next_wakeup() is None
        writer.save_calendar(calendar)
        save_reminder_preferences(writer, prefs, now=clock.now())
        drain()
        clock.time = BASE+5*MINUTE
        drain()
        assert owner.store.entry(entry.reminder.key).status == 'delivered'
        assert owner.store.entry(entry.reminder.key).snoozed_until is None
    finally:
        peer.release()


@pytest.mark.parametrize('situation', ['active', 'muted', 'old_archive'])
def test_observed_empty_alarm_inventory_breaks_uidless_alignment(native_history, situation):
    from debacal.reminder_preferences import CalendarReminderPolicy
    from test_reminder_controller import ALARM
    writer, clock, worker, enable, drain, _ = native_history
    during_mute = situation == 'muted'
    original = replace(ALARM, key='original', uid=None)
    added = replace(original, key='added', trigger='-PT1M')
    source = event('daily', BASE, original, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-5*MINUTE)
    clock.time = BASE
    drain()
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    if during_mute:
        save_reminder_preferences(writer, replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}), now=clock.now())
        drain()
    writer.apply('Remove all alarms', [replace(source, alarms=())])
    drain()
    if situation == 'old_archive':
        # The previous experimental format copied the old alarm into its
        # receipt-only projection even though the source now has zero alarms.
        from debacal.query_projection import pack_state, unpack_state
        with worker.store._db as db:
            blob, = db.execute('SELECT schedule FROM receipt_source WHERE id=?', (source.id,)).fetchone()
            state, zones, definitions = unpack_state(blob)
            state = state[:9]
            state[3] = {'': [original.to_dict()]}
            db.execute('UPDATE receipt_source SET schedule=? WHERE id=?', (pack_state([state, zones, definitions]), source.id))
    writer.apply('Add a distinct alarm', [replace(source, alarms=(added,))])
    drain()
    if during_mute:
        # Reactivation establishes an eligibility boundary. Use a later
        # trigger to check identity independently of that catch-up policy.
        added = replace(added, trigger='PT1M')
        writer.apply('Future trigger', [replace(source, alarms=(added,))])
        drain()
        save_reminder_preferences(writer, prefs, now=clock.now())
        clock.time = BASE+MINUTE
        drain()
    assert {(row.reminder.key.alarm_key, row.reminder.key.trigger_utc) for row in worker.store.inbox()} == {
        ('original', BASE), ('added', BASE+MINUTE if during_mute else BASE-MINUTE)}


def test_direct_uidless_alarm_edit_keeps_predecessor_without_intervening_removal(native_history):
    from debacal.reminder_compilation import AlarmSchedule
    from test_reminder_controller import ALARM
    writer, clock, worker, enable, drain, _ = native_history
    original = replace(ALARM, key='original', uid=None)
    source = event('daily', BASE, original, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-5*MINUTE)
    clock.time = BASE
    drain()
    writer.apply('Edit existing alarm', [replace(source, alarms=(replace(original, key='new-key', trigger='-PT1M'),))])
    drain()
    assert [entry.reminder.key.alarm_key for entry in worker.store.inbox()] == ['original']
    blob, = worker.store._db.execute('SELECT schedule FROM source WHERE id=?', (source.id,)).fetchone()
    assert set(AlarmSchedule.loads(blob).components['']) == {'original'}


def test_secondary_inbox_snoozes_published_active_alarm(native_history):
    from alarm_support import FakeTransport
    from debacal.reminder_worker import ReminderWorker
    writer, clock, worker, enable, drain, _ = native_history
    enable(event('live', BASE), BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = worker.store.inbox()
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        peer.tick()
        assert peer.owner is None
        peer.command('snooze', (worker.store.partition_id, entry.reminder.key, MINUTE))
        assert worker.store.entry(entry.reminder.key).snoozed_until == BASE+MINUTE
        clock.time = BASE+MINUTE
        drain()
        assert worker.store.entry(entry.reminder.key).status == 'delivered'
    finally:
        peer.release()


@pytest.fixture(params=['owner', 'secondary'])
def daily_inbox_action(native_history, request):
    from alarm_support import FakeTransport
    from debacal.reminder_worker import ReminderWorker
    writer, clock, owner, enable, drain, _ = native_history
    source = event('daily', BASE, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = owner.store.inbox()
    assert entry.status == 'delivered'
    key = entry.reminder.key
    owner.store.mark_read((key,), partition=owner.store.partition_id)
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        peer.tick()
        assert peer.owner is None
        action = owner if request.param == 'owner' else peer
        yield writer, clock, drain, source, key, action
    finally:
        peer.release()


@pytest.mark.parametrize('edit', ['remove', 'key', 'uid', 'trigger', 'description'])
def test_inbox_snooze_qualifies_pending_event_alarm_edit(daily_inbox_action, edit, monkeypatch):
    from debacal.reminder_sources import ReminderSources
    writer, clock, drain, source, key, action = daily_inbox_action
    alarm, = source.alarms
    edited = source
    if edit in ('description', 'key'):
        # A local key alone is excluded from the Store's semantic edit comparison.
        edited = replace(source, description='Revised notes')
    if edit == 'remove':
        edited = replace(edited, alarms=())
    elif edit != 'description':
        changes = {'key': 'different-key', 'uid': 'different-uid', 'trigger': '-PT1M'}
        edited = replace(edited, alarms=(replace(alarm, **{edit: changes[edit]}),))
    assert writer.apply('Edit event', [edited]) is not None
    assert writer.get_event(source.id).alarms == edited.alarms
    content_reads = []
    read_source = ReminderSources.snooze_source

    def observe(reader, source_id, *args, read_event):
        if read_event:
            content_reads.append((source_id, action.store._db.in_transaction))
        return read_source(reader, source_id, *args, read_event=read_event)

    monkeypatch.setattr(ReminderSources, 'snooze_source', observe)
    action.command('snooze', (action.store.partition_id, key, 5*MINUTE))
    action_deadline = action.store.entry(key).snoozed_until
    if edit != 'description':
        # The owner's next journal read sees only this final, restored alarm.
        writer.apply('Restore alarm', [source])
    drain()
    reconciled_deadline = action.store.entry(key).snoozed_until
    clock.time = BASE+5*MINUTE
    drain()
    receipt = action.store.entry(key)
    deadline = BASE+5*MINUTE if edit == 'description' else None
    assert (action_deadline, reconciled_deadline, receipt.status, receipt.unread) == (
        deadline, deadline, 'delivered', edit == 'description')
    assert content_reads == [(source.id, False)]


def test_inbox_snooze_rechecks_stable_event_revision_in_writer(daily_inbox_action, monkeypatch):
    writer, _, _, source, key, action = daily_inbox_action
    begin_write = action.store._write

    def edit_before_write():
        writer.apply('Change description during Snooze', [replace(source, description='New revision')])
        return begin_write()

    with monkeypatch.context() as patch:
        patch.setattr(action.store, '_write', edit_before_write)
        action.command('snooze', (action.store.partition_id, key, 5*MINUTE))
    assert action.store.entry(key).snoozed_until is None
    action.command('snooze', (action.store.partition_id, key, 5*MINUTE))
    assert action.store.entry(key).snoozed_until == BASE+5*MINUTE


@pytest.mark.parametrize('transition', ['event_to_replace', 'replace_to_event', 'replace_to_replace', 'same_replacement', 'expanded_replacement'])
@pytest.mark.parametrize('published', [False, True])
def test_secondary_snooze_tracks_current_replacement_membership(native_history, transition, published, monkeypatch):
    from alarm_support import FakeTransport
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_worker import ReminderWorker
    from debacal.reminder_sources import ReminderSources
    from test_reminder_controller import ALARM
    writer, clock, owner, enable, drain, _ = native_history
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    first = CalendarReminderPolicy('replace', (replace(ALARM, key='one', uid='one'),))
    second = CalendarReminderPolicy('replace', (replace(ALARM, key='two', uid='two'),))
    enable(event('daily', BASE, recurrence_rule='FREQ=DAILY'), BASE-MINUTE)
    if transition != 'event_to_replace':
        save_reminder_preferences(writer, replace(prefs, calendars={'c': first}), now=clock.now())
        drain()
    clock.time = BASE
    drain()
    original, = owner.store.inbox()
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        peer.tick()
        assert peer.owner is None
        replacements = first if transition == 'same_replacement' else second
        if transition == 'expanded_replacement':
            replacements = CalendarReminderPolicy('replace', first.alarms+second.alarms)
        current = prefs if transition == 'replace_to_event' else replace(prefs, calendars={'c': replacements})
        save_reminder_preferences(writer, current, now=clock.now())
        if published:
            drain()
        reads = []
        source_read = ReminderSources.snooze_source
        def observe(reader, *args, read_event):
            reads.append(read_event)
            return source_read(reader, *args, read_event=read_event)
        monkeypatch.setattr(ReminderSources, 'snooze_source', observe)
        peer.command('snooze', (owner.store.partition_id, original.reminder.key, 5*MINUTE))
        deadline = owner.store.entry(original.reminder.key).snoozed_until
        assert deadline == (BASE+5*MINUTE if transition in ('same_replacement', 'expanded_replacement') else None)
        if not published:
            expected = [False, False] if transition == 'same_replacement' else [True]
            if transition == 'expanded_replacement':
                expected.append(False)
            assert reads == expected
        elif reads:
            assert reads == [False, False]
    finally:
        peer.release()


def test_policy_change_during_secondary_snooze_proof_declines_the_write(native_history, monkeypatch):
    from alarm_support import FakeTransport
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_sources import ReminderSources
    from debacal.reminder_worker import ReminderWorker
    writer, clock, owner, enable, drain, _ = native_history
    prefs = ReminderPreferences(enabled=True, delivery='in_app')
    enable(event('live', BASE), BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = owner.store.inbox()
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    try:
        peer.tick()
        read = ReminderSources.snooze_source
        def mute_after_read(reader, *args, **kwargs):
            result = read(reader, *args, **kwargs)
            save_reminder_preferences(writer, replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}), now=clock.now())
            return result
        monkeypatch.setattr(ReminderSources, 'snooze_source', mute_after_read)
        peer.command('snooze', (owner.store.partition_id, entry.reminder.key, MINUTE))
        assert owner.store.entry(entry.reminder.key).snoozed_until is None
    finally:
        peer.release()


@pytest.mark.parametrize('actor', ['owner', 'secondary'])
@pytest.mark.parametrize('edit', ['start', 'exdate', 'rule', 'end', 'detached_end', 'uid', 'description'])
def test_pending_snooze_proves_selected_occurrence_and_anchor(native_history, actor, edit):
    from alarm_support import FakeTransport
    from debacal.recurrence import recurrence_key
    from debacal.reminder_worker import ReminderWorker
    from test_reminder_controller import ALARM
    writer, clock, owner, enable, drain, _ = native_history
    day = timedelta(days=1)
    alarm = replace(ALARM, related='END', trigger='-PT5M') if 'end' in edit else ALARM
    source = event('series', BASE - day, alarm, recurrence_rule='FREQ=DAILY')
    coordinate = recurrence_key(BASE)
    if edit == 'start':
        source = event('single', BASE)
    if edit == 'detached_end':
        detached = event('child', BASE, alarm, recurrence_id=coordinate)
        source = replace(source, recurrence_overrides={coordinate: detached.to_dict()})
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = owner.store.inbox()
    key = entry.reminder.key
    token = owner.partition
    owner.store.mark_read((key,), partition=token)
    peer = ReminderWorker(str(writer.path), clock, FakeTransport(), True, '', lambda *_: None)
    try:
        peer.tick()
        if edit == 'start':
            changed = replace(source, start=BASE+MINUTE)
        elif edit == 'exdate':
            changed = replace(source, excluded_dates=(coordinate,))
        elif edit == 'rule':
            changed = replace(source, recurrence_rule='FREQ=WEEKLY')
        elif edit == 'end':
            changed = replace(source, end=source.end+MINUTE)
        elif edit == 'detached_end':
            changed = replace(source, recurrence_overrides={coordinate: replace(detached, end=detached.end+MINUTE).to_dict()})
        elif edit == 'uid':
            changed = replace(source, ical_uid='replacement')
        else:
            changed = replace(source, description='Edited notes')
        if edit == 'uid':
            writer.apply_remote(delete_ids=[source.id], calendar_id='c')
        writer.apply_remote([changed])
        action = owner if actor == 'owner' else peer
        action.command('snooze', (token, key, 5*MINUTE))
        action_deadline = owner.store.entry(key).snoozed_until
        if edit == 'uid':
            writer.apply_remote(delete_ids=[source.id], calendar_id='c')
        writer.apply_remote([source])
        drain()
        clock.time = BASE+5*MINUTE
        drain()
        assert (action_deadline, owner.store.entry(key).unread) == (
            (BASE+5*MINUTE, True) if edit == 'description' else (None, False))
    finally:
        peer.release()


@pytest.mark.parametrize('edit', ['title', 'exdate', 'uid', 'delete', 'partition'])
def test_off_open_proves_only_the_selected_current_target(native_history, edit, monkeypatch):
    from debacal.reminder_sources import ReminderSources
    from debacal.reminder_reveal import reminder_target
    writer, clock, worker, enable, drain, _ = native_history
    source = event('series', BASE, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    entry, = worker.store.inbox()
    token, key = worker.partition, entry.reminder.key
    save_reminder_preferences(writer, ReminderPreferences(enabled=False), now=clock.now())
    drain()
    assert worker.owner is None
    def unexpected_scan(*args, **kwargs):
        pytest.fail('Explicit Open scanned the calendar')
    monkeypatch.setattr(ReminderSources, 'scan', unexpected_scan)
    monkeypatch.setattr(ReminderSources, 'changes', unexpected_scan)
    if edit == 'delete':
        writer.apply('Delete', delete_ids=[source.id])
    elif edit == 'partition':
        token = 'retired-partition'
    else:
        fields = {'title': {'title': 'Current title'}, 'exdate': {'excluded_dates': (key.occurrence_key,)},
                  'uid': {'ical_uid': 'replacement'}}
        if edit == 'uid':
            writer.apply_remote(delete_ids=[source.id], calendar_id='c')
        writer.apply_remote([replace(source, **fields[edit])])
    writes = writer.connection.total_changes
    opened = worker.store.open_entry(key, partition=token)
    assert worker.store.entry(key).unread == (edit != 'title')
    if edit == 'title':
        assert reminder_target(writer.path, *opened)[1].title == 'Current title'
    else:
        assert opened is None
    assert worker.owner is None and writer.connection.total_changes == writes


@pytest.mark.parametrize('missed,gap,delivered', [('none', False, {'a'}), ('all', False, {'a', 'z'}), ('none', True, set())])
def test_bootstrap_known_future_and_late_discovery(native_history, missed, gap, delivered):
    writer, clock, worker, _, _, _ = native_history
    clock.time = BASE-timedelta(milliseconds=100)
    filler = [replace(event(f'm{i:02}', BASE), alarms=()) for i in range(65)]
    writer.apply_remote([event('a', BASE), *filler, event('z', BASE)])
    save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app', missed=missed),
                              now=clock.now())
    worker.tick()
    assert worker.store.next_wakeup() == BASE and worker.store.bootstrap is not None
    if gap:
        clock.time += 5*MINUTE
    for step in range(30):
        clock.time += timedelta(milliseconds=850)
        delay = worker.tick()
        if step == 0:
            assert worker.store.bootstrap is not None
            assert {entry.reminder.key.source_id for entry in worker.store.inbox()} == (set() if gap else {'a'})
        if delay >= 1:
            break
    assert worker.store.bootstrap is None
    assert {entry.reminder.key.source_id for entry in worker.store.inbox()} == delivered


@pytest.mark.parametrize('pending_event_edit', [False, True])
def test_snooze_floating_receipt_proves_current_scheduling_zone(native_history, monkeypatch, pending_event_edit):
    writer, clock, worker, enable, drain, _ = native_history
    monkeypatch.setenv('TZ', 'UTC')
    source = event('floating', BASE, floating_time=True)
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    receipt, = worker.store.inbox()
    worker.store.mark_read((receipt.reminder.key,), partition=worker.partition)
    if pending_event_edit:
        writer.apply('Edit notes', [replace(source, description='Current notes')])
    monkeypatch.setenv('TZ', 'Europe/Berlin')
    worker.command('snooze', (worker.partition, receipt.reminder.key, 5*MINUTE))
    deadline = worker.store.entry(receipt.reminder.key).snoozed_until
    monkeypatch.setenv('TZ', 'UTC')
    clock.time += 5*MINUTE
    drain()
    assert deadline is None and not worker.store.entry(receipt.reminder.key).unread


def test_off_open_rechecks_revision_after_the_selected_proof(native_history, monkeypatch):
    writer, clock, worker, enable, drain, _ = native_history
    source = event('one', BASE)
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    receipt, = worker.store.inbox()
    save_reminder_preferences(writer, ReminderPreferences(enabled=False), now=clock.now())
    drain()
    writer.apply('Rename', [replace(source, title='Current title')])
    store = worker.store
    begin = store._write
    def remove_before_write():
        writer.apply('Remove', delete_ids=[source.id])
        return begin()
    monkeypatch.setattr(store, '_write', remove_before_write)
    assert store.open_entry(receipt.reminder.key, partition=store.partition_id) is None
    assert store.entry(receipt.reminder.key).unread


def test_restart_before_due_services_known_partial_bootstrap(native_history):
    from alarm_support import FakeTransport
    from debacal.reminder_worker import ReminderWorker
    writer, clock, first, _, _, _ = native_history
    clock.time = BASE-timedelta(seconds=1)
    filler = [replace(event(f'm{i:02}', BASE), alarms=()) for i in range(45)]
    writer.apply_remote([event('a', BASE), *filler])
    save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app', missed='none'),
                              now=clock.now())
    first.tick()
    assert first.store.next_wakeup() == BASE
    first.release()
    second = ReminderWorker(str(writer.path), clock, FakeTransport(), True, '', lambda *_: None)
    try:
        clock.time += timedelta(milliseconds=500)
        second.tick()
        clock.time += timedelta(milliseconds=750)
        second.tick()
        assert second.store.bootstrap is not None
        assert [entry.reminder.key.source_id for entry in second.store.inbox()] == ['a']
    finally:
        second.release()


@pytest.mark.parametrize('action', ['snooze', 'native'])
@pytest.mark.parametrize('published', [False, True])
def test_user_snooze_declines_a_retired_native_member(native_history, action, published):
    writer, clock, worker, enable, drain, _ = native_history
    source = event('series', BASE-timedelta(days=1), recurrence_rule='FREQ=DAILY')
    enable(source, BASE-MINUTE)
    save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
    drain()
    clock.time = BASE
    drain()
    due, *_ = worker.transport.sent[0]
    key = due.key
    worker.command('delivered', (key, 7))
    assert worker.store.entry(key).status == 'delivered'
    worker.store.mark_read((key,), partition=worker.partition)
    writer.apply('Change series', [replace(source, recurrence_rule='FREQ=WEEKLY')])
    if published:
        drain()
    assert key in worker.native_calendars
    worker.command(action, (worker.partition, key, 'snooze' if action == 'native' else 5*MINUTE))
    deadline = worker.store.entry(key).snoozed_until
    writer.apply('Restore series', [source])
    drain()
    clock.time += 5*MINUTE
    drain()
    assert deadline is None and not worker.store.entry(key).unread
    assert len(worker.transport.sent) == 1


@pytest.mark.parametrize('action', ['inbox', 'native'])
@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('days', [-1, 1])
def test_snooze_tracks_logical_member_when_all_move_reuses_old_clock(native_history, action, published, days):
    from alarm_support import FakeTransport
    from debacal.recurrence import recurrence_key
    from debacal.reminder_worker import ReminderWorker
    writer, clock, worker, enable, drain, move = native_history
    day = timedelta(days=1)
    series = event('series', BASE-2*day, recurrence_rule='FREQ=DAILY')
    enable(series, BASE-MINUTE)
    save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
    drain()
    clock.time = BASE
    drain()
    key = worker.transport.sent[0][0].key
    worker.command('delivered', (key, 7))
    move(series, series.start+days*day)
    if published:
        drain()
    target, _ = worker.store.open_entry(key, partition=worker.partition)
    assert target.key.occurrence_key == recurrence_key(BASE+days*day)
    notices = []
    worker.notify = lambda _, name, args: notices.append((name, args))
    if action == 'inbox':
        worker.command('request', (42, 'snooze', worker.partition, key, 5*MINUTE))
        error, = [args[-1] for name, args in notices if name == 'actionResult']
    else:
        worker.command('native', (worker.partition, key, 'snooze'))
        error = next((args[0] for name, args in notices if name == 'actionIssue'), None)
    deadline = worker.store.entry(key).snoozed_until
    writer.apply_remote([series])
    worker.release()
    resumed = ReminderWorker(str(writer.path), clock, FakeTransport(), True, '', lambda *_: None)
    try:
        for now in (BASE, BASE+5*MINUTE):
            clock.time = now
            for _ in range(100):
                if resumed.tick() >= 1:
                    break
        assert error and deadline is None
        assert not resumed.store.entry(key).unread and not resumed.transport.sent
    finally:
        resumed.release()


@pytest.mark.parametrize('edit,interruption,expected', [
    ('title', 'none', True), ('alarm description', 'none', True), ('title', 'restart before', True),
    ('title', 'restart after', False), ('title', 'gap', False), ('title', 'enable after', False),
    ('geometry', 'none', False),
    ('alarm uid', 'none', False), ('alarm rule', 'none', False),
])
def test_bootstrap_known_wake_across_selected_source_replay(native_history, edit, interruption, expected):
    from alarm_support import FakeTransport
    from debacal.reminder_worker import ReminderWorker
    writer, clock, first, _, _, _ = native_history
    clock.time = BASE-timedelta(milliseconds=100)
    source = event('a', BASE)
    filler = [replace(event(f'm{i:02}', BASE), alarms=()) for i in range(65)]
    writer.apply_remote([source, *filler])
    save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app', missed='none'), now=clock.now())
    first.tick()
    assert first.store.next_wakeup() == BASE and first.store.bootstrap is not None
    clock.time += timedelta(milliseconds=50)
    alarm, = source.alarms
    if edit == 'geometry':
        updated = replace(source, start=source.start+timedelta(seconds=1), end=source.end+timedelta(seconds=1))
    elif edit == 'alarm uid':
        updated = replace(source, alarms=(replace(alarm, uid='replacement'),))
    elif edit == 'alarm rule':
        updated = replace(source, alarms=(replace(alarm, trigger='-PT5M', related='END'),))
    elif edit == 'alarm description':
        updated = replace(source, alarms=(replace(alarm, description='Updated alarm text'),))
    else:
        updated = replace(source, title='Current title')
    writer.apply_remote([updated])
    worker = first
    if interruption == 'enable after':
        save_reminder_preferences(writer, ReminderPreferences(enabled=False), now=clock.now())
        first.tick()
        clock.time = BASE+timedelta(milliseconds=50)
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app', missed='none'),
                                  now=clock.now())
    if interruption.startswith('restart'):
        first.release()
        worker = ReminderWorker(str(writer.path), clock, FakeTransport(), True, '', lambda *_: None)
        if interruption == 'restart before':
            worker.tick()
    if interruption == 'gap':
        clock.time += 5*MINUTE
    try:
        for _ in range(30):
            clock.time += timedelta(milliseconds=400 if edit == 'geometry' else 100)
            if worker.tick() >= 1:
                break
        assert worker.store.bootstrap is None
        if interruption != 'enable after':
            disposition, = worker.store._db.execute("SELECT status FROM ledger WHERE source='a'").fetchone()
            assert disposition == ('delivered' if expected else 'suppressed')
        assert [(entry.reminder.title, entry.reminder.key.trigger_utc) for entry in worker.store.inbox()] == (
            [(updated.title, BASE)] if expected else [])
    finally:
        if worker is not first:
            worker.release()


@pytest.mark.parametrize('pending', ['rename', 'move back'])
def test_snooze_composes_published_and_unpublished_all_moves(native_history, pending):
    writer, clock, worker, enable, drain, move = native_history
    day = timedelta(days=1)
    source = event('series', BASE-2*day, recurrence_rule='FREQ=DAILY')
    enable(source, BASE-MINUTE)
    clock.time = BASE
    drain()
    key = worker.store.inbox()[0].reminder.key
    worker.store.mark_read((key,), partition=worker.partition)
    move(source, source.start+day)
    drain()
    current = writer.get_event(source.id)
    if pending == 'move back':
        move(current, source.start)
    else:
        writer.apply('Rename', [replace(current, title='Current title')])
    worker.command('request', (1, 'snooze', worker.partition, key, 5*MINUTE))
    deadline = worker.store.entry(key).snoozed_until
    drain()
    clock.time = BASE+5*MINUTE
    drain()
    assert (deadline, worker.store.entry(key).unread) == (
        (clock.time, True) if pending == 'move back' else (None, False))
