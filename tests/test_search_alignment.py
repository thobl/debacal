"""Search cards align with source controls without losing series hierarchy."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.event_style import event_colors
from debacal.models import Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def workspace_factory():
    app = QApplication.instance() or QApplication([])
    workspaces, errors = [], []
    with patch('debacal.ui.now', return_value=START), \
         patch('debacal.search_panel.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(scale=100):
            store = Store(':memory:')
            Preferences(font_scale=scale, locale_name='en_GB').save(store)
            store.apply('Create search examples', [
                Event('leaf', 'local', 'Needle one-off', START, START + timedelta(hours=1)),
                Event('series', 'local', 'Needle series', START + timedelta(hours=1),
                      START + timedelta(hours=2), recurrence_rule='FREQ=DAILY;COUNT=3'),
            ])
            window = MainWindow(store, enable_sync=False)
            window.resize(1440, 960)
            window.go_date(START.date())
            window.show()
            window.activateWindow()
            window.focus_search()
            window.search.setText('needle')
            window.flush_search()
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


def point_in(rect):
    center = rect.center()
    return QPoint(round(center.x()), round(center.y()))


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_top_level_search_frames_share_the_source_and_search_field_edge(workspace_factory, scale):
    app, window, store = workspace_factory(scale)
    panel = window.results
    source_left = window.calendar_rows['local'].swatch.mapTo(window, QPoint()).x()
    field_left = window.search.mapTo(window, QPoint()).x()
    viewport_left = panel.viewport().mapTo(window, QPoint()).x()
    assert source_left == field_left == viewport_left
    assert not panel.rootIsDecorated()
    panel.clearSelection()
    panel.clearFocus()
    settle(app)
    pixels = panel.viewport().grab().toImage()
    fill = event_colors(store.get_calendar('local').color).fill
    roots = [panel.row_item(identity) for identity in ('leaf', 'series')]
    for root in roots:
        rect = panel.visualItemRect(root)
        assert rect.left() == 0
        # Verify the frame really paints into that outer area rather than
        # merely reporting a full-width row while retaining a blank tree lane.
        assert pixels.pixelColor(round(5 * scale / 100), rect.bottom() - round(9 * scale / 100)).name() == fill
    assert panel.disclosure_rect(roots[0]) is None
    assert panel.disclosure_rect(roots[1]) is not None
    roots[1].setExpanded(True)
    settle(app)
    child = roots[1].child(0)
    assert panel.visualItemRect(child).left() == panel.indentation()
    assert panel.disclosure_rect(child) is None
    window.exit_search()
    window.focus_search()
    window.search.setText('needle')
    window.flush_search()
    settle(app)
    assert panel.visualItemRect(panel.row_item('leaf')).left() == 0
    assert panel.visualItemRect(panel.row_item('series')).left() == 0


def test_disclosure_clicks_fold_without_changing_selected_event(workspace_factory):
    app, window, _ = workspace_factory()
    panel = window.results
    leaf, series = panel.row_item('leaf'), panel.row_item('series')
    panel.setCurrentItem(leaf)
    before = panel.selected_row_ids()
    selected, activated = [], []
    panel.result_selected.connect(lambda *args: selected.append(args))
    panel.activated.connect(activated.append)
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, pos=point_in(panel.disclosure_rect(series)))
    settle(app)
    assert series.isExpanded() and series.childCount() == 3
    assert panel.selected_row_ids() == before
    assert panel.hasFocus()
    assert not selected and not activated
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, pos=point_in(panel.disclosure_rect(series)))
    settle(app)
    assert not series.isExpanded() and panel.selected_row_ids() == before
    assert not selected and not activated


def test_disclosure_double_click_does_not_activate_or_select_series(workspace_factory):
    app, window, _ = workspace_factory()
    panel = window.results
    leaf, series = panel.row_item('leaf'), panel.row_item('series')
    panel.setCurrentItem(leaf)
    before = panel.selected_row_ids()
    activated = []
    panel.activated.connect(activated.append)
    # A real double click consists of a first click followed by the double-click
    # event. Only the first click should toggle the disclosure.
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, pos=point_in(panel.disclosure_rect(series)))
    QTest.mouseDClick(panel.viewport(), Qt.MouseButton.LeftButton, pos=point_in(panel.disclosure_rect(series)))
    settle(app)
    assert series.isExpanded()
    assert panel.selected_row_ids() == before and not activated


def test_native_keyboard_folding_keeps_series_and_occurrence_selection_modes(workspace_factory):
    app, window, _ = workspace_factory()
    panel = window.results
    series = panel.row_item('series')
    panel.setCurrentItem(series)
    panel.setFocus()
    assert panel.row_scope(series) == 'group'
    QTest.keyClick(panel, Qt.Key.Key_Right)
    settle(app)
    assert series.isExpanded() and series.childCount() == 3
    assert panel.currentItem() is series
    QTest.keyClick(panel, Qt.Key.Key_Down)
    settle(app)
    assert panel.currentItem() is series.child(0)
    assert panel.row_scope(panel.currentItem()) == 'single'
    assert panel.selected_row_ids() == {panel.event_id(series.child(0))}
    panel.setCurrentItem(series)
    QTest.keyClick(panel, Qt.Key.Key_Left)
    settle(app)
    assert not series.isExpanded()
    assert panel.row_scope(panel.currentItem()) == 'group'
