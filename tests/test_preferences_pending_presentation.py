"""Only an outstanding source write disables the preferences control tree."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog


@pytest.fixture
def dialog():
    app = QApplication.instance() or QApplication([])
    widget = PreferencesDialog(Preferences())
    widget.show()
    app.processEvents()
    widget.sidebar_title.setText('Changed title')
    try:
        yield widget
    finally:
        widget._applying = False
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def controls(dialog):
    return dialog.buttons, dialog.pages, dialog.categories, dialog.restore_button


def source(dialog, apply):
    dialog.source_editor = SimpleNamespace(apply=apply, set_preferences=lambda _preferences: None)


@pytest.mark.parametrize('close_after', [False, True], ids=['apply', 'save'])
def test_synchronous_source_apply_keeps_all_controls_enabled(dialog, close_after):
    emitted = []
    dialog.applied.connect(lambda *_args: emitted.append(True))
    def apply(*, on_success, on_error):
        assert dialog._applying
        assert all(control.isEnabled() for control in controls(dialog))
        assert not dialog.apply(), 'Reentrant Apply must remain guarded'
        on_success(False)
    source(dialog, apply)
    with patch.object(dialog.pages, 'setEnabled', wraps=dialog.pages.setEnabled) as enabled:
        assert dialog.apply(close_after=close_after)
    assert all(call.args == (True,) for call in enabled.call_args_list)
    assert all(control.isEnabled() for control in controls(dialog))
    assert not dialog._applying
    assert emitted == [True]
    assert dialog.result_preferences.sidebar_title == 'Changed title'
    assert dialog.isVisible() is not close_after
    assert (dialog.result() == QDialog.DialogCode.Accepted) is close_after


@pytest.mark.parametrize('close_after', [False, True], ids=['apply', 'save'])
def test_pending_source_write_disables_controls_until_success(dialog, close_after):
    callbacks = {}
    def apply(**values):
        assert dialog._applying
        assert all(control.isEnabled() for control in controls(dialog))
        callbacks.update(values)
    source(dialog, apply)
    assert not dialog.apply(close_after=close_after)
    assert dialog._applying
    assert not any(control.isEnabled() for control in controls(dialog))
    assert dialog.save_button.text() == 'Saving…'
    assert not dialog.apply()
    dialog.reject()
    assert dialog.isVisible(), 'Pending changes must not close prematurely'
    callbacks['on_success'](True)
    assert not dialog._applying
    assert all(control.isEnabled() for control in controls(dialog))
    assert dialog.save_button.text() == 'Save'
    assert dialog.isVisible() is not close_after
    assert (dialog.result() == QDialog.DialogCode.Accepted) is close_after


@pytest.mark.parametrize('synchronous', [True, False], ids=['immediate', 'pending'])
def test_source_errors_restore_editable_dialog_without_applying_or_closing(dialog, synchronous):
    callbacks, emitted = {}, []
    dialog.applied.connect(lambda *_args: emitted.append(True))
    def apply(**values):
        if synchronous:
            raise ValueError('Synthetic source error')
        callbacks.update(values)
    source(dialog, apply)
    assert not dialog.apply(close_after=True)
    if not synchronous:
        assert dialog._applying
        callbacks['on_error'](ValueError('Synthetic source error'))
    assert not dialog._applying
    assert all(control.isEnabled() for control in controls(dialog))
    assert dialog.isVisible()
    assert dialog.page_heading.text() == 'Calendar sources'
    assert dialog.error.text() == 'Synthetic source error'
    assert not emitted
    assert dialog.result_preferences.sidebar_title == 'debacal'
