"""A blocked calendar writer must not occupy the GUI thread for any mutation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, Thread, get_ident
from time import monotonic

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui_database import DatabaseCommands


START = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def wait_until(app, condition, timeout=4):
    deadline = monotonic() + timeout
    while not condition() and monotonic() < deadline:
        app.processEvents()
        QTest.qWait(2)
    assert condition()


def hold_writer(path):
    ready, release, errors = ThreadEvent(), ThreadEvent(), []
    def hold():
        try:
            with Store(path, query_index=False) as writer:
                with writer._transaction():
                    writer.set_setting('synthetic_sync_marker', True)
                    ready.set()
                    assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()
    thread = Thread(target=hold)
    thread.start()
    assert ready.wait(3)
    assert not errors
    return release, thread, errors


@pytest.mark.parametrize('kind', ['create', 'edit', 'delete', 'move', 'undo', 'redo', 'source', 'confirm'])
def test_every_canonical_mutation_returns_control_while_sync_writer_remains_active(tmp_path, kind):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'calendar.sqlite3'
    source = Event('source', 'local', 'Original', START, START + timedelta(hours=1))
    if kind == 'confirm':
        source = replace(source, candidate=True, group_id='choices')
    with Store(path) as store:
        store.apply('Create', [source])
        if kind == 'confirm':
            store.apply('Other choice', [replace(source, id='other-choice', ical_uid='other-choice',
                                               start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1))])
        change = store.apply('Rename', [replace(source, title='Renamed')])
        if kind == 'redo':
            store.undo(change)
        current = store.get_occurrence(source.id)
        new = replace(source, id='new', ical_uid='new')
        operations = {
            'create': lambda writer: writer.apply('Create another', [new]),
            'edit': lambda writer: writer.edit_selection([current], 'single', current, replace(current, title='Edited')),
            'delete': lambda writer: writer.delete_selection([current], 'single'),
            'move': lambda writer: writer.move_selection([current], 'single', current, START + timedelta(days=2),
                                                        START + timedelta(days=2, hours=1)),
            'undo': lambda writer: writer.undo(change),
            'redo': lambda writer: writer.redo(change),
            'source': lambda writer: writer.save_calendar(Calendar('another', 'Another', '#123456')),
            'confirm': lambda writer: writer.confirm_candidate(current),
        }
        errors, completed, pulses = [], [], []
        gui_thread = get_ident()
        commands = DatabaseCommands(store, on_error=errors.append)
        release, thread, background_errors = hold_writer(path)
        timer = QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.start(2)
        try:
            immediate = commands.write(operations[kind], lambda result: completed.append((result, get_ident())))
            assert immediate is False
            assert commands.pending == 1
            assert not release.is_set() and not completed
            # Other UI work, including fresh range reads and preference writes,
            # must run before the calendar writer is allowed to finish.
            store.query_occurrences(START, START + timedelta(days=3))
            store.set_calendar_visible('local', False)
            store.set_setting('show_full_day', True)
            wait_until(app, lambda: len(pulses) >= 3)
            assert not release.is_set() and not completed
            release.set()
            thread.join(3)
            wait_until(app, lambda: not commands.pending)
            assert not errors and not background_errors
            assert len(completed) == 1 and completed[0][1] == gui_thread
            if kind == 'create':
                assert store.get_event(new.id) is not None
            elif kind == 'delete':
                assert store.get_event(source.id) is None
            elif kind == 'move':
                assert store.get_event(source.id).start == START + timedelta(days=2)
            elif kind == 'edit':
                assert store.get_event(source.id).title == 'Edited'
            elif kind == 'undo':
                assert store.get_event(source.id).title == 'Original'
            elif kind == 'redo':
                assert store.get_event(source.id).title == 'Renamed'
            elif kind == 'confirm':
                assert not store.get_event(source.id).candidate
                assert store.get_event('other-choice') is None
            else:
                assert store.get_calendar('another') is not None
        finally:
            timer.stop()
            release.set()
            thread.join(3)
            commands.shutdown()


@pytest.mark.parametrize('queued', [False, True])
def test_multi_step_command_rolls_back_together_and_does_not_replay(tmp_path, queued):
    app = QApplication.instance() or QApplication([])
    with Store(tmp_path / 'atomic.sqlite3') as store:
        commands = DatabaseCommands(store)
        errors, calls = [], []
        def fail_after_write(writer):
            calls.append(True)
            writer.save_calendar(Calendar('partial', 'Partial', '#123456'))
            raise ValueError('Abort entire captured command')
        try:
            commands.write(fail_after_write, on_error=errors.append, force_async=queued)
            wait_until(app, lambda: not commands.pending)
            assert len(calls) == 1 and len(errors) == 1
            assert store.get_calendar('partial') is None
        finally:
            commands.shutdown()
            app.processEvents()


def test_queued_commands_keep_order_report_failure_and_continue(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as store:
        commands = DatabaseCommands(store)
        release, thread, background_errors = hold_writer(path)
        seen, errors = [], []
        item = Event('queued', 'local', 'Queued', START, START + timedelta(hours=1))
        try:
            assert not commands.write(lambda writer: writer.apply('Create', [item]), lambda result: seen.append('created'))
            def fail(_writer):
                raise ValueError('Synthetic save failure')
            assert not commands.write(fail, on_error=errors.append)
            assert not commands.write(lambda writer: writer.undo(), lambda result: seen.append('undone'))
            release.set()
            thread.join(3)
            wait_until(app, lambda: not commands.pending)
            assert seen == ['created', 'undone']
            assert len(errors) == 1 and str(errors[0]) == 'Synthetic save failure'
            assert store.get_event(item.id) is None
            assert not background_errors
        finally:
            release.set()
            thread.join(3)
            commands.shutdown()


def test_queued_user_write_gets_a_turn_before_sync_reacquires_the_writer(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'priority.sqlite3'
    ready, release = ThreadEvent(), ThreadEvent()
    order, errors = [], []
    with Store(path) as store:
        commands = DatabaseCommands(store, on_error=errors.append)
        def sync():
            try:
                with Store(path, query_index=False) as writer:
                    with writer._transaction():
                        ready.set()
                        assert release.wait(5)
                    # A sync with many resources must yield here, rather than
                    # repeatedly winning the writer ahead of the user action.
                    writer.save_calendar(Calendar('sync', 'Sync', '#123456'))
                    order.append('sync')
            except BaseException as error:
                errors.append(error)
                ready.set()
        thread = Thread(target=sync)
        thread.start()
        try:
            assert ready.wait(3) and not errors
            def user(writer):
                writer.save_calendar(Calendar('user', 'User', '#123456'))
                order.append('user')
            assert not commands.write(user)
            release.set()
            wait_until(app, lambda: not commands.pending and len(order) == 2)
            thread.join(3)
            assert order == ['user', 'sync'] and not errors
        finally:
            release.set()
            thread.join(3)
            commands.shutdown()


def test_projection_error_diagnostics_are_persisted_off_the_gui_thread(tmp_path):
    import json
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as store:
        item = Event('bad', 'local', 'Synthetic', START, START + timedelta(hours=1))
        store.apply_remote([item])
        with store._transaction():
            store.connection.execute('UPDATE events SET data=?,recurring=1 WHERE id=?',
                                     (json.dumps(item.to_dict() | {'recurrence_rule': 'FREQ=INVALID', 'raw_ics': 'synthetic retained resource'}), item.id))
        errors = []
        commands = DatabaseCommands(store, on_error=errors.append)
        release, thread, background_errors = hold_writer(path)
        try:
            assert not store.query_occurrences(START, START + timedelta(days=1))
            assert commands.pending
            assert not store.list_quarantined_resources()
            release.set()
            thread.join(3)
            wait_until(app, lambda: not commands.pending)
            assert not errors and not background_errors
            assert store.list_quarantined_resources()[0]['raw_ics'] == 'synthetic retained resource'
        finally:
            release.set()
            thread.join(3)
            commands.shutdown()
