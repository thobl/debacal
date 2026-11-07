"""A compact, interactive month preview of a series or candidate time slots."""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .calendar_widgets import at, text, display_scale, paint_colors, WEEKDAY_SIZE
from .event_style import event_colors
from .theme import scheme_for
from .preferences import Preferences
from .calendar_appearance import weekend_text, weekend_fill
from .recurrence import expand_events, key_datetime, recurrence_key, recurrence_set, overlaps
from .timeutil import local_zone, now


class SeriesPreview(QWidget):
    """Expand only the displayed month; rebuilding editor values keeps its page.

    ``date_toggled`` requests an edit from the owner; the owner decides whether
    to add an RDATE or EXDATE, then calls ``set_events`` with the new values.
    The preview never mutates the supplied event objects.
    """
    date_toggled = Signal(object)
    date_clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preferences = Preferences()
        self.masters = []
        self.visible_events = []
        self.current_month = now().date().replace(day=1)
        self.grid_start = self.preferences.week_start(self.current_month)
        self.date_editing = False
        self.error = ''
        self.total_occurrences = None
        self._initialized = False
        self._by_day = {}
        self.anchor_dates, self.added_dates, self.excluded_dates = set(), set(), set()
        self.setMinimumHeight(325)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        toolbar = QHBoxLayout()
        self.previous_button = QPushButton('‹')
        self.previous_button.setToolTip('Previous month')
        self.previous_button.setFixedWidth(32)
        self.previous_button.setStyleSheet('padding: 3px;')
        self.next_button = QPushButton('›')
        self.next_button.setToolTip('Next month')
        self.next_button.setFixedWidth(32)
        self.next_button.setStyleSheet('padding: 3px;')
        self.month_label = QLabel()
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.first_button = QPushButton('First date')
        self.previous_button.clicked.connect(lambda: self.navigate_month(-1))
        self.next_button.clicked.connect(lambda: self.navigate_month(1))
        self.first_button.clicked.connect(self.show_first_date)
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.month_label, 1)
        toolbar.addWidget(self.next_button)
        toolbar.addWidget(self.first_button)
        outer.addLayout(toolbar)
        self.grid = _PreviewGrid(self)
        outer.addWidget(self.grid, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setProperty('muted', True)
        outer.addWidget(self.summary)
        self.instructions = QLabel()
        self.instructions.setWordWrap(True)
        self.instructions.setProperty('muted', True)
        outer.addWidget(self.instructions)
        self._rebuild()

    def set_events(self, masters_or_events, preferences=None):
        self.masters = list(masters_or_events)
        if preferences is not None:
            self.preferences = preferences
        if self.masters and not self._initialized:
            self.current_month = min(event.start.astimezone(local_zone()).date() for event in self.masters).replace(day=1)
            self._initialized = True
        self._rebuild()

    def enable_date_editing(self, enabled):
        self.date_editing = bool(enabled)
        self.grid.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self._update_instructions()

    def navigate_month(self, delta):
        value = self.current_month.year * 12 + self.current_month.month - 1 + delta
        year, month = divmod(value, 12)
        if not 1 <= year <= 9999:
            return
        self.show_month(date(year, month + 1, 1))

    def show_month(self, day):
        self.current_month = day.replace(day=1)
        self._initialized = True
        self._rebuild()

    def show_first_date(self):
        if self.masters:
            self.show_month(min(event.start.astimezone(local_zone()).date() for event in self.masters))

    def events_on(self, day):
        return self._by_day.get(day, [])

    def _update_instructions(self):
        legend = '● First occurrence · + Added date · × Skipped date'
        self.instructions.setText(('Click a date to add or remove an occurrence.\n' if self.date_editing else '') + legend)

    def _rebuild(self):
        self.grid_start = self.preferences.week_start(self.current_month)
        self.month_label.setText(self.preferences.format_month(self.current_month))
        scale = display_scale(self)
        self.setMinimumHeight(round(325 * scale))
        self.grid.header_height = round(23 * scale)
        self.grid.setMinimumHeight(round(235 * scale))
        self.error = ''
        self.total_occurrences = None
        self.anchor_dates, self.added_dates, self.excluded_dates = set(), set(), set()
        try:
            for master in self.masters:
                self.anchor_dates.add(master.start.astimezone(local_zone()).date())
                self.added_dates.update(key_datetime(key, master).astimezone(local_zone()).date() for key in master.recurrence_dates)
                self.excluded_dates.update(key_datetime(key, master).astimezone(local_zone()).date() for key in master.excluded_dates)
            self.visible_events = expand_events(self.masters, at(self.grid_start), at(self.grid_start + timedelta(days=42)), limit=2000)
            if self.masters and not any(master.recurrence_rule for master in self.masters):
                self.total_occurrences = sum(len(
                    {recurrence_key(value, master.all_day) for value in recurrence_set(master)}
                    | (set(master.recurrence_overrides) - set(master.excluded_dates))) for master in self.masters)
        except (ValueError, OverflowError) as exc:
            self.visible_events = []
            self.error = str(exc)
        self._by_day = {}
        for offset in range(42):
            day = self.grid_start + timedelta(days=offset)
            start, end = at(day), at(day + timedelta(days=1))
            self._by_day[day] = [event for event in self.visible_events if overlaps(event, start, end)]
        count = sum(event.start.astimezone(local_zone()).year == self.current_month.year
                    and event.start.astimezone(local_zone()).month == self.current_month.month for event in self.visible_events)
        number = f'{count} of {self.total_occurrences}' if self.total_occurrences is not None else str(count)
        self.summary.setText('Preview unavailable: ' + self.error if self.error else
                             number + ' occurrence' + ('' if count == 1 and self.total_occurrences in (None, 1) else 's')
                             + ' this month · use ‹ › to inspect other months')
        self.first_button.setEnabled(bool(self.masters))
        self._update_instructions()
        self.grid.update()


class _PreviewGrid(QWidget):
    header_height = 23

    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.setMinimumHeight(235)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def cell_rect(self, day):
        offset = (day - self.preview.grid_start).days
        if not 0 <= offset < 42:
            return QRectF()
        width, height = self.width() / 7, (self.height() - self.header_height) / 6
        return QRectF((offset % 7) * width, self.header_height + (offset // 7) * height, width, height)

    def date_at(self, point):
        if point.y() < self.header_height or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        width, height = self.width() / 7, (self.height() - self.header_height) / 6
        offset = min(5, int((point.y() - self.header_height) / height)) * 7 + min(6, int(point.x() / width))
        return self.preview.grid_start + timedelta(days=offset)

    def paintEvent(self, _):
        preview = self.preview
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        painter.fillRect(self.rect(), colors.background)
        width = self.width() / 7
        scale = display_scale(self)
        for index in range(7):
            day = preview.grid_start + timedelta(days=index)
            shade = weekend_fill(day, preview.preferences, colors)
            if shade is not None:
                painter.fillRect(QRectF(index * width, 0, width, self.header_height), shade)
            text(painter, QRectF(index * width, 0, width, self.header_height),
                 preview.preferences.weekday_name(day).upper(), weekend_text(day, preview.preferences, colors, colors.muted),
                 WEEKDAY_SIZE, True, Qt.AlignmentFlag.AlignCenter)
        for offset in range(42):
            day = preview.grid_start + timedelta(days=offset)
            rect = self.cell_rect(day)
            events = preview.events_on(day)
            outside = day.month != preview.current_month.month
            if outside:
                painter.fillRect(rect, colors.outside_month)
            shade = weekend_fill(day, preview.preferences, colors)
            if shade is not None:
                painter.fillRect(rect, shade)
            if not outside:
                inset = rect.adjusted(2 * scale, scale, -2 * scale, -scale) if shade is not None else rect
                if day in preview.excluded_dates:
                    painter.fillRect(inset, colors.control_surface)
                elif events:
                    painter.fillRect(inset, colors.selection)
            painter.setPen(QPen(colors.line, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            anchor = day in preview.anchor_dates
            text(painter, QRectF(rect.x() + 5, rect.y() + 1, rect.width() - 10, 19 * scale),
                 str(day.day), weekend_text(day, preview.preferences, colors,
                                            colors.muted if outside else colors.accent if events else colors.ink), 9, anchor)
            marker = '●' if anchor else '×' if day in preview.excluded_dates else '+' if day in preview.added_dates else ''
            if marker:
                text(painter, QRectF(rect.right() - 20 * scale, rect.y() + 1, 16 * scale, 19 * scale), marker,
                     colors.muted if day in preview.excluded_dates else colors.accent, 9, True, Qt.AlignmentFlag.AlignCenter)
            capacity = max(1, int((rect.height() - 22 * scale) / (16 * scale)))
            for index, event in enumerate(events[:capacity]):
                event_rect = QRectF(rect.x() + 3, rect.y() + 21 * scale + index * 16 * scale, rect.width() - 6, 14 * scale)
                style = event_colors(scheme_for(preview).accent, preview, alpha=38 if event.candidate else 52)
                painter.setPen(QPen(QColor(style.outline), 1,
                                     Qt.PenStyle.DashLine if event.candidate else Qt.PenStyle.SolidLine))
                painter.setBrush(QColor(style.fill))
                painter.drawRoundedRect(event_rect, 3, 3)
                label = 'All day' if event.all_day else preview.preferences.format_time(event.start.astimezone(local_zone()))
                painter.save()
                painter.setClipRect(event_rect)
                text(painter, event_rect, label, QColor(style.ink), 7, False, Qt.AlignmentFlag.AlignCenter)
                painter.restore()
            if len(events) > capacity:
                text(painter, QRectF(rect.right() - 27 * scale, rect.y() + 1, 24 * scale, 19 * scale), '+' + str(len(events) - capacity), colors.muted, 7)
            if day in preview.excluded_dates and not events:
                text(painter, QRectF(rect.x() + 3, rect.y() + 21 * scale, rect.width() - 6, 15 * scale), 'skipped', colors.muted, 7)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.date_at(event.position())
        if day is not None:
            self.preview.date_clicked.emit(day)
            if self.preview.date_editing:
                self.preview.date_toggled.emit(day)

    def mouseMoveEvent(self, event):
        day = self.date_at(event.position())
        if day is None:
            self.setToolTip('')
            return
        preferences = self.preview.preferences
        lines = [preferences.format_date(day, weekday=True)]
        for item in self.preview.events_on(day)[:10]:
            lines.append(preferences.format_time(item.start.astimezone(local_zone())) + ' · ' + item.title)
        if day in self.preview.excluded_dates:
            lines.append('This date is excluded from the series.')
        self.setToolTip('\n'.join(lines))
