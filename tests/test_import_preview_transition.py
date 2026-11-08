"""Uncached import pages replace complete frames without blanking or reflow."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta, timezone
from threading import Event as ThreadEvent
from unittest.mock import patch

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.ics import export_ics
from debacal.import_preparation import PreparedImport
from debacal.import_sources import ImportSource
from debacal.models import Event
from debacal.preview_navigation import PreviewNavigation
from test_import_dialog import CALENDARS, dialogs, wait_navigation


class _PaintFrames(QObject):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.frames = []
        self.canvas = canvas
        canvas.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.frames.append((self.canvas.month, tuple(self.canvas.events), self.canvas.geometry()))
        return False


def test_uncached_month_navigation_keeps_the_last_frame_until_replacement_is_ready(dialogs):
    start = datetime(2026, 11, 30, 9, tzinfo=timezone.utc)
    first = Event('first', 'source', 'First meeting', start, start + timedelta(hours=1))
    last = Event('last', 'source', 'Last meeting', start + timedelta(days=1), start + timedelta(days=1, hours=1))
    entered, release = ThreadEvent(), ThreadEvent()
    original = PreparedImport.month_page

    def delayed(prepared, month, preferences, **kwargs):
        if month == date(2026, 12, 1):
            entered.set()
            assert release.wait(timeout=3)
        return original(prepared, month, preferences, **kwargs)

    with patch('debacal.import_preparation.now', return_value=start - timedelta(days=1)), \
         patch.object(PreparedImport, 'month_page', delayed):
        widget = dialogs(CALENDARS, sources=[ImportSource.from_data(export_ics([first, last]))])
        wait_navigation(widget)
        canvas = widget.preview.canvas
        frames = _PaintFrames(canvas)
        previous = tuple(canvas.events)
        geometry = canvas.geometry()
        selected = widget.selection.snapshot(masters=widget.prepared_import.events)
        try:
            QTest.mouseClick(widget.preview.next_occurrence_button, Qt.MouseButton.LeftButton)
            timer = QElapsedTimer()
            timer.start()
            while not entered.is_set() and timer.elapsed() < 2000:
                QApplication.processEvents()
                QTest.qWait(5)
            assert entered.is_set()
            QApplication.processEvents()
            assert widget.preview.current_month == date(2026, 11, 1)
            assert tuple(canvas.events) == previous
            assert canvas.geometry() == geometry
            assert not widget.preview.message.isVisible()
            assert not canvas.isEnabled()
            canvas.grab()
        finally:
            release.set()
        wait_navigation(widget)
        assert widget.preview.current_month == date(2026, 12, 1)
        assert widget.preview.focused_event.start == last.start
        assert canvas.geometry() == geometry
        assert frames.frames and all(events for _month, events, _rect in frames.frames)
        assert widget.selection.snapshot(masters=widget.prepared_import.events) == selected


def test_large_plain_import_navigation_uses_its_index_for_each_arrow():
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    events = [Event(str(index), 'source', 'Meeting', start + timedelta(minutes=index),
                    start + timedelta(minutes=index + 30)) for index in range(50000)]
    navigation = PreviewNavigation(events)

    class _NoScan(list):
        def __iter__(self):
            raise AssertionError('A navigation arrow must use the prepared ordinary-event index')

    navigation.masters = _NoScan(navigation.masters)
    current = events[25000]
    for index in range(25001, 25041):
        current = navigation.adjacent(current, 1)
        assert current is events[index]
        assert navigation.position(current) == (index + 1, len(events))
    assert navigation.adjacent(current, -1) is events[25039]
    assert navigation.last() is events[-1]


def test_month_requests_accumulate_while_the_previous_page_is_still_visible(dialogs):
    widget = dialogs(CALENDARS)
    preview = widget.preview
    original_month = date(2026, 11, 1)
    preview.set_events(original_month, (), ())
    requests = []
    preview.month_requested.connect(requests.append)
    preview.navigate_month(1)
    preview.navigate_month(1)
    assert requests == [date(2026, 12, 1), date(2027, 1, 1)]
    assert preview.current_month == original_month
    preview.set_events(date(2027, 1, 1), (), ())
    preview.navigate_month(-1)
    assert requests[-1] == date(2026, 12, 1)
