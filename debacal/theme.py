"""Interface color roles, independent of calendar-source colors and Qt widgets."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re


@dataclass(frozen=True, slots=True)
class ColorScheme:
    name: str
    label: str
    background: str
    sidebar: str
    panel: str
    ink: str
    muted: str
    accent: str
    weekend: str
    line: str
    control_surface: str
    selection: str
    selection_edge: str
    now: str
    outside_month: str
    focus: str
    hover: str
    control_edge: str
    disabled: str
    accent_hover: str
    on_accent: str
    error: str
    notice_background: str
    notice_ink: str
    weekend_background: str = '#f8f3f5'


SCHEMES = {
    'neutral': ColorScheme(
        name='neutral', label='Neutral', background='#ffffff', sidebar='#f0f3f6', panel='#ffffff',
        ink='#27313a', muted='#596570', accent='#475f52', weekend='#925866', line='#e0e5e8',
        control_surface='#edf0f2', selection='#e7eee8', selection_edge='#b9cbbf', now='#9b5f56',
        outside_month='#f4f6f7', focus='#475f52', hover='#edf1ee', control_edge='#cbd3da',
        disabled='#98a1a9', accent_hover='#354c40', on_accent='#ffffff', error='#a44240',
        notice_background='#f0f2e6', notice_ink='#5d6540',
    ),
    'warm': ColorScheme(
        name='warm', label='Warm', background='#ffffff', sidebar='#f5f0e8', panel='#fffdfa',
        ink='#342e28', muted='#685f55', accent='#5b6246', weekend='#865e50', line='#e8e2d9',
        control_surface='#f1ede7', selection='#eee8dc', selection_edge='#cdbfa6', now='#9b5f48',
        outside_month='#f6f2ec', focus='#5b6246', hover='#f3eee5', control_edge='#d6cbbd',
        disabled='#a19a90', accent_hover='#444c33', on_accent='#ffffff', error='#a04435',
        notice_background='#f4eddf', notice_ink='#71603e',
        weekend_background='#f8f2ed',
    ),
    'cool': ColorScheme(
        name='cool', label='Cool', background='#ffffff', sidebar='#edf2f7', panel='#ffffff',
        ink='#27343e', muted='#566673', accent='#465f76', weekend='#805b75', line='#dce4eb',
        control_surface='#eaf0f5', selection='#e2ebf2', selection_edge='#b1c2d1', now='#995f68',
        outside_month='#f0f4f7', focus='#465f76', hover='#edf2f6', control_edge='#c0cdd9',
        disabled='#929fa9', accent_hover='#344b60', on_accent='#ffffff', error='#a04450',
        notice_background='#eaf0f5', notice_ink='#4b6073',
        weekend_background='#f4f0f6',
    ),
}


CUSTOM_COLORS = {'custom_accent': '#475f52', 'custom_surface': '#88939b', 'custom_weekend': '#925866'}


def corner_radius(preferences=None, base=4):
    """One corner treatment for native controls and painted calendar surfaces."""
    preferences = getattr(preferences, 'preferences', preferences)
    return base * {'square': 0, 'subtle': 1, 'rounded': 2}.get(getattr(preferences, 'corner_style', 'subtle'), 1)


def valid_rgb(value):
    """Palette inputs are opaque RGB values, independent of Qt color names."""
    return isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) is not None


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def _mix(first, second, amount):
    return '#' + ''.join(f'{round(a * (1 - amount) + b * amount):02x}'
                          for a, b in zip(_rgb(first), _rgb(second)))


def _luminance(value):
    channels = [channel / 255 for channel in _rgb(value)]
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
              for channel in channels]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))


def _contrast(first, second):
    low, high = sorted((_luminance(first), _luminance(second)))
    return (high + .05) / (low + .05)


def _readable(source, backgrounds, minimum=4.5):
    """Retain the chosen hue while deriving enough contrast for its surfaces."""
    target = max(('#000000', '#ffffff'), key=lambda value: min(_contrast(value, bg) for bg in backgrounds))
    for step in range(101):
        candidate = _mix(source, target, step / 100)
        if all(_contrast(candidate, bg) >= minimum for bg in backgrounds):
            return candidate
    return target


@lru_cache(maxsize=128)
def _derived_scheme(name, mode, accent_source, surface_source, weekend_source):
    dark = mode == 'dark'
    if dark:
        background = _mix(surface_source, '#000000', .84)
        panel = _mix(surface_source, '#000000', .76)
        sidebar = _mix(surface_source, '#000000', .80)
        ink_source, muted_source = '#edf1f5', '#b1bac3'
    else:
        background = _mix(surface_source, '#ffffff', .98)
        panel = '#ffffff'
        sidebar = _mix(surface_source, '#ffffff', .88)
        ink_source, muted_source = '#27313a', '#596570'
    surfaces = background, sidebar, panel
    accent = _readable(accent_source, surfaces)
    weekend = _readable(weekend_source, surfaces)
    selection = _mix(background, accent, .17 if dark else .10)
    weekend_background = _mix(background, weekend, .11 if dark else .065)
    # Today and weekend shading retain the same text roles. Include those
    # surfaces when deriving contrast so a colored today can remain a weekend.
    text_surfaces = (*surfaces, selection, weekend_background)
    ink = _readable(ink_source, text_surfaces)
    muted = _readable(muted_source, text_surfaces)
    accent = _readable(accent_source, text_surfaces)
    weekend = _readable(weekend_source, text_surfaces)
    accent_hover = _mix(accent, '#ffffff' if dark else '#000000', .12)
    on_accent = max(('#ffffff', '#000000'),
                    key=lambda value: min(_contrast(value, accent), _contrast(value, accent_hover)))
    notice_background = _mix(background, accent, .12)
    return ColorScheme(
        name=f'{name}-{mode}-{accent_source[1:]}-{surface_source[1:]}-{weekend_source[1:]}',
        label=('Custom' if name == 'custom' else SCHEMES[name].label) + (' · Dark' if dark else ' · Light'),
        background=background, sidebar=sidebar, panel=panel, ink=ink, muted=muted,
        accent=accent, weekend=weekend, line=_mix(background, ink, .14),
        control_surface=_mix(panel, ink, .065), selection=selection,
        selection_edge=_mix(selection, accent, .42), now=_readable(weekend_source, surfaces, 3),
        outside_month=_mix(background, ink, .035), focus=accent,
        hover=_mix(background, accent, .12), control_edge=_mix(panel, ink, .26),
        disabled=_mix(background, ink, .42), accent_hover=accent_hover, on_accent=on_accent,
        error=_readable('#ed9c98' if dark else '#a44240', surfaces),
        notice_background=notice_background, notice_ink=_readable(ink, (notice_background,)),
        weekend_background=weekend_background,
    )


def scheme_for(value=None) -> ColorScheme:
    """Resolve a name, preferences object, or widget without importing either.

    Invalid or older saved settings use the neutral interface. Calendar colors
    are data and are intentionally outside these interface palettes.
    """
    if isinstance(value, ColorScheme):
        return value
    preferences = None if isinstance(value, str) else getattr(value, 'preferences', value)
    name = value if isinstance(value, str) else getattr(preferences, 'color_scheme', None)
    if not isinstance(name, str) or name not in (*SCHEMES, 'custom'):
        name = 'neutral'
    mode = getattr(preferences, 'appearance_mode', 'light')
    if mode != 'dark':
        mode = 'light'
    if name != 'custom' and mode == 'light':
        return SCHEMES[name]
    if name == 'custom':
        colors = tuple(getattr(preferences, field, fallback) for field, fallback in CUSTOM_COLORS.items())
        colors = tuple(value.lower() if valid_rgb(value) else fallback
                       for value, fallback in zip(colors, CUSTOM_COLORS.values()))
    else:
        base = SCHEMES[name]
        colors = base.accent, base.sidebar, base.weekend
    return _derived_scheme(name, mode, *colors)
