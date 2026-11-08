"""Shared calendar-presence drawing for the year view and its preferences."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen

from .fonts import ui_font
from .theme import corner_radius


def _fill(painter, rect, color, preferences, radius, scale):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    radius = corner_radius(preferences, radius) * scale
    painter.drawRoundedRect(rect, radius, radius)


def _overflow(painter, rect, count, preferences, colors, scale):
    _fill(painter, rect, colors.control_surface, preferences, 1, scale)
    painter.setFont(ui_font(preferences, 6))
    painter.setPen(QPen(QColor(colors.muted)))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, '+' + str(count))


def paint_day_presence(painter, rect, source_colors, preferences, colors, *, style=None, scale=None):
    """Paint distinct calendar colors and return the date-label rectangle.

    Source colors arrive in sidebar order, with one entry per calendar. The
    caller retains date, weekend, today, selection, and paste-preview behavior;
    these two treatments only change how calendar presence occupies the cell.
    """
    style = style or preferences.year_view_style
    scale = preferences.font_scale / 100 if scale is None else scale
    sources = [QColor(value) for value in source_colors]
    sources = [value if value.isValid() else QColor(colors.accent) for value in sources]
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    if style == 'tints':
        area = rect.adjusted(1.5 * scale, 1.5 * scale, -1.5 * scale, -1.5 * scale)
        visible = sources[:4] if len(sources) <= 4 else sources[:3]
        overflow = len(sources) - len(visible)
        count = len(visible) + bool(overflow)
        for index, source in enumerate(visible):
            segment = QRectF(area.x() + index * area.width() / count, area.y(), area.width() / count, area.height())
            tint = QColor(source)
            tint.setAlpha(58 if preferences.appearance_mode == 'dark' else 43)
            _fill(painter, segment, tint, preferences, 1, scale)
            _fill(painter, QRectF(segment.x(), segment.bottom() - 3 * scale, segment.width(), 3 * scale),
                  source, preferences, .5, scale)
        if overflow:
            segment = QRectF(area.right() - area.width() / count, area.y(), area.width() / count, area.height())
            _fill(painter, segment, colors.control_surface, preferences, 1, scale)
            # Keep the solid source-color edges visible even when a narrow
            # overflow badge spans more than its reserved background segment.
            badge = QRectF(area.right() - 16 * scale, area.bottom() - 12 * scale, 16 * scale, 9 * scale)
            _overflow(painter, badge, overflow, preferences, colors, scale)
        number = area.adjusted(0, -2 * scale, 0, (-9 if overflow else -3) * scale)
    else:
        number = QRectF(rect.x() + 3 * scale, rect.y() + scale, 15 * scale, 12 * scale)
        area = rect.adjusted(4 * scale, 15 * scale, -4 * scale, -2 * scale)
        maximum = max(1, min(4, int((area.height() + scale) / (4 * scale))))
        # On the narrowest two-column layout, keep the first calendar colors
        # beside the overflow badge instead of covering them or the date.
        narrow = rect.width() < 30 * scale
        visible, overflow = sources[:maximum], max(0, len(sources) - maximum)
        if narrow and overflow:
            area.setLeft(rect.left() + scale)
            area.setWidth(max(2 * scale, rect.width() * .28))
        count = len(visible)
        gap = scale
        height = min(3 * scale, max(1.5 * scale, (area.height() - gap * (count - 1)) / max(1, count)))
        for index, source in enumerate(visible):
            track = QRectF(area.x(), area.y() + index * (height + gap), area.width(), height)
            _fill(painter, track, source, preferences, .8, scale)
        if overflow:
            badge = (QRectF(area.right() + scale, rect.bottom() - 9 * scale,
                           rect.right() - area.right() - 2 * scale, 9 * scale)
                     if narrow else QRectF(rect.right() - 14 * scale, rect.y() + scale, 14 * scale, 10 * scale))
            _overflow(painter, badge, overflow, preferences, colors, scale)
    painter.restore()
    return number
