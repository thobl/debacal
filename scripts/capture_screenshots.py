"""Regenerate documentation screenshots from isolated, synthetic demo profiles.

Run with .venv/bin/python scripts/capture_screenshots.py. Captures the real Qt
interface using its offscreen backend; no desktop session or account is needed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'docs' / 'screenshots')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    with TemporaryDirectory(prefix='debacal-screenshots-') as temporary, ExitStack() as context:
        # This helper never connects to a desktop or uses existing application
        # data, settings, sync mirrors, or credentials.
        context.enter_context(patch.dict(os.environ, {
            'QT_QPA_PLATFORM': 'offscreen', 'TZ': 'Europe/Berlin',
            'XDG_CONFIG_HOME': str(Path(temporary) / 'config'),
            'XDG_DATA_HOME': str(Path(temporary) / 'data'),
            'XDG_CACHE_HOME': str(Path(temporary) / 'cache'),
        }))
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtWidgets import QApplication
        from debacal.__main__ import configure_application, seed_demo
        from debacal.event_dialog import EventDialog
        from debacal.models import Event
        from debacal.preferences import Preferences
        from debacal.store import Store
        from debacal.ui import MainWindow

        zone = ZoneInfo('Europe/Berlin')
        sample_now = datetime(2026, 11, 11, 10, 20, tzinfo=zone)
        for module in ('__main__', 'timeutil', 'ui', 'calendar_widgets', 'calendar_appearance',
                       'date_time_fields', 'year_view', 'series_preview', 'search_panel'):
            context.enter_context(patch('debacal.' + module + '.now', return_value=sample_now))

        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        configure_application(app)
        callback_errors = []
        context.enter_context(patch('sys.excepthook', side_effect=lambda _kind, error, _trace: callback_errors.append(error)))
        preferences = Preferences(start_weekday=0, date_format='european', time_format='24h',
            locale_name='en_GB', font_family='DejaVu Sans', auto_sync_enabled=False)
        profiles, widgets = [], []

        def settle():
            for _ in range(10):
                app.processEvents()
            if callback_errors:
                raise RuntimeError('A screenshot callback failed: ' + str(callback_errors[0])) from callback_errors[0]

        def capture(widget, filename, width=1400, height=900, *, prepare=None):
            widget.resize(width, height)
            widget.show()
            widget.activateWindow()
            settle()
            if prepare is not None:
                prepare()
                settle()
            image = widget.grab()
            path = args.output_dir / filename
            if image.isNull() or not image.save(str(path)):
                raise RuntimeError('Could not save screenshot: ' + str(path))
            print(str(path))
            widget.hide()

        def profile(name, settings):
            store = Store(Path(temporary) / (name + '.sqlite3'))
            profiles.append(store)
            seed_demo(store)
            sample_events = []
            for event in store.list_events():
                if event.calendar_id == 'work':
                    event = replace(event, location='Studio')
                if event.id == '3':
                    event = replace(event, title='Workshop plan')
                elif event.id == '4':
                    begin = event.start.replace(hour=12, minute=30)
                    event = replace(event, start=begin, end=begin + timedelta(hours=1))
                elif event.id in ('5', '8'):
                    event = replace(event, title='Workshop · ' + ('A' if event.id == '5' else 'B'))
                elif event.id == '2':
                    event = replace(event, title='Riverside walk')
                elif event.id == '14':
                    event = replace(event, title='Dinner together')
                elif event.id == '16':
                    event = replace(event, title='Coffee & reading')
                sample_events.append(event)
            store.apply('Prepare concise sample event labels', sample_events)
            settings.save(store)
            store.set_default_calendar('work')
            return store

        try:
            week_store = profile('week', preferences)
            festival = sample_now.replace(day=13, hour=0, minute=0)
            week_store.apply('Add sample festival', [Event('festival', 'community', 'Neighborhood festival',
                festival, festival + timedelta(days=2), all_day=True)])
            week = MainWindow(week_store, enable_sync=False)
            widgets.append(week)
            week.go_date(sample_now.date())
            capture(week, 'calendar-week.png')

            dark = replace(preferences, appearance_mode='dark', year_view_style='tints')
            year_store = profile('year', dark)
            year_store.apply('Add synthetic recurring events', [
                Event('weekly-work', 'work', 'Project sync',
                    datetime(2026, 1, 6, 13, tzinfo=zone), datetime(2026, 1, 6, 14, tzinfo=zone),
                    recurrence_rule='FREQ=WEEKLY;COUNT=51'),
                Event('weekly-reading', 'community', 'Community lecture',
                    datetime(2026, 1, 8, 18, tzinfo=zone), datetime(2026, 1, 8, 19, tzinfo=zone),
                    recurrence_rule='FREQ=WEEKLY;COUNT=51'),
                Event('weekly-market', 'local', 'Weekend ride',
                    datetime(2026, 1, 10, 8, tzinfo=zone), datetime(2026, 1, 10, 10, tzinfo=zone),
                    recurrence_rule='FREQ=WEEKLY;COUNT=51'),
                Event('spring-break', 'local', 'Spring break',
                    datetime(2026, 4, 3, tzinfo=zone), datetime(2026, 4, 7, tzinfo=zone), all_day=True),
                Event('summer-holiday', 'local', 'Summer holiday',
                    datetime(2026, 8, 17, tzinfo=zone), datetime(2026, 8, 29, tzinfo=zone), all_day=True),
                Event('winter-break', 'local', 'Winter break',
                    datetime(2026, 12, 24, tzinfo=zone), datetime(2026, 12, 31, tzinfo=zone), all_day=True),
            ])
            year = MainWindow(year_store, enable_sync=False)
            widgets.append(year)
            year.go_date(sample_now.date())
            year.set_view('year')
            capture(year, 'calendar-year-dark.png')

            editor_store = profile('editor', preferences)
            start = sample_now.replace(hour=9, minute=30)
            editor = EventDialog(editor_store.list_calendars(), start, start + timedelta(minutes=90),
                                 preferences=preferences, default_calendar_id='community')
            widgets.append(editor)
            editor.title.setText('Studio sessions')
            editor.location.setText('Community workshop')
            editor.description.setPlainText('Sketch · build · share\nBring your ideas for the next workshop.')
            editor.pattern.setText('+0d 13. 17. 11:00 20. 24. 27.')
            def focus_sequence():
                editor.pattern.setFocus(Qt.FocusReason.OtherFocusReason)
                editor.pattern.deselect()
                editor.pattern.setCursorPosition(len(editor.pattern.text()))
            capture(editor, 'event-editor.png', width=1200, height=790, prepare=focus_sequence)
        finally:
            for widget in reversed(widgets):
                widget.close()
                executor = getattr(widget, 'executor', None)
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            for store in profiles:
                store.close()
            if callback_errors and sys.exc_info()[0] is None:
                raise RuntimeError('Screenshot cleanup failed: ' + str(callback_errors[0])) from callback_errors[0]


if __name__ == '__main__':
    main()
