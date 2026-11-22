"""Bounded work during actual reminder-related calendar interactions."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import sqlite3
import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication
import pytest

from alarm_support import BASE
from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.recurrence import occurrence
from debacal.sequence_document import document_expression, is_sequence_document
from debacal.store import Store
from debacal.ui import MainWindow
from test_alarm_series import family
from test_reminder_editor import release_widget


@contextmanager
def alarm_work():
    counts = Counter()
    def record(frame, event, _):
        if event != 'call':
            return
        module, function = frame.f_globals.get('__name__'), frame.f_code.co_name
        if ((module == 'debacal.alarms' and function in ('from_dict', 'to_dict'))
            or (module == 'debacal.ics_resources' and function == '_blocks')
            or (module == 'debacal.alarm_ics' and function == 'read_alarms')
            or (module == 'debacal.alarm_series' and function == 'copied_alarms' and frame.f_locals['alarms'])
            or (module == 'debacal.store' and function == 'get_event')
            or (module == 'debacal.series_operations' and function == 'reidentify_series')):
            counts[function] += 1
    previous = sys.getprofile()
    sys.setprofile(record)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('gesture', ['drag', 'paste'])
def test_alarmed_placement_hovers_only_move_geometry(gesture, legacy):
    app = QApplication.instance() or QApplication([])
    master, = parse_ics(export_ics([family()]), 'local')
    if legacy:
        master = replace(master, alarms=None, recurrence_overrides={
            k: {field: v for field, v in p.items() if field != 'alarms'}
            for k, p in master.recurrence_overrides.items()})
    with Store(':memory:') as store:
        store.apply_remote([master])
        window = MainWindow(store, enable_sync=False, demo_mode=True)
        try:
            window.open_day(BASE.date())
            root = occurrence(master, BASE)
            window.select_event(root.id)
            window.set_selection_mode('group')
            if gesture == 'paste':
                window.copy_event(root.id)
                window.paste_events()
                assert window.pending_paste and not window.pending_paste.error
                window._paste_mouse_origin = QCursor.pos() + QPoint(2, 2)
            else:
                window.preview_drag(root.id, BASE, root.end, Qt.KeyboardModifier.NoModifier)
                assert window._drag_preview_events
            with alarm_work() as counts:
                for minutes, modifiers in [(15, Qt.KeyboardModifier.NoModifier), (30, Qt.KeyboardModifier.ControlModifier)]:
                    shift = timedelta(minutes=minutes)
                    if gesture == 'paste':
                        window.move_paste_preview(BASE + shift)
                        assert window.pending_paste.events[0].start == BASE + shift
                    else:
                        window.preview_drag(root.id, BASE + shift, root.end + shift, modifiers)
                        assert any(e.start == BASE + shift for e in window._drag_preview_events)
            assert not counts, counts
            if gesture == 'paste':
                window.confirm_paste()
            else:
                window.move_event(root.id, BASE + shift, root.end + shift, copy=True)
            result, = [event for event in store.list_events() if event.id != master.id]
            assert result.start == BASE + shift
            assert {a.trigger for a in result.alarms} == {'-PT15M', '20261122T100000Z'}
            child, = map(Event.from_dict, result.recurrence_overrides.values())
            assert child.alarms[0].trigger == '20261230T110000Z'
        finally:
            release_widget(window)


def test_document_controls_handle_range_and_sequence_without_alarm_content():
    master, = parse_ics(export_ics([family()]), 'local')
    master = replace(master, recurrence_rule='FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1', schedule_syntax=2,
                     recurrence_dates=tuple(master.recurrence_overrides))
    master = replace(master, schedule_expression=document_expression(master))
    dlg = EventDialog([Calendar('local', 'Example', '#556677')], BASE, event=occurrence(master, BASE),
                      series=master, edit_scope='group')
    try:
        assert is_sequence_document(dlg.pattern.text())
        with alarm_work() as counts:
            dlg.start.set_value(dlg.start.value() + timedelta(minutes=10))
            dlg.start.editingFinished.emit()
            dlg.duration.setText('75min')
            dlg.duration.editingFinished.emit()
            dlg.pattern.setText(dlg.pattern.text() + ' ')
            dlg.pattern.editingFinished.emit()
            assert dlg.calendar_occurrence_preview()[1]
        assert not counts, counts
        assert dlg.error.isHidden()
        expression = dlg.pattern.text()
        dlg.scope.setCurrentData('single')
        dlg.scope.setCurrentData('group')
        assert dlg.pattern.text() == expression
        full = dlg.calendar_occurrence_master()
        child_key, child_payload = next(iter(full.recurrence_overrides.items()))
        child = occurrence(full, Event.from_dict(child_payload).start, child_key)
        assert dlg.edit_calendar_occurrence(child, child.start + timedelta(minutes=12), child.end + timedelta(minutes=12))
        dlg.save()
        saved = dlg._project_preview(dlg.result_events[0])
        assert saved.start == BASE + timedelta(minutes=10)
        assert saved.end - saved.start == timedelta(minutes=75)
        assert saved.alarms == master.alarms
        child, = map(Event.from_dict, saved.recurrence_overrides.values())
        assert child.alarms == Event.from_dict(next(iter(master.recurrence_overrides.values()))).alarms
        wire, = parse_ics(export_ics([saved]), 'local')
        assert [(a.uid, a.trigger) for a in wire.alarms] == [(a.uid, a.trigger) for a in master.alarms]
        child_wire, = map(Event.from_dict, wire.recurrence_overrides.values())
        assert child_wire.start == child.start and child_wire.alarms[0].uid == child.alarms[0].uid
    finally:
        release_widget(dlg)


def test_disabled_startup_idle_and_single_source_update(tmp_path, monkeypatch):
    from alarm_support import FakeClock, FakeTransport
    from debacal import alarm_ics, reminder_worker
    from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
    from debacal.reminder_sources import ReminderSources
    from test_reminder_controller import event

    path = tmp_path / 'synthetic'
    clock = FakeClock(BASE - timedelta(minutes=1))
    worker = reminder_worker.ReminderWorker(str(path), clock, FakeTransport(), True, 'Example', lambda *_: None)
    calls = []
    def observe(obj, name):
        original = getattr(obj, name)
        def recorded(*args, **kwargs):
            calls.append((name, args[0].event_id if name == 'compile_schedule' else None))
            return original(*args, **kwargs)
        monkeypatch.setattr(obj, name, recorded)
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        values = [event(str(i), BASE) for i in range(20)]
        writer.apply_remote(values)
        for obj, name in [(Store, 'list_events'), (Store, 'get_events_with_alarms'), (ReminderSources, 'scan'),
                          (ReminderSources, '_change'), (alarm_ics, 'read_alarms'), (alarm_ics, 'hydrate_alarms'),
                          (reminder_worker, 'compile_schedule')]:
            observe(obj, name)
        try:
            worker.tick()
            assert not calls and worker.store is None
            save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
            while worker.tick() == .001:
                pass
            assert set(identity for name, identity in calls if name == 'compile_schedule') == {e.id for e in values}
            calls.clear()
            worker.tick()
            assert not calls
            writer.apply('One change', [replace(values[4], title='Rescheduled')])
            worker.tick()
            assert [identity for name, identity in calls if name == 'compile_schedule'] == ['4']
            assert not any(name in ('list_events', 'scan', 'read_alarms') for name, _ in calls)
        finally:
            worker.release()


def test_due_batches_use_frontier_index_and_drain_once():
    from contextlib import closing
    from debacal.alarms import Alarm
    from debacal.event_range_index import EventRangeIndex
    from debacal.reminder_sources import SourceChange
    from debacal.reminder_store import ReminderStore
    from test_reminder_controller import event
    from test_reminder_store import put
    source = event('batch', BASE, *(Alarm(str(i), 'DISPLAY', 'PT0S', uid=str(i)) for i in range(37)))
    with closing(ReminderStore(':memory:')) as queue:
        queue.set_context(now=BASE, generation=0)
        put(queue, SourceChange(1, source.id, source, EventRangeIndex.prepare(source)))
        statements = []
        queue._db.set_trace_callback(statements.append)
        keys = []
        while queue.next_wakeup() is not None:
            batch = queue.claim_due(BASE, limit=5)
            assert 0 < len(batch) <= 5
            keys.extend(item.key for item in batch)
        queue._db.set_trace_callback(None)
        query = next(s for s in statements if 'FROM frontier ORDER BY' in s)
        plans = [str(row[-1]) for row in queue._db.execute('EXPLAIN QUERY PLAN ' + query)]
        assert any('frontier_order' in plan for plan in plans)
        assert not any('TEMP B-TREE' in plan for plan in plans)
        assert len(keys) == len(set(keys)) == 37



def test_ownerless_action_can_commit_while_historical_group_geometry_is_computed(tmp_path, monkeypatch):
    from contextlib import closing
    from debacal.reminder_compilation import AlarmSchedule
    from debacal.reminder_owner import ReminderOwner
    from debacal.reminder_store import ReminderStore
    from test_reminder_controller import event
    from test_reminder_store import put
    path = str(tmp_path/'profile')
    owner = ReminderOwner(path)
    assert owner.acquire()
    try:
        with Store(path) as writer, closing(ReminderStore(path)) as queue, closing(ReminderStore(path)) as observer:
            writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
            master = event('history', BASE, recurrence_rule='FREQ=HOURLY')
            writer.apply_remote([master])
            queue.attach_owner(owner, now=BASE)
            put(queue, queue.read_sources('selected', (master.id,))[0])
            delivered, = queue.claim_due(BASE)
            queue.record_delivery(delivered.key, desktop_id=None, unread=True)
            queue.snooze(delivered.key, BASE + timedelta(minutes=8))
            original = AlarmSchedule.due
            actions = []
            def project(schedule, *args):
                if not actions:
                    observer.dismiss(delivered.key)
                    actions.append(True)
                return original(schedule, *args)
            monkeypatch.setattr(AlarmSchedule, 'due', project)
            writer.apply_remote([replace(master, end=master.end + timedelta(minutes=2))])
            put(queue, queue.read_sources('selected', (master.id,))[0])
            assert actions and observer.entry(delivered.key).status == 'dismissed'
    finally:
        owner.release()



@pytest.mark.parametrize('interrupt', ['new_revision', 'replacement'])
def test_history_pages_yield_to_actions_and_fence_stale_publication(tmp_path, monkeypatch, interrupt):
    from contextlib import closing
    from datetime import timezone
    from debacal import reminder_ledger
    from debacal.alarms import Alarm
    from debacal.reminder_owner import ReminderOwner
    from debacal.reminder_preferences import CalendarReminderPolicy
    from debacal.reminder_schedule import compile_schedule
    from debacal.reminder_store import ReminderStore, StaleSourcePage
    from test_reminder_controller import event
    from test_reminder_store import put
    path = tmp_path/'profile'
    owner = ReminderOwner(str(path))
    assert owner.acquire()
    try:
        with Store(path) as writer, closing(ReminderStore(str(path))) as queue, closing(ReminderStore(str(path))) as peer:
            writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
            master = event('history', BASE, *(Alarm(str(i), 'DISPLAY', 'PT0S') for i in range(70)))
            writer.apply_remote([master])
            queue.attach_owner(owner, now=BASE)
            put(queue, queue.read_sources('selected', (master.id,))[0])
            batch = queue.claim_due(BASE)
            assert len(batch) == 70
            for item in batch:
                queue.record_delivery(item.key, desktop_id=None, unread=True)
            before = queue.cursor
            changed = replace(master, end=master.end+timedelta(minutes=1))
            writer.apply_remote([changed])
            change, = queue.read_sources('changes', before)
            work = queue.put_source_steps(change, compile_schedule(change, CalendarReminderPolicy(), zone=timezone.utc),
                                          cursor=(change.version, change.event_id))
            original = reminder_ledger._staged_group
            calls = []
            def record(*args):
                calls.append(1)
                return original(*args)
            monkeypatch.setattr(reminder_ledger, '_staged_group', record)
            next(work)
            assert len(calls) == reminder_ledger.HISTORY_PAGE == 32
            assert not queue._db.in_transaction
            peer.dismiss(batch[0].key)
            peer.snooze(batch[-1].key, BASE+timedelta(minutes=9))
            assert queue.cursor == before
            if interrupt == 'new_revision':
                writer.apply_remote([replace(changed, title='New revision')])
            else:
                target = tmp_path/'replacement'
                with Store(target) as fresh:
                    fresh.apply_remote([replace(changed, calendar_id='local')])
                target.replace(path)
            with pytest.raises((StaleSourcePage, sqlite3.OperationalError)):
                list(work)
            assert queue.cursor != (change.version, change.event_id)
            if interrupt == 'new_revision':
                latest, = queue.read_sources('changes', before)
                put(queue, latest)
                assert peer.entry(batch[0].key).status == 'dismissed'
                assert peer.entry(batch[-1].key).status == 'snoozed'
    finally:
        owner.release()


@pytest.mark.local_performance
def test_synthetic_reminder_benchmark_reports_complete_workflows(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    root = Path(__file__).resolve().parents[1]
    script = root/'scripts/benchmark_reminders.py'
    if not script.exists():
        pytest.skip('The source archive excludes maintainer benchmarks.')
    output = tmp_path/'measurements.json'
    subprocess.run([sys.executable, str(script), '--sources', '180', '--rounds', '1', '--output', str(output)],
                   check=True, env=os.environ | {'QT_QPA_PLATFORM':'offscreen'}, timeout=180)
    report = json.loads(output.read_text())
    assert report['workload']['sources'] == 180
    assert report['rounds'][1]['index']['retained_partition']
    assert report['rounds'][1]['history']['groups'] >= 47
    assert {'enable_and_build', 'drain_due_cohort', 'held_writer_inbox', 'history_rule_edit'} <= report['operations'].keys()

def test_owner_thread_services_inbox_request_between_history_pages(tmp_path, monkeypatch):
    from threading import current_thread
    from alarm_support import FakeClock, wait_for
    from debacal import reminder_ledger
    from debacal.alarms import Alarm
    from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
    from debacal.reminder_worker import ReminderWorker
    from test_reminder_controller import event, running

    path = tmp_path / 'calendar'
    clock = FakeClock(BASE - timedelta(minutes=1))
    source = event('many', BASE, *(Alarm(str(i), 'DISPLAY', 'PT0S', uid=str(i)) for i in range(100)))
    with Store(path) as writer:
        writer.save_calendar(Calendar('c', 'Calendar', '#123456'))
        writer.apply_remote([source])
        save_reminder_preferences(writer, ReminderPreferences(enabled=True, delivery='in_app'), now=clock.now())
        with running(path, clock) as (controller, _, page):
            assert not page()
            clock.time = BASE
            controller.reload_settings()
            wait_for(lambda: controller.unread_count == 100)
            counts = []
            observed = []
            original = reminder_ledger._staged_group
            command = ReminderWorker.command
            def project(*args):
                counts.append(1)
                if len(counts) == reminder_ledger.HISTORY_PAGE:
                    current_thread().enqueue('page', 9900, 1, 0, False)
                return original(*args)
            def handle(self, kind, args):
                if kind == 'page' and args[0] == 9900:
                    observed.append((len(counts), self.store.cursor, self.store._db.in_transaction))
                return command(self, kind, args)
            monkeypatch.setattr(reminder_ledger, '_staged_group', project)
            monkeypatch.setattr(ReminderWorker, 'command', handle)
            writer.apply('Change duration', [replace(source, end=source.end+timedelta(minutes=1))])
            revision = writer.events_revision()
            controller.reload_settings()
            wait_for(lambda: observed)
            computed, cursor, in_transaction = observed[0]
            assert computed in (reminder_ledger.HISTORY_PAGE, 2*reminder_ledger.HISTORY_PAGE)
            assert cursor[0] < revision and not in_transaction
            wait_for(lambda: len(counts) == 100)
            assert len(page()) == 100
