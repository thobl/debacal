"""Selection details never resize the calendar's viewport or time axis."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with patch("debacal.ui.local_zone", return_value=ZONE), \
            patch("debacal.calendar_widgets.local_zone", return_value=ZONE), \
            patch("sys.excepthook", side_effect=lambda _, error, __: errors.append(error)):
        store = Store(":memory:")
        window = MainWindow(store, enable_sync=False)
        window.go_date(START.date())
        a = Event("short", "local", "Short title", START, START + timedelta(hours=1))
        b = Event("long", "local", "A long description of the selected event " * 40,
                  START + timedelta(hours=3), START + timedelta(hours=4), candidate=True)
        store.apply("Synthetic events", [a, b])
        window.refresh()
        window.show()
        settle(app)
        try:
            yield window, store, app, a, b
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            store.close()
            assert errors == []


def geometry(window):
    viewport, canvas = window.scroll.viewport(), window.canvas
    return (viewport.width(), viewport.height(), canvas.width(), canvas.height(),
            canvas.start_hour, canvas.end_hour, canvas.hour_height,
            window.scroll.verticalScrollBar().isVisible(), window.scroll.verticalScrollBar().maximum(),
            window.scroll.horizontalScrollBar().isVisible(), window.details.height())


@pytest.mark.parametrize("size", [(1400, 900), (1030, 690)])
def test_select_clear_and_long_candidate_switch_keep_identical_calendar_geometry(workspace, size):
    window, _, app, short, long = workspace
    window.resize(*size)
    settle(app)
    baseline = geometry(window)
    assert not window.details.isVisible()
    assert not window.scroll.verticalScrollBar().isVisible()
    assert not window.scroll.horizontalScrollBar().isVisible()
    for event in (short, long, short, long):
        window.select_event(event.id)
        settle(app)
        assert window.details.isVisible()
        assert event.title in window.detail_text.text()
        assert geometry(window) == baseline
    window.select_day(START.date())
    settle(app)
    assert not window.details.isVisible()
    assert geometry(window) == baseline


def test_small_full_day_view_keeps_necessary_scrollbar_without_selection_oscillation(workspace):
    window, _, app, short, long = workspace
    window.resize(window.minimumWidth(), window.minimumHeight())
    window.full_day = True
    window.fit_calendar()
    settle(app)
    baseline = geometry(window)
    assert window.scroll.verticalScrollBar().isVisible()
    assert window.scroll.verticalScrollBar().maximum() > 0
    assert not window.scroll.horizontalScrollBar().isVisible()
    for event in (short, long):
        window.select_event(event.id)
        for _ in range(6):
            app.processEvents()
            assert geometry(window) == baseline
    window.select_day(START.date())
    settle(app)
    assert geometry(window) == baseline


def test_adaptive_eleven_hour_fit_does_not_create_a_one_pixel_scrollbar(workspace):
    window, store, app, short, _ = workspace
    start = START.replace(hour=7)
    early = Event("early", "local", "Early event", start, start + timedelta(hours=1))
    store.apply("Early event", [early])
    window.refresh()
    settle(app)
    # An eleven-hour range has non-terminating scale values at these heights.
    # Its contents still fit and should not reserve a scrollbar for roundoff.
    target_height = 387
    window.setMinimumHeight(1)
    window.resize(window.width(), window.height() + target_height - window.scroll.viewport().height())
    settle(app)
    assert window.scroll.viewport().height() == target_height
    assert (window.canvas.start_hour, window.canvas.end_hour) == (7, 18)
    assert window.canvas.minimumHeight() <= target_height
    assert not window.scroll.verticalScrollBar().isVisible()
    baseline = geometry(window)
    window.select_event(short.id)
    settle(app)
    assert geometry(window) == baseline
