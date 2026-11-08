"""Preference navigation and staged account changes integrate with the workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future
from queue import Queue
from threading import get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTabWidget

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    store.save_calendar(Calendar('work', 'Work', '#446677', kind='caldav',
        url='https://cloud.example/remote.php/dav/calendars/alice/work/', username='alice',
        server_name='Work', server_color='#446677'))
    window = MainWindow(store, enable_sync=False)
    window.show()
    for _ in range(5):
        app.processEvents()
    yield app, store, window
    window.close()
    window.executor.shutdown(wait=True)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    store.close()


def test_sidebar_navigation_replaces_top_level_and_nested_tabs(workspace):
    app, _, window = workspace
    def inspect(dialog):
        dialog.show()
        app.processEvents()
        assert dialog.findChildren(QTabWidget) == []
        for index in range(dialog.categories.count()):
            dialog.categories.setCurrentRow(index)
            app.processEvents()
            assert dialog.page_heading.text() == dialog.categories.item(index).text()
            assert dialog.pages.currentIndex() == index
        dialog.reject()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', new=inspect):
        window.show_preferences()


def test_sources_cancel_keeps_alias_and_subscription_unchanged(workspace):
    _, store, window = workspace
    def inspect(dialog):
        editor = dialog.source_editor
        editor.sources.setCurrentItem(editor._items['work'])
        editor._name_changed('Draft alias')
        editor._items['work'].setCheckState(0, Qt.CheckState.Unchecked)
        dialog.reject()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', new=inspect):
        window.show_preferences('sources')
    assert store.get_calendar('work').enabled
    assert store.get_calendar('work').name == 'Work'


def test_sources_apply_updates_main_list_and_keeps_events_queued(workspace):
    app, store, window = workspace
    start = datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
    event = Event('meeting', 'work', 'Kept locally', start, start + timedelta(hours=1))
    store.apply('Create meeting', [event])
    before = store.pending_changes(), store.history()
    def inspect(dialog):
        editor = dialog.source_editor
        editor.sources.setCurrentItem(editor._items['work'])
        editor._name_changed('Team calendar')
        editor._items['work'].setCheckState(0, Qt.CheckState.Unchecked)
        assert dialog.apply()
        app.processEvents()
        assert 'work' not in window.calendar_rows
        assert not store.get_calendar('work').enabled
        assert store.get_calendar('work').name == 'Team calendar'
        assert (store.pending_changes(), store.history()) == before
        assert store.get_event('meeting') == event
        editor._name_changed('Unapplied later name')
        dialog.reject()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', new=inspect):
        window.show_preferences('sources')
    assert store.get_calendar('work').name == 'Team calendar'


def test_invalid_shortcuts_prevent_source_drafts_from_applying(workspace):
    _, store, window = workspace
    def inspect(dialog):
        editor = dialog.source_editor
        editor.sources.setCurrentItem(editor._items['work'])
        editor._name_changed('Uncommitted alias')
        dialog.shortcut_fields['scope_all'].setKeySequence('F1')
        assert not dialog.apply()
        assert dialog.page_heading.text() == 'Keyboard shortcuts'
        assert store.get_calendar('work').name == 'Work'
        dialog.reject()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', new=inspect):
        window.show_preferences('sources')


def test_account_password_apply_uses_a_worker_and_the_shared_credential_store(workspace):
    app, store, window = workspace
    calls = []
    class Vault:
        def save(self, calendar, password, *, remember):
            calls.append((calendar.id, password, remember, get_ident()))
            return True
    window.credentials = Vault()
    def inspect(dialog):
        editor = dialog.source_editor
        editor.add_calendars([store.get_calendar('work')], password='synthetic-password', remember=True)
        assert dialog.apply()
        for _ in range(100):
            app.processEvents()
            window.poll()
            if calls and window.tasks.empty():
                break
            QTest.qWait(5)
        assert calls == [('work', 'synthetic-password', True, calls[0][3])]
        assert calls[0][3] != get_ident()
        assert 'synthetic-password' not in str(store.get_calendar('work').to_dict())
        assert dialog.apply() and len(calls) == 1
        dialog.reject()
        return dialog.result()
    with patch.object(PreferencesDialog, 'exec', new=inspect):
        window.show_preferences('sources')


def test_hidden_title_moves_preferences_down_without_changing_its_horizontal_position(workspace):
    app, _, window = workspace
    before = window.preferences_button.mapTo(window, QPoint(0, 0)).x()
    window.apply_preferences(replace(window.preferences, show_sidebar_title=False), window.bindings)
    for _ in range(5):
        app.processEvents()
    assert window.preferences_button.parentWidget() is window.sidebar_footer
    assert window.preferences_button.mapTo(window, QPoint(0, 0)).x() == before
    assert abs(window.preferences_button.geometry().center().y() - window.sidebar_footer.rect().center().y()) <= 1


def test_sources_sync_once_after_all_account_credentials_finish_saving(workspace):
    _, _, window = workspace
    class Engine:
        def __init__(self):
            self.events = Queue()
            self.conflicts = []
            self.futures = []
            self.calls = 0
        def set_password(self, calendar_id, password, *, remember):
            future = Future()
            self.futures.append(future)
            return future
        def sync_now(self):
            self.calls += 1
        def close(self):
            pass
    engine = window.engine = Engine()
    window.save_source_credentials('work', 'synthetic-password', True)
    window.save_source_credentials('local', 'another-synthetic-password', True)
    window.preferences_sources_changed()
    assert engine.calls == 0
    engine.futures[0].set_result(True)
    window.poll()
    assert engine.calls == 0
    engine.futures[1].set_result(True)
    window.poll()
    assert engine.calls == 1
