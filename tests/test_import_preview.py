"""Import previews share calendar rendering while keeping imports immutable."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal.import_preview import ImportPreview
from debacal.models import Calendar, Event
from debacal.preferences import Preferences

ZONE = ZoneInfo('Europe/Berlin')
MONTH = date(2026, 11, 1)
CALENDAR = Calendar('destination', 'Work', '#b56934')


@pytest.fixture
def preview():
    app = QApplication.instance() or QApplication([])
    errors = []
    widget = ImportPreview(Preferences(start_weekday=0, locale_name='en_GB', time_format='24h'))
    widget.resize(980, 600)
    widget.show()
    with patch('sys.excepthook', side_effect=lambda _type, error, _traceback: errors.append(error)):
        app.processEvents()
        yield widget
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def event(*, all_day=False, candidate=False):
    start = datetime(2026, 11, 2, 0 if all_day else 9, tzinfo=ZONE)
    return Event('source', CALENDAR.id, 'Import review <details>', start,
                 start + (timedelta(days=3) if all_day else timedelta(hours=1)),
                 all_day=all_day, candidate=candidate, location='Room & garden', description='Original notes')


@pytest.mark.parametrize('all_day', [False, True])
def test_preview_renders_real_event_spans_and_details_without_editing(preview, all_day):
    source = event(all_day=all_day, candidate=True)
    original = source.to_dict()
    with patch('debacal.recurrence.expand_events', side_effect=AssertionError('Expansion belongs to the worker')):
        preview.set_events(MONTH, [source], CALENDAR)
        assert not preview.grab().isNull()
    canvas = preview.canvas
    bars = [(rect, item) for rect, item in canvas.hits if item.id == source.id]
    assert len(bars) == 1
    rect = bars[0][0]
    width = (canvas.width() - canvas.week_gutter) / 7
    assert rect.width() == pytest.approx((3 if all_day else 1) * width - 10)
    signals = [QSignalSpy(getattr(canvas, name)) for name in (
        'create_requested', 'create_all_day_requested', 'edit_scoped_requested',
        'move_scoped_requested', 'confirm_requested', 'context_requested')]
    point = rect.center().toPoint()
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=point)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(canvas, point + QPoint(round(width), 0))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=point + QPoint(round(width), 0))
    assert not canvas.drag and not canvas.ghost
    assert all(signal.count() == 0 for signal in signals)
    assert not canvas.confirm_hits
    assert source.to_dict() == original
    assert canvas.selected_ids == {source.id}
    assert '&lt;details&gt;' in preview.details.text() and 'Original notes' in preview.details.text()
    assert not any(command[0] == 'text' and ('all day' in command[2] or command[2] == '+')
                   for command in canvas.month_layout()[0])


def test_preview_navigation_requests_only_visible_month_and_handles_empty_pages(preview):
    preview.set_events(MONTH, [event()], [CALENDAR])
    preview.set_first_date(date(2026, 11, 2))
    requested = QSignalSpy(preview.month_requested)
    QTest.mouseClick(preview.next_button, Qt.MouseButton.LeftButton)
    assert requested.at(0) == [date(2026, 12, 1)]
    assert preview.current_month == date(2026, 12, 1) and preview.canvas.events == []
    assert preview.month_label.text() == 'December 2026'
    preview.set_loading(True)
    assert not preview.canvas.isEnabled() and preview.previous_button.isEnabled()
    QTest.mouseClick(preview.first_button, Qt.MouseButton.LeftButton)
    assert requested.at(1) == [MONTH]
    preview.set_events(MONTH, (), CALENDAR)
    preview.set_loading(False)
    preview.set_message('No events in this month.')
    assert preview.canvas.isEnabled() and preview.message.text() == 'No events in this month.'


def test_readonly_tooltips_show_event_details_and_week_numbers_have_no_navigation_hint(preview):
    source = event()
    preview.set_events(MONTH, [source], CALENDAR)
    preview.grab()
    canvas = preview.canvas
    rect = next(rect for rect, item in canvas.hits if item.id == source.id)
    QTest.mouseMove(canvas, QPoint(1, 1))
    QTest.mouseMove(canvas, rect.center().toPoint())
    assert 'Original notes' in canvas.toolTip() and 'Room &amp; garden' in canvas.toolTip()
    assert 'Double-click' not in canvas.toolTip() and 'Drag' not in canvas.toolTip()
    week = canvas.week_rect(0)
    QTest.mouseMove(canvas, week.center().toPoint())
    assert canvas.toolTip().startswith('Week ') and 'Open' not in canvas.toolTip()


def test_preview_language_and_week_order_follow_preferences(preview):
    preview.set_preferences(Preferences(start_weekday=6, locale_name='de_DE', show_week_numbers=True, font_scale=140))
    preview.set_events(MONTH, [event()], CALENDAR)
    assert preview.canvas.start.weekday() == 6
    assert preview.month_label.text() == 'November 2026'
    assert preview.canvas.minimumHeight() == round(280 * 1.4)
    assert not preview.grab().isNull()
