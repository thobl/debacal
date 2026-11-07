"""Themed flat subcontrols have visible arrows and preserve native stepping."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QComboBox, QDateTimeEdit, QSpinBox, QStyle,
                              QStyleOptionComboBox, QStyleOptionSpinBox, QVBoxLayout, QWidget)
from debacal.appearance import stylesheet, widget_palette
from debacal.preferences import Preferences
from debacal.theme import scheme_for
from debacal.event_style import contrast_ratio


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('name', ['neutral', 'warm', 'cool', 'custom'])
def test_combo_and_number_arrows_are_visible_flat_and_operable(mode, name):
    app = QApplication.instance() or QApplication([])
    preferences = Preferences(appearance_mode=mode, color_scheme=name)
    window = QWidget()
    window.setPalette(widget_palette(preferences))
    window.setStyleSheet(stylesheet(100, preferences))
    layout = QVBoxLayout(window)
    combo, number, stamp = QComboBox(), QSpinBox(), QDateTimeEdit()
    combo.addItems(['First', 'Second'])
    number.setValue(50)
    for field in (combo, number, stamp):
        layout.addWidget(field)
    window.show()
    app.processEvents()
    try:
        scheme = scheme_for(preferences)
        for field, option, control, subcontrol in (
            (combo, QStyleOptionComboBox(), QStyle.ComplexControl.CC_ComboBox, QStyle.SubControl.SC_ComboBoxArrow),
            (number, QStyleOptionSpinBox(), QStyle.ComplexControl.CC_SpinBox, QStyle.SubControl.SC_SpinBoxUp),
            (stamp, QStyleOptionSpinBox(), QStyle.ComplexControl.CC_SpinBox, QStyle.SubControl.SC_SpinBoxDown),
        ):
            field.initStyleOption(option)
            rect = field.style().subControlRect(control, option, subcontrol, field)
            assert rect.width() >= 20 and rect.height() >= 10
            image = field.grab().toImage()
            ink = [image.pixelColor(x, y).name() for x in range(rect.left() + 3, rect.right() - 2)
                   for y in range(rect.top() + 2, rect.bottom() - 1)]
            assert sum(contrast_ratio(pixel, scheme.control_surface) >= 3 for pixel in ink) >= 4
        # The button's surface is a solid color rather than a native gradient.
        image = combo.grab().toImage()
        assert len({image.pixelColor(image.width() - 5, y).name() for y in range(5, image.height() - 5)}) == 1
        option = QStyleOptionSpinBox()
        number.initStyleOption(option)
        for subcontrol, expected in ((QStyle.SubControl.SC_SpinBoxUp, 51), (QStyle.SubControl.SC_SpinBoxDown, 50)):
            rect = number.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, subcontrol, number)
            QTest.mouseClick(number, Qt.MouseButton.LeftButton, pos=rect.center())
            assert number.value() == expected
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
