"""The sidebar shares a surface and keeps footer controls vertically centered."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDateEdit

from debacal.dialogs import _style_date_popup
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.theme import scheme_for
from debacal.ui import MainWindow


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        window.resize(1030, 690)
        window.show()
        settle(app)
        try:
            yield app, window
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def cell_background(calendar):
    pixels = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
    pixels.fill(Qt.GlobalColor.magenta)
    painter = QPainter(pixels)
    try:
        calendar.paintCell(painter, QRect(0, 0, 60, 40), QDate(2026, 11, 2))
    finally:
        painter.end()
    return pixels.pixelColor(0, 0)


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_sidebar_and_native_date_cells_share_surface_after_theme_changes(workspace, mode):
    app, window = workspace
    for name in ('neutral', 'warm', 'cool', 'custom'):
        preferences = Preferences(appearance_mode=mode, color_scheme=name, custom_surface='#946b83')
        window.apply_preferences(preferences, window.bindings)
        settle(app)
        colors = scheme_for(preferences)
        pixels = window.grab().toImage()
        for section in (window.sidebar, window.sidebar_overview, window.source_sections,
                        window.calendar_section, window.agenda_section, window.sidebar_footer):
            assert pixels.pixelColor(section.mapTo(window, QPoint(3, 3))) == QColor(colors.sidebar)
        calendar = window.mini.calendar
        assert cell_background(calendar) == QColor(colors.sidebar)
        for day in (Qt.DayOfWeek.Monday, Qt.DayOfWeek.Sunday):
            assert calendar.weekdayTextFormat(day).background().color() == QColor(colors.sidebar)
        # Sidebar styling must not recolor date popups used in event editors.
        field = QDateEdit(window)
        field.setCalendarPopup(True)
        _style_date_popup(field, preferences)
        assert cell_background(field.calendarWidget()) == QColor(colors.background)
        field.deleteLater()


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('scale', [80, 100, 140])
def test_footer_centers_controls_with_warnings_and_hidden_title(workspace, mode, scale):
    app, window = workspace
    preferences = Preferences(appearance_mode=mode, font_scale=scale)
    window.apply_preferences(preferences, window.bindings)
    settle(app)
    footer = window.sidebar_footer
    baseline = window.stack.geometry(), window.scroll.viewport().size(), footer.height()
    for title_visible in (True, False):
        window.apply_preferences(replace(preferences, show_sidebar_title=title_visible), window.bindings)
        for warning in (False, True):
            issue = window.report_error('Synthetic import error', title='Import failed') if warning else None
            settle(app)
            assert (window.stack.geometry(), window.scroll.viewport().size(), footer.height()) == baseline
            controls = [window.sync_btn, window.status]
            if warning:
                controls.append(window.warning_button)
            if not title_visible:
                controls.append(window.preferences_button)
            for control in controls:
                assert control.isVisible()
                assert abs(control.geometry().center().y() - footer.rect().center().y()) <= 1
            if issue:
                window.dismiss_error(issue)
