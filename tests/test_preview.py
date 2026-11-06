"""The editor previews actual dates, and repainting cannot tint later cells."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal.calendar_widgets import MonthCanvas
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview, text


ZONE = ZoneInfo('Europe/Berlin')


class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []
        self.callback_errors = []
        self.exception_patch = patch('sys.excepthook', side_effect=lambda _, value, __: self.callback_errors.append(value))
        self.exception_patch.start()

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        self.app.processEvents()
        self.exception_patch.stop()
        self.assertEqual(self.callback_errors, [])

    def event(self, **kwargs):
        start = datetime(2026, 11, 4, 14, tzinfo=ZONE)
        return Event('master', 'local', 'Seminar', start, start + timedelta(hours=1), **kwargs)

    def preview(self, events=(), preferences=None):
        widget = SeriesPreview()
        self.widgets.append(widget)
        widget.resize(560, 370)
        widget.set_events(events, preferences or Preferences(0, 'iso', '24h'))
        widget.show()
        self.app.processEvents()
        return widget

    def test_weekly_series_expands_visible_month_and_navigation_crosses_year(self):
        preview = self.preview([self.event(recurrence_rule='FREQ=WEEKLY;COUNT=10')])
        self.assertEqual(preview.current_month, date(2026, 11, 1))
        self.assertEqual(preview.grid_start, date(2026, 10, 26))
        self.assertEqual(len(preview.visible_events), 5)  # Four November dates and 2 December.
        self.assertIn('4 occurrences this month', preview.summary.text())
        self.assertEqual(len(preview.events_on(date(2026, 11, 11))), 1)
        self.assertEqual(preview.events_on(date(2026, 11, 12)), [])
        QTest.mouseClick(preview.next_button, Qt.MouseButton.LeftButton)
        self.assertEqual(preview.current_month, date(2026, 12, 1))
        self.assertIn('5 occurrences this month', preview.summary.text())
        QTest.mouseClick(preview.next_button, Qt.MouseButton.LeftButton)
        self.assertEqual(preview.current_month, date(2027, 1, 1))
        self.assertIn('1 occurrence this month', preview.summary.text())

    def test_editor_rebuild_keeps_browsed_month_even_when_ids_and_start_change(self):
        master = self.event(recurrence_rule='FREQ=WEEKLY;COUNT=10')
        preview = self.preview([master])
        preview.navigate_month(1)
        changed = replace(master, id='a-new-preview-id', title='New title',
                          start=master.start + timedelta(days=90), end=master.end + timedelta(days=90))
        preview.set_events([changed])
        self.assertEqual(preview.current_month, date(2026, 12, 1))
        preview.set_events([])
        self.assertEqual(preview.current_month, date(2026, 12, 1))
        preview.set_events([changed])
        preview.show_first_date()
        self.assertEqual(preview.current_month, changed.start.date().replace(day=1))

    def test_added_and_excluded_dates_are_visible_and_sources_unchanged(self):
        master = self.event(recurrence_rule='FREQ=DAILY;COUNT=4',
                            recurrence_dates=('2026-11-10T14:00:00+01:00',),
                            excluded_dates=('2026-11-05T14:00:00+01:00',))
        original = master.to_dict()
        preview = self.preview([master])
        self.assertIn(date(2026, 11, 4), preview.anchor_dates)
        self.assertIn(date(2026, 11, 10), preview.added_dates)
        self.assertIn(date(2026, 11, 5), preview.excluded_dates)
        self.assertEqual(preview.events_on(date(2026, 11, 5)), [])
        self.assertEqual(len(preview.events_on(date(2026, 11, 10))), 1)
        self.assertFalse(preview.grab().isNull())
        self.assertEqual(master.to_dict(), original)

    def test_explicit_schedule_shows_total_dates_beyond_visible_month(self):
        preview = self.preview([self.event(recurrence_dates=('2026-12-10T14:00:00+01:00',
                                                            '2027-01-10T14:00:00+01:00'))])
        self.assertEqual(preview.total_occurrences, 3)
        self.assertIn('1 of 3 occurrences this month', preview.summary.text())

    def test_click_requests_date_edit_only_when_enabled(self):
        preview = self.preview([self.event()])
        toggle = QSignalSpy(preview.date_toggled)
        clicked = QSignalSpy(preview.date_clicked)
        selected = date(2026, 11, 12)
        point = preview.grid.cell_rect(selected).center().toPoint()
        QTest.mouseClick(preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(clicked.count(), 1)
        self.assertEqual(toggle.count(), 0)
        preview.enable_date_editing(True)
        QTest.mouseClick(preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(toggle.count(), 1)
        self.assertEqual(toggle.at(0)[0], selected)
        self.assertEqual(preview.events_on(selected), [])  # Owner applies the change.

    def test_calendar_order_and_time_labels_follow_preferences(self):
        preferences = Preferences(6, 'iso', '12h')
        preview = self.preview([self.event()], preferences)
        self.assertEqual(preview.grid_start, date(2026, 11, 1))
        with patch('debacal.series_preview.text', wraps=text) as draw_text:
            preview.grid.grab()
            labels = [call.args[2] for call in draw_text.call_args_list]
            self.assertEqual(labels[:7], [preferences.weekday_name(day).upper() for day in [6, 0, 1, 2, 3, 4, 5]])
            self.assertIn(preferences.format_time(datetime(2026, 11, 4, 14)), labels)

    def test_candidate_slots_have_distinct_visual_style(self):
        preview = self.preview([self.event(candidate=True)])
        rect = preview.grid.cell_rect(date(2026, 11, 4))
        point = QPoint(int(rect.x() + 8), int(rect.y() + 27))
        candidate = preview.grid.grab().toImage().pixelColor(point)
        preview.set_events([self.event(candidate=False)])
        ordinary = preview.grid.grab().toImage().pixelColor(point)
        self.assertNotEqual(candidate, ordinary)

    def test_empty_and_invalid_series_still_render_useful_state(self):
        preview = self.preview()
        self.assertEqual(preview.visible_events, [])
        self.assertFalse(preview.first_button.isEnabled())
        self.assertFalse(preview.grab().isNull())
        preview.set_events([self.event(recurrence_rule='FREQ=INVALID')])
        self.assertTrue(preview.error)
        self.assertIn('Preview unavailable', preview.summary.text())
        self.assertEqual(preview.visible_events, [])

    def test_month_background_is_stable_when_calendars_are_toggled(self):
        canvas = MonthCanvas()
        self.widgets.append(canvas)
        canvas.resize(1050, 700)
        start = date(2026, 10, 26)
        calendars = [Calendar('local', 'Personal', '#d04462')]
        canvas.set_data(start, 11, [], calendars)
        canvas.show()
        self.app.processEvents()
        without_events = canvas.grab().toImage()
        canvas.set_data(start, 11, [self.event()], calendars)
        with_events = canvas.grab().toImage()
        canvas.set_data(start, 11, [], calendars)
        toggled_off = canvas.grab().toImage()
        width, height = canvas.width() / 7, (canvas.height() - 28) / 6
        # Blank portions of every cell, including all dates after the event,
        # must remain identical regardless of the event painter's fill brush.
        for offset in range(42):
            point = QPoint(int(offset % 7 * width + width - 12),
                           int(28 + offset // 7 * height + height - 12))
            with self.subTest(cell=offset):
                self.assertEqual(with_events.pixelColor(point), without_events.pixelColor(point))
                self.assertEqual(toggled_off.pixelColor(point), without_events.pixelColor(point))


if __name__ == '__main__':
    unittest.main()
