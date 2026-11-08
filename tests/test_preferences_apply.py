"""Preferences Apply updates the live workspace without closing the dialog."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QScrollArea

from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


NOW = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(8):
        app.processEvents()


def click(dialog, standard):
    QTest.mouseClick(dialog.buttons.button(standard), Qt.MouseButton.LeftButton)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        yield app, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.fixture
def workspace(desktop, tmp_path):
    app, widgets = desktop
    store = Store(tmp_path / 'preferences.sqlite3')
    with patch('debacal.ui.now', return_value=NOW):
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        window.activateWindow()
        settle(app)
        try:
            yield app, store, window
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            widgets.remove(window)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            store.close()


def test_apply_keeps_dialog_open_and_save_reuses_the_applied_value(desktop):
    app, widgets = desktop
    dialog = PreferencesDialog(Preferences(), bindings={'new_event': 'N'})
    widgets.append(dialog)
    dialog.show()
    applied = []
    dialog.applied.connect(lambda preferences, bindings: applied.append((preferences, bindings)))
    dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
    click(dialog, QDialogButtonBox.StandardButton.Apply)
    settle(app)
    assert dialog.isVisible() and dialog.result() == QDialog.DialogCode.Rejected
    assert len(applied) == 1 and applied[0][0].control_style == 'filled'
    click(dialog, QDialogButtonBox.StandardButton.Apply)
    assert len(applied) == 1
    click(dialog, QDialogButtonBox.StandardButton.Save)
    assert dialog.result() == QDialog.DialogCode.Accepted and not dialog.isVisible()
    assert len(applied) == 1


def test_compact_preferences_sidebar_keeps_its_title_and_categories_readable(desktop):
    app, widgets = desktop
    dialog = PreferencesDialog(Preferences(font_scale=140))
    widgets.append(dialog)
    dialog.resize(800, 540)
    dialog.show()
    settle(app)
    assert dialog.width() <= 800 and dialog.height() <= 540
    title = dialog.navigation_title
    assert title.fontMetrics().horizontalAdvance(title.text()) <= title.contentsRect().width()
    assert not dialog.categories.horizontalScrollBar().isVisible()
    for index in range(dialog.categories.count()):
        item = dialog.categories.item(index)
        assert item.toolTip() == item.text()


def test_invalid_preferences_or_shortcuts_do_not_apply_or_close(desktop):
    app, widgets = desktop
    original = Preferences()
    dialog = PreferencesDialog(original, bindings={'previous': 'H', 'next': 'L'})
    widgets.append(dialog)
    dialog.show()
    applied = []
    dialog.applied.connect(lambda *value: applied.append(value))
    dialog.visible_start.setValue(20)
    dialog.visible_end.setValue(8)
    click(dialog, QDialogButtonBox.StandardButton.Apply)
    assert dialog.isVisible() and dialog.error.text() and not applied
    assert dialog.result_preferences == original
    dialog.visible_start.setValue(8)
    dialog.visible_end.setValue(18)
    dialog.shortcut_fields['next'].setKeySequence(QKeySequence('H'))
    click(dialog, QDialogButtonBox.StandardButton.Save)
    assert dialog.isVisible() and not applied
    assert dialog.page_heading.text() == 'Keyboard shortcuts'
    assert dialog.shortcut_editor.error.text()
    dialog.shortcut_fields['next'].setKeySequence(QKeySequence('Alt+L'))
    click(dialog, QDialogButtonBox.StandardButton.Apply)
    settle(app)
    assert len(applied) == 1 and dialog.isVisible()
    assert not dialog.error.text() and not dialog.shortcut_editor.error.text()


def test_live_apply_then_cancel_keeps_applied_settings_and_discards_later_edits(workspace):
    app, store, window = workspace
    original = window.preferences
    expected = replace(original, font_scale=140, color_scheme='warm', control_style='filled',
                       agenda_style='grouped', locale_name='de_DE', auto_sync_enabled=False,
                       sync_interval_minutes=13)
    applied_bindings = dict(window.bindings, search='Ctrl+Alt+F')

    def interact(dialog):
        dialog.show()
        dialog.font_scale.setValue(140)
        dialog.color_scheme.setCurrentIndex(dialog.color_scheme.findData('warm'))
        dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
        dialog.agenda_style.setCurrentIndex(dialog.agenda_style.findData('grouped'))
        dialog.locale.setCurrentIndex(dialog.locale.findData('de_DE'))
        dialog.auto_sync.setChecked(False)
        dialog.sync_interval.setValue(13)
        dialog.shortcut_fields['search'].setKeySequence(QKeySequence('Ctrl+Alt+F'))
        click(dialog, QDialogButtonBox.StandardButton.Apply)
        settle(app)
        assert dialog.isVisible()
        assert window.preferences == expected and Preferences.load(store) == expected
        assert window.bindings == applied_bindings and store.get_setting('keybindings') == applied_bindings
        assert window.locale().name() == QDialog.locale(dialog).name() == 'de_DE'
        assert window.view_control.preferences == expected
        assert 'Ctrl+Alt+F' in window.search_button.toolTip()
        assert not window.sync_timer.isActive()
        dialog.font_scale.setValue(80)
        dialog.color_scheme.setCurrentIndex(dialog.color_scheme.findData('cool'))
        click(dialog, QDialogButtonBox.StandardButton.Cancel)
        return dialog.result()

    with patch.object(PreferencesDialog, 'exec', new=interact):
        window.show_preferences('Appearance')
    assert window.preferences == expected and window.bindings == applied_bindings
    assert Preferences.load(store) == expected
    with Store(store.path) as reopened:
        assert Preferences.load(reopened) == expected
        assert reopened.get_setting('keybindings') == applied_bindings


def test_save_only_and_apply_then_save_refresh_once_per_changed_value(workspace):
    app, store, window = workspace

    def save_only(dialog):
        dialog.color_scheme.setCurrentIndex(dialog.color_scheme.findData('warm'))
        dialog.save()
        return dialog.result()

    with patch.object(window, 'refresh', wraps=window.refresh) as refresh, \
         patch.object(PreferencesDialog, 'exec', new=save_only):
        window.show_preferences()
        assert refresh.call_count == 1
    assert window.preferences.color_scheme == 'warm'

    def apply_then_save(dialog):
        dialog.show()
        dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
        click(dialog, QDialogButtonBox.StandardButton.Apply)
        settle(app)
        assert dialog.isVisible()
        dialog.save()
        return dialog.result()

    with patch.object(window, 'refresh', wraps=window.refresh) as refresh, \
         patch.object(PreferencesDialog, 'exec', new=apply_then_save):
        window.show_preferences()
        assert refresh.call_count == 1
    assert window.preferences == Preferences.load(store)
    assert window.preferences.control_style == 'filled'


def test_apply_large_text_keeps_category_controls_and_dialog_actions_reachable(workspace):
    app, _, window = workspace

    def interact(dialog):
        dialog.resize(580, 440)
        dialog.show()
        dialog.font_scale.setValue(140)
        dialog.apply()
        settle(app)
        assert dialog.isVisible()
        assert dialog.rect().contains(dialog.buttons.geometry())
        for name in ('Calendar', 'Appearance'):
            dialog.set_category(name)
            page = dialog.appearance_editor.controls_scroll if name == 'Appearance' else dialog.pages.currentWidget()
            assert isinstance(page, QScrollArea)
            settle(app)
            scrollbar = page.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            settle(app)
            assert scrollbar.value() == scrollbar.maximum()
        assert dialog.height() <= 440
        dialog.reject()
        return dialog.result()

    with patch.object(PreferencesDialog, 'exec', new=interact):
        window.show_preferences('Appearance')
