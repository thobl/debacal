"""Readable source-tinted cards shared by the calendar and event lists."""
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen

from .theme import corner_radius, scheme_for


@dataclass(frozen=True, slots=True)
class EventColors:
    # Immutable CSS colors keep cached values safe from QColor.setAlpha() calls.
    fill: str
    outline: str
    selected_outline: str
    ink: str
    muted: str


def paint_event_card(painter, card, colors, *, selected=False, primary=None, focused=False,
                     focus_color=None, scale=1, radius=4, preferences=None):
    """Paint the calendar's source-colored selection language in an event list.

    Affected events use a stronger source edge. Only explicit selections add
    an inset vertical bar; their fill never changes. Keyboard focus needs an additional cue only when its
    current row is unselected, so moving focus to a selected card adds no ring.
    """
    painter.save()
    edge = QColor(colors.selected_outline if selected else colors.outline)
    painter.setBrush(QColor(colors.fill))
    painter.setPen(QPen(edge, (2 if selected else 1) * scale))
    radius = corner_radius(preferences, radius)
    painter.drawRoundedRect(card, radius * scale, radius * scale)
    if selected and (selected if primary is None else primary):
        painter.fillRect(QRectF(card.left() + 3 * scale, card.top() + 4 * scale,
                                3 * scale, max(0, card.height() - 8 * scale)), edge)
    elif not selected and focused:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(focus_color or colors.selected_outline),
                            2 * scale, Qt.PenStyle.DotLine))
        painter.drawRoundedRect(card.adjusted(3 * scale, 3 * scale, -3 * scale, -3 * scale),
                                corner_radius(preferences, 2) * scale, corner_radius(preferences, 2) * scale)
    painter.restore()


def _luminance(value):
    color = QColor(value)
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))


def _contrast(first, second):
    first, second = sorted((first, second))
    return (second + .05) / (first + .05)


def contrast_ratio(foreground, background):
    return _contrast(_luminance(foreground), _luminance(background))


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
    """Return cached, opaque card colors for one source and display preferences.

    Selection changes the edge/weight, never the source tint. Compositing once
    against the canvas also prevents grid/current-time lines showing through
    cards on other surfaces such as the sidebar. Scheme names and palette
    objects retain the original tint; preferences also select literal RGB or
    adjust the uniform tint's brightness and source-color intensity.
    """
    palette = scheme_for(scheme)
    source = QColor(source_color)
    if not source.isValid():
        source = QColor(palette.accent)
    mode, brightness, intensity = event_color_options(scheme)
    return _event_colors(source.name(), palette, max(0, min(255, int(alpha))),
                         mode, brightness, intensity)


def event_color_options(value=None):
    """Resolve event styling from preferences or their owning widget.

    Palette objects and scheme names keep the historical card treatment.
    Defensive bounds also make isolated previews safe with partial preferences.
    """
    preferences = getattr(value, 'preferences', value)
    mode = getattr(preferences, 'event_color_mode', 'uniform')
    brightness = getattr(preferences, 'event_color_brightness', 0)
    intensity = getattr(preferences, 'event_color_intensity', 100)
    return ('calendar' if mode == 'calendar' else 'uniform',
            max(-50, min(50, brightness)) if type(brightness) is int else 0,
            max(0, min(300, intensity)) if type(intensity) is int else 100)


def _mix_color(first, second, weight):
    return QColor(*(round(a * (1 - weight) + b * weight) for a, b in
                    zip((first.red(), first.green(), first.blue()),
                        (second.red(), second.green(), second.blue()))))


def _card_outlines(source, fill, background, selected_source):
    """Find edges visible against both the card and the surrounding canvas."""
    normal_target, selected_target = source_outline(source, fill), source_outline(selected_source, fill)
    candidates = {normal_target, selected_target}
    color = QColor(source)
    for endpoint in ('#000000', '#ffffff'):
        target = QColor(endpoint)
        candidates.update(_mix_color(color, target, step / 64).name() for step in range(65))
    luminance = {value: _luminance(value) for value in candidates}
    fill_light, background_light = _luminance(fill), _luminance(background)
    scores = {value: min(_contrast(luminance[value], fill_light), _contrast(luminance[value], background_light))
              for value in candidates}
    readable = [value for value in sorted(candidates) if scores[value] >= 3]
    if not readable:
        # Some fill/canvas pairs cannot share an edge with 3:1 contrast on
        # both sides. Preserve the card's own boundary and selection bar.
        readable = [value for value in sorted(candidates) if _contrast(luminance[value], fill_light) >= 3]

    def distance(value, target):
        value, target = QColor(value), QColor(target)
        return sum((a - b) ** 2 for a, b in zip(value.getRgb()[:3], target.getRgb()[:3]))

    outline = min(readable, key=lambda value: distance(value, normal_target))
    distinct = [value for value in readable if _contrast(luminance[value], luminance[outline]) >= 1.25]
    selected = min(distinct or readable,
                   key=lambda value: distance(value, selected_target))
    return outline, selected


@lru_cache(maxsize=1024)
def _event_colors(source, palette, alpha, mode='uniform', brightness=0, intensity=100):
    palette = scheme_for(palette)
    color, background = QColor(source), QColor(palette.background)
    weight = min(255, round(alpha * intensity / 100)) / 255
    fill_color = color if mode == 'calendar' else _mix_color(background, color, weight)
    if mode != 'calendar' and brightness:
        fill_color = _mix_color(fill_color, QColor('#ffffff' if brightness > 0 else '#000000'),
                               abs(brightness) / 100)
    fill = fill_color.name()
    dark = contrast_ratio('#ffffff', palette.background) > contrast_ratio('#000000', palette.background)
    selected_source = color.lighter(145) if dark else color.darker(145)
    if mode == 'calendar' or brightness or intensity != 100:
        outline, selected = _card_outlines(source, fill, palette.background, selected_source.name())
    else:
        # Preserve every default edge as well as the original composited fill.
        outline = source_outline(source, fill)
        if contrast_ratio(outline, palette.background) < 3:
            outline = source_outline(outline, palette.background)
        selected = source_outline(selected_source.name(), fill)
        if contrast_ratio(selected, palette.background) < 3:
            selected = source_outline(selected, palette.background)
    if mode == 'calendar':
        ink = max(('#000000', '#ffffff'), key=lambda value: contrast_ratio(value, fill))
        return EventColors(fill, outline, selected, ink, ink)
    return EventColors(fill, outline, selected,
                       source_outline(palette.ink, fill, minimum=4.5),
                       source_outline(palette.muted, fill, minimum=4.5))
