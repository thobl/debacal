"""Shared weekend treatment for calendar views and date overviews."""

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPen, QFont
from PySide6.QtWidgets import QCalendarWidget

from .theme import corner_radius, scheme_for
from .timeutil import now


def _weekend(day):
    weekday = day if isinstance(day, int) else day.weekday()
    return weekday >= 5


def weekend_text(day, preferences, colors, ordinary=None):
    """Preserve weekend text even when the same date is today or selected."""
    ordinary = colors.ink if ordinary is None else ordinary
    return (colors.weekend if _weekend(day) and getattr(preferences, 'weekend_style', 'text') == 'text'
            else ordinary)


def weekend_fill(day, preferences, colors):
    return (colors.weekend_background
            if _weekend(day) and getattr(preferences, 'weekend_style', 'text') == 'background' else None)


class CalendarDateGrid(QCalendarWidget):
    """Keep weekend treatment visible on selected/current-day overview cells."""
    def __init__(self, preferences, parent=None, *, surface_role='background'):
        super().__init__(parent)
        self.preferences = preferences
        self.surface_role = surface_role

    def paintCell(self, painter, rect, day):
        colors = scheme_for(self.preferences)
        value = day.toPython()
        scale = self.preferences.font_scale / 100
        shade = weekend_fill(value, self.preferences, colors)
        painter.save()
        painter.fillRect(rect, QColor(shade or getattr(colors, self.surface_role)))
        selected = day == self.selectedDate()
        today = value == now().date()
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.selection))
            painter.drawRoundedRect(QRectF(rect).adjusted(scale, scale, -scale, -scale),
                                    corner_radius(self, 2) * scale, corner_radius(self, 2) * scale)
        if today:
            painter.setPen(QPen(QColor(colors.accent), max(1, scale)))
            painter.drawLine(rect.left() + round(5 * scale), rect.bottom() - round(2 * scale),
                             rect.right() - round(5 * scale), rect.bottom() - round(2 * scale))
        ordinary = colors.ink if day.month() == self.monthShown() else colors.muted
        ink = (colors.disabled if day < self.minimumDate() or day > self.maximumDate() else
               weekend_text(value, self.preferences, colors, ordinary))
        painter.setPen(QColor(ink))
        font = QFont(painter.font())
        font.setBold(today)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(day.day()))
        painter.restore()
