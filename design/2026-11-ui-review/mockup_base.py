"""Synthetic, process-local design experiments; never used by the application."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets as cw
from debacal.__main__ import configure_application, seed_demo
from debacal.models import Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
NOW = datetime(2026, 11, 6, 13, 15, tzinfo=ZONE)
ORIGINAL_TEXT = cw.text
ORIGINAL_TIMED = cw.CalendarCanvas.paint_event
ORIGINAL_MONTH = cw.MonthCanvas._paint_month_event
ORIGINAL_ALL_DAY = cw.AllDayCanvas._paint_bar
ORIGINAL_MUTED = QColor(cw.MUTED)
ORIGINAL_INK = QColor(cw.INK)


def contrast(a, b):
    def luminance(value):
        value = QColor(value)
        channels = [value.redF(), value.greenF(), value.blueF()]
        channels = [item / 12.92 if item <= .04045 else ((item + .055) / 1.055) ** 2.4 for item in channels]
        return sum(left * right for left, right in zip(channels, (.2126, .7152, .0722)))
    first, second = sorted((luminance(a), luminance(b)))
    return (second + .05) / (first + .05)


def contrast_color(color, background='#ffffff', minimum=3):
    result = QColor(color)
    for _index in range(24):
        if contrast(result, background) >= minimum:
            return result
        result = result.darker(112)
    return QColor('#24312a')


class _EventPainter:
    """Adjust only event outlines/bars; original source-color fills stay intact."""
    def __init__(self, painter, source_colors, background):
        self._painter, self._colors, self._background = painter, source_colors, background

    def __getattr__(self, name):
        return getattr(self._painter, name)

    def setPen(self, pen):
        if isinstance(pen, QPen) and pen.style() != Qt.PenStyle.NoPen and pen.color().rgb() in self._colors:
            pen = QPen(pen)
            pen.setColor(contrast_color(pen.color(), self._background))
        return self._painter.setPen(pen)

    def fillRect(self, *args):
        if len(args) == 2 and isinstance(args[1], QColor) and args[1].rgb() in self._colors and args[0].width() <= 5:
            args = (args[0], contrast_color(args[1], self._background))
        return self._painter.fillRect(*args)


def source_colors(widget):
    return {QColor(calendar.color).darker(factor).rgb()
            for calendar in getattr(widget, 'calendars', {}).values()
            for factor in (100, 120, 135, 140, 145)}


def install_paint_palette(*, background='#ffffff', muted='#59645c', ink='#24312a',
                          line='#dbe0db', accent='#416c51', text_scale=1.04,
                          neutral_event_text=True):
    """Patch this disposable rendering process, never the source files."""
    cw.BG, cw.MUTED, cw.INK, cw.LINE, cw.GREEN = map(QColor, (background, muted, ink, line, accent))

    def text(painter, rect, value, color=ORIGINAL_INK, size=10, bold=False, align=None):
        color = QColor(color)
        if color == ORIGINAL_MUTED:
            color = QColor(muted)
        elif color == ORIGINAL_INK:
            color = QColor(ink)
        if neutral_event_text and color.rgb() in source_colors(painter.device()):
            color = QColor(ink)
        return ORIGINAL_TEXT(painter, rect, value, color, max(8.6, size * text_scale), bold, align)

    cw.text = text

    def timed(widget, painter, *args, **kwargs):
        return ORIGINAL_TIMED(widget, _EventPainter(painter, source_colors(widget), background), *args, **kwargs)

    def month(widget, painter, *args, **kwargs):
        return ORIGINAL_MONTH(widget, _EventPainter(painter, source_colors(widget), background), *args, **kwargs)

    def all_day(widget, painter, *args, **kwargs):
        return ORIGINAL_ALL_DAY(widget, _EventPainter(painter, source_colors(widget), background), *args, **kwargs)

    cw.CalendarCanvas.paint_event = timed
    cw.MonthCanvas._paint_month_event = month
    cw.AllDayCanvas._paint_bar = all_day


def make_sample_window():
    """One identical week for every direction, with no live accounts or disk DB."""
    app = QApplication.instance() or QApplication([])
    configure_application(app)
    patches = [patch(module + '.now', return_value=NOW) for module in (
        'debacal.__main__', 'debacal.ui', 'debacal.calendar_widgets',
        'debacal.search_panel', 'debacal.year_view', 'debacal.import_preview',
        'debacal.import_preparation')]
    for item in patches:
        item.start()
    store = Store(':memory:')
    seed_demo(store)
    start = NOW.replace(day=5, hour=0, minute=0)
    store.apply('Synthetic all-day example', [Event('autumn-break', 'local', 'Autumn break', start,
                                                   start + timedelta(days=3), all_day=True)])
    preferences = Preferences(start_weekday=0, date_format='european', time_format='24h',
                              locale_name='en_GB', visible_start_hour=8, visible_end_hour=20)
    preferences.save(store)
    window = MainWindow(store, enable_sync=False, profile_label='Testing')
    window.resize(1440, 960)
    window.go_date(NOW.date())
    window.select_event('0')
    window.show()
    for _index in range(6):
        app.processEvents()
    return app, store, window, patches


def section(window, name):
    for index in range(window.toolbar.layout().count()):
        item = window.toolbar.layout().itemAt(index)
        widget = item.widget()
        if widget is not None and widget.accessibleName() == name:
            return widget
    raise ValueError(name)


def settle(app):
    for _index in range(8):
        app.processEvents()
