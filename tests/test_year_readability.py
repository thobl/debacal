"""Year dates remain readable and event menus identify calendar sources."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QContextMenuEvent, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QApplication, QMenu

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.year_view import YearView


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 28, 9, tzinfo=ZONE)


@pytest.fixture
def overview():
    app = QApplication.instance() or QApplication([])
    with patch('debacal.year_view.local_zone', return_value=ZONE), \
         patch('debacal.year_view.now', return_value=START):
        view = YearView()
        view.resize(1040, view.heightForWidth(1040))
        view.show()
        app.processEvents()
        yield view
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def appointment(identity, calendar='local'):
    return Event(identity, calendar, 'Same title', START, START + timedelta(hours=1))


def recorded_paint(view):
    labels = []
    class Painter(QPainter):
        def drawText(self, *args):
            if len(args) == 3 and isinstance(args[2], str):
                labels.append((args[0], args[2], QFont(self.font())))
            return super().drawText(*args)
    with patch('debacal.year_view.QPainter', Painter):
        view.grab()
    return labels


@pytest.mark.parametrize('style', ['tracks', 'tints'])
@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('width', [400, 1040])
def test_selected_two_digit_dates_are_drawn_in_full_even_with_crowded_calendars(overview, style, scale, width):
    view = overview
    view.set_preferences(Preferences(year_view_style=style, font_scale=scale))
    view.resize(width, view.heightForWidth(width))
    sources = [Calendar(str(index), 'Calendar ' + str(index), '#547797') for index in range(6)]
    view.set_data(2026, [appointment(str(index), str(index)) for index in range(6)], sources)
    view.set_selection({'0'})
    day = view.day_rect(START.date())
    painted = [value for value in recorded_paint(view) if day.contains(value[0].center())]
    number, = [value for value in painted if value[1] == '28']
    rect, _value, font = number
    assert font.bold()
    assert QFontMetricsF(font).horizontalAdvance('28') <= rect.width() + .5
    assert not any(value == '…' or value == '...' for _rect, value, _font in painted)


def test_tinted_day_numbers_use_more_of_the_available_cell(overview):
    view = overview
    day = START.date()
    sizes = {}
    for style in ('tracks', 'tints'):
        view.set_preferences(Preferences(year_view_style=style))
        view.set_data(2026, [appointment('one')], [Calendar('local', 'Personal', '#547797')])
        target = view.day_rect(day)
        _rect, _text, font = next(value for value in recorded_paint(view)
                                 if value[1] == '28' and target.contains(value[0].center()))
        sizes[style] = font.pointSizeF()
    assert sizes['tints'] >= 1.2 * sizes['tracks']


def test_repeated_day_context_menus_release_their_actions_and_callbacks(overview):
    view = overview
    view.set_data(2026, [appointment('one')], [Calendar('local', 'Personal', '#547797')])
    point = view.day_rect(START.date()).center().toPoint()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.mapToGlobal(point))
    def make_menu(parent):
        menu = QMenu(parent)
        menu.exec = lambda *_args: None
        return menu
    with patch('debacal.year_view.QMenu', side_effect=make_menu):
        for _ in range(3):
            view.contextMenuEvent(event)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not view.findChildren(QMenu)


def test_day_context_menu_distinguishes_identical_event_titles_by_source(overview):
    view = overview
    calendars = [Calendar('work', 'Work', '#547797'), Calendar('personal', 'Personal & family', '#b6815c')]
    view.set_data(2026, [appointment('one', 'work'), appointment('two', 'personal')], calendars)
    selected = []
    view.selected.connect(selected.append)
    point = view.day_rect(START.date()).center().toPoint()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.mapToGlobal(point))
    menu = QMenu(view)
    menu.exec = lambda *_args: None
    with patch('debacal.year_view.QMenu', return_value=menu):
        view.contextMenuEvent(event)
    actions = [value for value in menu.actions() if 'Same title' in value.text()]
    assert len(actions) == 2
    assert 'Work' in actions[0].text() and 'Personal && family' in actions[1].text()
    assert all(not action.icon().isNull() for action in actions)
    assert actions[0].icon().pixmap(16, 16).toImage() != actions[1].icon().pixmap(16, 16).toImage()
    actions[1].trigger()
    assert selected == ['two']


def test_appearance_and_selection_reuse_year_geometry_and_interval_index(overview):
    view = overview
    view.set_data(2026, [appointment('one')], [Calendar('local', 'Personal', '#547797')])
    view._ensure_geometry()
    geometry, intervals = view._day_rects, view._intervals
    with patch.object(view, '_summarize', side_effect=AssertionError('Appearance must not reindex events')):
        view.set_preferences(replace(view.preferences, year_view_style='tints'))
        view._ensure_geometry()
        assert view._day_rects is geometry and view._intervals is intervals
    view.set_selection({'one'})
    view._ensure_geometry()
    assert view._day_rects is geometry


def test_interval_index_resolves_local_timezone_once_per_batch(overview):
    view = overview
    events = [appointment(str(index)) for index in range(5000)]
    with patch('debacal.year_view.local_zone', return_value=ZONE) as resolve:
        view.set_data(2026, events, [Calendar('local', 'Personal', '#547797')])
        assert resolve.call_count <= 2  # Event index plus the separate preview index.
    with patch('debacal.year_view.local_zone', return_value=ZONE) as resolve:
        view.set_preview(events[:5])
        assert resolve.call_count == 1
