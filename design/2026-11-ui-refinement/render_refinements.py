"""Capture B refinements with synthetic data; never opens or writes a profile."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / '2026-11-ui-review'))

from mockup_base import make_sample_window, settle, install_paint_palette
from PySide6.QtCore import QElapsedTimer, QPoint
from PySide6.QtTest import QTest
from debacal.ics import export_ics
from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource

import calendar_styles
import header_styles
import sidebar_styles


def update_gallery_metrics():
    """Embed measured geometry so the viewer also works from a file URL."""
    gallery = HERE / 'index.html'
    if not gallery.exists():
        return
    metrics = {variant: json.loads(path.read_text())
               for variant in ('original', 'open', 'panels')
               if (path := HERE / 'images' / f'{variant}-metrics.json').exists()}
    html = gallery.read_text()
    html = re.sub(r'/\* METRICS_START \*/[\s\S]*?/\* METRICS_END \*/',
                  '/* METRICS_START */ ' + json.dumps(metrics) + ' /* METRICS_END */', html)
    gallery.write_text(html)


def select_agenda_item(window):
    """Show the actual agenda selection state, independently of creation target."""
    item = window.results.topLevelItem(1)
    if item is not None:
        window.results.setCurrentItem(item)
        window.results.clearFocus()


def capture(variant):
    original = variant == 'original'
    if not original:
        install_paint_palette(background='#ffffff', ink='#27313a', muted='#596570',
                              line='#e0e5e8', accent='#475f52', text_scale=1.0)
        calendar_styles.install()
    app, store, window, patches = make_sample_window()
    output = HERE / 'images'
    output.mkdir(exist_ok=True)
    metrics = {}
    try:
        if original:
            import concept_b
            concept_b.apply(window)
        else:
            sidebar_styles.apply(window, variant=variant)
            header_styles.apply(window)
            calendar_styles.apply(window)
        for state in ('week', 'narrow', 'month', 'yellow', 'search'):
            window.resize(*((1030, 690) if state == 'narrow' else (1440, 960)))
            window.set_view('month' if state == 'month' else 'week')
            if state == 'yellow':
                store.save_calendar(replace(store.get_calendar('work'), color='#ffff00'))
                window.refresh(calendars=True)
                if not original:
                    sidebar_styles.apply_rows(window, variant=variant)
            if state == 'search':
                window.search.setText('work')
                QTest.qWait(250)
            window.select_event('0')
            select_agenda_item(window)
            settle(app)
            if state == 'week':
                rect = next((rect for rect, event, _ in window.canvas.rects() if event.id == '5'), None)
                if rect is not None:
                    QTest.mouseMove(window.canvas, rect.center().toPoint())
                    settle(app)
            path = output / f'{variant}-{state}.png'
            if not window.grab().save(str(path)):
                raise RuntimeError('Could not save ' + str(path))
            calendar = next(iter(window.calendar_rows.values()))
            heading = window.heading
            heading_baseline = (heading.mapTo(window, QPoint(0, 0)).y() + heading.height()
                                - heading.contentsMargins().bottom() - heading.fontMetrics().descent())
            metrics[state] = {
                'size': [window.width(), window.height()],
                'bar_x': calendar.swatch.mapTo(window, QPoint(0, 0)).x(),
                'text_x': calendar.choice.mapTo(window, QPoint(0, 0)).x(),
                'list_top': window.calendar_scroll.mapTo(window, QPoint(0, 0)).y(),
                'list_bottom': window.results.mapTo(window, QPoint(0, 0)).y() + window.results.height(),
                'heading_baseline': heading_baseline,
                'controls_y': window.view_control.mapTo(window, QPoint(0, 0)).y() + window.view_control.height() / 2,
                'range_y': window.range_label.mapTo(window, QPoint(0, 0)).y() + window.range_label.height() / 2,
            }
            if state == 'yellow':
                store.save_calendar(replace(store.get_calendar('work'), color='#587c67'))
                window.refresh(calendars=True)
                if not original:
                    sidebar_styles.apply_rows(window, variant=variant)

        source = ImportSource.from_data(export_ics([store.get_event(value) for value in
                                                   ('0', '5', '6', 'autumn-break')]), 'Planning.ics')
        dialog = ImportDialog(store.list_calendars(), sources=[source], calendar_id='work',
                              preferences=window.preferences)
        dialog.setStyleSheet(window.styleSheet())
        if original:
            concept_b.apply_dialog(dialog)
        else:
            sidebar_styles.apply_dialog(dialog, variant=variant)
            calendar_styles.apply_dialog(dialog)
        dialog.resize(980, 680)
        dialog.show()
        clock = QElapsedTimer()
        clock.start()
        while not dialog.preview_ready and clock.elapsed() < 10000:
            app.processEvents()
            QTest.qWait(5)
        if not dialog.preview_ready:
            raise RuntimeError('Import preview did not load')
        dialog._toggle_occurrence(next(event for event in dialog._page.events if event.title == 'Plan the week'))
        while not dialog.preview_ready and clock.elapsed() < 10000:
            app.processEvents()
            QTest.qWait(5)
        settle(app)
        if not dialog.grab().save(str(output / f'{variant}-import.png')):
            raise RuntimeError('Could not save import preview')
        dialog.reject()
        dialog.deleteLater()
        settle(app)
        (output / f'{variant}-metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    finally:
        window.close()
        window.executor.shutdown(wait=True)
        store.close()
        for patcher in reversed(patches):
            patcher.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=('original', 'open', 'panels'))
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        for variant in ('original', 'open', 'panels'):
            subprocess.run([sys.executable, __file__, '--variant', variant], check=True)
    elif args.variant:
        capture(args.variant)
    else:
        parser.error('Choose --all or --variant')
    update_gallery_metrics()
