"""Compact sidebar calendar with explicit, keyboard-friendly month controls."""
from __future__ import annotations

from PySide6.QtCore import QDate, Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QCalendarWidget, QComboBox, QHBoxLayout, QSizePolicy, QSpinBox,
    QToolButton, QVBoxLayout, QWidget,
)

from .preferences import Preferences


class MiniCalendar(QWidget):
    """A small QCalendar-compatible surface without its styled popup toolbar."""
    clicked = Signal(QDate)
    activated = Signal(QDate)
    selectionChanged = Signal()
    currentPageChanged = Signal(int, int)

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self._shown_page = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.previous_button = QToolButton(self)
        self.previous_button.setText('‹')
        self.previous_button.setToolTip('Previous month')
        self.next_button = QToolButton(self)
        self.next_button.setText('›')
        self.next_button.setToolTip('Next month')
        self.month_selector = QComboBox(self)
        self.month_selector.setObjectName('miniMonthSelector')
        self.month_selector.setAccessibleName('Month')
        self.month_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.month_selector.setMinimumContentsLength(3)
        self.month_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.year_selector = QSpinBox(self)
        self.year_selector.setObjectName('miniYearSelector')
        self.year_selector.setAccessibleName('Year')
        self.year_selector.setRange(1, 9999)
        self.year_selector.setKeyboardTracking(False)
        self.year_selector.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.year_selector.setGroupSeparatorShown(False)
        row.addWidget(self.month_selector, 1)
        row.addWidget(self.year_selector)
        row.addWidget(self.previous_button)
        row.addWidget(self.next_button)
        outer.addLayout(row)
        self.calendar = QCalendarWidget(self)
        self.calendar.setNavigationBarVisible(False)
        self.calendar.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self.calendar.setGridVisible(False)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.calendar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        outer.addWidget(self.calendar, 1)
        self.calendar.clicked.connect(self.clicked.emit)
        self.calendar.activated.connect(self.activated.emit)
        self.calendar.selectionChanged.connect(self.selectionChanged.emit)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.previous_button.clicked.connect(self.showPreviousMonth)
        self.next_button.clicked.connect(self.showNextMonth)
        self.month_selector.currentIndexChanged.connect(self._choose_page)
        self.year_selector.valueChanged.connect(self._choose_page)
        self.set_preferences(self.preferences)
        self._page_changed(self.calendar.yearShown(), self.calendar.monthShown())

    def set_preferences(self, preferences):
        self.preferences = preferences
        locale = preferences.qt_locale()
        self.setLocale(locale)
        locale_changed = self.calendar.locale().name() != locale.name() or self.month_selector.count() != 12
        self.calendar.setLocale(locale)
        self.month_selector.setLocale(locale)
        self.year_selector.setLocale(locale)
        month = self.calendar.monthShown()
        if locale_changed:
            with QSignalBlocker(self.month_selector):
                self.month_selector.clear()
                for number in range(1, 13):
                    self.month_selector.addItem(preferences.month_name(number, short=True), number)
                self.month_selector.setCurrentIndex(month - 1)
        self.setFirstDayOfWeek(Qt.DayOfWeek(preferences.first_weekday + 1))
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.ISOWeekNumbers
                                    if preferences.show_week_numbers else QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        scale = preferences.font_scale / 100
        # Keep date cells usable at large fonts while remaining compact by default.
        self.setMinimumHeight(round(195 * scale))
        for button in (self.previous_button, self.next_button):
            button.setFixedWidth(round(23 * scale))
        self._page_changed(self.calendar.yearShown(), self.calendar.monthShown())

    def _choose_page(self, *_):
        month = self.month_selector.currentData()
        if month:
            self.calendar.setCurrentPage(self.year_selector.value(), month)

    def _page_changed(self, year, month):
        with QSignalBlocker(self.month_selector), QSignalBlocker(self.year_selector):
            self.month_selector.setCurrentIndex(month - 1)
            self.year_selector.setValue(year)
        self.previous_button.setEnabled((year, month) > (1, 1))
        self.next_button.setEnabled((year, month) < (9999, 12))
        if self._shown_page != (year, month):
            self._shown_page = (year, month)
            self.currentPageChanged.emit(year, month)

    def selectedDate(self):
        return self.calendar.selectedDate()

    def setSelectedDate(self, value):
        self.calendar.setSelectedDate(value)

    def firstDayOfWeek(self):
        return self.calendar.firstDayOfWeek()

    def setFirstDayOfWeek(self, day):
        self.calendar.setFirstDayOfWeek(day)

    def setCurrentPage(self, year, month):
        self.calendar.setCurrentPage(year, month)

    def monthShown(self):
        return self.calendar.monthShown()

    def yearShown(self):
        return self.calendar.yearShown()

    def showPreviousMonth(self):
        if self.previous_button.isEnabled():
            self.calendar.showPreviousMonth()

    def showNextMonth(self):
        if self.next_button.isEnabled():
            self.calendar.showNextMonth()

    def setGridVisible(self, visible):
        self.calendar.setGridVisible(visible)

    def setVerticalHeaderFormat(self, value):
        self.calendar.setVerticalHeaderFormat(value)

    def verticalHeaderFormat(self):
        return self.calendar.verticalHeaderFormat()

    def setHorizontalHeaderFormat(self, value):
        self.calendar.setHorizontalHeaderFormat(value)
