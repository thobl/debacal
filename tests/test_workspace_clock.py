"""Workspace sizing and date-dependent surfaces honor their real environment."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.store import Store
from debacal.models import Calendar, Event
from debacal.recurrence import RecurrenceRangeError
from debacal.preferences import Preferences
from debacal.theme import scheme_for
from debacal.ui import MainWindow
from debacal.calendar_widgets import minimum_week_width


NOW = datetime(2026, 11, 4, 23, 59, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    windows, errors, current = [], [], [NOW]
    with Store(':memory:') as store, \
         patch('debacal.ui.now', side_effect=lambda: current[0]), \
         patch('debacal.ui.local_zone', return_value=NOW.tzinfo), \
         patch('debacal.calendar_widgets.local_zone', return_value=NOW.tzinfo), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        yield app, store, windows, current
        for window in windows:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize(('available', 'expected'), [((1920, 1080), (1400, 900)),
                                                   ((1280, 800), (1280, 800)),
                                                   ((1100, 720), (1100, 720)),
                                                   ((800, 600), (800, 600))])
def test_initial_workspace_size_respects_available_screen_and_scrollable_content_minimum(desktop, available, expected):
    _, store, windows, _ = desktop
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, *available))
    with patch.object(MainWindow, 'screen', return_value=screen):
        window = MainWindow(store, enable_sync=False)
    windows.append(window)
    assert (window.width(), window.height()) == expected
    # Localized headers and desktop fonts can require a little more room.
    assert window.workspace_content.minimumWidth() >= 1030
    assert window.workspace_content.minimumHeight() == 690


@pytest.mark.parametrize('scale', [100, 140])
@pytest.mark.parametrize('appearance', ['light', 'dark'])
def test_compact_workspace_scrolls_to_all_controls_and_returns_to_normal_without_extra_bars(desktop, scale, appearance):
    app, store, windows, _ = desktop
    Preferences(font_scale=scale, appearance_mode=appearance).save(store)
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 800, 600))
    with patch.object(MainWindow, 'screen', return_value=screen):
        window = MainWindow(store, enable_sync=False)
    windows.append(window)
    window.show()
    window.activateWindow()
    for _ in range(6):
        app.processEvents()
    scroll = window.workspace_scroll
    assert (window.width(), window.height()) == (800, 600)
    assert window.workspace_content.width() >= 1030 and window.workspace_content.height() >= 690
    assert scroll.horizontalScrollBar().maximum() > 0
    assert scroll.verticalScrollBar().maximum() > 0
    # Large text must fit weekday names and TODAY without changing column
    # positions as the user switches between month and week.
    widths = []
    for view in ('week', 'month', 'year', 'week'):
        window.set_view(view)
        for _ in range(6):
            app.processEvents()
        widths.append(window.workspace_content.width())
        if view in ('week', 'month'):
            surface = window.header if view == 'week' else window.month
            assert surface.width() >= minimum_week_width(window.preferences)
    assert len(set(widths)) == 1
    assert window.header.width() == window.month.width()

    def visible(control):
        rect = QRect(control.mapTo(scroll.viewport(), QPoint()), control.size())
        return scroll.viewport().rect().contains(rect)

    for control in (window.view_buttons['year'], window.status, window.preferences_button):
        scroll.ensureWidgetVisible(control)
        app.processEvents()
        assert visible(control)
    window.canvas.setFocus()
    scroll.horizontalScrollBar().setValue(scroll.horizontalScrollBar().maximum())
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    before_focus = scroll.horizontalScrollBar().value(), scroll.verticalScrollBar().value()
    app.processEvents()
    assert (scroll.horizontalScrollBar().value(), scroll.verticalScrollBar().value()) == before_focus
    QTest.keyClick(window.canvas, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    assert window.search.hasFocus() and visible(window.search)
    window.canvas.setFocus()
    QTest.keyClick(window.canvas, Qt.Key.Key_D)
    assert window.view == 'day'
    window.resize(1400, 900)
    for _ in range(6):
        app.processEvents()
    assert scroll.horizontalScrollBar().maximum() == scroll.verticalScrollBar().maximum() == 0
    assert not scroll.horizontalScrollBar().isVisible() and not scroll.verticalScrollBar().isVisible()
    assert window.workspace_content.size() == scroll.viewport().size() == window.size()
    assert window.grab().toImage().pixelColor(310, 10).name() == scheme_for(window.preferences).background


def test_minute_timer_repaints_every_today_marker_at_midnight_without_navigating_or_requerying_view(desktop):
    _, store, windows, current = desktop
    window = MainWindow(store, enable_sync=False)
    windows.append(window)
    anchor, selected = window.anchor, window.selected_day
    with patch.object(window.canvas, 'update') as timed, \
         patch.object(window.year, 'update') as year, \
         patch.object(window.header, 'update') as header, \
         patch.object(window.month, 'update') as month, \
         patch.object(window.mini.calendar, 'updateCells') as mini, \
         patch.object(window, 'refresh_agenda_clock') as agenda, \
         patch.object(window, 'query_view_events', side_effect=AssertionError('No view requery')):
        window.clock_timer.timeout.emit()
        assert timed.call_count == year.call_count == 1
        assert header.call_count == month.call_count == mini.call_count == 0
        current[0] += timedelta(minutes=2)
        window.clock_timer.timeout.emit()
        assert timed.call_count == year.call_count == 2
        assert header.call_count == month.call_count == mini.call_count == 1
        assert agenda.call_args.args == (current[0],)
        window.clock_timer.timeout.emit()
        assert header.call_count == month.call_count == mini.call_count == 1
    assert window.anchor == anchor and window.selected_day == selected
    assert window._clock_day == current[0].date()


def test_dense_series_keeps_healthy_events_visible_with_actionable_range_warning(desktop):
    _, store, windows, _ = desktop
    calendar = Calendar('dense-source', 'Dense source', '#587c67')
    store.save_calendar(calendar)
    first = NOW.replace(month=1, day=1, hour=0, minute=0)
    dense = Event('dense', calendar.id, 'Frequent appointments', first, first + timedelta(minutes=5),
                  recurrence_rule='FREQ=DAILY;BYHOUR=' + ','.join(str(hour) for hour in range(24))
                                  + ';BYMINUTE=0,15,30,45')
    healthy = Event('healthy', 'local', 'A healthy event', NOW.replace(hour=9, minute=0), NOW.replace(hour=10, minute=0))
    store.apply_remote([dense, healthy])
    window = MainWindow(store, enable_sync=False)
    windows.append(window)
    window.select_event(healthy.id)
    window.set_view('year')
    assert [event.id for event in window.range_events] == [healthy.id]
    assert window.selected_id == healthy.id
    warning = next(issue for issue in window.status_issues() if issue.key == 'view:dense')
    assert dense.title in warning.title and calendar.name in warning.details
    assert 'Other events remain visible' in warning.details
    assert 'unavailable in this view' in window.status.text()
    assert not window.warning_button.isHidden()
    # Every visibility change queries the requested sources and current issues.
    with patch.object(store, 'query_occurrences', wraps=store.query_occurrences) as query:
        window.toggle_calendar(calendar.id, False)
        assert not window._view_issues and [event.id for event in window.range_events] == [healthy.id]
        window.toggle_calendar(calendar.id, True)
        assert len(window._view_issues) == 1
        assert query.call_count == 2
    warning.action()
    assert window.view == 'day' and not window._view_issues
    assert healthy.id in {event.id for event in window.range_events}
    assert any(event.series_id == dense.id for event in window.range_events)
    assert not any(issue.key.startswith('view:') for issue in window.status_issues())
    window.set_view('year')
    assert len(window._view_issues) == 1
    store.apply('Simplify a dense series', [replace(dense, recurrence_rule='FREQ=DAILY;COUNT=2')])
    window.refresh()
    assert not window._view_issues
    assert len(window.range_events) == 3
    assert store.list_quarantined_resources() == []


def test_agenda_budget_warning_follows_fresh_queries_and_agenda_visibility(desktop):
    _, store, windows, _ = desktop
    source = Event('source', 'local', 'A repeating event', NOW, NOW + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY')
    healthy = Event('healthy', 'local', 'Healthy appointment', NOW, NOW + timedelta(minutes=30))
    store.apply_remote([source, healthy])
    window = MainWindow(store, enable_sync=False)
    windows.append(window)
    window.results.invalidate()

    def limited_agenda(start, end, calendars, *, limit, issues):
        issues.append((source, RecurrenceRangeError('This recurrence needs a smaller date range.')))
        return [healthy]

    with patch.object(store, 'upcoming_occurrences', side_effect=limited_agenda) as query:
        window.refresh()
        assert window.results.agenda_mode
        assert window.results.count() == 1 and window.results.event_id(window.results.item(0)) == healthy.id
        issue = next(issue for issue in window.status_issues() if issue.key == 'view:source')
        assert 'upcoming events' in issue.details and issue.action_label == 'Edit event…'
        assert 'unavailable in upcoming events' in window.status.text()
        assert not window.warning_button.isHidden()
        window.refresh_agenda_clock(NOW)
        assert query.call_count == 2
        window.search.setText('healthy')
        assert not window.results.agenda_mode
        assert not any(issue.key.startswith('view:') for issue in window.status_issues())
        assert window.warning_button.isHidden()
        window.search.clear()
        assert window.results.agenda_mode and not window.warning_button.isHidden()
        assert query.call_count == 3
    assert store.list_quarantined_resources() == []
