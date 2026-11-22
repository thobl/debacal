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
            window.poll_timer.stop()
            controller.open_reminder(row.reminder.key)
            wait_for(ready.is_set)
            writer.apply_remote([event('one', BASE + MINUTE)])
            release.set()
            wait_for(lambda: window._reminder_future is None)
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
                assert dialog.rows.topLevelItem(0).text(0).splitlines()[0] == row.reminder.title
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


@contextmanager
def delivered_workspace(tmp_path, *alarms, style=None):
    path = tmp_path / 'calendar'
    clock = FakeClock(BASE-60*MINUTE)
    with Store(path) as writer:
        if style:
            style.save(writer)
        writer.save_calendar(Calendar('c', 'Appointments', '#123456'))
        writer.apply_remote([event('one', BASE, *alarms)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with workspace(path, clock, writer) as (window, transport, page):
            assert not page()
            clock.time = BASE
            window.reminder_controller.reload_settings()
            wait_for(lambda: len(transport.sent) == (len(alarms) or 1))
            yield writer, window, transport, page


def test_inbox_exposes_trigger_for_selected_dismiss_and_uncertain_read_state(tmp_path):
    from dataclasses import replace
    from debacal.timeutil import local_zone
    from test_reminder_controller import ALARM
    early = replace(ALARM, trigger='-PT30M', uid='early', key='early')
    later = replace(ALARM, trigger='-PT5M', uid='later', key='later')
    with delivered_workspace(tmp_path, early, later) as (_, window, transport, page):
        transport.failed.emit(transport.sent[0][0].key, 'Unavailable')
        transport.delivered.emit(transport.sent[1][0].key, 4)
        wait_for(lambda: {row.status for row in page()} == {'uncertain', 'delivered'})
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 2)
        rows = [dialog.rows.topLevelItem(i) for i in range(2)]
        clock = lambda dt: window.preferences.format_time(dt.astimezone(local_zone()))
        chosen = [row for row in rows if clock(BASE-30*MINUTE) in row.text(1)]
        assert len(chosen) == 1 and all(clock(BASE) in row.text(1) for row in rows)
        row, = chosen
        assert 'uncertain' in row.text(3).lower() and 'unread' in row.toolTip(3).lower()
        dialog.rows.setCurrentItem(row)
        dialog.dismiss_button.click()
        assert wait_for(lambda: {e.reminder.alarm.uid for e in page() if e.status == 'dismissed'}) == {'early'}


@pytest.mark.parametrize('action', ['open', 'snooze'])
def test_inbox_rejected_action_leaves_selected_row_with_feedback(tmp_path, action):
    with delivered_workspace(tmp_path) as (writer, window, transport, page):
        transport.delivered.emit(transport.sent[0][0].key, 1)
        assert page()[0].unread
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
        dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
        writer.apply('Delete event', delete_ids=['one'])
        (dialog.open_button if action == 'open' else dialog.snooze_button).click()
        assert page()[0].unread
        assert dialog.isVisible() and dialog.status.isVisible() and dialog.status.text().strip()


@pytest.mark.parametrize('mode', ['off', 'mute'])
def test_inbox_explains_known_disabled_snooze(tmp_path, mode):
    from debacal.reminder_preferences import CalendarReminderPolicy
    with delivered_workspace(tmp_path) as (writer, window, transport, page):
        transport.delivered.emit(transport.sent[0][0].key, 1)
        assert page()[0].unread
        prefs = ReminderPreferences(enabled=mode != 'off', calendars={'c': CalendarReminderPolicy('mute')})
        save_reminder_preferences(writer, prefs, now=BASE)
        window.apply_reminder_preferences(prefs)
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
        dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
        assert not dialog.snooze_button.isEnabled() and dialog.snooze_button.toolTip()
        assert dialog.open_button.isEnabled()


@pytest.mark.parametrize('interaction', ['editor', 'picker'])
def test_native_open_restores_unfinished_interaction_before_read(tmp_path, interaction):
    from debacal.event_dialog import EventDialog
    from debacal.occurrence_picker import CalendarOccurrencePicker
    from test_reminder_editor import release_widget
    with delivered_workspace(tmp_path) as (writer, window, transport, page):
        key = transport.sent[0][0].key
        transport.delivered.emit(key, 1)
        assert page()[0].unread
        draft = EventDialog(writer.list_calendars(), BASE, parent=window)
        draft.title.setText('Unsaved draft')
        try:
            if interaction == 'editor':
                draft.setModal(True)
                draft.show()
            else:
                window.occurrence_picker = CalendarOccurrencePicker(window, draft)
            transport.action.emit(key, 'open')
            assert page()[0].unread and window.isVisible()
            assert draft.title.text() == 'Unsaved draft' and window.selection.primary is None
        finally:
            window.occurrence_picker = None
            release_widget(draft)


@pytest.mark.parametrize('change', ['none', 'event', 'partition'])
def test_accepted_open_waits_for_late_modal_then_reveals(tmp_path, monkeypatch, change):
    from threading import Event
    from PySide6.QtWidgets import QDialog
    from debacal import reminder_reveal
    with delivered_workspace(tmp_path) as (_, window, transport, page):
        key = transport.sent[0][0].key
        transport.delivered.emit(key, 1)
        assert page()[0].unread
        started, release = Event(), Event()
        resolve = reminder_reveal.reminder_target
        def hold(*args):
            started.set()
            release.wait(4)
            return resolve(*args)
        monkeypatch.setattr(reminder_reveal, 'reminder_target', hold)
        transport.action.emit(key, 'open')
        wait_for(started.is_set)
        blocker = QDialog(window)
        blocker.setModal(True)
        blocker.show()
        try:
            release.set()
            wait_for(lambda: window._reminder_future is None)
            assert window.selection.primary is None and blocker.isVisible()
            if change == 'event':
                window.store.apply('Remove target', delete_ids=['one'])
            if change == 'partition':
                with sqlite3.connect(str(window.store.path) + '.reminders.sqlite3') as db:
                    db.execute("UPDATE metadata SET value=? WHERE key='partition'", (json.dumps('replacement'),))
        finally:
            blocker.close()
        if change != 'none':
            from PySide6.QtTest import QTest
            QTest.qWait(200)
            assert window.selection.primary is None
            return
        wait_for(lambda: window.selection.primary)
        assert window.selection.primary.id == 'one' and not page()[0].unread


def test_inbox_own_modal_opens_renamed_event_with_delivery_off(tmp_path):
    from dataclasses import replace
    with delivered_workspace(tmp_path) as (writer, window, transport, page):
        key = transport.sent[0][0].key
        transport.delivered.emit(key, 1)
        assert page()[0].unread
        prefs = ReminderPreferences(enabled=False)
        save_reminder_preferences(writer, prefs, now=BASE)
        window.apply_reminder_preferences(prefs)
        wait_for(lambda: not transport.started)
        writer.apply('Rename', [replace(writer.get_event('one'), title='Current title')])
        window.show_reminders()
        dialog = window._reminder_dialog
        dialog.setModal(True)
        wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
        dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
        dialog.open_button.click()
        wait_for(lambda: window.selection.primary)
        assert window.selection.primary.title == 'Current title' and window._reminder_dialog is None
        assert not page()[0].unread


def test_same_trigger_and_rule_have_distinct_visible_choices(tmp_path):
    from dataclasses import replace
    from test_reminder_controller import ALARM
    with delivered_workspace(tmp_path, ALARM, replace(ALARM, key='other', uid='other')) as (_, window, transport, page):
        for i, sent in enumerate(transport.sent):
            transport.delivered.emit(sent[0].key, i+1)
        assert all(entry.unread for entry in page())
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 2)
        rows = [dialog.rows.topLevelItem(i) for i in range(2)]
        visible = [tuple(row.text(col) for col in range(4)) for row in rows]
        assert visible[0] != visible[1]
        dialog.rows.setCurrentItem(rows[1])
        dialog.dismiss_button.click()
        assert wait_for(lambda: [e.reminder.key.alarm_key for e in page() if e.status == 'dismissed']) == ['other']


def test_uncertain_unread_is_visible_and_accessible(tmp_path):
    from PySide6.QtCore import Qt
    with delivered_workspace(tmp_path) as (_, window, transport, page):
        transport.failed.emit(transport.sent[0][0].key, 'Unavailable')
        assert page()[0].status == 'uncertain'
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 1)
        row = dialog.rows.topLevelItem(0)
        assert 'uncertain' in row.text(3).lower()
        assert row.font(0).bold() and 'unread' in row.data(3, Qt.ItemDataRole.AccessibleDescriptionRole).lower()


def test_coincident_triggers_expose_alarm_relation_and_repeat_interval(tmp_path):
    from dataclasses import replace
    from PySide6.QtCore import Qt
    from test_reminder_controller import ALARM
    end = replace(ALARM, key='end', uid='end', trigger='-PT5M', related='END', repeat=1, repeat_interval='PT10M')
    with delivered_workspace(tmp_path, ALARM, end) as (_, window, transport, page):
        for i, sent in enumerate(transport.sent):
            transport.delivered.emit(sent[0].key, i+1)
        assert all(entry.unread for entry in page())
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 2)
        rows = [dialog.rows.topLevelItem(i) for i in range(2)]
        assert rows[0].text(1) == rows[1].text(1)
        assert any('start' in row.text(0).lower() for row in rows)
        end_row, = [row for row in rows if 'end' in row.text(0).lower()]
        assert '10min' in end_row.toolTip(0) and '10min' in end_row.data(0, Qt.ItemDataRole.AccessibleDescriptionRole)
        dialog.rows.setCurrentItem(end_row)
        dialog.dismiss_button.click()
        assert wait_for(lambda: [e.reminder.key.alarm_key for e in page() if e.status == 'dismissed']) == ['end']


def test_inbox_result_belongs_to_exact_request_selection_and_lifetime(tmp_path, monkeypatch):
    from dataclasses import replace
    from threading import Event
    from test_reminder_controller import ALARM
    with delivered_workspace(tmp_path, ALARM, replace(ALARM, key='other', uid='other')) as (_, window, transport, page):
        for i, sent in enumerate(transport.sent):
            transport.delivered.emit(sent[0].key, i+1)
        assert all(entry.unread for entry in page())
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 2)
        dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
        started, release = Event(), Event()
        store_snooze = ReminderStore.snooze
        def hold(*args, **kwargs):
            started.set()
            release.wait(4)
            return store_snooze(*args, **kwargs)
        monkeypatch.setattr(ReminderStore, 'snooze', hold)
        dialog.snooze_button.click()
        wait_for(started.is_set)
        request, token, key, action = dialog.pending_action
        signal = window.reminder_controller.actionFinished
        try:
            signal.emit(request+1, token, key, action, 'Obsolete request')
            signal.emit(request, token+'old', key, action, 'Obsolete page')
            assert dialog.pending_action and not dialog.status.isVisible()
            dialog.rows.setCurrentItem(dialog.rows.topLevelItem(1))
            signal.emit(request, token, key, action, 'Previous selection')
            assert not dialog.status.isVisible()
            dialog.close()
            window.show_reminders()
            other = window._reminder_dialog
            signal.emit(request, token, key, action, 'Closed dialog')
            assert other.status.text() != 'Closed dialog'
        finally:
            release.set()
        assert wait_for(lambda: any(entry.status == 'snoozed' for entry in page()))


def test_retired_native_open_cannot_restore_a_draft(tmp_path):
    from PySide6.QtWidgets import QDialog
    with delivered_workspace(tmp_path) as (writer, window, transport, page):
        key = transport.sent[0][0].key
        transport.delivered.emit(key, 1)
        assert page()[0].unread
        writer.apply('Remove event', delete_ids=['one'])
        blocker = QDialog(window)
        blocker.setModal(True)
        blocker.show()
        try:
            transport.action.emit(key, 'open')
            assert page()[0].unread and not window.isVisible()
        finally:
            blocker.close()


def test_rejected_action_scrollbar_preserves_full_event_time_at_140_percent(tmp_path):
    from dataclasses import replace
    from PySide6.QtWidgets import QStyleOptionViewItem
    from debacal.preferences import Preferences
    from test_reminder_controller import ALARM
    style = Preferences(font_scale=140, appearance_mode='dark', control_style='filled',
                        locale_name='en_GB', date_format='european', time_format='24h')
    alarms = [replace(ALARM, key=str(i), uid=str(i)) for i in range(4)]
    with delivered_workspace(tmp_path, *alarms, style=style) as (writer, window, transport, page):
        for i, sent in enumerate(transport.sent):
            transport.delivered.emit(sent[0].key, i+1)
        assert all(entry.unread for entry in page())
        window.show_reminders()
        dialog = window._reminder_dialog
        wait_for(lambda: dialog.rows.topLevelItemCount() == 4)
        dialog.resize(680, 410)
        dialog.rows.setCurrentItem(dialog.rows.topLevelItem(0))
        writer.apply('Remove event', delete_ids=['one'])
        dialog.snooze_button.click()
        wait_for(lambda: dialog.status.isVisible() and dialog.rows.verticalScrollBar().isVisible())
        rows = dialog.rows
        assert dialog.width() == 680 and rows.horizontalScrollBar().maximum() == 0
        # The delegate's natural width includes text plus the current style's padding.
        index = rows.model().index(0, 1)
        option = QStyleOptionViewItem()
        rows.initViewItemOption(option)
        required = rows.itemDelegateForIndex(index).sizeHint(option, index).width()
        assert rows.columnWidth(1) >= required
