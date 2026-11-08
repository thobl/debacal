"""Compact sidebar calendar with explicit, keyboard-friendly month controls."""
from __future__ import annotations

from PySide6.QtCore import QDate, Qt, Signal, QPoint
from PySide6.QtGui import QIntValidator, QActionGroup, QColor, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QApplication, QMenu, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .preferences import Preferences
from .theme import corner_radius, scheme_for
from .calendar_appearance import CalendarDateGrid, weekend_text, weekend_fill
from .button_focus import install_button_focus


class _YearPopup(QFrame):
    year_chosen = Signal(int)

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('miniYearPopup')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.set_preferences(parent.preferences)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(7)
        self.year_input = QLineEdit(self)
        self.year_input.setAccessibleName('Year')
        self.year_input.setPlaceholderText('Year')
        self.year_input.setValidator(QIntValidator(1, 9999, self))
        self.year_input.setMaxLength(4)
        self.year_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.year_input.returnPressed.connect(self._submit)
        outer.addWidget(self.year_input)
        row = QHBoxLayout()
        self.previous_button = QToolButton(self)
        self.previous_button.setText('‹')
        self.previous_button.setToolTip('Earlier years')
        self.next_button = QToolButton(self)
        self.next_button.setText('›')
        self.next_button.setToolTip('Later years')
        self.range_label = QLabel(self)
        self.range_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.previous_button)
        row.addWidget(self.range_label, 1)
        row.addWidget(self.next_button)
        outer.addLayout(row)
        grid = QGridLayout()
        grid.setSpacing(3)
        self.year_buttons = []
        for index in range(12):
            button = QPushButton(self)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, offset=index: self.choose(self._first_year + offset))
            self.year_buttons.append(button)
            grid.addWidget(button, index // 3, index % 3)
        outer.addLayout(grid)
        self.previous_button.clicked.connect(lambda: self._show_years(self._first_year - 12))
        self.next_button.clicked.connect(lambda: self._show_years(self._first_year + 12))
        self._year, self._first_year = 2026, 2021

    def set_preferences(self, preferences):
        colors = scheme_for(preferences)
        self.setStyleSheet(f'''
            QFrame#miniYearPopup {{ background: {colors.panel}; color: {colors.ink}; border: 1px solid {colors.line}; border-radius: {corner_radius(preferences, 6)}px; }}
            QFrame#miniYearPopup QPushButton {{ padding: 5px 8px; border: 1px solid transparent; border-radius: {corner_radius(preferences, 4)}px; }}
            QFrame#miniYearPopup QPushButton[keyboardFocus="true"]:focus {{ border-color: {colors.focus}; }}
            QFrame#miniYearPopup QPushButton:hover {{ background: {colors.control_surface}; }}
            QFrame#miniYearPopup QPushButton:checked {{ background: {colors.selection}; color: {colors.ink}; font-weight: bold; }}
            QFrame#miniYearPopup QToolButton {{ border: 1px solid transparent; background: transparent; padding: 3px; border-radius: {corner_radius(preferences, 4)}px; }}
            QFrame#miniYearPopup QToolButton[keyboardFocus="true"]:focus {{ border-color: {colors.focus}; }}
            QFrame#miniYearPopup QToolButton:hover {{ background: {colors.control_surface}; }}
        ''')

    def prepare(self, year, preferences):
        self._year = year
        self.set_preferences(preferences)
        self.setLocale(preferences.qt_locale())
        self.year_input.validator().setLocale(preferences.qt_locale())
        self.year_input.setText(str(year))
        scale = preferences.font_scale / 100
        for button in self.year_buttons:
            button.setMinimumWidth(round(43 * scale))
        self._show_years(year - 5)
        self.adjustSize()

    def _show_years(self, first):
        self._first_year = max(1, min(9988, first))
        self.range_label.setText(f'{self._first_year}–{self._first_year + 11}')
        for offset, button in enumerate(self.year_buttons):
            year = self._first_year + offset
            button.setText(str(year))
            button.setChecked(year == self._year)
        self.previous_button.setEnabled(self._first_year > 1)
        self.next_button.setEnabled(self._first_year < 9988)

    def _submit(self):
        if self.year_input.hasAcceptableInput():
            self.choose(int(self.year_input.text()))

    def choose(self, year):
        self.year_chosen.emit(year)
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
        else:
            super().keyPressEvent(event)


class MiniCalendar(QWidget):
    """A quiet calendar heading with a month menu and year chooser on demand."""
    clicked = Signal(QDate)
    activated = Signal(QDate)
    selectionChanged = Signal()
    currentPageChanged = Signal(int, int)

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        install_button_focus(QApplication.instance())
        self.preferences = preferences or Preferences()
        self._shown_page = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.previous_button = QToolButton(self)
        self.previous_button.setText('‹')
        self.previous_button.setToolTip('Previous month')
        self.next_button = QToolButton(self)
        self.next_button.setText('›')
        self.next_button.setToolTip('Next month')
        row.addWidget(self.previous_button)
        row.addStretch(1)
        heading = QHBoxLayout()
        heading.setSpacing(0)
        self.month_button = QToolButton(self)
        self.month_button.setAccessibleName('Choose month')
        self.year_button = QToolButton(self)
        self.year_button.setAccessibleName('Choose year')
        heading.addWidget(self.month_button)
        heading.addWidget(self.year_button)
        row.addLayout(heading)
        row.addStretch(1)
        row.addWidget(self.next_button)
        for button in (self.previous_button, self.next_button, self.month_button, self.year_button):
            button.setAutoRaise(True)
        for button in (self.month_button, self.year_button):
            font = button.font()
            font.setBold(True)
            button.setFont(font)
        outer.addLayout(row)
        self.month_menu = QMenu(self)
        self.month_group = QActionGroup(self)
        self.month_group.setExclusive(True)
        self.month_actions = []
        self._month_locale = None
        self.year_popup = _YearPopup(self)
        self.month_button.clicked.connect(self.show_month_menu)
        self.year_button.clicked.connect(self.show_year_popup)
        self.year_popup.year_chosen.connect(lambda year: self.setCurrentPage(year, self.monthShown()))
        self.calendar = CalendarDateGrid(self.preferences, self, surface_role='sidebar')
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
        self.set_preferences(self.preferences)
        self._page_changed(self.calendar.yearShown(), self.calendar.monthShown())

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.calendar.preferences = preferences
        locale = preferences.qt_locale()
        self.setLocale(locale)
        self.calendar.setLocale(locale)
        colors = scheme_for(preferences)
        surface = getattr(colors, self.calendar.surface_role)
        scale = preferences.font_scale / 100
        self.calendar.setStyleSheet(f'''
            QCalendarWidget, QCalendarWidget QWidget {{ background: {surface}; color: {colors.ink}; font-size: {12 * scale:g}px; }}
            QCalendarWidget QAbstractItemView {{
                background: {surface}; alternate-background-color: {surface}; color: {colors.ink};
                selection-background-color: {colors.selection}; selection-color: {colors.ink};
                border: none; outline: none;
            }}
        ''')
        palette = self.calendar.palette()
        for role in (QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase, QPalette.ColorRole.Window):
            palette.setColor(role, QColor(surface))
        palette.setColor(QPalette.ColorRole.Text, QColor(colors.ink))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(colors.selection))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors.ink))
        self.calendar.setPalette(palette)
        for view in self.calendar.findChildren(QFrame):
            if hasattr(view, 'viewport'):
                view.setPalette(palette)
                view.viewport().setPalette(palette)
                view.viewport().setAutoFillBackground(True)
        for day in (Qt.DayOfWeek.Monday, Qt.DayOfWeek.Tuesday, Qt.DayOfWeek.Wednesday,
                    Qt.DayOfWeek.Thursday, Qt.DayOfWeek.Friday, Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            formatting = QTextCharFormat()
            formatting.setForeground(QColor(weekend_text(day.value - 1, preferences, colors)))
            formatting.setBackground(QColor(weekend_fill(day.value - 1, preferences, colors) or surface))
            self.calendar.setWeekdayTextFormat(day, formatting)
        self.calendar.updateCells()
        for button in (self.previous_button, self.next_button, self.month_button, self.year_button):
            button.setStyleSheet(f'QToolButton {{ border: 1px solid transparent; background: transparent; color: {colors.ink}; '
                                f'padding: 2px; border-radius: {corner_radius(preferences, 4)}px; }} '
                                f'QToolButton[keyboardFocus="true"]:focus {{ border-color: {colors.focus}; }} '
                                f'QToolButton:hover {{ background: {colors.control_surface}; }} '
                                f'QToolButton:pressed {{ background: {colors.selection}; }}')
        self.year_popup.set_preferences(preferences)
        if self._month_locale != locale.name():
            self.month_menu.clear()
            self.month_actions = []
            for number in range(1, 13):
                action = self.month_menu.addAction(preferences.month_name(number))
                action.setData(number)
                action.setCheckable(True)
                self.month_group.addAction(action)
                action.triggered.connect(lambda _checked=False, month=number: self.setCurrentPage(self.yearShown(), month))
                self.month_actions.append(action)
            self._month_locale = locale.name()
        self.setFirstDayOfWeek(Qt.DayOfWeek(preferences.first_weekday + 1))
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.ISOWeekNumbers
                                    if preferences.show_week_numbers else QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        # Keep date cells usable at large fonts while remaining compact by default.
        self.setMinimumHeight(round(190 * scale))
        for button in (self.previous_button, self.next_button):
            button.setFixedWidth(round(22 * scale))
        for button in (self.previous_button, self.next_button, self.month_button, self.year_button):
            button.setFixedHeight(round(25 * scale))
        self._page_changed(self.calendar.yearShown(), self.calendar.monthShown())

    def _update_heading(self):
        if not hasattr(self, 'calendar'):
            return
        month, year = self.monthShown(), self.yearShown()
        scale = self.preferences.font_scale / 100
        padding = round(8 * scale)
        year_width = self.year_button.fontMetrics().horizontalAdvance(str(year)) + padding
        self.year_button.setText(str(year))
        self.year_button.setFixedWidth(year_width)
        available = max(1, self.width() - self.previous_button.width() - self.next_button.width() - year_width - padding)
        full = self.preferences.month_name(month)
        short = self.preferences.month_name(month, short=True)
        metrics = self.month_button.fontMetrics()
        label = full if metrics.horizontalAdvance(full) + padding <= available else short
        self.month_button.setText(label)
        self.month_button.setFixedWidth(min(available, metrics.horizontalAdvance(label) + padding))
        self.month_button.setToolTip(full + ' · choose month')
        self.year_button.setToolTip(str(year) + ' · choose year')

    def _page_changed(self, year, month):
        self._update_heading()
        for action in self.month_actions:
            action.setChecked(action.data() == month)
        self.previous_button.setEnabled((year, month) > (1, 1))
        self.next_button.setEnabled((year, month) < (9999, 12))
        if self._shown_page != (year, month):
            self._shown_page = (year, month)
            self.currentPageChanged.emit(year, month)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_heading()

    def show_month_menu(self):
        self.month_menu.setActiveAction(self.month_actions[self.monthShown() - 1])
        self.month_menu.popup(self.month_button.mapToGlobal(QPoint(0, self.month_button.height())))

    def show_year_popup(self):
        self.year_popup.prepare(self.yearShown(), self.preferences)
        point = self.year_button.mapToGlobal(QPoint(0, self.year_button.height()))
        point.setX(self.mapToGlobal(QPoint((self.width() - self.year_popup.width()) // 2, 0)).x())
        bounds = self.screen().availableGeometry()
        point.setX(max(bounds.left(), min(point.x(), bounds.right() - self.year_popup.width() + 1)))
        if point.y() + self.year_popup.height() > bounds.bottom():
            point.setY(self.year_button.mapToGlobal(QPoint(0, 0)).y() - self.year_popup.height())
        point.setY(max(bounds.top(), min(point.y(), bounds.bottom() - self.year_popup.height() + 1)))
        self.year_popup.move(point)
        self.year_popup.show()
        self.year_popup.year_input.setFocus()
        self.year_popup.year_input.selectAll()

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
