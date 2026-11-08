"""Typeface and text size preview safely without accidental scroll edits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from debacal.appearance import stylesheet
from debacal.fonts import resolved_font_family, ui_font
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store


@pytest.fixture
def dialog():
    app = QApplication.instance() or QApplication([])
    widget = PreferencesDialog(Preferences())
    widget.set_category('Appearance')
    widget.show()
    widget.activateWindow()
    app.processEvents()
    yield app, widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wheel(widget, amount=-120):
    local = QPointF(10, 10)
    event = QWheelEvent(local, QPointF(widget.mapToGlobal(QPoint(10, 10))), QPoint(), QPoint(0, amount),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


def test_wheel_over_text_controls_never_edits_values_even_after_focusing_them(dialog):
    app, widget = dialog
    editor = widget.appearance_editor
    editor.controls_scroll.ensureWidgetVisible(editor.section_widgets['Font'])
    app.processEvents()
    initial_size, initial_family = widget.font_scale.value(), widget.font_family.currentData()
    for control in (widget.font_scale, widget.font_family):
        widget.save_button.setFocus()
        wheel(control)
        control.setFocus()
        wheel(control, 120)
    assert widget.font_scale.value() == initial_size
    assert widget.font_family.currentData() == initial_family
    widget.font_scale.setFocus()
    QTest.keyClick(widget.font_scale, Qt.Key.Key_Up)
    assert widget.font_scale.value() == initial_size + 10


def test_font_choice_previews_applies_and_persists_as_one_shared_family(dialog):
    app, widget = dialog
    families = QFontDatabase.families()
    if not families:
        pytest.skip('No desktop fonts are available.')
    family = next((name for name in families if 'Serif' in name), families[0])
    widget.font_family.setCurrentIndex(widget.font_family.findData(family))
    assert widget.appearance_editor.preview.preferences.font_family == family
    widget.font_scale.setValue(120)
    assert widget.apply()
    assert widget.result_preferences.font_family == family
    assert widget.font_scale.value() == 120
    assert resolved_font_family(widget.result_preferences) == family
    assert ui_font(widget.result_preferences, 10).pointSizeF() == 12
    assert 'font-family: "' + family + '"' in stylesheet(120, widget.result_preferences)
    with Store(':memory:') as store:
        widget.result_preferences.save(store)
        assert Preferences.load(store) == widget.result_preferences
    widget.restore_defaults()
    assert widget.font_family.currentData() == 'system'


@pytest.mark.parametrize('family', ['', ' ', None, [], 'font\nfamily', 'x' * 121])
def test_invalid_font_settings_cannot_break_startup(family):
    with pytest.raises(ValueError, match='font'):
        Preferences(font_family=family)
    assert Preferences.from_dict({'font_family': family}).font_family == 'system'


def test_settings_put_explanations_and_format_samples_in_tooltips(dialog):
    _, widget = dialog
    visible_text = [label.text() for label in widget.findChildren(QLabel)]
    assert not any('Interface labels remain' in value or 'Separate the durations' in value
                   or 'Conflicts can always' in value or 'Week begins' in value for value in visible_text)
    assert 'Interface labels remain' in widget.locale.toolTip()
    assert 'Week begins' in widget.date_format.toolTip()
    assert 'commas' in widget.duration_presets.toolTip()
