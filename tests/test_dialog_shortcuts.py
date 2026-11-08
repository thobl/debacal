"""Dialog keys share configurable actions without consuming text or popup input."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import gc

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from debacal.event_dialog import EventDialog
from debacal.dialogs import CalendarDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.shortcuts import DEFAULT_DIALOG_BINDINGS, shortcut_keys


START = datetime(2026, 11, 8, 9, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def show(widget):
            widgets.append(widget)
            widget.show()
            widget.activateWindow()
            app.processEvents()
            return widget
        yield app, show
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def event_editor(show, bindings=None):
    dialog = EventDialog([Calendar('local', 'Personal', '#587c67')], START, START + timedelta(hours=1),
                         scope_bindings=bindings)
    dialog.title.setText('An appointment')
    return show(dialog)


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_form_enter_accepts_both_physical_enter_keys(desktop, key):
    _, show = desktop
    dialog = event_editor(show)
    QTest.keyClick(dialog.title, key)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_events[0].title == 'An appointment'


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_notes_keep_newlines_and_explicit_accept_works_from_notes(desktop, key):
    _, show = desktop
    dialog = event_editor(show)
    dialog.description.setPlainText('First line')
    dialog.description.moveCursor(dialog.description.textCursor().MoveOperation.End)
    QTest.keyClick(dialog.description, key)
    assert dialog.isVisible()
    assert dialog.description.toPlainText() == 'First line\n'
    QTest.keyClick(dialog.description, key, Qt.KeyboardModifier.ControlModifier)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_events[0].description == 'First line\n'


def test_remapped_dialog_keys_replace_native_fallbacks_including_nested_picker(desktop):
    _, show = desktop
    dialog = event_editor(show, {'dialog_accept': ['Ctrl+S', 'Alt+A'],
                                'dialog_accept_field': 'F6', 'dialog_close': 'Alt+X'})
    QTest.keyClick(dialog.title, Qt.Key.Key_Return)
    QTest.keyClick(dialog.title, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dialog.title, Qt.Key.Key_Escape)
    assert dialog.isVisible() and not dialog.result_events
    dialog.open_range_picker('start')
    picker = dialog.range_picker
    QApplication.processEvents()
    QTest.keyClick(picker.strip, Qt.Key.Key_Return)
    assert picker.isVisible() and dialog.isVisible()
    QTest.keyClick(picker.strip, Qt.Key.Key_A, Qt.KeyboardModifier.AltModifier)
    assert not picker.isVisible() and dialog.isVisible()
    QTest.keyClick(dialog.description, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_close_action_can_be_remapped_or_disabled(desktop):
    _, show = desktop
    dialog = event_editor(show, {'dialog_close': 'Alt+X'})
    QTest.keyClick(dialog.description, Qt.Key.Key_Escape)
    assert dialog.isVisible()
    QTest.keyClick(dialog.description, Qt.Key.Key_X, Qt.KeyboardModifier.AltModifier)
    assert not dialog.isVisible() and not dialog.result_events
    dialog = event_editor(show, {'dialog_close': ''})
    QTest.keyClick(dialog.title, Qt.Key.Key_Escape)
    assert dialog.isVisible()


def test_shortcut_capture_can_record_dialog_keys_and_apply_updates_live_behavior(desktop):
    app, show = desktop
    parent = show(QWidget())
    parent.bindings = DEFAULT_DIALOG_BINDINGS
    dialog = show(PreferencesDialog(Preferences(), parent, bindings=DEFAULT_DIALOG_BINDINGS,
                                    default_bindings=DEFAULT_DIALOG_BINDINGS))
    dialog.set_category('keyboard')
    field = dialog.shortcut_fields['dialog_accept'].edits[0]
    field.setFocus()
    QTest.keyClick(field, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    assert dialog.isVisible()
    assert shortcut_keys(dialog.shortcut_fields['dialog_accept'].value()) == ('Ctrl+Return',)
    dialog.shortcut_fields['dialog_accept'].set_value('Ctrl+S')
    assert dialog.apply()
    dialog.set_category('calendar')
    dialog.duration_presets.setText('30min, 1h')
    QTest.keyClick(dialog.duration_presets, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dialog.isVisible()
    QTest.keyClick(dialog.duration_presets, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_scope_and_picker_actions_accept_multiple_configured_keys(desktop):
    _, show = desktop
    event = Event('event', 'local', 'An appointment', START, START + timedelta(hours=1),
                  recurrence_rule='FREQ=WEEKLY;COUNT=4')
    dialog = show(EventDialog([Calendar('local', 'Personal', '#587c67')], START, event=event,
                              scope_bindings={'scope_all': ['Alt+A', 'Alt+B'], 'full_day': ['F8', 'F9']}))
    for key in (Qt.Key.Key_A, Qt.Key.Key_B):
        dialog.scope.setCurrentData('single')
        QTest.keyClick(dialog.title, key, Qt.KeyboardModifier.AltModifier)
        assert dialog.scope.currentData() == 'group'
    dialog.open_range_picker('start')
    picker = dialog.range_picker
    QApplication.processEvents()
    for key in (Qt.Key.Key_F8, Qt.Key.Key_F9):
        previous = picker.full_day
        QTest.keyClick(picker.strip, key)
        assert picker.full_day != previous
    assert 'F8 or F9' in picker.full_day_button.toolTip()
    picker.reject()


def test_reopening_a_dialog_restores_keyboard_handling(desktop):
    _, show = desktop
    dialog = event_editor(show)
    QTest.keyClick(dialog.title, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    dialog.show()
    dialog.activateWindow()
    QApplication.processEvents()
    dialog.title.setText('Reopened appointment')
    QTest.keyClick(dialog.title, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_events[0].title == 'Reopened appointment'


def test_explicit_dialog_key_combination_wins_over_a_printable_layout_alias(desktop):
    _, show = desktop
    dialog = event_editor(show, {'dialog_accept': 'Shift+7', 'dialog_close': '/'})
    QCoreApplication.sendEvent(dialog.title, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_7,
                                                      Qt.KeyboardModifier.ShiftModifier, '/'))
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_unused_dialogs_can_be_collected_without_qt_callback_errors(desktop):
    app, _ = desktop
    for _ in range(6):
        dialog = CalendarDialog(allow_remote=False)
        dialog.close()
        del dialog
    gc.collect()
    app.processEvents()
