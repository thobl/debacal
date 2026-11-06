"""Sidebar month/year controls work independently of Qt's styled toolbar."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QDate, Qt, QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QCalendarWidget, QComboBox, QSpinBox

from debacal.mini_calendar import MiniCalendar
from debacal.preferences import Preferences


@pytest.fixture
def calendars():
    app = QApplication.instance() or QApplication([])
    widgets = []
    def create(preferences=None):
        widget = MiniCalendar(preferences or Preferences(locale_name='en_GB'))
        widgets.append(widget)
        widget.resize(260, 225)
        widget.show()
        widget.activateWindow()
        app.processEvents()
        return widget
    yield app, create
    for widget in widgets:
        widget.month_menu.hide()
        widget.year_popup.close()
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_month_popup_selects_a_month_without_changing_the_selected_date(calendars):
    app, create = calendars
    widget = create()
    original = QDate(2026, 11, 6)
    widget.setSelectedDate(original)
    clicked = QSignalSpy(widget.clicked)
    changed = QSignalSpy(widget.currentPageChanged)
    QTest.mouseClick(widget.month_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    action = widget.month_actions[2]
    QTest.mouseClick(widget.month_menu, Qt.MouseButton.LeftButton, pos=widget.month_menu.actionGeometry(action).center())
    assert (widget.yearShown(), widget.monthShown()) == (2026, 3)
    assert widget.month_button.text() == 'March'
    assert widget.selectedDate() == original
    assert changed.count() == 1 and clicked.count() == 0
    widget.show_month_menu()
    QTest.mouseClick(widget.month_menu, Qt.MouseButton.LeftButton,
                     pos=widget.month_menu.actionGeometry(action).center())
    assert action.isChecked() and changed.count() == 1


def test_year_entry_and_previous_next_stay_in_sync_across_years(calendars):
    app, create = calendars
    widget = create()
    widget.setCurrentPage(2026, 1)
    QTest.mouseClick(widget.year_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    field = widget.year_popup.year_input
    QTest.keyClicks(field, '2028')
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert (widget.yearShown(), widget.monthShown()) == (2028, 1)
    assert not widget.year_popup.isVisible()
    QTest.mouseClick(widget.previous_button, Qt.MouseButton.LeftButton)
    assert (widget.yearShown(), widget.monthShown()) == (2027, 12)
    assert widget.year_button.text() == '2027' and widget.month_button.text() == 'December'
    QTest.mouseClick(widget.next_button, Qt.MouseButton.LeftButton)
    assert (widget.yearShown(), widget.monthShown()) == (2028, 1)
    QTest.mouseClick(widget.year_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    QTest.keyClicks(field, '2032')
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert widget.yearShown() == 2032


def test_year_popup_mouse_grid_and_escape_keep_selected_date(calendars):
    app, create = calendars
    widget = create()
    original = QDate(2026, 3, 6)
    widget.setSelectedDate(original)
    QTest.mouseClick(widget.year_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    QTest.mouseClick(widget.year_popup.next_button, Qt.MouseButton.LeftButton)
    choice = next(button for button in widget.year_popup.year_buttons if button.text() == '2033')
    QTest.mouseClick(choice, Qt.MouseButton.LeftButton)
    assert widget.yearShown() == 2033 and widget.monthShown() == 3
    assert widget.selectedDate() == original
    QTest.mouseClick(widget.year_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    widget.year_popup.year_input.setText('0')
    QTest.keyClick(widget.year_popup.year_input, Qt.Key.Key_Return)
    assert widget.year_popup.isVisible() and widget.yearShown() == 2033
    QTest.keyClick(widget.year_popup.year_input, Qt.Key.Key_Escape)
    assert not widget.year_popup.isVisible() and widget.yearShown() == 2033


def test_centered_heading_uses_full_month_when_space_allows_without_visible_spin_controls(calendars):
    app, create = calendars
    widget = create()
    widget.resize(260, 210)
    widget.setCurrentPage(2026, 9)
    app.processEvents()
    assert widget.month_button.text() == 'September'
    assert widget.previous_button.x() < widget.month_button.x() < widget.year_button.x() < widget.next_button.x()
    middle = (widget.month_button.x() + widget.year_button.x() + widget.year_button.width()) / 2
    assert abs(middle - widget.width() / 2) <= 2
    assert not any(control.isVisible() for control in widget.findChildren(QSpinBox))
    assert not any(control.isVisible() for control in widget.findChildren(QComboBox))
    widget.resize(130, 210)
    app.processEvents()
    assert widget.month_button.text() == 'Sept'
    assert 'September' in widget.month_button.toolTip()


def test_date_cell_click_emits_standard_date_signal(calendars):
    app, create = calendars
    widget = create()
    widget.setSelectedDate(QDate(2028, 2, 28))
    app.processEvents()
    view = widget.calendar.findChild(QAbstractItemView)
    current = view.currentIndex()
    following = view.model().index(current.row(), current.column() + 1)
    assert following.data() == 29
    clicked = QSignalSpy(widget.clicked)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(following).center())
    assert widget.selectedDate() == QDate(2028, 2, 29)
    assert clicked.count() == 1 and clicked.at(0)[0] == QDate(2028, 2, 29)


def test_explicit_locale_week_start_and_week_numbers_follow_preferences(calendars):
    _, create = calendars
    german = Preferences(locale_name='de_DE', start_weekday=6, show_week_numbers=True)
    widget = create(german)
    widget.setCurrentPage(2027, 3)
    assert widget.calendar.locale().name() == 'de_DE'
    assert widget.month_button.text() == german.month_name(3)
    assert widget.firstDayOfWeek() == Qt.DayOfWeek.Sunday
    assert widget.verticalHeaderFormat() == QCalendarWidget.VerticalHeaderFormat.ISOWeekNumbers
    english = Preferences(locale_name='en_GB', show_week_numbers=False)
    widget.set_preferences(english)
    assert widget.calendar.locale().name() == 'en_GB'
    assert widget.month_button.text() == english.month_name(3)
    assert widget.firstDayOfWeek() == Qt.DayOfWeek.Monday
    assert widget.verticalHeaderFormat() == QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    assert (widget.yearShown(), widget.monthShown()) == (2027, 3)


def test_unchanged_preferences_do_not_close_month_popup_or_emit_false_page_change(calendars):
    app, create = calendars
    widget = create()
    widget.setCurrentPage(2027, 3)
    changes = QSignalSpy(widget.currentPageChanged)
    widget.show_month_menu()
    app.processEvents()
    assert widget.month_menu.isVisible()
    widget.set_preferences(widget.preferences)
    app.processEvents()
    assert widget.month_menu.isVisible()
    assert changes.count() == 0


def test_navigation_is_bounded_to_python_calendar_years(calendars):
    _, create = calendars
    widget = create()
    widget.setCurrentPage(1, 1)
    assert not widget.previous_button.isEnabled()
    widget.showPreviousMonth()
    assert (widget.yearShown(), widget.monthShown()) == (1, 1)
    widget.setCurrentPage(9999, 12)
    assert not widget.next_button.isEnabled()
    widget.showNextMonth()
    assert (widget.yearShown(), widget.monthShown()) == (9999, 12)


def test_main_window_shortcuts_do_not_intercept_month_popup_navigation(calendars):
    app, _ = calendars
    from debacal.store import Store
    from debacal.ui import MainWindow
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        try:
            window.show()
            window.activateWindow()
            app.processEvents()
            mini = window.mini
            mini.setCurrentPage(2026, 3)
            QTest.mouseClick(mini.month_button, Qt.MouseButton.LeftButton)
            app.processEvents()
            assert mini.month_menu.activeAction().data() == 3
            QTest.keyClick(mini.month_menu, Qt.Key.Key_Down)
            assert mini.month_menu.activeAction().data() == 4
            QTest.keyClick(mini.month_menu, Qt.Key.Key_Return)
            assert mini.monthShown() == 4
        finally:
            window.mini.month_menu.hide()
            window.mini.year_popup.close()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
