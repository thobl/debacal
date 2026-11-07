"""Weekend treatments retain their meaning on today's date across every view."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas, paint_colors
from debacal.calendar_appearance import CalendarDateGrid
from debacal.dialogs import EventDialog
from debacal.mini_calendar import MiniCalendar
from debacal.preferences import Preferences
from debacal.models import Event
from debacal.series_preview import SeriesPreview
from debacal.theme import scheme_for
from debacal.year_view import YearView


TODAY = date(2026, 11, 7)
FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=index) for index in range(7)]
INSTANT = datetime(2026, 11, 7, 9, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('debacal.calendar_widgets.now', return_value=INSTANT), \
         patch('debacal.year_view.now', return_value=INSTANT), \
         patch('debacal.calendar_appearance.now', return_value=INSTANT), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(kind, preferences):
            widget = kind()
            widgets.append(widget)
            widget.set_preferences(preferences)
            widget.resize(1000, 700)
            if kind is DayHeader:
                widget.days = DAYS
            elif kind in (CalendarCanvas, AllDayCanvas):
                widget.set_data(DAYS, [], [])
                if kind is CalendarCanvas:
                    widget.fixed_all_day = True
                    widget.fit_to_viewport(600)
            elif kind is MonthCanvas:
                widget.set_data(FIRST, 11, [], [])
            elif kind is YearView:
                widget.set_data(2026, [], [])
            widget.show()
            app.processEvents()
            return widget
        yield app, make, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('style', ['text', 'background', 'none'])
def test_weekend_today_keeps_the_chosen_text_treatment_in_day_month_and_year(desktop, mode, style):
    _, make, _ = desktop
    preferences = Preferences(locale_name='en_GB', appearance_mode=mode, weekend_style=style)
    scheme = scheme_for(preferences)
    expected = QColor(scheme.weekend if style == 'text' else scheme.accent)
    header, month, year = [make(kind, preferences) for kind in (DayHeader, MonthCanvas, YearView)]
    for surface in (header, month):
        with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as draw:
            surface.grab()
        number = next(call for call in draw.call_args_list if call.args[2] == '7'
                      and call.args[1].left() > (header.gutter if surface is header else month.week_gutter))
        assert number.args[3] == expected
        if surface is header:
            weekday_number = next(call for call in draw.call_args_list if call.args[2] == '2'
                                  and call.args[1].left() > header.gutter)
            assert number.args[4:6] == weekday_number.args[4:6]
    target = year.day_rect(TODAY)
    with patch.object(year, '_text', wraps=year._text) as draw:
        year.grab()
    number = next(call for call in draw.call_args_list if call.args[2] == '7'
                  and target.contains(call.args[1].center()))
    assert number.args[3] == expected


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('style', ['text', 'background', 'none'])
def test_weekend_background_is_consistent_in_header_timed_all_day_month_and_year(desktop, mode, style):
    _, make, _ = desktop
    preferences = Preferences(appearance_mode=mode, weekend_style=style, time_format='24h')
    scheme = scheme_for(preferences)
    for kind in (DayHeader, CalendarCanvas, AllDayCanvas):
        surface = make(kind, preferences)
        image = surface.grab().toImage()
        column = (surface.width() - surface.gutter) / 7
        weekday = QPoint(round(surface.gutter + .5 * column), surface.height() - 3 if kind is DayHeader else 15)
        weekend = QPoint(round(surface.gutter + 6.5 * column), weekday.y())
        assert image.pixelColor(weekday) == QColor(scheme.background)
        assert image.pixelColor(weekend) == QColor(scheme.weekend_background if style == 'background' else scheme.background)
    month = make(MonthCanvas, preferences)
    image = month.grab().toImage()
    width = (month.width() - month.week_gutter) / 7
    height = (month.height() - month.header_height) / 6
    # Today's background cue remains present below its inset date marker.
    weekend = QPoint(round(month.week_gutter + 5.5 * width), round(month.header_height + height - 5))
    assert image.pixelColor(weekend) == QColor(scheme.weekend_background if style == 'background' else scheme.selection)
    year = make(YearView, preferences)
    target = year.day_rect(date(2026, 11, 8))
    image = year.grab().toImage()
    point = QPoint(round(target.left() + 3), round(target.top() + 2))
    assert image.pixelColor(point) == QColor(scheme.weekend_background if style == 'background' else scheme.panel)


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('style', ['text', 'background', 'none'])
def test_selected_weekend_today_in_native_overview_and_date_popups_retains_its_treatment(desktop, mode, style):
    _, make, widgets = desktop
    preferences = Preferences(appearance_mode=mode, weekend_style=style)
    overview = make(MiniCalendar, preferences)
    overview.setCurrentPage(2026, 11)
    overview.setSelectedDate(QDate(2026, 11, 7))
    dialog = EventDialog([], INSTANT, preferences=preferences)
    widgets.append(dialog)
    scheme = scheme_for(preferences)
    for calendar in (overview.calendar, dialog.start.calendarWidget(), dialog.end.calendarWidget(), dialog.repeat_until.calendarWidget()):
        assert isinstance(calendar, CalendarDateGrid)
        calendar.setCurrentPage(2026, 11)
        calendar.setSelectedDate(QDate(2026, 11, 7))
        pixels = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
        pixels.fill('#000000')
        painter = QPainter(pixels)
        calendar.paintCell(painter, QRect(0, 0, 60, 40), QDate(2026, 11, 7))
        painter.end()
        expected = QColor(scheme.weekend if style == 'text' else scheme.ink)
        assert any(pixels.pixelColor(x, y) == expected for x in range(10, 50) for y in range(4, 32))
        assert pixels.pixelColor(0, 0) == QColor(scheme.weekend_background if style == 'background' else scheme.background)
        format = calendar.weekdayTextFormat(Qt.DayOfWeek.Saturday)
        assert format.foreground().color() == expected


def test_resolved_custom_dark_calendar_palette_does_not_fall_back_through_its_name(desktop):
    _, make, _ = desktop
    preferences = Preferences(appearance_mode='dark', color_scheme='custom', custom_accent='#00ff00', custom_weekend='#ff00ff')
    header = make(DayHeader, preferences)
    assert paint_colors(header).background == QColor(scheme_for(preferences).background)
    assert paint_colors(header).weekend == QColor(scheme_for(preferences).weekend)
    previous = paint_colors(header)
    header.set_preferences(replace(preferences, custom_weekend='#00ffff'))
    assert paint_colors(header).weekend != previous.weekend


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('style', ['text', 'background', 'none'])
def test_series_preview_keeps_weekend_cues_on_event_dates(desktop, mode, style):
    app, _, widgets = desktop
    preferences = Preferences(appearance_mode=mode, weekend_style=style, locale_name='en_GB')
    preview = SeriesPreview()
    widgets.append(preview)
    preview.set_events([Event('sample', 'local', 'Sample', INSTANT, INSTANT + timedelta(hours=1))], preferences)
    preview.resize(800, 500)
    preview.show()
    app.processEvents()
    from debacal import series_preview
    with patch('debacal.series_preview.text', wraps=series_preview.text) as draw:
        image = preview.grid.grab().toImage()
    colors = scheme_for(preferences)
    target = preview.grid.cell_rect(TODAY)
    number = next(call for call in draw.call_args_list if call.args[2] == '7'
                  and target.contains(call.args[1].center()))
    assert number.args[3] == QColor(colors.weekend if style == 'text' else colors.accent)
    sunday = preview.grid.cell_rect(TODAY + timedelta(days=1))
    point = QPoint(round(sunday.left() + 4), round(sunday.bottom() - 4))
    assert image.pixelColor(point) == QColor(colors.weekend_background if style == 'background' else colors.background)
