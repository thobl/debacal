"""Toolbar history availability follows the actual default undo/redo targets."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow

START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)

def entry(identity='entry'):
    return Event(identity, 'local', 'Synthetic history', START, START + timedelta(hours=1))


def test_availability_tracks_default_targets_and_independent_redo():
    with Store(':memory:') as store:
        assert store.history_availability() == (False, False)
        store.apply('Create', [entry()])
        assert store.history_availability() == (True, False)
        store.undo()
        assert store.history_availability() == (False, True)
        store.redo()
        assert store.history_availability() == (True, False)
        store.apply('Rename', [replace(entry(), title='Renamed')])
        store.undo()
        assert store.history_availability() == (True, True)
        store.apply('Independent event', [entry('independent')])
        assert store.history_availability() == (True, True)
        store.redo()
        assert store.get_event('entry').title == 'Renamed'


def test_availability_respects_remote_changes_and_source_permissions():
    with Store(':memory:') as store:
        store.apply('Create', [entry()])
        source = store.get_calendar('local')
        assert store.history_availability() == (True, False)
        store.save_calendar(replace(source, read_only=True))
        assert store.history_availability() == (False, False)
        store.save_calendar(source)
        assert store.history_availability() == (True, False)
        store.apply_remote([replace(entry(), title='Changed elsewhere')])
        assert store.history_availability() == (False, False)


def test_availability_cache_ignores_visibility_and_settings_without_reading_all_history():
    with Store(':memory:') as store:
        store.apply('Create', [entry()])
        assert store.history_availability() == (True, False)
        with patch.object(store, 'history', side_effect=AssertionError('No full history scan')), \
             patch.object(store, '_matches', wraps=store._matches) as checks:
            store.set_setting('unrelated', 'setting')
            store.set_calendar_visible('local', False)
            for _ in range(3):
                assert store.history_availability() == (True, False)
            assert checks.call_count == 0
            store.apply('Create next', [entry('next')])
            assert store.history_availability() == (True, False)
            assert checks.call_count == 1


def test_undo_and_redo_buttons_update_without_changing_their_shortcuts():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        try:
            window.show()
            window.activateWindow()
            app.processEvents()
            assert not window.undo_button.isEnabled() and not window.redo_button.isEnabled()
            window.apply_events('Create', [entry()])
            assert window.undo_button.isEnabled() and not window.redo_button.isEnabled()
            QTest.mouseClick(window.undo_button, Qt.MouseButton.LeftButton)
            app.processEvents()
            assert not window.undo_button.isEnabled() and window.redo_button.isEnabled()
            assert not app.focusWidget().property('keyboardFocus')
            window.redo_button.click()
            assert window.undo_button.isEnabled() and not window.redo_button.isEnabled()
            assert '(Ctrl+Z)' in window.undo_button.toolTip()
            assert '(Ctrl+Y)' in window.redo_button.toolTip()
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
