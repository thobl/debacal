"""Calendar chrome stays continuous, quiet, and independent between previews."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets as widgets
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas, text
from debacal.mini_calendar import MiniCalendar
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview
from debacal.theme import scheme_for
from debacal.year_view import YearView


FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=index) for index in range(7)]
ZONE = ZoneInfo('Europe/Berlin')


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.calendar_widgets.now', return_value=datetime(2027, 1, 1, tzinfo=ZONE)), \
            patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(kind, preferences=None):
            widget = kind()
            owned.append(widget)
            preferences = preferences or Preferences(locale_name='en_GB')
            if kind is SeriesPreview:
                widget.set_events([], preferences)
            else:
                widget.set_preferences(preferences)
            widget.resize(1050, 600)
            if kind is DayHeader:
                widget.days = DAYS
            elif kind in (CalendarCanvas, AllDayCanvas):
                widget.set_data(DAYS, [], [])
                if kind is CalendarCanvas:
                    widget.fixed_all_day = True
                    widget.fit_to_viewport(600)
            elif kind is MonthCanvas:
                widget.set_data(FIRST, 11, [], [])
            widget.show()
            app.processEvents()
            return widget
        yield app, make
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
def test_day_columns_continue_through_compact_header_and_real_all_day_lane(desktop, scheme):
    _, make = desktop
    preferences = Preferences(color_scheme=scheme, locale_name='en_GB')
    header, strip, canvas = [make(kind, preferences) for kind in (DayHeader, AllDayCanvas, CalendarCanvas)]
    assert header.height() == 64
    captures = [surface.grab().toImage() for surface in (header, strip, canvas)]
    background = QColor(scheme_for(preferences).background)
    for surface, image in zip((header, strip, canvas), captures):
        column = (surface.width() - surface.gutter) / len(DAYS)
        for index in range(7):
            # Edges share the same geometry; weekday and weekend interiors have
            # the same background rather than decorative weekend column washes.
            edge = round(surface.gutter + index * column)
            assert image.pixelColor(edge, 15) != background
            inside = round(surface.gutter + (index + .6) * column)
            assert image.pixelColor(inside, surface.height() - 4 if surface is header else 15) == background


def test_removing_creation_strip_keeps_header_and_pinned_lane_gestures(desktop):
    app, make = desktop
    header, strip = make(DayHeader), make(AllDayCanvas)
    for surface in (header, strip):
        created = QSignalSpy(surface.create_all_day_requested)
        point = QPoint(round(surface.gutter + (surface.width() - surface.gutter) / 14), 25)
        QTest.mouseMove(surface, point)
        app.processEvents()
        with patch.object(widgets, 'text', wraps=text) as rendered:
            surface.grab()
        assert not any('+' in str(call.args[2]) for call in rendered.call_args_list)
        QTest.mouseDClick(surface, Qt.MouseButton.LeftButton, pos=point)
        assert created.count() == 1
        assert created.at(0)[0].date() == FIRST
        assert created.at(0)[1].date() == FIRST + timedelta(days=1)


def test_week_and_month_share_weekday_role_and_subtle_weekend_text(desktop):
    _, make = desktop
    sizes = []
    for surface in (make(DayHeader), make(MonthCanvas)):
        with patch.object(widgets, 'text', wraps=text) as rendered:
            surface.grab()
        saturday = next(call for call in rendered.call_args_list if call.args[2] == 'SAT')
        monday = next(call for call in rendered.call_args_list if call.args[2] == 'MON')
        assert saturday.args[3] == QColor(scheme_for(surface).weekend)
        assert saturday.args[3] != monday.args[3]
        sizes.append(saturday.args[4])
    assert sizes == [widgets.WEEKDAY_SIZE] * 2


def test_scheme_previews_do_not_recolor_other_calendar_instances(desktop):
    _, make = desktop
    first = make(DayHeader, Preferences(color_scheme='warm'))
    original = first.grab().toImage()
    second = make(DayHeader, Preferences(color_scheme='cool'))
    assert second.grab().toImage() != original
    assert first.grab().toImage() == original
    second.set_preferences(replace(second.preferences, color_scheme='neutral'))
    assert first.grab().toImage() == original


@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
def test_overview_weekends_follow_scheme_without_default_red_or_a_surface_patch(desktop, scheme):
    _, make = desktop
    overview = make(MiniCalendar, Preferences(color_scheme=scheme))
    colors = scheme_for(scheme)
    assert overview.calendar.weekdayTextFormat(Qt.DayOfWeek.Sunday).foreground().color() == QColor(colors.weekend)
    assert overview.calendar.weekdayTextFormat(Qt.DayOfWeek.Monday).foreground().color() == QColor(colors.ink)
    assert overview.calendar.palette().color(overview.calendar.backgroundRole()) == QColor(colors.background)


def test_year_and_series_preview_render_their_own_scheme(desktop):
    _, make = desktop
    for kind in (YearView, SeriesPreview):
        warm = make(kind, Preferences(color_scheme='warm'))
        cool = make(kind, Preferences(color_scheme='cool'))
        assert warm.grab().toImage() != cool.grab().toImage()


def test_overview_and_year_popup_controls_show_keyboard_focus_without_resizing(desktop):
    app, make = desktop
    overview = make(MiniCalendar)
    overview.activateWindow()
    for button in (overview.previous_button, overview.month_button):
        overview.next_button.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        before, geometry = button.grab().toImage(), button.geometry()
        button.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        assert button.hasFocus()
        assert button.grab().toImage() != before
        assert button.geometry() == geometry
    overview.show_year_popup()
    popup = overview.year_popup
    for button in (popup.year_buttons[0], popup.previous_button):
        popup.year_input.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        before, geometry = button.grab().toImage(), button.geometry()
        button.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        assert button.hasFocus()
        assert button.grab().toImage() != before
        assert button.geometry() == geometry
    popup.close()
