"""New-event gestures follow the creation target; existing previews keep theirs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas
from debacal.models import Calendar, Event
from debacal.event_style import contrast_ratio, event_colors

ZONE = ZoneInfo('Europe/Berlin')
FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=index) for index in range(7)]
RED = Calendar('red', 'Synthetic red', '#b12f45')
BLUE = Calendar('blue', 'Synthetic blue', '#315ab7')
YELLOW = Calendar('yellow', 'Synthetic yellow', '#ffff00')


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch.object(calendar_widgets, 'local_zone', return_value=ZONE), \
         patch.object(calendar_widgets, 'now', return_value=datetime(2026, 11, 6, 12, tzinfo=ZONE)), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def make(kind='timed', events=(), days=DAYS):
            widget = {'timed': CalendarCanvas, 'month': MonthCanvas,
                      'all-day': AllDayCanvas, 'header': DayHeader}[kind]()
            widgets.append(widget)
            if kind == 'header':
                widget.days = list(days)
            elif kind == 'month':
                widget.set_data(FIRST, FIRST.month, list(events), [RED, BLUE])
            else:
                widget.set_data(list(days), list(events), [RED, BLUE])
            if kind == 'timed':
                widget.fixed_all_day = True
                widget.fit_to_viewport(600)
            widget.resize(1042, 700 if kind == 'month' else 600)
            widget.show()
            app.processEvents()
            QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
            return widget
        yield make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def timed_point(widget, day=0, minute=9 * 60 + 15):
    width = (widget.width() - widget.gutter) / len(widget.days)
    return QPoint(round(widget.gutter + (day + .5) * width),
                  round(widget.time_top + (minute / 60 - widget.start_hour) * widget.hour_height))


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day', 'header'])
def test_creation_target_api_accepts_writable_calendars_and_has_a_safe_fallback(surfaces, kind):
    widget = surfaces(kind)
    for target in (RED, BLUE, replace(RED, color='#976dbb')):
        widget.set_creation_calendar(target)
        assert widget.creation_color() == QColor(target.color)
    for target in (None, replace(RED, read_only=True), replace(BLUE, kind='ics'), replace(RED, color='invalid')):
        widget.set_creation_calendar(target)
        assert widget.creation_color() == calendar_widgets.GREEN


def test_target_switch_before_and_during_creation_drag_updates_ghost_and_time_label(surfaces):
    widget = surfaces(days=[FIRST])
    start, finish = timed_point(widget), timed_point(widget, minute=10 * 60 + 45)
    widget.set_creation_calendar(RED)
    widget.set_creation_calendar(BLUE)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, finish)
    assert widget.drag[0] == 'create' and widget.ghost is not None
    sample = QPoint(widget.width() - 25, round(widget.time_top + (9.75 - widget.start_hour) * widget.hour_height))
    for target in (BLUE, RED, YELLOW):
        widget.set_creation_calendar(target)
        with patch.object(calendar_widgets, 'text', wraps=calendar_widgets.text) as labels:
            image = widget.grab().toImage()
        pixel = image.pixelColor(sample)
        if target == RED:
            assert pixel.red() > pixel.blue()
        elif target == BLUE:
            assert pixel.blue() > pixel.red()
        else:
            assert pixel.red() > pixel.blue() and pixel.green() > pixel.blue()
        clock = widget.preferences.format_time(datetime(2026, 11, 2, 9, 15).time())
        calls = [call for call in labels.call_args_list if call.args[2] == clock]
        assert len(calls) == 1
        assert contrast_ratio(calls[0].args[3], event_colors(target.color, widget, alpha=34).fill) >= 4.5
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)


def test_hover_time_is_readable_and_independent_of_the_calendar_color(surfaces):
    widget = surfaces(days=[FIRST])
    point = timed_point(widget)
    QTest.mouseMove(widget, point)
    initial = widget.grab().toImage()
    for target in (RED, BLUE, replace(RED, color='#ffff00')):
        widget.set_creation_calendar(target)
        with patch.object(calendar_widgets, 'text', wraps=calendar_widgets.text) as labels:
            widget.grab()
        clock = widget.preferences.format_time(datetime(2026, 11, 2, 9, 15).time())
        assert next(call.args[3] for call in labels.call_args_list if call.args[2] == clock) == calendar_widgets.paint_colors(widget).muted
        assert widget.grab().toImage() == initial
    header = surfaces('header')
    original = header.grab().toImage()
    header.set_creation_calendar(RED)
    assert header.grab().toImage() == original


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_existing_and_paste_previews_keep_source_colors_when_creation_target_changes(surfaces, kind):
    begin = datetime(2026, 11, 2, 0 if kind == 'all-day' else 9, tzinfo=ZONE)
    source = Event('source', RED.id, 'Synthetic existing event', begin,
                   begin + (timedelta(days=2) if kind == 'all-day' else timedelta(hours=1)),
                   all_day=kind == 'all-day')
    widget = surfaces(kind, [source])
    widget.set_preview([replace(source, id='preview', start=source.start + timedelta(days=3), end=source.end + timedelta(days=3))])
    widget.set_creation_calendar(RED)
    before = widget.grab().toImage()
    widget.set_creation_calendar(BLUE)
    assert widget.grab().toImage() == before


def test_existing_event_move_without_union_preview_uses_its_own_calendar(surfaces):
    begin = datetime(2026, 11, 2, 9, tzinfo=ZONE)
    source = Event('source', RED.id, 'Synthetic existing event', begin, begin + timedelta(hours=1))
    widget = surfaces(events=[source])
    widget.set_creation_calendar(BLUE)
    rect = widget.hits[0][0]
    start = rect.center().toPoint()
    finish = start + QPoint(round((widget.width() - widget.gutter) / len(DAYS)), 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, finish)
    assert widget.drag[0] == 'move' and widget.ghost is not None and widget.preview_events == []
    day, minute, end_minute = widget.ghost
    width = (widget.width() - widget.gutter) / len(DAYS)
    point = QPoint(round(widget.gutter + (day + .8) * width),
                   round(widget.time_top + ((minute + end_minute) / 120 - widget.start_hour) * widget.hour_height))
    pixel = widget.grab().toImage().pixelColor(point)
    assert pixel.red() > pixel.blue()
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
