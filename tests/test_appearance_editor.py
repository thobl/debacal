"""Visual choices preview pending appearance locally and apply validated values."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QTabWidget

from debacal.appearance import stylesheet, widget_palette
from debacal.appearance_preferences import VisualChoice
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets = []
    yield app, widgets
    for widget in reversed(widgets):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def dialog(desktop, preferences=None):
    app, widgets = desktop
    preferences = preferences or Preferences(locale_name='en_GB')
    item = PreferencesDialog(preferences)
    widgets.append(item)
    item.setStyleSheet(stylesheet(preferences.font_scale, preferences))
    item.setPalette(widget_palette(preferences))
    item.resize(760, 720)
    item.set_category('Appearance')
    item.show()
    item.activateWindow()
    settle(app)
    return item


def choose(widget, value):
    QTest.mouseClick(widget.buttons[widget.findData(value)], Qt.MouseButton.LeftButton)


def test_visual_options_are_real_clickable_and_keyboard_operable_controls(desktop):
    app, _ = desktop
    item = dialog(desktop)
    editor = item.appearance_editor
    assert list(editor.section_widgets) == ['Colors', 'Font', 'Interface', 'Sidebar']
    assert editor.section_widgets['Font'].isAncestorOf(item.font_scale)
    assert editor.section_widgets['Font'].isAncestorOf(item.font_family)
    assert not editor.findChildren(QTabWidget)
    assert editor.section_widgets['Colors'].isAncestorOf(item.weekend_style)
    assert isinstance(item.color_scheme, VisualChoice) and not isinstance(item.color_scheme, QComboBox)
    choose(item.color_scheme, 'cool')
    assert item.color_scheme.currentData() == editor.preview.preferences.color_scheme == 'cool'
    current = item.color_scheme.buttons[item.color_scheme.findData('cool')]
    current.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(current, Qt.Key.Key_Right)
    settle(app)
    assert item.color_scheme.currentData() == 'custom'
    assert editor.custom_colors.isVisible()
    assert item.color_scheme.buttons[-1].hasFocus()
    assert sum(button.isChecked() for button in item.color_scheme.buttons) == 1


def test_pending_palette_and_layout_preview_never_write_to_the_workspace(desktop):
    app, widgets = desktop
    with Store(':memory:') as store:
        original = Preferences(locale_name='en_GB')
        original.save(store)
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        item = PreferencesDialog(original, window, bindings=window.bindings)
        widgets.append(item)
        item.applied.connect(window.apply_preferences)
        item.set_category('Appearance')
        item.show()
        item.appearance_mode.setCurrentIndex(item.appearance_mode.findData('dark'))
        item.color_scheme.setCurrentIndex(item.color_scheme.findData('custom'))
        item.custom_accent.setColor('#2854b9')
        item.custom_surface.setColor('#6c7288')
        item.custom_weekend.setColor('#a82a64')
        item.weekend_style.setCurrentIndex(item.weekend_style.findData('background'))
        item.show_sidebar_title.setChecked(False)
        item.sidebar_title.setText('Work calendar')
        settle(app)
        pending = item.appearance_editor.preview.preferences
        assert pending.appearance_mode == 'dark' and pending.custom_accent == '#2854b9'
        assert pending.weekend_style == 'background' and not pending.show_sidebar_title
        assert Preferences.load(store) == window.preferences == original
        QTest.mouseClick(item.buttons.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
        settle(app)
        assert item.isVisible()
        assert window.preferences == Preferences.load(store) == replace(
            original, appearance_mode='dark', color_scheme='custom', custom_accent='#2854b9',
            custom_surface='#6c7288', custom_weekend='#a82a64', weekend_style='background',
            show_sidebar_title=False, sidebar_title='Work calendar')
        item.reject()
        window.close()
        window.executor.shutdown(wait=True)


@pytest.mark.parametrize('field,value', [
    ('appearance_mode', 'dark'), ('color_scheme', 'warm'), ('control_style', 'filled'),
    ('agenda_style', 'cards'), ('weekend_style', 'background'), ('weekend_style', 'none'),
    ('font_scale', 140), ('week_numbers', False), ('visible_start', 6), ('visible_end', 22),
    ('show_sidebar_title', False), ('sidebar_title', 'My calendar'), ('corner_style', 'square'),
    ('corner_style', 'rounded'),
])
def test_each_appearance_control_changes_the_live_sample(desktop, field, value):
    app, _ = desktop
    item = dialog(desktop)
    preview = item.appearance_editor.preview
    before = preview.grab().toImage()
    control = getattr(item, field)
    if isinstance(control, VisualChoice):
        control.setCurrentIndex(control.findData(value))
    elif isinstance(value, bool):
        control.setChecked(value)
    elif isinstance(value, int):
        control.setValue(value)
    else:
        control.setText(value)
    settle(app)
    assert preview.grab().toImage() != before


def test_custom_colors_validate_before_apply_and_presets_keep_usable_values(desktop):
    app, _ = desktop
    item = dialog(desktop)
    item.color_scheme.setCurrentIndex(item.color_scheme.findData('custom'))
    item.custom_accent.field.setText('#12')
    before = item.appearance_editor.preview.preferences.custom_accent
    assert not item.apply() and 'six-digit' in item.error.text()
    assert item.appearance_editor.preview.preferences.custom_accent == before
    item.color_scheme.setCurrentIndex(item.color_scheme.findData('neutral'))
    assert item.apply()
    assert item.result_preferences.color_scheme == 'neutral'
    assert item.result_preferences.custom_accent == before
    item.sidebar_title.setText('')
    assert not item.apply() and 'title' in item.error.text().lower()
    item.show_sidebar_title.setChecked(False)
    assert item.apply() and not item.result_preferences.show_sidebar_title


@pytest.mark.parametrize('scale', [80, 140])
def test_narrow_appearance_layout_reflows_choices_and_keeps_preview_and_apply_reachable(desktop, scale):
    app, _ = desktop
    item = dialog(desktop, Preferences(font_scale=scale, color_scheme='custom'))
    item.resize(580, 440)
    settle(app)
    assert item.width() == 580 and item.height() == 440
    assert item.rect().contains(item.buttons.geometry())
    editor = item.appearance_editor
    preview_rect = editor.preview.geometry()
    for section in editor.section_widgets.values():
        scroll = editor.controls_scroll
        scroll.ensureWidgetVisible(section)
        settle(app)
        assert not scroll.horizontalScrollBar().isVisible()
        assert editor.preview.isVisible() and editor.preview.geometry() == preview_rect
        for choice in section.findChildren(VisualChoice):
            assert all(button.width() > 40 for button in choice.buttons)
    item.restore_defaults()
    assert item.value() == Preferences()
