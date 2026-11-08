"""Calendar-presence studies derived from the native year-view geometry.

This module is a design prototype. It does not modify the production YearView.
"""
from datetime import date

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from debacal.calendar_appearance import weekend_fill, weekend_text
from debacal.calendar_widgets import paint_colors
from debacal.theme import corner_radius
from debacal.timeutil import now
from debacal.year_view import YearView


OPTIONS = {
    'tracks': ('A', 'Calendar tracks',
               'Wide color tracks keep each calendar easy to recognize while the date stays separate.'),
    'tiles': ('B', 'Calendar tiles',
              'Larger tiles share the day with its number; dense days use a final +N tile.'),
    'tints': ('C', 'Tinted day cells',
              'Soft calendar-color segments fill the day, with a strong color edge for identification.'),
}


class YearPresenceStudy(YearView):
    """Show distinct visible calendars on a day, never individual event targets."""
    def __init__(self, option='tracks', parent=None):
        self.option = option
        super().__init__(parent)
        self._default_tooltip = 'Open a month or week from its heading; double-click a day to create an all-day event.'
        self.setToolTip(self._default_tooltip)

    def set_data(self, year, events, calendars):
        calendars = list(calendars)
        visible = {calendar.id for calendar in calendars if calendar.enabled and calendar.visible}
        self.source_order = {calendar.id: index for index, calendar in enumerate(calendars)}
        super().set_data(year, [event for event in events if event.calendar_id in visible], calendars)

    def _ensure_geometry(self):
        super()._ensure_geometry()
        # The calendar presence is an overview: no marker is an event target.
        self._marker_hits = []
        self.hits = []

    def calendars_on_day(self, day):
        offset = (day - self._first).days
        if not 0 <= offset < len(self._summaries):
            return ()
        return tuple(sorted(self._summaries[offset], key=lambda identity: self.source_order[identity]))

    def _color(self, identity):
        return QColor(self.calendars[identity].color)

    def _fill(self, painter, rect, color, radius=1):
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, radius * self.scale, radius * self.scale)

    def _tracks(self, painter, rect, identities, colors):
        scale = self.scale
        number = QRectF(rect.x() + 3 * scale, rect.y() + scale, 15 * scale, 12 * scale)
        if not identities:
            return number
        area = rect.adjusted(4 * scale, 15 * scale, -4 * scale, -2 * scale)
        maximum = max(1, min(4, int((area.height() + scale) / (4 * scale))))
        visible = identities[:maximum]
        overflow = max(0, len(identities) - maximum)
        count = len(visible)
        gap = scale
        height = min(3 * scale, max(1.5 * scale, (area.height() - gap * (count - 1)) / count))
        for index, identity in enumerate(visible):
            track = QRectF(area.x(), area.y() + index * (height + gap), area.width(), height)
            self._fill(painter, track, self._color(identity), .8)
        if overflow:
            # Keep overflow alongside the date, never outside a short cell.
            overflow_rect = QRectF(rect.right() - 14 * scale, rect.y() + scale, 14 * scale, 10 * scale)
            self._fill(painter, overflow_rect, colors.control_surface, 1.5)
            self._text(painter, overflow_rect, '+' + str(overflow), colors.muted, 6)
        return number

    def _tiles(self, painter, rect, identities, colors):
        scale = self.scale
        if not identities:
            return rect.adjusted(0, 0, 0, -scale)
        number = QRectF(rect.x() + scale, rect.y(), max(15 * scale, rect.width() * .42), rect.height())
        area = QRectF(number.right() + scale, rect.y() + 3 * scale,
                      rect.right() - number.right() - 3 * scale, rect.height() - 6 * scale)
        visible = identities[:4] if len(identities) <= 4 else identities[:2]
        count = len(visible) if len(identities) <= 4 else 4
        columns = 1 if count == 1 else 2
        rows = 1 if count <= 2 else 2
        gap = 2 * scale
        width = min(9 * scale, max(3 * scale, (area.width() - gap * (columns - 1)) / columns))
        height = min(9 * scale, max(3 * scale, (area.height() - gap * (rows - 1)) / rows))
        top = area.center().y() - (height * rows + gap * (rows - 1)) / 2
        left = area.center().x() - (width * columns + gap * (columns - 1)) / 2
        for index in range(count):
            row, column = divmod(index, columns)
            tile = QRectF(left + column * (width + gap), top + row * (height + gap), width, height)
            if index < len(visible):
                self._fill(painter, tile, self._color(visible[index]), 1.5)
            elif index == len(visible):
                tile = QRectF(left, top + height + gap, width * 2 + gap, height)
                self._fill(painter, tile.adjusted(0, -scale, 0, scale), colors.control_surface, 1.5)
                self._text(painter, tile.adjusted(0, -2 * scale, 0, 2 * scale),
                           '+' + str(len(identities) - len(visible)), colors.muted, 6)
        return number

    def _tints(self, painter, rect, identities, colors):
        scale = self.scale
        area = rect.adjusted(1.5 * scale, 1.5 * scale, -1.5 * scale, -1.5 * scale)
        if identities:
            visible = identities[:4] if len(identities) <= 4 else identities[:3]
            overflow = len(identities) - len(visible)
            count = len(visible) + bool(overflow)
            for index in range(count):
                segment = QRectF(area.x() + index * area.width() / count, area.y(), area.width() / count, area.height())
                if index < len(visible):
                    source = self._color(visible[index])
                    tint = QColor(source)
                    tint.setAlpha(43 if self.preferences.appearance_mode == 'light' else 58)
                    self._fill(painter, segment, tint, 1)
                    self._fill(painter, QRectF(segment.x(), segment.bottom() - 3 * scale, segment.width(), 3 * scale), source, .5)
                else:
                    self._fill(painter, segment, colors.control_surface, 1)
                    badge = QRectF(area.right() - 16 * scale, area.bottom() - 9 * scale, 16 * scale, 9 * scale)
                    self._fill(painter, badge, colors.control_surface, 1)
                    self._text(painter, badge,
                               '+' + str(overflow), colors.muted, 6)
        return area.adjusted(0, -2 * scale, 0, -3 * scale)

    def paintEvent(self, event):
        self._ensure_geometry()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        painter.fillRect(self.rect(), colors.background)
        scale, today = self.scale, now().date()
        for month, (outer, heading, grid) in self._month_rects.items():
            painter.setPen(QPen(colors.line, 1))
            painter.setBrush(colors.panel)
            painter.drawRoundedRect(outer, corner_radius(self, 7) * scale, corner_radius(self, 7) * scale)
            self._text(painter, heading, self._month_names[month - 1], colors.accent, 11, True,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for column, label in enumerate(self._weekday_names):
                weekday = (self.preferences.first_weekday + column) % 7
                label_rect = QRectF(grid.x() + column * grid.width() / 7, outer.y() + 31 * scale,
                                    grid.width() / 7, 18 * scale)
                self._text(painter, label_rect, label, weekend_text(weekday, self.preferences, colors, colors.muted), 8)
            if self.preferences.show_week_numbers:
                self._text(painter, QRectF(outer.x() + 4 * scale, outer.y() + 31 * scale, 21 * scale, 18 * scale),
                           'Wk', colors.muted, 7)
                for (week_month, _row), (rect, start) in self._week_rects.items():
                    if week_month == month:
                        self._text(painter, rect, str(self.preferences.week_number(start)), colors.muted, 7)
            # Fine day separators lend structure without adding a box per mark.
            painter.setPen(QPen(colors.line, .45))
            for row in range(7):
                y = grid.y() + row * grid.height() / 6
                painter.drawLine(grid.x(), y, grid.right(), y)
        for day, rect in self._day_rects.items():
            identities = self.calendars_on_day(day)
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                painter.fillRect(rect, shade)
            method = {'tracks': self._tracks, 'tiles': self._tiles, 'tints': self._tints}[self.option]
            number = method(painter, rect, identities, colors)
            if day == today:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(colors.accent, 1.5 * scale))
                painter.drawRoundedRect(rect.adjusted(scale, scale, -scale, -scale),
                                        corner_radius(self, 3) * scale, corner_radius(self, 3) * scale)
            self._text(painter, number, str(day.day), weekend_text(day, self.preferences, colors,
                       colors.accent if day == today else colors.ink), 8, day == today)
        painter.end()
