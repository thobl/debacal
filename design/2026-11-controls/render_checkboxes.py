"""Render real Qt checkbox alternatives; these do not change the application."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from debacal.appearance import stylesheet, widget_palette
from debacal.preferences import Preferences
from debacal.theme import scheme_for


OPTIONS = (
    ('A · Filled tick', 'The current compact square, with a clear contrasting tick.', 'filled'),
    ('B · Outline tick', 'A quieter option using the same line weight as action icons.', 'outline'),
    ('C · Larger framed tick', 'More emphasis and a larger mark, with a soft selected fill.', 'framed'),
)


def tick(directory, color):
    path = Path(directory) / (color[1:] + '.svg')
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
                    f'<path d="m4 10 4 4 8-8" fill="none" stroke="{color}" stroke-width="2.2" '
                    'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return path.as_posix()


def option_style(style, preferences, directory):
    colors = scheme_for(preferences)
    edge = 2 if style == 'framed' else 1
    size = 17 if style == 'framed' else 13
    fill = colors.accent if style == 'filled' else colors.selection if style == 'framed' else colors.panel
    mark = colors.on_accent if style == 'filled' else colors.accent
    return f'''
        QCheckBox::indicator {{ width: {size}px; height: {size}px; background: {colors.panel};
            border: {edge}px solid {colors.control_edge}; border-radius: 3px; }}
        QCheckBox::indicator:checked {{ background: {fill}; border-color: {colors.accent}; image: url("{tick(directory, mark)}"); }}
        QCheckBox::indicator:disabled {{ border-color: {colors.line}; background: {colors.control_surface}; }}
        QCheckBox::indicator:checked:disabled {{ image: url("{tick(directory, colors.disabled)}"); }}
        QCheckBox[keyboardFocus="true"]::indicator:focus {{ border-color: {colors.focus}; }}
    '''


def card(option, preferences, directory):
    title, description, value = option
    colors = scheme_for(preferences)
    frame = QFrame()
    frame.setObjectName('checkboxOption')
    frame.setStyleSheet('QFrame#checkboxOption { background: ' + colors.sidebar + '; border: 1px solid '
                        + colors.line + '; border-radius: 6px; }' + option_style(value, preferences, directory))
    column = QVBoxLayout(frame)
    column.setContentsMargins(20, 17, 20, 19)
    column.setSpacing(10)
    heading = QLabel(title)
    heading.setObjectName('sidebarSectionHeading')
    column.addWidget(heading)
    note = QLabel(description)
    note.setProperty('muted', True)
    note.setWordWrap(True)
    note.setFixedHeight(42)
    column.addWidget(note)
    rule = QFrame()
    rule.setObjectName('sourceDivider')
    rule.setFixedHeight(1)
    column.addWidget(rule)
    heading = QLabel('Date and time')
    heading.setObjectName('sidebarSectionHeading')
    column.addWidget(heading)
    for title, checked, enabled in [('All day', True, True), ('Indefinite', False, True),
                                     ('Sync automatically', True, True), ('Read-only option', True, False),
                                     ('Unavailable option', False, False)]:
        box = QCheckBox(title)
        box.setChecked(checked)
        box.setEnabled(enabled)
        column.addWidget(box)
    column.addStretch()
    return frame


def main():
    app = QApplication.instance() or QApplication([])
    folder = Path(__file__).resolve().parent / 'images'
    folder.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='debacal-checkbox-design-') as directory:
        for mode in ('light', 'dark'):
            preferences = Preferences(appearance_mode=mode)
            window = QWidget()
            window.setPalette(widget_palette(preferences))
            window.setStyleSheet(stylesheet(preferences.font_scale, preferences))
            window.setAutoFillBackground(True)
            row = QHBoxLayout(window)
            row.setContentsMargins(16, 16, 16, 16)
            row.setSpacing(14)
            for option in OPTIONS:
                row.addWidget(card(option, preferences, directory), 1)
            window.resize(1060, 380)
            window.show()
            app.processEvents()
            window.grab().save(str(folder / ('checkboxes-' + mode + '.png')))
            window.close()


if __name__ == '__main__':
    main()
