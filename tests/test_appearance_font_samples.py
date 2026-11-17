"""Font samples are complete before paint without repeated Qt font resolution."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from debacal.appearance_preferences import AppearanceEditor
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog


FAMILIES = ['DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono']


@pytest.fixture
def desktop(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QFontDatabase, 'families', lambda: list(FAMILIES))
    widgets = []
    yield app, widgets
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def assert_samples(editor):
    field = editor.font_family
    assert field.itemData(0, Qt.ItemDataRole.FontRole) is None
    assert [field.itemData(index) for index in range(field.count())] == ['system'] + FAMILIES
    for index, family in enumerate(FAMILIES, 1):
        assert field.itemData(index, Qt.ItemDataRole.FontRole).family() == family


def test_standalone_editor_keeps_complete_font_sample_model(desktop):
    _, widgets = desktop
    editor = AppearanceEditor(Preferences(font_family='DejaVu Serif'))
    widgets.append(editor)
    assert_samples(editor)
    assert editor.font_family.currentData() == 'DejaVu Serif'


def test_deferred_samples_preserve_value_and_emit_no_preference_change(desktop):
    _, widgets = desktop
    editor = AppearanceEditor(Preferences(font_family='DejaVu Serif'), defer_font_samples=True)
    widgets.append(editor)
    changed = QSignalSpy(editor.changed)
    assert all(editor.font_family.itemData(index, Qt.ItemDataRole.FontRole) is None
               for index in range(editor.font_family.count()))
    editor.finish_font_samples()
    assert_samples(editor)
    assert editor.font_family.currentData() == 'DejaVu Serif'
    assert editor.preview.preferences.font_family == 'DejaVu Serif'
    assert changed.count() == 0
    editor.font_family.setItemData(1, QFont('Unchanged second time'), Qt.ItemDataRole.FontRole)
    editor.finish_font_samples()
    assert editor.font_family.itemData(1, Qt.ItemDataRole.FontRole).family() == 'Unchanged second time'
    assert changed.count() == 0


def test_dialog_completes_samples_after_attachment_and_before_first_paint(desktop, monkeypatch):
    app, widgets = desktop
    completed = []
    painted = []
    finish = AppearanceEditor.finish_font_samples
    paint = AppearanceEditor.paintEvent

    def finish_attached(editor):
        assert editor.parentWidget() is not None
        assert editor.window() is dialog
        assert editor.font_family.itemData(1, Qt.ItemDataRole.FontRole) is None
        completed.append(editor)
        finish(editor)

    def paint_complete(editor, event):
        assert_samples(editor)
        painted.append(editor)
        paint(editor, event)

    monkeypatch.setattr(AppearanceEditor, 'finish_font_samples', finish_attached)
    monkeypatch.setattr(AppearanceEditor, 'paintEvent', paint_complete)
    dialog = PreferencesDialog(Preferences())
    widgets.append(dialog)
    dialog.set_category('Appearance')
    assert completed == [dialog.appearance_editor]
    assert_samples(dialog.appearance_editor)
    dialog.show()
    app.processEvents()
    assert painted
    dialog.set_category('Calendar')
    dialog.set_category('Appearance')
    assert completed == [dialog.appearance_editor]
