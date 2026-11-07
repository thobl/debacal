"""Readable source-tinted cards shared by the calendar and event lists."""
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen

from .theme import scheme_for


@dataclass(frozen=True, slots=True)
class EventColors:
    # Immutable CSS colors keep cached values safe from QColor.setAlpha() calls.
    fill: str
    outline: str
    selected_outline: str
    ink: str
    muted: str


def paint_event_card(painter, card, colors, *, selected=False, focused=False,
                     focus_color=None, scale=1, radius=4):
    """Paint the calendar's source-colored selection language in an event list.

    Selected events use a stronger source edge and an inset vertical bar; their
    fill never changes. Keyboard focus needs an additional cue only when its
    current row is unselected, so moving focus to a selected card adds no ring.
    """
    painter.save()
    edge = QColor(colors.selected_outline if selected else colors.outline)
    painter.setBrush(QColor(colors.fill))
    painter.setPen(QPen(edge, (2 if selected else 1) * scale))
    painter.drawRoundedRect(card, radius * scale, radius * scale)
    if selected:
        painter.fillRect(QRectF(card.left() + 3 * scale, card.top() + 4 * scale,
                                3 * scale, max(0, card.height() - 8 * scale)), edge)
    elif focused:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(focus_color or colors.selected_outline),
                            2 * scale, Qt.PenStyle.DotLine))
        painter.drawRoundedRect(card.adjusted(3 * scale, 3 * scale, -3 * scale, -3 * scale),
                                2 * scale, 2 * scale)
    painter.restore()


def contrast_ratio(foreground, background):
    def luminance(value):
        color = QColor(value)
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                  for channel in channels]
        return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))
    first, second = sorted((luminance(foreground), luminance(background)))
    return (second + .05) / (first + .05)


def source_outline(source, background, minimum=3.0):
    """Keep the source hue, darkening only the edge when necessary."""
    color = QColor(source)
    if not color.isValid():
        color = QColor('#475f52')
    return _source_outline(color.name(), QColor(background).name(), float(minimum))


@lru_cache(maxsize=1024)
def _source_outline(source, background, minimum):
    color = QColor(source)
    lighten = contrast_ratio('#ffffff', background) > contrast_ratio('#000000', background)
    for _ in range(40):
        if contrast_ratio(color, background) >= minimum:
            return color.name()
        if lighten:
            color = QColor(*(round(channel * .88 + 255 * .12)
                             for channel in (color.red(), color.green(), color.blue())))
        else:
            color = color.darker(112)
    return '#ffffff' if lighten else '#000000'


def event_colors(source_color, scheme=None, *, alpha=52):
    """Return cached, opaque card colors for one source and interface scheme.

    Selection changes the edge/weight, never the source tint. Compositing once
    against the canvas also prevents grid/current-time lines showing through
    cards on other surfaces such as the sidebar.
    """
    palette = scheme_for(scheme)
    source = QColor(source_color)
    if not source.isValid():
        source = QColor(palette.accent)
    return _event_colors(source.name(), palette, max(0, min(255, int(alpha))))


@lru_cache(maxsize=1024)
def _event_colors(source, palette, alpha):
    palette = scheme_for(palette)
    color, background = QColor(source), QColor(palette.background)
    weight = alpha / 255
    fill = QColor(*(round(a * weight + b * (1 - weight)) for a, b in
                    zip((color.red(), color.green(), color.blue()),
                        (background.red(), background.green(), background.blue())))).name()
    outline = source_outline(source, fill)
    if contrast_ratio(outline, palette.background) < 3:
        outline = source_outline(outline, palette.background)
    dark = contrast_ratio('#ffffff', palette.background) > contrast_ratio('#000000', palette.background)
    selected_source = color.lighter(145) if dark else color.darker(145)
    selected = source_outline(selected_source.name(), fill)
    if contrast_ratio(selected, palette.background) < 3:
        selected = source_outline(selected, palette.background)
    return EventColors(fill, outline, selected,
                       source_outline(palette.ink, fill, minimum=4.5),
                       source_outline(palette.muted, fill, minimum=4.5))
