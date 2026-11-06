"""Editing gestures, timed creation guides, and copy-drag feedback."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from lebacal import calendar_widgets
from lebacal.calendar_widgets import AllDayCanvas, CalendarCanvas, MonthCanvas
from lebacal.models import Calendar, Event
from lebacal.preferences import Preferences


ZONE = ZoneInfo('Europe/Berlin')
FIRST = date(2026, 11, 2)
DAYS = [FIRST + timedelta(days=number) for number in range(7)]
CTRL = Qt.KeyboardModifier.ControlModifier
NONE = Qt.KeyboardModifier.NoModifier


def sample(kind='timed', identifier='event'):
    start = datetime(2026, 11, 2, 0 if kind == 'all-day' else 9, tzinfo=ZONE)
    return Event(identifier, 'local', 'Synthetic event', start,
                 start + (timedelta(days=2) if kind == 'all-day' else timedelta(hours=1)),
                 all_day=kind == 'all-day')


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    calendars = [Calendar('local', 'Personal', '#5f8a67')]
    with patch('lebacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('lebacal.calendar_widgets.now', return_value=datetime(2026, 11, 6, 12, tzinfo=ZONE)), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def make(kind='timed', events=(), *, days=DAYS):
            widget = {'timed': CalendarCanvas, 'month': MonthCanvas, 'all-day': AllDayCanvas}[kind]()
            widgets.append(widget)
            if kind == 'timed':
                widget.fixed_all_day = True
                widget.set_data(list(days), list(events), calendars)
                widget.fit_to_viewport(600)
            elif kind == 'month':
                widget.set_data(FIRST, 11, list(events), calendars)
            else:
                widget.set_data(list(days), list(events), calendars)
            widget.resize(1042, 700 if kind == 'month' else 600)
            widget.show()
            app.processEvents()
            QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
            return widget
        yield app, make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def hit(widget):
    return widget.hits[0][0]


def timed_point(widget, day=0, hour=9, minute=15):
    width = (widget.width() - widget.gutter) / len(widget.days)
    return QPoint(round(widget.gutter + (day + .5) * width),
                  round(widget.time_top + (hour + minute / 60 - widget.start_hour) * widget.hour_height))


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
@pytest.mark.parametrize('modifiers', [NONE, CTRL])
def test_double_click_selected_event_edits_without_clearing_union(surfaces, kind, modifiers):
    _, make = surfaces
    source = sample(kind)
    widget = make(kind, [source])
    widget.set_selection({source.id, 'another-selected-id'})
    edits, selection, days = QSignalSpy(widget.edit_scoped_requested), QSignalSpy(widget.selected_scoped), QSignalSpy(widget.day_selected)
    timed, all_day = QSignalSpy(widget.create_requested), QSignalSpy(widget.create_all_day_requested)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, modifiers, hit(widget).center().toPoint())
    assert edits.count() == 1 and edits.at(0) == [source.id, modifiers]
    assert selection.count() == days.count() == timed.count() == all_day.count() == 0
    assert widget.selected_ids == {source.id, 'another-selected-id'}
    assert widget.drag is None and widget.ghost is None


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_double_click_unselected_event_selects_normally_then_uses_edit_signal(surfaces, kind):
    _, make = surfaces
    source = sample(kind)
    widget = make(kind, [source])
    widget.set_selection({'previous-id'})
    order = []
    widget.selected_scoped.connect(lambda key, modifiers: order.append(('select', key, modifiers)))
    widget.edit_scoped_requested.connect(lambda key, modifiers: order.append(('edit', key, modifiers)))
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, CTRL, hit(widget).center().toPoint())
    assert order == [('select', source.id, NONE), ('edit', source.id, CTRL)]


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_placement_confirmation_consumes_following_double_click_without_editing(surfaces, kind):
    _, make = surfaces
    widget = make(kind, [sample(kind)])
    widget.placement_active = True
    confirmed, edited = QSignalSpy(widget.placement_confirmed), QSignalSpy(widget.edit_scoped_requested)
    widget.placement_confirmed.connect(lambda _target: setattr(widget, 'placement_active', False))
    point = hit(widget).center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=point)
    assert confirmed.count() == 1 and edited.count() == 0


@pytest.mark.parametrize('days', [[FIRST], DAYS])
def test_blank_timed_hover_snaps_line_and_formats_compact_time_label(surfaces, days):
    _, make = surfaces
    widget = make(events=[], days=days)
    widget.set_preferences(Preferences(time_format='12h'))
    original = widget.grab().toImage()
    point = timed_point(widget, 0, 9, 17)
    QTest.mouseMove(widget, point)
    assert widget.hover_time == (0, 9 * 60 + 15)
    assert widget.time_feedback() == (0, 9 * 60 + 15, False)
    with patch('lebacal.calendar_widgets.text', wraps=calendar_widgets.text) as painted:
        guided = widget.grab().toImage()
    expected = widget.preferences.format_time(time(9, 15))
    labels = [call.args[1] for call in painted.call_args_list if call.args[2] == expected]
    assert len(labels) == 1 and labels[0].width() < 100
    assert guided != original
    width = (widget.width() - widget.gutter) / len(days)
    y = round((9.25 - widget.start_hour) * widget.hour_height)
    x = round(widget.gutter + width - 10)
    assert guided.pixelColor(x, y) != original.pixelColor(x, y)
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert widget.hover_time is None and widget.grab().toImage() == original


@pytest.mark.parametrize('days', [[FIRST], DAYS])
def test_single_click_keeps_the_same_light_hover_guide_without_moving(surfaces, days):
    _, make = surfaces
    widget = make(days=days)
    point = timed_point(widget, 0, 10, 15)
    QTest.mouseMove(widget, point)
    original = widget.grab().toImage()
    created = QSignalSpy(widget.create_requested)
    for _ in range(2):
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=point)
        assert widget.time_feedback() == (0, 10 * 60 + 15, False)
        assert widget.grab().toImage() == original
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=point)
        assert widget.time_feedback() == (0, 10 * 60 + 15, False)
        assert widget.grab().toImage() == original
    assert created.count() == 0 and widget.drag is None and widget.ghost is None


def test_pending_click_hover_disappears_on_leave_and_release_outside(surfaces):
    _, make = surfaces
    widget = make()
    point = timed_point(widget, 0, 10, 15)
    QTest.mouseMove(widget, point)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=point)
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    assert widget.hover_time is None and widget.time_feedback() is None
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(-10, point.y()))
    assert widget.hover_time is None and widget.time_feedback() is None


def test_hover_guide_disappears_on_event_drag_and_placement(surfaces):
    _, make = surfaces
    widget = make(events=[sample()])
    blank = timed_point(widget, 1, 10, 15)
    QTest.mouseMove(widget, blank)
    assert widget.hover_time is not None
    point = hit(widget).center().toPoint()
    QTest.mouseMove(widget, point)
    assert widget.hover_time is None and widget.time_feedback() is None
    assert 'Double-click to edit' in widget.toolTip()
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(widget, blank)
    assert widget.time_feedback() is None
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=blank)
    QTest.mouseMove(widget, timed_point(widget, 2, 11, 15))
    assert widget.hover_time is not None
    widget.placement_active = True
    widget.set_preview([replace(sample(), id='preview')])
    assert widget.hover_time is None and widget.time_feedback() is None


@pytest.mark.parametrize('reverse', [False, True])
def test_creation_drag_keeps_its_actual_start_line_label_and_exact_interval(surfaces, reverse):
    _, make = surfaces
    widget = make()
    early, late = timed_point(widget, 2, 10, 15), timed_point(widget, 2, 11, 45)
    begin, finish = (late, early) if reverse else (early, late)
    created = QSignalSpy(widget.create_requested)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=begin)
    assert widget.time_feedback() == (2, (11 * 60 + 45) if reverse else (10 * 60 + 15), False)
    QTest.mouseMove(widget, finish)
    assert widget.time_feedback() == (2, 10 * 60 + 15, True)
    with patch('lebacal.calendar_widgets.text', wraps=calendar_widgets.text) as painted:
        widget.grab()
    assert any(call.args[2] == widget.preferences.format_time(time(10, 15)) for call in painted.call_args_list)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
    assert created.at(0) == [datetime(2026, 11, 4, 10, 15, tzinfo=ZONE), datetime(2026, 11, 4, 11, 45, tzinfo=ZONE)]
    assert widget.time_feedback() is None


@pytest.mark.parametrize('kind', ['timed', 'month', 'all-day'])
def test_copy_badge_updates_with_stationary_ctrl_and_union_previews(surfaces, kind):
    _, make = surfaces
    source = sample(kind)
    widget = make(kind, [source])
    widget.set_selection({source.id})
    begin = hit(widget).center().toPoint()
    if kind == 'month':
        finish = begin + QPoint(round(widget.width() / 7), 0)
    else:
        finish = begin + QPoint(round((widget.width() - widget.gutter) / len(widget.days)), 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=begin)
    QTest.mouseMove(widget, finish)
    previews = [replace(source, id='preview-one', start=source.start + timedelta(days=1), end=source.end + timedelta(days=1)),
                replace(source, id='preview-two', start=source.start + timedelta(days=3), end=source.end + timedelta(days=3))]
    widget.set_preview(previews)
    preview_requests = QSignalSpy(widget.drag_preview_requested)
    plain = widget.grab().toImage()
    assert widget.copy_badge_rect() is None
    widget.set_drag_modifiers(CTRL)
    badge = widget.copy_badge_rect()
    assert badge is not None
    copying = widget.grab().toImage()
    assert copying.copy(badge.toRect()) != plain.copy(badge.toRect())
    assert widget.preview_events == previews and preview_requests.count() == 0
    widget.set_drag_modifiers(NONE)
    assert widget.copy_badge_rect() is None
    assert widget.grab().toImage() == plain
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=finish)
    assert widget.copy_badge_rect() is None
