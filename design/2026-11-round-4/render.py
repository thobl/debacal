"""Render this design round from real Qt widgets and synthetic, in-memory data."""
import argparse
from dataclasses import replace
from datetime import date
import json
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest

from fixture import make_window, settle, close_window
import header_options

HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'
OPTIONS = {
    'n1': ('split', 'underlined', False),
    'n2': ('split', 'filled', False),
    'n3': ('split', 'underlined', True),
    'n4': ('split', 'filled', True),
    'n5': ('after', 'underlined', False),
    'n6': ('after', 'filled', False),
}
METRICS = {}


def save(pixmap, name):
    IMAGES.mkdir(exist_ok=True)
    assert pixmap.save(str(IMAGES / (name + '.png'))), name


def header(key):
    layout, style, tight = OPTIONS[key]
    measures = {}
    for locale in ('en_GB', 'de_DE'):
        app, store, window, patches = make_window()
        try:
            window.preferences = replace(window.preferences, locale_name=locale)
            window.refresh()
            header_options.apply(window, layout, style, tight=tight)
            for size, dimensions in [('wide', (1440, 960)), ('narrow', (1030, 690))]:
                arrows = []
                for month in (5, 9):
                    window.resize(*dimensions)
                    window.go_date(date(2026, month, 6))
                    settle(app)
                    left = window.stack.parentWidget().mapTo(window, QPoint()).x()
                    bottom = window.all_day_scroll.mapTo(window, window.all_day_scroll.rect().bottomLeft()).y() + 1
                    name = f'{key}-{locale}-{size}-{month}'
                    save(window.grab(QRect(left, 0, window.width() - left, bottom)), name)
                    if month == 9 and locale == 'en_GB':
                        save(window.grab(), name + '-full')
                    measure = dict(first_column=window.canvas.mapTo(window, QPoint(window.canvas.gutter, 0)).x() - left,
                        month=window.heading.mapTo(window, QPoint()).x() - left,
                        previous=window.previous_button.mapTo(window, QPoint()).x() - left,
                        next=window.next_button.mapTo(window, QPoint()).x() - left,
                        range=window.range_label.mapTo(window, QPoint()).x() - left,
                        title_width=window.heading.width(), text_width=window.heading.fontMetrics().horizontalAdvance(window.heading.text()),
                        width=window.width() - left, height=bottom, inline=window.workspace_header.inline)
                    measures[f'{locale}-{size}-{month}'] = measure
                    arrows.append((measure['previous'], measure['next']))
                    assert measure['first_column'] == measure['month'], measure
                    assert window.width() == dimensions[0]
                    assert window.range_label.width() >= window.range_label.fontMetrics().horizontalAdvance(window.range_label.text())
                    assert all(window.workspace_header.rect().contains(button.mapTo(window.workspace_header, button.rect().center()))
                               for button in [window.previous_button, window.today_button, window.next_button,
                                              window.new_button, *window.view_buttons.values(), *window.selection_buttons.values()])
                    if style == 'filled':
                        for control in (window.view_control, window.selection_control):
                            assert control.buttons[0].geometry().left() == 0
                            assert control.buttons[-1].geometry().right() == control.width() - 1, (name, control.width(), [b.geometry().getRect() for b in control.buttons])
                            assert control.buttons[0].height() == control.height()
                assert arrows[0] == arrows[1], (key, size, arrows)
        finally:
            close_window(app, store, window, patches)
    METRICS[key] = measures


def query(window, app):
    window.focus_search()
    window.search.setText('work')
    window.flush_search()
    for index in range(window.results.topLevelItemCount()):
        item = window.results.topLevelItem(index)
        if item.text(0) == 'Weekly work review':
            item.setExpanded(True)
            settle(app)
            window.results.setCurrentItem(item.child(0) or item)
            window.results.setFocus()
            break
    settle(app)


def production():
    from debacal.preferences_dialog import PreferencesDialog
    app, store, window, patches = make_window()
    try:
        save(window.grab(), 'applied-week')
        save(window.grab(QRect(0, 0, 300, window.height())), 'applied-resting')
        QTest.mouseClick(window.search_button, Qt.MouseButton.LeftButton)
        assert window.search.hasFocus()
        settle(app)
        save(window.grab(QRect(0, 0, 300, window.height())), 'applied-search-empty')
        query(window, app)
        save(window.grab(), 'applied-search')
        save(window.grab(QRect(0, 0, 300, window.height())), 'applied-query')
        window.exit_search()
        window.calendar_list.move_calendar('holidays', 0)
        settle(app)
        save(window.grab(QRect(0, 0, 300, window.height())), 'applied-reordered')
        for style in ('cards', 'grouped', 'timeline'):
            window.preferences = replace(window.preferences, agenda_style=style)
            window.refresh()
            settle(app)
            save(window.grab(QRect(0, 0, 300, window.height())), 'applied-' + style)
        dialog = PreferencesDialog(window.preferences, window, bindings=window.bindings)
        dialog.set_category('Appearance')
        dialog.show()
        settle(app)
        save(dialog.grab(), 'applied-preferences')
        dialog.reject()
    finally:
        close_window(app, store, window, patches)


def sidebar(key):
    import sidebar_options
    measures = {}
    for scale in (100, 140):
        app, store, window, patches = make_window(font_scale=scale)
        try:
            sidebar_options.apply(window, key)
            for state in ('agenda', 'query'):
                if state == 'query':
                    query(window, app)
                settle(app)
                save(window.grab(QRect(0, 0, 300, window.height())), f'{key}-{state}-{scale}')
                measures[f'{state}-{scale}'] = sidebar_options.alignment_metrics(window)
        finally:
            close_window(app, store, window, patches)
    METRICS[key] = measures


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?', default='all')
    args = parser.parse_args()
    if args.target in ('all', 'production'):
        production()
    if args.target == 'all' or args.target in OPTIONS:
        for key in OPTIONS if args.target == 'all' else [args.target]:
            header(key)
    if args.target == 'all' or args.target in ('a3', 'flush', 'utility'):
        for key in ('a3', 'flush', 'utility') if args.target == 'all' else [args.target]:
            sidebar(key)
    existing = json.loads((HERE / 'metrics.json').read_text()) if (HERE / 'metrics.json').exists() else {}
    existing.update(METRICS)
    (HERE / 'metrics.json').write_text(json.dumps(existing, indent=2) + '\n')
