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
    assert point(window, window.previous_button).x() < point(window, window.heading).x()
    assert point(window, window.next_button).x() >= point(window, window.heading).x() + window.heading.width()
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


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_button_style_changes_only_paint_and_keeps_shared_separator_in_every_view(desktop, scale):
    window, app = desktop
    window.resize(1440, 960)
    for view in ('day', 'week', 'month', 'year'):
        states = []
        for style in ('underlined', 'filled'):
            window.preferences = replace(window.preferences, font_scale=scale, control_style=style)
            window.setStyleSheet(stylesheet(scale))
            window.set_view(view)
            settle(app)
            states.append(tuple(widget.geometry().getRect() for widget in (
                window.workspace_header, window.period, window.heading, window.range_label,
                window.previous_button, window.today_button, window.next_button,
                window.toolbar, window.selection_control, window.view_control,
                window.workspace_header.separator, window.stack)))
            assert window.workspace_header.separator.isVisible()
            assert window.range_label.width() >= window.range_label.fontMetrics().horizontalAdvance(window.range_label.text())
            heading_baseline = (point(window, window.heading).y() + window.heading.contentsRect().bottom()
                                - window.heading.fontMetrics().descent())
            for section in window.toolbar_sections:
                label = section.layout().itemAt(0).layout().itemAt(0).widget()
                baseline = point(window, label).y() + label.contentsRect().bottom() - label.fontMetrics().descent()
                assert baseline == heading_baseline
            assert point(window, window.heading).y() == point(window, window.next_button).y()
            assert point(window, window.range_label).y() + window.range_label.height() / 2 == pytest.approx(
                point(window, window.today_button).y() + window.today_button.height() / 2, abs=1)
        assert states[0] == states[1]


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_month_navigation_aligns_with_grid_and_next_stays_still_for_short_names(desktop, scale):
    from datetime import date
    window, app = desktop
    window.preferences = replace(window.preferences, font_scale=scale, locale_name='en_GB')
    window.setStyleSheet(stylesheet(scale))
    for view in ('week', 'month'):
        positions = []
        for month in (5, 9):
            window.go_date(date(2026, month, 6))
            window.set_view(view)
            settle(app)
            canvas = window.month if view == 'month' else window.canvas
            gutter = window.month.week_gutter if view == 'month' else window.canvas.gutter
            assert point(window, window.heading).x() == point(window, canvas).x() + gutter
            positions.append((point(window, window.previous_button), point(window, window.next_button)))
        assert positions[0] == positions[1]
    month_width = window.heading.width()
    window.set_view('year')
    settle(app)
    assert window.heading.width() < month_width / 2
    # Long full-year range sits below independently of the short year label.
    assert point(window, window.next_button).x() <= point(window, window.heading).x() + window.heading.width() + 1
    assert window.range_label.width() > window.heading.width()


def test_filled_group_surface_reaches_edges_and_keeps_native_choice_hit_targets(desktop):
    from debacal.theme import scheme_for
    window, app = desktop
    window.preferences = replace(window.preferences, control_style='filled')
    window.refresh()
    window.active_canvas().setFocus()
    settle(app)
    group = window.view_control
    for mode in ('day', 'year'):
        window.set_view(mode)
        settle(app)
        buttons = list(window.view_buttons.values())
        assert buttons[0].geometry().left() == 0
        assert buttons[-1].geometry().right() == group.width() - 1
        assert all(button.height() == group.height() for button in buttons)
        assert all(left.geometry().right() + 1 == right.geometry().left() for left, right in zip(buttons, buttons[1:]))
        image = group.grab().toImage()
        selected = window.view_buttons[mode]
        color = scheme_for(window.preferences).selection
        assert image.pixelColor(selected.geometry().center().x(), 2).name() == color
        edge = 2 if mode == 'day' else group.width() - 3
        assert image.pixelColor(edge, group.height() // 2).name() == color
