"""Accepted design choices stay consistent through real workspace operations."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from debacal.dialogs import EventDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.theme import scheme_for
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))


def settle(app):
    for _ in range(12):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        window.go_date(START.date())
        window.show()
        settle(app)
        try:
            yield app, store, window
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def test_saved_palette_updates_the_workspace_without_altering_sources(workspace):
    app, store, window = workspace
    source = Calendar('remote', 'Server yellow', '#ffff00', kind='caldav',
                      url='https://calendar.invalid/', server_color='#ffff00')
    store.save_calendar(source)
    for name in ('warm', 'cool', 'neutral'):
        def accept(dialog):
            dialog.color_scheme.setCurrentIndex(dialog.color_scheme.findData(name))
            dialog.save()
            return dialog.result()
        with patch.object(PreferencesDialog, 'exec', accept):
            window.show_preferences()
        settle(app)
        assert Preferences.load(store).color_scheme == name
        assert window.canvas.preferences.color_scheme == window.results.preferences.color_scheme == name
        assert store.get_calendar('remote') == source
        body = window.calendar_box.parentWidget()
        assert body.grab().toImage().pixelColor(1, 1).name() == scheme_for(name).sidebar


@pytest.mark.parametrize('timed_scroll,all_day_scroll', [(False, False), (True, False), (False, True), (True, True)])
def test_pinned_and_timed_columns_align_with_either_scrollbar(workspace, timed_scroll, all_day_scroll):
    app, store, window = workspace
    if all_day_scroll:
        start = START.replace(hour=0)
        store.apply('Crowded all-day lane', [Event(f'all-day-{index}', 'local', f'Span {index}',
                    start, start + timedelta(days=2), all_day=True) for index in range(12)])
    # A crowded pinned lane in a short window can legitimately make both
    # surfaces scroll. Give this case enough height to isolate its scrollbar.
    window.resize(1030, 960 if all_day_scroll and not timed_scroll else 690)
    window.full_day = timed_scroll
    window.refresh()
    settle(app)
    assert window.scroll.verticalScrollBar().isVisible() == timed_scroll
    assert window.all_day_scroll.verticalScrollBar().isVisible() == all_day_scroll
    assert window.header.width() == window.canvas.width() == window.all_day_canvas.width()
    origins = [widget.mapTo(window, widget.rect().topLeft()).x()
               for widget in (window.header, window.canvas, window.all_day_canvas)]
    assert len(set(origins)) == 1
    assert not window.scroll.horizontalScrollBar().isVisible()
    assert not window.all_day_scroll.horizontalScrollBar().isVisible()


@pytest.mark.parametrize('name', ['neutral', 'warm', 'cool'])
def test_event_date_popups_keep_subtle_weekend_colors(name):
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        dialog = EventDialog(store.list_calendars(), START, preferences=Preferences(color_scheme=name))
        try:
            for field in (dialog.start, dialog.end, dialog.repeat_until):
                for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
                    assert field.calendarWidget().weekdayTextFormat(day).foreground().color().name() == scheme_for(name).weekend
        finally:
            dialog.close()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
