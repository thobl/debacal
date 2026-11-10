"""Desktop font faces must not prevent selected text from rendering bold."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from contextlib import contextmanager

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.__main__ import configure_application
from debacal.fonts import resolved_font_family
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


def font_family(*candidates):
    available = set(QFontDatabase.families())
    for family in candidates:
        if family in available:
            styles = set(QFontDatabase.styles(family))
            regular = next((style for style in ('Book', 'Regular') if style in styles), None)
            if regular is not None and 'Bold' in styles:
                return family, regular
    pytest.skip('A font with separate regular and bold faces is required.')


def settle(app):
    for _ in range(3):
        app.processEvents()


@pytest.fixture
def desktop(monkeypatch):
    app = QApplication.instance() or QApplication([])
    previous_font, previous_palette = app.font(), app.palette()
    previous_style = app.style().objectName()
    previous_names = (app.applicationName(), app.organizationName(),
                      app.applicationDisplayName(), app.desktopFileName())
    family, regular = font_family('DejaVu Sans', 'Liberation Sans', 'Noto Sans')
    desktop_font = QFont(family, 10)
    desktop_font.setStyleName(regular)
    app.setFont(desktop_font)
    # Model desktop integrations which expose an explicitly named Regular/Book
    # face. Qt's offscreen fallback otherwise lacks that face restriction.
    monkeypatch.setattr(QFontDatabase, 'systemFont', lambda _role: QFont(desktop_font))
    configure_application(app)
    assert app.font().family() == desktop_font.family()
    assert app.font().pointSizeF() == desktop_font.pointSizeF()
    try:
        yield app, family
    finally:
        app.setStyle(previous_style)
        app.setFont(previous_font)
        app.setPalette(previous_palette)
        app.setApplicationName(previous_names[0])
        app.setOrganizationName(previous_names[1])
        app.setApplicationDisplayName(previous_names[2])
        app.setDesktopFileName(previous_names[3])
        settle(app)


@contextmanager
def workspace(app, preferences):
    with Store(':memory:') as store:
        preferences.save(store)
        store.save_calendar(Calendar('work', 'Work calendar', '#587c67'))
        window = MainWindow(store, enable_sync=False)
        window.resize(1400, 900)
        window.show()
        window.activateWindow()
        settle(app)
        try:
            yield window
        finally:
            window.close()
            window.executor.shutdown(wait=True, cancel_futures=True)
            window.database_commands.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            settle(app)


def rendered_at_weight(app, widget, weight):
    previous = widget.styleSheet()
    widget.setStyleSheet(f'font-weight: {weight};')
    settle(app)
    image = widget.grab().toImage()
    widget.setStyleSheet(previous)
    settle(app)
    return image


@pytest.mark.parametrize('custom', [False, True], ids=['desktop-font', 'custom-font'])
def test_selected_calendar_stays_visibly_bold_after_focus_and_refresh(desktop, custom):
    app, system_family = desktop
    family = font_family('DejaVu Serif', 'Liberation Serif', 'Noto Serif')[0] if custom else system_family
    preferences = Preferences(font_family=family if custom else 'system')
    with workspace(app, preferences) as window:
        row = window.calendar_rows['work']
        QTest.mouseClick(row.choice, Qt.MouseButton.LeftButton)
        QTest.mouseMove(window.active_canvas())
        window.active_canvas().setFocus()
        settle(app)
        selected = row.choice.grab().toImage()
        normal = rendered_at_weight(app, row.choice, 400)
        assert selected.size() == normal.size()
        # QFont.bold() alone is insufficient: a named Regular/Book face can
        # remain regular even when Qt reports a requested bold weight.
        assert selected != normal
        assert row.choice.font().underline()
        assert QFontInfo(row.choice.font()).family() == family
        assert resolved_font_family(window.preferences) == family
        for _ in range(2):
            window.refresh()
            row.choice.setFocus(Qt.FocusReason.TabFocusReason)
            window.active_canvas().setFocus()
            settle(app)
            assert row.choice.isChecked()
            assert row.choice.grab().toImage() == selected
        window.set_creation_calendar('local')
        settle(app)
        assert not row.choice.isChecked() and not row.choice.font().underline()
        unselected = row.choice.grab().toImage()
        assert unselected == rendered_at_weight(app, row.choice, 400)


@pytest.mark.parametrize('custom', [False, True], ids=['desktop-font', 'custom-font'])
def test_heading_weight_is_visible_with_a_named_desktop_face(desktop, custom):
    app, system_family = desktop
    family = font_family('DejaVu Serif', 'Liberation Serif', 'Noto Serif')[0] if custom else system_family
    preferences = Preferences(font_family=family if custom else 'system')
    with workspace(app, preferences) as window:
        heading = window.heading
        emphasized = heading.grab().toImage()
        normal = rendered_at_weight(app, heading, 400)
        assert emphasized.size() == normal.size()
        assert emphasized != normal
        assert heading.grab().toImage() == emphasized
        assert QFontInfo(heading.font()).family() == family
