"""Independent key combinations share actions without prefix or dialog ambiguity."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import timedelta
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.preferences import Preferences
from debacal.shortcuts import (
    DEFAULT_DIALOG_BINDINGS, KeyboardShortcutsEditor, migrate_workspace_bindings,
    shortcut_keys, shortcut_label, validate_bindings,
)
from debacal.store import Store
from debacal.ui import DEFAULT_BINDINGS, MainWindow


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    window = MainWindow(store, enable_sync=False)
    window.show()
    window.activateWindow()
    window.canvas.setFocus()
    app.processEvents()
    yield app, store, window
    window.close()
    window.executor.shutdown(wait=True)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    store.close()


def test_default_period_keys_and_edit_alternatives_work_from_calendar(workspace):
    app, _, window = workspace
    anchor = window.anchor
    QTest.keyClick(window.canvas, Qt.Key.Key_PageDown)
    assert window.anchor == anchor + timedelta(days=7)
    QTest.keyClick(window.canvas, Qt.Key.Key_PageUp)
    assert window.anchor == anchor
    window.edit_event = Mock()
    for key, modifiers in ((Qt.Key.Key_E, Qt.KeyboardModifier.NoModifier),
                           (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
                           (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)):
        QTest.keyClick(window.canvas, key, modifiers)
    assert window.edit_event.call_count == 3
    QTest.keyClick(window.canvas, Qt.Key.Key_E, Qt.KeyboardModifier.ShiftModifier)
    assert window.edit_event.call_count == 3
    assert not hasattr(window, '_shortcut_pending') and not hasattr(window, 'shortcut_timer')


def test_remapped_multiple_keys_persist_and_work_without_sequence_state(workspace):
    app, store, window = workspace
    bindings = dict(window.bindings, next=['Ctrl+K', 'Alt+L'])
    window.apply_preferences(Preferences(), bindings)
    anchor = window.anchor
    for key, modifiers in ((Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier),
                           (Qt.Key.Key_L, Qt.KeyboardModifier.AltModifier)):
        QTest.keyClick(window.canvas, key, modifiers)
    assert window.anchor == anchor + timedelta(days=14)
    assert store.get_setting('keybindings')['next'] == ['Ctrl+K', 'Alt+L']
    assert shortcut_label(bindings['next']) == 'Ctrl+K or Alt+L'
    restored = MainWindow(store, enable_sync=False)
    try:
        assert restored.bindings['next'] == ['Ctrl+K', 'Alt+L']
    finally:
        restored.close()
        restored.executor.shutdown(wait=True)
        restored.deleteLater()


def test_search_aliases_are_visible_and_each_can_be_removed(workspace):
    app, _, window = workspace
    assert window.bindings['search'] == ['Ctrl+F', '/']
    QTest.keyClick(window.canvas, Qt.Key.Key_Slash)
    assert window.search.hasFocus()
    window.exit_search()
    window.bindings['search'] = 'Ctrl+F'
    QTest.keyClick(window.canvas, Qt.Key.Key_Slash)
    assert not window.search.hasFocus()
    QTest.keyClick(window.canvas, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert window.search.hasFocus()


def test_alternative_editor_records_one_combination_and_adds_removes_rows(workspace):
    _, _, window = workspace
    editor = KeyboardShortcutsEditor(window.bindings, DEFAULT_BINDINGS)
    try:
        field = editor.fields['edit_event']
        assert field.value() == ['E', 'Return']
        assert len(field.edits) == 2
        assert all(edit.maximumSequenceLength() == 1 for edit in field.edits)
        extra = field.add_shortcut('Alt+E')
        assert editor.value()['edit_event'] == ['E', 'Return', 'Alt+E']
        field.remove_shortcut(extra)
        assert editor.value()['edit_event'] == ['E', 'Return']
        field.remove_shortcut(field.edits[1])
        field.setKeySequence('Alt+E')
        assert editor.value()['edit_event'] == 'Alt+E'
        editor.restore_defaults()
        assert field.value() == ['E', 'Return']
        assert 'Dialogs' in editor.groups and 'edit_single' not in editor.fields
    finally:
        editor.deleteLater()


def test_validation_canonicalizes_enter_and_checks_each_active_context():
    assert shortcut_keys(['Enter', 'Return']) == ('Return',)
    valid = validate_bindings({'edit_event': ['E', 'Enter']} | DEFAULT_DIALOG_BINDINGS)
    assert valid['edit_event'] == ['E', 'Return']
    for bindings in ({'edit_event': ['E', 'Return'], 'new_event': 'E'},
                     {'edit_event': ['E', 'Return'], 'new_event': 'Enter'},
                     {'scope_all': 'Ctrl+Enter'} | DEFAULT_DIALOG_BINDINGS,
                     {'full_day': 'Esc'} | DEFAULT_DIALOG_BINDINGS):
        with pytest.raises(ValueError, match='unique'):
            validate_bindings(bindings)
    with pytest.raises(ValueError, match='single'):
        validate_bindings({'next': 'Ctrl+K, Ctrl+L'})


def test_migration_retires_sequences_and_yields_only_conflicting_default_alternatives():
    saved = {'previous': 'H', 'next': 'L', 'edit_event': 'E', 'edit_single': 'Alt+E',
             'history': 'Ctrl+K, H', 'new_event': 'Return', 'full_day': ''}
    migrated = migrate_workspace_bindings(saved, DEFAULT_BINDINGS, version=6)
    assert migrated['previous'] == 'PgUp' and migrated['next'] == 'PgDown'
    assert migrated['edit_event'] == 'E' and migrated['new_event'] == 'Return'
    assert migrated['history'] == '' and migrated['full_day'] == ''
    assert 'edit_single' not in migrated
    assert migrated['dialog_accept_field'] == 'Return'
    assert migrated['search'] == ['Ctrl+F', '/']
    assert validate_bindings(migrated) == migrated
    assert migrate_workspace_bindings(migrated, DEFAULT_BINDINGS, version=7) == migrated


def test_custom_navigation_and_editing_shortcuts_survive_default_upgrade():
    saved = {'previous': ['Alt+Left', 'Ctrl+PgUp'], 'next': 'Alt+Right', 'edit_event': 'F9'}
    migrated = migrate_workspace_bindings(saved, DEFAULT_BINDINGS, version=6)
    assert all(migrated[action] == value for action, value in saved.items())
    assert validate_bindings(migrated) == migrated
