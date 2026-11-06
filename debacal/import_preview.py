"""Choose imported dates without editing the calendar snapshot."""
from __future__ import annotations

from datetime import date, timedelta
from PySide6.QtCore import Qt, QSize, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .calendar_widgets import MonthCanvas, display_scale, event_tooltip
from .icons import icon
from .models import Calendar
from .preferences import Preferences
from .timeutil import local_zone, now


class _ImportMonth(MonthCanvas):
    occurrence_toggled = Signal(object)
    date_toggled = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_creation_hints = False
        self._included_ids = None
        self._starting_by_day = {}
        self._pending_click = None
        self._focus_day = None
        self._focus_event = None
        self._keyboard_focus = False

    def set_preferences(self, preferences):
        super().set_preferences(preferences)
        self.setMinimumHeight(round(280 * display_scale(self)))

    def month_row_metrics(self):
        scale = display_scale(self)
        return 20 * scale, 18 * scale, 16 * scale, 2 * scale

    def confirmation_event(self, _event_id):
        return None

    def set_data(self, start, month, events, calendars):
        super().set_data(start, month, events, calendars)
        self._starting_by_day = {}
        for item in events:
            day = item.start.date() if item.all_day else item.start.astimezone(local_zone()).date()
            self._starting_by_day.setdefault(day, []).append(item)
        if self._focus_event is not None:
            self._focus_event = next((item for item in events if item.id == self._focus_event.id), None)
        if self._focus_day is not None and not 0 <= (self._focus_day - start).days < 42:
            self._focus_day = None

    def set_import_selection(self, included_ids):
        self._included_ids = None if included_ids is None else set(included_ids)
        self.update()

    def is_included(self, item):
        return self._included_ids is None or item.id in self._included_ids

    def day_rect(self, day):
        offset = (day - self.start).days
        if not 0 <= offset < 42:
            return QRectF()
        width = (self.width() - self.week_gutter) / 7
        height = (self.height() - self.header_height) / 6
        return QRectF(self.week_gutter + offset % 7 * width,
                      self.header_height + offset // 7 * height, width, height)

    def date_check_state(self, day):
        events = self._starting_by_day.get(day, ())
        if not events:
            return None
        selected = sum(self.is_included(item) for item in events)
        return (Qt.CheckState.Checked if selected == len(events) else
                Qt.CheckState.PartiallyChecked if selected else Qt.CheckState.Unchecked)

    def _paint_month_event(self, painter, rect, item, **options):
        included = self.is_included(item)
        painter.save()
        if not included:
            painter.setOpacity(.32)
        super()._paint_month_event(painter, rect, item, **options)
        painter.restore()
        if not included:
            calendar = self.calendars.get(item.calendar_id)
            ink = QColor(calendar.color).darker(135) if calendar else QColor('#65735f')
            ink.setAlpha(115)
            painter.setPen(QPen(ink, 1))
            painter.drawLine(QPointF(rect.left() + 5, rect.center().y()), QPointF(rect.right() - 5, rect.center().y()))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = display_scale(self)
        size = 10 * scale
        for offset in range(42):
            day = self.start + timedelta(days=offset)
            state = self.date_check_state(day)
            if state is None:
                continue
            cell = self.day_rect(day)
            box = QRectF(cell.right() - size - 5 * scale, cell.top() + 4 * scale, size, size)
            painter.setPen(QPen(QColor('#97a88f'), 1))
            painter.setBrush(QColor('#fafbf8'))
            painter.drawRoundedRect(box, 2, 2)
            painter.setPen(QPen(QColor('#416c51'), max(1.2, 1.2 * scale)))
            if state == Qt.CheckState.Checked:
                painter.drawLine(QPointF(box.left() + size * .18, box.top() + size * .51),
                                 QPointF(box.left() + size * .42, box.top() + size * .75))
                painter.drawLine(QPointF(box.left() + size * .42, box.top() + size * .75),
                                 QPointF(box.left() + size * .84, box.top() + size * .22))
            elif state == Qt.CheckState.PartiallyChecked:
                painter.drawLine(QPointF(box.left() + size * .2, box.center().y()),
                                 QPointF(box.right() - size * .2, box.center().y()))
        if self.hasFocus() and self._keyboard_focus and self._focus_day is not None:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#7c9273'), 1, Qt.PenStyle.DotLine))
            painter.drawRect(self.day_rect(self._focus_day).adjusted(2, 2, -2, -2))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._keyboard_focus = False
        hit = self.hit(event.position())
        self._focus_day = self.day_at(event.position())
        self._focus_event = hit[1] if hit else None
        self._pending_click = event.position(), self._focus_event, self._focus_day
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        # The first click already changed inclusion. A double-click must not
        # reverse it again or open an editor.
        self._pending_click = None
        event.accept()

    def mouseReleaseEvent(self, event):
        pending, self._pending_click = self._pending_click, None
        if (event.button() == Qt.MouseButton.LeftButton and pending is not None
                and (event.position() - pending[0]).manhattanLength() < 5
                and self.rect().contains(event.position().toPoint())):
            _point, item, day = pending
            if item is not None:
                self.occurrence_toggled.emit(item)
            elif day is not None:
                self.date_toggled.emit(day)
        event.accept()

    def mouseMoveEvent(self, event):
        hit = self.hit(event.position())
        if hit:
            tooltip = event_tooltip(hit[1], self.preferences, self.calendars.get(hit[1].calendar_id))
            if not self.is_included(hit[1]):
                tooltip = tooltip.removesuffix('</qt>') + '<br>Excluded from import</qt>'
            self.setToolTip(tooltip)
        else:
            day = self.day_at(event.position())
            week = self.week_at(event.position())
            self.setToolTip(self.preferences.format_date(day, weekday=True) if day is not None else
                            'Week ' + str(self.preferences.week_number(week)) if week is not None else '')
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit or self.day_at(event.position()) else Qt.CursorShape.ArrowCursor)

    def contextMenuEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        if not event.modifiers():
            shifts = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
            if event.key() in shifts:
                current = self._focus_day or next(self.start + timedelta(days=index) for index in range(42)
                                                 if (self.start + timedelta(days=index)).month == self.month)
                target = current + timedelta(days=shifts[event.key()])
                if 0 <= (target - self.start).days < 42:
                    self._focus_day = target
                self._focus_event = None
                self._keyboard_focus = True
                self.update()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._focus_event is not None:
                    self.occurrence_toggled.emit(self._focus_event)
                else:
                    day = self._focus_day or next(self.start + timedelta(days=index) for index in range(42)
                                                 if (self.start + timedelta(days=index)).month == self.month)
                    self._focus_day = day
                    self.date_toggled.emit(day)
                self._keyboard_focus = True
                self.update()
                event.accept()
                return
        super().keyPressEvent(event)


class ImportPreview(QWidget):
    """Display already-expanded events; the owner loads each requested month."""
    month_requested = Signal(object)
    occurrence_toggled = Signal(object)
    date_toggled = Signal(object)
    selection_all_requested = Signal(bool)
    selection_month_requested = Signal(object)

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
        self.all_button = QPushButton('All')
        self.all_button.setToolTip('Select all dates from the import')
        self.all_button.clicked.connect(lambda: self.selection_all_requested.emit(True))
        toolbar.addWidget(self.all_button)
        self.none_button = QPushButton('None')
        self.none_button.setToolTip('Deselect all dates from the import')
        self.none_button.clicked.connect(lambda: self.selection_all_requested.emit(False))
        toolbar.addWidget(self.none_button)
        self.month_button = QPushButton('This month')
        self.month_button.setToolTip('Select only events starting in this month')
        self.month_button.clicked.connect(lambda: self.selection_month_requested.emit(self.current_month))
        toolbar.addWidget(self.month_button)
        self.first_button = QPushButton('First event')
        self.first_button.setToolTip('Show the first imported event')
        self.first_button.clicked.connect(self.show_first_date)
        self.first_button.setEnabled(False)
        toolbar.addWidget(self.first_button)
        outer.addLayout(toolbar)
        self.canvas = _ImportMonth(self)
        self.canvas.occurrence_toggled.connect(self.occurrence_toggled)
        self.canvas.date_toggled.connect(self.date_toggled)
        outer.addWidget(self.canvas, 1)
        self.message = QLabel()
        self.message.setProperty('muted', True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
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
        self._update_heading()

    def set_selection(self, included_ids):
        """Set inclusion for this page; None displays every occurrence included."""
        self.canvas.set_import_selection(included_ids)

    def set_first_date(self, day):
        self._first_date = day
        self.first_button.setEnabled(day is not None)

    def set_loading(self, loading):
        self._loading = bool(loading)
        self.canvas.setEnabled(not self._loading)
        for button in (self.all_button, self.none_button, self.month_button):
            button.setEnabled(not self._loading)
        if loading:
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
