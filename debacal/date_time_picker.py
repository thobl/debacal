"""Transactional date and time-range selection for primary and extra events."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
import math

from PySide6.QtCore import QDate, QEvent, QPointF, QRectF, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QCalendarWidget, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .appearance import stylesheet, widget_palette
from .buttons import icon_button
from .calendar_widgets import calendar_gutter, text
from .date_time_fields import format_duration, format_event_duration, validate_datetime
from .event_duration import add_duration, add_elapsed, elapsed_duration, normalized_spec, parse_duration_spec
from .dialog_base import dialog_buttons, install_dialog_keyboard
from .event_style import event_colors
from .mini_calendar import MiniCalendar
from .preferences import Preferences
from .theme import corner_radius, scheme_for
from .shortcuts import shortcut_keys, shortcut_label, shortcut_sequences


def range_duration(start, end, *, all_day=False, floating_time=False):
    return (end.replace(tzinfo=None) - start.replace(tzinfo=None) if all_day or floating_time
            else elapsed_duration(start, end))


def validate_range_order(start, end, *, all_day=False, allow_point=False, floating_time=False):
    """Check floating range order separately from timed instant order."""
    duration = range_duration(start, end, all_day=all_day, floating_time=floating_time)
    if duration < timedelta(0) or (duration == timedelta(0) and not allow_point):
        raise ValueError('The end must be after the start.')


def relocate_range(start, end, target, *, endpoint='start', all_day=False, duration_spec='', duration_zone=None,
                    floating_time=False):
    """Retain floating days, explicit nominal days, or exact elapsed time."""
    if all_day or floating_time:
        duration = range_duration(start, end, all_day=True)
        return (target, target + duration) if endpoint == 'start' else (target - duration, target)
    if duration_spec:
        zone = duration_zone or start.tzinfo
        if endpoint == 'start':
            return target, add_duration(target.astimezone(zone), duration_spec).astimezone(target.tzinfo)
        days, seconds = parse_duration_spec(duration_spec)
        before_seconds = add_elapsed(target.astimezone(zone), -timedelta(seconds=seconds))
        first = (before_seconds - timedelta(days=days)).replace(fold=start.astimezone(zone).fold)
        return first.astimezone(target.tzinfo), target
    duration = elapsed_duration(start, end)
    if endpoint == 'start':
        return target, add_elapsed(target, duration)
    return add_elapsed(target, -duration), target


class DateTimeRangePicker(QDialog):
    """Edit a range in a draft, using inclusive dates for all-day display.

    Callers always supply and receive an exclusive ``end``. All-day End date
    selection displays the last included day and converts it back on Apply.
    """
    def __init__(self, start, end, *, endpoint='start', preferences=None,
                 calendar_color='#587c67', parent=None, all_day=False, full_day_shortcut='F',
                 duration_spec='', duration_zone=None, floating_time=False):
        super().__init__(parent, Qt.WindowType.Dialog)
        # Establish the final transient-dialog modality before its first map.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        if endpoint not in ('start', 'end'):
            raise ValueError('Choose the start or end picker.')
        validate_datetime(start, all_day=all_day, floating_time=floating_time)
        validate_datetime(end, all_day=all_day, floating_time=floating_time)
        # Imported timed events may intentionally have no duration. Keep them
        # editable without inventing an end, while ordinary range drafts still
        # require a positive duration.
        self._allow_point = not all_day and range_duration(start, end, floating_time=floating_time) == timedelta(0)
        validate_range_order(start, end, all_day=all_day, allow_point=self._allow_point, floating_time=floating_time)
        self.preferences = preferences or Preferences()
        self.endpoint, self.all_day = endpoint, bool(all_day)
        self.floating_time = bool(floating_time and not all_day)
        self.duration_spec = normalized_spec(duration_spec) if not all_day else ''
        self.duration_zone = duration_zone or start.tzinfo
        self.zone, self.calendar_color = start.tzinfo, calendar_color
        self.draft_start = start
        self.draft_end = end.replace(tzinfo=self.zone) if all_day or self.floating_time else end.astimezone(self.zone)
        self.result_start, self.result_end = start, end
        self.result_duration_spec = self.duration_spec
        self._updating = False
        self.full_day = False
        self.setWindowTitle(('Start date' if endpoint == 'start' else 'End date') + ('' if all_day else ' and time'))
        self.setLocale(self.preferences.qt_locale())
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences))
        scale = self.preferences.font_scale / 100
        self.resize(round((345 if all_day else 500) * scale), round((310 if all_day else 450) * scale))
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
        self.buttons = dialog_buttons(QDialogButtonBox.StandardButton.Ok)
        self.accept_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button = self.accept_button
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.full_day_shortcut = QShortcut(self)
        self.full_day_shortcut.setKeys(shortcut_sequences(full_day_shortcut))
        self._full_day_binding = full_day_shortcut
        self.full_day_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.full_day_shortcut.setEnabled(bool(shortcut_keys(full_day_shortcut) and not all_day))
        self.full_day_shortcut.activated.connect(self.full_day_button.toggle)
        for field in self.findChildren(QLineEdit):
            field.installEventFilter(self)
        self._update_full_day_tooltip()
        install_dialog_keyboard(self, self.accept, field_widgets=(self.strip, self.calendar))
        self.refresh()

    @property
    def view_day(self):
        if self.endpoint == 'start':
            return self.draft_start.date()
        return (self.draft_end - timedelta(days=1)).date() if self.all_day else self.draft_end.date()

    @property
    def duration(self):
        return range_duration(self.draft_start, self.draft_end, all_day=self.all_day, floating_time=self.floating_time)

    def relocate(self, start, end, value, *, endpoint='start'):
        return relocate_range(start, end, value, endpoint=endpoint, all_day=self.all_day,
                              duration_spec=self.duration_spec, duration_zone=self.duration_zone,
                              floating_time=self.floating_time)

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
        shortcut = shortcut_label(self._full_day_binding)
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

    def set_range(self, start, end, *, duration_spec=''):
        try:
            spec = normalized_spec(duration_spec) if not self.all_day else ''
            validate_datetime(start, all_day=self.all_day, floating_time=self.floating_time)
            validate_datetime(end, all_day=self.all_day, floating_time=self.floating_time)
            validate_range_order(start, end, all_day=self.all_day, allow_point=self._allow_point, floating_time=self.floating_time)
            if self.all_day and (start.time() != time.min or end.time() != time.min):
                raise ValueError('All-day events use whole calendar dates.')
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False
        self.draft_start = start.replace(tzinfo=self.zone) if self.all_day or self.floating_time else start.astimezone(self.zone)
        self.draft_end = end.replace(tzinfo=self.zone) if self.all_day or self.floating_time else end.astimezone(self.zone)
        self.duration_spec = spec
        self.refresh()
        return True

    def set_start(self, value, *, preserve_duration=True):
        try:
            validate_datetime(value, all_day=self.all_day, floating_time=self.floating_time)
            value = value.replace(tzinfo=self.zone) if self.all_day or self.floating_time else value.astimezone(self.zone)
            end = self.relocate(self.draft_start, self.draft_end, value)[1] if preserve_duration else self.draft_end
            return self.set_range(value, end, duration_spec=self.duration_spec if preserve_duration else '')
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False

    def end_pair(self, value, *, start=None, end=None):
        """Keep the duration and move Start when an earlier End is selected."""
        start = self.draft_start if start is None else start
        end = self.draft_end if end is None else end
        if range_duration(start, value, all_day=self.all_day, floating_time=self.floating_time) <= timedelta(0):
            return self.relocate(start, end, value, endpoint='end')
        return start, value

    def set_end(self, value):
        try:
            validate_datetime(value, all_day=self.all_day, floating_time=self.floating_time)
            value = value.replace(tzinfo=self.zone) if self.all_day or self.floating_time else value.astimezone(self.zone)
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
        if self.set_range(self.draft_start, self.draft_end, duration_spec=self.duration_spec):
            self.result_start, self.result_end = self.draft_start, self.draft_end
            self.result_duration_spec = self.duration_spec
            super().accept()


@dataclass
class _RangeDrag:
    kind: str
    origin: QPointF
    last: QPointF
    initial_minute: float
    pointer_minute: float
    start: datetime
    end: datetime


class TimeRangeStrip(QWidget):
    """Choose a time on the left; move or resize the event on the right.

    Pointer previews never alter the draft. A single range calculation supplies
    both the preview and the eventual commit, including earlier-end recovery.
    Drag distance is integrated in minutes so edge panning cannot move the
    event merely because the displayed axis has changed.
    """
    STEP = 5

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.preferences = picker.preferences
        self.gutter = calendar_gutter(self.preferences)
        self.first_minute, self.last_minute = 480, 1080
        self._hover = self._drag = self._draft = None
        self._invalid = None
        self._moved = False
        self._pan_timer = QTimer(self)
        self._pan_timer.setInterval(80)
        self._pan_timer.timeout.connect(self._pan_at_edge)
        self.setFixedWidth(self.gutter + round(112 * self.scale))
        self.setMinimumHeight(round(280 * self.scale))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName('Choose a time on the left, move or resize the event on the right')

    @property
    def scale(self):
        return self.preferences.font_scale / 100

    @property
    def axis_top(self):
        return 10 * self.scale

    @property
    def axis_bottom(self):
        return self.height() - 10 * self.scale

    @property
    def axis_height(self):
        return max(1, self.axis_bottom - self.axis_top)

    @property
    def timeline_right(self):
        return self.gutter + 3 * self.scale

    @property
    def active_range(self):
        return self._draft or (self.picker.draft_start, self.picker.draft_end)

    @property
    def active_duration_spec(self):
        if not self._draft:
            return self.picker.duration_spec
        preserve = (self._drag.kind == 'move' if self._drag else False)
        if (self._drag and self._drag.kind == 'timeline') or self._hover is not None:
            preserve = self.picker.endpoint == 'start' and (self.picker.same_day or bool(self.picker.duration_spec))
        return self.picker.duration_spec if preserve else ''

    @property
    def continues_before(self):
        return self._minute_for(self.active_range[0]) < self.first_minute

    @property
    def continues_after(self):
        return self._minute_for(self.active_range[1]) > self.last_minute

    def refresh(self):
        if not self._drag:
            self._hover = self._draft = self._invalid = None
            first, last = self.preferences.visible_start_hour, self.preferences.visible_end_hour
            if self.picker.full_day:
                first, last = 0, 24
            else:
                start, end, day = self.picker.draft_start, self.picker.draft_end, self.picker.view_day
                first = 0 if start.date() < day else min(first, start.hour)
                last = 24 if end.date() > day else max(last, math.ceil((end.hour * 60 + end.minute + end.second / 60) / 60))
            self.first_minute, self.last_minute = first * 60, min(1440, max(first + 1, last) * 60)
        self.update()

    def _minute_for(self, value):
        local = value.astimezone(self.picker.zone)
        return (local.replace(tzinfo=None) - datetime.combine(self.picker.view_day, time.min)).total_seconds() / 60

    def y_for(self, value):
        minute = self._minute_for(value) if isinstance(value, datetime) else value
        return self.axis_top + (minute - self.first_minute) / max(1, self.last_minute - self.first_minute) * self.axis_height

    minute_y = y_for

    def _raw_minute_at(self, point):
        return self.first_minute + (point.y() - self.axis_top) / self.axis_height * (self.last_minute - self.first_minute)

    def _snap(self, minute):
        return round(minute / self.STEP) * self.STEP

    def minute_at(self, point, *, endpoint=False):
        minute = self._snap(self._raw_minute_at(point))
        return max(self.first_minute, min(self.last_minute if endpoint else self.last_minute - self.STEP, minute))

    def time_at(self, point, *, endpoint=False):
        minute = self.minute_at(point, endpoint=endpoint) if hasattr(point, 'y') else point
        previous = self.picker.draft_start if self.picker.endpoint == 'start' else self.picker.draft_end
        return (datetime.combine(self.picker.view_day, time.min, self.picker.zone)
                + timedelta(minutes=minute)).replace(fold=previous.fold)

    def proposed_range(self, value, *, start=None, end=None):
        """The range a timeline click will choose, without mutating the picker."""
        start = self.picker.draft_start if start is None else start
        end = self.picker.draft_end if end is None else end
        if self.picker.endpoint == 'end':
            return self.picker.end_pair(value, start=start, end=end)
        return self.picker.relocate(start, end, value) if start.date() == end.date() or self.picker.duration_spec else (value, end)

    def hover_hint(self, value):
        start, end = self.proposed_range(value)
        spec = self.picker.duration_spec if self.picker.endpoint == 'start' else ''
        return self.preferences.format_time(value), format_event_duration(start, end, duration_spec=spec, floating_time=self.picker.floating_time)

    def _band_for(self, start, end):
        top, bottom = sorted((self.y_for(start), self.y_for(end)))
        top, bottom = max(self.axis_top, top), min(self.axis_bottom, bottom)
        height = max(3 * self.scale, bottom - top)
        return QRectF(self.gutter + 11 * self.scale, min(top, self.axis_bottom - height), 30 * self.scale, height)

    def band_rect(self):
        return self._band_for(self.picker.draft_start, self.picker.draft_end)

    def _handle(self, value, *, end=False):
        minute = self._minute_for(value)
        if not (0 <= minute <= 1440 if end else 0 <= minute < 1440):
            return QRectF()
        band = self.band_rect()
        # A tiny range still needs a body that can be moved. Its resize targets
        # use the outer edge rather than covering the entire event.
        left, width = ((band.right() - 7 * self.scale, 10 * self.scale) if band.height() < 16 * self.scale
                       else (self.gutter + 8 * self.scale, 36 * self.scale))
        return QRectF(left, self.y_for(value) - 5 * self.scale, width, 10 * self.scale)

    def start_handle(self):
        return self._handle(self.picker.draft_start)

    def end_handle(self):
        return self._handle(self.picker.draft_end, end=True)

    def clock_labels(self, start=None, end=None):
        """Keep both endpoint labels on the axis, separating close captions."""
        start, end = self.active_range if start is None else (start, end)
        entries = []
        endpoints = (start,) if start.timestamp() == end.timestamp() else (start, end)
        for value in endpoints:
            minute = self._minute_for(value)
            if 0 <= minute <= 1440:
                entries.append([value, max(9 * self.scale, min(self.height() - 9 * self.scale, self.y_for(value)))])
        if len(entries) == 2:
            # Ordering by position also handles a repeated daylight-saving hour.
            entries.sort(key=lambda entry: entry[1])
            if entries[1][1] - entries[0][1] < 21 * self.scale:
                middle = (entries[0][1] + entries[1][1]) / 2
                entries[0][1], entries[1][1] = middle - 10.5 * self.scale, middle + 10.5 * self.scale
            offset = max(0, 9 * self.scale - entries[0][1]) - max(0, entries[-1][1] + 9 * self.scale - self.height())
            for entry in entries:
                entry[1] += offset
        return [(value, QRectF(0, max(0, min(self.height() - 18 * self.scale, center - 9 * self.scale)),
                               self.gutter - 7 * self.scale, 18 * self.scale)) for value, center in entries]

    def _draw_range(self, painter, start, end, style, *, ghost=False):
        band = self._band_for(start, end)
        outline = QColor(style.outline)
        if ghost:
            outline.setAlpha(100)
        painter.setPen(QPen(outline, self.scale if ghost else 1.2 * self.scale,
                            Qt.PenStyle.DashLine if ghost else Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush if ghost else QColor(style.fill))
        radius = min(corner_radius(self.preferences, 3), band.height() / 2)
        painter.drawRoundedRect(band, radius, radius)
        return band

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = scheme_for(self.preferences)
        style = event_colors(self.picker.calendar_color, self.preferences)
        scale = self.scale
        start, end = self.active_range
        labels = self.clock_labels(start, end)
        painter.fillRect(self.rect(), QColor(colors.background))
        painter.setPen(QPen(QColor(colors.line), scale))
        painter.drawLine(QPointF(self.gutter, self.axis_top), QPointF(self.gutter, self.axis_bottom))
        step = 60 if self.axis_height / max(1, (self.last_minute - self.first_minute) / 60) >= 22 * scale else 120
        for minute in range(self.first_minute, self.last_minute + 1, 30):
            y = self.y_for(minute)
            painter.setPen(QPen(QColor(colors.line), scale))
            painter.drawLine(QPointF(self.gutter - (5 if minute % 60 == 0 else 3) * scale, y), QPointF(self.gutter, y))
            if minute % step == 0 and all(not rect.adjusted(0, -4 * scale, 0, 4 * scale).contains(QPointF(0, y))
                                         for _value, rect in labels):
                caption = '24:00' if minute == 1440 else self.preferences.format_time(self.time_at(minute))
                text(painter, QRectF(0, y - 8 * scale, self.gutter - 8 * scale, 16 * scale), caption,
                     QColor(colors.muted), 7.5, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        original = (self.picker.draft_start, self.picker.draft_end)
        if self._draft and self.active_range != original:
            self._draw_range(painter, *original, style, ghost=True)
        band = self._draw_range(painter, start, end, style)
        # The guide always crosses the event lane, including the current range.
        if self._hover is not None and not self._drag:
            painter.setPen(QPen(QColor(colors.muted), .9 * scale, Qt.PenStyle.DotLine))
            y = self.y_for(self._hover)
            painter.drawLine(QPointF(self.gutter - 4 * scale, y), QPointF(self.width(), y))
        for value, label in labels:
            actual_y = self.y_for(value)
            painter.setPen(QPen(QColor(style.outline), .8 * scale))
            painter.drawLine(QPointF(label.right() + 2 * scale, label.center().y()),
                             QPointF(self.gutter, actual_y))
            painter.drawLine(QPointF(self.gutter, actual_y), QPointF(band.left(), actual_y))
            painter.fillRect(label, QColor(colors.background))
            caption = '24:00' if self._minute_for(value) == 1440 else self.preferences.format_time(value)
            text(painter, label, caption, QColor(colors.ink), 8, True,
                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bracket_x = band.right() + 6 * scale
        painter.setPen(QPen(QColor(colors.muted), scale))
        painter.drawLine(QPointF(bracket_x, band.top()), QPointF(bracket_x, band.bottom()))
        for y in (band.top(), band.bottom()):
            painter.drawLine(QPointF(bracket_x - 2 * scale, y), QPointF(bracket_x + 2 * scale, y))
        info_x = bracket_x + 6 * scale
        duration = format_event_duration(start, end, duration_spec=self.active_duration_spec, floating_time=self.picker.floating_time)
        # Long duration captions can wrap instead of reserving an empty column.
        info = QRectF(info_x, max(0, min(self.height() - 36 * scale, band.center().y() - 18 * scale)),
                      self.width() - info_x, 36 * scale)
        text(painter, info, duration, QColor(colors.ink), 8, True,
             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextWordWrap))
        for continued, y, direction in ((self.continues_before, self.axis_top + 2 * scale, -1),
                                        (self.continues_after, self.axis_bottom - 2 * scale, 1)):
            if continued:
                x = band.center().x()
                painter.setPen(QPen(QColor(style.outline), 1.4 * scale))
                painter.drawLine(QPointF(x - 5 * scale, y - direction * 4 * scale), QPointF(x, y + direction * scale))
                painter.drawLine(QPointF(x, y + direction * scale), QPointF(x + 5 * scale, y - direction * 4 * scale))
        painter.end()

    def _valid_preview(self, draft):
        validate_datetime(draft[0], all_day=self.picker.all_day, floating_time=self.picker.floating_time)
        validate_datetime(draft[1], all_day=self.picker.all_day, floating_time=self.picker.floating_time)
        validate_range_order(*draft, all_day=self.picker.all_day, allow_point=self.picker._allow_point, floating_time=self.picker.floating_time)
        return draft

    def _set_preview(self, calculate):
        try:
            self._draft, self._invalid = self._valid_preview(calculate()), None
        except (ValueError, OverflowError) as exc:
            self._draft, self._invalid = None, str(exc)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        self._hover = self._draft = self._invalid = None
        self._moved = False
        if point.x() <= self.timeline_right:
            kind = 'timeline'
        else:
            handles = [(name, handle) for name, handle in (('start', self.start_handle()), ('end', self.end_handle()))
                       if not handle.isEmpty() and handle.contains(point)]
            target = min(handles, key=lambda item: (abs(point.y() - item[1].center().y()),
                                                    item[0] != self.picker.endpoint))[0] if handles else None
            kind = target
            if kind is None and self.band_rect().contains(point):
                kind = 'move' if self.picker.same_day else self.picker.endpoint
        if kind is None:
            return
        minute = self._raw_minute_at(point)
        self._drag = _RangeDrag(kind, point, point, minute, minute, self.picker.draft_start, self.picker.draft_end)
        if kind == 'timeline':
            self._update_drag()

    def mouseMoveEvent(self, event):
        point = event.position()
        if not self._drag:
            self._hover = self.minute_at(point, endpoint=self.picker.endpoint == 'end') if point.x() <= self.timeline_right else None
            if self._hover is not None:
                self._set_preview(lambda: self.proposed_range(self.time_at(self._hover)))
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self._draft = self._invalid = None
                self.setCursor(Qt.CursorShape.SizeVerCursor if self.start_handle().contains(point) or self.end_handle().contains(point)
                               else (Qt.CursorShape.OpenHandCursor if self.picker.same_day else Qt.CursorShape.SizeVerCursor)
                               if self.band_rect().contains(point)
                               else Qt.CursorShape.ArrowCursor)
                self.update()
            return
        drag = self._drag
        drag.pointer_minute += (point.y() - drag.last.y()) * (self.last_minute - self.first_minute) / self.axis_height
        drag.last = point
        self._moved |= (point - drag.origin).manhattanLength() >= 1
        if self._moved:
            self._update_drag()
            if self._pan_direction():
                if not self._pan_timer.isActive():
                    self._pan_timer.start()
            else:
                self._pan_timer.stop()

    def _update_drag(self):
        drag = self._drag
        if drag is None:
            return
        def calculate():
            shift = drag.pointer_minute - drag.initial_minute
            if drag.kind == 'timeline':
                minute = max(0, min(1440 if self.picker.endpoint == 'end' else 1440 - self.STEP,
                                    self._snap(drag.pointer_minute)))
                return self.proposed_range(self.time_at(minute), start=drag.start, end=drag.end)
            if drag.kind == 'move':
                lower = math.ceil(-self._minute_for(drag.start) / self.STEP) * self.STEP
                upper = math.floor((1440 - self._minute_for(drag.end)) / self.STEP) * self.STEP
                delta = timedelta(minutes=max(lower, min(upper, self._snap(shift))))
                return self.picker.relocate(drag.start, drag.end, (drag.start + delta).replace(fold=drag.start.fold))
            original = drag.start if drag.kind == 'start' else drag.end
            minute = max(0, min(1440 if drag.kind == 'end' else 1440 - self.STEP,
                                self._snap(self._minute_for(original) + shift)))
            value = self.time_at(minute).replace(fold=original.fold)
            return (value, drag.end) if drag.kind == 'start' else self.picker.end_pair(
                value, start=drag.start, end=drag.end)
        self._set_preview(calculate)

    def _pan_direction(self):
        if not self._drag:
            return 0
        y, edge = self._drag.last.y(), 13 * self.scale
        return (-1 if y < self.axis_top + edge and self.first_minute > 0 else
                1 if y > self.axis_bottom - edge and self.last_minute < 1440 else 0)

    def _pan_at_edge(self):
        direction = self._pan_direction()
        if not direction or not self.isVisible():
            self._pan_timer.stop()
            return
        span = self.last_minute - self.first_minute
        first = max(0, min(1440 - span, self.first_minute + direction * 15))
        shift = first - self.first_minute
        self.first_minute, self.last_minute = first, first + span
        self._drag.pointer_minute += shift
        self._moved = True
        self._update_drag()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        draft, invalid, spec = self._draft, self._invalid, self.active_duration_spec
        self._pan_timer.stop()
        self._drag = self._draft = self._invalid = self._hover = None
        if invalid:
            self.picker.error.setText(invalid)
            self.picker.error.show()
        elif draft:
            self.picker.set_range(*draft, duration_spec=spec)
        self.refresh()

    def leaveEvent(self, event):
        if not self._drag:
            self._hover = self._draft = self._invalid = None
            self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._pan_timer.stop()
        self._drag = self._draft = self._invalid = self._hover = None
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = 1 if event.modifiers() & Qt.KeyboardModifier.AltModifier else 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else self.STEP
            delta = timedelta(minutes=amount * (-1 if event.key() == Qt.Key.Key_Up else 1))
            try:
                if self.picker.endpoint == 'start':
                    value = (self.picker.draft_start + delta).replace(fold=self.picker.draft_start.fold)
                    self.picker.set_start(value, preserve_duration=self.picker.same_day or bool(self.picker.duration_spec))
                else:
                    self.picker.set_end((self.picker.draft_end + delta).replace(fold=self.picker.draft_end.fold))
            except (ValueError, OverflowError) as exc:
                self.picker.error.setText(str(exc))
                self.picker.error.show()
            event.accept()
        else:
            super().keyPressEvent(event)
