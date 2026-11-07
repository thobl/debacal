"""Transactional, native date and time-range picker for the third editor study."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import math

from PySide6.QtCore import QDate, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCalendarWidget, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from debacal.appearance import stylesheet, widget_palette
from debacal.button_focus import install_button_focus
from debacal.calendar_widgets import calendar_gutter, text
from debacal.event_style import event_colors
from debacal.icons import icon
from debacal.mini_calendar import MiniCalendar
from debacal.preferences import Preferences
from debacal.theme import scheme_for


def _validate(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Choose a date and time with a timezone.')
    returned = value.astimezone(timezone.utc).astimezone(value.tzinfo)
    if returned.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError('This local time does not exist because the clocks change. Choose another time.')
    return value


def _duration(start, end):
    wall = end.replace(tzinfo=None) - start.replace(tzinfo=None)
    return wall if wall > timedelta(0) else end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


def _clock_on(day, previous):
    return _validate(datetime.combine(day, previous.timetz()))


class DateTimeRangePicker(QDialog):
    """One small picker shared by primary Start, End and additional occurrences.

    Draft edits remain local until Apply. ``result_start`` and ``result_end``
    keep their original values after Cancel. Date selection changes the opened
    endpoint; a same-day strip can edit either endpoint. A multi-day strip has
    only the real endpoint on its displayed day and explicit continuation cues.
    """
    def __init__(self, start, end, *, endpoint='start', preferences=None,
                 calendar_color='#587c67', parent=None):
        super().__init__(parent)
        if endpoint not in ('start', 'end'):
            raise ValueError('Choose the start or end picker.')
        _validate(start)
        _validate(end)
        if end.timestamp() <= start.timestamp():
            raise ValueError('The end must be after the start.')
        self.preferences = preferences or Preferences()
        self.endpoint = endpoint
        self.calendar_color = calendar_color
        self.zone = start.tzinfo
        self.draft_start, self.draft_end = start, end.astimezone(self.zone)
        self.result_start, self.result_end = start, end
        self._updating = False
        self.full_day = False
        self.setWindowTitle('Start date and time' if endpoint == 'start' else 'End date and time')
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences))
        self.setLocale(self.preferences.qt_locale())
        install_button_focus(QApplication.instance())
        scale = self.preferences.font_scale / 100
        self.setMinimumWidth(round(490 * scale))
        self.resize(round(520 * scale), round(405 * scale))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 13)
        outer.setSpacing(11)
        heading = QHBoxLayout()
        self.day_label = QLabel()
        self.day_label.setObjectName('sidebarSectionHeading')
        heading.addWidget(self.day_label, 1)
        self.full_day_button = QPushButton()
        self.full_day_button.setProperty('iconOnly', True)
        self.full_day_button.setIcon(icon('expand', scheme_for(self.preferences).accent))
        self.full_day_button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        self.full_day_button.setToolTip('Show all 24 hours')
        self.full_day_button.setAccessibleName('Show all 24 hours')
        self.full_day_button.setCheckable(True)
        self.full_day_button.toggled.connect(self.set_full_day)
        heading.addWidget(self.full_day_button)
        outer.addLayout(heading)
        visual = QHBoxLayout()
        visual.setSpacing(15)
        month_preferences = replace(self.preferences, show_week_numbers=False)
        self.month = MiniCalendar(month_preferences)
        self.month.calendar.surface_role = 'background'
        self.month.set_preferences(month_preferences)
        self.month.calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.month.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.calendar = self.month.calendar
        self.calendar.selectionChanged.connect(self.choose_selected_date)
        visual.addWidget(self.month, 1)
        self.strip = _RangeStrip(self)
        visual.addWidget(self.strip)
        outer.addLayout(visual, 1)
        self.summary = QLabel()
        self.summary.setProperty('muted', True)
        outer.addWidget(self.summary)
        self.error = QLabel()
        self.error.setProperty('error', True)
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.refresh()

    @property
    def view_day(self):
        return (self.draft_start if self.endpoint == 'start' else self.draft_end).date()

    @property
    def same_day(self):
        return self.draft_start.date() == self.draft_end.date()

    def set_full_day(self, enabled):
        self.full_day = bool(enabled)
        self.strip.refresh()
        self.full_day_button.setToolTip('Fit event hours' if enabled else 'Show all 24 hours')

    def refresh(self):
        self._updating = True
        try:
            day = self.view_day
            date = QDate(day.year, day.month, day.day)
            self.calendar.setSelectedDate(date)
            self.calendar.setCurrentPage(day.year, day.month)
            self.day_label.setText(self.preferences.format_date(day, weekday=True))
            first = self.preferences.format_time(self.draft_start)
            last = self.preferences.format_time(self.draft_end)
            if not self.same_day:
                first = self.preferences.format_date(self.draft_start.date()) + ' · ' + first
                last = self.preferences.format_date(self.draft_end.date()) + ' · ' + last
            length = _duration(self.draft_start, self.draft_end)
            minutes = int(length.total_seconds() // 60)
            days, remainder = divmod(minutes, 1440)
            hours, remainder = divmod(remainder, 60)
            duration = ' '.join(part for part in (f'{days}d' if days else '', f'{hours}h' if hours else '',
                                                f'{remainder}min' if remainder else '') if part)
            self.summary.setText(first + ' — ' + last + ('  ·  ' + duration if duration else ''))
            self.error.hide()
            self.strip.refresh()
        finally:
            self._updating = False

    def set_range(self, start, end):
        """Validate before replacing either endpoint; invalid drags lose no data."""
        try:
            _validate(start)
            _validate(end)
            if end.timestamp() <= start.timestamp():
                raise ValueError('The end must be after the start.')
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False
        self.draft_start, self.draft_end = start.astimezone(self.zone), end.astimezone(self.zone)
        self.refresh()
        return True

    def set_start(self, value, *, preserve_duration=True):
        try:
            if preserve_duration:
                wall = self.draft_end.replace(tzinfo=None) - self.draft_start.replace(tzinfo=None)
                if wall > timedelta(0):
                    end = (value.replace(tzinfo=None) + wall).replace(tzinfo=self.zone, fold=self.draft_end.fold)
                else:
                    seconds = self.draft_end.timestamp() - self.draft_start.timestamp()
                    end = datetime.fromtimestamp(value.timestamp() + seconds, self.zone)
            else:
                end = self.draft_end
            return self.set_range(value, end)
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False

    def set_end(self, value):
        return self.set_range(self.draft_start, value)

    def choose_date(self, day):
        try:
            if hasattr(day, 'toPython'):
                day = day.toPython()
            previous = self.draft_start if self.endpoint == 'start' else self.draft_end
            value = _clock_on(day, previous)
            success = self.set_start(value) if self.endpoint == 'start' else self.set_end(value)
            if not success:
                self._restore_calendar()
            return success
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            self._restore_calendar()
            return False

    def _restore_calendar(self):
        self._updating = True
        day = self.view_day
        self.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
        self._updating = False

    def choose_selected_date(self):
        if not self._updating:
            self.choose_date(self.calendar.selectedDate())

    def accept(self):
        if self.set_range(self.draft_start, self.draft_end):
            self.result_start, self.result_end = self.draft_start, self.draft_end
            super().accept()


class _RangeStrip(QWidget):
    """A neutral hover clock and source-colored range with real endpoints only."""
    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.preferences = picker.preferences
        self.gutter = calendar_gutter(self.preferences)
        self.first_minute, self.last_minute = 8 * 60, 18 * 60
        self._hover = None
        self._drag = None
        self._draft = None
        self._invalid_drag = None
        self._moved = False
        self.setFixedWidth(round(158 * self.preferences.font_scale / 100))
        self.setMinimumHeight(round(260 * self.preferences.font_scale / 100))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Time range')

    @property
    def continues_before(self):
        return self.picker.draft_start.date() < self.picker.view_day

    @property
    def continues_after(self):
        return self.picker.draft_end.date() > self.picker.view_day

    def refresh(self):
        if not self._drag:
            first, last = self.preferences.visible_start_hour, self.preferences.visible_end_hour
            if self.picker.full_day:
                first, last = 0, 24
            else:
                start, end, day = self.picker.draft_start, self.picker.draft_end, self.picker.view_day
                if start.date() < day:
                    first = 0
                elif start.date() == day:
                    first = min(first, start.hour)
                if end.date() > day:
                    last = 24
                elif end.date() == day:
                    last = max(last, math.ceil((end.hour * 60 + end.minute + end.second / 60) / 60))
            self.first_minute = first * 60
            self.last_minute = min(1440, max(first + 1, last) * 60)
        self.update()

    def y_for(self, value):
        if isinstance(value, datetime):
            midnight = datetime.combine(self.picker.view_day, time.min)
            value = (value.astimezone(self.picker.zone).replace(tzinfo=None) - midnight).total_seconds() / 60
        return 12 + (value - self.first_minute) / max(1, self.last_minute - self.first_minute) * max(1, self.height() - 24)

    minute_y = y_for

    def minute_at(self, point, *, endpoint=False):
        minute = self.first_minute + (point.y() - 12) / max(1, self.height() - 24) * (self.last_minute - self.first_minute)
        maximum = self.last_minute if endpoint else self.last_minute - 15
        return max(self.first_minute, min(maximum, round(minute / 15) * 15))

    def time_at(self, point, *, endpoint=False):
        minute = self.minute_at(point, endpoint=endpoint) if hasattr(point, 'y') else point
        previous = self.picker.draft_start if self.picker.endpoint == 'start' else self.picker.draft_end
        value = datetime.combine(self.picker.view_day, time.min, self.picker.zone) + timedelta(minutes=minute)
        return value.replace(fold=previous.fold)

    def _band_for(self, start, end):
        top = max(12, self.y_for(start))
        bottom = min(self.height() - 12, self.y_for(end))
        if bottom < 12 or top > self.height() - 12:
            return QRectF()
        height = max(16, bottom - top)
        top = min(top, self.height() - 12 - height)
        return QRectF(self.gutter + 10, top, 30, height)

    def band_rect(self):
        return self._band_for(self.picker.draft_start, self.picker.draft_end)

    def start_handle(self):
        if self.picker.draft_start.date() != self.picker.view_day:
            return QRectF()
        return QRectF(self.gutter + 7, self.y_for(self.picker.draft_start) - 5, 36, 10)

    def end_handle(self):
        if self.picker.draft_end.date() != self.picker.view_day:
            return QRectF()
        return QRectF(self.gutter + 7, self.y_for(self.picker.draft_end) - 5, 36, 10)

    def _paint_handle(self, painter, rect, color):
        if rect.isEmpty():
            return
        painter.setPen(QPen(QColor(color), 1.4))
        painter.drawLine(QPointF(rect.left() + 3, rect.center().y()), QPointF(rect.right() - 3, rect.center().y()))

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scheme = scheme_for(self.preferences)
        scale = self.preferences.font_scale / 100
        style = event_colors(self.picker.calendar_color, self.preferences)
        painter.fillRect(self.rect(), QColor(scheme.background))
        painter.setPen(QPen(QColor(scheme.line), scale))
        painter.drawLine(QPointF(self.gutter, 12), QPointF(self.gutter, self.height() - 12))
        step = 60 if (self.height() - 24) / max(1, (self.last_minute - self.first_minute) / 60) >= 22 else 120
        for minute in range(self.first_minute, self.last_minute + 1, 30):
            y = self.y_for(minute)
            painter.setPen(QPen(QColor(scheme.line), scale))
            painter.drawLine(QPointF(self.gutter - (5 if minute % 60 == 0 else 3), y), QPointF(self.gutter, y))
            if minute % step == 0:
                caption = '24:00' if minute == 1440 else self.preferences.format_time(self.time_at(minute))
                text(painter, QRectF(0, y - 8, self.gutter - 8, 16), caption, QColor(scheme.muted), 7.5,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        start, end = self._draft or (self.picker.draft_start, self.picker.draft_end)
        band = self._band_for(start, end)
        painter.setPen(QPen(QColor(style.outline), 1.2))
        painter.setBrush(QColor(style.fill))
        if not band.isEmpty() and self.y_for(end) > 12 and self.y_for(start) < self.height() - 12:
            painter.drawRoundedRect(band, 3, 3)
        if not self._draft:
            self._paint_handle(painter, self.start_handle(), style.outline)
            self._paint_handle(painter, self.end_handle(), style.outline)
        for continued, y, direction in ((self.continues_before, 14, -1),
                                        (self.continues_after, self.height() - 14, 1)):
            if continued:
                x = self.gutter + 25
                painter.setPen(QPen(QColor(style.outline), 1.4))
                painter.drawLine(QPointF(x - 5, y - direction * 4), QPointF(x, y + direction * 1))
                painter.drawLine(QPointF(x, y + direction * 1), QPointF(x + 5, y - direction * 4))
        if self._hover is not None and not self._drag:
            y = self.y_for(self._hover)
            painter.setPen(QPen(QColor(scheme.muted), .9, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(self.gutter + 2, y), QPointF(self.width() - 3, y))
            caption = '24:00' if self._hover == 1440 else self.preferences.format_time(self.time_at(self._hover))
            box = QRectF(self.gutter + 48, max(2, min(self.height() - 20, y - 10)), self.width() - self.gutter - 49, 20)
            painter.fillRect(box, QColor(scheme.background))
            text(painter, box, caption, QColor(scheme.ink), 8, True,
                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        self._hover = None
        self._invalid_drag = None
        self._moved = False
        picker = self.picker
        if self.start_handle().contains(point):
            kind = 'start'
        elif self.end_handle().contains(point):
            kind = 'end'
        elif self.band_rect().contains(point) and picker.same_day:
            kind = 'move'
        elif self.band_rect().contains(point):
            kind = 'cross'
        else:
            kind = 'create' if picker.same_day else picker.endpoint
        self._drag = (kind, point, self.minute_at(point), picker.draft_start, picker.draft_end)

    def mouseMoveEvent(self, event):
        point = event.position()
        if not self._drag:
            self._hover = self.minute_at(point)
            if self.start_handle().contains(point) or self.end_handle().contains(point):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif self.band_rect().contains(point) and self.picker.same_day:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        kind, origin, minute, start, end = self._drag
        if (point - origin).manhattanLength() < 4:
            return
        self._moved = True
        if kind == 'cross':
            return
        try:
            value = self.time_at(point, endpoint=True)
            if kind == 'start':
                draft = value, end
            elif kind == 'end':
                draft = start, value
            elif kind == 'create':
                other = self.time_at(minute)
                first, last = sorted((other, value), key=lambda stamp: stamp.timestamp())
                draft = first, max(first + timedelta(minutes=15), last)
            else:
                distance = (point.y() - origin.y()) / max(1, self.height() - 24) * (self.last_minute - self.first_minute)
                delta = timedelta(minutes=round(distance / 15) * 15)
                draft = ((start.replace(tzinfo=None) + delta).replace(tzinfo=start.tzinfo, fold=start.fold),
                         (end.replace(tzinfo=None) + delta).replace(tzinfo=end.tzinfo, fold=end.fold))
            _validate(draft[0])
            _validate(draft[1])
            if draft[1].timestamp() <= draft[0].timestamp():
                raise ValueError('The end must be after the start.')
            self._draft = draft
            self._invalid_drag = None
        except (ValueError, OverflowError) as exc:
            self._draft = None
            self._invalid_drag = str(exc)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        kind, _origin, minute, _start, _end = self._drag
        draft, invalid = self._draft, self._invalid_drag
        self._drag = self._draft = None
        self._invalid_drag = None
        if invalid:
            self.picker.error.setText(invalid)
            self.picker.error.show()
        elif draft:
            self.picker.set_range(*draft)
        elif kind == 'create':
            value = self.time_at(minute)
            if self.picker.endpoint == 'start':
                self.picker.set_start(value)
            else:
                self.picker.set_end(value)
        elif kind in ('start', 'end'):
            value = self.time_at(minute)
            if kind == 'start':
                self.picker.set_start(value, preserve_duration=False)
            else:
                self.picker.set_end(value)
        elif kind == 'cross' and not self._moved:
            value = self.time_at(minute)
            if self.picker.endpoint == 'start':
                self.picker.set_start(value, preserve_duration=False)
            else:
                self.picker.set_end(value)
        self.refresh()

    def leaveEvent(self, event):
        if not self._drag:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 15
            shift = timedelta(minutes=amount * (-1 if event.key() == Qt.Key.Key_Up else 1))
            if self.picker.endpoint == 'start':
                self.picker.set_start(self.picker.draft_start + shift, preserve_duration=self.picker.same_day)
            else:
                self.picker.set_end(self.picker.draft_end + shift)
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.picker.accept()
            event.accept()
        else:
            super().keyPressEvent(event)
