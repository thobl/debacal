"""Shared section styling and keyboard-only focus cues on native buttons."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from debacal.appearance import stylesheet
from debacal.button_focus import install_button_focus
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


def settle(app):
    for _ in range(5):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    install_button_focus(app)
    widgets = []
    yield app, widgets
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
def test_mouse_focused_action_has_no_added_frame_but_keyboard_focus_remains(desktop, scheme):
    app, widgets = desktop
    window = QWidget()
    widgets.append(window)
    window.setStyleSheet(stylesheet(100, scheme))
    layout = QHBoxLayout(window)
    action, other = QPushButton('A'), QPushButton('B')
    action.setProperty('iconOnly', True)
    action.setFixedSize(32, 32)
    layout.addWidget(action)
    layout.addWidget(other)
    window.show()
    window.activateWindow()
    other.setFocus(Qt.FocusReason.MouseFocusReason)
    QTest.mouseMove(other)
    settle(app)
    plain = action.grab().toImage()
    action.setFocus(Qt.FocusReason.MouseFocusReason)
    settle(app)
    assert action.hasFocus() and not action.property('keyboardFocus')
    assert action.grab().toImage() == plain
    other.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(other, Qt.Key.Key_Backtab)
    settle(app)
    assert action.hasFocus() and action.property('keyboardFocus')
    assert action.grab().toImage() != plain
    # Clicking a keyboard-focused button must remove its cue even though no
    # new FocusIn occurs when the clicked button already owns focus.
    QTest.mouseClick(action, Qt.MouseButton.LeftButton)
    QTest.mouseMove(other)
    settle(app)
    assert action.hasFocus() and not action.property('keyboardFocus')
    assert action.grab().toImage() == plain
    QTest.keyClick(action, Qt.Key.Key_Space)
    settle(app)
    assert action.property('keyboardFocus')


@pytest.mark.parametrize('style', ['underlined', 'filled'])
def test_main_buttons_and_shared_groups_omit_click_focus_frames(desktop, style):
    app, widgets = desktop
    with Store(':memory:') as store:
        Preferences(control_style=style).save(store)
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        window.activateWindow()
        settle(app)
        for action, surface in ((window.today_button, window.today_button),
                                (window.selection_buttons['single'], window.selection_control),
                                (window.mini.month_button, window.mini.month_button)):
            action.setFocus(Qt.FocusReason.MouseFocusReason)
            settle(app)
            mouse = surface.grab().toImage()
            window.active_canvas().setFocus()
            settle(app)
            assert mouse == surface.grab().toImage()
            action.setFocus(Qt.FocusReason.TabFocusReason)
            settle(app)
            assert action.property('keyboardFocus')
            assert mouse != surface.grab().toImage()
        window.close()
        window.executor.shutdown(wait=True)


@pytest.mark.parametrize('view', ['day', 'week', 'month', 'year'])
def test_clicking_workspace_header_keeps_native_window_lookup_and_does_not_raise(desktop, view):
    app, widgets = desktop
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        try:
            window.resize(1400, 900)
            window.show()
            window.set_view(view)
            settle(app)
            errors = []
            with patch('sys.excepthook', side_effect=lambda _type, error, _tb: errors.append(error)):
                window.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, True)
                QTest.mouseClick(window.workspace_header, Qt.MouseButton.LeftButton,
                                 pos=window.workspace_header.rect().bottomRight() - QPoint(4, 4))
                settle(app)
            assert not errors
            assert window.workspace_header.window() is window
            assert not window.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
        finally:
            window.close()
            window.executor.shutdown(wait=True)


def test_application_focus_filter_handles_widgets_with_a_shadowed_native_window_attribute(desktop):
    app, widgets = desktop
    window = QWidget()
    widgets.append(window)
    layout = QHBoxLayout(window)
    for widget in (QWidget(), QPushButton('Action')):
        # Model a custom widget supplied to the application-wide event filter;
        # its owner attribute must not break the native top-level lookup.
        widget.window = window
        layout.addWidget(widget)
    window.show()
    settle(app)
    errors = []
    with patch('sys.excepthook', side_effect=lambda _type, error, _tb: errors.append(error)):
        for index in range(layout.count()):
            window.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, True)
            QTest.mouseClick(layout.itemAt(index).widget(), Qt.MouseButton.LeftButton)
            settle(app)
            assert not window.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
    assert not errors


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
def test_calendar_and_agenda_section_headings_share_one_style(desktop, scale, scheme):
    app, widgets = desktop
    with Store(':memory:') as store:
        Preferences(font_scale=scale, color_scheme=scheme).save(store)
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        settle(app)
        calendars = window.calendar_section.findChild(QLabel, 'sidebarSectionHeading')
        agenda = window.results_heading
        assert calendars is not None and agenda.objectName() == calendars.objectName()
        assert calendars.font() == agenda.font()
        assert calendars.palette().color(calendars.foregroundRole()) == agenda.palette().color(agenda.foregroundRole())
        assert calendars.mapTo(window, QPoint()).x() == agenda.mapTo(window, QPoint()).x()
        window.close()
        window.executor.shutdown(wait=True)
