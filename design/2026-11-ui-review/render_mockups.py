"""Render sample-only design alternatives. Does not modify the running app."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

from mockup_base import make_sample_window, settle
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from debacal.ics import export_ics
from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource


HERE = Path(__file__).resolve().parent


def capture(concept):
    app, store, window, patches = make_sample_window()
    module = None
    if concept != 'current':
        module = __import__('concept_' + concept)
        module.apply(window)
    output = HERE / 'images'
    output.mkdir(exist_ok=True)
    try:
        for state in ('week', 'narrow', 'month', 'yellow'):
            window.resize(*(1030, 690) if state == 'narrow' else (1440, 960))
            window.set_view('month' if state == 'month' else 'week')
            if state == 'yellow':
                store.save_calendar(replace(store.get_calendar('work'), color='#ffff00'))
                window.refresh(calendars=True)
            window.select_event('0')
            settle(app)
            if state == 'week':
                rect = next((rect for rect, event, _number in window.canvas.rects() if event.id == '5'), None)
                if rect:
                    QTest.mouseMove(window.canvas, rect.center().toPoint())
                    settle(app)
            if not window.grab().save(str(output / f'{concept}-{state}.png')):
                raise RuntimeError('Could not save ' + concept + '-' + state)
            if state == 'yellow':
                store.save_calendar(replace(store.get_calendar('work'), color='#587c67'))
                window.refresh(calendars=True)

        sources = ImportSource.from_data(export_ics([store.get_event('0'), store.get_event('5'),
                                                    store.get_event('6'), store.get_event('autumn-break')]),
                                         'Planning.ics')
        dialog = ImportDialog(store.list_calendars(), sources=[sources], calendar_id='work',
                              preferences=window.preferences)
        dialog.setStyleSheet(window.styleSheet())
        if module and hasattr(module, 'apply_dialog'):
            module.apply_dialog(dialog)
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
        dialog.grab().save(str(output / f'{concept}-import.png'))
        dialog.reject()
        dialog.deleteLater()
        settle(app)
    finally:
        window.close()
        window.executor.shutdown(wait=True)
        store.close()
        for item in reversed(patches):
            item.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--concept', choices=('current', 'a', 'b', 'c'))
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        for value in ('current', 'a', 'b', 'c'):
            subprocess.run([sys.executable, __file__, '--concept', value], check=True)
    elif args.concept:
        capture(args.concept)
    else:
        parser.error('Choose --all or --concept')
