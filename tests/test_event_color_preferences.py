"""Event display settings persist without changing calendar data or source chips."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, MonthCanvas
from debacal.date_time_picker import DateTimeRangePicker
from debacal.event_style import event_colors
from debacal.import_preview import _ImportMonth
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.search_panel import SearchPanel
from debacal.series_preview import SeriesPreview
from debacal.store import Store
from debacal.theme import scheme_for
from debacal.year_presence import paint_day_presence


START = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))
SOURCE = Calendar('local', 'Local', '#192a3b', server_color='#acbed0')
EVENT = Event('one', SOURCE.id, 'Example', START, START + timedelta(hours=1))


@pytest.mark.parametrize('values', [
    {'event_color_mode': 'calendar', 'event_color_brightness': -50, 'event_color_intensity': 300},
    {'event_color_brightness': 20, 'event_color_intensity': 0},
])
def test_event_color_options_round_trip_without_changing_calendar_or_event_data(tmp_path, values):
    path = tmp_path / 'preferences.sqlite3'
    preferences = Preferences(**values)
    with Store(path) as store:
        store.save_calendar(SOURCE)
        store.apply('Create example', [EVENT])
        revision = store.events_revision()
        preferences.save(store)
        assert store.events_revision() == revision
    with Store(path) as store:
        assert Preferences.load(store) == preferences
        assert store.get_calendar(SOURCE.id) == SOURCE
        assert store.get_event(EVENT.id) == EVENT


@pytest.mark.parametrize('field,invalid', [
    ('event_color_mode', None), ('event_color_mode', []), ('event_color_mode', True),
    ('event_color_mode', 'source'), ('event_color_brightness', -51), ('event_color_brightness', 51),
    ('event_color_brightness', True), ('event_color_brightness', '0'),
    ('event_color_intensity', -1), ('event_color_intensity', 301),
    ('event_color_intensity', False), ('event_color_intensity', 100.0),
])
def test_invalid_color_settings_fall_back_per_field(field, invalid):
    assert Preferences.from_dict({field: invalid, 'date_format': 'iso'}) == Preferences(date_format='iso')
    with pytest.raises(ValueError):
        Preferences(**{field: invalid})


def test_older_settings_and_positional_construction_keep_the_existing_event_palette():
    for preferences in (Preferences.from_dict({'date_format': 'iso'}), Preferences(6, 'iso', '24h')):
        assert (preferences.event_color_mode, preferences.event_color_brightness,
                preferences.event_color_intensity) == ('uniform', 0, 100)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        yield app, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def image_with_fill(image, color):
    expected = QColor(color).rgba()
    return sum(image.pixelColor(x, y).rgba() == expected
               for y in range(image.height()) for x in range(image.width()))


CUSTOM_OPTIONS = [
    {'event_color_mode': 'calendar'},
    {'event_color_brightness': -25, 'event_color_intensity': 170},
]


@pytest.mark.parametrize('options', CUSTOM_OPTIONS)
@pytest.mark.parametrize('surface,method,alpha,preview_alpha', [
    (CalendarCanvas, 'paint_event', 52, 34),
    (MonthCanvas, '_paint_month_event', 40, 45),
    (AllDayCanvas, '_paint_bar', 45, 36),
    (_ImportMonth, '_paint_month_event', 52, 52),
])
@pytest.mark.parametrize('preview', [False, True])
def test_actual_calendar_and_import_cards_use_event_display_settings(desktop, options, surface,
                                                                    method, alpha, preview_alpha, preview):
    _, widgets = desktop
    widget = surface()
    widgets.append(widget)
    preferences = Preferences(**options)
    widget.set_preferences(preferences)
    widget.calendars = {SOURCE.id: SOURCE}
    widget.days = [START.date()]
    image = QImage(270, 90, QImage.Format.Format_ARGB32)
    image.fill(QColor(scheme_for(preferences).background))
    painter = QPainter(image)
    try:
        getattr(widget, method)(painter, QRectF(10, 10, 240, 60), EVENT, preview=preview)
    finally:
        painter.end()
    expected = event_colors(SOURCE.color, preferences, alpha=preview_alpha if preview else alpha).fill
    assert image_with_fill(image, expected) > 5000
    assert widget.calendars[SOURCE.id] == SOURCE


def test_literal_outside_month_cards_retain_the_original_calendar_rgb(desktop):
    _, widgets = desktop
    widget = MonthCanvas()
    widgets.append(widget)
    widget.set_preferences(Preferences(event_color_mode='calendar'))
    widget.calendars = {SOURCE.id: SOURCE}
    image = QImage(270, 90, QImage.Format.Format_ARGB32)
    image.fill(QColor('#ffffff'))
    painter = QPainter(image)
    try:
        widget._paint_month_event(painter, QRectF(10, 10, 240, 60), EVENT, muted=True)
    finally:
        painter.end()
    assert image_with_fill(image, SOURCE.color) > 5000


@pytest.mark.parametrize('options', CUSTOM_OPTIONS)
@pytest.mark.parametrize('style', ['search', 'cards', 'grouped', 'timeline'])
def test_existing_search_and_agenda_rows_repaint_without_reindexing(desktop, options, style):
    app, widgets = desktop
    preferences = Preferences(agenda_style='cards' if style == 'search' else style)
    with Store(':memory:') as store, patch('debacal.search_panel.now', return_value=START):
        store.save_calendar(SOURCE)
        store.apply('Create example', [EVENT])
        panel = SearchPanel(store, preferences)
        widgets.append(panel)
        panel.resize(350, 180)
        panel.set_range(START, START + timedelta(days=7))
        if style == 'search':
            panel.populate('example')
        else:
            panel.populate_upcoming(START)
        panel.show()
        app.processEvents()
        item, index = panel.topLevelItem(0), panel._index
        before = panel.viewport().grab().toImage()
        modified = replace(preferences, **options)
        with (patch.object(store, 'query_occurrences', side_effect=AssertionError('Keep occurrences')),
              patch.object(store, 'event_search_index', side_effect=AssertionError('Keep search index')),
              patch.object(store, '_synchronize_query_index', side_effect=AssertionError('Keep prepared index'))):
            assert panel.update_context(store.list_calendars(), modified)
            app.processEvents()
            after = panel.viewport().grab().toImage()
        assert after != before
        assert image_with_fill(after, event_colors(SOURCE.color, modified).fill) > 1000
        assert panel.topLevelItem(0) is item and panel._index is index
        assert store.get_calendar(SOURCE.id) == SOURCE


@pytest.mark.parametrize('options', CUSTOM_OPTIONS)
def test_scheduling_and_datetime_picker_previews_use_the_same_card_fill(desktop, options):
    app, widgets = desktop
    preferences = Preferences(**options)
    preview = SeriesPreview()
    widgets.append(preview)
    preview.set_events([EVENT], preferences, [SOURCE])
    preview.resize(800, 480)
    preview.show()
    picker = DateTimeRangePicker(START, EVENT.end, preferences=preferences, calendar_color=SOURCE.color)
    widgets.append(picker)
    picker.show()
    app.processEvents()
    expected = event_colors(SOURCE.color, preferences).fill
    assert image_with_fill(preview.grid.grab().toImage(), expected) > 500
    assert image_with_fill(picker.strip.grab().toImage(), expected) > 500


@pytest.mark.parametrize('appearance', ['light', 'dark'])
def test_year_tint_defaults_preserve_existing_transparent_compositing(desktop, appearance):
    preferences = Preferences(appearance_mode=appearance, year_view_style='tints')
    palette = scheme_for(preferences)
    image = QImage(80, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor(palette.panel))
    painter = QPainter(image)
    try:
        paint_day_presence(painter, QRectF(0, 0, 80, 40), [SOURCE.color], preferences, palette)
    finally:
        painter.end()
    expected = QImage(1, 1, QImage.Format.Format_ARGB32)
    expected.fill(QColor(palette.panel))
    painter = QPainter(expected)
    tint = QColor(SOURCE.color)
    tint.setAlpha(58 if appearance == 'dark' else 43)
    painter.fillRect(expected.rect(), tint)
    painter.end()
    assert image.pixelColor(10, 10) == expected.pixelColor(0, 0)


@pytest.mark.parametrize('options', CUSTOM_OPTIONS)
def test_year_tints_follow_event_options_and_preserve_a_readable_date_label(desktop, options):
    preferences = Preferences(year_view_style='tints', **options)
    palette = scheme_for(preferences)
    image = QImage(80, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor(palette.panel))
    painter = QPainter(image)
    try:
        number = paint_day_presence(painter, QRectF(0, 0, 80, 40),
                                    [SOURCE.color, '#ffffff'], preferences, palette)
    finally:
        painter.end()
    assert image.pixelColor(10, 10) == QColor(event_colors(SOURCE.color, preferences, alpha=43).fill)
    assert image.pixelColor(number.center().toPoint()) == QColor(palette.panel)
