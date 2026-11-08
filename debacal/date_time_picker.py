"""Transactional date and time-range selection for primary and extra events."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import math

from PySide6.QtCore import QDate, QEvent, QPointF, QRectF, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QCalendarWidget, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .appearance import stylesheet, widget_palette
from .buttons import icon_button
from .calendar_widgets import calendar_gutter, text
from .date_time_fields import format_duration, validate_datetime
from .event_style import event_colors
from .mini_calendar import MiniCalendar
from .preferences import Preferences
from .theme import corner_radius, scheme_for


def range_duration(start, end):
    wall = end.replace(tzinfo=None) - start.replace(tzinfo=None)
    return wall if wall > timedelta(0) else end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


class DateTimeRangePicker(QDialog):
    """Edit a range in a draft, using inclusive dates for all-day display.

    Callers always supply and receive an exclusive ``end``. All-day End date
    selection displays the last included day and converts it back on Apply.
    """
    def __init__(self, start, end, *, endpoint='start', preferences=None,
                 calendar_color='#587c67', parent=None, all_day=False, full_day_shortcut='F'):
        super().__init__(parent, Qt.WindowType.Dialog)
        # Establish the final transient-dialog modality before its first map.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        if endpoint not in ('start', 'end'):
            raise ValueError('Choose the start or end picker.')
        validate_datetime(start)
        validate_datetime(end)
        if end.timestamp() <= start.timestamp():
            raise ValueError('The end must be after the start.')
        self.preferences = preferences or Preferences()
        self.endpoint, self.all_day = endpoint, bool(all_day)
        self.zone, self.calendar_color = start.tzinfo, calendar_color
        self.draft_start = start
        self.draft_end = end.replace(tzinfo=self.zone) if all_day else end.astimezone(self.zone)
        self.result_start, self.result_end = start, end
        self._updating = False
        self.full_day = False
        self.setWindowTitle(('Start date' if endpoint == 'start' else 'End date') + ('' if all_day else ' and time'))
        self.setLocale(self.preferences.qt_locale())
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences))
        scale = self.preferences.font_scale / 100
        self.resize(round((345 if all_day else 540) * scale), round((310 if all_day else 420) * scale))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(15, 13, 15, 13)
        outer.setSpacing(10)
        visual = QHBoxLayout()
        visual.setSpacing(14)
        calendar_preferences = replace(self.preferences, show_week_numbers=False)
        self.month = MiniCalendar(calendar_preferences)
        self.month.calendar.surface_role = 'background'
        self.month.set_preferences(calendar_preferences)
        self.month.calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.month.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.calendar = self.month.calendar
        self.calendar.selectionChanged.connect(self.choose_selected_date)
        calendar_column = QVBoxLayout()
        calendar_column.setSpacing(4)
        calendar_column.addWidget(self.month, 1)
        self.day_count = QLabel(self)
        self.day_count.setProperty('muted', True)
        self.day_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        calendar_column.addWidget(self.day_count)
        self.day_count.setVisible(all_day)
        visual.addLayout(calendar_column, 1)
        self.time_column = QWidget(self)
        timeline = QVBoxLayout(self.time_column)
        timeline.setContentsMargins(0, 0, 0, 0)
        timeline.setSpacing(2)
        time_header = QHBoxLayout()
        time_header.setContentsMargins(0, 0, 0, 0)
        label = QLabel('Time')
        label.setObjectName('sidebarSectionHeading')
        time_header.addWidget(label, 1)
        # Give every control a parent before changing visibility.
        self.full_day_button = icon_button('expand', 'Show all 24 hours', preferences=self.preferences, parent=self.time_column)
        self.full_day_button.setCheckable(True)
        self.full_day_button.toggled.connect(self.set_full_day)
        time_header.addWidget(self.full_day_button)
        timeline.addLayout(time_header)
        self.strip = TimeRangeStrip(self)
        timeline.addWidget(self.strip, 1)
        visual.addWidget(self.time_column)
        self.time_column.setVisible(not all_day)
        outer.addLayout(visual, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('error', True)
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
        self.full_day_shortcut = QShortcut(QKeySequence(full_day_shortcut or ''), self)
        self.full_day_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.full_day_shortcut.setEnabled(bool(full_day_shortcut and not all_day))
        self.full_day_shortcut.activated.connect(self.full_day_button.toggle)
        for field in self.findChildren(QLineEdit):
            field.installEventFilter(self)
        self._update_full_day_tooltip()
        self.refresh()

    @property
    def view_day(self):
        if self.endpoint == 'start':
            return self.draft_start.date()
        return (self.draft_end - timedelta(days=1)).date() if self.all_day else self.draft_end.date()

    @property
    def duration(self):
        return range_duration(self.draft_start, self.draft_end)

    @property
    def same_day(self):
        return self.draft_start.date() == self.draft_end.date()

    def set_full_day(self, enabled):
        self.full_day = bool(enabled)
        if self.full_day_button.isChecked() != self.full_day:
            with QSignalBlocker(self.full_day_button):
                self.full_day_button.setChecked(self.full_day)
        self._update_full_day_tooltip()
        self.strip.refresh()

    def _update_full_day_tooltip(self):
        caption = 'Fit event hours' if self.full_day else 'Show all 24 hours'
        shortcut = self.full_day_shortcut.key().toString(QKeySequence.SequenceFormat.NativeText)
        self.full_day_button.setToolTip(caption + (' (' + shortcut + ')' if shortcut else ''))
        self.full_day_button.setAccessibleName(caption)

    def eventFilter(self, field, event):
        if isinstance(field, QLineEdit) and event.type() == QEvent.Type.ShortcutOverride:
            # Remapped letter/digit shortcuts must not consume year input.
            event.accept()
            return True
        return super().eventFilter(field, event)

    def refresh(self):
        self._updating = True
        try:
            day = self.view_day
            self.calendar.setSelectedDate(QDate(day.year, day.month, day.day))
            self.calendar.setCurrentPage(day.year, day.month)
            if self.all_day:
                self.day_count.setText(format_duration(self.duration, all_day=True))
            self.error.hide()
            self.strip.refresh()
        finally:
            self._updating = False

    def set_range(self, start, end):
        try:
            validate_datetime(start)
            validate_datetime(end)
            if end.timestamp() <= start.timestamp():
                raise ValueError('The end must be after the start.')
            if self.all_day and (start.time() != time.min or end.time() != time.min):
                raise ValueError('All-day events use whole calendar dates.')
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False
        self.draft_start = start.replace(tzinfo=self.zone) if self.all_day else start.astimezone(self.zone)
        self.draft_end = end.replace(tzinfo=self.zone) if self.all_day else end.astimezone(self.zone)
        self.refresh()
        return True

    def set_start(self, value, *, preserve_duration=True):
        try:
            validate_datetime(value)
            value = value.replace(tzinfo=self.zone) if self.all_day else value.astimezone(self.zone)
            end = value + self.duration if preserve_duration else self.draft_end
            return self.set_range(value, end)
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False

    def end_pair(self, value, *, start=None, duration=None):
        """Keep the duration and move Start when an earlier End is selected."""
        start = self.draft_start if start is None else start
        duration = self.duration if duration is None else duration
        return (value - duration if value.timestamp() <= start.timestamp() else start), value

    def set_end(self, value):
        try:
            validate_datetime(value)
            value = value.replace(tzinfo=self.zone) if self.all_day else value.astimezone(self.zone)
            return self.set_range(*self.end_pair(value))
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False

    def choose_selected_date(self):
        if not self._updating:
            self.choose_date(self.calendar.selectedDate())

    def choose_date(self, value):
        day = value.toPython() if isinstance(value, QDate) else value
        try:
            if self.endpoint == 'start':
                candidate = datetime.combine(day, self.draft_start.timetz())
                changed = self.set_start(candidate)
            else:
                candidate = datetime.combine(day + timedelta(days=1) if self.all_day else day,
                                             self.draft_end.timetz())
                changed = self.set_end(candidate)
            if not changed:
                self._updating = True
                previous = self.view_day
                self.calendar.setSelectedDate(QDate(previous.year, previous.month, previous.day))
                self._updating = False
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()

    def accept(self):
        if self.set_range(self.draft_start, self.draft_end):
            self.result_start, self.result_end = self.draft_start, self.draft_end
            super().accept()


class TimeRangeStrip(QWidget):
    """A compact range with real endpoints, continuation marks and duration hints."""
    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.preferences = picker.preferences
        self.gutter = calendar_gutter(self.preferences)
        self.first_minute, self.last_minute = 480, 1080
        self._hover = self._drag = self._draft = None
        self._invalid = None
        self._moved = False
        self.setFixedWidth(self.gutter + round(148 * self.preferences.font_scale / 100))
        self.setMinimumHeight(round(250 * self.preferences.font_scale / 100))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName('Start, end and duration')

    @property
    def continues_before(self):
        return self.active_range[0].date() < self.picker.view_day

    @property
    def continues_after(self):
        return self.active_range[1].date() > self.picker.view_day

    @property
    def active_range(self):
        return self._draft or (self.picker.draft_start, self.picker.draft_end)

    @property
    def axis_top(self):
        return 18 * self.preferences.font_scale / 100

    @property
    def axis_bottom(self):
        # A stable strip-local line is reserved for the pre-drag reference.
        return self.height() - 30 * self.preferences.font_scale / 100

    @property
    def axis_height(self):
        return max(1, self.axis_bottom - self.axis_top)

    def range_caption(self, start, end):
        caption = self.preferences.format_time(start) + '–' + self.preferences.format_time(end)
        days = (end.date() - start.date()).days
        if days:
            caption += f' (+{days}d)'
        return caption + ' · ' + format_duration(range_duration(start, end))

    @property
    def before_caption(self):
        return 'Before: ' + self.range_caption(self._drag[3], self._drag[4]) if self._drag else ''

    def refresh(self):
        if not self._drag:
            first, last = self.preferences.visible_start_hour, self.preferences.visible_end_hour
            if self.picker.full_day:
                first, last = 0, 24
            else:
                start, end, day = self.picker.draft_start, self.picker.draft_end, self.picker.view_day
                first = 0 if start.date() < day else min(first, start.hour)
                last = 24 if end.date() > day else max(last, math.ceil((end.hour * 60 + end.minute + end.second / 60) / 60))
            self.first_minute, self.last_minute = first * 60, min(1440, max(first + 1, last) * 60)
        self.update()

    def y_for(self, value):
        if isinstance(value, datetime):
            local = value.astimezone(self.picker.zone)
            value = (local.replace(tzinfo=None) - datetime.combine(self.picker.view_day, time.min)).total_seconds() / 60
        return self.axis_top + (value - self.first_minute) / max(1, self.last_minute - self.first_minute) * self.axis_height

    minute_y = y_for

    def minute_at(self, point, *, endpoint=False):
        minute = self.first_minute + (point.y() - self.axis_top) / self.axis_height * (self.last_minute - self.first_minute)
        return max(self.first_minute, min(self.last_minute if endpoint else self.last_minute - 15, round(minute / 15) * 15))

    def time_at(self, point, *, endpoint=False):
        minute = self.minute_at(point, endpoint=endpoint) if hasattr(point, 'y') else point
        previous = self.picker.draft_start if self.picker.endpoint == 'start' else self.picker.draft_end
        return (datetime.combine(self.picker.view_day, time.min, self.picker.zone)
                + timedelta(minutes=minute)).replace(fold=previous.fold)

    def _band_for(self, start, end):
        scale = self.preferences.font_scale / 100
        top, bottom = max(self.axis_top, self.y_for(start)), min(self.axis_bottom, self.y_for(end))
        height = max(3 * scale, bottom - top)
        return QRectF(self.gutter + 8 * scale, min(top, self.axis_bottom - height), 36 * scale, height)

    def band_rect(self):
        return self._band_for(self.picker.draft_start, self.picker.draft_end)

    def start_handle(self):
        scale = self.preferences.font_scale / 100
        return (QRectF(self.gutter + 5 * scale, self.y_for(self.picker.draft_start) - 5 * scale, 42 * scale, 10 * scale)
                if self.picker.draft_start.date() == self.picker.view_day else QRectF())

    def end_handle(self):
        scale = self.preferences.font_scale / 100
        return (QRectF(self.gutter + 5 * scale, self.y_for(self.picker.draft_end) - 5 * scale, 42 * scale, 10 * scale)
                if self.picker.draft_end.date() == self.picker.view_day else QRectF())

    def hover_hint(self, value):
        if self.picker.endpoint == 'end':
            start, end = self.picker.end_pair(value)
            duration = range_duration(start, end)
        else:
            duration = self.picker.duration
        return self.preferences.format_time(value), format_duration(duration)

    def _clock_label(self, value):
        scale = self.preferences.font_scale / 100
        return QRectF(0, self.y_for(value) - 9 * scale, self.gutter - 7 * scale, 18 * scale)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = scheme_for(self.preferences)
        style = event_colors(self.picker.calendar_color, self.preferences)
        scale = self.preferences.font_scale / 100
        start, end = self.active_range
        band = self._band_for(start, end)
        visible = [value for value in (start, end) if value.date() == self.picker.view_day]
        short = len(visible) == 2 and self.y_for(end) - self.y_for(start) < 28 * scale
        painter.fillRect(self.rect(), QColor(colors.background))
        painter.setPen(QPen(QColor(colors.line), scale))
        painter.drawLine(QPointF(self.gutter, self.axis_top), QPointF(self.gutter, self.axis_bottom))
        step = 60 if self.axis_height / max(1, (self.last_minute - self.first_minute) / 60) >= 22 * scale else 120
        for minute in range(self.first_minute, self.last_minute + 1, 30):
            y = self.y_for(minute)
            painter.setPen(QPen(QColor(colors.line), scale))
            painter.drawLine(QPointF(self.gutter - (5 if minute % 60 == 0 else 3) * scale, y), QPointF(self.gutter, y))
            if minute % step == 0 and all(abs(y - self.y_for(value)) >= 14 * scale for value in visible):
                caption = '24:00' if minute == 1440 else self.preferences.format_time(self.time_at(minute))
                text(painter, QRectF(0, y - 8 * scale, self.gutter - 8 * scale, 16 * scale), caption,
                     QColor(colors.muted), 7.5, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if self._draft and self._drag:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            previous = QColor(style.outline)
            previous.setAlpha(110)
            painter.setPen(QPen(previous, scale, Qt.PenStyle.DashLine))
            old = self._band_for(self._drag[3], self._drag[4])
            radius = corner_radius(self.preferences, 3)
            painter.drawRoundedRect(old, radius, radius)
        painter.setPen(QPen(QColor(style.outline), 1.2 * scale))
        painter.setBrush(QColor(style.fill))
        radius = min(corner_radius(self.preferences, 3), band.height() / 2)
        painter.drawRoundedRect(band, radius, radius)
        # The outline itself supplies both resize edges; no extra lines cut
        # across the rounded corners.
        for value in visible:
            if short and value == end:
                continue
            label = self._clock_label(value)
            painter.fillRect(label, QColor(colors.background))
            text(painter, label, self.preferences.format_time(value), QColor(colors.ink), 8, True,
                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bracket_x = band.right() + 8 * scale
        painter.setPen(QPen(QColor(colors.muted), scale))
        painter.drawLine(QPointF(bracket_x, band.top()), QPointF(bracket_x, band.bottom()))
        for y in (band.top(), band.bottom()):
            painter.drawLine(QPointF(bracket_x - 3 * scale, y), QPointF(bracket_x + 3 * scale, y))
        info_x = bracket_x + 8 * scale
        lines = []
        if short:
            lines.append(self.preferences.format_time(end))
        duration_caption = format_duration(range_duration(start, end))
        lines.append(duration_caption)
        outside = end if self.continues_after else start if self.continues_before else None
        if outside is not None:
            days = (outside.date() - self.picker.view_day).days
            lines.append(self.preferences.format_time(outside) + f' ({days:+}d)')
        height = 17 * scale * len(lines)
        top = max(self.axis_top - 6 * scale, min(self.axis_bottom - height, band.center().y() - height / 2))
        for index, caption in enumerate(lines):
            text(painter, QRectF(info_x, top + 17 * scale * index, self.width() - info_x, 17 * scale), caption,
                 QColor(colors.ink if caption == lines[0] else colors.muted), 8,
                 caption == duration_caption)
        for continued, y, direction in ((self.continues_before, self.axis_top + 2 * scale, -1),
                                        (self.continues_after, self.axis_bottom - 2 * scale, 1)):
            if continued:
                x = band.center().x()
                painter.setPen(QPen(QColor(style.outline), 1.4 * scale))
                painter.drawLine(QPointF(x - 5 * scale, y - direction * 4 * scale), QPointF(x, y + direction * scale))
                painter.drawLine(QPointF(x, y + direction * scale), QPointF(x + 5 * scale, y - direction * 4 * scale))
        if self._hover is not None and not self._drag:
            y = self.y_for(self._hover)
            if y < band.top() - 8 * scale or y > band.bottom() + 8 * scale:
                painter.setPen(QPen(QColor(colors.muted), .9 * scale, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(self.gutter + 2 * scale, y), QPointF(self.width() - 3 * scale, y))
                caption, duration = self.hover_hint(self.time_at(self._hover))
                top = max(self.axis_top - 8 * scale, min(self.axis_bottom - 36 * scale, y - 9 * scale))
                box = QRectF(info_x, top, self.width() - info_x, 36 * scale)
                painter.fillRect(box, QColor(colors.background))
                text(painter, QRectF(box.x(), box.y(), box.width(), 18 * scale), caption, QColor(colors.ink), 8, True)
                text(painter, QRectF(box.x(), box.y() + 18 * scale, box.width(), 18 * scale), duration, QColor(colors.muted), 8)
        if self.before_caption:
            text(painter, QRectF(0, self.height() - 20 * scale, self.width(), 18 * scale),
                 self.before_caption, QColor(colors.muted), 7.5)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        self._hover = self._draft = self._invalid = None
        self._moved = False
        handles = [(name, handle) for name, handle in (('start', self.start_handle()), ('end', self.end_handle()))
                   if not handle.isEmpty() and (handle.contains(point) or self._clock_label(
                       self.picker.draft_start if name == 'start' else self.picker.draft_end).contains(point))]
        target = min(handles, key=lambda item: abs(point.y() - item[1].center().y()))[0] if handles else None
        kind = (target if target else 'move' if self.band_rect().contains(point) and self.picker.same_day
                else 'cross' if self.band_rect().contains(point)
                else 'create' if self.picker.same_day else self.picker.endpoint)
        self._drag = kind, point, self.minute_at(point), self.picker.draft_start, self.picker.draft_end

    def mouseMoveEvent(self, event):
        point = event.position()
        if not self._drag:
            self._hover = self.minute_at(point)
            self.setCursor(Qt.CursorShape.SizeVerCursor if self.start_handle().contains(point) or self.end_handle().contains(point)
                           else Qt.CursorShape.OpenHandCursor if self.band_rect().contains(point) and self.picker.same_day
                           else Qt.CursorShape.CrossCursor)
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
                draft = self.picker.end_pair(value, start=start, duration=range_duration(start, end))
            elif kind == 'create':
                other = self.time_at(minute)
                first, last = sorted((other, value), key=lambda stamp: stamp.timestamp())
                draft = first, max(first + timedelta(minutes=15), last)
            else:
                distance = (point.y() - origin.y()) / self.axis_height * (self.last_minute - self.first_minute)
                delta = timedelta(minutes=round(distance / 15) * 15)
                draft = start + delta, end + delta
            validate_datetime(draft[0])
            validate_datetime(draft[1])
            if draft[1].timestamp() <= draft[0].timestamp():
                raise ValueError('The end must be after the start.')
            self._draft, self._invalid = draft, None
        except (ValueError, OverflowError) as exc:
            self._draft, self._invalid = None, str(exc)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        kind, _origin, minute, _start, _end = self._drag
        draft, invalid = self._draft, self._invalid
        self._drag = self._draft = self._invalid = None
        if invalid:
            self.picker.error.setText(invalid)
            self.picker.error.show()
        elif draft:
            self.picker.set_range(*draft)
        elif kind == 'create':
            (self.picker.set_start if self.picker.endpoint == 'start' else self.picker.set_end)(self.time_at(minute))
        elif kind in ('start', 'end') or (kind == 'cross' and not self._moved):
            target = kind if kind != 'cross' else self.picker.endpoint
            if target == 'start':
                self.picker.set_start(self.time_at(minute), preserve_duration=False)
            else:
                self.picker.set_end(self.time_at(minute))
        self.refresh()

    def leaveEvent(self, event):
        if not self._drag:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = 1 if event.modifiers() & Qt.KeyboardModifier.AltModifier else 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 15
            delta = timedelta(minutes=amount * (-1 if event.key() == Qt.Key.Key_Up else 1))
            if self.picker.endpoint == 'start':
                self.picker.set_start(self.picker.draft_start + delta, preserve_duration=self.picker.same_day)
            else:
                self.picker.set_end(self.picker.draft_end + delta)
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.picker.accept()
            event.accept()
        else:
            super().keyPressEvent(event)
