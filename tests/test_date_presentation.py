"""Date emphasis and adjacent-month content share readable theme-aware styling."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets
from debacal.calendar_widgets import DayHeader, MonthCanvas, paint_colors
from debacal.event_style import contrast_ratio, event_colors
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.year_view import YearView


ZONE = ZoneInfo('Europe/Berlin')
TODAY = date(2026, 11, 7)
CURRENT = datetime(2026, 11, 7, 12, tzinfo=ZONE)
GRID = date(2026, 10, 26)
CALENDAR = Calendar('source', 'Synthetic calendar', '#cf573e')


@pytest.fixture(params=['light', 'dark'])
def desktop(request):
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    preferences = Preferences(appearance_mode=request.param, locale_name='en_GB', time_format='24h')
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.now', return_value=CURRENT), \
         patch('debacal.year_view.now', return_value=CURRENT), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(kind, events=()):
            widget = kind()
            owned.append(widget)
            widget.set_preferences(preferences)
            widget.resize(1040, 700)
            if kind is MonthCanvas:
                widget.set_data(GRID, 11, list(events), [CALENDAR])
            elif kind is YearView:
                widget.set_data(2026, list(events), [CALENDAR])
                widget.resize(1040, widget.heightForWidth(1040))
            else:
                widget.days = [date(2026, 11, 2) + timedelta(days=offset) for offset in range(7)]
            widget.show()
            app.processEvents()
            return widget
        yield app, preferences, make
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def appointment(identity, day, *, all_day=False, days=1, **fields):
    start = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=0 if all_day else 9)
    end = start + (timedelta(days=days) if all_day else timedelta(hours=1))
    return Event(identity, CALENDAR.id, 'Synthetic appointment', start, end, all_day=all_day, **fields)


def month_cell(widget, day):
    row, column = divmod((day - widget.start).days, 7)
    width = (widget.width() - widget.week_gutter) / 7
    height = (widget.height() - widget.header_height) / 6
    return widget.week_gutter + column * width, widget.header_height + row * height, width, height


def fill_pixel(image, rect):
    return image.pixelColor(round(rect.right() - 9), round(rect.bottom() - 3))


def test_today_labels_use_weekend_ink_and_single_day_has_no_selection_fill(desktop):
    _, _, make = desktop
    for kind in (DayHeader, MonthCanvas):
        widget = make(kind)
        if kind is DayHeader:
            widget.days = [TODAY]
        before = widget.grab().toImage()
        widget.set_selected_day(TODAY)
        with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as rendered:
            image = widget.grab().toImage()
        label = next(call for call in rendered.call_args_list if call.args[2] == 'TODAY')
        assert label.args[3] == paint_colors(widget).weekend
        assert label.kwargs['align'] & Qt.AlignmentFlag.AlignRight
        assert label.args[4] < calendar_widgets.WEEKDAY_SIZE
        if kind is DayHeader:
            assert image == before
            assert image.pixelColor(widget.gutter + 2, 35) == paint_colors(widget).background
        else:
            assert image != before
            left, top, width, height = month_cell(widget, TODAY)
            assert image.pixelColor(round(left + 3), round(top + height / 2)) == paint_colors(widget).background


def test_week_and_month_selection_tints_are_subtle_but_visible(desktop):
    _, _, make = desktop
    for kind in (DayHeader, MonthCanvas):
        widget = make(kind)
        colors = paint_colors(widget)
        assert 1.01 < contrast_ratio(colors.date_selection, colors.background) < 1.25
        widget.set_selected_day(TODAY)
        with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as rendered:
            widget.grab()
        assert any(call.args[2] == 'TODAY' and call.args[3] == colors.weekend for call in rendered.call_args_list)


def test_adjacent_month_days_keep_canvas_background_but_mute_date_numbers(desktop):
    _, _, make = desktop
    widget = make(MonthCanvas)
    with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as rendered:
        image = widget.grab().toImage()
    colors = paint_colors(widget)
    for day in (date(2026, 10, 27), date(2026, 10, 31), date(2026, 12, 2)):
        left, top, width, height = month_cell(widget, day)
        assert image.pixelColor(round(left + width / 2), round(top + height / 2)) == colors.background
        label = next(call for call in rendered.call_args_list if call.args[2] == str(day.day)
                     and left < call.args[1].center().x() < left + width
                     and top < call.args[1].center().y() < top + height)
        assert label.args[3] == colors.muted


def test_adjacent_event_cards_are_muted_readable_and_keep_strong_selection(desktop):
    _, _, make = desktop
    outside = appointment('outside', date(2026, 10, 27))
    inside = appointment('inside', date(2026, 11, 3))
    widget = make(MonthCanvas, [outside, inside])
    with patch('debacal.calendar_widgets.text', wraps=calendar_widgets.text) as rendered:
        image = widget.grab().toImage()
    rects = {event.id: rect for rect, event in widget.hits}
    faded = fill_pixel(image, rects[outside.id])
    normal = fill_pixel(image, rects[inside.id])
    assert faded != normal
    outside_label = next(call for call in rendered.call_args_list if 'Synthetic appointment' in call.args[2]
                         and rects[outside.id].contains(call.args[1].center()))
    assert contrast_ratio(outside_label.args[3], faded) >= 4.5
    widget.set_selection({outside.id})
    image = widget.grab().toImage()
    assert fill_pixel(image, rects[outside.id]) == normal
    assert widget.hit(rects[outside.id].center())[1].id == outside.id
    widget.set_selection(set())
    widget.set_data(GRID, 11, [], [CALENDAR])
    widget.set_preview([outside])
    image = widget.grab().toImage()
    expected = QColor(event_colors(CALENDAR.color, widget, alpha=45).fill)
    assert fill_pixel(image, rects[outside.id]) == expected
    assert not widget.hits


@pytest.mark.parametrize(('start', 'outside_day', 'inside_day', 'bar_count'), [
    (date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), 2),
    (date(2026, 11, 30), date(2026, 12, 2), date(2026, 11, 30), 1),
])
def test_cross_month_all_day_bar_fades_only_outside_portions_and_has_one_confirmation_per_bar(
        desktop, start, outside_day, inside_day, bar_count):
    _, _, make = desktop
    source = appointment('span', start, all_day=True, days=4, candidate=True)
    widget = make(MonthCanvas, [source])
    widget._hovered_event_id = source.id
    image = widget.grab().toImage()
    bars = [rect for rect, event in widget.hits if event.id == source.id]
    assert len(bars) == bar_count
    assert len(widget.confirm_hits) == len(bars)
    first = bars[0]
    outside_left, _, width, _ = month_cell(widget, outside_day)
    inside_left, _, _, _ = month_cell(widget, inside_day)
    outside = image.pixelColor(round(outside_left + width / 2), round(first.bottom() - 3))
    inside = image.pixelColor(round(inside_left + width / 2), round(first.bottom() - 3))
    assert outside != inside
    assert widget.hit(QPoint(round(outside_left + width / 2), round(first.center().y())))[1].id == source.id
    assert widget.hit(QPoint(round(inside_left + width / 2), round(first.center().y())))[1].id == source.id
    for control, _identity in widget.confirm_hits:
        assert image.pixelColor(round(control.left() + control.width() * .3),
                                round(control.top() + control.height() * .2)) == paint_colors(widget).panel


@pytest.mark.parametrize('style', ['tracks', 'tints'])
def test_year_today_is_bold_underlined_and_retains_independent_selection_border(desktop, style):
    _, _, make = desktop
    widget = make(YearView)
    widget.set_preferences(replace(widget.preferences, year_view_style=style))
    target = widget.day_rect(TODAY)
    with patch.object(widget, '_text', wraps=widget._text) as rendered:
        plain = widget.grab().toImage()
    number = next(call for call in rendered.call_args_list if call.args[2] == str(TODAY.day)
                  and target.contains(call.args[1].center()))
    assert number.args[5] is True and number.kwargs['underline'] is True
    ordinary = widget._number_font(number.args[1], number.args[4], True)
    assert not ordinary.underline()  # Today's decoration must not mutate cached fonts.
    widget.set_selected_day(TODAY)
    selected = widget.grab().toImage()
    assert selected != plain
    widget.set_selected_day(None)
    assert widget.grab().toImage() == plain
