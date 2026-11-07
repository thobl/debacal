"""Retired numbered selection cannot return through saved shortcut bindings."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

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
