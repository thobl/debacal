"""Week numbers navigate to the represented week without acting as event cells."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.calendar_widgets import DayHeader, MonthCanvas, at, text
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2027, 1, 6, 9, tzinfo=ZONE)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
            patch('debacal.ui.now', return_value=START), \
            patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(kind, preferences):
            widget = kind()
            widgets.append(widget)
            widget.resize(1000, 700)
            widget.set_preferences(preferences)
            return widget
        yield app, create, widgets
        for widget in widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def settle(app):
    for _ in range(4):
        app.processEvents()


@pytest.mark.parametrize('first,start', [(0, date(2026, 12, 28)), (6, date(2026, 12, 27))])
def test_month_week_numbers_and_clicks_cross_the_year_in_the_configured_rows(desktop, first, start):
    _, create, _ = desktop
    preferences = Preferences(start_weekday=first)
    month = create(MonthCanvas, preferences)
    month.set_data(start, 1, [], [])
    weeks, selected, created = [], [], []
    month.week_requested.connect(weeks.append)
    month.day_selected.connect(selected.append)
    month.create_requested.connect(lambda *_: created.append(True))
    month.create_all_day_requested.connect(lambda *_: created.append(True))
    with patch('debacal.calendar_widgets.text', wraps=text) as painted:
        assert not month.grab().isNull()
    painted_numbers = [call.args[2] for call in painted.call_args_list
                       if call.args[1].left() == 0 and call.args[1].width() == month.week_gutter]
    assert painted_numbers[:3] == ['Wk', '53', '1']
    for row in range(6):
        point = month.week_rect(row).center()
        expected = start + timedelta(days=7 * row)
        assert month.week_at(point) == expected
        assert month.day_at(point) is None
        assert month.placement_target(point) is None
        QTest.mouseClick(month, Qt.MouseButton.LeftButton, pos=point.toPoint())
    assert weeks == [start + timedelta(days=7 * row) for row in range(6)]
    assert not selected and not created


@pytest.mark.parametrize('first,expected', [(0, date(2026, 12, 28)), (6, date(2027, 1, 3))])
def test_header_week_number_opens_the_week_containing_the_shown_day(desktop, first, expected):
    _, create, _ = desktop
    header = create(DayHeader, Preferences(start_weekday=first))
    header.days = [date(2027, 1, 3)]
    opened, selected = [], []
    header.week_requested.connect(opened.append)
    header.day_selected.connect(selected.append)
    point = header.week_rect().center()
    with patch('debacal.calendar_widgets.text', wraps=text) as painted:
        assert not header.grab().isNull()
    values = [call.args[2] for call in painted.call_args_list]
    assert 'WEEK' in values and ('53' if first == 0 else '1') in values
    assert header.day_at(point) is None
    QTest.mouseClick(header, Qt.MouseButton.LeftButton, pos=point.toPoint())
    assert opened == [expected] and not selected


@pytest.mark.parametrize('kind', [DayHeader, MonthCanvas])
def test_week_gutter_cannot_create_or_place_an_event(desktop, kind):
    app, create, _ = desktop
    widget = create(kind, Preferences(start_weekday=6))
    if kind is DayHeader:
        widget.days = [date(2027, 1, 3) + timedelta(days=index) for index in range(7)]
        point = widget.week_rect().center().toPoint()
    else:
        widget.set_data(date(2026, 12, 27), 1, [], [])
        point = widget.week_rect(1).center().toPoint()
    actions = []
    for signal in (widget.day_selected, widget.create_all_day_requested,
                   widget.placement_moved, widget.placement_confirmed):
        signal.connect(lambda *_: actions.append(True))
    if kind is MonthCanvas:
        widget.create_requested.connect(lambda *_: actions.append(True))
    widget.show()
    settle(app)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert not actions
    widget.placement_active = True
    opened = []
    widget.week_requested.connect(opened.append)
    QTest.mouseMove(widget, point)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert not actions and not opened


def test_month_events_and_hits_stay_out_of_the_week_number_gutter(desktop):
    _, create, _ = desktop
    month = create(MonthCanvas, Preferences(start_weekday=0, font_scale=140))
    month.resize(1000, 1100)
    start = date(2026, 12, 28)
    timed = Event('timed', 'local', 'Meeting', at(start, 9), at(start, 10))
    span = Event('trip', 'local', 'Trip', at(start), at(start + timedelta(days=3)), all_day=True)
    month.set_data(start, 1, [timed, span], [Calendar('local', 'Local', '#c8a320')])
    assert not month.grab().isNull()
    assert {event.id for _, event in month.hits} == {timed.id, span.id}
    for rect, event in month.hits:
        assert rect.left() > month.week_gutter
        assert month.hit(rect.center())[1].id == event.id
        assert month.day_at(rect.center()) >= start
    for row in range(6):
        assert month.hit(month.week_rect(row).center()) is None
    first_day = QPointF(month.week_gutter + 10, month.header_height + 10)
    assert month.day_at(first_day) == start
    assert month.week_at(first_day) is None


def test_saved_opt_out_removes_number_targets_without_losing_day_cells(desktop):
    _, create, _ = desktop
    preferences = Preferences.from_dict({'show_week_numbers': False, 'start_weekday': 0})
    header = create(DayHeader, preferences)
    header.days = [date(2027, 1, 4)]
    assert header.week_rect() is None
    month = create(MonthCanvas, preferences)
    month.set_data(date(2026, 12, 28), 1, [], [])
    assert month.week_gutter == 0 and month.week_rect(0) is None
    point = QPointF(10, month.header_height + 10)
    assert month.week_at(point) is None
    assert month.day_at(point) == date(2026, 12, 28)


@pytest.mark.parametrize('view', ['day', 'month', 'year'])
def test_week_targets_open_the_same_week_view_in_the_workspace(desktop, view):
    app, _, widgets = desktop
    with Store(':memory:') as store:
        Preferences(start_weekday=6).save(store)
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        window.go_date(START.date())
        window.set_view(view)
        settle(app)
        if view == 'day':
            surface, rect = window.header, window.header.week_rect()
        elif view == 'month':
            surface, rect = window.month, window.month.week_rect(1)
        else:
            surface, rect = window.year, window.year.week_rect(1, 1)
        QTest.mouseClick(surface, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        settle(app)
        assert window.view == 'week'
        assert window.anchor == date(2027, 1, 3)
        assert window.dates() == [date(2027, 1, 3) + timedelta(days=index) for index in range(7)]
        window.close()
