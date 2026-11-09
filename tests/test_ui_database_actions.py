"""GUI actions stay responsive while canonical writes wait for synchronization."""
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Event as ThreadEvent, Thread
from time import monotonic, sleep

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QDialog

from debacal.models import Event
from debacal.ics_resources import ParseReport
from debacal.store import Store
from debacal.ui import MainWindow


APP = QApplication.instance() or QApplication([])
START = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def event(identity='event', **changes):
    return replace(Event(identity, 'local', 'Synthetic', START, START + timedelta(hours=1)), **changes)


def until(condition, timeout=8):
    deadline = monotonic() + timeout
    while not condition() and monotonic() < deadline:
        APP.processEvents()
        sleep(.001)
    assert condition()


@contextmanager
def held_writer(path):
    acquired, release = ThreadEvent(), ThreadEvent()
    errors = []
    def write():
        try:
            with Store(path, query_index=False) as store:
                with store._transaction():
                    store.connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('synthetic_writer','true')")
                    acquired.set()
                    assert release.wait(8)
        except BaseException as error:
            errors.append(error)
            acquired.set()
    thread = Thread(target=write)
    thread.start()
    assert acquired.wait(8)
    assert not errors
    try:
        yield
    finally:
        release.set()
        thread.join(8)
        assert not thread.is_alive()
        assert not errors


def test_queued_create_keeps_later_view_and_selection(tmp_path):
    path = tmp_path / 'db'
    with Store(path) as store:
        other = event('other', start=START + timedelta(days=60), end=START + timedelta(days=60, hours=1))
        store.apply_remote([other])
        window = MainWindow(store, enable_sync=False)
        try:
            with held_writer(path):
                begin = monotonic()
                window.apply_events('Create', [event('created')], on_success=lambda _value: window.open_day(START.date()))
                assert monotonic() - begin < .2
                assert window.database_commands.pending == 1
                window.open_day(other.start.date())
                window.select_event(other.id)
                later_context = window.view, window.anchor, window.selected_day, window.selected_id
            until(lambda: window.database_commands.pending == 0)
            assert store.get_event('created') is not None
            assert (window.view, window.anchor, window.selected_day, window.selected_id) == later_context
        finally:
            window.close()
            APP.processEvents()


def test_close_waits_for_accepted_create_to_be_durable(tmp_path):
    path = tmp_path / 'db'
    with Store(path) as store:
        window = MainWindow(store, enable_sync=False)
        try:
            with held_writer(path):
                window.apply_events('Create', [event('created')])
                assert window.database_commands.pending == 1
                window.close()
                assert not window._closed
            until(lambda: window._closed)
            assert store.get_event('created') is not None
        finally:
            window.close()
            APP.processEvents()


def test_close_waits_for_import_preparation_and_contended_commit(tmp_path):
    path = tmp_path / 'db'
    with Store(path) as store:
        window = MainWindow(store, enable_sync=False)
        future = Future()
        future.calendar_id = 'local'
        future.import_memory_bytes = 24
        window._import_memory_bytes = 24
        window._import_jobs.add(future)
        try:
            with held_writer(path):
                window.close()
                assert not window._closed
                future.set_result(ParseReport((event('imported', ical_uid='synthetic-import'),)))
                window.tasks.put(('import', future))
                begin = monotonic()
                window.poll()
                assert monotonic() - begin < .2
                assert window.database_commands.pending == 1
                assert future in window._import_jobs
                assert window._import_memory_bytes == 24
            until(lambda: window._closed)
            assert store.get_event('imported') is not None
            assert not window._import_jobs
            assert window._import_memory_bytes == 0
        finally:
            window.close()
            APP.processEvents()


def test_queued_editor_uses_snapshot_after_dialog_is_deleted(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    with Store(path) as store:
        original = event()
        store.apply_remote([original])
        window = MainWindow(store, enable_sync=False)
        window.open_day(START.date())
        window.select_event(original.id)
        instances = []
        class Signal:
            def connect(self, _callback):
                pass
        class Dialog:
            def __init__(self, _calendars, _start, _end, source, *_args, **_kwargs):
                self.result_events = [replace(source, title='Saved snapshot')]
                self.edit_reference = source
                self.schedule_replacement = False
                self.calendar_change_requested = False
                self.mode_changed = Signal()
                self.deleted = False
                instances.append(self)
            def exec(self):
                return QDialog.DialogCode.Accepted
            def deleteLater(self):
                self.deleted = True
                self.result_events.clear()
                self.edit_reference = None
        monkeypatch.setattr('debacal.ui.EventDialog', Dialog)
        try:
            with held_writer(path):
                begin = monotonic()
                window.edit_event(original.id)
                assert monotonic() - begin < .2
                assert instances[0].deleted
                assert window.database_commands.pending == 1
            until(lambda: window.database_commands.pending == 0)
            assert store.get_event(original.id).title == 'Saved snapshot'
            assert not window.local_issues
        finally:
            window.close()
            APP.processEvents()
