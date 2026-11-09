"""Applying presentation settings preserves unchanged render geometry."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


NOW = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=NOW):
        events = []
        for index in range(5000):
            start = NOW.replace(month=1, day=10) + timedelta(days=index % 300, minutes=index // 300)
            events.append(Event(str(index), 'local', f'Appointment {index}', start, start + timedelta(hours=1)))
        store.apply('Create synthetic year', events)
        window = MainWindow(store, enable_sync=False)
        window.go_date(NOW.date())
        window.set_view('year')
        window.show()
        app.processEvents()
        try:
            yield app, store, window
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_year_style_and_sync_changes_do_not_restyle_or_reindex_the_year(workspace):
    app, store, window = workspace
    with (patch.object(window.year, '_event_interval', side_effect=AssertionError('Do not scan events')),
         patch.object(store, 'query_occurrences', wraps=store.query_occurrences) as queries,
          patch.object(window, 'setStyleSheet', side_effect=AssertionError('Keep unchanged stylesheet'))):
        window.apply_preferences(replace(window.preferences, year_view_style='tints'), window.bindings)
        app.processEvents()
        window.apply_preferences(replace(window.preferences, sync_interval_minutes=19), window.bindings)
        app.processEvents()
        assert queries.call_count == 2
    assert window.year.preferences.year_view_style == 'tints'
    assert Preferences.load(store).sync_interval_minutes == 19


def test_theme_and_font_changes_repaint_without_rebuilding_calendar_intervals(workspace):
    app, _, window = workspace
    with patch.object(window.year, '_event_interval', side_effect=AssertionError('Reuse event intervals')):
        window.apply_preferences(replace(window.preferences, color_scheme='warm', font_scale=120), window.bindings)
        app.processEvents()
    assert window.year.preferences.font_scale == 120
    assert window.year.preferences.color_scheme == 'warm'


def test_reused_year_index_is_invalidated_by_event_and_calendar_changes(workspace):
    app, store, window = workspace
    changed = replace(store.get_event('0'), title='Changed after appearance apply')
    store.apply('Edit', [changed])
    window.refresh()
    assert window.year._event_map[changed.id].title == changed.title
    store.set_calendar_visible('local', False)
    window.refresh()
    assert not window.year.events
    store.set_calendar_visible('local', True)
    window.refresh()
    assert len(window.year.events) == 5000


def test_appearance_apply_keeps_unchanged_source_tree_and_native_style(workspace):
    app, store, window = workspace
    dialog = PreferencesDialog(window.preferences, window, store=store,
                               bindings=window.bindings, allow_remote=False)
    try:
        dialog.applied.connect(window.apply_preferences)
        dialog.year_view_style.setCurrentIndex(dialog.year_view_style.findData('tints'))
        with (patch.object(dialog, 'setStyleSheet', side_effect=AssertionError('Style is unchanged')),
              patch.object(dialog.source_editor, '_rebuild', side_effect=AssertionError('Sources are unchanged'))):
            assert dialog.apply()
            app.processEvents()
        assert window.preferences.year_view_style == 'tints'
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_new_sync_default_preserves_an_explicit_saved_interval():
    assert Preferences().sync_interval_minutes == 5
    assert Preferences.from_dict({}).sync_interval_minutes == 5
    with Store(':memory:') as store:
        Preferences(sync_interval_minutes=1).save(store)
        assert Preferences.load(store).sync_interval_minutes == 1
