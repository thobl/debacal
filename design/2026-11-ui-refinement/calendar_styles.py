"""Disposable calendar refinements for the second design review.

Install these subclasses before constructing the synthetic workspace. Production
files, calendar data, and event-card styling are deliberately left untouched.
"""
from datetime import time, timedelta
import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from debacal import calendar_widgets as cw
from debacal import import_preview
from debacal import workspace_chrome


GRID = QColor('#e0e5e8')
BACKGROUND = QColor('#ffffff')
INK = QColor('#27313a')
MUTED = QColor('#596570')
ACCENT = QColor('#475f52')
# Time's current position is a semantic marker, so it keeps one deliberate
# accent distinct from sources and Today. It is quieter than the old salmon.
NOW_COLOR = QColor('#9b5f56')
TODAY_FILL = QColor('#edf2ef')
WEEKEND_FILL = QColor('#fafbfc')
OUTSIDE_MONTH = QColor('#f5f7f8')
WEEKDAY_SIZE = 9


def _column_grid(painter, gutter, width, days, height):
    """The same boundary geometry in the header, all-day lane, and hours."""
    painter.setPen(QPen(GRID, 1))
    column = (width - gutter) / max(1, len(days))
    for index in range(len(days) + 1):
        x = gutter + index * column
        painter.drawLine(QPointF(x, 0), QPointF(x, height))


class ReviewDayHeader(cw.DayHeader):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(64)
        self.all_day_top = 64

    def set_preferences(self, preferences):
        super().set_preferences(preferences)
        self.all_day_top = math.ceil(64 * cw.display_scale(self))
        self.setFixedHeight(self.all_day_top)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        if not self.days:
            painter.end()
            return
        width = (self.width() - self.gutter) / len(self.days)
        scale = cw.display_scale(self)
        today = cw.now().date()
        week = self.week_rect()
        if week is not None:
            cw.text(painter, QRectF(week.left(), 8 * scale, week.width(), 18 * scale),
                    'Week', MUTED, WEEKDAY_SIZE, align=Qt.AlignmentFlag.AlignCenter)
            cw.text(painter, QRectF(week.left(), 29 * scale, week.width(), 25 * scale),
                    str(self.preferences.week_number(self.days[0])), ACCENT, 13, True,
                    Qt.AlignmentFlag.AlignCenter)
        for index, day in enumerate(self.days):
            left = self.gutter + index * width
            selected = self.navigation_active and index == self.navigation_day
            if day.weekday() >= 5:
                painter.fillRect(QRectF(left, 0, width, self.height()), WEEKEND_FILL)
            if day == today or selected:
                painter.setPen(QPen(ACCENT, 1.5) if selected else Qt.PenStyle.NoPen)
                painter.setBrush(TODAY_FILL)
                painter.drawRoundedRect(QRectF(left + 6, 5 * scale, width - 12, 52 * scale), 4, 4)
            cw.text(painter, QRectF(left + 11, 8 * scale, width - 20, 19 * scale),
                    self.preferences.weekday_name(day).upper(), ACCENT if day == today else MUTED,
                    WEEKDAY_SIZE, True)
            cw.text(painter, QRectF(left + 11, 29 * scale, width - 20, 27 * scale),
                    str(day.day), ACCENT if day == today else INK, 17, True)
            if self.navigation_active:
                cw.badge(painter, QRectF(left + width - 31, 10 * scale, 21, 21), str(index + 1), selected)
        _column_grid(painter, self.gutter, self.width(), self.days, self.height())
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(QPointF(self.gutter, self.height() - .5), QPointF(self.width(), self.height() - .5))
        painter.end()


class ReviewAllDayCanvas(cw.AllDayCanvas):
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        if not self.days:
            painter.end()
            return
        width = (self.width() - self.gutter) / len(self.days)
        for index, day in enumerate(self.days):
            if day.weekday() >= 5:
                painter.fillRect(QRectF(self.gutter + index * width, 0, width, self.height()), WEEKEND_FILL)
        _column_grid(painter, self.gutter, self.width(), self.days, self.height())
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(QPointF(self.gutter, self.height() - .5), QPointF(self.width(), self.height() - .5))
        scale = cw.display_scale(self)
        cw.text(painter, QRectF(3, 3, 55 * scale, 24 * scale), 'ALL DAY', MUTED, 8.6)
        self.hits, self.confirm_hits = [], []
        for rect, item, _number in self.rects():
            self._paint_bar(painter, rect, item)
            self.hits.append((rect, item))
        previews = [self.ghost] if not self.preview_events and self.ghost is not None else self.preview_events
        pad = 4 * scale
        slots = max(1, (self.height() - 2 * pad) // self.lane_height)
        for rect, item, _number in self.rects(previews):
            rect.moveTop(pad + (int((rect.top() - pad) / self.lane_height) % slots) * self.lane_height)
            self._paint_bar(painter, rect, item, preview=True)
        self.paint_copy_badge(painter)
        painter.end()


class ReviewCalendarCanvas(cw.CalendarCanvas):
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BACKGROUND)
        self.confirm_hits = []
        if not self.days:
            painter.end()
            return
        width = (self.width() - self.gutter) / len(self.days)
        scale = cw.display_scale(self)
        for index, day in enumerate(self.days):
            if day.weekday() >= 5:
                painter.fillRect(QRectF(self.gutter + index * width, 0, width, self.height()), WEEKEND_FILL)
            if self.show_numbers and index == self.navigation_day:
                painter.fillRect(QRectF(self.gutter + index * width, 0, width, self.height()), TODAY_FILL)
        _column_grid(painter, self.gutter, self.width(), self.days, self.height())
        if not self.fixed_all_day:
            cw.text(painter, QRectF(4, 4, 56 * scale, 28 * scale), 'ALL DAY', MUTED, 8.6)
            painter.setPen(QPen(GRID, 1))
            painter.drawLine(0, self.time_top, self.width(), self.time_top)
        for hour in range(self.start_hour, self.end_hour + 1):
            top = self.time_top + (hour - self.start_hour) * self.hour_height
            painter.setPen(QPen(GRID, 1))
            painter.drawLine(QPointF(self.gutter, top), QPointF(self.width(), top))
            if hour and hour < 24:
                label = self.preferences.format_time(time(hour))
                cw.text(painter, QRectF(2, max(0, top - 11 * scale), 54 * scale, 22 * scale),
                        label, MUTED, 8.6, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if hour < self.end_hour:
                painter.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(self.gutter, top + self.hour_height / 2),
                                 QPointF(self.width(), top + self.hour_height / 2))
        self.hits = self.rects()
        preview_rects = self.rects(self.preview_events)
        now_point = None
        current = cw.now()
        if current.date() in self.days and self.start_hour <= current.hour < self.end_hour:
            index = self.days.index(current.date())
            top = self.time_top + (current.hour + current.minute / 60 - self.start_hour) * self.hour_height
            now_point = QPointF(self.gutter + index * width, top)
            # The inherited B cards use translucent tints. Clip their shapes
            # out so the line cannot show through even when painted first.
            visible = QPainterPath()
            visible.addRect(QRectF(self.rect()))
            cards = QPainterPath()
            for rect, _item, _number in self.hits + preview_rects:
                if rect.top() <= top <= rect.bottom():
                    cards.addRoundedRect(rect.adjusted(-.5, -.5, .5, .5), 5, 5)
            painter.save()
            painter.setClipPath(visible.subtracted(cards))
            painter.setPen(QPen(NOW_COLOR, 1.5))
            painter.drawLine(now_point, QPointF(self.gutter + (index + 1) * width, top))
            painter.restore()
        # Cards cover the current-time line so it never strikes through a title.
        # The dot stays on top at the column edge, preserving its exact position.
        for rect, item, number in self.hits:
            self.paint_event(painter, rect, item, number)
        for rect, item, number in preview_rects:
            self.paint_event(painter, rect, item, number, preview=True)
        if now_point is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(NOW_COLOR)
            painter.drawEllipse(now_point, 3, 3)
        if self.ghost and not self.preview_events:
            index, start_min, end_min = self.ghost
            if 0 <= index < len(self.days):
                rect = QRectF(self.gutter + index * width + 3,
                              self.time_top + (start_min / 60 - self.start_hour) * self.hour_height,
                              width - 6, max(12, (end_min - start_min) / 60 * self.hour_height))
                creating = self.drag and self.drag[0] == 'create'
                item = self.drag[1] if self.drag and not creating else None
                calendar = self.calendars.get(item.calendar_id) if item is not None else None
                color = self.creation_color() if creating else QColor(calendar.color) if calendar else QColor(ACCENT)
                fill = QColor(color)
                fill.setAlpha(45)
                painter.setPen(QPen(color, 2, Qt.PenStyle.DashLine))
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 5, 5)
                if self.drag and self.drag[0] != 'create':
                    label = {'single': 'One event', 'group': 'All events', 'future': 'This and following'}[self.scope]
                    cw.text(painter, rect.adjusted(5, 0, -5, 0), label, color, 8.6, True)
        self.paint_time_feedback(painter)
        self.paint_copy_badge(painter)
        painter.end()


def _paint_month(self, _event):
    painter = QPainter(self)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(self.rect(), BACKGROUND)
    self.confirm_hits = []
    scale = cw.display_scale(self)
    gutter = self.week_gutter
    width, height = (self.width() - gutter) / 7, (self.height() - self.header_height) / 6
    day_label_height = min(25 * scale, self.month_row_metrics()[0] - 2 * scale)
    if gutter:
        cw.text(painter, QRectF(0, 0, gutter, self.header_height), 'Wk', MUTED, 8.6, True,
                Qt.AlignmentFlag.AlignCenter)
        for row in range(6):
            rect = self.week_rect(row)
            cw.text(painter, QRectF(rect.left(), rect.top() + 3 * scale, rect.width(), 25 * scale),
                    str(self.preferences.week_number(self.start + timedelta(days=7 * row))), ACCENT, 8.6, True,
                    Qt.AlignmentFlag.AlignCenter)
    today = cw.now().date()
    for index in range(7):
        day = self.start + timedelta(days=index)
        if day.weekday() >= 5:
            painter.fillRect(QRectF(gutter + index * width, 0, width, self.height()), WEEKEND_FILL)
        cw.text(painter, QRectF(gutter + index * width + 11, 0, width - 18, self.header_height),
                self.preferences.weekday_name(day).upper(), MUTED, WEEKDAY_SIZE, True)
    for index in range(42):
        day = self.start + timedelta(days=index)
        left, top = gutter + index % 7 * width, self.header_height + index // 7 * height
        rect = QRectF(left, top, width, height)
        if day.month != self.month:
            painter.fillRect(rect, OUTSIDE_MONTH)
        if day == today:
            painter.fillRect(rect, TODAY_FILL)
        painter.setPen(QPen(GRID, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        cw.text(painter, QRectF(left + 10, top + 3 * scale, width - 18, day_label_height), str(day.day),
                ACCENT if day == today else INK, 10, True)
    # Extend the same column boundaries through the weekday strip.
    painter.setPen(QPen(GRID, 1))
    for index in range(8):
        left = gutter + index * width
        painter.drawLine(QPointF(left, 0), QPointF(left, self.header_height))
    commands, hits = self.month_layout()
    self.hits = [(QRectF(rect), item) for rect, item in hits]
    for command in commands:
        if command[0] == 'event':
            _kind, rect, item, preview, before, after = command
            self._paint_month_event(painter, rect, item, preview=preview, before=before, after=after)
        else:
            _kind, rect, label, size, align = command
            cw.text(painter, rect, label, MUTED, size, align=align)
    self.paint_copy_badge(painter)
    painter.end()


class ReviewMonthCanvas(cw.MonthCanvas):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_creation_hints = False

    paintEvent = _paint_month


class ReviewImportMonth(import_preview._ImportMonth):
    """The existing inclusion gestures and overlays, on the matching grid."""
    def month_row_metrics(self):
        scale = cw.display_scale(self)
        return 23 * scale, 19 * scale, 17 * scale, 2 * scale

    def paintEvent(self, event):
        _paint_month(self, event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = cw.display_scale(self)
        size = 10 * scale
        for offset in range(42):
            day = self.start + timedelta(days=offset)
            state = self.date_check_state(day)
            if state is None:
                continue
            cell = self.day_rect(day)
            box = QRectF(cell.right() - size - 5 * scale, cell.top() + 4 * scale, size, size)
            painter.setPen(QPen(QColor('#97a69e'), 1))
            painter.setBrush(BACKGROUND)
            painter.drawRoundedRect(box, 2, 2)
            painter.setPen(QPen(ACCENT, max(1.2, 1.2 * scale)))
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
            painter.setPen(QPen(ACCENT, 1, Qt.PenStyle.DotLine))
            painter.drawRect(self.day_rect(self._focus_day).adjusted(2, 2, -2, -2))
        painter.end()


def install():
    """Use real subclasses so Qt binds their virtual methods during creation."""
    workspace_chrome.DayHeader = ReviewDayHeader
    workspace_chrome.CalendarCanvas = ReviewCalendarCanvas
    workspace_chrome.AllDayCanvas = ReviewAllDayCanvas
    workspace_chrome.MonthCanvas = ReviewMonthCanvas
    import_preview._ImportMonth = ReviewImportMonth


class _ViewportAlignment(QObject):
    """Reserve the timed scrollbar's gutter in the two pinned surfaces."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.queued = False
        window.scroll.viewport().installEventFilter(self)
        window.scroll.verticalScrollBar().rangeChanged.connect(self.queue)
        self.sync()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.queue()
        return False

    def queue(self, *_args):
        if not self.queued:
            self.queued = True
            QTimer.singleShot(0, self.sync)

    def sync(self):
        self.queued = False
        width = self.window.scroll.viewport().width()
        # Maximum widths reserve the trailing gutter without increasing the
        # window's minimum width when a wide mockup is resized to narrow.
        for surface in (self.window.header, self.window.all_day_scroll):
            if surface.maximumWidth() != width:
                surface.setMaximumWidth(width)
        self.window.header.update()
        self.window.all_day_canvas.update()


def apply(window):
    """Keep the three calendar surfaces flush and free of scroll-area frames."""
    for area in (window.scroll, window.all_day_scroll):
        area.setStyleSheet(area.styleSheet() + 'QScrollArea { border: 0; background: #ffffff; }')
    window._review_grid_alignment = _ViewportAlignment(window)
    window.header.update()
    window.all_day_canvas.update()
    window.canvas.update()
    window.month.update()


def apply_dialog(dialog):
    dialog.setStyleSheet(dialog.styleSheet() + 'QScrollArea { border: 0; background: #ffffff; }')
