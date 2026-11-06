"""Calendar clicks select dates; creation stays explicit and visible."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QColor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from lebacal.calendar_widgets import CalendarCanvas, DayHeader, MonthCanvas
from lebacal.models import Calendar, Event


ZONE = ZoneInfo('Europe/Berlin')
MONDAY = date(2026, 11, 2)
DAYS = [MONDAY + timedelta(days=index) for index in range(7)]
NOW = datetime(2026, 11, 5, 12, 30, tzinfo=ZONE)


def event(key='event', *, all_day=False, duration=1, start=None):
    start = start or datetime(2026, 11, 2, 9, tzinfo=ZONE)
    return Event(key, 'local', 'Calendar event', start, start + timedelta(hours=duration), all_day=all_day)


@pytest.fixture
def widgets():
    app = QApplication.instance() or QApplication([])
    created, errors = [], []
    calendars = [Calendar('local', 'Personal', '#ffffff')]
    with patch('lebacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('lebacal.calendar_widgets.now', return_value=NOW), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def show(widget, width=1042, height=1640):
            created.append(widget)
            widget.resize(width, height)
            widget.show()
            app.processEvents()
            return widget

        def canvas(events=()):
            widget = CalendarCanvas()
            widget.set_data(DAYS, list(events), calendars)
            return show(widget)

        def month(events=(), *, height=700):
            widget = MonthCanvas()
            widget.set_data(date(2026, 10, 26), 11, list(events), calendars)
            return show(widget, 1050, height)

        def header():
            widget = DayHeader()
            widget.days = DAYS
            return show(widget, 1042, widget.height())

        yield app, created, calendars, canvas, month, header
        for widget in reversed(created):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def timed_point(canvas, index=0, hour=9, minute=0):
    width = (canvas.width() - canvas.gutter) / len(canvas.days)
    return QPoint(int(canvas.gutter + (index + .5) * width),
                  int(canvas.all_day_height + (hour + minute / 60) * canvas.hour_height))


def month_point(month, day, *, heading=False):
    offset = (day - month.start).days
    width, height = month.width() / 7, (month.height() - 28) / 6
    return QPoint(int((offset % 7 + .5) * width),
                  int(28 + (offset // 7) * height + (15 if heading else height - 12)))


def assert_all_day(spy, index, day):
    start, end = spy.at(index)
    assert start == datetime.combine(day, datetime.min.time(), ZONE)
    assert end == datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE)


def test_empty_single_clicks_select_day_without_creating_any_event(widgets):
    _, _, _, make_canvas, make_month, make_header = widgets
    for widget, point, day in (
        (make_canvas(), None, DAYS[2]),
        (make_month(), None, DAYS[2]),
        (make_header(), None, DAYS[2]),
    ):
        if isinstance(widget, CalendarCanvas):
            point = timed_point(widget, 2, 11)
        elif isinstance(widget, MonthCanvas):
            point = month_point(widget, day)
        else:
            point = QPoint(int(62 + 2.5 * (widget.width() - 62) / 7), 35)
        selected = QSignalSpy(widget.day_selected)
        all_day = QSignalSpy(widget.create_all_day_requested)
        timed = QSignalSpy(widget.create_requested) if hasattr(widget, 'create_requested') else None
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
        assert widget.selected_day == day
        assert selected.count() == 1 and selected.at(0) == [day]
        assert all_day.count() == 0
        assert timed is None or timed.count() == 0


def test_timed_double_click_creates_on_empty_grid_and_edits_existing_event(widgets):
    _, _, _, make_canvas, _, _ = widgets
    canvas = make_canvas([event()])
    created, edited = QSignalSpy(canvas.create_requested), QSignalSpy(canvas.edit_requested)
    for point in (timed_point(canvas, 1, 10, 15), timed_point(canvas, 0, 9, 30)):
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    assert created.count() == 1
    assert created.at(0) == [datetime(2026, 11, 3, 10, 15, tzinfo=ZONE), datetime(2026, 11, 3, 11, 15, tzinfo=ZONE)]
    assert edited.count() == 1 and edited.at(0) == ['event']
    assert canvas.drag is None and canvas.ghost is None


def test_timed_drag_still_creates_an_exact_range_but_single_click_does_not(widgets):
    _, _, _, make_canvas, _, _ = widgets
    canvas = make_canvas()
    created = QSignalSpy(canvas.create_requested)
    start, finish = timed_point(canvas, 3, 9, 15), timed_point(canvas, 3, 10, 45)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, finish)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=finish)
    assert created.count() == 1
    assert created.at(0) == [datetime(2026, 11, 5, 9, 15, tzinfo=ZONE), datetime(2026, 11, 5, 10, 45, tzinfo=ZONE)]
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=timed_point(canvas, 4, 9))
    assert created.count() == 1


def test_fixed_header_and_empty_canvas_strip_create_all_day_while_existing_event_edits(widgets):
    _, _, _, make_canvas, _, make_header = widgets
    existing = event(all_day=True, duration=24, start=datetime(2026, 11, 2, tzinfo=ZONE))
    canvas, header = make_canvas([existing]), make_header()
    for widget, points in (
        (canvas, [QPoint(130, 20), QPoint(270, 20)]),
        (header, [QPoint(130, 30), QPoint(270, header.all_day_top + 10)]),
    ):
        created = QSignalSpy(widget.create_all_day_requested)
        edited = QSignalSpy(widget.edit_requested) if widget is canvas else None
        for index, point in enumerate(points):
            QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
            if widget is canvas:
                assert created.count() == index
                if index:
                    assert_all_day(created, 0, DAYS[index])
            else:
                assert created.count() == index + 1
                assert_all_day(created, index, DAYS[index])
        assert edited is None or edited.count() == 1
        assert 'all-day' in widget.toolTip() or widget is canvas
    assert header.height() > header.all_day_top + 16


def test_month_double_click_heading_and_empty_body_create_while_existing_event_edits(widgets):
    _, _, _, _, make_month, _ = widgets
    month = make_month([event()])
    timed, all_day, edited, opened = (QSignalSpy(month.create_requested), QSignalSpy(month.create_all_day_requested),
                                    QSignalSpy(month.edit_requested), QSignalSpy(month.open_day))
    QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=month_point(month, DAYS[1], heading=True))
    assert_all_day(all_day, 0, DAYS[1])
    event_rect = month.hits[0][0]
    QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=event_rect.center().toPoint())
    QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=month_point(month, DAYS[2]))
    assert timed.count() == 1
    assert timed.at(0) == [datetime(2026, 11, 4, 9, tzinfo=ZONE), datetime(2026, 11, 4, 10, tzinfo=ZONE)]
    assert edited.count() == 1 and opened.count() == 0
    QTest.mouseMove(month, month_point(month, DAYS[1], heading=True))
    assert 'all-day' in month.toolTip()


@pytest.mark.parametrize('kind', ['canvas', 'month'])
def test_multiday_event_click_and_context_preserve_clicked_day_without_clearing_selection(widgets, kind):
    _, _, _, make_canvas, make_month, _ = widgets
    existing = event(all_day=True, duration=72, start=datetime(2026, 11, 2, tzinfo=ZONE))
    widget = make_canvas([existing]) if kind == 'canvas' else make_month([existing])
    if kind == 'canvas':
        point = QPoint(int(62 + 1.5 * (widget.width() - 62) / 7), 20)
    else:
        point = next(rect.center().toPoint() for rect, item in widget.hits
                     if item.id == existing.id and widget.day_at(rect.center()) == DAYS[1])
    order = []
    widget.day_selected.connect(lambda day: order.append(('day', day)))
    widget.selected.connect(lambda key: order.append(('event', key)))
    widget.context_requested.connect(lambda key, _position: order.append(('menu', key)))
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert order == [('event', existing.id)]
    assert widget.selected_day == DAYS[1]
    order.clear()
    QApplication.sendEvent(widget, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, widget.mapToGlobal(point)))
    assert order == [('menu', existing.id)]
    assert widget.selected_day == DAYS[1]


@pytest.mark.parametrize('kind', ['canvas', 'month'])
def test_selected_event_uses_visible_marker_independent_of_white_calendar_color(widgets, kind):
    _, _, _, make_canvas, make_month, _ = widgets
    existing = event()
    widget = make_canvas([existing]) if kind == 'canvas' else make_month([existing])
    rect = widget.hits[0][0]
    point = QPoint(int(rect.left() + 4), int(rect.center().y()))
    plain = widget.grab().toImage().pixelColor(point)
    widget.selected_id = existing.id
    widget.update()
    selected = widget.grab().toImage().pixelColor(point)
    assert selected == QColor('#2c527c')
    assert selected != plain


def test_selected_day_is_state_only_and_navigation_has_its_own_highlight(widgets):
    app, _, _, make_canvas, make_month, make_header = widgets
    canvas, month, header = make_canvas(), make_month(), make_header()
    selected = DAYS[1]
    initial = {widget: widget.grab().toImage() for widget in (canvas, month, header)}
    for widget in (canvas, month, header):
        widget.set_selected_day(selected)
        assert widget.grab().toImage() == initial[widget]
        assert widget.selected_day == selected and selected != NOW.date()
    canvas.set_navigation(True, 2)
    header.set_navigation(True, 2)
    app.processEvents()
    assert canvas.grab().toImage() != initial[canvas]
    assert header.grab().toImage() != initial[header]
    assert month.grab().toImage() == initial[month]
    for widget in (canvas, month, header):
        navigation = widget.grab().toImage()
        widget.set_selected_day(None)
        assert widget.grab().toImage() == navigation


@pytest.mark.parametrize('kind', ['header', 'month'])
def test_click_takes_focus_from_search_for_subsequent_calendar_shortcuts(widgets, kind):
    app, created, calendars, _, _, _ = widgets
    container = QWidget()
    created.append(container)
    layout = QVBoxLayout(container)
    search = QLineEdit()
    widget = DayHeader() if kind == 'header' else MonthCanvas()
    if kind == 'header':
        widget.days = DAYS
    else:
        widget.set_data(date(2026, 10, 26), 11, [], calendars)
    layout.addWidget(search)
    layout.addWidget(widget)
    container.resize(1050, 600)
    container.show()
    app.processEvents()
    search.setFocus()
    assert search.hasFocus()
    point = QPoint(130, 30) if kind == 'header' else month_point(widget, DAYS[0])
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert widget.hasFocus() and not search.hasFocus()
    search.setFocus()
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert widget.hasFocus() and not search.hasFocus()


@pytest.mark.parametrize('height', [440, 700])
def test_crowded_month_keeps_search_selected_event_visible_without_spilling_to_next_day(widgets, height):
    _, _, _, _, make_month, _ = widgets
    values = [event(str(index), start=datetime(2026, 11, 2, 8 + index, tzinfo=ZONE)) for index in range(8)]
    month = make_month(values, height=height)
    assert values[-1].id not in {item.id for _, item in month.hits}
    month.selected_id = values[-1].id
    month.update()
    image = month.grab().toImage()
    selected = [(rect, item) for rect, item in month.hits if item.id == values[-1].id]
    assert len(selected) == 1
    rect = selected[0][0]
    day_height = (month.height() - 28) / 6
    assert rect.bottom() < 28 + 2 * day_height
    marker = QPoint(int(rect.left() + 4), int(rect.center().y()))
    assert image.pixelColor(marker) == QColor('#2c527c')
    # All visible pills remain in their own day, including a one-row cell.
    assert all(month.day_at(rect.topLeft()) == month.day_at(rect.bottomRight()) for rect, _ in month.hits)
