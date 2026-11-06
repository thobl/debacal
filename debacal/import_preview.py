"""Read-only calendar rendering for an import that has not been committed."""
from __future__ import annotations

from datetime import date, timedelta
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from .calendar_widgets import MonthCanvas, display_scale, event_tooltip
from .icons import icon
from .models import Calendar
from .preferences import Preferences
from .timeutil import local_zone, now


class _ImportMonth(MonthCanvas):
    event_inspected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_creation_hints = False

    def set_preferences(self, preferences):
        super().set_preferences(preferences)
        self.setMinimumHeight(round(280 * display_scale(self)))

    def month_row_metrics(self):
        scale = display_scale(self)
        return 20 * scale, 18 * scale, 16 * scale, 2 * scale

    def confirmation_event(self, _event_id):
        return None

    def inspect_event(self, item):
        self.selected_id = item.id
        self.set_selection({item.id})
        self.event_inspected.emit(item)

    def events_on(self, day):
        days = [self.start + timedelta(days=index) for index in range(42)]
        _bounds, buckets = self.day_buckets(days, date_events=True)
        offset = (day - self.start).days
        return buckets[offset] if 0 <= offset < len(buckets) else []

    def _show_day(self, day, global_point):
        events = self.events_on(day)
        if not events:
            return
        if len(events) == 1:
            self.inspect_event(events[0])
            return
        menu = QMenu(self)
        for item in events[:100]:
            label = ('All day' if item.all_day else self.preferences.format_time(item.start.astimezone(local_zone())))
            action = menu.addAction((label + ' · ' + item.title).replace('&', '&&'))
            action.triggered.connect(lambda _checked=False, value=item: self.inspect_event(value))
        if len(events) > 100:
            menu.addAction(f'{len(events) - 100} more events').setEnabled(False)
        try:
            menu.exec(global_point)
        finally:
            menu.deleteLater()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        hit = self.hit(event.position())
        if hit:
            self.inspect_event(hit[1])
        else:
            day = self.day_at(event.position())
            if day is not None:
                self._show_day(day, event.globalPosition().toPoint())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        hit = self.hit(event.position())
        if hit:
            self.setToolTip(event_tooltip(hit[1], self.preferences, self.calendars.get(hit[1].calendar_id)))
        else:
            day = self.day_at(event.position())
            week = self.week_at(event.position())
            self.setToolTip(self.preferences.format_date(day, weekday=True) if day is not None else
                            'Week ' + str(self.preferences.week_number(week)) if week is not None else '')
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def contextMenuEvent(self, event):
        day = self.day_at(event.pos())
        if day is not None:
            self._show_day(day, event.globalPos())
        event.accept()


class ImportPreview(QWidget):
    """Display already-expanded events; the owner loads each requested month."""
    month_requested = Signal(object)

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self.current_month = now().date().replace(day=1)
        self._first_date = None
        self._calendars = []
        self._loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.previous_button = self._button('previous', 'Previous month', lambda: self.navigate_month(-1), toolbar)
        self.today_button = self._button('today', 'Show this month', lambda: self.show_month(now().date()), toolbar)
        self.next_button = self._button('next', 'Next month', lambda: self.navigate_month(1), toolbar)
        self.month_label = QLabel()
        self.month_label.setObjectName('section')
        toolbar.addSpacing(6)
        toolbar.addWidget(self.month_label, 1)
        self.first_button = QPushButton('First event')
        self.first_button.setToolTip('Show the first imported event')
        self.first_button.clicked.connect(self.show_first_date)
        self.first_button.setEnabled(False)
        toolbar.addWidget(self.first_button)
        outer.addLayout(toolbar)
        self.canvas = _ImportMonth(self)
        self.canvas.event_inspected.connect(self._inspect_event)
        outer.addWidget(self.canvas, 1)
        self.message = QLabel()
        self.message.setProperty('muted', True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(0)
        self.details.hide()
        outer.addWidget(self.details)
        self.set_preferences(self.preferences)
        self.set_events(self.current_month, (), ())

    def _button(self, symbol, title, action, layout):
        button = QPushButton()
        button.setIcon(icon(symbol))
        button.setIconSize(QSize(18, 18))
        button.setProperty('iconOnly', True)
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.clicked.connect(lambda _checked=False: action())
        layout.addWidget(button)
        return button

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.canvas.set_preferences(preferences)
        size = max(26, round(30 * preferences.font_scale / 100))
        for button in (self.previous_button, self.today_button, self.next_button):
            button.setFixedSize(size, size)
        self._update_heading()

    def _update_heading(self):
        self.month_label.setText(self.preferences.format_month(self.current_month))
        self.previous_button.setEnabled(self.current_month > date(2, 1, 1))
        self.next_button.setEnabled(self.current_month < date(9998, 12, 1))

    def set_events(self, month, events, calendars):
        self.current_month = month.replace(day=1)
        self._calendars = [calendars] if isinstance(calendars, Calendar) else list(calendars)
        self.canvas.selected_id = None
        self.canvas.set_selection(())
        self.canvas.set_data(self.preferences.week_start(self.current_month), self.current_month.month,
                             list(events), self._calendars)
        self.details.clear()
        self.details.hide()
        self._update_heading()

    def set_first_date(self, day):
        self._first_date = day
        self.first_button.setEnabled(day is not None)

    def set_loading(self, loading):
        self._loading = bool(loading)
        self.canvas.setEnabled(not self._loading)
        if loading:
            self.details.clear()
            self.details.hide()
            self.set_message('Preparing preview…')
        else:
            self.set_message('')

    def set_message(self, message):
        self.message.setText(message)

    def set_error(self, message):
        self.set_loading(False)
        self.set_events(self.current_month, (), self._calendars)
        self.set_message(message)

    def show_month(self, day):
        month = day.replace(day=1)
        if not date(2, 1, 1) <= month <= date(9998, 12, 1):
            return
        self.set_events(month, (), self._calendars)
        self.month_requested.emit(month)

    def navigate_month(self, delta):
        value = self.current_month.year * 12 + self.current_month.month - 1 + delta
        year, month = divmod(value, 12)
        if 2 <= year <= 9998:
            self.show_month(date(year, month + 1, 1))

    def show_first_date(self):
        if self._first_date is not None:
            self.show_month(self._first_date)

    def _inspect_event(self, event):
        calendar = self.canvas.calendars.get(event.calendar_id)
        self.details.setText(event_tooltip(event, self.preferences, calendar))
        self.details.setMaximumHeight(round(72 * self.preferences.font_scale / 100))
        self.details.show()
