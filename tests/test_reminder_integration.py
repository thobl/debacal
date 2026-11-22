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
