"""Calendar identity and button conventions are shared by the native dialogs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.buttons import action_button, icon_button
from debacal.calendar_choice import CalendarChoice
from debacal.calendar_choice_dialog import CalendarChoiceDialog
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


CALENDARS = [Calendar('work', 'Meetings', '#9166b2'), Calendar('home', 'Meetings', '#d8b136'),
             Calendar('feed', 'Subscription', '#b36952', kind='ics'),
             Calendar('locked', 'Read-only', '#5c8a93', read_only=True)]


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned = []
    yield app, owned
    for widget in reversed(owned):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_choice_uses_ids_and_source_colors_and_filters_writable_destinations(desktop):
    _, owned = desktop
    choice = CalendarChoice(CALENDARS, selected_id='home')
    owned.append(choice)
    assert [choice.itemData(index) for index in range(choice.count())] == ['work', 'home']
    assert choice.currentData() == 'home'
    assert choice.selected_calendar == CALENDARS[1]
    for index, calendar in enumerate(CALENDARS[:2]):
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            swatch = choice.itemIcon(index).pixmap(QSize(16, 16), mode).toImage()
            assert swatch.pixelColor(swatch.width() // 2, swatch.height() // 2) == QColor(calendar.color)


def test_readonly_export_choices_and_preference_refresh_preserve_selected_identity(desktop):
    _, owned = desktop
    choice = CalendarChoice(CALENDARS, selected_id='feed', writable_only=False)
    owned.append(choice)
    assert choice.count() == 4 and choice.currentData() == 'feed'
    spy = QSignalSpy(choice.currentIndexChanged)
    choice.set_preferences(Preferences(font_scale=140, appearance_mode='dark'))
    assert choice.currentData() == 'feed'
    assert choice.iconSize().width() == 22
    assert spy.count() == 0
    choice.set_calendars([CALENDARS[3], CALENDARS[0]])
    assert choice.currentData() == 'locked'
    assert spy.count() == 1


def test_choice_dialog_uses_normal_enter_and_cancel_behavior(desktop):
    app, owned = desktop
    dialog = CalendarChoiceDialog(CALENDARS, selected_id='home')
    owned.append(dialog)
    dialog.show()
    app.processEvents()
    QTest.keyClick(dialog.calendar, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_calendar.id == 'home'
    empty = CalendarChoiceDialog([])
    owned.append(empty)
    empty.save()
    assert empty.result() == QDialog.DialogCode.Rejected
    assert not empty.accept_button.isEnabled()


def test_export_picker_defaults_to_creation_calendar_and_allows_readonly_sources(desktop):
    _, owned = desktop
    with Store(':memory:') as store:
        for calendar in CALENDARS:
            store.save_calendar(calendar)
        store.set_default_calendar('home')
        window = MainWindow(store, enable_sync=False)
        owned.append(window)
        def choose(dialog):
            assert isinstance(dialog.calendar, CalendarChoice)
            assert dialog.calendar.currentData() == 'home'
            assert dialog.calendar.findData('feed') >= 0
            dialog.calendar.setCurrentIndex(dialog.calendar.findData('feed'))
            return QDialog.DialogCode.Accepted
        with patch.object(CalendarChoiceDialog, 'exec', choose):
            assert window.choose_calendar().id == 'feed'
        with patch.object(CalendarChoiceDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            assert window.choose_calendar() is None
        window.close()


def test_shared_action_and_icon_buttons_keep_accessibility_and_focus_conventions(desktop):
    _, owned = desktop
    actions = []
    plain = action_button('Apply', lambda: actions.append('apply'))
    image = icon_button('today', 'Go to today', lambda: actions.append('today'), preferences=Preferences(font_scale=140))
    owned.extend([plain, image])
    plain.click()
    image.click()
    assert actions == ['apply', 'today']
    assert not plain.autoDefault() and not image.autoDefault()
    assert image.accessibleName() == image.toolTip() == 'Go to today'
    assert image.property('iconOnly') and image.iconSize().width() == 25
    assert image.width() == 42
