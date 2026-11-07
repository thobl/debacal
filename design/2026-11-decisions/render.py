"""Capture applied app changes and unresolved design choices separately."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QPoint, QRect, QElapsedTimer
from PySide6.QtTest import QTest

from fixture import make_window, settle, close_window, select_result
import header_options
import search_options
import agenda_options
from debacal.ics import export_ics
from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource
from debacal.preferences_dialog import PreferencesDialog


HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'
HEADER_KEYS = [f'{layout}-{style}' for layout in header_options.LAYOUTS for style in header_options.STYLES]
HEADER_KEYS.append('split-filled-tight')


def save(pixmap, name):
    IMAGES.mkdir(exist_ok=True)
    if not pixmap.save(str(IMAGES / (name + '.png'))):
        raise RuntimeError('Could not save ' + name)


def metadata(group, key, value):
    path = HERE / 'metrics.json'
    result = json.loads(path.read_text()) if path.exists() else {}
    result.setdefault(group, {})[key] = value
    path.write_text(json.dumps(result, indent=2) + '\n')


def header(key):
    layout, style, *density = key.split('-')
    app, store, window, patches = make_window()
    try:
        header_options.apply(window, layout, style, tight=bool(density))
        measures = {}
        for size, dimensions in [('wide', (1440, 960)), ('narrow', (1030, 690))]:
            window.resize(*dimensions)
            settle(app)
            save(window.grab(), 'header-' + key + '-' + size + '-full')
            left = window.stack.parentWidget().mapTo(window, QPoint()).x()
            bottom = window.all_day_scroll.mapTo(window, window.all_day_scroll.rect().bottomLeft()).y() + 1
            save(window.grab(QRect(left, 0, window.width() - left, bottom)), 'header-' + key + '-' + size)
            measures[size] = dict(width=window.width() - left, height=bottom,
                first_column=window.canvas.mapTo(window, QPoint(window.canvas.gutter, 0)).x() - left,
                month=window.heading.mapTo(window, QPoint()).x() - left,
                next_button=window.next_button.mapTo(window, QPoint()).x() - left)
            assert window.width() == dimensions[0]
            assert window.range_label.width() >= window.range_label.fontMetrics().horizontalAdvance(window.range_label.text())
            if layout != 'compact':
                assert measures[size]['month'] == measures[size]['first_column'], measures[size]
            assert all(window.workspace_header.rect().contains(button.mapTo(window.workspace_header, button.rect().center()))
                       for button in [window.previous_button, window.today_button, window.next_button,
                                      window.new_button, *window.view_buttons.values(), *window.selection_buttons.values()])
        metadata('headers', key, measures)
    finally:
        close_window(app, store, window, patches)


def search(key):
    for scale in (100, 140):
        app, store, window, patches = make_window(font_scale=scale)
        try:
            search_options.apply(window, key)
            for state in ('agenda', 'query'):
                if state == 'query':
                    window.focus_search()
                    window.search.setText('work')
                    QTest.qWait(200)
                    for index in range(window.results.topLevelItemCount()):
                        item = window.results.topLevelItem(index)
                        if item.text(0) == 'Weekly work review':
                            item.setExpanded(True)
                            QTest.qWait(20)
                            window.results.setCurrentItem(item.child(0) or item)
                            break
                settle(app)
                save(window.grab(QRect(0, 0, 300, window.height())), f'search-{key}-{state}-{scale}')
            assert not window.search.isHidden()
        finally:
            close_window(app, store, window, patches)


def agenda(key):
    app, store, window, patches = make_window()
    try:
        agenda_options.apply(window, key)
        for state in ('agenda', 'query'):
            if state == 'query':
                window.search.setText('work')
                QTest.qWait(200)
            settle(app)
            save(window.grab(QRect(0, 0, 300, window.height())), f'agenda-{key}-{state}')
    finally:
        close_window(app, store, window, patches)


def production(scheme):
    app, store, window, patches = make_window(scheme)
    try:
        states = ('week', 'month', 'year', 'narrow', 'search') if scheme == 'neutral' else ('week',)
        for state in states:
            window.resize(*((1030, 690) if state == 'narrow' else (1440, 960)))
            window.set_view(state if state in ('month', 'year') else 'week')
            if state == 'search':
                window.search.setText('work')
                QTest.qWait(200)
                for index in range(window.results.topLevelItemCount()):
                    item = window.results.topLevelItem(index)
                    if item.text(0) == 'Weekly work review':
                        item.setExpanded(True)
                        QTest.qWait(20)
                        window.results.setCurrentItem(item.child(0) or item)
                        break
            else:
                select_result(window)
            settle(app)
            save(window.grab(), f'applied-{scheme}-{state}')
        if scheme == 'neutral':
            dialog = PreferencesDialog(window.preferences, window, bindings=window.bindings)
            dialog.set_category('Appearance')
            dialog.show()
            settle(app)
            save(dialog.grab(), 'applied-preferences')
            dialog.reject()
        if scheme == 'warm':
            source = ImportSource.from_data(export_ics([store.get_event(value) for value in
                ('0', '5', '6', 'autumn-break')]), 'Planning.ics')
            dialog = ImportDialog(store.list_calendars(), sources=[source], calendar_id='work', parent=window,
                                  preferences=window.preferences)
            dialog.show()
            timer = QElapsedTimer()
            timer.start()
            while not dialog.preview_ready and timer.elapsed() < 10000:
                app.processEvents()
                QTest.qWait(5)
            assert dialog.preview_ready
            settle(app)
            save(dialog.grab(), 'applied-warm-import')
            dialog.reject()
    finally:
        close_window(app, store, window, patches)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--header', choices=HEADER_KEYS)
    parser.add_argument('--search', choices=tuple(search_options.OPTIONS))
    parser.add_argument('--production', choices=('neutral', 'warm', 'cool'))
    parser.add_argument('--agenda', choices=tuple(agenda_options.OPTIONS))
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        tasks = [('--production', name) for name in ('neutral', 'warm', 'cool')]
        tasks += [('--header', name) for name in HEADER_KEYS]
        tasks += [('--search', name) for name in search_options.OPTIONS]
        tasks += [('--agenda', name) for name in agenda_options.OPTIONS]
        for option, key in tasks:
            subprocess.run([sys.executable, __file__, option, key], check=True)
        print('Captured applied changes, seven headers, four search placements, and three agenda styles.')
    elif args.header:
        header(args.header)
    elif args.search:
        search(args.search)
    elif args.production:
        production(args.production)
    elif args.agenda:
        agenda(args.agenda)
    else:
        parser.error('Choose a capture or --all')
