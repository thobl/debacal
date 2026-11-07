"""Isolated background alternatives for disposable review windows."""
from dataclasses import fields

from debacal.theme import ColorScheme, scheme_for


OPTIONS = {
    'segmented': {
        'id': 'A',
        'name': 'Current surfaces',
        'description': 'The overview and footer use the main canvas surface; calendars and agenda use the source-panel surface.',
        'tradeoff': 'The overview reads as a separate block. The footer has another visible background transition.',
    },
    'uniform': {
        'id': 'B',
        'name': 'One sidebar surface',
        'description': 'The complete left column uses the existing calendars-and-agenda background, including the overview and footer.',
        'tradeoff': 'A calmer continuous column. Headings, spacing and thin rules retain the section structure.',
    },
}


class _MiniSurface(ColorScheme):
    """Give only this miniature the proposed surface, retaining real formats.

    CalendarDateGrid paints native date cells from the resolved scheme, so a
    stylesheet alone cannot color its base. No application globals are changed.
    """
    def __init__(self, scheme, preferences):
        for item in fields(ColorScheme):
            object.__setattr__(self, item.name, getattr(scheme, item.name))
        object.__setattr__(self, 'background', scheme.sidebar)
        object.__setattr__(self, '_display_preferences', preferences)

    def __getattr__(self, name):
        return getattr(self._display_preferences, name)


def apply(window, key):
    if key not in OPTIONS:
        raise ValueError('Unknown sidebar alternative')
    if key == 'segmented':
        return
    colors = scheme_for(window.preferences)
    window.setStyleSheet(window.styleSheet() + f'''
        QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter,
        QWidget#sidebarOverview, QWidget#sourceSections {{ background: {colors.sidebar}; }}
    ''')
    window.mini.set_preferences(_MiniSurface(colors, window.preferences))
