"""Responsive header grouping and connected single-choice controls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet
from debacal.store import Store
from debacal.ui import MainWindow


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        window.show()
        settle(app)
        try:
            yield window, app
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def point(window, widget):
    return widget.mapTo(window, widget.rect().topLeft())


def test_header_wraps_controls_and_can_shrink_again_without_moving_navigation(desktop):
    window, app = desktop
    window.resize(1400, 900)
    settle(app)
    assert window.workspace_header.inline
    navigation = point(window, window.previous_button)
    assert point(window, window.next_button).x() < point(window, window.heading).x()
    assert point(window, window.new_button).y() < window.workspace_header.geometry().bottom()
    assert point(window, window.new_button).x() < point(window, window.selection_control).x()
    assert point(window, window.selection_control).x() < point(window, window.view_control).x()
    window.resize(1030, 690)
    settle(app)
    assert window.width() == 1030 and not window.workspace_header.inline
    assert point(window, window.previous_button) == navigation
    assert point(window, window.new_button).y() > point(window, window.heading).y() + window.heading.height()
    window.resize(1400, 900)
    settle(app)
    assert window.workspace_header.inline
    assert point(window, window.previous_button) == navigation


def test_choice_groups_are_connected_and_full_day_is_independent(desktop):
    window, app = desktop
    for container, buttons in ((window.view_control, list(window.view_buttons.values())),
                               (window.selection_control, list(window.selection_buttons.values()))):
        assert container.objectName() == 'segmentedControl'
        assert all(button.parentWidget() is container and button.isCheckable() for button in buttons)
        assert [button.property('segment') for button in buttons] == ['first', *(['middle'] * (len(buttons) - 2)), 'last']
        for button in buttons:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            settle(app)
            assert button.isChecked()
            assert sum(item.isChecked() for item in buttons) == 1
    assert window.full_day_button.parentWidget() is not window.view_control
    assert window.new_button.property('segment') is None
    window.set_view('week')
    QTest.mouseClick(window.full_day_button, Qt.MouseButton.LeftButton)
    assert window.view_buttons['week'].isChecked()
    assert window.full_day_button.isChecked()


@pytest.mark.parametrize('scale', [80, 140])
def test_header_reflows_after_text_size_change_without_horizontal_overflow(desktop, scale):
    window, app = desktop
    window.preferences = replace(window.preferences, font_scale=scale)
    window.setStyleSheet(stylesheet(scale))
    window.resize(1030, 690)
    window.refresh()
    settle(app)
    assert window.width() == 1030
    right = window.toolbar.mapTo(window, window.toolbar.rect().topRight()).x()
    assert right < window.width()
    for button in (window.new_button, *window.selection_buttons.values(), *window.view_buttons.values(), window.full_day_button):
        assert button.isVisible()
        assert window.workspace_header.rect().contains(button.mapTo(window.workspace_header, button.rect().center()))
