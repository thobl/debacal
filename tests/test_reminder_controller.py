from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from PySide6.QtWidgets import QApplication

from alarm_support import BASE, FakeClock, FakeTransport, alarm_event, wait_for
from debacal.alarms import Alarm
from debacal.models import Calendar
from debacal.reminder_controller import ReminderController
from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
from debacal.store import Store

MINUTE = timedelta(minutes=1)
ALARM = Alarm('alarm', 'DISPLAY', 'PT0S', uid='alarm')


def event(identity, start, *alarms, **kwargs):
    return alarm_event(id=identity, ical_uid=identity, start=start, end=start + 5*MINUTE,
                       calendar_id='c', alarms=alarms or (ALARM,), **kwargs)


@contextmanager
def running(path, clock, *, allowed=True):
    app = QApplication.instance() or QApplication([])
    transport = FakeTransport()
    controller = ReminderController(str(path), clock=clock, transport=transport, delivery_allowed=allowed)
    pages = {}
    controller.inboxPageReady.connect(lambda request, entries: pages.update({request: entries}))
    controller.start()
    def page():
        request = controller.request_inbox()
        wait_for(lambda: request in pages)
        return pages.pop(request)
    try:
        yield controller, transport, page
    finally:
        controller.stop()


def test_catchup_mute_restart_and_slow_transport(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE - 3*MINUTE)
    prefs = ReminderPreferences(enabled=True, sound='silent')
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Appointments', '#112233'))
        writer.set_calendar_visible('c', False)
        writer.set_calendar_in_main_list('c', False)
        writer.apply_remote([event('previous', BASE-4*MINUTE), event('current', BASE)])
        with running(path, clock) as (controller, transport, page):
            assert page() == () and not transport.started
            save_reminder_preferences(writer, prefs, now=clock.now())
            controller.reload_settings()
            wait_for(lambda: transport.started)
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: transport.sent)
            due, calendar, _, silent = transport.sent[0]
            assert (due.key.source_id, calendar, silent) == ('current', 'Appointments', True)
            assert [r.status for r in page()] == ['pending']
            writer.apply_remote([replace(event('current', BASE), title='Renamed')])
            controller.reload_settings()
            transport.delivered.emit(due.key, 0xffffffff)
            wait_for(lambda: any(r.unread and r.reminder.title == 'Renamed' for r in page()))
            controller.snooze(due.key, 2*MINUTE)
            wait_for(lambda: page()[0].status == 'snoozed')
        # Restart, then missed repeats coalesce per occurrence. Snooze is explicit.
        clock.time += 2*MINUTE
        with running(path, clock) as (controller, transport, page):
            wait_for(lambda: transport.sent)
            assert transport.sent[0][0].title == 'Renamed'
            transport.delivered.emit(due.key, 8)
            writer.apply_remote([event('late', BASE + MINUTE, replace(ALARM, repeat=1, repeat_interval='PT10S'))])
            controller.reload_settings()
            wait_for(lambda: len(transport.sent) == 2)
            assert transport.sent[-1][0].key.source_id == 'late'
            assert len([r for r in page() if r.reminder.key.source_id == 'late']) == 1
            controller.snooze(due.key, MINUTE)
            save_reminder_preferences(writer, replace(prefs, enabled=False), now=clock.now())
            controller.reload_settings()
            wait_for(lambda: not transport.started)
            writer.apply_remote([event('muted', clock.now()+MINUTE)])
            clock.time += 2*MINUTE
            save_reminder_preferences(writer, prefs, now=clock.now())
            controller.reload_settings()
            wait_for(lambda: transport.started)
            assert not any(r.status == 'snoozed' for r in page())
            assert len(transport.sent) == 2


@pytest.mark.parametrize('delivery,native,unread', [('both', True, True), ('in_app', False, True),
                                                   ('desktop', True, False)])
def test_delivery_modes_and_explicit_action(tmp_path, delivery, native, unread):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('e', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery=delivery), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            clock.time = BASE
            controller.reload_settings()
            opened = []
            controller.activate.connect(opened.append)
            if native:
                wait_for(lambda: transport.sent)
                transport.delivered.emit(transport.sent[0][0].key, 22)
            entry = wait_for(lambda: next((r for r in page() if r.status=='delivered'), None))
            assert entry.unread == unread
            assert bool(transport.sent) == native and not opened
            controller.open_reminder(entry.reminder.key)
            wait_for(lambda: opened)
            assert opened == [entry.reminder] and not page()[0].unread


def test_delivery_gate_never_opens_native_or_scans(tmp_path, monkeypatch):
    from debacal.reminder_store import ReminderStore
    path = tmp_path/'profile'
    clock = FakeClock()
    scans = []
    monkeypatch.setattr(ReminderStore, 'read_sources', lambda *args, **kwargs: scans.append(args))
    with Store(path) as writer:
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=BASE)
        with running(path, clock, allowed=False) as (controller, transport, page):
            assert page() == ()
            controller.test_notification(ReminderPreferences(enabled=True))
            assert page() == () and not scans and not transport.started


def test_missed_time_window_absolute_series_and_jitter(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE - timedelta(days=2))
    absolute = replace(ALARM, trigger='20261121T090000Z')
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        prefs = ReminderPreferences(enabled=True, delivery='in_app')
        save_reminder_preferences(writer, prefs, now=clock.now())
        with running(path, clock) as (controller, _, page):
            assert page() == ()
            clock.time = BASE - timedelta(milliseconds=800)
            writer.apply_remote([event('armed', BASE - timedelta(days=4), absolute)])
            controller.reload_settings()
            assert page() == ()
            clock.time = BASE + timedelta(milliseconds=200)
            assert wait_for(lambda: any(r.reminder.key.source_id == 'armed' for r in page()))
            # A newly discovered, already-ended event at the same trigger is missed.
            writer.apply_remote([event('ended', BASE-timedelta(days=4), absolute),
                                 event('series', BASE-timedelta(days=4), absolute, recurrence_rule='FREQ=WEEKLY'),
                                 event('resume', BASE-timedelta(days=4), replace(ALARM, trigger='20261121T090100Z'))])
            controller.reload_settings()
            wait_for(lambda: any(r.reminder.key.source_id == 'series' for r in page()))
            assert {r.reminder.key.source_id for r in page()} == {'armed','series'}
            clock.time += timedelta(hours=1)
            assert {r.reminder.key.source_id for r in page()} == {'armed','series'}
            prefs = replace(prefs, missed='all')
            save_reminder_preferences(writer, prefs, now=clock.now())
            writer.apply_remote([event('boundary', clock.now()-timedelta(days=1)),
                                 event('older', clock.now()-timedelta(days=1, microseconds=1))])
            controller.reload_settings()
            rows = wait_for(lambda: (rows if any(r.reminder.key.source_id == 'boundary' for r in rows) else None)
                            if (rows := page()) else None)
            assert {r.reminder.key.source_id for r in rows} == {'armed','series','boundary'}


def test_slow_discovery_keeps_local_unread_and_revalidates_settings(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer, running(path, clock) as (controller, transport, page):
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('e', BASE)])
        transport.ready = None
        prefs = ReminderPreferences(enabled=True)
        save_reminder_preferences(writer, prefs, now=clock.now())
        controller.reload_settings()
        assert page() == ()
        clock.time = BASE
        entry = wait_for(lambda: next(iter(page()), None))
        assert entry.status == 'pending' and entry.unread and not transport.sent
        controller.mark_read((entry.reminder.key,))
        writer.apply_remote([replace(event('e', BASE), title='Current title')])
        save_reminder_preferences(writer, replace(prefs, sound='silent', import_mode='remove'), now=clock.now())
        transport.ready = True
        transport.availabilityChanged.emit(True)
        wait_for(lambda: transport.sent)
        assert transport.sent[0][0].title == 'Current title' and transport.sent[0][-1]
        transport.delivered.emit(entry.reminder.key, 10)
        final = wait_for(lambda: next((r for r in page() if r.status=='delivered'), None))
        assert not final.unread


def test_absent_service_recovers_without_resending_and_failure_issue_is_stable(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer, running(path, clock) as (controller, transport, page):
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('first', BASE), event('second', BASE+MINUTE)])
        issues = []
        controller.issueChanged.connect(issues.append)
        transport.ready = False
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        controller.reload_settings()
        assert page() == ()
        clock.time = BASE
        wait_for(lambda: any(r.status=='uncertain' for r in page()))
        assert len(issues)==1 and issues[0]
        transport.ready = True
        transport.availabilityChanged.emit(True)
        clock.time += MINUTE
        wait_for(lambda: transport.sent)
        assert [x[0].key.source_id for x in transport.sent] == ['second']
        transport.failed.emit(transport.sent[0][0].key, 'Timeout')
        wait_for(lambda: len([r for r in page() if r.status=='uncertain'])==2)
        assert issues[-1] and page() and issues[-1]


def test_local_mute_source_disable_and_semantic_reschedule(tmp_path):
    from debacal.reminder_preferences import CalendarReminderPolicy
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    prefs = ReminderPreferences(enabled=True, delivery='in_app', missed='all')
    with Store(path) as writer:
        enabled = Calendar('c', 'Example', '#123456')
        writer.save_calendar(enabled)
        writer.save_calendar(replace(enabled, id='off', enabled=False))
        writer.apply_remote([event('e', BASE), replace(event('disabled', BASE), calendar_id='off')])
        save_reminder_preferences(writer, prefs, now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            clock.time = BASE
            original = wait_for(lambda: next(iter(page()), None)).reminder
            assert [r.reminder.key.source_id for r in page()] == ['e']
            controller.snooze(original.key, 8*MINUTE)
            wait_for(lambda: page()[0].status=='snoozed')
            writer.apply_remote([event('e', BASE+2*MINUTE)])
            controller.reload_settings()
            wait_for(lambda: page()[0].status=='dismissed')
            clock.time += 2*MINUTE
            wait_for(lambda: len(page())==2)
            writer.apply_remote([event('e', BASE+MINUTE)])
            controller.reload_settings()
            assert len(page()) == 2
            later = event('future', BASE+3*MINUTE)
            writer.apply_remote([later])
            save_reminder_preferences(writer, replace(prefs, calendars={'c': CalendarReminderPolicy('mute')}),
                                      now=clock.now())
            controller.reload_settings()
            clock.time += 2*MINUTE
            assert len(page()) == 2
            save_reminder_preferences(writer, prefs, now=clock.now())
            controller.reload_settings()
            assert len(page()) == 2 and not transport.sent


def test_disabled_startup_and_unrelated_defaults_skip_compilation(tmp_path, monkeypatch):
    from debacal import reminder_worker
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    calls = []
    original = reminder_worker.compile_schedule
    def compile_spy(change, *args, **kwargs):
        calls.append(change.event_id)
        return original(change, *args, **kwargs)
    monkeypatch.setattr(reminder_worker, 'compile_schedule', compile_spy)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('e', BASE)])
        with running(path, clock) as (controller, transport, page):
            assert page() == () and not calls
            assert not path.with_name('profile.reminders.sqlite3').exists()
            prefs = ReminderPreferences(enabled=True)
            save_reminder_preferences(writer, prefs, now=clock.now())
            controller.reload_settings()
            wait_for(lambda: transport.started)
            assert page() == () and calls
            calls.clear()
            save_reminder_preferences(writer, replace(prefs, new_timed=(ALARM,), import_mode='remove',
                                                      sound='silent'), now=clock.now())
            controller.reload_settings()
            clock.time = BASE
            wait_for(lambda: transport.sent)
            assert not calls


def test_dismiss_native_and_delayed_callbacks_after_mute(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('one', BASE), event('two', BASE)])
        prefs = ReminderPreferences(enabled=True)
        save_reminder_preferences(writer, prefs, now=clock.now())
        with running(path, clock) as (controller, transport, page):
            opened = []
            controller.activate.connect(opened.append)
            assert page() == ()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: len(transport.sent)==2)
            first, second = (x[0].key for x in transport.sent)
            transport.delivered.emit(first, 1)
            transport.action.emit(first, 'dismiss')
            wait_for(lambda: any(r.status=='dismissed' for r in page()))
            transport.action.emit(first, 'open')
            assert page() and not opened
            save_reminder_preferences(writer, replace(prefs, enabled=False), now=clock.now())
            controller.reload_settings()
            wait_for(lambda: not transport.started)
            transport.action.emit(second, 'open')
            transport.delivered.emit(second, 2)
            rows = page()
            assert not opened and {r.status for r in rows}=={'dismissed','uncertain'}


def test_calendar_lock_does_not_delay_gui_actions(tmp_path):
    import sqlite3
    from PySide6.QtCore import QTimer
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('e', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            conn = sqlite3.connect(str(path)+'.reminders.sqlite3', timeout=0)
            try:
                conn.execute('BEGIN IMMEDIATE')
                clock.time = BASE
                controller.reload_settings()
                pumped = []
                QTimer.singleShot(0, lambda: pumped.append(True))
                wait_for(lambda: pumped)
                assert not transport.sent
            finally:
                conn.rollback()
                conn.close()
            wait_for(lambda: transport.sent)


def test_master_absolute_missed_dense_series_seeks_past_range_guard(tmp_path):
    path = tmp_path/'profile'
    now = BASE.replace(year=2050, hour=23, minute=55, second=40)
    clock = FakeClock(now-timedelta(minutes=2))
    numbers = lambda n: ','.join(map(str, range(n)))
    rule = f'FREQ=DAILY;BYHOUR={numbers(24)};BYMINUTE={numbers(60)};BYSECOND={numbers(42)}'
    absolute = replace(ALARM, trigger=(now-MINUTE).strftime('%Y%m%dT%H%M%SZ'))
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            clock.time = now
            writer.apply_remote([event('series', BASE, absolute, recurrence_rule=rule)])
            controller.reload_settings()
            row = wait_for(lambda: next(iter(page()), None))
            assert row.reminder.key.source_id == 'series' and row.unread


@pytest.mark.parametrize('action', ['dismiss', 'snooze', 'disable', 'handoff'])
def test_discovery_pending_cannot_revive_retired_claim(tmp_path, action):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    prefs = ReminderPreferences(enabled=True)
    with Store(path) as writer, running(path, clock) as (controller, transport, page):
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('e', BASE)])
        transport.ready = None
        save_reminder_preferences(writer, prefs, now=clock.now())
        controller.reload_settings()
        assert page() == ()
        clock.time = BASE
        key = wait_for(lambda: next(iter(page()), None)).reminder.key
        if action == 'dismiss':
            controller.dismiss(key)
            wait_for(lambda: page()[0].status=='dismissed')
        elif action == 'snooze':
            controller.snooze(key, MINUTE)
            wait_for(lambda: page()[0].status=='snoozed')
        elif action == 'disable':
            save_reminder_preferences(writer, replace(prefs, enabled=False), now=clock.now())
            controller.reload_settings()
            wait_for(lambda: not transport.started)
        else:
            controller.stop()
            with running(path, clock) as (following, other, new_page):
                wait_for(lambda: new_page() and new_page()[0].status=='uncertain')
                assert not other.sent
            return
        transport.ready = True
        transport.availabilityChanged.emit(True)
        assert page() and not transport.sent


def test_identity_retarget_while_native_service_is_discovering(tmp_path):
    first, second, alias = tmp_path/'first', tmp_path/'second', tmp_path/'alias'
    alias.symlink_to(first)
    clock = FakeClock(BASE-MINUTE)
    with Store(alias) as writer, Store(second) as replacement, running(alias, clock) as (controller, transport, page):
        for target in (writer, replacement):
            target.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('old', BASE)])
        replacement.apply_remote([event('new', BASE+MINUTE)])
        transport.ready = None
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        controller.reload_settings()
        assert page() == ()
        clock.time = BASE
        wait_for(lambda: next(iter(page()), None))
        alias.unlink()
        alias.symlink_to(second)
        controller.reload_settings()
        transport.ready = True
        transport.availabilityChanged.emit(True)
        wait_for(lambda: not page() and transport.started)
        clock.time += MINUTE
        controller.reload_settings()
        wait_for(lambda: transport.sent)
        assert [x[0].key.source_id for x in transport.sent] == ['new']


def test_canonical_edit_in_final_handoff_refreshes_unsent_title(tmp_path, monkeypatch):
    from debacal.reminder_store import ReminderStore
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    source = event('e', BASE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True), now=clock.now())
        can_emit = ReminderStore.can_emit
        edited = []
        def concurrent_title_edit(store, due, fence):
            if not edited:
                writer.apply_remote([replace(source, title='Revised during handoff')])
                edited.append(True)
            return can_emit(store, due, fence)
        monkeypatch.setattr(ReminderStore, 'can_emit', concurrent_title_edit)
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: transport.sent)
            assert transport.sent[0][0].title == 'Revised during handoff'


def test_rapid_global_mute_between_polls_fences_native_actions_and_pending_reply(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('one', BASE), event('two', BASE)])
        prefs = ReminderPreferences(enabled=True)
        save_reminder_preferences(writer, prefs, now=clock.now())
        with running(path, clock) as (controller, transport, page):
            opened = []
            controller.activate.connect(opened.append)
            assert page() == ()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: len(transport.sent) == 2)
            one, two = (x[0].key for x in transport.sent)
            transport.delivered.emit(one, 1)
            wait_for(lambda: any(r.status=='delivered' for r in page()))
            with writer.ui_transaction():
                save_reminder_preferences(writer, replace(prefs, enabled=False), now=clock.now())
                save_reminder_preferences(writer, prefs, now=clock.now()+MINUTE)
            controller.reload_settings()
            transport.action.emit(one, 'open')
            transport.delivered.emit(two, 2)
            assert {r.status for r in page()} == {'uncertain', 'delivered'} and not opened


def test_local_action_waits_out_brief_sidecar_write_contention(tmp_path):
    import sqlite3
    from time import sleep
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('one', BASE)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            clock.time = BASE
            key = wait_for(lambda: next(iter(page()), None)).reminder.key
            conn = sqlite3.connect(str(path)+'.reminders.sqlite3', timeout=0)
            try:
                conn.execute('BEGIN IMMEDIATE')
                controller.dismiss(key)
                sleep(.1)
            finally:
                conn.rollback()
                conn.close()
            wait_for(lambda: page()[0].status=='dismissed')


def test_none_missed_and_malformed_neighbor_keep_on_time_alert(tmp_path):
    path = tmp_path/'profile'
    clock = FakeClock(BASE-2*MINUTE)
    prefs = ReminderPreferences(enabled=True, delivery='in_app', missed='none')
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        save_reminder_preferences(writer, prefs, now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            writer.apply_remote([event('overdue', BASE-MINUTE), event('now', BASE),
                                 event('broken', BASE, replace(ALARM, trigger='bad interval'))])
            clock.time = BASE
            controller.reload_settings()
            rows = wait_for(page)
            assert [r.reminder.key.source_id for r in rows] == ['now']


def test_zone_change_rebinds_civil_sources_before_claim(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    from debacal import reminder_worker
    path = tmp_path/'profile'
    clock = FakeClock(BASE-MINUTE)
    monkeypatch.setattr(reminder_worker, 'local_zone', lambda: ZoneInfo('UTC'))
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Example', '#123456'))
        writer.apply_remote([event('utc', BASE), event('floating', BASE, floating_time=True)])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, transport, page):
            assert page() == ()
            monkeypatch.setattr(reminder_worker, 'local_zone', lambda: ZoneInfo('America/New_York'))
            clock.time = BASE
            controller.reload_settings()
            rows = wait_for(page)
            assert [r.reminder.key.source_id for r in rows] == ['utc']
            clock.time += timedelta(hours=5)
            wait_for(lambda: any(r.reminder.key.source_id == 'floating' for r in page()))


def test_test_notification_uses_draft_without_global_enable_or_ledger(tmp_path):
    path = tmp_path/'profile'
    with Store(path), running(path, FakeClock()) as (controller, transport, page):
        previews = []
        controller.testNotificationShown.connect(previews.append)
        controller.test_notification(ReminderPreferences(delivery='both', sound='silent'))
        wait_for(lambda: previews and transport.sent)
        assert transport.sent[0][-1] and transport.sent[0][0] == previews[0]
        transport.delivered.emit(previews[0].key, 1)
        wait_for(lambda: not transport.started)
        assert page() == () and not path.with_name('profile.reminders.sqlite3').exists()
