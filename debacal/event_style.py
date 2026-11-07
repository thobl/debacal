"""Readable source-tinted cards shared by the calendar and event lists."""
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtGui import QColor

from .theme import scheme_for


@dataclass(frozen=True, slots=True)
class EventColors:
    # Immutable CSS colors keep cached values safe from QColor.setAlpha() calls.
    fill: str
    outline: str
    selected_outline: str
    ink: str
    muted: str


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
    for _ in range(40):
        if contrast_ratio(color, background) >= minimum:
            return color.name()
        color = color.darker(112)
    return '#000000'


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
    return _event_colors(source.name(), palette.name, max(0, min(255, int(alpha))))


@lru_cache(maxsize=1024)
def _event_colors(source, scheme_name, alpha):
    palette = scheme_for(scheme_name)
    color, background = QColor(source), QColor(palette.background)
    weight = alpha / 255
    fill = QColor(*(round(a * weight + b * (1 - weight)) for a, b in
                    zip((color.red(), color.green(), color.blue()),
                        (background.red(), background.green(), background.blue())))).name()
    outline = source_outline(source, fill)
    if contrast_ratio(outline, palette.background) < 3:
        outline = source_outline(outline, palette.background)
    selected = source_outline(color.darker(145).name(), fill)
    if contrast_ratio(selected, palette.background) < 3:
        selected = source_outline(selected, palette.background)
    return EventColors(fill, outline, selected,
                       source_outline(palette.ink, fill, minimum=4.5),
                       source_outline(palette.muted, fill, minimum=4.5))
