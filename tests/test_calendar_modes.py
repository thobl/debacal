"""Selection, adaptive hours, fixed all-day lanes, and pending placements."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QColor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from lebacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas
from lebacal.models import Calendar, Event


ZONE = ZoneInfo('Europe/Berlin')
FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=index) for index in range(7)]
CTRL = Qt.KeyboardModifier.ControlModifier


def sample(identifier='event', *, day=0, hour=9, minute=0, duration=60, all_day=False):
    start = datetime.combine(FIRST + timedelta(days=day), datetime.min.time(), ZONE) + timedelta(hours=hour, minutes=minute)
    return Event(identifier, 'local', identifier, start, start + timedelta(minutes=duration), all_day=all_day)


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    calendars = [Calendar('local', 'Color', '#ed7135')]
    with patch('lebacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('lebacal.calendar_widgets.now', return_value=datetime(2026, 11, 5, 12, 30, tzinfo=ZONE)), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def make(kind, events=()):
            widget = {'timed': CalendarCanvas, 'month': MonthCanvas, 'all-day': AllDayCanvas, 'header': DayHeader}[kind]()
            widgets.append(widget)
            if kind == 'timed':
                widget.fixed_all_day = True
                widget.set_data(DAYS, list(events), calendars)
                widget.fit_to_viewport(600)
            elif kind == 'month':
                widget.set_data(FIRST, 11, list(events), calendars)
            elif kind == 'all-day':
                widget.set_data(DAYS, list(events), calendars)
            else:
                widget.days = DAYS
            widget.resize(1042, 600 if kind != 'month' else 700)
            widget.show()
            app.processEvents()
            return widget
        yield app, make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def hit(widget, identifier='event'):
    return next(rect for rect, item, *_ in widget.hits if item.id == identifier)


def month_day_point(widget, day, row_offset=42):
    index = (day - widget.start).days
    width, height = widget.width() / 7, (widget.height() - 28) / 6
    return QPoint(int((index % 7 + .5) * width), int(28 + (index // 7) * height + row_offset))


def test_adaptive_hours_fit_viewport_and_position_returns_actual_clock(surfaces):
    _, make = surfaces
    widget = make('timed')
    assert (widget.start_hour, widget.end_hour) == (8, 18)
    assert widget.time_top == 0 and widget.minimumHeight() == 600
    assert widget.hour_height >= 24
    x = widget.gutter + 2.5 * (widget.width() - widget.gutter) / 7
    assert widget.position(QPointF(x, 0)) == (2, 8 * 60)
    assert widget.position(QPointF(x, 1.25 * widget.hour_height)) == (2, 9 * 60 + 15)
    assert widget.position(QPointF(x, -500)) == (2, 8 * 60)
    assert widget.position(QPointF(x, 10000), endpoint=True) == (2, 18 * 60)
    widget.fit_to_viewport(100, full_day=True)
    assert (widget.start_hour, widget.end_hour, widget.hour_height) == (0, 24, 24)
    assert widget.minimumHeight() == 24 * 24 + 20


def test_actual_timed_events_expand_hours_and_all_day_stays_on_fixed_surface(surfaces):
    _, make = surfaces
    events = [sample('early', hour=6, minute=30), sample('late', hour=20, minute=15, duration=105),
              sample('all', all_day=True, hour=0, duration=3 * 1440)]
    widget = make('timed', events)
    assert (widget.start_hour, widget.end_hour) == (6, 22)
    assert {item.id for _, item, _ in widget.rects()} == {'early', 'late'}
    assert min(rect.top() for rect, _, _ in widget.rects()) >= 0
    preview = sample('preview', hour=4, minute=30)
    geometry = widget.start_hour, widget.end_hour, widget.hour_height
    widget.set_preview([preview])
    assert (widget.start_hour, widget.end_hour, widget.hour_height) == geometry
    widget.fit_to_viewport(600)
    assert widget.start_hour == 4
    assert {item.id for _, item, _ in widget.hits} == {'early', 'late'}
    widget.set_preview([sample('offscreen', day=10, hour=1)])
    widget.fit_to_viewport(600)
    assert widget.start_hour == 6


def test_fixed_all_day_events_are_single_connected_bars_with_clipped_continuations(surfaces):
    _, make = surfaces
    left = sample('left', day=-1, hour=0, duration=4 * 1440, all_day=True)
    right = sample('right', day=3, hour=0, duration=7 * 1440, all_day=True)
    overlapping = sample('overlap', day=2, hour=0, duration=3 * 1440, all_day=True)
    widget = make('all-day', [left, right, overlapping, sample('timed')])
    assert len(widget.rects()) == 3
    width = (widget.width() - widget.gutter) / 7
    assert hit(widget, 'left').width() == pytest.approx(3 * width - 6)
    assert hit(widget, 'right').width() == pytest.approx(4 * width - 6)
    assert hit(widget, 'left').top() == hit(widget, 'right').top()
    assert hit(widget, 'overlap').top() > hit(widget, 'left').top()
    continuation = {item.id: (before, after) for item, _lane, _start, _end, _number, before, after in widget._lanes(widget.events)}
    assert continuation['left'] == (True, False)
    assert continuation['right'] == (False, True)
    widget.set_selection({'left', 'overlap'})
    image = widget.grab().toImage()
    for identifier in ('left', 'overlap'):
        rect = hit(widget, identifier)
        assert image.pixelColor(int(rect.left() + 4), int(rect.center().y())) == QColor('#2c527c')


def test_month_all_day_span_is_one_connected_bar_and_click_uses_its_segment_date(surfaces):
    _, make = surfaces
    source = sample('trip', day=1, hour=0, duration=3 * 1440, all_day=True)
    widget = make('month', [source])
    bars = [(rect, item) for rect, item in widget.hits if item.id == source.id]
    assert len(bars) == 1
    rect = bars[0][0]
    width = widget.width() / 7
    assert rect.width() == pytest.approx(3 * width - 10)
    assert widget.day_at(rect.topLeft()) == FIRST + timedelta(days=1)
    assert widget.day_at(rect.bottomRight()) == FIRST + timedelta(days=3)
    clicked = QPoint(int(2.5 * width), int(rect.center().y()))
    selection = QSignalSpy(widget.selected_scoped)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=clicked)
    assert selection.count() == 1 and selection.at(0)[0] == source.id
    assert widget.selected_day == FIRST + timedelta(days=2)


def test_month_all_day_span_splits_only_at_week_boundary_with_continuation_arrows(surfaces):
    _, make = surfaces
    source = sample('trip', day=4, hour=0, duration=5 * 1440, all_day=True)
    widget = make('month', [source])
    with patch.object(widget, '_paint_month_event', wraps=widget._paint_month_event) as painted:
        widget.grab()
    bars = [rect for rect, item in widget.hits if item.id == source.id]
    assert len(bars) == 2
    width, height = widget.width() / 7, (widget.height() - 28) / 6
    assert bars[0].width() == pytest.approx(3 * width - 10)
    assert bars[1].width() == pytest.approx(2 * width - 10)
    assert bars[1].top() - bars[0].top() == pytest.approx(height)
    arrows = [(call.kwargs['before'], call.kwargs['after']) for call in painted.call_args_list
              if call.args[2].id == source.id]
    assert arrows == [(False, True), (True, False)]


def test_month_all_day_lanes_share_cell_capacity_with_pinned_timed_selection(surfaces):
    _, make = surfaces
    spans = [sample(f'trip-{index}', hour=0, duration=3 * 1440, all_day=True) for index in range(8)]
    timed = sample('selected-timed', minute=17)
    widget = make('month', [*spans, timed])
    widget.set_selection({timed.id})
    widget.grab()
    visible = [(rect, item) for rect, item in widget.hits if widget.day_at(rect.topLeft()) == FIRST]
    assert timed.id in {item.id for _rect, item in visible}
    capacity = int((((widget.height() - 28) / 6) - 37) / 24)
    assert len(visible) <= capacity
    for index, (rect, _item) in enumerate(visible):
        assert rect.bottom() < 28 + (widget.height() - 28) / 6
        assert not any(rect.intersects(other) for other, _ in visible[index + 1:])
    # Hidden all-day occurrences remain one resource each, never daily pills.
    assert len({item.id for _rect, item in widget.hits}) == len(widget.hits)
    marker = hit(widget, timed.id)
    assert widget.grab().toImage().pixelColor(int(marker.left() + 4), int(marker.center().y())) == QColor('#2c527c')


def test_month_pending_all_day_spans_and_timed_ghosts_use_distinct_rows_without_hits(surfaces):
    _, make = surfaces
    widget = make('month', [sample('existing')])
    original = widget.grab().toImage()
    span = sample('preview-span', day=2, hour=0, duration=3 * 1440, all_day=True)
    timed = sample('preview-timed', day=2, hour=14)
    widget.set_preview([span, timed])
    with patch.object(widget, '_paint_month_event', wraps=widget._paint_month_event) as painted:
        assert widget.grab().toImage() != original
    previews = [(call.args[1], call.args[2]) for call in painted.call_args_list if call.kwargs.get('preview')]
    spans = [rect for rect, item in previews if item.id == span.id]
    times = [rect for rect, item in previews if item.id == timed.id]
    assert len(spans) == len(times) == 1
    assert spans[0].width() == pytest.approx(3 * widget.width() / 7 - 10)
    assert not spans[0].intersects(times[0])
    assert {item.id for _rect, item in widget.hits} == {'existing'}


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_soft_multiple_selection_keeps_calendar_color_and_no_persistent_day_fill(surfaces, kind):
    _, make = surfaces
    all_day = kind == 'all-day'
    values = [sample('event', all_day=all_day, hour=0 if all_day else 9, duration=1440 if all_day else 60),
              sample('second', day=2, all_day=all_day, hour=0 if all_day else 11, duration=1440 if all_day else 60)]
    widget = make(kind, values)
    before = widget.grab().toImage()
    widget.set_selected_day(DAYS[1])
    assert widget.grab().toImage() == before
    widget.set_selection({item.id for item in values})
    image = widget.grab().toImage()
    for identifier in ('event', 'second'):
        rect = hit(widget, identifier)
        assert image.pixelColor(int(rect.left() + 4), int(rect.center().y())) == QColor('#2c527c')
        fill = image.pixelColor(int(rect.right() - 8), int(rect.bottom() - 5))
        assert fill.red() > fill.blue()  # Original orange calendar tint remains.


def test_header_selection_day_is_state_only_while_navigation_can_highlight(surfaces):
    _, make = surfaces
    widget = make('header')
    original = widget.grab().toImage()
    widget.set_selected_day(DAYS[1])
    assert widget.grab().toImage() == original
    widget.set_navigation(True, 1)
    assert widget.grab().toImage() != original


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
@pytest.mark.parametrize('modifiers', [Qt.KeyboardModifier.NoModifier, CTRL])
def test_selected_multi_event_press_defers_collapse_or_toggle_until_no_drag_release(surfaces, kind, modifiers):
    _, make = surfaces
    item = sample(all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 9,
                  duration=2 * 1440 if kind == 'all-day' else 60)
    widget = make(kind, [item])
    widget.set_selection({item.id, 'another-selected-id'})
    selected = QSignalSpy(widget.selected_scoped)
    day_selected = QSignalSpy(widget.day_selected)
    point = hit(widget).center().toPoint()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, modifiers, point)
    assert selected.count() == 0
    assert widget.selected_ids == {item.id, 'another-selected-id'}
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, modifiers, point)
    assert selected.count() == 1 and selected.at(0) == [item.id, modifiers]
    assert day_selected.count() == 0


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_ctrl_drag_keeps_selection_and_emits_copy_modifier_without_toggling(surfaces, kind):
    _, make = surfaces
    item = sample(minute=17, all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 9,
                  duration=2 * 1440 if kind == 'all-day' else 45)
    widget = make(kind, [item])
    widget.set_selection({item.id, 'another-selected-id'})
    selected, moved, preview = QSignalSpy(widget.selected_scoped), QSignalSpy(widget.move_scoped_requested), QSignalSpy(widget.drag_preview_requested)
    rect = hit(widget)
    start = rect.center().toPoint()
    if kind == 'month':
        finish = month_day_point(widget, FIRST + timedelta(days=2))
        expected = timedelta(days=2)
    else:
        width = (widget.width() - widget.gutter) / 7
        if kind == 'all-day':
            start.setX(int(widget.gutter + .5 * width))
        else:
            start.setY(int(rect.bottom() - 2))  # Ctrl overrides the resize handle.
        finish = QPoint(start.x() + int(width), start.y())
        expected = timedelta(days=1)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, CTRL, start)
    QTest.mouseMove(widget, finish)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, CTRL, finish)
    assert selected.count() == 0
    assert widget.selected_ids == {item.id, 'another-selected-id'}
    assert moved.count() == 1 and preview.count() >= 1
    _reference, begin, end, modifiers = moved.at(0)
    assert modifiers & CTRL
    assert begin == item.start + expected and end == item.end + expected


def test_horizontal_ctrl_drag_keeps_start_and_end_seconds_and_microseconds(surfaces):
    _, make = surfaces
    source = sample(minute=17, duration=45)
    source = replace(source, start=source.start.replace(second=31, microsecond=123456),
                     end=source.end.replace(second=47, microsecond=654321))
    widget = make('timed', [source])
    widget.set_selection({source.id})
    moved = QSignalSpy(widget.move_scoped_requested)
    start = hit(widget).center().toPoint()
    width = (widget.width() - widget.gutter) / len(widget.days)
    finish = QPoint(start.x() + int(width), start.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, CTRL, start)
    QTest.mouseMove(widget, finish)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, CTRL, finish)
    assert moved.count() == 1
    _reference, begin, end, modifiers = moved.at(0)
    assert modifiers & CTRL
    assert begin == source.start + timedelta(days=1)
    assert end == source.end + timedelta(days=1)
    assert begin.microsecond == 123456 and end.microsecond == 654321


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_context_on_selected_event_keeps_existing_multi_selection(surfaces, kind):
    _, make = surfaces
    widget = make(kind, [sample(all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 9,
                                duration=1440 if kind == 'all-day' else 60)])
    widget.set_selection({'event', 'other'})
    selection, menu = QSignalSpy(widget.selected_scoped), QSignalSpy(widget.context_requested)
    point = hit(widget).center().toPoint()
    QApplication.sendEvent(widget, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, widget.mapToGlobal(point)))
    assert selection.count() == 0 and menu.count() == 1
    assert widget.selected_ids == {'event', 'other'}


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day', 'header'])
def test_placement_moves_and_confirms_without_selecting_creating_or_dragging(surfaces, kind):
    _, make = surfaces
    widget = make(kind, [sample(all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 9,
                                duration=1440 if kind == 'all-day' else 60)])
    widget.placement_active = True
    moved, confirmed, day = QSignalSpy(widget.placement_moved), QSignalSpy(widget.placement_confirmed), QSignalSpy(widget.day_selected)
    selection = QSignalSpy(widget.selected_scoped) if hasattr(widget, 'selected_scoped') else None
    created = QSignalSpy(widget.create_all_day_requested)
    point = hit(widget).center().toPoint() if kind != 'header' else QPoint(130, 30)
    QTest.mouseMove(widget, point)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=point)
    assert moved.count() >= 1 and confirmed.count() == 1
    assert confirmed.at(0)[0].tzinfo is not None
    assert confirmed.at(0)[0].hour == (0 if kind in ('header', 'all-day') else 9)
    assert day.count() == created.count() == 0
    assert selection is None or selection.count() == 0
    assert not getattr(widget, 'drag', None)


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_preview_instances_are_visible_but_never_added_to_hit_targets(surfaces, kind):
    _, make = surfaces
    original = sample('existing', all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 9,
                      duration=1440 if kind == 'all-day' else 60)
    preview = sample('preview-only', day=2, all_day=kind == 'all-day', hour=0 if kind == 'all-day' else 11,
                     duration=2 * 1440 if kind == 'all-day' else 60)
    widget = make(kind, [original])
    before = widget.grab().toImage()
    widget.set_preview([preview])
    assert widget.grab().toImage() != before
    assert {item.id for _rect, item, *_number in widget.hits} == {'existing'}
    widget.set_preview([])
    assert widget.grab().toImage() == before
