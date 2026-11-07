"""Isolated background alternatives for disposable review windows."""
from debacal.theme import scheme_for


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


def apply(window, key):
    if key not in OPTIONS:
        raise ValueError('Unknown sidebar alternative')
    colors = scheme_for(window.preferences)
    surface_role = 'sidebar' if key == 'uniform' else 'background'
    surface = getattr(colors, surface_role)
    window.setStyleSheet(window.styleSheet() + f'''
        QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter,
        QWidget#sidebarOverview {{ background: {surface}; }}
    ''')
    window.mini.calendar.surface_role = surface_role
    window.mini.set_preferences(window.preferences)
