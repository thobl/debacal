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
