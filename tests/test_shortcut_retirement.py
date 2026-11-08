"""Retired commands stay retired while persistent selection keys upgrade."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from debacal.shortcuts import KeyboardShortcutsEditor
from debacal.store import Store
from debacal.ui import DEFAULT_BINDINGS, MainWindow


def test_shortcut_editor_omits_retired_action_and_preserves_the_reassigned_g_key():
    app = QApplication.instance() or QApplication([])
    editor = KeyboardShortcutsEditor({'select_event': 'Ctrl+G', 'next': 'G'}, DEFAULT_BINDINGS)
    try:
        assert 'select_event' not in editor.fields
        assert editor.value()['next'] == 'G'
        editor.restore_defaults()
        assert 'select_event' not in editor.value()
        assert editor.value()['next'] == DEFAULT_BINDINGS['next']
    finally:
        editor.close()
        editor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_keyboard_editor_groups_related_actions_without_the_old_explanatory_line():
    app = QApplication.instance() or QApplication([])
    editor = KeyboardShortcutsEditor({'shortcuts': 'F1', 'cycle_scope': 'Alt+S'}, DEFAULT_BINDINGS)
    try:
        assert editor.groups['Calendar view'] == ('day_view', 'week_view', 'month_view', 'year_view', 'full_day')
        assert editor.groups['Selection'][:3] == ('scope_single', 'scope_all', 'scope_following')
        assert not {'shortcuts', 'cycle_scope', 'select_event'} & editor.fields.keys()
        assert not any('Selection modes have their own' in label.text() for label in editor.findChildren(QLabel))
        assert set(editor.fields) == set(DEFAULT_BINDINGS)
    finally:
        editor.close()
        editor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_workspace_removes_saved_number_selection_without_losing_other_bindings():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        store.set_setting('keybindings_version', 5)
        store.set_setting('keybindings', {'select_event': 'Ctrl+G', 'next': 'G'})
        window = MainWindow(store, enable_sync=False)
        try:
            assert 'select_event' not in window.bindings
            assert 'select_event' not in store.get_setting('keybindings')
            assert window.bindings['next'] == store.get_setting('keybindings')['next'] == 'G'
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_workspace_upgrades_default_selection_keys_and_removes_old_commands():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        store.set_setting('keybindings_version', 5)
        store.set_setting('keybindings', {'shortcuts': 'F1', 'cycle_scope': 'Alt+S',
                                         'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4',
                                         'new_event': 'Ctrl+N'})
        window = MainWindow(store, enable_sync=False)
        try:
            window.show()
            window.activateWindow()
            window.canvas.setFocus()
            app.processEvents()
            assert not {'shortcuts', 'cycle_scope'} & window.bindings.keys()
            assert store.get_setting('keybindings_version') == 6
            assert window.bindings['new_event'] == 'Ctrl+N'
            for key, scope in ((Qt.Key.Key_F3, 'future'), (Qt.Key.Key_F1, 'single'), (Qt.Key.Key_F2, 'group')):
                QTest.keyClick(window.canvas, key)
                app.processEvents()
                assert window.selection.scope == scope
            QTest.keyClick(window.canvas, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier)
            assert window.selection.scope == 'group'
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
