"""Calendar authoring changes the editor's draft, never unrelated events."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas
from debacal.models import Calendar, Event
from debacal.year_view import YearView


ZONE = ZoneInfo('Europe/Berlin')
MONDAY = date(2026, 11, 2)
DAYS = [MONDAY + timedelta(days=index) for index in range(7)]


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    calendars = [Calendar('local', 'Local', '#558877')]
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.year_view.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _kind, value, _trace: errors.append(value)):
        def make(kind, events=()):
            widget = {'timed': CalendarCanvas, 'month': MonthCanvas,
                      'all_day': AllDayCanvas, 'header': DayHeader, 'year': YearView}[kind]()
            widgets.append(widget)
            widget.resize(1100, 1640 if kind == 'timed' else 800)
            if kind == 'month':
                widget.set_data(MONDAY, 11, list(events), calendars)
            elif kind == 'year':
                widget.set_data(2026, list(events), calendars)
            elif kind == 'header':
                widget.days = DAYS
            else:
                widget.set_data(DAYS, list(events), calendars)
            widget.show()
            app.processEvents()
            return widget
        yield make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def occurrence(identity, *, all_day=False, hour=9, day=MONDAY):
    start = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=0 if all_day else hour)
    return Event(identity, 'local', identity, start,
                 start + timedelta(days=1) if all_day else start + timedelta(hours=1), all_day=all_day)


def event_rect(widget, identity):
    if isinstance(widget, MonthCanvas) and not isinstance(widget, AllDayCanvas):
        entries = widget.month_layout()[1]
    else:
        entries = widget.rects()
    return next(rect for rect, item in entries if item.id == identity)


def empty_point(widget, day=DAYS[2], hour=14):
    if isinstance(widget, YearView):
        return widget.day_rect(day).center().toPoint()
    if isinstance(widget, MonthCanvas) and not isinstance(widget, AllDayCanvas):
        index = (day - widget.start).days
        width = (widget.width() - widget.week_gutter) / 7
        height = (widget.height() - widget.header_height) / 6
        return QPoint(int(widget.week_gutter + (index % 7 + .5) * width),
                      int(widget.header_height + (index // 7 + .85) * height))
    width = (widget.width() - widget.gutter) / 7
    y = widget.time_top + (hour - widget.start_hour) * widget.hour_height if isinstance(widget, CalendarCanvas) else widget.height() // 2
    return QPoint(int(widget.gutter + (DAYS.index(day) + .5) * width), int(y))


@pytest.mark.parametrize('kind', ['timed', 'month', 'all_day'])
def test_only_draft_hits_select_and_drag_existing_events_instead_add_occurrence(surfaces, kind):
    draft = occurrence('draft', all_day=kind == 'all_day')
    existing = occurrence('existing', all_day=kind == 'all_day', hour=11,
                          day=DAYS[3] if kind == 'all_day' else MONDAY)
    widget = surfaces(kind, [existing, draft])
    widget.set_occurrence_picking({'draft'})
    placed, selected, moved, edited = (QSignalSpy(widget.placement_confirmed), QSignalSpy(widget.selected),
                                       QSignalSpy(widget.move_requested), QSignalSpy(widget.edit_requested))
    point = event_rect(widget, 'existing').center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert placed.count() == 1
    assert selected.count() == moved.count() == edited.count() == 0
    point = event_rect(widget, 'draft').center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert placed.count() == 1 and selected.count() == 1
    assert selected.at(0) == ['draft']
    assert moved.count() == edited.count() == 0
    menu = QSignalSpy(widget.context_requested)
    QApplication.sendEvent(widget, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, widget.mapToGlobal(point)))
    assert menu.count() == 0


@pytest.mark.parametrize('kind', ['timed', 'month', 'all_day', 'header', 'year'])
def test_hover_and_single_click_provide_targets_and_leave_clears_preview(surfaces, kind):
    widget = surfaces(kind)
    widget.set_occurrence_picking(set())
    moved, placed = QSignalSpy(widget.placement_moved), QSignalSpy(widget.placement_confirmed)
    point = empty_point(widget)
    QTest.mouseMove(widget, point)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert moved.count() >= 1 and placed.count() == 1
    assert placed.at(0)[0].date() == DAYS[2]
    if kind == 'timed':
        assert placed.at(0)[0].hour == 14
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert moved.at(moved.count() - 1) == [None]


@pytest.mark.parametrize('kind', ['timed', 'month', 'all_day'])
def test_hovering_draft_suppresses_placement_preview_and_ordinary_edit_returns_after_mode(surfaces, kind):
    widget = surfaces(kind, [occurrence('draft', all_day=kind == 'all_day')])
    widget.set_occurrence_picking({'draft'})
    hover = QSignalSpy(widget.placement_moved)
    point = event_rect(widget, 'draft').center().toPoint()
    QTest.mouseMove(widget, empty_point(widget))
    QTest.mouseMove(widget, point)
    assert hover.at(hover.count() - 1) == [None]
    edited = QSignalSpy(widget.edit_requested)
    widget.set_occurrence_picking(None)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert edited.count() == 1 and edited.at(0) == ['draft']
    assert not widget.occurrence_picking_active


@pytest.mark.parametrize('kind', ['timed', 'month', 'all_day'])
def test_draft_move_uses_normal_drag_without_adding_an_occurrence(surfaces, kind):
    draft = occurrence('draft', all_day=kind == 'all_day')
    widget = surfaces(kind, [draft])
    widget.set_occurrence_picking({'draft'})
    moved, placed = QSignalSpy(widget.move_requested), QSignalSpy(widget.placement_confirmed)
    start = event_rect(widget, 'draft').center().toPoint()
    gutter = widget.week_gutter if kind == 'month' else widget.gutter
    end = start + QPoint(round((widget.width() - gutter) / 7), 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    assert moved.count() == 1 and placed.count() == 0
    identity, begin, finish = moved.at(0)
    assert identity == 'draft' and begin.date() == DAYS[1]
    assert finish - begin == draft.end - draft.start


def test_timed_draft_resizes_and_empty_drag_can_choose_an_explicit_duration(surfaces):
    widget = surfaces('timed', [occurrence('draft')])
    widget.set_occurrence_picking({'draft'})
    moved, created, placed = QSignalSpy(widget.move_requested), QSignalSpy(widget.create_requested), QSignalSpy(widget.placement_confirmed)
    rect = event_rect(widget, 'draft')
    start = QPoint(int(rect.center().x()), int(rect.bottom() - 2))
    end = start + QPoint(0, widget.hour_height)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    assert moved.count() == 1 and moved.at(0)[2].hour == 11
    start = empty_point(widget, DAYS[2], hour=14)
    end = start + QPoint(0, widget.hour_height * 2)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    assert created.count() == 1 and placed.count() == 0
    assert created.at(0)[1] - created.at(0)[0] == timedelta(hours=2)


def test_all_day_draft_can_resize_its_end_without_moving_its_start(surfaces):
    draft = occurrence('draft', all_day=True)
    widget = surfaces('all_day', [draft])
    widget.set_occurrence_picking({'draft'})
    moved = QSignalSpy(widget.move_requested)
    rect = event_rect(widget, 'draft')
    start = QPoint(int(rect.right() - 2), int(rect.center().y()))
    end = start + QPoint(round((widget.width() - widget.gutter) / 7) * 2, 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end)
    assert moved.count() == 1
    assert moved.at(0) == ['draft', draft.start, draft.end + timedelta(days=2)]


@pytest.mark.parametrize('kind', ['month', 'header', 'year'])
def test_period_navigation_remains_available_while_picking(surfaces, kind):
    widget = surfaces(kind)
    widget.set_occurrence_picking(set())
    requested, placed = QSignalSpy(widget.week_requested), QSignalSpy(widget.placement_confirmed)
    if kind == 'month':
        point = widget.week_rect(0).center().toPoint()
    elif kind == 'header':
        point = widget.week_rect().center().toPoint()
    else:
        widget._ensure_geometry()
        point = next(iter(widget._week_rects.values()))[0].center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert requested.count() == 1 and placed.count() == 0


@pytest.mark.parametrize('kind', ['timed', 'month', 'all_day'])
def test_refreshing_unchanged_draft_ids_preserves_an_active_drag(surfaces, kind):
    widget = surfaces(kind, [occurrence('draft', all_day=kind == 'all_day')])
    widget.set_occurrence_picking({'draft'})
    start = event_rect(widget, 'draft').center().toPoint()
    gutter = widget.week_gutter if kind == 'month' else widget.gutter
    finish = start + QPoint(round((widget.width() - gutter) / 7), 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, finish)
    drag, ghost = widget.drag, widget.ghost
    assert drag is not None and ghost is not None
    widget.set_occurrence_picking({'draft'})
    assert widget.drag is drag and widget.ghost is ghost
    moved = QSignalSpy(widget.move_requested)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
    assert moved.count() == 1
    # A changed draft cannot keep a pointer gesture using stale event IDs.
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, finish)
    widget.set_occurrence_picking({'replacement'})
    assert widget.drag is None and widget.ghost is None
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
    assert moved.count() == 1
