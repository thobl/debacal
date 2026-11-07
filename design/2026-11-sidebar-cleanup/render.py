"""Render sidebar background studies after the functional cleanup is applied."""
import json
from pathlib import Path

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QLabel

from fixture import make_window, settle, close_window
from options import OPTIONS, apply
from debacal.theme import scheme_for

HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'
STATES = {
    'ready': 'Normal footer',
    'warning': 'Warning in the footer',
    'compact': 'Title hidden, with warning',
}


def save(pixmap, name):
    IMAGES.mkdir(exist_ok=True)
    assert pixmap.save(str(IMAGES / (name + '.png'))), name


def warning(window):
    """Synthetic local failure, without file or network access."""
    window.report_error('A calendar file could not be imported. The file could not be read.',
                        title='Import failed', action_label='Choose another file…', action=window.import_ics)


def render():
    metrics = {}
    for mode in ('light', 'dark'):
        for state in STATES:
            for key in OPTIONS:
                app, store, window, patches = make_window(appearance_mode=mode, show_title=state != 'compact')
                try:
                    if state != 'ready':
                        warning(window)
                    settle(app)
                    apply(window, key)
                    settle(app)
                    name = f'{key}-{mode}-{state}'
                    save(window.grab(), name + '-full')
                    save(window.grab(QRect(0, 0, 300, window.height())), name + '-sidebar')
                    save(window.grab(QRect(0, window.height() - 92, 300, 92)), name + '-footer')
                    assert not hasattr(window, 'message_stack')
                    assert window.findChild(QLabel, 'profileBadge') is None
                    calendar_bottom = window.stack.mapTo(window, window.stack.rect().bottomLeft()).y()
                    assert window.height() - calendar_bottom <= 12
                    metrics[name] = {
                        'calendar_bottom_gap': window.height() - calendar_bottom,
                        'overview': window.sidebar_overview.mapTo(window, QPoint()).y(),
                        'calendars': window.calendar_section.mapTo(window, QPoint()).y(),
                        'agenda': window.agenda_section.mapTo(window, QPoint()).y(),
                        'footer': window.sidebar_footer.mapTo(window, QPoint()).y(),
                    }
                    if key == 'uniform':
                        palette = scheme_for(window.preferences)
                        pixels = window.grab().toImage()
                        for point in (QPoint(6, 6), QPoint(6, window.height() - 6)):
                            assert pixels.pixelColor(point).name() == palette.sidebar
                        assert window.mini.calendar.palette().color(window.mini.calendar.backgroundRole()).name() == palette.sidebar
                finally:
                    close_window(app, store, window, patches)
    for mode in ('light', 'dark'):
        for state in STATES:
            assert metrics[f'segmented-{mode}-{state}'] == metrics[f'uniform-{mode}-{state}']
    (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')


if __name__ == '__main__':
    render()
