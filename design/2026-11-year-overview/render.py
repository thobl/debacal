"""Render three year-overview studies from native widgets and synthetic data.

Run from the repository root:
  QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-year-overview/render.py
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, time, timedelta
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet, widget_palette
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow
from options import OPTIONS, YearPresenceStudy


HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'
ZONE = ZoneInfo('Europe/Berlin')
NOW = datetime(2026, 11, 8, 13, 15, tzinfo=ZONE)
CALENDARS = (
    Calendar('work', 'Work', '#547797'),
    Calendar('local', 'Personal', '#a36f59'),
    Calendar('projects', 'Projects', '#557e69'),
    Calendar('family', 'Family', '#9b85ad'),
    Calendar('holidays', 'Public holidays', '#b69b46', read_only=True),
    Calendar('sport', 'Sport', '#6f9c9a'),
    Calendar('archive', 'Archive', '#ef2639', visible=False),
)


def settle(app):
    for _ in range(8):
        app.processEvents()


def fixture_events():
    events = []
    def add(identity, calendar, day, hour=9, duration=60, title='Appointment', *, all_day=False):
        start = datetime.combine(day, time.min if all_day else time(hour), ZONE)
        end = start + (timedelta(days=duration) if all_day else timedelta(minutes=duration))
        events.append(Event(identity, calendar, title, start, end, all_day=all_day))
    for offset in range(365):
        day = date(2026, 1, 1) + timedelta(days=offset)
        if day.weekday() in (0, 2, 4):
            add(f'work-{offset}', 'work', day, title='Project work')
            if day.weekday() == 2:
                add(f'work-extra-{offset}', 'work', day, 14, title='Team review')
        if day.weekday() in (2, 5):
            add(f'personal-{offset}', 'local', day, 18 if day.weekday() == 2 else 10, title='Personal plans')
        if day.weekday() in (1, 3):
            add(f'sport-{offset}', 'sport', day, 18, title='Training')
        if day.weekday() == 6:
            add(f'family-{offset}', 'family', day, 12, 90, title='Family lunch')
    for month in range(1, 13):
        add(f'project-{month}', 'projects', date(2026, month, 16), duration=3,
            title='Project workshop', all_day=True)
    for day in (date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1),
                date(2026, 5, 14), date(2026, 10, 3), date(2026, 12, 25), date(2026, 12, 26)):
        add('holiday-' + day.isoformat(), 'holidays', day, duration=1, title='Public holiday', all_day=True)
    # Six distinct sources and several duplicate events challenge overflow.
    for index, calendar in enumerate(CALENDARS[:-1]):
        add(f'busy-{index}', calendar.id, date(2026, 11, 18), 9 + index, title='Busy day')
    add('busy-work-duplicate', 'work', date(2026, 11, 18), 17, title='Another work event')
    add('hidden-source', 'archive', date(2026, 11, 18), 9, title='Hidden archive event')
    return events


def save(pixmap, name):
    IMAGES.mkdir(exist_ok=True)
    assert pixmap.save(str(IMAGES / (name + '.png'))), name


def assert_summary(view):
    busy = view.calendars_on_day(date(2026, 11, 18))
    assert busy == tuple(calendar.id for calendar in CALENDARS[:-1]), busy
    assert 'archive' not in busy
    # One source stays one indicator despite duplicate appointments.
    assert len(busy) == 6 and len(view.events_on_day(date(2026, 11, 18))) > len(busy)
    for day in (date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)):
        assert 'projects' in view.calendars_on_day(day)
    assert 'projects' not in view.calendars_on_day(date(2026, 11, 19))
    view._ensure_geometry()
    assert len(view._month_rects) == 12 and len(view._day_rects) == 365
    assert not view._marker_hits and not view.hits


def main():
    app = QApplication.instance() or QApplication([])
    patches = [patch(module + '.now', return_value=NOW) for module in
               ('debacal.ui', 'debacal.calendar_widgets', 'debacal.calendar_appearance',
                'debacal.search_panel', 'debacal.year_view', 'options')]
    for item in patches:
        item.start()
    metrics = {}
    events = fixture_events()
    try:
        for mode in ('light', 'dark'):
            preferences = Preferences(start_weekday=0, date_format='european', time_format='24h',
                locale_name='en_GB', appearance_mode=mode, agenda_style='grouped')
            for key in OPTIONS:
                store = Store(':memory:')
                for calendar in CALENDARS:
                    store.save_calendar(replace(calendar, read_only=False))
                store.set_calendar_order(calendar.id for calendar in CALENDARS)
                store.apply('Synthetic year design fixture', events)
                for calendar in CALENDARS:
                    if calendar.read_only:
                        store.save_calendar(calendar)
                preferences.save(store)
                with patch('debacal.workspace_chrome.YearView', lambda: YearPresenceStudy(key)):
                    window = MainWindow(store, enable_sync=False)
                try:
                    window.resize(1440, 960)
                    window.anchor = NOW.date()
                    window.set_view('year')
                    window.show()
                    settle(app)
                    view = window.year
                    assert_summary(view)
                    assert view._columns(view.width()) == 4
                    save(window.grab(), f'{key}-{mode}-full')
                    save(view.grab(view._month_rects[11][0].toAlignedRect()), f'{key}-{mode}-detail')
                    day = view.day_rect(date(2026, 11, 18))
                    metrics[f'{key}-{mode}'] = dict(width=view.width(), height=view.height(),
                        day_width=round(day.width(), 2), day_height=round(day.height(), 2),
                        busy_calendars=list(view.calendars_on_day(date(2026, 11, 18))))
                    compact = YearPresenceStudy(key)
                    compact.set_preferences(preferences)
                    compact.setPalette(widget_palette(preferences))
                    compact.setStyleSheet(stylesheet(preferences.font_scale, preferences))
                    compact.set_data(2026, events, CALENDARS)
                    compact.resize(780, 1000)
                    compact.show()
                    settle(app)
                    assert_summary(compact)
                    assert compact._columns(compact.width()) == 3
                    save(compact.grab(), f'{key}-{mode}-compact')
                    compact.close()
                    compact.deleteLater()
                finally:
                    window.close()
                    window.executor.shutdown(wait=True)
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    app.processEvents()
                    store.close()
        (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    finally:
        for item in reversed(patches):
            item.stop()


if __name__ == '__main__':
    main()
