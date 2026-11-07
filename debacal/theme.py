"""Interface color roles, independent of calendar-source colors and Qt widgets."""
from __future__ import annotations

from dataclasses import dataclass


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
        ink='#342e28', muted='#685f55', accent='#5b6246', weekend='#8b6254', line='#e8e2d9',
        control_surface='#f1ede7', selection='#eee8dc', selection_edge='#cdbfa6', now='#9b5f48',
        outside_month='#f6f2ec', focus='#5b6246', hover='#f3eee5', control_edge='#d6cbbd',
        disabled='#a19a90', accent_hover='#444c33', on_accent='#ffffff', error='#a04435',
        notice_background='#f4eddf', notice_ink='#71603e',
    ),
    'cool': ColorScheme(
        name='cool', label='Cool', background='#ffffff', sidebar='#edf2f7', panel='#ffffff',
        ink='#27343e', muted='#566673', accent='#465f76', weekend='#805b75', line='#dce4eb',
        control_surface='#eaf0f5', selection='#e2ebf2', selection_edge='#b1c2d1', now='#995f68',
        outside_month='#f0f4f7', focus='#465f76', hover='#edf2f6', control_edge='#c0cdd9',
        disabled='#929fa9', accent_hover='#344b60', on_accent='#ffffff', error='#a04450',
        notice_background='#eaf0f5', notice_ink='#4b6073',
    ),
}


def scheme_for(value=None) -> ColorScheme:
    """Resolve a name, preferences object, or widget without importing either.

    Invalid or older saved settings use the neutral interface. Calendar colors
    are data and are intentionally outside these interface palettes.
    """
    if isinstance(value, ColorScheme):
        return value
    if not isinstance(value, str):
        preferences = getattr(value, 'preferences', value)
        value = getattr(preferences, 'color_scheme', None)
    return SCHEMES.get(value, SCHEMES['neutral']) if isinstance(value, str) else SCHEMES['neutral']
