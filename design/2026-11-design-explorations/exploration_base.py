"""Shared synthetic fixture; independent visual hypotheses, no app changes."""
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / '2026-11-ui-review'))
sys.path.insert(0, str(HERE.parent / '2026-11-ui-refinement'))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QTextCharFormat
from mockup_base import NOW, make_sample_window, settle, install_paint_palette
import calendar_styles as calendar
import header_styles
import sidebar_styles
from debacal import calendar_widgets as cw
from debacal import workspace_chrome
from debacal.models import Event


SPECS = {
    'sources': dict(name='Sources & agenda', weekend_text='#8b6254', weekend_fill='#f8f1eb',
                    header_grid=False, weekday_size=9, month_weekday_size=9, uppercase=True, all_day='integrated'),
    'shared': dict(name='Shared list language', weekend_text='#546c83', weekend_fill='#eef3f8',
                   header_grid=True, weekday_size=9, month_weekday_size=9, uppercase=True, all_day='persistent'),
    'type': dict(name='Typographic', weekend_text='#925866', weekend_fill='#ffffff',
                 header_grid=False, weekday_size=9.5, month_weekday_size=8.75, uppercase=False, all_day='gestures'),
}
SPEC = SPECS['sources']


class ExploreDayHeader(calendar.ReviewDayHeader):
    def set_preferences(self, preferences):
        super().set_preferences(preferences)
        if SPEC['all_day'] == 'persistent':
            self.setFixedHeight(round(84 * cw.display_scale(self)))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), calendar.BACKGROUND)
        if not self.days:
            return
        scale = cw.display_scale(self)
        width = (self.width() - self.gutter) / len(self.days)
        today = cw.now().date()
        week = self.week_rect()
        if week is not None:
            cw.text(painter, QRectF(week.left(), 8 * scale, week.width(), 18 * scale),
                    'Week', calendar.MUTED, 9, align=Qt.AlignmentFlag.AlignCenter)
            cw.text(painter, QRectF(week.left(), 29 * scale, week.width(), 25 * scale),
                    str(self.preferences.week_number(self.days[0])), calendar.ACCENT, 13, True,
                    Qt.AlignmentFlag.AlignCenter)
        for index, day in enumerate(self.days):
            left = self.gutter + index * width
            weekend = day.weekday() >= 5
            selected = self.navigation_active and index == self.navigation_day
            if weekend:
                painter.fillRect(QRectF(left, 0, width, self.height()), calendar.WEEKEND_FILL)
            if day == today or selected:
                painter.setPen(QPen(calendar.ACCENT, 1.5) if selected else Qt.PenStyle.NoPen)
                painter.setBrush(calendar.TODAY_FILL)
                painter.drawRoundedRect(QRectF(left + 6, 5 * scale, width - 12, 52 * scale), 4, 4)
            label = self.preferences.weekday_name(day)
            if SPEC['uppercase']:
                label = label.upper()
            color = calendar.ACCENT if day == today else QColor(SPEC['weekend_text']) if weekend else calendar.MUTED
            cw.text(painter, QRectF(left + 11, 8 * scale, width - 20, 19 * scale),
                    label, color, SPEC['weekday_size'], True)
            date_color = color if weekend else calendar.ACCENT if day == today else calendar.INK
            cw.text(painter, QRectF(left + 11, 29 * scale, width - 20, 27 * scale),
                    str(day.day), date_color, 17, True)
            if self.navigation_active:
                cw.badge(painter, QRectF(left + width - 31, 10 * scale, 21, 21), str(index + 1), selected)
            if SPEC['all_day'] == 'persistent':
                cw.text(painter, QRectF(left + 8, 62 * scale, width - 16, 20 * scale),
                        '+ All day', calendar.ACCENT, 8.6, align=Qt.AlignmentFlag.AlignCenter)
            if not SPEC['header_grid']:
                painter.setPen(QPen(calendar.GRID, 1))
                painter.drawLine(QPointF(left + 8, self.height() - .5),
                                 QPointF(left + width - 8, self.height() - .5))
        if SPEC['header_grid']:
            calendar._column_grid(painter, self.gutter, self.width(), self.days, self.height())
            painter.setPen(QPen(calendar.GRID, 1))
            painter.drawLine(QPointF(self.gutter, self.height() - .5), QPointF(self.width(), self.height() - .5))


class ExploreAllDay(calendar.ReviewAllDayCanvas):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._review_hover_day = None

    def _plus_rect(self, day):
        if day not in self.days:
            return QRectF()
        scale = cw.display_scale(self)
        width = (self.width() - self.gutter) / len(self.days)
        rect = QRectF(self.gutter + self.days.index(day) * width + width - 26 * scale,
                      5 * scale, 22 * scale, 22 * scale)
        return QRectF() if any(box.intersects(rect) for box, _item, _number in self.rects()) else rect

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._review_hover_day = self.day_at(event.position())
        self.update()

    def leaveEvent(self, event):
        self._review_hover_day = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if SPEC['all_day'] == 'integrated' and event.button() == Qt.MouseButton.LeftButton and not self.placement_active:
            day = self.day_at(event.position())
            if not self._plus_rect(day).isEmpty() and self._plus_rect(day).contains(event.position()):
                self.create_all_day_requested.emit(cw.at(day), cw.at(day + calendar.timedelta(days=1)))
                return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if SPEC['all_day'] != 'integrated':
            return
        day = self._review_hover_day or (self.selected_day if self.hasFocus() else None)
        rect = self._plus_rect(day)
        if rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor('#a9bcb0'), 1))
        painter.setBrush(QColor('#f0f5f1'))
        painter.drawRoundedRect(rect, 4, 4)
        cw.text(painter, rect, '+', calendar.ACCENT, 12, align=Qt.AlignmentFlag.AlignCenter)


class ExploreMonth(calendar.ReviewMonthCanvas):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        width = (self.width() - self.week_gutter) / 7
        # Replace only the weekday strip. Event cards and month hit geometry
        # remain identical, so typography/color can be compared honestly.
        for index in range(7):
            day = self.start + calendar.timedelta(days=index)
            left = self.week_gutter + index * width
            weekend = day.weekday() >= 5
            fill = calendar.WEEKEND_FILL if weekend else calendar.BACKGROUND
            painter.fillRect(QRectF(left + 1, 0, width - 2, self.header_height - 1), fill)
            label = self.preferences.weekday_name(day)
            if SPEC['uppercase']:
                label = label.upper()
            color = QColor(SPEC['weekend_text']) if weekend else calendar.MUTED
            cw.text(painter, QRectF(left + 11, 0, width - 18, self.header_height),
                    label, color, SPEC['month_weekday_size'], True)
        if not SPEC['header_grid']:
            # Header labels sit above the matrix rather than inside cells.
            for index in range(8):
                left = self.week_gutter + index * width
                color = calendar.WEEKEND_FILL if index >= 6 else calendar.BACKGROUND
                painter.setPen(QPen(color, 2))
                painter.drawLine(QPointF(left, 0), QPointF(left, self.header_height - 2))


def make_window(direction):
    global SPEC
    SPEC = SPECS[direction]
    install_paint_palette(background='#ffffff', ink='#27313a', muted='#596570',
                          line='#e0e5e8', accent='#475f52', text_scale=1.0)
    calendar.WEEKEND_FILL = QColor(SPEC['weekend_fill'])
    calendar.WEEKDAY_SIZE = SPEC['month_weekday_size']
    calendar.install()
    workspace_chrome.DayHeader = ExploreDayHeader
    workspace_chrome.MonthCanvas = ExploreMonth
    workspace_chrome.AllDayCanvas = ExploreAllDay
    app, store, window, patches = make_sample_window()
    start = NOW.replace(day=2, hour=15, minute=30)
    store.apply('Synthetic recurring example', [Event('weekly-work-review', 'work', 'Weekly work review',
        start, start + calendar.timedelta(minutes=45), recurrence_rule='FREQ=WEEKLY;COUNT=6')])
    window.refresh()
    sidebar_styles.apply(window, variant='open')
    header_styles.apply(window)
    calendar.apply(window)
    window.workspace_header.setObjectName('explorationHeader')
    window.workspace_header.setStyleSheet('QWidget#explorationHeader { background: #ffffff; }')
    # Weekend distinction survives in the overview too, with readable muted
    # color instead of either bright red or no emphasis.
    weekend = QTextCharFormat()
    weekend.setForeground(QColor(SPEC['weekend_text']))
    weekend.setBackground(QColor(SPEC['weekend_fill']) if direction != 'type' else QColor('#f6f7f8'))
    for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        window.mini.calendar.setWeekdayTextFormat(day, weekend)
    return app, store, window, patches
