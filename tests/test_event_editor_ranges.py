"""Production range editing, including pending keyboard text and all-day dates."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from dataclasses import replace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 8, 9, tzinfo=ZONE)


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        dialog = EventDialog([Calendar('local', 'Personal', '#587c67')], START,
                             START + timedelta(hours=1), preferences=Preferences(date_format='european'))
        dialog.title.setText('An appointment')
        dialog.show()
        app.processEvents()
        yield dialog
        if dialog.range_picker:
            dialog.range_picker.close()
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_enter_on_pending_start_preserves_duration_across_days(editor):
    editor.start.setText('9.11.2026 10:5')
    editor.start.setFocus()
    QTest.keyClick(editor.start.lineEdit(), Qt.Key.Key_Return)
    assert editor.result() == QDialog.DialogCode.Accepted, editor.error.text()
    event = editor.result_events[0]
    assert event.start == START.replace(day=9, hour=10, minute=5)
    assert event.end - event.start == timedelta(hours=1)


def test_pending_end_before_start_moves_start_and_preserves_duration(editor):
    editor.end.setText('7.11.2026 8:00')
    editor.save()
    event = editor.result_events[0]
    assert event.end == START.replace(day=7, hour=8)
    assert event.start == START.replace(day=7, hour=7)


def test_pending_duration_adjusts_end_without_changing_start(editor):
    editor.duration.setText('1h 15min')
    editor.save()
    assert editor.result_events[0].start == START
    assert editor.result_events[0].end == START + timedelta(minutes=75)


@pytest.mark.parametrize('change', ['start', 'duration'])
def test_pending_range_in_repeated_hour_keeps_both_endpoint_folds(editor, change):
    start = datetime(2026, 10, 25, 2, 10, tzinfo=ZONE, fold=1)
    end = start.replace(minute=30)
    event = Event('fold', 'local', 'Repeated hour', start, end)
    dialog = EventDialog(editor.calendars, start, end, event=event, preferences=Preferences(date_format='european'))
    try:
        if change == 'start':
            dialog.start.setText('25.10.2026 2:15')
        else:
            dialog.duration.setText('25min')
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        saved, = dialog.result_events
        assert saved.start.timestamp() == start.timestamp() + (300 if change == 'start' else 0)
        assert saved.end.timestamp() == end.timestamp() + 300
        assert saved.start.fold == saved.end.fold == 1
    finally:
        dialog.close()
        dialog.deleteLater()


def test_all_day_fields_are_inclusive_and_storage_end_is_exclusive(editor):
    editor.all_day.setChecked(True)
    assert editor.start.text() == editor.end.text()
    assert editor.duration.text() == '1 day'
    editor.end.setText('10.11.2026')
    editor.save()
    event = editor.result_events[0]
    assert event.all_day
    assert event.start == START.replace(hour=0)
    assert event.end == START.replace(day=11, hour=0)
    assert event.end - event.start == timedelta(days=3)


def test_date_picker_cancel_is_transactional_and_apply_updates_range(editor):
    editor.open_range_picker('start')
    picker = editor.range_picker
    picker.set_start(START + timedelta(days=3))
    picker.reject()
    assert editor.value(editor.start) == START
    editor.open_range_picker('end')
    picker = editor.range_picker
    picker.set_end(START - timedelta(days=2))
    picker.accept()
    assert editor.value(editor.end) == START - timedelta(days=2)
    assert editor.value(editor.start) == START - timedelta(days=2, hours=1)


def test_duration_presets_and_all_day_toggle_keep_endpoint_semantics(editor):
    editor.duration_choices.setCurrentData(45)
    assert editor.value(editor.end) == START + timedelta(minutes=45)
    editor.all_day.setChecked(True)
    assert editor.value(editor.end) - editor.value(editor.start) == timedelta(days=1)
    assert editor.start.isAllDay() and editor.end.isAllDay()
    editor.open_range_picker('end')
    picker = editor.range_picker
    assert not picker.strip.isVisible()
    assert picker.view_day == START.date()
    picker.reject()
    editor.all_day.setChecked(False)
    assert editor.value(editor.start) == START
    assert editor.value(editor.end) == START + timedelta(minutes=45)


def test_invalid_sequence_cannot_save_previous_valid_pattern(editor):
    editor.schedule_mode.setCurrentData('repeat')
    editor.repeat.setCurrentData('RDATE')
    editor.pattern.setText('2*(+2')
    editor.save()
    assert editor.result() == QDialog.DialogCode.Rejected
    assert not editor.result_events
    assert editor.pattern.text() == '2*(+2'
    assert not editor.preview.date_editing


def test_new_event_has_no_legacy_scope_cycle(editor):
    assert not hasattr(editor, 'cycle_scope')
    assert editor.scope.currentData() == 'single'
    assert editor.value(editor.start) == START


def test_longer_duration_preset_lists_use_a_full_row_and_keep_custom_duration(editor):
    editor._set_duration_preferences(replace(editor.preferences, duration_presets_minutes=(15, 30, 60, 90, 120, 180)))
    QApplication.processEvents()
    assert not editor.duration_fields._wide
    assert editor.value(editor.end) == START + timedelta(hours=1)
    editor.duration_choices.setCurrentData(90)
    assert editor.value(editor.end) == START + timedelta(minutes=90)
    assert editor.duration_choices.buttons[90].text() == '1h30'
    assert editor.duration_choices.buttons[90].toolTip() == '1h 30min'
