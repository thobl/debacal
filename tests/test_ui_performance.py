"""UI work is reused across navigation and invalidated by actual event writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        store.save_calendar(Calendar('work', 'Work', '#123abc'))
        events = [Event(f'event-{index}', 'local' if index % 2 else 'work', f'Meeting {index}',
                        START + timedelta(days=index % 20, minutes=index % 4 * 60),
                        START + timedelta(days=index % 20, minutes=index % 4 * 60 + 30))
                  for index in range(200)]
        store.apply_remote(events)
        window = MainWindow(store, enable_sync=False)
        window.show()
        window.activateWindow()
        window.canvas.setFocus()
        app.processEvents()
        try:
            yield app, window, store
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert errors == []


def test_navigation_and_calendar_visibility_reuse_event_search_index(desktop):
    _, window, store = desktop
    with patch.object(store, 'list_events', wraps=store.list_events) as reads:
        window.search.setText('Meeting')
        index = window.results._index
        initial_reads = reads.call_count
        window.set_view('month')
        window.set_view('week')
        window.toggle_calendar('work', False)
        window.toggle_calendar('work', True)
        assert window.results._index is index
        assert reads.call_count == initial_reads
        original = store.get_event('event-1')
        store.apply('Rename', [replace(original, title='Changed title')])
        window.refresh()
        assert window.results._index is not index
        window.search.setText('Changed title')
        assert window.results.count() == 1


def test_view_and_visibility_caches_are_invalidated_by_event_changes(desktop):
    _, window, store = desktop
    with patch.object(store, 'query_occurrences', wraps=store.query_occurrences) as queries:
        window.set_view('month')
        window.set_view('week')
        window.toggle_calendar('work', False)
        window.toggle_calendar('work', True)
        assert queries.call_count == 2  # new month and hidden-calendar page
        window.set_view('month')
        assert queries.call_count == 2
        original = store.get_event('event-1')
        store.apply('Rename', [replace(original, title='Updated immediately')])
        window.refresh()
        assert queries.call_count == 3
        assert any(event.title == 'Updated immediately' for event in window.visible_events)


def test_remote_connection_changes_invalidate_cached_pages(tmp_path):
    app = QApplication.instance() or QApplication([])
    with Store(tmp_path / 'calendar.sqlite3') as store:
        window = MainWindow(store, enable_sync=False)
        try:
            window.go_date(START.date())
            window.set_view('month')
            with Store(store.path) as remote:
                event = Event('remote', 'local', 'Arrived from sync', START, START + timedelta(hours=1))
                remote.apply_remote([event])
            window.refresh()
            assert [event.title for event in window.visible_events] == ['Arrived from sync']
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_typing_burst_does_one_search_and_arrow_flushes_without_waiting(desktop):
    _, window, _ = desktop
    window.focus_search()
    with patch.object(window.results, 'populate', wraps=window.results.populate) as searches:
        QTest.keyClicks(window.search, 'Meeting 12')
        assert window.search_timer.isActive()
        assert searches.call_count == 0
        assert not window.results.isEnabled()
        QTest.keyClick(window.search, Qt.Key.Key_Down)
        assert searches.call_count == 1
        assert not window.search_timer.isActive()
        assert window.results.isEnabled() and window.results.hasFocus()
        assert 'Meeting 12' in window.event_by_id(window.selected_id).title


def test_typing_timer_commits_once_and_clear_restores_agenda_immediately(desktop):
    _, window, _ = desktop
    window.focus_search()
    with patch.object(window.results, 'populate', wraps=window.results.populate) as searches:
        QTest.keyClicks(window.search, 'Meeting 12')
        QTest.qWait(120)
        assert searches.call_count == 1
        assert window.results.isEnabled() and not window.results.agenda_mode
        window.search.setText('')
        assert not window.search_timer.isActive()
        assert window.results.agenda_mode and window.results.isEnabled()


def test_status_counts_do_not_decode_outbox_payloads(desktop):
    _, window, store = desktop
    store.save_calendar(Calendar('remote', 'Remote', '#123abc', kind='caldav', url='https://calendar.invalid/'))
    store.apply('Pending local change', [Event('pending', 'remote', 'Edited', START, START + timedelta(hours=1))])
    with patch.object(store, 'pending_changes', side_effect=AssertionError('Footer must not read sync payloads')):
        window.update_sync_status()
        assert '1 change(s) waiting' in window.status.text()


def test_future_highlights_resolve_each_series_once(desktop):
    _, _, store = desktop
    from debacal.selection import affected_visible
    master = Event('series', 'local', 'Daily', START, START + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=200')
    store.apply_remote([master])
    events = [event for event in store.query_occurrences(START, START + timedelta(days=201)) if event.series_id == master.id]
    with patch.object(store, 'get_event', wraps=store.get_event) as reads:
        result = affected_visible(store, [events[50]], 'future', events)
        assert len(result) == 150
        assert reads.call_count <= 2
