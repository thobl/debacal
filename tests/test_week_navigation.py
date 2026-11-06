"""Week-number navigation follows the configured first weekday."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.preferences import Preferences
from debacal.year_view import YearView


@pytest.fixture
def year():
    app = QApplication.instance() or QApplication([])
    widget = YearView()
    widget.set_preferences(Preferences(start_weekday=0, show_week_numbers=True))
    widget.set_data(2026, [], [])
    widget.resize(1040, widget.heightForWidth(1040))
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('first_weekday,first,second', [
    (0, date(2025, 12, 29), date(2026, 1, 5)),
    (6, date(2025, 12, 28), date(2026, 1, 4)),
    (2, date(2025, 12, 31), date(2026, 1, 7)),
])
def test_year_week_column_opens_week_without_selecting_a_day(year, first_weekday, first, second):
    year.set_preferences(Preferences(start_weekday=first_weekday, show_week_numbers=True))
    weeks, days, months, creations = [], [], [], []
    year.week_requested.connect(weeks.append)
    year.day_selected.connect(days.append)
    year.month_requested.connect(months.append)
    year.create_all_day_requested.connect(lambda *values: creations.append(values))
    for row, expected in [(0, first), (1, second)]:
        rect = year.week_rect(1, row)
        assert rect is not None
        assert year.week_at(rect.center()) == expected
        assert year.day_at(rect.center()) is None
        QTest.mouseClick(year, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert weeks == [first, second]
    assert days == months == creations == []


def test_year_week_numbers_only_cover_rows_containing_days(year):
    # February 2027 begins on Monday and fits exactly into four rows.
    year.set_data(2027, [], [])
    assert [year.week_at(year.week_rect(2, row).center()) for row in range(4)] == [
        date(2027, 2, 1), date(2027, 2, 8), date(2027, 2, 15), date(2027, 2, 22)]
    assert year.week_rect(2, 4) is None and year.week_rect(2, 5) is None
    assert year.week_rect(2, -1) is None and year.week_rect(13, 0) is None
    # No week-number rectangle overlaps any of its day's rectangles.
    assert all(not rect.intersects(day_rect) for rect, _day in year._week_rects.values()
               for day_rect in year._day_rects.values())


def test_hiding_week_numbers_removes_navigation_and_returns_its_space(year):
    numbered_grid = year.day_rect(date(2026, 1, 1))
    old_target = year.week_rect(1, 0).center()
    year.set_preferences(Preferences(start_weekday=0, show_week_numbers=False))
    assert year.week_rect(1, 0) is None and year.week_at(old_target) is None
    assert not year._week_rects
    assert year.day_rect(date(2026, 1, 1)).width() > numbered_grid.width()


def test_week_column_hover_explains_navigation_and_resets_on_leave(year):
    point = year.week_rect(1, 0).center().toPoint()
    QTest.mouseMove(year, point)
    assert year._hover_week == (1, 0)
    assert year.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert year.toolTip().startswith('Open week 1')
    assert not year.grab().isNull()
    QApplication.sendEvent(year, QEvent(QEvent.Type.Leave))
    assert year._hover_week is None
    assert year.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert year.toolTip() == year._default_tooltip


@pytest.mark.parametrize('first_weekday', [0, 6])
def test_painted_numbers_match_configured_week_rows_at_year_boundary(year, first_weekday):
    year.set_preferences(Preferences(start_weekday=first_weekday, show_week_numbers=True))
    year.set_data(2027, [], [])
    with patch.object(year, '_text', wraps=year._text) as draw_text:
        year.grab()
    first_rect = year.week_rect(1, 0)
    labels = [call.args[2] for call in draw_text.call_args_list if call.args[1] == first_rect]
    assert labels == ['53']
    next_rect = year.week_rect(1, 1)
    labels = [call.args[2] for call in draw_text.call_args_list if call.args[1] == next_rect]
    assert labels == ['1']
