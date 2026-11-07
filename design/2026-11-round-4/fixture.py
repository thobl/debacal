"""Native review fixture using the current application and synthetic events."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from debacal.__main__ import configure_application, seed_demo
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


NOW = datetime(2026, 11, 6, 13, 15, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(8):
        app.processEvents()


def make_window(color_scheme='neutral', *, font_scale=100):
    app = QApplication.instance() or QApplication([])
    configure_application(app)
    patches = [patch(module + '.now', return_value=NOW) for module in
               ('debacal.__main__', 'debacal.ui', 'debacal.calendar_widgets',
                'debacal.search_panel', 'debacal.year_view', 'debacal.import_preview',
                'debacal.import_preparation')]
    for item in patches:
        item.start()
    store = Store(':memory:')
    seed_demo(store)
    store.save_calendar(Calendar('holidays', 'Public holidays', '#b78247', kind='ics', read_only=True))
    midnight = NOW.replace(day=5, hour=0, minute=0)
    start = NOW.replace(day=2, hour=15, minute=30)
    store.apply('Synthetic design examples', [
        Event('autumn-break', 'local', 'Autumn break', midnight, midnight + timedelta(days=3), all_day=True),
        Event('weekly-work-review', 'work', 'Weekly work review', start, start + timedelta(minutes=45),
              recurrence_rule='FREQ=WEEKLY;COUNT=6')])
    Preferences(start_weekday=0, date_format='european', time_format='24h', locale_name='en_GB',
                visible_start_hour=8, visible_end_hour=20, color_scheme=color_scheme,
                font_scale=font_scale).save(store)
    window = MainWindow(store, enable_sync=False, profile_label='Testing')
    window.resize(1440, 960)
    window.go_date(NOW.date())
    window.show()
    select_result(window)
    settle(app)
    return app, store, window, patches


def select_result(window):
    item = window.results.topLevelItem(1)
    if item is not None:
        window.results.clearSelection()
        window.results.setCurrentItem(None)
        window.results.setCurrentItem(item)
        window.results.clearFocus()


def close_window(app, store, window, patches):
    window.close()
    window.executor.shutdown(wait=True)
    window.deleteLater()
    settle(app)
    store.close()
    for item in reversed(patches):
        item.stop()
