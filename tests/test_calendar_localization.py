"""Painter calendars follow display locale, font size and preferred hours."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QRectF, QCoreApplication, QEvent
from PySide6.QtGui import QPainter, QFont, QFontMetricsF
from PySide6.QtWidgets import QApplication, QWidget

from debacal.calendar_widgets import CalendarCanvas, DayHeader, MonthCanvas, AllDayCanvas, text
from debacal.models import Event, Calendar
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def widgets():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(kind, preferences=None):
            widget = kind()
            owned.append(widget)
            widget.resize(1050, 700)
            if preferences is not None:
                widget.set_preferences(preferences)
            return widget
        yield app, create
        for widget in owned:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_visible_hour_preferences_are_baseline_and_expand_for_actual_events(widgets):
    _, create = widgets
    canvas = create(CalendarCanvas, Preferences(visible_start_hour=6, visible_end_hour=20))
    canvas.fixed_all_day = True
    canvas.set_data([START.date()], [], [])
    canvas.fit_to_viewport(600)
    assert (canvas.start_hour, canvas.end_hour) == (6, 20)
    early = Event('early', 'local', 'Early', START.replace(hour=4), START.replace(hour=5))
    late = Event('late', 'local', 'Late', START.replace(hour=22), START.replace(hour=23))
    canvas.set_data([START.date()], [early, late], [])
    assert (canvas.start_hour, canvas.end_hour) == (4, 23)
    canvas.fit_to_viewport(600, full_day=True)
    assert (canvas.start_hour, canvas.end_hour) == (0, 24)


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_painter_text_uses_requested_font_scale(widgets, scale):
    _, create = widgets
    class Surface(QWidget):
        def set_preferences(self, preferences):
            self.preferences = preferences
        def paintEvent(self, _event):
            painter = QPainter(self)
            text(painter, QRectF(0, 0, 200, 50), 'Synthetic', size=10)
            self.actual_size = painter.font().pointSizeF()
            painter.end()
    surface = create(Surface, Preferences(font_scale=scale))
    assert not surface.grab().isNull()
    assert surface.actual_size == pytest.approx(10 * scale / 100)


def test_large_fonts_keep_week_gutters_aligned_and_all_day_rows_usable(widgets):
    _, create = widgets
    preferences = Preferences(font_scale=140, locale_name='en_GB', time_format='12h')
    header = create(DayHeader, preferences)
    canvas = create(CalendarCanvas, preferences)
    strip = create(AllDayCanvas, preferences)
    assert header.gutter == canvas.gutter == strip.gutter
    assert header.gutter > 62 and header.height() > 84 and strip.lane_height > 27
    start = START.replace(hour=0)
    event = Event('trip', 'local', 'Trip', start, start + timedelta(days=3), all_day=True)
    strip.set_data([start.date() + timedelta(days=day) for day in range(7)], [event], [])
    rect, _, _ = strip.rects()[0]
    font = QFont('Sans Serif')
    font.setPointSizeF(8 * 1.4)
    assert rect.height() >= QFontMetricsF(font).height()
    assert rect.bottom() <= strip.height()
    assert strip.day_at(rect.center()) in (start.date(), start.date() + timedelta(days=1), start.date() + timedelta(days=2))


def test_scaled_month_rows_still_hit_the_actual_event_date(widgets):
    _, create = widgets
    preferences = Preferences(font_scale=140, locale_name='en_GB')
    month = create(MonthCanvas, preferences)
    source = Event('meeting', 'local', 'Meeting', START, START + timedelta(hours=1))
    month.set_data(date(2026, 11, 2), 11, [source], [Calendar('local', 'Local', '#123456')])
    assert not month.grab().isNull()
    rect = next(rect for rect, event in month.hits if event.id == source.id)
    assert month.day_at(rect.center()) == source.start.date()
    assert rect.height() > 21 and month.header_height > 28


def test_day_header_uses_explicit_english_weekday_names(widgets):
    _, create = widgets
    header = create(DayHeader, Preferences(locale_name='en_GB'))
    header.days = [START.date()]
    with patch('debacal.calendar_widgets.text', wraps=text) as rendered:
        assert not header.grab().isNull()
    values = [call.args[2] for call in rendered.call_args_list]
    assert 'WED' in values
    assert 'MI.' not in values


@pytest.mark.parametrize(('locale', 'name'), [('en_GB', 'March'), ('de_DE', 'März')])
def test_series_preview_month_uses_explicit_locale(widgets, locale, name):
    _, create = widgets
    preview = create(SeriesPreview)
    preview.set_events([], Preferences(locale_name=locale))
    preview.show_month(date(2027, 3, 1))
    assert name in preview.month_label.text()


def test_month_creation_tooltip_uses_preferred_time_format(widgets):
    app, create = widgets
    preferences = Preferences(locale_name='en_GB', time_format='12h')
    month = create(MonthCanvas, preferences)
    month.set_data(date(2026, 11, 2), 11, [], [])
    month.show()
    app.processEvents()
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    QTest.mouseMove(month, QPoint(50, month.header_height + 60))
    assert preferences.format_time(time(9)) in month.toolTip()
    assert '09:00' not in month.toolTip()
