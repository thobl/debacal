"""Sidebar month/year controls work independently of Qt's styled toolbar."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QDate, Qt, QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QCalendarWidget

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
        widget.month_selector.hidePopup()
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
    widget.month_selector.showPopup()
    view = widget.month_selector.view()
    index = view.model().index(2, 0)
    view.scrollTo(index)
    app.processEvents()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
    assert (widget.yearShown(), widget.monthShown()) == (2026, 3)
    assert widget.month_selector.currentData() == 3
    assert widget.selectedDate() == original
    assert changed.count() == 1 and clicked.count() == 0


def test_year_entry_and_previous_next_stay_in_sync_across_years(calendars):
    _, create = calendars
    widget = create()
    widget.setCurrentPage(2026, 1)
    widget.year_selector.setValue(2028)
    assert (widget.yearShown(), widget.monthShown()) == (2028, 1)
    QTest.mouseClick(widget.previous_button, Qt.MouseButton.LeftButton)
    assert (widget.yearShown(), widget.monthShown()) == (2027, 12)
    assert widget.year_selector.value() == 2027 and widget.month_selector.currentData() == 12
    QTest.mouseClick(widget.next_button, Qt.MouseButton.LeftButton)
    assert (widget.yearShown(), widget.monthShown()) == (2028, 1)
    widget.year_selector.setFocus()
    widget.year_selector.lineEdit().selectAll()
    QTest.keyClicks(widget.year_selector, '2032')
    QTest.keyClick(widget.year_selector, Qt.Key.Key_Return)
    assert widget.yearShown() == 2032


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
    assert widget.month_selector.currentText() == german.month_name(3, short=True)
    assert widget.firstDayOfWeek() == Qt.DayOfWeek.Sunday
    assert widget.verticalHeaderFormat() == QCalendarWidget.VerticalHeaderFormat.ISOWeekNumbers
    english = Preferences(locale_name='en_GB', show_week_numbers=False)
    widget.set_preferences(english)
    assert widget.calendar.locale().name() == 'en_GB'
    assert widget.month_selector.currentText() == english.month_name(3, short=True)
    assert widget.firstDayOfWeek() == Qt.DayOfWeek.Monday
    assert widget.verticalHeaderFormat() == QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    assert (widget.yearShown(), widget.monthShown()) == (2027, 3)


def test_unchanged_preferences_do_not_close_month_popup_or_emit_false_page_change(calendars):
    app, create = calendars
    widget = create()
    widget.setCurrentPage(2027, 3)
    changes = QSignalSpy(widget.currentPageChanged)
    widget.month_selector.showPopup()
    app.processEvents()
    assert widget.month_selector.view().isVisible()
    widget.set_preferences(widget.preferences)
    app.processEvents()
    assert widget.month_selector.view().isVisible()
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
            combo = window.mini.month_selector
            combo.setCurrentIndex(2)
            combo.showPopup()
            app.processEvents()
            QTest.keyClick(combo.view(), Qt.Key.Key_Down)
            assert combo.view().currentIndex().row() == 3
            QTest.keyClick(combo.view(), Qt.Key.Key_Return)
            assert window.mini.monthShown() == 4
        finally:
            window.mini.month_selector.hidePopup()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
