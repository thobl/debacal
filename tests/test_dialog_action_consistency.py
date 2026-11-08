"""Dialog closing actions use one visual order and unambiguous semantics."""
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from debacal.calendar_choice_dialog import CalendarChoiceDialog
from debacal.date_time_picker import DateTimeRangePicker
from debacal.dialogs import CalendarDialog
from debacal.event_dialog import EventDialog
from debacal.import_dialog import ImportDialog
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
CALENDARS = [Calendar('local', 'Personal', '#587c67')]


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets = []
    yield app, widgets
    for dialog in reversed(widgets):
        dialog.close()
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('kind', ['event', 'calendar', 'preferences', 'time', 'export', 'import'])
def test_dialog_footers_place_neutral_cancel_before_the_highlighted_accept(desktop, kind):
    app, widgets = desktop
    if kind == 'event':
        dialog = EventDialog(CALENDARS, START)
    elif kind == 'calendar':
        dialog = CalendarDialog(allow_remote=False)
    elif kind == 'preferences':
        dialog = PreferencesDialog(Preferences())
    elif kind == 'time':
        dialog = DateTimeRangePicker(START, START + timedelta(hours=1))
    elif kind == 'export':
        dialog = CalendarChoiceDialog(CALENDARS, accept_text='Export')
    else:
        dialog = ImportDialog(CALENDARS)
    widgets.append(dialog)
    dialog.show()
    app.processEvents()
    buttons = dialog.buttons
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    accept = next(button for button in buttons.buttons() if buttons.buttonRole(button) == QDialogButtonBox.ButtonRole.AcceptRole)
    assert cancel.geometry().right() < accept.geometry().left()
    assert accept.objectName() == 'primary'
    assert cancel.objectName() != 'primary'
    assert abs(accept.geometry().bottom() - cancel.geometry().bottom()) <= 1
    apply = buttons.button(QDialogButtonBox.StandardButton.Apply)
    if apply:
        assert kind == 'preferences'
        assert apply.geometry().right() < cancel.geometry().left()
        assert apply.objectName() != 'primary'
    if kind == 'import':
        assert buttons.parentWidget().parentWidget() is dialog.preview_column


def test_temporary_picker_says_ok_and_closes_but_preferences_apply_stays_open(desktop):
    app, widgets = desktop
    picker = DateTimeRangePicker(START, START + timedelta(hours=1))
    widgets.append(picker)
    picker.show()
    app.processEvents()
    assert picker.buttons.button(QDialogButtonBox.StandardButton.Apply) is None
    assert picker.accept_button.text().replace('&', '') == 'OK'
    QTest.mouseClick(picker.accept_button, Qt.MouseButton.LeftButton)
    assert picker.result() == QDialog.DialogCode.Accepted
    assert not picker.isVisible()
    preferences = PreferencesDialog(Preferences())
    widgets.append(preferences)
    preferences.show()
    app.processEvents()
    QTest.mouseClick(preferences.buttons.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
    assert preferences.isVisible()
