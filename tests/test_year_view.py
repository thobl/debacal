"""Year overview rendering, interval indexing and native gestures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.year_view import YearView


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
DAY = START.date()


def event(identity='event', **changes):
    return replace(Event(identity, 'local', 'Synthetic appointment', START,
                         START + timedelta(hours=1)), **changes)


@pytest.fixture
def view():
    app = QApplication.instance() or QApplication([])
    with patch('debacal.year_view.local_zone', return_value=ZONE), \
         patch('debacal.year_view.now', return_value=START):
        widget = YearView()
        widget.set_preferences(Preferences(locale_name='en_GB'))
        widget.resize(1040, widget.heightForWidth(1040))
        widget.set_data(2026, [], [Calendar('local', 'Local', '#123456')])
        widget.show()
        app.processEvents()
        yield widget
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def summary(view, day):
    return view._summaries[(day - date(view.year, 1, 1)).days]


def test_twelve_month_grid_reflows_and_tracks_leap_days(view):
    assert len(view._month_rects) == 12 and len(view._day_rects) == 365
    assert view.month_rect(1).top() == view.month_rect(4).top()
    view.resize(780, view.heightForWidth(780))
    assert view.month_rect(1).top() < view.month_rect(4).top()
    assert view.month_rect(1).top() == view.month_rect(3).top()
    view.set_data(2024, [], [])
    assert view.day_rect(date(2024, 2, 29)) is not None
    assert view.day_at(view.day_rect(date(2024, 2, 29)).center()) == date(2024, 2, 29)
    assert len(view._day_rects) == 366


def test_locale_week_start_text_scale_and_week_numbers(view):
    view.set_preferences(Preferences(locale_name='de_DE', start_weekday=0, show_week_numbers=True))
    monday_rect = view.day_rect(date(2026, 1, 1))
    monday_grid = view._month_rects[1][2]
    assert view._month_names[2] == 'März' and view._weekday_names[0] == 'Mo.'
    assert abs((monday_rect.left() - monday_grid.left()) / monday_rect.width() - 3) < .001
    view.set_preferences(Preferences(locale_name='en_GB', start_weekday=6))
    sunday_rect = view.day_rect(date(2026, 1, 1))
    sunday_grid = view._month_rects[1][2]
    assert view._weekday_names[0] == 'Sun'
    assert abs((sunday_rect.left() - sunday_grid.left()) / sunday_rect.width() - 4) < .001
    before = view.heightForWidth(1400)
    view.set_preferences(Preferences(font_scale=140))
    assert view.heightForWidth(1400) > before
    view.resize(700, view.heightForWidth(700))
    assert view._columns(view.width()) == 2
    assert view.minimumWidth() < 700
    assert not view.grab().isNull()


def test_date_intervals_are_exclusive_and_timed_points_use_local_calendar_days(view):
    midnight = datetime(2026, 11, 5, tzinfo=ZONE)
    values = [event('timed', start=midnight - timedelta(hours=2), end=midnight),
              event('point', start=midnight, end=midnight),
              event('date', start=datetime(2026, 11, 4, tzinfo=timezone.utc),
                    end=datetime(2026, 11, 6, tzinfo=timezone.utc), all_day=True),
              event('foreign', start=datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc),
                    end=datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc))]
    view.set_data(2026, values, [])
    assert summary(view, DAY)['local'][0] == 2
    assert summary(view, DAY + timedelta(days=1))['local'][0] == 3
    assert summary(view, DAY + timedelta(days=2)) == {}
    assert [value.id for value in view.events_on_day(DAY)] == ['timed', 'date']
    assert [value.id for value in view.events_on_day(DAY + timedelta(days=1))] == ['point', 'date', 'foreign']


def test_year_clipping_cross_year_event_and_far_events(view):
    values = [event('cross', start=datetime(2025, 12, 29, tzinfo=ZONE),
                    end=datetime(2026, 1, 3, tzinfo=ZONE), all_day=True),
              event('outside', start=datetime(2027, 1, 1, tzinfo=ZONE),
                    end=datetime(2027, 1, 2, tzinfo=ZONE), all_day=True)]
    view.set_data(2026, values, [])
    assert summary(view, date(2026, 1, 1))['local'][0] == 1
    assert summary(view, date(2026, 1, 2))['local'][0] == 1
    assert summary(view, date(2026, 1, 3)) == {}
    assert view.event_rect('cross') is not None
    assert view.event_rect('outside') is None


def test_exact_selected_occurrences_highlight_all_their_days_and_prefer_selected_marker(view):
    values = [event('first'), event('selected', end=START + timedelta(days=3))]
    view.set_data(2026, values, [Calendar('local', 'Read-only calendar', '#aabbcc', read_only=True)])
    assert summary(view, DAY)['local'] == (2, 'first')
    view.set_selection({'selected'})
    view.selected_id = 'selected'
    view.set_selected_day(DAY + timedelta(days=1))
    view._ensure_geometry()
    for day in (DAY, DAY + timedelta(days=1), DAY + timedelta(days=2), DAY + timedelta(days=3)):
        selected = view._selected_summaries[(day - date(2026, 1, 1)).days]
        assert selected['local'][1] == 'selected'
    assert any(identity == 'selected' for _rect, identity, _day, _calendar in view._marker_hits)
    assert not view.grab().isNull()
    view.set_selection(set())
    assert not any(view._selected_summaries)
    assert view.selected_id is None


def test_markers_select_with_modifiers_without_clearing_previous_day_selection(view):
    view.set_data(2026, [event()], [Calendar('local', 'Local', '#123456')])
    days, scoped, legacy = [], [], []
    view.day_selected.connect(days.append)
    view.selected_scoped.connect(lambda identity, modifiers: scoped.append((identity, modifiers)))
    view.selected.connect(legacy.append)
    rect = view.event_rect('event')
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, rect.center().toPoint())
    assert days == []
    assert scoped == [('event', Qt.KeyboardModifier.ControlModifier)] and legacy == ['event']
    assert view.selected_day == DAY and view.selected_id == 'event'
    empty = date(2026, 11, 8)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.day_rect(empty).center().toPoint())
    assert days == [empty]


def test_month_heading_drills_down_and_day_double_click_always_creates_all_day(view):
    months, created = [], []
    view.month_requested.connect(months.append)
    view.create_all_day_requested.connect(lambda start, end: created.append((start, end)))
    view.set_data(2026, [event()], [])
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.month_rect(11).center().toPoint())
    assert months == [date(2026, 11, 1)]
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=view.event_rect('event').center().toPoint())
    assert created == [(datetime(2026, 11, 4, tzinfo=ZONE), datetime(2026, 11, 5, tzinfo=ZONE))]


def test_paste_preview_day_hover_and_click_confirm_without_creation(view):
    target = date(2026, 12, 14)
    preview = event('preview', start=datetime(2026, 12, 14, 9, tzinfo=ZONE), end=datetime(2026, 12, 14, 10, tzinfo=ZONE))
    view.set_creation_calendar(Calendar('other', 'Other', '#aa4400'))
    view.set_preview([preview])
    view.placement_active = True
    moved, confirmed, created = [], [], []
    view.placement_moved.connect(moved.append)
    view.placement_confirmed.connect(confirmed.append)
    view.create_all_day_requested.connect(lambda *args: created.append(args))
    point = view.day_rect(target).center().toPoint()
    QTest.mouseMove(view, point)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=point)
    assert moved[-1].date() == target and confirmed[-1].date() == target
    assert view._preview_summaries[(target - date(2026, 1, 1)).days]['local'][0] == 1
    # The second half of a double click cannot create after placement commits.
    view.placement_active = False
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=point)
    assert created == []
    view.set_preview([])
    assert not any(view._preview_summaries)


def test_large_overlapping_resources_use_compact_summaries_and_paint_without_event_iteration(view):
    first = datetime(2026, 1, 1, tzinfo=ZONE)
    last = datetime(2027, 1, 1, tzinfo=ZONE)
    values = [event(str(index), start=first, end=last, all_day=True) for index in range(50_000)]
    started = perf_counter()
    view.set_data(2026, values, [Calendar('local', 'Local', '#123456')])
    assert perf_counter() - started < 2
    assert len(view._summaries) == 365
    assert sum(len(value) for value in view._summaries) == 365
    assert summary(view, DAY)['local'][0] == 50_000
    assert len(view.events_on_day(DAY, limit=20)) == 20
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError('Painting must use summaries, not scan events.')
    view.events = NoIteration(values)
    for _ in range(3):
        assert not view.grab().isNull()


@pytest.mark.parametrize('year', [1, 9999])
def test_supported_date_boundaries_render_without_overflow(view, year):
    view.set_preferences(Preferences(show_week_numbers=True, start_weekday=6))
    view.set_data(year, [], [])
    assert view.day_rect(date(year, 1, 1)) is not None
    assert view.day_rect(date(year, 12, 31)) is not None
    assert not view.grab().isNull()


def test_selected_fourth_calendar_is_pinned_into_the_three_visible_markers(view):
    calendars = [Calendar(identity, 'Synthetic ' + identity, color)
                 for identity, color in [('a', '#a14747'), ('b', '#47815a'), ('c', '#4767a1'), ('d', '#925ca1')]]
    values = [event('event-' + calendar.id, calendar_id=calendar.id) for calendar in calendars]
    view.set_data(2026, values, calendars)
    view._ensure_geometry()
    assert {value.id for _rect, value in view.hits} == {'event-a', 'event-b', 'event-c'}
    view.set_selection({'event-d'})
    view._ensure_geometry()
    assert len(view.hits) == 3
    assert 'event-d' in {value.id for _rect, value in view.hits}
    selected = []
    view.selected_scoped.connect(lambda identity, _modifiers: selected.append(identity))
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.event_rect('event-d').center().toPoint())
    assert selected == ['event-d']


def test_preview_outline_uses_its_event_calendar_even_if_creation_target_changes(view):
    from PySide6.QtGui import QColor, QPen
    red = Calendar('red', 'Synthetic red', '#b12f45')
    blue = Calendar('blue', 'Synthetic blue', '#315ab7')
    view.set_data(2026, [], [red, blue])
    view.set_preview([event('preview', calendar_id=red.id)])
    for target in (blue, None):
        view.set_creation_calendar(target)
        with patch('debacal.year_view.QPen', wraps=QPen) as pens:
            view.grab()
        outlines = [call.args[0] for call in pens.call_args_list
                    if len(call.args) == 3 and call.args[2] == Qt.PenStyle.DashLine]
        assert outlines == [QColor(red.color)]
    # A destination can have no real events yet; its matching creation target
    # still supplies its color, without coloring unrelated unknown calendars.
    view.set_data(2026, [], [])
    view.set_creation_calendar(blue)
    view.set_preview([event('preview', calendar_id=blue.id)])
    with patch('debacal.year_view.QPen', wraps=QPen) as pens:
        view.grab()
    outlines = [call.args[0] for call in pens.call_args_list
                if len(call.args) == 3 and call.args[2] == Qt.PenStyle.DashLine]
    assert outlines == [QColor(blue.color)]


def test_year_selection_keeps_calendar_marker_color_without_blue_ink(view):
    from PySide6.QtGui import QColor, QPen
    from debacal.event_style import contrast_ratio
    source = Calendar('orange', 'Orange', '#ed7135')
    item = event('chosen', calendar_id=source.id)
    view.set_data(2026, [item], [source])
    view.set_selection({item.id})
    with patch('debacal.year_view.QPen', wraps=QPen) as pens, \
            patch.object(view, '_text', wraps=view._text) as labels:
        view.grab()
    source_edges = [call.args[0] for call in pens.call_args_list
                    if len(call.args) > 1 and call.args[1] == 1.5 and isinstance(call.args[0], QColor)]
    assert source_edges
    assert source_edges[0].hslHueF() == pytest.approx(QColor(source.color).hslHueF(), abs=.01)
    assert contrast_ratio(source_edges[0], '#ffffff') >= 3
    assert not any(call.args[0] == QColor('#2c527c') for call in pens.call_args_list if call.args)
    assert not any(len(call.args) > 3 and call.args[3] == QColor('#2c527c') for call in labels.call_args_list)


def test_year_event_marker_tooltip_shows_details_without_interface_help(view):
    from dataclasses import replace
    source = Calendar('local', 'Personal', '#b37a4d')
    item = replace(event('chosen'), title='A <meeting>', location='Room & garden', description='Bring notes')
    view.set_data(2026, [item], [source])
    marker = view.event_rect(item.id)
    QTest.mouseMove(view, QPoint(1, 1))
    QTest.mouseMove(view, marker.center().toPoint())
    tooltip = view.toolTip()
    assert 'A &lt;meeting&gt;' in tooltip and 'Room &amp; garden' in tooltip and 'Bring notes' in tooltip
    assert not any(phrase in tooltip for phrase in ('Click ', 'Double-click', 'select an event'))


def test_context_menu_keeps_ampersands_literal_in_event_titles(view):
    from PySide6.QtGui import QContextMenuEvent
    view.set_data(2026, [event(title='Research & Development && Design')], [])
    point = view.day_rect(DAY).center().toPoint()
    context = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.mapToGlobal(point))
    with patch('debacal.year_view.QMenu') as constructor:
        view.contextMenuEvent(context)
    labels = [call.args[0] for call in constructor.return_value.addAction.call_args_list]
    assert any(label.endswith('Research && Development &&&& Design') for label in labels)


def test_marker_context_requests_shared_event_actions_without_clearing_selection(view):
    from PySide6.QtGui import QContextMenuEvent
    first, second = event('first'), event('second', start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1))
    view.set_data(2026, [first, second], [Calendar('local', 'Local', '#123456')])
    view.set_selection({first.id, second.id})
    point = view.event_rect(first.id).center().toPoint()
    contexts, day_changes, selections = [], [], []
    view.context_requested.connect(lambda identity, _point: contexts.append(identity))
    view.day_selected.connect(day_changes.append)
    view.selected_scoped.connect(lambda identity, _modifiers: selections.append(identity))
    context = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.mapToGlobal(point))
    with patch('debacal.year_view.QMenu') as constructor:
        view.contextMenuEvent(context)
        constructor.assert_not_called()
    assert contexts == [first.id] and not day_changes and not selections
    assert view.selected_ids == {first.id, second.id}
