"""Render distinct design hypotheses using one account-free native fixture."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

from exploration_base import make_window, settle
from PySide6.QtCore import QPoint, QRect
from PySide6.QtTest import QTest

HERE = Path(__file__).resolve().parent


def capture(direction):
    module = __import__('direction_' + direction)
    app, store, window, patches = make_window(direction)
    output = HERE / 'images'
    output.mkdir(exist_ok=True)
    try:
        module.apply(window)
        for state in ('week', 'month', 'narrow', 'search', 'yellow'):
            window.resize(*((1030, 690) if state == 'narrow' else (1440, 960)))
            window.set_view('month' if state == 'month' else 'week')
            if state == 'search':
                window.search.setText('work')
                QTest.qWait(250)
            if state == 'yellow':
                window.search.clear()
                QTest.qWait(250)
                store.save_calendar(replace(store.get_calendar('work'), color='#ffff00'))
                window.refresh(calendars=True)
                if hasattr(module, 'refresh_rows'):
                    module.refresh_rows(window)
            window.select_event('0')
            item = window.results.topLevelItem(1)
            if state == 'search':
                for index in range(window.results.topLevelItemCount()):
                    root = window.results.topLevelItem(index)
                    if root.text(0) == 'Weekly work review':
                        root.setExpanded(True)
                        QTest.qWait(30)
                        item = root.child(0) or root
                        break
            if item is not None:
                window.results.clearSelection()
                window.results.setCurrentItem(None)
                window.results.setCurrentItem(item)
            window.results.clearFocus()
            settle(app)
            if state == 'week':
                rect = next((rect for rect, event, _ in window.canvas.rects() if event.id == '5'), None)
                if rect is not None:
                    QTest.mouseMove(window.canvas, rect.center().toPoint())
                    settle(app)
                if direction == 'sources':
                    width = (window.all_day_canvas.width() - window.all_day_canvas.gutter) / 7
                    QTest.mouseMove(window.all_day_canvas, QPoint(int(window.all_day_canvas.gutter + width - 15), 15))
                    settle(app)
            if not window.grab().save(str(output / f'{direction}-{state}.png')):
                raise RuntimeError('Could not save screenshot')
            if state == 'week':
                # Native widget-region captures for comparisons at legible size.
                width = window.sidebar.width()
                window.grab(QRect(0, 0, width, window.height())).save(str(output / f'{direction}-sidebar.png'))
                window.workspace_header.grab().save(str(output / f'{direction}-header.png'))
                window.header.parentWidget().grab(QRect(0, 0, window.header.width(),
                    window.header.height() + window.all_day_scroll.height())).save(str(output / f'{direction}-all-day.png'))
        if hasattr(module, 'verify'):
            module.verify(window)
    finally:
        window.close()
        window.executor.shutdown(wait=True)
        store.close()
        for patcher in reversed(patches):
            patcher.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--direction', choices=('sources', 'shared', 'type'))
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.all:
        for direction in ('sources', 'shared', 'type'):
            subprocess.run([sys.executable, __file__, '--direction', direction], check=True)
    elif args.direction:
        capture(args.direction)
    else:
        parser.error('Choose --all or --direction')
