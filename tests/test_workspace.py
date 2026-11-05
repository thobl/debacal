"""Cross-component regressions for workspace commands and navigation."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QEvent, QCoreApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from lebacal.models import Calendar, Event
from lebacal.dialogs import EventDialog
from lebacal.store import Store
from lebacal.ui import MainWindow


ZONE = ZoneInfo("Europe/Berlin")


class WorkspaceRegressionTests(unittest.TestCase):
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

    def event(self, key="a", day=date(2026, 11, 4), hour=10, **kwargs):
        start = datetime(day.year, day.month, day.day, hour, tzinfo=ZONE)
        return Event(key, "local", "Study", start, start + timedelta(hours=1), **kwargs)

    def test_confirmation_removes_only_same_calendar_candidates(self):
        self.store.save_calendar(Calendar("other", "Other", "#456789"))
        chosen = self.event("chosen", group_id="group", candidate=True)
        rejected = self.event("rejected", group_id="group", candidate=True)
        confirmed = self.event("confirmed", group_id="group", candidate=False)
        other_calendar = replace(self.event("other", group_id="group", candidate=True), calendar_id="other")
        self.window.apply_events("Create", [chosen, rejected, confirmed, other_calendar])
        self.window.confirm_candidate(chosen.id)
        self.assertIsNone(self.store.get_event(rejected.id))
        self.assertEqual(self.store.get_event(confirmed.id), confirmed)
        self.assertEqual(self.store.get_event(other_calendar.id), other_calendar)
        self.assertFalse(self.store.get_event(chosen.id).candidate)
        self.window.undo()
        self.assertEqual(len(self.store.list_events()), 4)

    def test_navigation_clears_offscreen_selection_before_delete(self):
        event = self.event()
        self.window.apply_events("Create", [event])
        self.window.select_event(event.id)
        self.window.navigate(1)
        self.assertIsNone(self.window.selected_id)
        self.window.delete_event()
        self.assertEqual(self.store.get_event(event.id), event)

    def test_group_edit_guard_runs_before_opening_editor(self):
        selected = self.event("selected", group_id="group")
        locked = self.event("locked", group_id="group", read_only=True)
        self.store.apply_remote([selected, locked])
        self.window.refresh()
        with patch("lebacal.ui.EventDialog") as editor:
            self.window.edit_event(selected.id, group=True)
            editor.assert_not_called()
        self.assertIn("read-only", self.window.notice.text())

    def test_invalid_move_leaves_event_and_history_unchanged(self):
        event = self.event()
        self.window.apply_events("Create", [event])
        self.window.move_event(event.id, event.end, event.start)
        self.assertEqual(self.store.get_event(event.id), event)
        self.assertEqual(len(self.store.history()), 1)
        self.assertIn("end must not be before", self.window.notice.text())

    def test_multichord_custom_shortcut_and_selection_arrows(self):
        self.window.bindings["next"] = "Ctrl+K, Ctrl+L"
        anchor = self.window.anchor
        QTest.keyClick(self.window.canvas, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.anchor, anchor)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.anchor, anchor + timedelta(days=7))
        self.window.go_date(anchor)
        a, b = self.event("a"), self.event("b", hour=12)
        self.window.apply_events("Create", [a, b])
        self.window.start_selection()
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Down)
        self.assertEqual(self.window.selected_id, a.id)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Down)
        self.assertEqual(self.window.selected_id, b.id)

    def test_month_open_day_signal_changes_view_and_anchor(self):
        self.window.set_view("month")
        self.window.month.open_day.emit(date(2026, 11, 18))
        self.assertEqual(self.window.view, "day")
        self.assertEqual(self.window.anchor, date(2026, 11, 18))
        self.assertEqual(self.window.canvas.days, [date(2026, 11, 18)])

    def test_printable_search_shortcut_works_with_shifted_layout(self):
        for key in (Qt.Key.Key_Slash, Qt.Key.Key_7):
            with self.subTest(key=key):
                self.window.canvas.setFocus()
                QCoreApplication.sendEvent(self.window.canvas, QKeyEvent(
                    QEvent.Type.KeyPress, key, Qt.KeyboardModifier.ShiftModifier, "/"))
                self.assertTrue(self.window.search.hasFocus())
        self.window.canvas.setFocus()
        self.window.bindings["next"] = "Shift+7"
        anchor = self.window.anchor
        QCoreApplication.sendEvent(self.window.canvas, QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_7, Qt.KeyboardModifier.ShiftModifier, "/"))
        self.assertEqual(self.window.anchor, anchor + timedelta(days=7))
        self.assertFalse(self.window.search.hasFocus())

    def test_search_reuses_index_and_refresh_invalidates_it(self):
        self.window.apply_events("Create", [self.event()])
        with patch.object(self.store, "list_events", wraps=self.store.list_events) as reads:
            self.window.search.setText("st")
            self.window.search.setText("study")
            self.assertEqual(reads.call_count, 1)
        self.window.apply_events("Create another", [self.event("another")])
        self.assertEqual(self.window.results.count(), 2)

    def test_group_date_shift_preserves_wall_hour_across_dst(self):
        selected = self.event("first", day=date(2026, 10, 19), group_id="group")
        later = self.event("later", day=date(2026, 11, 2), group_id="group")
        self.store.apply("Create", [selected, later])
        def save_dialog(dialog):
            self.assertEqual(dialog.scope.currentData(), 'group')
            for field in (dialog.start, dialog.end):
                field.setDateTime(field.dateTime().addDays(7))
            dialog.save()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
            return dialog.result()
        with patch.object(EventDialog, 'exec', save_dialog), \
                patch('lebacal.ui.local_zone', return_value=ZONE), \
                patch('lebacal.dialogs.local_zone', return_value=ZONE):
            self.window.edit_event(selected.id, group=True)
        moved = self.store.get_event(later.id).start.astimezone(ZONE)
        self.assertEqual(moved.date(), date(2026, 11, 9))
        self.assertEqual(moved.hour, 10)

    def edit_with_real_dialog(self, event_id, *, group=None, scope=None, title='Updated study', shift_hours=1):
        scopes = []

        def save_dialog(dialog):
            scopes.append(dialog.scope.currentData())
            if scope:
                dialog.scope.setCurrentIndex(dialog.scope.findData(scope))
            dialog.title.setText(title)
            dialog.location.setText('New room')
            for field in (dialog.start, dialog.end):
                field.setDateTime(field.dateTime().addSecs(shift_hours * 3600))
            dialog.save()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
            return dialog.result()

        with patch.object(EventDialog, 'exec', new=save_dialog), \
                patch('lebacal.ui.local_zone', return_value=ZONE), \
                patch('lebacal.dialogs.local_zone', return_value=ZONE):
            self.window.edit_event(event_id, group=group)
        return scopes

    def test_real_editor_explicit_whole_candidate_group_edit(self):
        originals = [self.event(str(index), day=date(2026, 11, 4 + index),
                                group_id='meeting-options', candidate=True) for index in range(3)]
        self.window.apply_events('Create options', originals)
        scopes = self.edit_with_real_dialog(originals[1].id, group=True)
        self.assertEqual(scopes, ['group'])
        for original in originals:
            changed = self.store.get_event(original.id)
            self.assertEqual(changed.title, 'Updated study')
            self.assertEqual(changed.location, 'New room')
            self.assertEqual(changed.start, original.start + timedelta(hours=1))
            self.assertTrue(changed.candidate)
            self.assertEqual(changed.group_id, original.group_id)
        self.assertEqual(len(self.store.history()), 2)
        self.window.undo()
        self.assertEqual(self.store.list_events(), originals)

    def test_real_editor_explicit_single_candidate_edit_keeps_siblings(self):
        originals = [self.event(str(index), day=date(2026, 11, 4 + index),
                                group_id='meeting-options', candidate=True) for index in range(3)]
        self.window.apply_events('Create options', originals)
        # Choosing the single-event option after opening the whole group must
        # propagate to the command, not just change the visible form.
        scopes = self.edit_with_real_dialog(originals[1].id, group=True, scope='single')
        self.assertEqual(scopes, ['group'])
        self.assertEqual(self.store.get_event(originals[0].id), originals[0])
        self.assertEqual(self.store.get_event(originals[2].id), originals[2])
        changed = self.store.get_event(originals[1].id)
        self.assertEqual(changed.title, 'Updated study')
        self.assertEqual(changed.start, originals[1].start + timedelta(hours=1))
        self.assertEqual(len(self.store.history()), 2)

    def test_real_editor_group_recurrence_edit_updates_one_master(self):
        master = self.event('series', recurrence_rule='FREQ=WEEKLY;COUNT=3')
        self.window.apply_events('Create recurring study', [master])
        self.window.go_date(date(2026, 11, 11))
        displayed = self.window.visible_events[0]
        self.assertEqual(displayed.series_id, master.id)
        self.assertEqual(displayed.start.date(), date(2026, 11, 11))
        with patch.object(self.store, 'edit_selection', wraps=self.store.edit_selection) as edit_selection:
            scopes = self.edit_with_real_dialog(displayed.id, group=True)
            self.assertEqual(scopes, ['group'])
            self.assertEqual(edit_selection.call_count, 1)
            self.assertEqual(edit_selection.call_args.args[1], 'group')
            self.assertEqual(edit_selection.call_args.args[2], master)
        changed = self.store.get_event(master.id)
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(changed.title, 'Updated study')
        self.assertEqual(changed.start, master.start + timedelta(hours=1))
        self.assertIn('FREQ=WEEKLY', changed.recurrence_rule)
        self.assertIn('COUNT=3', changed.recurrence_rule)
        self.assertEqual(changed.recurrence_overrides, {})
        occurrences = self.store.query_occurrences(master.start, master.start + timedelta(days=30))
        self.assertEqual(len(occurrences), 3)
        self.assertTrue(all(event.title == 'Updated study' and event.start.hour == 11 for event in occurrences))
        self.window.undo()
        self.assertEqual(self.store.get_event(master.id), master)

    def test_series_edit_through_dialog_preserves_exclusions(self):
        master = self.event('excluded-series', recurrence_rule='FREQ=WEEKLY;COUNT=3',
                            excluded_dates=(self.event(day=date(2026, 11, 11)).start.astimezone(ZoneInfo('UTC')).isoformat(),))
        self.window.apply_events('Create series', [master])
        selected = self.window.visible_events[0]
        self.edit_with_real_dialog(selected.id, group=True, shift_hours=0)
        self.assertEqual(self.store.get_event(master.id).excluded_dates, master.excluded_dates)
        occurrences = self.store.query_occurrences(master.start, master.start + timedelta(days=30))
        self.assertEqual(len(occurrences), 2)

    def test_search_finds_and_opens_single_occurrence_title(self):
        master = self.event('search-series', recurrence_rule='FREQ=WEEKLY;COUNT=3')
        self.window.apply_events('Create series', [master])
        self.window.go_date(date(2026, 11, 11))
        selected = self.window.visible_events[0]
        self.edit_with_real_dialog(selected.id, group=False, title='Exceptional review')
        self.window.search.setText('Exceptional')
        self.assertEqual(self.window.results.count(), 1)
        self.window.open_search_result(self.window.results.item(0))
        self.assertEqual(self.window.anchor, date(2026, 11, 11))
        self.assertEqual(self.window.event_by_id(self.window.selected_id).title, 'Exceptional review')

    def test_real_editor_single_recurrence_edit_creates_one_override(self):
        master = self.event('series', recurrence_rule='FREQ=WEEKLY;COUNT=3')
        self.window.apply_events('Create recurring study', [master])
        self.window.go_date(date(2026, 11, 11))
        displayed = self.window.visible_events[0]
        with patch.object(self.store, 'edit_selection', wraps=self.store.edit_selection) as edit_selection:
            scopes = self.edit_with_real_dialog(displayed.id, group=False, title='Only this review', shift_hours=2)
            self.assertEqual(scopes, ['single'])
            self.assertEqual(edit_selection.call_count, 1)
            self.assertEqual(edit_selection.call_args.args[1], 'single')
            self.assertEqual(edit_selection.call_args.args[2], displayed)
        changed = self.store.get_event(master.id)
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(changed.title, master.title)
        self.assertEqual(changed.start, master.start)
        self.assertEqual(changed.recurrence_rule, master.recurrence_rule)
        self.assertEqual(len(changed.recurrence_overrides), 1)
        self.assertIn(displayed.recurrence_id, changed.recurrence_overrides)
        occurrences = self.store.query_occurrences(master.start, master.start + timedelta(days=30))
        self.assertEqual([event.title for event in occurrences], ['Study', 'Only this review', 'Study'])
        self.assertEqual([event.start.hour for event in occurrences], [10, 12, 10])
        self.window.undo()
        self.assertEqual(self.store.get_event(master.id), master)


if __name__ == "__main__":
    unittest.main()
