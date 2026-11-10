"""Switching calendar views preserves the date heading and shared day columns."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet
from debacal.fonts import ui_font
from debacal import calendar_widgets
from debacal.models import Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def settle(app):
    for _ in range(10):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
            patch('debacal.calendar_widgets.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        Preferences(start_weekday=0, date_format='european', time_format='24h', locale_name='en_GB').save(store)
        window = MainWindow(store, enable_sync=False)
        window.resize(1440, 960)
        window.go_date(START.date())
        window.show()
        settle(app)
        try:
            yield window, store, app
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert not errors


def origin(widget, window):
    return widget.mapTo(window, QPoint())


def boundaries(widget, gutter, window):
    left = origin(widget, window).x()
    return [left + gutter + index * (widget.width() - gutter) / 7 for index in range(8)]


def apply_preferences(window, app, **values):
    window.preferences = replace(window.preferences, **values)
    window.setStyleSheet(stylesheet(window.preferences.font_scale, window.preferences.color_scheme))
    window.refresh()
    settle(app)


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('week_numbers', [False, True])
@pytest.mark.parametrize('size', [(1440, 960), (1030, 690)])
def test_month_and_week_share_every_vertical_boundary(workspace, scale, week_numbers, size):
    window, _, app = workspace
    window.resize(*size)
    apply_preferences(window, app, font_scale=scale, show_week_numbers=week_numbers)
    window.set_view('week')
    settle(app)
    expected = boundaries(window.header, window.header.gutter, window)
    assert boundaries(window.canvas, window.canvas.gutter, window) == pytest.approx(expected)
    assert boundaries(window.all_day_canvas, window.all_day_canvas.gutter, window) == pytest.approx(expected)
    heading = origin(window.heading, window)
    today = origin(window.today_button, window)
    window.set_view('month')
    settle(app)
    assert boundaries(window.month, window.month.week_gutter, window) == pytest.approx(expected)
    assert origin(window.heading, window) == heading
    assert origin(window.today_button, window) == today
    assert heading.x() == pytest.approx(expected[0])
    window.set_view('week')
    settle(app)
    assert boundaries(window.header, window.header.gutter, window) == pytest.approx(expected)


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('locale', ['en_GB', 'de_DE'])
def test_month_and_week_paint_identical_weekday_labels_at_identical_positions(workspace, scale, locale):
    window, _, app = workspace
    apply_preferences(window, app, font_scale=scale, locale_name=locale)
    expected_names = {window.preferences.weekday_name(day).upper() for day in range(7)}
    records = []
    for view in ('week', 'month'):
        window.set_view(view)
        settle(app)
        target = window.header if view == 'week' else window.month
        offset = origin(target, window)
        original = calendar_widgets.text
        with patch('debacal.calendar_widgets.text', wraps=original) as draw:
            target.grab()
        labels = []
        for call in draw.call_args_list:
            painter, rect, value, *_ = call.args
            if value in expected_names:
                # Compare actual drawing arguments rather than restating the
                # shared layout helper's formulas.
                labels.append((value, QRectF(rect).translated(offset), call.args[4], call.args[5]))
        assert len(labels) == 7
        records.append(labels)
    for (week_name, week_rect, week_size, week_bold), (month_name, month_rect, month_size, month_bold) in zip(*records):
        assert month_name == week_name
        assert month_size == week_size and month_bold == week_bold
        assert month_rect.getRect() == pytest.approx(week_rect.getRect())


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('style', ['underlined', 'filled'])
def test_headline_previous_and_today_keep_their_positions_in_all_views(workspace, scale, style):
    window, _, app = workspace
    window.resize(1030, 690)
    apply_preferences(window, app, font_scale=scale, control_style=style)
    placements = []
    for view in ('week', 'month', 'year', 'day', 'week'):
        window.set_view(view)
        settle(app)
        placements.append(tuple(origin(widget, window) for widget in
                                (window.heading, window.previous_button, window.today_button)))
    assert all(value == placements[0] for value in placements)


@pytest.mark.parametrize('crowded_all_day', [False, True])
def test_month_columns_keep_week_scrollbar_reservation(workspace, crowded_all_day):
    window, store, app = workspace
    window.resize(1030, 690)
    if crowded_all_day:
        day = START.replace(hour=0)
        store.apply('Crowded all-day lanes', [Event(f'all-day-{index}', 'local', f'All-day {index}', day,
                                                    day + timedelta(days=1), all_day=True)
                                             for index in range(12)])
        window.refresh()
    else:
        window.full_day = True
        window.fit_calendar()
    settle(app)
    area = window.all_day_scroll if crowded_all_day else window.scroll
    assert area.verticalScrollBar().isVisible()
    expected = boundaries(window.header, window.header.gutter, window)
    assert boundaries(window.canvas, window.canvas.gutter, window) == pytest.approx(expected)
    assert boundaries(window.all_day_canvas, window.all_day_canvas.gutter, window) == pytest.approx(expected)
    window.set_view('month')
    settle(app)
    assert boundaries(window.month, window.month.week_gutter, window) == pytest.approx(expected)
    window.set_view('week')
    settle(app)
    assert boundaries(window.header, window.header.gutter, window) == pytest.approx(expected)


@pytest.mark.parametrize('scale', [80, 140])
@pytest.mark.parametrize('full_day', [False, True])
def test_resizing_month_first_updates_the_next_week_columns(workspace, scale, full_day):
    window, _, app = workspace
    apply_preferences(window, app, font_scale=scale)
    window.full_day = full_day
    window.set_view('month')
    for size in ((1030, 690), (1440, 960), (1200, 850)):
        window.resize(*size)
        settle(app)
        expected = boundaries(window.month, window.month.week_gutter, window)
        headline = origin(window.heading, window)
        window.set_view('week')
        settle(app)
        assert origin(window.heading, window) == headline
        for widget in (window.header, window.canvas, window.all_day_canvas):
            assert boundaries(widget, widget.gutter, window) == pytest.approx(expected)
        window.set_view('month')
        settle(app)
        assert boundaries(window.month, window.month.week_gutter, window) == pytest.approx(expected)


@pytest.mark.parametrize('week_numbers', [False, True])
def test_month_gutter_visibility_does_not_change_days_or_steal_clicks(workspace, week_numbers):
    window, _, app = workspace
    apply_preferences(window, app, show_week_numbers=week_numbers)
    window.set_view('month')
    settle(app)
    month = window.month
    gutter = month.week_gutter
    assert gutter > 0 and gutter == window.canvas.gutter
    point = QPointF(gutter / 2, month.header_height + 12)
    assert month.day_at(point) is None
    assert bool(month.week_rect(0)) is week_numbers
    assert (month.week_at(point) is not None) is week_numbers
    width = (month.width() - gutter) / 7
    for index in range(7):
        center = QPointF(gutter + (index + .5) * width, month.header_height + 12)
        assert month.day_at(center) == month.start + timedelta(days=index)


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('time_format', ['24h', '12h'])
def test_all_day_caption_is_smaller_and_right_aligned_with_readable_timestamps(workspace, scale, time_format):
    window, _, app = workspace
    apply_preferences(window, app, font_scale=scale, time_format=time_format)
    values = []
    for target in (window.all_day_canvas, window.canvas):
        offset = origin(target, window)
        with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as draw:
            target.grab()
        values.append([(QRectF(call.args[1]).translated(offset), call.args[2], call.args[4], call.kwargs.get('align'))
                       for call in draw.call_args_list])
    all_day = next(row for row in values[0] if row[1].casefold() == 'all day')
    stamps = {window.preferences.format_time(time(hour)) for hour in range(1, 24)}
    times = [row for row in values[1] if row[1] in stamps]
    assert times and all_day[2] < times[0][2]
    assert all_day[3] & Qt.AlignmentFlag.AlignRight
    for rect, value, size, align in times:
        assert rect.right() == pytest.approx(all_day[0].right())
        assert align & Qt.AlignmentFlag.AlignRight
        font = ui_font(window.preferences, size)
        assert QFontMetricsF(font).horizontalAdvance(value) <= rect.width()


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_week_caption_and_number_align_with_day_labels_and_numbers(workspace, scale):
    window, _, app = workspace
    apply_preferences(window, app, font_scale=scale)
    header = window.header
    week = str(window.preferences.week_number(header.days[0]))
    with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as draw:
        header.grab()
    calls = draw.call_args_list
    week_call = next(call for call in calls if call.args[2] == week and call.args[1].right() < header.gutter)
    day_call = next(call for call in calls if call.args[2] == str(header.days[0].day)
                    and call.args[1].left() > header.gutter)
    assert week_call.args[4] == 13 < day_call.args[4] == 17
    def baseline(call):
        font = ui_font(window.preferences, call.args[4], bold=True)
        metrics = QFontMetricsF(font)
        return call.args[1].center().y() + (metrics.ascent() - metrics.descent()) / 2
    assert baseline(day_call) == pytest.approx(baseline(week_call))
    assert day_call.args[1].height() == week_call.args[1].height()
    caption = next(call for call in calls if call.args[2] == 'WEEK')
    weekday = next(call for call in calls if call.args[2] == window.preferences.weekday_name(header.days[0]).upper())
    assert caption.args[4:6] == weekday.args[4:6]
    assert caption.args[1].top() == weekday.args[1].top()
    assert caption.args[1].height() == weekday.args[1].height()


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_week_axis_caption_has_identical_geometry_and_style_in_month_and_week(workspace, scale):
    window, _, app = workspace
    apply_preferences(window, app, font_scale=scale)
    captions = []
    for view in ('week', 'month', 'week'):
        window.set_view(view)
        settle(app)
        target = window.header if view == 'week' else window.month
        with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as draw:
            target.grab()
        call = next(call for call in draw.call_args_list if call.args[2] == 'WEEK')
        captions.append((QRectF(call.args[1]).translated(origin(target, window)), call.args[3].name(),
                         call.args[4], call.kwargs.get('align')))
    assert captions[0] == captions[1] == captions[2]
