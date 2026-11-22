import json
import sqlite3
from contextlib import contextmanager

import pytest

from alarm_support import BASE, FakeClock, wait_for
from debacal.models import Calendar
from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
from debacal.reminder_store import ReminderStore
from debacal.store import Store
from test_reminder_controller import MINUTE, event, running


@pytest.mark.parametrize('native', [False, True])
def test_rotation_between_open_validation_and_write_declines_activation(tmp_path, monkeypatch, native):
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            opened = []
            controller.activate.connect(opened.append)
            assert not page()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: transport.sent)
            key = transport.sent[0][0].key
            transport.delivered.emit(key, 10)
            assert page()[0].unread

            original = ReminderStore._write
            @contextmanager
            def rotating(store):
                if not rotated:
                    rotated.append(True)
                    with sqlite3.connect(str(path) + '.reminders.sqlite3') as db:
                        db.execute("UPDATE metadata SET value=? WHERE key='partition'", (json.dumps('rotated'),))
                with original(store):
                    yield
            rotated = []
            monkeypatch.setattr(ReminderStore, '_write', rotating)
            if native:
                transport.action.emit(key, 'open')
            else:
                controller.open_reminder(key)
            assert page()[0].unread
            assert rotated and not opened


def test_inbox_actions_capture_page_and_snooze_duration(tmp_path):
    from debacal.reminder_dialog import ReminderDialog
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        from dataclasses import replace
        from test_reminder_controller import ALARM
        first = replace(ALARM, key='first', uid='first', repeat=2, repeat_interval='PT1M')
        other = replace(first, key='other', uid='other')
        writer.apply_remote([event('one', BASE, first, other, replace(first, key='third', uid='third'))])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, _, page):
            assert not page()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: len(page()) == 3)
            dialog = ReminderDialog(controller)
            dialog.show()
            try:
                wait_for(lambda: dialog.rows.topLevelItemCount() == 3)
                assert all(row.unread for row in page())
                dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
                assert dialog.snooze_duration.text() == '5min'
                dialog.snooze_duration.setText('12min')
                dialog.snooze_button.click()
                assert wait_for(lambda: next((row for row in page() if row.status == 'snoozed'), None)).snoozed_until == BASE + 12*MINUTE
                snoozed = next(row.reminder.key for row in page() if row.status == 'snoozed')
                for i in range(dialog.rows.topLevelItemCount()):
                    dialog.rows.setCurrentItem(dialog.rows.topLevelItem(i))
                    if dialog.key() != snoozed:
                        break
                dismissed = dialog.key()
                assert dismissed.alarm_key != snoozed.alarm_key
                dialog.dismiss_button.click()
                wait_for(lambda: any(row.status == 'dismissed' for row in page()))
                untouched = next(row.reminder.key for row in page() if row.reminder.key not in (snoozed, dismissed))
                clock.time = BASE + MINUTE
                controller.reload_settings()
                wait_for(lambda: any(row.reminder.key.repeat_index == 1 and
                                     row.reminder.key.alarm_key == untouched.alarm_key for row in page()))
                clock.time = BASE + 12*MINUTE
                controller.reload_settings()
                wait_for(lambda: any(row.reminder.key == snoozed and row.status == 'delivered' for row in page()))
                assert all(row.reminder.key.repeat_index == 0 for row in page()
                           if row.reminder.key.alarm_key == dismissed.alarm_key)
            finally:
                dialog.close()


def test_absolute_navigation_includes_ongoing_last_and_empty():
    from dataclasses import replace
    from datetime import timedelta
    from debacal.event_range_index import EventRangeIndex
    from debacal.recurrence import recurrence_key
    from debacal.reminder_reveal import absolute_occurrence
    master = event('one', BASE, recurrence_rule='FREQ=DAILY;COUNT=80000')
    day = timedelta(days=1)
    record = EventRangeIndex.prepare(master)
    assert absolute_occurrence(record, BASE + 40000*day + MINUTE).start == BASE + 40000*day
    assert absolute_occurrence(record, BASE + 79999*day + 6*MINUTE).start == BASE + 79999*day
    empty = replace(master, recurrence_rule='FREQ=DAILY;COUNT=1', excluded_dates=(recurrence_key(BASE, False),))
    assert absolute_occurrence(EventRangeIndex.prepare(empty), BASE) is None


@contextmanager
def workspace(path, clock, writer, *, allowed=True, demo=False):
    from PySide6.QtWidgets import QApplication
    from alarm_support import FakeTransport
    from debacal.reminder_controller import ReminderController
    from debacal.ui import MainWindow
    app = QApplication.instance() or QApplication([])
    transport = FakeTransport()
    def factory(database, **options):
        return ReminderController(database, transport=transport, clock=clock, **options)
    window = MainWindow(writer, enable_sync=False, demo_mode=demo, delivery_allowed=allowed, reminder_factory=factory)
    controller = window.reminder_controller
    pages = {}
    controller.inboxPageReady.connect(lambda request, entries: pages.update({request: entries}))
    def page():
        request = controller.request_inbox()
        wait_for(lambda: request in pages)
        return pages.pop(request)
    try:
        yield window, transport, page
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize('mode', ['relative', 'absolute', 'empty', 'read_only'])
def test_explicit_open_reveals_occurrence_or_canonical_editor(tmp_path, monkeypatch, mode):
    from datetime import timedelta
    from PySide6.QtWidgets import QDialog, QApplication
    from debacal.alarms import Alarm
    from debacal.event_dialog import EventDialog
    from debacal.recurrence import recurrence_key
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - MINUTE)
    day = timedelta(days=1)
    alarm = Alarm('a', 'DISPLAY', 'PT0S' if mode == 'relative' else '20261121T090000Z', uid='a')
    start = BASE-day
    excluded = tuple(recurrence_key(start + i*day, False) for i in range(3)) if mode in ('empty','read_only') else ()
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Appointments', '#123456', read_only=mode=='read_only'))
        writer.apply_remote([event('one', start, alarm, recurrence_rule='FREQ=DAILY;COUNT=3', excluded_dates=excluded)])
        writer.set_calendar_visible('c', False)
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, missed='all'), now=clock.now())
        with workspace(path, clock, writer) as (window, transport, page):
            visits = []
            def inspect(dialog):
                visits.append((dialog.original, dialog.scope.currentData(), dialog.windowTitle()))
                return QDialog.DialogCode.Rejected
            monkeypatch.setattr(EventDialog, 'exec', inspect)
            assert not page()
            clock.time = BASE
            window.reminder_controller.reload_settings()
            wait_for(lambda: transport.sent)
            key = transport.sent[0][0].key
            transport.delivered.emit(key, 5)
            assert page()[0].unread
            assert not window.isVisible() and not visits and not writer.get_calendar('c').visible
            assert '1 unread' in window.reminder_button.accessibleName()
            original_day = window.anchor
            window.search.setText('unrelated search')
            window.filter_search.setChecked(True)
            transport.action.emit(key, 'open')
            if mode == 'relative':
                wait_for(lambda: window.selection.primary)
                selected = window.selection.primary
                assert selected.recurrence_id == recurrence_key(BASE, False)
                assert window.visible_event_by_id(selected.id) and not visits
            else:
                wait_for(lambda: visits)
                selected, scope, caption = visits[0]
                assert scope == 'group'
                if mode == 'absolute':
                    assert selected.recurrence_id == recurrence_key(BASE, False)
                else:
                    assert selected.id == 'one' and window.anchor == original_day
                    assert ('read-only' in caption) == (mode == 'read_only')
            assert window.isVisible() and not page()[0].unread


def test_queued_and_inflight_opens_revalidate_identity(tmp_path, monkeypatch):
    from threading import Event
    import debacal.reminder_reveal as reveal
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Appointments', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with workspace(path, clock, writer) as (window, _, page):
            assert not page()
            clock.time = BASE
            controller = window.reminder_controller
            controller.reload_settings()
            row = wait_for(lambda: next(iter(page()), None))
            ready, release = Event(), Event()
            ordinary = reveal.reminder_target
            def hold(*args):
                result = ordinary(*args)
                ready.set()
                release.wait(3)
                return result
            monkeypatch.setattr(reveal, 'reminder_target', hold)
            controller.open_reminder(row.reminder.key)
            wait_for(ready.is_set)
            writer.apply_remote([event('one', BASE + MINUTE)])
            release.set()
            wait_for(lambda: window._editor_future is None)
            assert window.selection.primary is None and not window.isVisible()
            context = controller.activation_context
            with sqlite3.connect(str(path)+'.reminders.sqlite3') as db:
                db.execute("UPDATE metadata SET value=? WHERE key='partition'", (json.dumps('rotated'),))
            received = []
            controller.activate.connect(received.append)
            controller._receive(controller._worker, 'activate', (row.reminder, context))
            assert not received


def test_displayed_page_keeps_token_across_another_request(tmp_path):
    from debacal.reminder_dialog import ReminderDialog
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (owner, _, owner_page):
            assert not owner_page()
            clock.time = BASE
            wait_for(lambda: owner_page())
            with running(path, clock) as (controller, _, page):
                dialog = ReminderDialog(controller)
                try:
                    wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
                    dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
                    old = dialog.partition
                    with sqlite3.connect(str(path)+'.reminders.sqlite3') as db:
                        db.execute("UPDATE metadata SET value=? WHERE key='partition'", (json.dumps('rotated'),))
                    assert page()[0].unread and controller.inbox_partition != old
                    dialog.dismiss_button.click()
                    assert page()[0].unread and page()[0].status == 'delivered'
                finally:
                    dialog.close()
                    dialog.deleteLater()


def test_inbox_snapshot_signal_retains_own_token_through_reentrant_delivery(tmp_path):
    from debacal.reminder_dialog import ReminderDialog
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, _, page):
            assert not page()
            clock.time = BASE
            row = wait_for(lambda: next(iter(page()), None))
            # Another subscriber may process queued Qt events before this dialog receives its reply.
            def nested(request, entries):
                if request == dialog.request:
                    controller._receive(controller._worker, 'page', (request + 200, (), 'other'))
            controller.inboxPageReady.connect(nested)
            dialog = ReminderDialog(controller)
            original = controller.inbox_partition
            try:
                wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
                assert dialog.partition == original
                assert dialog.rows.topLevelItem(0).text(0) == row.reminder.title
            finally:
                dialog.close()
                dialog.deleteLater()


def test_deleted_source_declines_open_and_preserves_unread(tmp_path):
    path = tmp_path/'calendar'
    clock = FakeClock(BASE - MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, _, page):
            assert not page()
            clock.time = BASE
            key = wait_for(lambda: next(iter(page()), None)).reminder.key
            writer.apply('Remove event', delete_ids=['one'])
            opened = []
            controller.activate.connect(opened.append)
            controller.open_reminder(key)
            assert page()[0].unread and not opened


def test_second_window_opens_inbox_and_updates_owners_badge(tmp_path):
    path = tmp_path/'calendar'
    clock = FakeClock(BASE - MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (owner, _, owner_page):
            assert not owner_page()
            clock.time = BASE
            owner.reload_settings()
            wait_for(lambda: owner.unread_count == 1)
            with running(path, clock) as (second, _, page):
                wait_for(lambda: second.unread_count == 1)
                key = page()[0].reminder.key
                second.open_reminder(key)
                wait_for(lambda: second.unread_count == 0)
                owner.reload_settings()
                wait_for(lambda: owner.unread_count == 0)


def test_long_calendar_name_keeps_status_in_view_at_large_scale(tmp_path):
    from debacal.preferences import Preferences
    from debacal.reminder_dialog import ReminderDialog
    path = tmp_path/'calendar'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        Preferences(font_scale=140).save(writer)
        calendar = Calendar('c', 'Architecture and design public programmes ' * 3, '#123456')
        writer.save_calendar(calendar)
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with workspace(path, clock, writer) as (window, _, page):
            assert not page()
            clock.time = BASE
            window.reminder_controller.reload_settings()
            wait_for(lambda: page())
            dialog = ReminderDialog(window.reminder_controller, window)
            dialog.show()
            try:
                wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
                assert dialog.rows.horizontalScrollBar().maximum() == 0
                assert dialog.rows.topLevelItem(0).toolTip(2) == calendar.name
            finally:
                dialog.close()
