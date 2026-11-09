"""View-only visibility must remain responsive while synchronization writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event as Signal, Thread
from time import perf_counter
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow


START = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)


def synthetic(identifier='event', calendar_id='local'):
    return Event(identifier, calendar_id, 'Synthetic event', START, START + timedelta(hours=1))


def while_writer_waits(writer, action, event):
    """The action must finish before a held writer's watchdog releases it.

    The ordering assertion catches a blocking GUI write without relying on a
    narrow latency threshold or sleeping for an arbitrary interval in the test.
    A watchdog keeps a regression from deadlocking the test runner.
    """
    acquired, completed = Signal(), Signal()
    outcome, failures = {}, []

    def synchronize():
        try:
            with writer._transaction():
                writer._write_event(replace(event, title='Received during sync'))
                acquired.set()
                outcome['completed_before_release'] = completed.wait(1.5)
        except BaseException as error:
            failures.append(error)
            acquired.set()

    thread = Thread(target=synchronize, name='synthetic-sync-writer')
    thread.start()
    assert acquired.wait(5), 'The synthetic writer could not start.'
    try:
        if failures:
            raise failures[0]
        begin = perf_counter()
        result = action()
        elapsed = (perf_counter() - begin) * 1000
    finally:
        completed.set()
        thread.join(6)
    assert not thread.is_alive(), 'The synthetic sync transaction did not finish.'
    if failures:
        raise failures[0]
    assert outcome['completed_before_release'], (
        f'Visibility waited for the event writer ({elapsed:.1f} ms); '
        'view-state persistence must not share that write lock.')
    return result


def test_visibility_read_and_write_complete_while_sync_owns_event_database(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    original = synthetic()
    with Store(path) as reader, Store(path, query_index=False) as writer:
        reader.apply_remote([original])
        def hide_and_query():
            reader.set_calendar_visible('local', False)
            assert not reader.get_calendar('local').visible
            assert not reader.list_calendars()[0].visible
            # Reads use the previous committed event snapshot during syncing.
            found = reader.query_occurrences(START, START + timedelta(days=1))
            assert len(found) == 1 and found[0].title == original.title
        while_writer_waits(writer, hide_and_query, original)
        assert reader.get_occurrence(original.id).title == 'Received during sync'
        assert not reader.get_calendar('local').visible


@pytest.mark.parametrize('view', ['week', 'month'])
def test_jump_then_hide_and_paint_finish_before_sync_writer_is_released(tmp_path, view):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'calendar.sqlite3'
    event = synthetic('work-event', 'work')
    errors = []
    with Store(path) as store, Store(path, query_index=False) as writer, \
            patch('debacal.ui.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        store.save_calendar(Calendar('work', 'Work', '#527365'))
        store.apply_remote([synthetic(), event])
        window = MainWindow(store, enable_sync=False)
        window.resize(1400, 900)
        window.show()
        try:
            app.processEvents()
            window.view = view
            window.anchor = window.selected_day = START.date()
            window.refresh()
            app.processEvents()
            assert any(item.calendar_id == 'work' for item in window.visible_events)
            def hide_and_paint():
                window.toggle_calendar('work', False)
                app.processEvents()
                assert not store.get_calendar('work').visible
                assert all(item.calendar_id != 'work' for item in window.visible_events)
                assert not errors
            while_writer_waits(writer, hide_and_paint, event)
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        assert not errors


def test_visibility_is_durable_cross_connection_and_isolated_by_database_path(tmp_path):
    first_path, second_path = tmp_path / 'first.sqlite3', tmp_path / 'second.sqlite3'
    with Store(first_path) as first, Store(first_path, query_index=False) as peer, Store(second_path) as other:
        first.set_calendar_visible('local', False)
        assert not peer.get_calendar('local').visible
        assert other.get_calendar('local').visible
    with Store(first_path) as reopened, Store(second_path) as other:
        assert not reopened.get_calendar('local').visible
        assert other.get_calendar('local').visible


def test_visibility_does_not_modify_canonical_calendars_events_history_or_outbox(tmp_path):
    with Store(tmp_path / 'calendar.sqlite3') as store:
        store.apply('Create', [synthetic()])
        canonical = [tuple(row) for row in store.connection.execute('SELECT * FROM calendars')]
        events = [tuple(row) for row in store.connection.execute('SELECT * FROM events')]
        history, pending = store.history(), store.pending_changes()
        revision = store.events_revision()
        source_changes = store.connection.total_changes
        store.set_calendar_visible('local', False)
        assert store.connection.total_changes == source_changes
        assert [tuple(row) for row in store.connection.execute('SELECT * FROM calendars')] == canonical
        assert [tuple(row) for row in store.connection.execute('SELECT * FROM events')] == events
        assert store.history() == history and store.pending_changes() == pending
        assert store.events_revision() == revision


def test_discovery_metadata_from_a_stale_calendar_cannot_overwrite_visibility(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as reader, Store(path, query_index=False) as writer:
        stale = writer.get_calendar('local')
        reader.set_calendar_visible('local', False)
        writer.save_calendar(replace(stale, name='Updated source name', color='#7d6589'))
        updated = reader.get_calendar('local')
        assert updated.name == 'Updated source name' and updated.color == '#7d6589'
        assert not updated.visible


def test_failed_visibility_change_cannot_leave_an_override_for_a_future_calendar(tmp_path):
    with Store(tmp_path / 'calendar.sqlite3') as store:
        with pytest.raises(ValueError, match='not found'):
            store.set_calendar_visible('missing', False)
        store.save_calendar(Calendar('missing', 'New source', '#527365'))
        assert store.get_calendar('missing').visible
