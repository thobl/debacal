"""Candidate confirmation stays at the selected calendar event."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, DayHeader, MonthCanvas
from debacal.models import Calendar, Event
from debacal.preferences import Preferences


ZONE = ZoneInfo('Europe/Berlin')
FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=index) for index in range(7)]


def sample(kind='week', *, candidate=True, identifier='candidate'):
    all_day = kind in ('all-day', 'month-span')
    start = datetime(2026, 11, 2, 0 if all_day else 9, tzinfo=ZONE)
    return Event(identifier, 'local', 'Alternative slot', start,
                 start + (timedelta(days=9 if kind == 'month-span' else 2) if all_day else timedelta(hours=1)),
                 candidate=candidate, all_day=all_day, group_id='choices' if candidate else None)


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    calendar = Calendar('local', 'Personal', '#5f8a67')
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.now', return_value=datetime(2026, 11, 6, 12, tzinfo=ZONE)), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def make(kind='week', events=None, source=calendar):
            if events is None:
                events = [sample(kind)]
            widget = AllDayCanvas() if kind == 'all-day' else MonthCanvas() if kind.startswith('month') else CalendarCanvas()
            widgets.append(widget)
            if isinstance(widget, AllDayCanvas):
                widget.set_data(DAYS, list(events), [source])
            elif isinstance(widget, MonthCanvas):
                widget.set_data(FIRST, 11, list(events), [source])
            else:
                widget.fixed_all_day = True
                widget.set_data([FIRST] if kind == 'day' else DAYS, list(events), [source])
                widget.fit_to_viewport(600)
            widget.resize(1042, 700 if kind.startswith('month') else 600)
            widget.show()
            app.processEvents()
            return widget
        yield app, widgets, make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def select(widget, event_id='candidate'):
    widget.set_selection({event_id})
    widget.grab()
    return next(rect for rect, key in widget.confirm_hits if key == event_id)


@pytest.mark.parametrize('kind', ['day', 'week', 'month', 'month-span', 'all-day'])
def test_only_selected_candidates_have_inline_confirmation_and_previews_never_do(surfaces, kind):
    _, _, make = surfaces
    candidate = sample(kind)
    ordinary = replace(candidate, id='ordinary', candidate=False,
                       start=candidate.start + timedelta(days=3), end=candidate.end + timedelta(days=3))
    widget = make(kind, [candidate, ordinary])
    assert widget.confirm_hits == []
    widget.set_selection({candidate.id, ordinary.id})
    widget.grab()
    assert {key for _rect, key in widget.confirm_hits} == {candidate.id}
    actual_count = len(widget.confirm_hits)
    widget.set_preview([replace(candidate, id='preview')])
    widget.grab()
    assert len(widget.confirm_hits) == actual_count
    widget.set_selection({ordinary.id})
    widget.grab()
    assert widget.confirm_hits == []


@pytest.mark.parametrize('kind', ['day', 'week', 'month', 'month-span', 'all-day'])
def test_check_click_confirms_once_without_selecting_creating_or_dragging(surfaces, kind):
    _, _, make = surfaces
    widget = make(kind)
    control = select(widget)
    before = [item.to_dict() for item in widget.events]
    confirmed = QSignalSpy(widget.confirm_requested)
    selected, moved = QSignalSpy(widget.selected_scoped), QSignalSpy(widget.move_scoped_requested)
    created, days = QSignalSpy(widget.create_requested), QSignalSpy(widget.day_selected)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=control.center().toPoint())
    assert confirmed.count() == 0 and widget._confirm_timer.isActive()
    assert selected.count() == moved.count() == created.count() == days.count() == 0
    assert widget.drag is None and widget.ghost is None
    widget._confirm_timer.timeout.emit()
    assert confirmed.count() == 1 and confirmed.at(0) == ['candidate']
    widget._confirm_timer.timeout.emit()
    assert confirmed.count() == 1
    assert [item.to_dict() for item in widget.events] == before


def test_single_click_uses_real_qt_timer_and_emits_before_timeout_limit(surfaces):
    _, _, make = surfaces
    widget = make()
    control = select(widget)
    confirmed = QSignalSpy(widget.confirm_requested)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=control.center().toPoint())
    assert confirmed.wait(QApplication.doubleClickInterval() + 200)
    assert confirmed.count() == 1 and not widget._confirm_timer.isActive()


@pytest.mark.parametrize('kind', ['week', 'month', 'all-day'])
def test_real_double_click_sequence_edits_without_confirming_or_collapsing_selection(surfaces, kind):
    _, _, make = surfaces
    widget = make(kind)
    widget.set_selection({'candidate', 'another-selected-id'})
    widget.grab()
    control = widget.confirm_hits[0][0]
    confirmed, edited = QSignalSpy(widget.confirm_requested), QSignalSpy(widget.edit_scoped_requested)
    selected = QSignalSpy(widget.selected_scoped)
    point = control.center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert widget._confirm_timer.isActive()
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert not widget._confirm_timer.isActive()
    widget._confirm_timer.timeout.emit()
    assert confirmed.count() == 0 and edited.count() == 1
    assert selected.count() == 0
    assert widget.selected_ids == {'candidate', 'another-selected-id'}


@pytest.mark.parametrize('kind', ['week', 'month', 'all-day'])
def test_dragging_off_check_control_cancels_without_starting_an_event_drag(surfaces, kind):
    _, _, make = surfaces
    widget = make(kind)
    control = select(widget)
    confirmed, moved, selected = QSignalSpy(widget.confirm_requested), QSignalSpy(widget.move_scoped_requested), QSignalSpy(widget.selected_scoped)
    start = control.center().toPoint()
    finish = start + QPoint(35, 8)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, finish)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
    widget._confirm_timer.timeout.emit()
    assert confirmed.count() == moved.count() == selected.count() == 0
    assert widget.drag is None and widget.ghost is None


@pytest.mark.parametrize('change', ['selection', 'data', 'placement', 'hide'])
def test_pending_confirmation_is_cancelled_when_its_context_changes(surfaces, change):
    _, _, make = surfaces
    widget = make()
    control = select(widget)
    confirmed = QSignalSpy(widget.confirm_requested)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=control.center().toPoint())
    if change == 'selection':
        widget.set_selection(set())
    elif change == 'data':
        widget.set_data(DAYS, [sample()], list(widget.calendars.values()))
    elif change == 'placement':
        widget.placement_active = True
        widget.set_preview([sample()])
    else:
        widget.hide()
    assert not widget._confirm_timer.isActive()
    widget._confirm_timer.timeout.emit()
    assert confirmed.count() == 0


@pytest.mark.parametrize('restriction', ['event-read-only', 'calendar-read-only', 'subscription'])
def test_read_only_candidates_never_offer_a_confirmation_action(surfaces, restriction):
    _, _, make = surfaces
    item = sample()
    source = Calendar('local', 'Personal', '#5f8a67')
    if restriction == 'event-read-only':
        item = replace(item, read_only=True)
    elif restriction == 'calendar-read-only':
        source = replace(source, read_only=True)
    else:
        source = replace(source, kind='ics', url='https://calendar.example.test/feed.ics')
    widget = make(events=[item], source=source)
    widget.set_selection({item.id})
    widget.grab()
    assert widget.confirm_hits == []


def test_confirmation_label_space_keeps_g_navigation_badge_separate_and_scales(surfaces):
    _, _, make = surfaces
    widget = make()
    widget.set_preferences(Preferences(font_scale=140))
    widget.fit_to_viewport(600)
    widget.set_selection({'candidate'})
    widget.set_navigation(True, 0, '1')
    with patch('debacal.calendar_widgets.badge', wraps=calendar_widgets.badge) as numbered:
        widget.grab()
    control = widget.confirm_hits[0][0]
    assert control.width() > 18
    assert all(not call.args[1].intersects(control) for call in numbered.call_args_list)
    event_rect = next(rect for rect, item, _number in widget.hits if item.id == 'candidate')
    assert event_rect.contains(control)


@pytest.mark.parametrize('kind', ['week', 'month', 'all-day'])
def test_confirmation_tooltip_is_explicit_and_other_tooltips_describe_current_actions(surfaces, kind):
    _, widgets, make = surfaces
    widget = make(kind)
    control = select(widget)
    QTest.mouseMove(widget, control.center().toPoint())
    assert 'Confirm this slot' in widget.toolTip()
    assert 'other choices' in widget.toolTip() and 'Undo' in widget.toolTip()
    assert widget.cursor().shape() == Qt.CursorShape.PointingHandCursor
    if kind == 'week':
        blank = QPoint(widget.gutter + 25, int(widget.time_top + (12 - widget.start_hour) * widget.hour_height))
    elif kind == 'month':
        blank = QPoint(int(widget.width() / 7 * 1.5), int(widget.header_height + 75))
    else:
        blank = QPoint(widget.width() - 20, 15)
    QTest.mouseMove(widget, blank)
    assert 'Double-click' in widget.toolTip()
    assert 'select this day' not in widget.toolTip() and 'duplicate' not in widget.toolTip().lower()
    header = DayHeader()
    widgets.append(header)
    assert 'select a day' not in header.toolTip()
