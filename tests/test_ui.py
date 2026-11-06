"""Offscreen Qt integration checks; no live display or network is required."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest

from debacal.dialogs import EventDialog
from debacal.calendar_widgets import CalendarCanvas, DayHeader, MonthCanvas, badge, text
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.timeutil import local_zone
from debacal.ui import MainWindow


ZONE = ZoneInfo("Europe/Berlin")


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = Store(":memory:")
        self.zone_patch = patch("debacal.dialogs.local_zone", return_value=ZONE)
        self.zone_patch.start()
        self.callback_errors = []
        self.exception_patch = patch("sys.excepthook", side_effect=lambda _, value, __: self.callback_errors.append(value))
        self.exception_patch.start()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
        self.app.processEvents()
        self.zone_patch.stop()
        self.store.close()
        self.exception_patch.stop()
        self.assertEqual(self.callback_errors, [])

    def dialog(self, **kwargs):
        start = kwargs.pop("start", datetime(2026, 11, 4, 10, tzinfo=ZONE))
        dialog = EventDialog(self.store.list_calendars(), start, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def test_pattern_preview_and_atomic_creation_undo(self):
        dialog = self.dialog()
        dialog.title.setText("Research seminar")
        dialog.pattern.setText("4*(+2) +3 3*+2")
        self.assertIsInstance(dialog.preview, SeriesPreview)
        self.assertEqual(dialog.repeat.currentData(), 'RDATE')
        self.assertEqual(len(dialog.preview.masters), 1)
        self.assertEqual([event.start.date() for event in dialog.preview.visible_events],
                         [date(2026, 11, 4), date(2026, 11, 18), date(2026, 12, 2)])
        self.assertEqual(dialog.error.text(), "")
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(len(dialog.result_events), 1)
        master = dialog.result_events[0]
        self.assertFalse(master.recurrence_rule)
        self.assertEqual(len(master.recurrence_dates), 8)
        self.assertIsNone(master.group_id)
        self.store.apply("Create seminar series", dialog.result_events)
        self.assertEqual(len(self.store.list_events()), 1)
        occurrences = self.store.query_occurrences(datetime(2026, 11, 4, tzinfo=ZONE),
                                                   datetime(2028, 11, 4, tzinfo=ZONE))
        self.assertEqual(len(occurrences), 9)
        self.assertTrue(all(event.start.hour == 10 for event in occurrences))
        self.assertTrue(all(event.series_id == master.id for event in occurrences))
        self.assertEqual(len(self.store.history()), 1)
        self.store.undo()
        self.assertEqual(self.store.list_events(), [])
        self.store.redo()
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(len(self.store.query_occurrences(datetime(2026, 11, 4, tzinfo=ZONE),
                                                         datetime(2028, 11, 4, tzinfo=ZONE))), 9)

    def test_candidate_pattern_uses_the_same_recurring_master_model(self):
        dialog = self.dialog()
        dialog.title.setText('Possible seminar slots')
        dialog.pattern.setText('4*(+2) +3 3*+2')
        dialog.candidate.setChecked(True)
        self.assertTrue(dialog.preview.masters[0].candidate)
        dialog.save()
        self.assertEqual(len(dialog.result_events), 1)
        master = dialog.result_events[0]
        self.assertTrue(master.candidate and master.recurring)
        self.assertIsNone(master.group_id)
        self.assertEqual(len(master.recurrence_dates), 8)
        self.store.apply('Create candidate slots', dialog.result_events)
        self.assertEqual(len(self.store.list_events()), 1)
        occurrences = self.store.query_occurrences(datetime(2026, 11, 1, tzinfo=ZONE), datetime(2028, 1, 1, tzinfo=ZONE))
        self.assertEqual(len(occurrences), 9)
        self.assertTrue(all(event.candidate and event.series_id == master.id for event in occurrences))
        self.store.undo()
        self.assertEqual(self.store.list_events(), [])

    def test_standard_weekly_editor_creates_one_rrule_master(self):
        dialog = self.dialog()
        dialog.title.setText('Weekly project review')
        dialog.repeat.setCurrentIndex(dialog.repeat.findData('WEEKLY'))
        dialog.interval.setValue(2)
        dialog.repeat_end.setCurrentIndex(dialog.repeat_end.findData('COUNT'))
        dialog.repeat_count.setValue(5)
        self.assertEqual(len(dialog.preview.visible_events), 3)
        self.assertTrue(dialog.preview.masters[0].recurrence_rule)
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(len(dialog.result_events), 1)
        master = dialog.result_events[0]
        self.assertEqual(master.recurrence_rule, 'FREQ=WEEKLY;INTERVAL=2;BYDAY=WE;COUNT=5')
        self.assertEqual(master.recurrence_dates, ())
        self.store.apply('Create weekly review', dialog.result_events)
        occurrences = self.store.query_occurrences(master.start, master.start + timedelta(days=100))
        self.assertEqual(len(occurrences), 5)
        self.assertTrue(all(event.start.hour == master.start.hour for event in occurrences))
        self.assertEqual(occurrences[-1].start.date() - occurrences[0].start.date(), timedelta(weeks=8))

    def test_regular_recurrence_can_also_be_candidate_alternatives(self):
        dialog = self.dialog()
        dialog.title.setText('Choose one review date')
        dialog.repeat.setCurrentIndex(dialog.repeat.findData('WEEKLY'))
        dialog.repeat_end.setCurrentIndex(dialog.repeat_end.findData('COUNT'))
        dialog.repeat_count.setValue(3)
        dialog.candidate.setChecked(True)
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
        master = dialog.result_events[0]
        self.assertTrue(master.candidate and master.recurrence_rule)
        self.store.apply('Create review alternatives', [master])
        self.assertEqual(len(self.store.query_occurrences(master.start, master.start + timedelta(days=30))), 3)

    def test_visual_date_clicks_edit_custom_schedule_across_months(self):
        dialog = self.dialog()
        dialog.title.setText('Irregular seminar')
        dialog.repeat.setCurrentIndex(dialog.repeat.findData('RDATE'))
        dialog.show()
        self.app.processEvents()
        november_date = date(2026, 11, 12)
        point = dialog.preview.grid.cell_rect(november_date).center().toPoint()
        QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertNotIn('2026', dialog.pattern.text())
        self.assertIn(november_date, dialog.dates())
        self.assertEqual(len(dialog.preview.events_on(november_date)), 1)
        QTest.mouseClick(dialog.preview.next_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        december_date = date(2026, 12, 3)
        point = dialog.preview.grid.cell_rect(december_date).center().toPoint()
        QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(dialog.preview.current_month, date(2026, 12, 1))
        self.assertIn(december_date, dialog.dates())
        self.assertEqual(len(dialog.preview.events_on(december_date)), 1)
        dialog.save()
        master = dialog.result_events[0]
        self.assertEqual(len(master.recurrence_dates), 2)
        self.store.apply('Create custom seminar', [master])
        occurrences = self.store.query_occurrences(master.start, datetime(2027, 1, 1, tzinfo=ZONE))
        self.assertEqual([event.start.date() for event in occurrences],
                         [date(2026, 11, 4), november_date, december_date])

    def test_reopened_sequence_keeps_fixed_event_and_can_remove_additions(self):
        create = self.dialog()
        create.title.setText('Irregular seminar')
        create.pattern.setText('11.11.2026, 2.12.2026')
        create.save()
        master = create.result_events[0]
        self.store.apply('Create custom seminar', [master])
        instances = self.store.query_occurrences(master.start, datetime(2027, 1, 1, tzinfo=ZONE))
        selected = instances[1]
        edit = self.dialog(event=selected, series=master, edit_scope='group')
        self.assertEqual(edit.repeat.currentData(), 'RDATE')
        self.assertEqual(edit.value(edit.start), master.start)
        self.assertEqual(len(edit.preview.events_on(date(2026, 11, 4))), 1)
        edit.show()
        self.app.processEvents()
        point = edit.preview.grid.cell_rect(date(2026, 11, 4)).center().toPoint()
        QTest.mouseClick(edit.preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertIn('initial event is fixed', edit.error.text())
        self.assertFalse(edit.schedule_replacement)
        point = edit.preview.grid.cell_rect(date(2026, 11, 11)).center().toPoint()
        QTest.mouseClick(edit.preview.grid, Qt.MouseButton.LeftButton, pos=point)
        self.assertNotIn('11.11.2026', edit.pattern.text())
        self.assertTrue(edit.schedule_replacement)
        edit.save()
        self.assertEqual(edit.result(), QDialog.DialogCode.Accepted, edit.error.text())
        self.store.edit_series(edit.edit_reference, edit.result_events[0], replace_schedule=edit.schedule_replacement)
        changed = self.store.query_occurrences(datetime(2026, 11, 1, tzinfo=ZONE), datetime(2027, 1, 1, tzinfo=ZONE))
        self.assertEqual([event.start.date() for event in changed], [date(2026, 11, 4), date(2026, 12, 2)])
        self.store.undo()
        self.assertEqual(self.store.get_event(master.id), master)

    def test_editor_date_time_and_all_calendar_popups_follow_preferences(self):
        preferences = Preferences(start_weekday=6, date_format='iso', time_format='12h')
        dialog = self.dialog(preferences=preferences)
        self.assertEqual(dialog.start.displayFormat(), 'yyyy-MM-dd   h:mm AP')
        self.assertEqual(dialog.end.displayFormat(), 'yyyy-MM-dd   h:mm AP')
        self.assertEqual(dialog.repeat_until.displayFormat(), 'yyyy-MM-dd')
        for field in (dialog.start, dialog.end, dialog.repeat_until):
            with self.subTest(field=field):
                self.assertEqual(field.calendarWidget().firstDayOfWeek(), Qt.DayOfWeek.Sunday)

    def test_invalid_pattern_cannot_be_saved(self):
        dialog = self.dialog()
        dialog.title.setText("Seminar")
        dialog.pattern.setText("4*(+2")
        self.assertIn("Missing closing", dialog.error.text())
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.result_events, [])

    def test_edit_locks_calendar_and_preserves_read_only_metadata(self):
        self.store.save_calendar(Calendar("other", "Other", "#775533"))
        start = datetime(2026, 11, 4, 10, tzinfo=ZONE)
        original = Event("old", "local", "Read only", start, start + timedelta(hours=1), read_only=True)
        dialog = self.dialog(event=original)
        self.assertFalse(dialog.calendar.isEnabled())
        dialog.title.setText("Changed title")
        dialog.save()
        self.assertEqual(len(dialog.result_events), 1)
        self.assertEqual(dialog.result_events[0].id, original.id)
        self.assertEqual(dialog.result_events[0].calendar_id, original.calendar_id)
        self.assertTrue(dialog.result_events[0].read_only)

    def test_notes_are_plain_text_and_survive_editing(self):
        start = datetime(2026, 11, 4, 10, tzinfo=ZONE)
        text = "Use <b> literally & keep <https://example.org> as text."
        original = Event("old", "local", "Literal notes", start, start + timedelta(hours=1), description=text)
        dialog = self.dialog(event=original)
        self.assertEqual(dialog.description.toPlainText(), text)
        dialog.save()
        self.assertEqual(dialog.result_events[0].description, text)

    def test_nonexistent_dst_time_is_rejected(self):
        start = datetime(2027, 3, 28, 2, 30, tzinfo=ZONE)
        dialog = self.dialog(start=start, end=start + timedelta(hours=1))
        dialog.title.setText("Impossible time")
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIn("clock change", dialog.error.text())

    def test_original_ambiguous_dst_fold_survives_editing(self):
        start = datetime(2026, 10, 25, 2, 15, tzinfo=ZONE, fold=1)
        end = datetime(2026, 10, 25, 2, 45, tzinfo=ZONE, fold=1)
        original = Event("fold", "local", "Late repeated hour", start, end)
        dialog = self.dialog(event=original)
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_events[0].start.timestamp(), original.start.timestamp())
        self.assertEqual(dialog.result_events[0].end.timestamp(), original.end.timestamp())


class CanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.zone_patch = patch("debacal.calendar_widgets.local_zone", return_value=ZONE)
        self.zone_patch.start()
        self.callback_errors = []
        self.exception_patch = patch("sys.excepthook", side_effect=lambda _, value, __: self.callback_errors.append(value))
        self.exception_patch.start()
        self.widgets = []
        self.calendars = [Calendar("local", "Personal", "#4f7865")]

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        self.app.processEvents()
        self.zone_patch.stop()
        self.exception_patch.stop()
        self.assertEqual(self.callback_errors, [])

    def event(self, key, day=date(2026, 11, 2), hour=9, duration=1, **kwargs):
        start = datetime(day.year, day.month, day.day, hour, tzinfo=ZONE)
        return Event(key, "local", "Event " + key, start, start + timedelta(hours=duration), **kwargs)

    def canvas(self, days=None, events=()):
        canvas = CalendarCanvas()
        self.widgets.append(canvas)
        canvas.resize(1042, 1580)
        canvas.set_data(days or [date(2026, 11, 2) + timedelta(days=index) for index in range(7)],
                        list(events), self.calendars)
        canvas.show()
        self.app.processEvents()
        return canvas

    def test_day_and_week_render_overlaps_multiday_and_all_day(self):
        events = [self.event("a", duration=2), self.event("b", hour=10),
                  self.event("overnight", hour=23, duration=3),
                  self.event("all-day", hour=0, duration=24, all_day=True)]
        for days in [[date(2026, 11, 2)], [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]]:
            with self.subTest(days=len(days)):
                canvas = self.canvas(days, events)
                self.assertFalse(canvas.grab().isNull())
                rects = {event.id: rect for rect, event, _ in canvas.hits}
                self.assertGreater(rects["a"].width(), 0)
                self.assertFalse(rects["a"].intersects(rects["b"]))
                self.assertLess(rects["all-day"].bottom(), canvas.all_day_height)
                self.assertEqual(sum(event.id == "overnight" for _, event, _ in canvas.hits), len(days[:2]))

    def test_mouse_drag_creates_expected_time_range(self):
        canvas = self.canvas()
        spy = QSignalSpy(canvas.create_requested)
        start = QPoint(canvas.gutter + 30, canvas.all_day_height + 9 * canvas.hour_height)
        end = QPoint(start.x(), start.y() + canvas.hour_height)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(spy.count(), 1)
        created_start, created_end = spy.at(0)
        self.assertEqual(created_start, datetime(2026, 11, 2, 9, tzinfo=ZONE))
        self.assertEqual(created_end - created_start, timedelta(hours=1))

    def test_drag_creation_can_end_at_midnight(self):
        canvas = self.canvas()
        spy = QSignalSpy(canvas.create_requested)
        start = QPoint(canvas.gutter + 30, canvas.all_day_height + 23 * canvas.hour_height)
        end = QPoint(start.x(), canvas.all_day_height + 24 * canvas.hour_height)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0)[1], datetime(2026, 11, 3, tzinfo=ZONE))

    def test_dst_fold_crossing_event_renders_and_moves_with_positive_duration(self):
        original = Event("fold", "local", "Repeated hour",
                         datetime(2026, 10, 25, 2, 45, tzinfo=ZONE, fold=0),
                         datetime(2026, 10, 25, 2, 15, tzinfo=ZONE, fold=1))
        canvas = self.canvas(days=[date(2026, 10, 25)], events=[original])
        self.assertFalse(canvas.grab().isNull())
        rect = canvas.hits[0][0]
        spy = QSignalSpy(canvas.move_requested)
        origin = QPoint(int(rect.center().x()), int(rect.top()) + 3)
        destination = QPoint(origin.x(), origin.y() + 2 * canvas.hour_height)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, destination)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=destination)
        self.assertEqual(spy.count(), 1)
        _, start, end = spy.at(0)
        self.assertEqual(end.timestamp() - start.timestamp(), 30 * 60)

    def test_drag_event_moves_to_another_day_and_preserves_duration(self):
        original = self.event("move")
        canvas = self.canvas(events=[original])
        spy = QSignalSpy(canvas.move_requested)
        rect = next(rect for rect, event, _ in canvas.hits if event.id == original.id)
        origin = QPoint(int(rect.center().x()), int(rect.top()) + 15)
        width = (canvas.width() - canvas.gutter) / len(canvas.days)
        destination = QPoint(origin.x() + int(width), origin.y() + canvas.hour_height)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, destination)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=destination)
        self.assertEqual(spy.count(), 1)
        event_id, start, end = spy.at(0)
        self.assertEqual(event_id, original.id)
        self.assertEqual(start, datetime(2026, 11, 3, 10, tzinfo=ZONE))
        self.assertEqual(end - start, original.end - original.start)

    def test_drag_event_bottom_resizes_duration(self):
        original = self.event("resize")
        canvas = self.canvas(events=[original])
        spy = QSignalSpy(canvas.move_requested)
        rect = next(rect for rect, event, _ in canvas.hits if event.id == original.id)
        origin = QPoint(int(rect.center().x()), int(rect.bottom()) - 3)
        destination = QPoint(origin.x(), origin.y() + canvas.hour_height // 2)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=origin)
        QTest.mouseMove(canvas, destination)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=destination)
        self.assertEqual(spy.count(), 1)
        event_id, start, end = spy.at(0)
        self.assertEqual(event_id, original.id)
        self.assertEqual(start, original.start)
        self.assertEqual(end, original.end + timedelta(minutes=30))

    def test_month_and_headers_render_without_exceptions(self):
        month = MonthCanvas()
        self.widgets.append(month)
        month.resize(1050, 700)
        month.set_data(date(2026, 10, 26), 11, [self.event("month")], self.calendars)
        month.show()
        self.app.processEvents()
        self.assertFalse(month.grab().isNull())
        self.assertEqual(len(month.hits), 1)
        for days in [[date(2026, 11, 2)], [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]]:
            header = DayHeader()
            self.widgets.append(header)
            header.resize(1042, 66)
            header.days = days
            self.assertFalse(header.grab().isNull())

    def test_month_empty_day_and_overflow_create_at_default_time(self):
        month = MonthCanvas()
        self.widgets.append(month)
        month.resize(1050, 700)
        events = [self.event(str(i), hour=9 + i) for i in range(8)]
        month.set_data(date(2026, 10, 26), 11, events, self.calendars)
        month.show()
        self.app.processEvents()
        open_spy = QSignalSpy(month.open_day)
        create_spy = QSignalSpy(month.create_requested)
        # Week two, Monday, on its overflow line below the visible events.
        day_height = (month.height() - 28) / 6
        overflow = QPoint(30, int(28 + day_height + 85))
        QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=overflow)
        self.assertEqual(create_spy.count(), 1)
        self.assertEqual(create_spy.at(0)[0], datetime(2026, 11, 2, 9, tzinfo=local_zone()))
        empty = QPoint(170, int(28 + day_height + 85))
        QTest.mouseDClick(month, Qt.MouseButton.LeftButton, pos=empty)
        self.assertEqual(create_spy.count(), 2)
        self.assertEqual(create_spy.at(1)[0], datetime(2026, 11, 3, 9, tzinfo=local_zone()))
        self.assertEqual(open_spy.count(), 0)

    def test_day_numbers_only_appear_during_navigation_and_mark_chosen_day(self):
        header = DayHeader()
        self.widgets.append(header)
        header.resize(1042, 66)
        header.days = [date(2026, 11, 2) + timedelta(days=index) for index in range(7)]
        with patch('debacal.calendar_widgets.badge', wraps=badge) as draw_badge:
            header.grab()
            self.assertFalse(draw_badge.called)
            header.set_navigation(True, 2, '8')
            header.grab()
            self.assertEqual([call.args[2] for call in draw_badge.call_args_list], list('1234567'))
            self.assertEqual([call.args[3] for call in draw_badge.call_args_list],
                             [False, False, True, False, False, False, False])
            draw_badge.reset_mock()
            header.set_navigation(False)
            header.grab()
            self.assertFalse(draw_badge.called)

    def test_event_number_badges_and_typed_target_are_scoped_to_chosen_day(self):
        events = [self.event('first'), self.event('second', hour=11),
                  self.event('tuesday', day=date(2026, 11, 3))]
        canvas = self.canvas(events=events)
        with patch('debacal.calendar_widgets.badge', wraps=badge) as draw_badge:
            canvas.grab()
            self.assertFalse(draw_badge.called)
            canvas.set_navigation(True)
            canvas.grab()
            self.assertFalse(draw_badge.called)
            canvas.set_navigation(True, 0, '2')
            canvas.grab()
            self.assertEqual([call.args[2] for call in draw_badge.call_args_list], ['1', '2'])
            self.assertEqual([call.args[3] for call in draw_badge.call_args_list], [False, True])
            self.assertEqual(canvas.navigation_target_id(), 'second')
            canvas.set_navigation(True, 0, '9')
            self.assertIsNone(canvas.navigation_target_id())
            draw_badge.reset_mock()
            canvas.set_navigation(False)
            canvas.grab()
            self.assertFalse(draw_badge.called)

    def test_month_headers_follow_actual_week_order_and_display_preferences(self):
        month = MonthCanvas()
        self.widgets.append(month)
        month.resize(1050, 700)
        month.set_preferences(Preferences(start_weekday=6, date_format='iso', time_format='12h'))
        month.set_data(date(2026, 11, 1), 11, [self.event('afternoon', hour=14)], self.calendars)
        with patch('debacal.calendar_widgets.text', wraps=text) as draw_text:
            month.grab()
            labels = [call.args[2] for call in draw_text.call_args_list]
            self.assertEqual(labels[:7], [month.preferences.weekday_name(day).upper() for day in [6, 0, 1, 2, 3, 4, 5]])
            self.assertTrue(any(label.startswith(month.preferences.format_time(datetime(2026, 11, 2, 14)) + ' ')
                                for label in labels))


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = Store(":memory:")
        self.callback_errors = []
        self.exception_patch = patch("sys.excepthook", side_effect=lambda _, value, __: self.callback_errors.append(value))
        self.exception_patch.start()
        self.window = MainWindow(self.store, enable_sync=False)
        self.window.anchor = date(2026, 11, 4)
        self.window.refresh()
        self.window.show()
        self.window.activateWindow()
        self.window.canvas.setFocus()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.exception_patch.stop()
        self.store.close()
        self.assertEqual(self.callback_errors, [])

    def test_confirm_candidate_is_one_undoable_action(self):
        dialog = EventDialog(self.store.list_calendars(), datetime(2026, 11, 4, 10, tzinfo=ZONE))
        dialog.title.setText("Find a meeting slot")
        dialog.pattern.setText("2*+1d")
        dialog.candidate.setChecked(True)
        dialog.save()
        self.window.apply_events("Create meeting options", dialog.result_events)
        original = self.store.list_events()
        selected = self.store.query_occurrences(datetime(2026, 11, 1, tzinfo=ZONE),
                                                datetime(2026, 12, 1, tzinfo=ZONE))[1]
        self.window.confirm_candidate(selected.id)
        self.assertEqual(len(self.store.list_events()), 1)
        confirmed = self.store.get_event(original[0].id)
        self.assertFalse(confirmed.candidate or confirmed.recurring)
        self.assertEqual(confirmed.start, selected.start)
        self.assertEqual(confirmed.end, selected.end)
        self.assertEqual(len(self.store.history()), 2)
        self.window.undo()
        self.assertEqual(self.store.list_events(), original)
        self.window.redo()
        self.assertEqual(self.store.list_events(), [confirmed])
        dialog.close()

    def test_custom_navigation_key_and_typing_are_independent(self):
        self.window.bindings["next"] = "J"
        self.window.update_hints()
        original = self.window.anchor
        QTest.keyClick(self.window.canvas, Qt.Key.Key_J)
        self.assertEqual(self.window.anchor, original + timedelta(days=7))
        QTest.keyClick(self.window.canvas, Qt.Key.Key_L)
        self.assertEqual(self.window.anchor, original + timedelta(days=7))
        self.window.search.setFocus()
        self.app.processEvents()
        QTest.keyClicks(self.window.search, "jndwmg")
        self.assertEqual(self.window.search.text(), "jndwmg")
        self.assertEqual(self.window.anchor, original + timedelta(days=7))
        self.assertEqual(self.window.view, "week")
        self.assertFalse(self.window.select_mode)

    def test_numbered_keyboard_selection_selects_third_day_eighth_event(self):
        start = datetime(2026, 11, 4, 8, tzinfo=ZONE)
        events = [Event(str(i), "local", f"Seminar {i}", start + timedelta(hours=i),
                        start + timedelta(hours=i + 1)) for i in range(9)]
        self.window.apply_events("Create seminars", events)
        self.window.canvas.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.canvas, Qt.Key.Key_G)
        self.assertTrue(self.window.select_mode)
        self.assertTrue(self.window.canvas.show_numbers)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_3)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_8)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Return)
        self.assertEqual(self.window.selected_id, "7")
        self.assertFalse(self.window.select_mode)

    def test_shortcuts_do_not_run_in_event_editor(self):
        dialog = EventDialog(self.store.list_calendars(), datetime(2026, 11, 4, 10, tzinfo=ZONE), parent=self.window)
        dialog.show()
        dialog.activateWindow()
        dialog.title.setFocus()
        self.app.processEvents()
        original = self.window.anchor
        QTest.keyClicks(dialog.title, "new event tomorrow")
        self.assertEqual(dialog.title.text(), "new event tomorrow")
        self.assertEqual(self.window.anchor, original)
        self.assertEqual(self.window.view, "week")
        dialog.close()

    def test_navigation_badges_track_day_and_typed_event_then_clear(self):
        start = datetime(2026, 11, 4, 10, tzinfo=ZONE)
        events = [Event(str(i), 'local', f'Meeting {i}', start + timedelta(hours=i),
                        start + timedelta(hours=i + 1)) for i in range(2)]
        self.window.apply_events('Create meetings', events)
        self.assertFalse(self.window.header.navigation_active)
        self.assertFalse(self.window.canvas.show_numbers)
        self.window.canvas.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.canvas, Qt.Key.Key_G)
        self.assertTrue(self.window.header.navigation_active)
        self.assertIsNone(self.window.header.navigation_day)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_3)
        self.assertEqual(self.window.header.navigation_day, 2)
        self.assertEqual(self.window.canvas.navigation_day, 2)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_2)
        self.assertEqual(self.window.canvas.navigation_target_id(), '1')
        self.assertIn('Meeting 1', self.window.navigation_prompt.text())
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Return)
        self.assertEqual(self.window.selected_id, '1')
        self.assertFalse(self.window.header.navigation_active)
        self.assertFalse(self.window.canvas.show_numbers)

    def test_entering_navigation_preserves_scroll_position(self):
        self.window.new_button.setFocus()
        self.app.processEvents()
        scrollbar = self.window.scroll.verticalScrollBar()
        scrollbar.setValue(9 * self.window.canvas.hour_height)
        original = scrollbar.value()
        self.window.start_selection()
        self.app.processEvents()
        self.assertEqual(scrollbar.value(), original)

    def test_arrow_selection_preserves_scroll_for_visible_events(self):
        start = datetime(2026, 11, 4, 10, tzinfo=ZONE)
        events = [Event(str(i), 'local', f'Meeting {i}', start + timedelta(hours=i),
                        start + timedelta(hours=i + 1)) for i in range(2)]
        self.window.apply_events('Create meetings', events)
        self.window.select_event('0')
        self.app.processEvents()
        scrollbar = self.window.scroll.verticalScrollBar()
        scrollbar.setValue(8 * self.window.canvas.hour_height)
        original = scrollbar.value()
        self.window.select_by_arrow('down')
        self.app.processEvents()
        self.assertEqual(self.window.selected_id, '1')
        self.assertEqual(scrollbar.value(), original)

    def test_arrow_selection_scrolls_only_enough_to_show_hidden_event(self):
        if not self.window.full_day:
            self.window.toggle_full_day()
        start = datetime(2026, 11, 4, 22, tzinfo=ZONE)
        event = Event('late', 'local', 'Late event', start, start + timedelta(hours=1))
        self.window.apply_events('Create late event', [event])
        self.app.processEvents()
        scrollbar = self.window.scroll.verticalScrollBar()
        scrollbar.setValue(0)
        self.window.select_by_arrow('down')
        self.app.processEvents()
        rect = next(rect for rect, item, _ in self.window.canvas.rects() if item.id == 'late')
        minimum = rect.bottom() - self.window.scroll.viewport().height()
        self.assertGreater(scrollbar.value(), 0)
        self.assertGreaterEqual(scrollbar.value(), minimum)
        self.assertLessEqual(scrollbar.value(), minimum + 40)

    def test_sunday_start_aligns_week_month_and_mini_calendar(self):
        self.window.preferences = Preferences(start_weekday=6, date_format='iso', time_format='24h')
        self.window.refresh()
        self.assertEqual(self.window.dates()[0], date(2026, 11, 1))
        self.assertEqual(self.window.header.days[0].weekday(), 6)
        self.assertEqual(self.window.canvas.days[0].weekday(), 6)
        self.assertEqual(self.window.mini.firstDayOfWeek(), Qt.DayOfWeek.Sunday)
        self.assertIn('2026-11-01', self.window.range_label.text())
        self.window.set_view('month')
        self.assertEqual(self.window.dates()[0], date(2026, 11, 1))
        self.assertEqual(self.window.month.start.weekday(), 6)
        self.assertEqual(self.window.month.preferences, self.window.preferences)


if __name__ == "__main__":
    unittest.main()
