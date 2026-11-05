"""Conflict previews stay aligned and only explicit choices reach the engine."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from queue import Queue
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from lebacal.conflict_dialog import ConflictDialog
from lebacal.models import Calendar, Event
from lebacal.preferences import Preferences
from lebacal.preferences_dialog import PreferencesDialog
from lebacal.store import Store
from lebacal.sync import SyncConflict
from lebacal.ui import MainWindow


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


def make_conflict():
    start = datetime(2026, 11, 30, 10, tzinfo=ZoneInfo('Europe/Berlin'))
    local = Event('example', 'local', 'Local title', start, start + timedelta(hours=1),
                  recurrence_rule='FREQ=WEEKLY;COUNT=4')
    remote = replace(local, title='Server title', start=start + timedelta(days=1),
                     end=local.end + timedelta(days=1), location='Server room')
    return SyncConflict('conflict', 'local', local.id, local, remote, 'Changed on both sides')


def test_calendar_comparison_aligns_months_and_expands_each_version(app):
    conflict = make_conflict()
    dialog = ConflictDialog(conflict, Preferences(0, 'iso', '24h'))
    try:
        dialog.show()
        app.processEvents()
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2026, 11, 1)
        assert dialog.local_preview.events_on(date(2026, 11, 30))
        assert dialog.remote_preview.events_on(date(2026, 12, 1))
        row = dialog.fields.index('Title')
        assert dialog.comparison.item(row, 1).text() == 'Local title'
        assert dialog.comparison.item(row, 2).text() == 'Server title'
        assert dialog.comparison.item(row, 1).background().color().name() == '#fff1c9'
        QTest.mouseClick(dialog.remote_date_button, Qt.MouseButton.LeftButton)
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2026, 12, 1)
        QTest.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2027, 1, 1)
        assert not dialog.grab().isNull()
        assert dialog.choice is None
        assert conflict == make_conflict()  # Viewing and navigating never mutate snapshots.
    finally:
        dialog.close()
        dialog.deleteLater()


def test_deleted_version_and_postpone_do_not_choose_automatically(app):
    conflict = replace(make_conflict(), remote=None)
    dialog = ConflictDialog(conflict, Preferences())
    try:
        assert not dialog.remote_date_button.isEnabled()
        assert dialog.remote_preview.masters == []
        assert dialog.remote_button.text() == 'Keep server deletion'
        dialog.later_button.click()
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert dialog.choice is None
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize('choice', ['local', 'remote', None])
def test_window_submits_exact_displayed_snapshot_only_on_choice(app, choice):
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        conflict = make_conflict()
        engine = Mock(conflicts=[conflict])
        window.engine = engine
        def choose(dialog):
            if choice:
                dialog.choose(choice)
            else:
                dialog.reject()
            return dialog.result()
        try:
            with patch.object(ConflictDialog, 'exec', choose):
                window.show_conflicts()
            if choice:
                engine.resolve_conflict.assert_called_once_with(conflict.id, choice, expected=conflict)
            else:
                engine.resolve_conflict.assert_not_called()
        finally:
            window.close()
            window.deleteLater()


def test_automatic_sync_can_be_paused_and_manual_sync_still_works(app):
    with Store(':memory:') as store:
        store.save_calendar(Calendar('remote', 'Remote', '#123456', kind='caldav', url='https://example.test/cal/'))
        window = MainWindow(store, enable_sync=False)
        engine = Mock(conflicts=[], events=Queue())
        window.engine = engine
        try:
            window.configure_auto_sync()
            assert window.sync_timer.isActive()
            assert window.sync_timer.interval() == 60_000
            window.startup_sync()
            engine.sync_now.assert_called_once()
            engine.reset_mock()
            window.preferences = replace(window.preferences, auto_sync_enabled=False, sync_interval_minutes=7)
            window.configure_auto_sync()
            assert not window.sync_timer.isActive()
            window.startup_sync()
            engine.sync_now.assert_not_called()
            window.sync_now()
            engine.sync_now.assert_called_once()
            window.preferences = replace(window.preferences, auto_sync_enabled=True)
            window.configure_auto_sync()
            assert window.sync_timer.interval() == 420_000
            window.close()
            window.startup_sync()
            assert not window.sync_timer.isActive()
            assert engine.sync_now.call_count == 1
        finally:
            window.close()
            window.deleteLater()


def test_automatic_sync_preferences_persist_and_validate(app, tmp_path):
    preferences = Preferences(auto_sync_enabled=False, sync_interval_minutes=15)
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as store:
        preferences.save(store)
    with Store(path) as store:
        assert Preferences.load(store) == preferences
    dialog = PreferencesDialog(preferences)
    try:
        assert not dialog.sync_interval.isEnabled()
        dialog.auto_sync.setChecked(True)
        assert dialog.sync_interval.isEnabled()
        dialog.sync_interval.setValue(5)
        dialog.save()
        assert dialog.result_preferences.auto_sync_enabled
        assert dialog.result_preferences.sync_interval_minutes == 5
        dialog.restore_defaults()
        assert dialog.value() == Preferences()
    finally:
        dialog.close()
        dialog.deleteLater()
    for invalid in (0, -1, 1441, True, '5'):
        with pytest.raises(ValueError):
            Preferences(sync_interval_minutes=invalid)
    assert Preferences.from_dict({'auto_sync_enabled': 'yes', 'sync_interval_minutes': 0}) == Preferences()
