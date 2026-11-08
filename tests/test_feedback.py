"""User-visible sync outcomes and explicit demo behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from queue import Queue
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from debacal.dialogs import CalendarDialog
from debacal.models import Calendar
from debacal.store import Store
from debacal.sync import SyncMessage
from debacal.ui import MainWindow
from debacal.dialogs import EventDialog
from datetime import datetime, timedelta, timezone


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    window = MainWindow(store, enable_sync=False)
    yield window, store
    window.close()
    app.processEvents()
    store.close()


class FakeEngine:
    def __init__(self):
        self.events = Queue()
        self.conflicts = []
        self.calls = 0
    def sync_now(self):
        self.calls += 1
    def close(self):
        pass


def test_demo_is_explicitly_offline_and_cannot_add_remote_sources():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False, demo_mode=True)
        try:
            assert not window.sync_btn.isEnabled()
            assert 'offline' in window.status.text()
            assert 'Demo mode' in window.sync_btn.toolTip()
            window.sync_now()
            assert 'Open my calendars' in window.local_issues[-1].details
            assert window.local_issues[-1].action_label == 'Open my calendars'
            dialog = CalendarDialog(allow_remote=False)
            assert dialog.kind.findData('caldav') == -1
            assert dialog.kind.findData('ics') == -1
            dialog.close()
        finally:
            window.close()
            app.processEvents()


def test_sync_click_and_failure_are_visible_and_survive_refresh(workspace):
    window, store = workspace
    store.save_calendar(Calendar('remote', 'Nextcloud', '#445566', kind='caldav', url='https://cloud.test/calendar/'))
    engine = FakeEngine()
    window.engine = engine
    window.sync_btn.setEnabled(True)
    window.sync_now()
    assert engine.calls == 1
    assert 'Syncing' in window.sync_btn.toolTip()
    assert not window.sync_btn.isEnabled()
    engine.events.put(SyncMessage('started', 'Starting synchronization'))
    engine.events.put(SyncMessage('error', 'Nextcloud: vdirsyncer is not installed', 'remote'))
    engine.events.put(SyncMessage('finished', 'Sync finished with an error'))
    window.poll()
    assert 'Sync failed' in window.status.text()
    assert any('not installed' in issue.details for issue in window.status_issues())
    assert not window.warning_button.isHidden()
    window.refresh()
    assert 'Sync failed' in window.status.text()
    assert 'not installed' in window.status.toolTip()
    assert window.sync_btn.text() == ''
    assert window.sync_btn.isEnabled()
    engine.events.put(SyncMessage('started', 'Starting synchronization'))
    engine.events.put(SyncMessage('updated', 'Nextcloud refreshed', 'remote'))
    engine.events.put(SyncMessage('finished', 'Sync finished successfully'))
    window.poll()
    assert 'Last synced' in window.status.text()
    assert not window._sync_errors
    assert not window.status_issues() and window.warning_button.isHidden()


def test_sync_status_includes_actionable_error_details(workspace):
    window, _ = workspace
    window._sync_messages.append('Nextcloud: authentication is required')
    captured = []
    def inspect(dialog):
        captured.append(dialog.sync_details.toPlainText())
        assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == 'Synchronization'
        return 0
    with patch('debacal.ui.StatusDialog.exec', inspect):
        window.show_sync_status()
    assert 'authentication is required' in captured[0]


def test_desktop_conflicts_fall_back_to_in_app_when_unavailable(workspace):
    window, _ = workspace
    window.preferences = replace(window.preferences, conflict_notifications='desktop')
    with patch('debacal.ui.QSystemTrayIcon.isSystemTrayAvailable', return_value=False):
        window.notify_conflict(SyncMessage('conflict', 'An event changed on both sides.'))
    assert 'changed on both sides' in window.local_issues[-1].details
    assert not window.warning_button.isHidden()


def test_weekly_schedule_uses_the_chosen_primary_weekday(workspace):
    _, store = workspace
    start = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)  # Wednesday
    dialog = EventDialog(store.list_calendars(), start)
    try:
        dialog.title.setText('Friday seminar')
        dialog.start.setDateTime(dialog.start.dateTime().addDays(2))
        dialog.schedule_mode.setCurrentData('repeat')
        dialog.repeat.setCurrentData('WEEKLY')
        event = dialog.build_events()[0]
        assert event.start.weekday() == 4
        assert 'FREQ=WEEKLY' in event.recurrence_rule
        store.apply('Create', [event])
        occurrences = store.query_occurrences(event.start, event.start + timedelta(days=30))
        assert all(occurrence.start.weekday() == 4 for occurrence in occurrences)
    finally:
        dialog.close()
