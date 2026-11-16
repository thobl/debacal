"""Theme changes repaint the current frame without laying out its events again."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, MonthCanvas, calendar_gutter, paint_colors
from debacal.models import Calendar, Event
from debacal.preferences import Preferences


BASE = Preferences(start_weekday=0, locale_name='en_GB', date_format='european', time_format='24h')
START = datetime(2026, 11, 2, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def calendar_zone():
    with patch('debacal.calendar_widgets.local_zone', return_value=timezone.utc):
        yield


@pytest.fixture(params=[CalendarCanvas, AllDayCanvas, MonthCanvas], ids=['week', 'all-day', 'month'])
def surface(request):
    app = QApplication.instance() or QApplication([])
    widget = request.param()
    widget.set_preferences(BASE)
    widget.resize(1120, 700)
    events = [Event('timed', 'local', 'Timed event', START, START + timedelta(hours=2)),
              Event('all-day', 'local', 'Whole span', START.replace(hour=0),
                    START.replace(hour=0) + timedelta(days=3), all_day=True)]
    days = [(START + timedelta(days=index)).date() for index in range(7)]
    calendars = [Calendar('local', 'Calendar', '#4f7865')]
    if type(widget) is MonthCanvas:
        widget.set_data(days[0], days[0].month, events, calendars)
    else:
        widget.set_data(days, events, calendars)
    if type(widget) is CalendarCanvas:
        widget.fit_to_viewport(700)
        widget.rects()
    elif type(widget) is AllDayCanvas:
        widget.rects()
    else:
        widget.month_layout()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_visual_and_unrelated_preferences_keep_event_placements(surface):
    original_shapes = dict(surface._shape_cache)
    original_events = surface.events
    original_background = paint_colors(surface).background.name()
    with patch.object(surface, 'invalidate_geometry', side_effect=AssertionError('Keep placements')), \
            patch.object(surface, 'day_buckets', side_effect=AssertionError('Do not rescan events')):
        for changes in (
                {'color_scheme': 'warm'}, {'appearance_mode': 'dark'},
                {'custom_accent': '#ab3421'}, {'custom_surface': '#343a42'}, {'custom_weekend': '#813b61'},
                {'corner_style': 'rounded'}, {'weekend_style': 'background'},
                {'control_style': 'filled'}, {'agenda_style': 'cards'}, {'year_view_style': 'tints'},
                {'show_sidebar_title': False}, {'sidebar_title': 'My calendar'},
                {'auto_sync_enabled': False}, {'sync_interval_minutes': 19},
                {'conflict_notifications': 'both'}, {'default_event_duration_minutes': 45},
                {'duration_presets_minutes': (15, 45)},
        ):
            preferences = replace(surface.preferences, **changes)
            surface.set_preferences(preferences)
            assert surface.preferences is preferences
            if type(surface) is CalendarCanvas:
                # The workspace fits again after applying preferences. This
                # second entry point must also leave event placement alone.
                surface.fit_to_viewport(700)
        assert surface.events is original_events
        assert surface._shape_cache.keys() == original_shapes.keys()
        assert all(surface._shape_cache[key] is value for key, value in original_shapes.items())
    assert paint_colors(surface).background.name() != original_background


@pytest.mark.parametrize('changes', [
    {'font_scale': 125}, {'font_family': 'DejaVu Sans Mono'}, {'time_format': '12h'},
    {'date_format': 'iso'}, {'locale_name': 'de_DE'}, {'start_weekday': 6}, {'show_week_numbers': False},
], ids=['scale', 'font', 'time-format', 'date-format', 'language', 'week-start', 'week-numbers'])
def test_metric_and_date_preferences_recompute_surface_geometry(surface, changes):
    preferences = replace(BASE, **changes)
    with patch.object(surface, 'invalidate_geometry', wraps=surface.invalidate_geometry) as invalidation:
        surface.set_preferences(preferences)
    assert invalidation.call_count >= 1
    assert surface.preferences is preferences
    if type(surface) in (CalendarCanvas, AllDayCanvas):
        assert surface.gutter == calendar_gutter(preferences)
    else:
        assert surface.week_gutter == calendar_gutter(preferences)
    if changes.get('font_scale'):
        if type(surface) is AllDayCanvas:
            assert surface.lane_height == 34
            assert surface.height() >= surface.lane_height
        elif type(surface) is MonthCanvas:
            assert surface.minimumHeight() == 550
        else:
            assert surface.all_day_height == 55


def test_visible_hours_only_relayout_the_timed_surface(surface):
    preferences = replace(BASE, visible_start_hour=5, visible_end_hour=22)
    with patch.object(surface, 'invalidate_geometry', wraps=surface.invalidate_geometry) as invalidation:
        surface.set_preferences(preferences)
    assert bool(invalidation.call_count) is (type(surface) is CalendarCanvas)
    if type(surface) is CalendarCanvas:
        assert (surface.start_hour, surface.end_hour) == (5, 22)


def test_new_event_data_still_invalidates_the_existing_frame(surface):
    surface.set_preferences(replace(BASE, color_scheme='warm'))
    before = surface._data_version
    start = START.replace(hour=21)
    added = Event('added', 'local', 'New event', start, start + timedelta(hours=2))
    events = [*surface.events, added]
    with patch.object(surface, 'invalidate_geometry', wraps=surface.invalidate_geometry) as invalidation:
        if type(surface) is MonthCanvas:
            surface.set_data(surface.start, surface.month, events, surface.calendars.values())
        else:
            surface.set_data(surface.days, events, surface.calendars.values())
    assert invalidation.call_count >= 1
    assert surface._data_version == before + 1
    assert added in surface.events
    if type(surface) is CalendarCanvas:
        assert surface.end_hour == 23
