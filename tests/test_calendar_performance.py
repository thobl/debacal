"""Bound the work repeated by painting; verify cache changes cannot stale hits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtWidgets import QApplication

from debacal import calendar_widgets
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, MonthCanvas, bucket_days
from debacal.layout import layout_events
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import overlaps

ZONE = ZoneInfo('Europe/Berlin')
FIRST = datetime(2026, 10, 19, tzinfo=ZONE)
DAYS = [(FIRST + timedelta(days=index)).date() for index in range(7)]
CALENDAR = Calendar('local', 'Synthetic', '#5f8a67')


def event(index=0, *, day=0, all_day=False, candidate=False):
    start = FIRST + timedelta(days=day, hours=0 if all_day else 9, minutes=0 if all_day else index % 8 * 15)
    return Event(str(index), 'local', f'Synthetic {index}', start,
                 start + (timedelta(days=3) if all_day else timedelta(minutes=45)),
                 all_day=all_day, candidate=candidate)


@pytest.fixture
def surfaces():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch.object(calendar_widgets, 'local_zone', return_value=ZONE), \
         patch.object(calendar_widgets, 'now', return_value=FIRST.replace(hour=12)), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def make(kind='week', events=None):
            events = events if events is not None else [event(index, day=index % 7, candidate=index % 2 == 0)
                                                        for index in range(150)]
            widget = {'week': CalendarCanvas, 'month': MonthCanvas, 'all-day': AllDayCanvas}[kind]()
            widgets.append(widget)
            if kind == 'month':
                widget.set_data(DAYS[0], FIRST.month, events, [CALENDAR])
            else:
                widget.set_data(DAYS, events, [CALENDAR])
            if kind == 'week':
                widget.fixed_all_day = True
                widget.fit_to_viewport(600)
            widget.resize(1050, 700 if kind == 'month' else 600)
            widget.show()
            app.processEvents()
            return widget
        yield make
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('date_events', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_day_index_matches_half_open_membership_across_dst_and_midnight(date_events, reverse):
    first = FIRST + timedelta(days=5)
    days = [(first + timedelta(days=index)).date() for index in range(5)]
    if reverse:
        days.reverse()
    events = [event(0, day=4, all_day=True)]
    for index in range(120):
        start = first + timedelta(minutes=index * 79 - 400)
        if index % 9 == 0:
            start = start.replace(hour=0, minute=0)
        end = start + timedelta(minutes=0 if index % 3 == 0 else 210)
        events.append(Event(f'timed-{index}', 'local', 'Synthetic', start, end))
    bounds, indexed = bucket_days(events, days, ZONE, date_events)
    expected = [[item for item in events
                 if (item.start.date() <= day < item.end.date() if date_events and item.all_day
                     else overlaps(item, begin, end))]
                for day, (begin, end) in zip(days, bounds)]
    assert indexed == expected


def test_layout_clips_without_changing_models_and_preserves_point_boundaries():
    begin, end = FIRST + timedelta(hours=9), FIRST + timedelta(hours=12)
    events = [event(0), event(1), event(2)]
    events += [replace(event(3), start=begin - timedelta(hours=2), end=begin + timedelta(minutes=30)),
               replace(event(4), start=end, end=end),
               replace(event(5), start=begin, end=begin)]
    before = [item.to_dict() for item in events]
    clipped = [SimpleNamespace(id=item.id, start=max(item.start, begin), end=min(item.end, end))
               for item in events if overlaps(item, begin, end)]
    assert layout_events(events, window_start=begin, window_end=end) == layout_events(clipped)
    assert [item.to_dict() for item in events] == before
    assert '4' not in {placement.event_id for placement in layout_events(events, window_start=begin, window_end=end)}


def test_layout_reads_each_timestamp_once_and_rejects_bad_input_before_clipping():
    reads = []
    class Stamp:
        def __init__(self, value):
            self.value = value
        def timestamp(self):
            reads.append(self.value)
            return self.value
    intervals = [SimpleNamespace(id=str(index), start=Stamp(index), end=Stamp(index + 2)) for index in range(250)]
    layout_events(intervals, window_start=Stamp(3), window_end=Stamp(100))
    assert len(reads) == 502
    with pytest.raises(ValueError, match='before'):
        layout_events([SimpleNamespace(id='bad', start=Stamp(-5), end=Stamp(-8))],
                      window_start=Stamp(0), window_end=Stamp(10))


def test_confirmation_lookup_never_rescans_events(surfaces):
    class CountingList(list):
        scans = 0
        def __iter__(self):
            self.scans += 1
            return super().__iter__()
    events = CountingList(event(index, day=index % 7, candidate=True) for index in range(1000))
    widget = surfaces(events=events)
    widget.set_selection({str(index) for index in range(1000)})
    events.scans = 0
    for index in range(1000):
        assert widget.confirmation_event(str(index)).id == str(index)
    assert events.scans == 0
    widget.set_data(DAYS, [replace(events[0], read_only=True)], [CALENDAR])
    assert widget.confirmation_event('0') is None
    widget.set_data(DAYS, [events[0]], [replace(CALENDAR, read_only=True)])
    assert widget.confirmation_event('0') is None


def test_week_reuses_columns_and_bounds_across_repaint_selection_and_hover(surfaces):
    widget = surfaces()
    widget.invalidate_geometry()
    widget._bucket_cache.clear()
    with patch.object(calendar_widgets, 'layout_events', wraps=layout_events) as columns, \
         patch.object(calendar_widgets, 'bucket_days', wraps=bucket_days) as buckets:
        widget.grab()
        first = columns.call_count, buckets.call_count
        assert first[0] == 14  # Seven actual days plus seven empty preview days.
        widget.set_selection({'149'})
        widget.grab()
        widget.hit(QPointF(widget.gutter + 10, 100))
        widget.grab()
        assert (columns.call_count, buckets.call_count) == first
        widget.set_preview([event(500)])
        widget.grab()
        assert columns.call_count == first[0] + 7
        assert buckets.call_count == first[1] + 1
        widget.resize(widget.width() + 100, widget.height())
        widget.rects()
        assert columns.call_count >= first[0] + 14


def test_month_reuses_layout_and_pins_new_selection_without_reindexing_days(surfaces):
    widget = surfaces('month')
    widget.invalidate_geometry()
    widget._bucket_cache.clear()
    with patch.object(calendar_widgets, 'week_spans', wraps=calendar_widgets.week_spans) as lanes, \
         patch.object(calendar_widgets, 'bucket_days', wraps=bucket_days) as buckets:
        widget.grab()
        first = lanes.call_count, buckets.call_count
        assert first == (12, 2)
        widget.grab()
        widget.hit(QPointF(30, 65))
        assert (lanes.call_count, buckets.call_count) == first
        widget.set_selection({'147'})  # A hidden event on the first day.
        widget.grab()
        assert '147' in {item.id for _rect, item in widget.hits}
        assert lanes.call_count == 24 and buckets.call_count == 2
        for index in range(30):
            widget.set_preview([event(1000 + index, day=index % 30)])
            widget.grab()
        assert len(widget._bucket_cache) <= 3
        assert len(widget._shape_cache) == 1


@pytest.mark.parametrize('kind', ['week', 'month', 'all-day'])
def test_warm_cache_pixels_match_recomputed_pixels_with_selection_and_preview(surfaces, kind):
    events = [event(index, day=index % 7, all_day=kind == 'all-day' or index % 7 == 0,
                    candidate=index % 2 == 0) for index in range(60)]
    widget = surfaces(kind, events)
    widget.set_selection({'0', '2', '4', '7'})
    widget.set_preview([replace(events[-1], id='preview', candidate=True)])
    if kind == 'week':
        widget.set_navigation(True, 0, '1')
    before = widget.grab().toImage()
    hits = [(rect.getRect(), item.id) for rect, item, *_rest in widget.hits]
    widget.invalidate_geometry()
    widget._bucket_cache.clear()
    assert widget.grab().toImage() == before
    assert [(rect.getRect(), item.id) for rect, item, *_rest in widget.hits] == hits
    widget.set_preferences(Preferences(font_scale=125, time_format='12h'))
    updated = widget.grab().toImage()
    assert updated != before
    widget.invalidate_geometry()
    assert widget.grab().toImage() == updated


@pytest.mark.parametrize('kind', ['week', 'all-day'])
def test_returned_rectangles_and_preview_painting_cannot_mutate_cached_geometry(surfaces, kind):
    events = [event(index, day=index % 3, all_day=kind == 'all-day') for index in range(5)]
    widget = surfaces(kind, events)
    before = widget.rects()[0][0].getRect()
    returned = widget.rects()
    returned[0][0].moveLeft(100000)
    assert widget.rects()[0][0].getRect() == before
    widget.set_preview(events)
    widget.grab()
    assert widget.rects()[0][0].getRect() == before
    moved = replace(events[0], start=events[0].start + timedelta(days=6), end=events[0].end + timedelta(days=6))
    widget.set_data(DAYS, [moved], [CALENDAR])
    assert widget.hits == []
    old_center = QPointF(before[0] + before[2] / 2, before[1] + before[3] / 2)
    assert widget.hit(old_center) is None


def test_dense_columns_skip_only_invisible_text_and_keep_every_event_hit(surfaces):
    events = [replace(event(index), start=FIRST + timedelta(hours=9), end=FIRST + timedelta(hours=10))
              for index in range(500)]
    widget = surfaces(events=events)
    with patch.object(calendar_widgets, 'text', wraps=calendar_widgets.text) as drawing:
        widget.grab()
    assert len(widget.hits) == len(events)
    assert not [call for call in drawing.call_args_list if str(call.args[2]).startswith('Synthetic')]
    assert all(rect.width() <= 12 for rect, _item, _number in widget.hits)
