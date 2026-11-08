"""Source identity stays visible in event authoring and its live calendar preview."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet, widget_palette
from debacal.dialogs import EventDialog
from debacal.event_style import event_colors
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview, text


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
CALENDARS = [Calendar('violet', 'Community', '#9457b5'), Calendar('gold', 'Personal', '#e8bd35')]


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def show(widget, preferences):
            owned.append(widget)
            widget.setStyleSheet(stylesheet(preferences.font_scale, preferences))
            widget.setPalette(widget_palette(preferences))
            widget.show()
            for _ in range(4):
                app.processEvents()
            return widget
        yield app, show
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def chip_pixel(preview, day):
    rect = preview.grid.chip_rects(day)[0][0]
    # Sample the unpainted card interior, clear of the clock, primary pin and ×.
    point = QPoint(round(rect.right() - 6), round(rect.top() + 3))
    return preview.grid.grab().toImage().pixelColor(point)


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_calendar_choice_updates_color_and_target_for_current_and_clicked_dates(desktop, mode):
    app, show = desktop
    preferences = Preferences(0, 'iso', '24h', appearance_mode=mode)
    dialog = show(EventDialog(CALENDARS, START, preferences=preferences, default_calendar_id='gold'), preferences)
    for calendar in reversed(CALENDARS):
        dialog.calendar.setCurrentIndex(dialog.calendar.findData(calendar.id))
        app.processEvents()
        assert dialog.preview.masters[0].calendar_id == calendar.id
        assert chip_pixel(dialog.preview, START.date()) == QColor(event_colors(calendar.color, preferences).fill)
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            swatch = dialog.calendar.itemIcon(dialog.calendar.currentIndex()).pixmap(QSize(16, 16), mode).toImage()
            assert swatch.pixelColor(swatch.width() // 2, swatch.height() // 2) == QColor(calendar.color)
    added = date(2026, 11, 10)
    QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton,
                     pos=dialog.preview.grid.cell_rect(added).center().toPoint())
    app.processEvents()
    assert dialog.repeat.currentData() == 'DAILY'  # Two dates become a regular six-day interval.
    assert len(dialog.preview.events_on(added)) == 1
    assert chip_pixel(dialog.preview, added) == QColor(event_colors(CALENDARS[0].color, preferences).fill)


def test_multi_source_preview_retains_colors_when_only_events_are_updated(desktop):
    _, show = desktop
    preferences = Preferences(0, 'iso', '24h')
    first = Event('first', 'violet', 'First event', START, START + timedelta(hours=1))
    second = replace(first, id='second', calendar_id='gold', start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1))
    preview = SeriesPreview()
    preview.resize(700, 500)
    preview.set_events([first, second], preferences, CALENDARS)
    show(preview, preferences)
    for event, calendar in zip((first, second), CALENDARS):
        assert chip_pixel(preview, event.start.date()) == QColor(event_colors(calendar.color, preferences).fill)
    preview.set_events([second])
    assert chip_pixel(preview, second.start.date()) == QColor(event_colors(CALENDARS[1].color, preferences).fill)


def test_date_markers_are_replaced_by_actual_occurrences_without_losing_exclusions(desktop):
    _, show = desktop
    preferences = Preferences(0, 'iso', '24h')
    source = Event('series', 'violet', 'Appointments', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=4', recurrence_dates=((START + timedelta(days=6)).isoformat(),),
                   excluded_dates=((START + timedelta(days=1)).isoformat(),))
    preview = SeriesPreview()
    preview.resize(700, 500)
    preview.set_events([source], preferences, CALENDARS)
    preview.enable_date_editing(True)
    show(preview, preferences)
    with patch('debacal.series_preview.text', wraps=text) as draw:
        pixels = preview.grid.grab().toImage()
    labels = [call.args[2] for call in draw.call_args_list]
    assert not {'+', '×', '●', 'skipped'}.intersection(labels)
    assert 'Added date' not in preview.summary.text() and 'Skipped date' not in preview.summary.text()
    skipped = START.date() + timedelta(days=1)
    blank = preview.grid.cell_rect(skipped)
    assert pixels.pixelColor(round(blank.left() + 9), round(blank.top() + 25)) == QColor('#ffffff')
    assert not preview.events_on(skipped)
    assert len(preview.events_on(START.date() + timedelta(days=6))) == 1
    assert preview.masters == [source]
