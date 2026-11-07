"""Point events keep their instant while remaining visible in every view."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.calendar_widgets import CalendarCanvas, MonthCanvas
from debacal.dialogs import EventDialog
from debacal.layout import layout_events
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_series, overlaps
from debacal.series_preview import SeriesPreview
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
DAY = date(2026, 11, 4)


def point(key='point', hour=0, minute=0, **kwargs):
    start = datetime(2026, 11, 4, hour, minute, tzinfo=ZONE)
    return Event(key, 'local', 'A point in time', start, start, **kwargs)


class PointSemanticsTests(unittest.TestCase):
    def test_zero_timed_event_is_preserved_but_backwards_and_zero_all_day_rejected(self):
        event = point()
        self.assertEqual(event.start, event.end)
        self.assertEqual(Event.from_dict(event.to_dict()), event)
        with self.assertRaises(ValueError):
            replace(event, end=event.start - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            replace(event, all_day=True)

    def test_point_uses_inclusive_lower_and_exclusive_upper_range(self):
        event = point()
        self.assertTrue(overlaps(event, event.start, event.start + timedelta(days=1)))
        self.assertFalse(overlaps(event, event.start - timedelta(days=1), event.start))
        self.assertFalse(overlaps(event, event.start + timedelta(seconds=1), event.start + timedelta(days=1)))
        ordinary = replace(event, end=event.start + timedelta(hours=1))
        self.assertFalse(overlaps(ordinary, ordinary.end, ordinary.end + timedelta(days=1)))

    def test_recurring_points_expand_at_range_boundary_without_inventing_duration(self):
        master = point(recurrence_rule='FREQ=DAILY;COUNT=3')
        events = expand_series(master, master.start, master.start + timedelta(days=2))
        self.assertEqual(len(events), 2)
        self.assertEqual([event.start.date() for event in events], [DAY, DAY + timedelta(days=1)])
        self.assertTrue(all(event.start.timestamp() == event.end.timestamp() for event in events))

    def test_points_get_visual_overlap_columns_without_data_changes(self):
        original = point(hour=10)
        nearby = replace(point('nearby', hour=10, minute=5),
                         end=datetime(2026, 11, 4, 10, 20, tzinfo=ZONE))
        separate = point('separate', hour=10, minute=30)
        placements = {value.event_id: value for value in layout_events([original, nearby, separate])}
        self.assertEqual(placements[original.id].columns, 2)
        self.assertNotEqual(placements[original.id].column, placements[nearby.id].column)
        self.assertEqual(placements[separate.id].columns, 1)
        self.assertEqual(original.start, original.end)
        simultaneous = [replace(original, id=str(index)) for index in range(3)]
        self.assertEqual({value.column for value in layout_events(simultaneous)}, {0, 1, 2})
        with self.assertRaises(ValueError):
            layout_events([SimpleNamespace(id='bad', start=original.start, end=original.start - timedelta(minutes=1))])


class PointViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []
        self.callback_errors = []
        self.exception_patch = patch('sys.excepthook', side_effect=lambda _, value, __: self.callback_errors.append(value))
        self.exception_patch.start()
        self.canvas_zone = patch('debacal.calendar_widgets.local_zone', return_value=ZONE)
        self.preview_zone = patch('debacal.series_preview.local_zone', return_value=ZONE)
        self.dialog_zone = patch('debacal.dialogs.local_zone', return_value=ZONE)
        self.canvas_zone.start()
        self.preview_zone.start()
        self.dialog_zone.start()
        self.calendars = [Calendar('local', 'Personal', '#4f7865')]

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        self.app.processEvents()
        self.exception_patch.stop()
        self.canvas_zone.stop()
        self.preview_zone.stop()
        self.dialog_zone.stop()
        self.assertEqual(self.callback_errors, [])

    def canvas(self, days, events):
        widget = CalendarCanvas()
        self.widgets.append(widget)
        widget.resize(1042, 1580)
        widget.set_data(days, events, self.calendars)
        widget.show()
        self.app.processEvents()
        return widget

    def test_midnight_and_late_points_render_in_day_and_week(self):
        events = [point(), point('late', hour=23, minute=59)]
        for days in ([DAY], [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]):
            with self.subTest(days=len(days)):
                widget = self.canvas(days, events)
                self.assertFalse(widget.grab().isNull())
                self.assertEqual({event.id for _, event in widget.hits}, {'point', 'late'})
                for rect, _ in widget.hits:
                    self.assertGreaterEqual(rect.height(), 18)
                    self.assertLessEqual(rect.bottom(), widget.height())

    def test_month_and_visual_editor_preview_include_midnight_point(self):
        event = point()
        month = MonthCanvas()
        self.widgets.append(month)
        month.resize(1050, 700)
        month.set_data(date(2026, 10, 26), 11, [event], self.calendars)
        self.assertFalse(month.grab().isNull())
        self.assertEqual([item.id for _, item in month.hits], ['point'])
        preview = SeriesPreview()
        self.widgets.append(preview)
        preview.resize(560, 370)
        preview.set_events([event], Preferences(0, 'iso', '24h'))
        self.assertFalse(preview.grab().isNull())
        self.assertEqual(preview.events_on(DAY), [event])
        self.assertEqual(preview.total_occurrences, 1)

    def test_dragging_point_preserves_zero_duration(self):
        original = point(hour=10)
        days = [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]
        canvas = self.canvas(days, [original])
        rect = canvas.hits[0][0]
        start = QPoint(int(rect.center().x()), int(rect.top()) + 3)
        width = (canvas.width() - canvas.gutter) / len(days)
        end = QPoint(start.x() + int(width), start.y() + canvas.hour_height)
        moved = QSignalSpy(canvas.move_requested)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(moved.count(), 1)
        key, moved_start, moved_end = moved.at(0)
        self.assertEqual(key, original.id)
        self.assertEqual(moved_start, datetime(2026, 11, 5, 11, tzinfo=ZONE))
        self.assertEqual(moved_start.timestamp(), moved_end.timestamp())

    def test_editing_point_title_does_not_create_duration(self):
        event = point(hour=10)
        dialog = EventDialog(self.calendars, event.start, event.end, event=event)
        self.widgets.append(dialog)
        dialog.title.setText('Renamed point')
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
        self.assertEqual(dialog.result_events[0].start, event.start)
        self.assertEqual(dialog.result_events[0].end, event.end)

    def test_workspace_day_view_and_arrow_selection_include_midnight_point(self):
        with Store(':memory:') as store:
            event = point()
            store.apply('Create point', [event])
            window = MainWindow(store, enable_sync=False)
            self.widgets.append(window)
            window.go_date(DAY)
            window.set_view('day')
            self.assertEqual([item for _rect, item in window.canvas.rects()], [event])
            window.select_by_arrow('down')
            self.assertEqual(window.selected_id, event.id)
            window.close()
            self.widgets.remove(window)


if __name__ == '__main__':
    unittest.main()
