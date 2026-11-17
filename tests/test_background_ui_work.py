"""Background notifications must not rebuild an unchanged desktop or its issues."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QApplication

from debacal.models import Event
from debacal.store import Store
from debacal.sync import SyncMessage
from debacal.ui import MainWindow
from debacal.ui_history import HistoryAvailability


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        window.engine = SimpleNamespace(events=Queue(), conflicts=[], close=lambda: None)
        window.poll_timer.stop()
        try:
            yield app, window, store
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_idle_and_unchanged_sync_leave_view_and_issue_details_alone(workspace):
    _app, window, _store = workspace
    with patch.object(window, 'refresh') as refresh, \
         patch.object(window, 'status_issues', side_effect=AssertionError('Eager diagnostic rendering')), \
         patch.object(window, 'update_sync_status', wraps=window.update_sync_status) as status:
        for _ in range(5):
            window.poll()
        assert status.call_count == 0
        window.engine.events.put(SyncMessage('updated', 'Nothing changed'))
        window.engine.events.put(SyncMessage('finished', 'Synchronization finished'))
        window.poll()
        assert not refresh.called and status.call_count == 1


@pytest.mark.parametrize('change', ['events', 'calendars'])
def test_changed_sync_refreshes_events_and_calendar_metadata(workspace, change):
    _app, window, store = workspace
    if change == 'events':
        store.apply_remote([Event('remote', 'local', 'Changed', START, START + timedelta(hours=1))])
    else:
        store.save_calendar(replace(store.get_calendar('local'), color='#445566'))
    window.engine.events.put(SyncMessage('updated', 'Source changed'))
    with patch.object(window, 'refresh', wraps=window.refresh) as refresh:
        window.poll()
        refresh.assert_called_once()


def test_warning_storm_is_processed_in_bounded_ordered_batches(workspace):
    _app, window, _store = workspace
    for index in range(350):
        window.engine.events.put(SyncMessage('issue', f'Bad resource {index}', 'source'))
    window.engine.events.put(SyncMessage('finished', 'Finished'))
    window._sync_running = True
    window.poll()
    assert window.engine.events.qsize() == 223
    assert window._sync_running and window._sync_issues['source'] == 'Bad resource 127'
    window.poll()
    window.poll()
    assert window.engine.events.empty() and not window._sync_running
    assert window._sync_issues['source'] == 'Bad resource 349'


def test_footer_counts_persisted_issues_without_loading_diagnostics(workspace):
    _app, window, store = workspace
    store.set_quarantined_resource('local', 'broken', 'BEGIN:VCALENDAR\nEND:VCALENDAR', 'Broken event')
    window._sync_issues['local'] = 'Local: resource broken: Broken event'
    expected = len(window.status_issues())
    with patch.object(store, 'quarantined_resource_summaries', side_effect=AssertionError('Decoded diagnostics')), \
         patch('debacal.ui.resource_details', side_effect=AssertionError('Formatted original resource')):
        window.update_sync_status()
        assert window.status_issue_counts() == (expected, 1)
        assert f'{expected} issue(s)' in window.warning_button.toolTip()


class ManualExecutor:
    def __init__(self):
        self.futures = []

    def submit(self, *_args):
        future = Future()
        self.futures.append(future)
        return future


def test_large_history_checks_are_asynchronous_and_discard_stale_answers(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr('debacal.ui_history.INLINE_HISTORY_BYTES', 0)
    executor = ManualExecutor()
    calls, errors = [], []
    with Store(tmp_path / 'calendar.sqlite3') as store:
        event = Event('large-import', 'local', 'Created', START, START + timedelta(hours=1))
        store.apply('Import', [event])
        owner = QObject()
        controller = HistoryAvailability(store, owner, lambda *value: calls.append(value),
                                         lambda *args, **kwargs: errors.append((args, kwargs)))
        controller._executor = executor
        controller._processor = SimpleNamespace(run=lambda *_args: None, shutdown=lambda: None)
        try:
            with patch.object(store, 'history_availability', side_effect=AssertionError('GUI decoded snapshots')):
                first_revision = store.events_revision()
                controller.update()
                first_signature = controller._signature
                assert calls[-1] == (False, False) and len(executor.futures) == 1
                store.apply_remote([replace(event, title='Changed remotely')])
                executor.futures[0].set_result({'available': [True, False], 'revision': first_revision,
                                               'signature': store._json(first_signature)})
                controller._poll()
                assert calls[-1] == (False, False) and len(executor.futures) == 2
                # A parent may already show pending permission edits that the
                # child's committed SQLite snapshot has not seen. Equal event
                # revisions alone must not make that older permission valid.
                wrong_signature = (*controller._signature[:3], ())
                executor.futures[1].set_result({'available': [True, False], 'revision': store.events_revision(),
                                               'signature': store._json(wrong_signature)})
                controller._poll()
                assert calls[-1] == (False, False) and len(executor.futures) == 3
                executor.futures[2].set_result({'available': [False, False], 'revision': store.events_revision(),
                                               'signature': store._json(controller._signature)})
                controller._poll()
                assert calls[-1] == (False, False) and not controller.timer.isActive()
                assert store.history_availability_probe()[2] == (False, False)
                assert not errors
        finally:
            controller._executor = None
            controller.shutdown()
            owner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_history_probe_does_not_read_snapshots_or_accept_changed_permissions():
    with Store(':memory:') as store:
        store.apply('Create', [Event('history', 'local', 'Created', START, START + timedelta(hours=1))])
        with patch.object(store, '_normalized_snapshot', side_effect=AssertionError('Decoded snapshots')):
            signature, size, cached = store.history_availability_probe()
            assert size > 0 and cached is None
            store.save_calendar(replace(store.get_calendar('local'), read_only=True))
            assert not store.remember_history_availability(signature, (True, False))
            assert store.history_availability_probe()[2] is None
