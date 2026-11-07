"""On-demand search has synchronous focus and a stable, scalable header slot."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.models import Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


NOW = datetime(2026, 11, 6, 13, 15, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.fixture
def workspace_factory():
    app = QApplication.instance() or QApplication([])
    workspaces, errors = [], []
    with patch('debacal.ui.now', return_value=NOW), \
         patch('debacal.search_panel.now', return_value=NOW), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(scale=100):
            store = Store(':memory:')
            Preferences(font_scale=scale).save(store)
            store.apply('Create', [Event('entry', 'local', 'Synthetic needle', NOW + timedelta(hours=1),
                                         NOW + timedelta(hours=2))])
            window = MainWindow(store, enable_sync=False)
            window.resize(1030, 690)
            window.go_date(NOW.date())
            window.show()
            window.activateWindow()
            window.active_canvas().setFocus()
            settle(app)
            workspaces.append((window, store))
            return app, window, store
        yield make
        for window, store in workspaces:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            store.close()
        assert not errors


def calendar_geometry(window):
    return window.stack.geometry(), window.scroll.viewport().size(), window.canvas.hour_height


def test_single_click_focuses_synchronously_and_keeps_the_first_character(workspace_factory):
    app, window, _ = workspace_factory()
    assert window.search_header.currentWidget() is window.search_header.resting
    QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
    # No queued focus/event-loop turn is needed before the user starts typing.
    assert QApplication.focusWidget() is window.search
    QTest.keyClicks(QApplication.focusWidget(), 'n')
    assert window.search.text() == 'n'
    settle(app)
    assert window.search_header.currentWidget() is window.search_header.active


def test_empty_query_can_tab_through_filter_then_collapse_on_blur(workspace_factory):
    app, window, _ = workspace_factory()
    QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
    QTest.keyClick(window.search, Qt.Key.Key_Tab)
    settle(app)
    assert window.filter_search.hasFocus()
    assert window.search_header.currentWidget() is window.search_header.active
    window.active_canvas().setFocus()
    settle(app)
    assert window.search_header.currentWidget() is window.search_header.resting
    assert not window.search.isVisible()


def test_query_stays_visible_with_result_focus_and_escape_restores_agenda(workspace_factory):
    app, window, _ = workspace_factory()
    baseline = calendar_geometry(window)
    QTest.keyClick(window.active_canvas(), Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert window.search.hasFocus() and window.search.isVisible()
    QTest.keyClicks(window.search, 'needle')
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    settle(app)
    assert window.results.hasFocus()
    assert window.search.text() == 'needle'
    assert window.search_header.currentWidget() is window.search_header.active
    assert calendar_geometry(window) == baseline
    QTest.keyClick(window.results, Qt.Key.Key_Escape)
    settle(app)
    assert window.search.text() == '' and window.results.agenda_mode
    assert window.search_header.currentWidget() is window.search_header.resting
    assert window.active_canvas().hasFocus()
    assert calendar_geometry(window) == baseline


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_header_children_fit_and_search_does_not_move_the_calendar(workspace_factory, scale):
    app, window, _ = workspace_factory(scale)
    window.sidebar_scroll.ensureWidgetVisible(window.search_header)
    settle(app)
    header = window.search_header
    before = header.height(), calendar_geometry(window)
    assert header.resting.rect().contains(window.search_button.geometry())
    window.focus_search()
    settle(app)
    assert header.active.rect().contains(window.search.geometry())
    assert header.active.rect().contains(window.filter_search.geometry())
    window.search.setText('needle')
    window.flush_search()
    settle(app)
    assert window.width() == 1030
    assert (header.height(), calendar_geometry(window)) == before
    window.exit_search()
    settle(app)
    assert (header.height(), calendar_geometry(window)) == before


def test_preferences_apply_updates_search_slot_and_agenda_style_without_losing_query(workspace_factory):
    app, window, store = workspace_factory()
    window.focus_search()
    window.search.setText('needle')
    settle(app)

    def accept_preferences(dialog):
        dialog.font_scale.setValue(140)
        dialog.agenda_style.setCurrentIndex(dialog.agenda_style.findData('grouped'))
        dialog.save()
        return QDialog.DialogCode.Accepted

    with patch.object(PreferencesDialog, 'exec', new=accept_preferences):
        window.show_preferences('Appearance')
    settle(app)
    assert window.preferences.font_scale == 140 and window.preferences.agenda_style == 'grouped'
    assert Preferences.load(store) == window.preferences
    assert window.search.text() == 'needle' and not window.results.agenda_mode
    assert window.search_header.currentWidget() is window.search_header.active
    window.focus_search()
    settle(app)
    assert window.search_header.active.rect().contains(window.filter_search.geometry())
    window.exit_search()
    settle(app)
    assert window.results.agenda_mode and not window.results.uniformRowHeights()
