"""Whole-value input and range selection preserve dates, durations and drafts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog

import debacal.date_time_picker as picker_module
from debacal.date_time_fields import DateTimeField, format_duration, parse_datetime, parse_duration
from debacal.date_time_picker import DateTimeRangePicker
from debacal.preferences import Preferences


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
PREFERENCES = Preferences(locale_name='en_GB', date_format='european', time_format='24h')


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        yield app, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def settle(app):
    for _ in range(5):
        app.processEvents()


def make_picker(desktop, start=START, end=None, **options):
    app, widgets = desktop
    picker = DateTimeRangePicker(start, end or start + timedelta(hours=1), preferences=PREFERENCES, **options)
    widgets.append(picker)
    picker.show()
    settle(app)
    return picker


def test_constructing_a_picker_never_shows_a_temporary_top_level_window(desktop):
    app, widgets = desktop
    parent = QDialog()
    widgets.append(parent)
    parent.show()
    settle(app)
    shown = []
    class Observer(QObject):
        def eventFilter(self, widget, event):
            if event.type() == QEvent.Type.Show and getattr(widget, 'isWindow', lambda: False)():
                shown.append(widget)
            return False
    observer = Observer()
    app.installEventFilter(observer)
    try:
        picker = DateTimeRangePicker(START, START + timedelta(hours=1), parent=parent)
        widgets.append(picker)
        assert shown == []
        assert picker.isAncestorOf(picker.full_day_button)
        assert not picker.full_day_button.isWindow()
        assert picker.windowType() == Qt.WindowType.Dialog
        assert picker.windowModality() == Qt.WindowModality.WindowModal
        picker.open()
        settle(app)
        assert shown == [picker]
        assert picker.windowHandle().transientParent() is parent.windowHandle()
    finally:
        app.removeEventFilter(observer)


@pytest.mark.parametrize('text', ['4.11.2026 9:5', '04.11.2026 09:05', '2026-11-4 9:05',
                                 '4 November 2026 9:05', '4. November 2026 9:05', '4.11.2026, 9:05'])
def test_unpadded_and_alternative_datetime_formats(text):
    assert parse_datetime(text, PREFERENCES, START) == START.replace(minute=5)


def test_ambiguous_slash_dates_follow_regional_format_and_dot_dates_stay_european():
    us = Preferences(date_format='us', time_format='12h')
    assert parse_datetime('4/11/2026 9:05 PM', us, START) == datetime(2026, 4, 11, 21, 5, tzinfo=ZONE)
    assert parse_datetime('4/11/2026 9:05', PREFERENCES, START) == START.replace(minute=5)
    assert parse_datetime('4.11.2026 9:05', us, START) == START.replace(minute=5)
    assert parse_datetime('4 März 2027 9:05', PREFERENCES, START) == datetime(2027, 3, 4, 9, 5, tzinfo=ZONE)


def test_field_unchanged_precision_fold_and_typed_timezone_are_preserved(desktop):
    _, widgets = desktop
    field = DateTimeField(PREFERENCES)
    widgets.append(field)
    precise = datetime(2026, 10, 25, 2, 30, 17, 123456, tzinfo=ZONE, fold=1)
    field.set_value(precise)
    assert field.value().timestamp() == precise.timestamp()
    assert field.value().microsecond == 123456 and field.value().fold == 1
    changed = QSignalSpy(field.dateTimeChanged)
    field.setDateTime(field.dateTime().addSecs(900))
    assert changed.count() == 1 and field.value().microsecond == 123456
    assert field.value().fold == 1
    assert parse_datetime('2026-11-04T08:00Z', PREFERENCES, START) == START
    with pytest.raises(ValueError):
        parse_datetime('4.11.2026 09:00 XYZ', PREFERENCES, START)
    field.setText('not a date')
    with pytest.raises(ValueError):
        field.value()


def test_field_all_day_hides_clock_without_moving_its_calendar_date(desktop):
    _, widgets = desktop
    field = DateTimeField(PREFERENCES)
    widgets.append(field)
    field.set_value(START)
    field.setAllDay(True)
    assert ':' not in field.text() and field.date().toPython() == START.date()
    assert field.value().hour == 0 and field.dateTime().time().hour() == 0
    field.setText('6.11.2026')
    assert field.value() == datetime(2026, 11, 6, tzinfo=ZONE)


def test_invalid_local_field_draft_remains_editable_and_fails_validation_on_read(desktop):
    _, widgets = desktop
    field = DateTimeField(PREFERENCES)
    widgets.append(field)
    field.set_value(datetime(2027, 3, 27, 2, 30, tzinfo=ZONE))
    field.setDate(field.date().addDays(1))
    assert '28.03.2027' in field.text()
    with pytest.raises(ValueError, match='clocks change'):
        field.value()
    field.setText('28.3.2027 3:30')
    field.commit()
    assert field.value().hour == 3


@pytest.mark.parametrize('text,duration', [('45', timedelta(minutes=45)), ('1h 30min', timedelta(minutes=90)),
                                         ('1:30', timedelta(minutes=90)), ('1,5h', timedelta(minutes=90)),
                                         ('2d 3h', timedelta(days=2, hours=3)), ('0.5min', timedelta(seconds=30))])
def test_duration_formats_are_whole_values(text, duration):
    assert parse_duration(text) == duration
    assert parse_duration(format_duration(duration)) == duration


def test_all_day_durations_and_invalid_precision_are_not_silently_rounded():
    assert parse_duration('3', all_day=True) == timedelta(days=3)
    assert format_duration(timedelta(days=3), all_day=True) == '3 days'
    for text in ('0', '-1h', '1:99', 'nonsense', '1.00000001s'):
        with pytest.raises(ValueError):
            parse_duration(text)
    with pytest.raises(ValueError):
        parse_duration('3h', all_day=True)


def test_picker_apply_cancel_and_start_changes_preserve_duration(desktop):
    picker = make_picker(desktop)
    picker.choose_date(START.date() + timedelta(days=2))
    assert picker.draft_start == START + timedelta(days=2)
    assert picker.duration == timedelta(hours=1)
    picker.reject()
    assert picker.result_start == START and picker.result_end == START + timedelta(hours=1)
    picker = make_picker(desktop)
    picker.set_start(START + timedelta(hours=2))
    picker.apply_button.click()
    assert picker.result() == QDialog.DialogCode.Accepted
    assert picker.result_start == START + timedelta(hours=2)
    assert picker.result_end == START + timedelta(hours=3)


def test_earlier_end_moves_start_and_keeps_duration(desktop):
    picker = make_picker(desktop, endpoint='end')
    picker.choose_date(START.date() - timedelta(days=1))
    assert picker.draft_end == START - timedelta(days=1) + timedelta(hours=1)
    assert picker.draft_start == START - timedelta(days=1)
    assert picker.duration == timedelta(hours=1)
    picker.set_end(picker.draft_start - timedelta(minutes=30))
    assert picker.duration == timedelta(hours=1)
    assert picker.draft_start.hour == 7 and picker.draft_start.minute == 30


def test_all_day_picker_uses_inclusive_end_but_returns_exclusive_end(desktop):
    start = START.replace(hour=0)
    picker = make_picker(desktop, start, start + timedelta(days=3), endpoint='end', all_day=True)
    assert not picker.strip.isVisible() and not picker.full_day_button.isVisible()
    assert picker.view_day == start.date() + timedelta(days=2)
    assert picker.day_count.text() == '3 days'
    picker.choose_date(start.date() - timedelta(days=1))
    assert picker.draft_end == start and picker.draft_start == start - timedelta(days=3)
    assert picker.duration == timedelta(days=3)
    picker.accept()
    assert picker.result_end == start


def test_multiday_endpoint_views_have_only_real_handles(desktop):
    start = START.replace(hour=21)
    end = start + timedelta(hours=12)
    first = make_picker(desktop, start, end)
    last = make_picker(desktop, start, end, endpoint='end')
    assert first.strip.continues_after and not first.strip.continues_before
    assert not first.strip.start_handle().isEmpty() and first.strip.end_handle().isEmpty()
    assert last.strip.continues_before and not last.strip.continues_after
    assert last.strip.start_handle().isEmpty() and not last.strip.end_handle().isEmpty()
    first.set_start(start + timedelta(hours=1), preserve_duration=False)
    last.set_end(end + timedelta(hours=1))
    assert first.draft_end == end and last.draft_start == start


def test_range_pointer_gestures_and_end_hover_show_duration(desktop):
    app, _ = desktop
    picker = make_picker(desktop, endpoint='end')
    strip = picker.strip
    point = lambda minute: QPoint(round(strip.gutter + 22), round(strip.y_for(minute)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=point(13 * 60))
    QTest.mouseMove(strip, point(14 * 60 + 30))
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=point(14 * 60 + 30))
    settle(app)
    assert picker.draft_start.hour == 13 and picker.duration == timedelta(minutes=90)
    position = point(15 * 60)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position), QPointF(strip.mapToGlobal(position)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(strip, event)
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert '15:00' in captions and '2h' in captions


@pytest.mark.parametrize('endpoint,minute,clock,duration', [
    ('start', 8 * 60 + 30, '08:30', '1h 30min'),
    ('end', 11 * 60 + 30, '11:30', '2h 30min'),
])
def test_resizing_keeps_original_range_and_live_endpoint_duration_feedback(desktop, endpoint, minute, clock, duration):
    app, _ = desktop
    picker = make_picker(desktop)
    strip = picker.strip
    handle = strip.start_handle() if endpoint == 'start' else strip.end_handle()
    point = handle.center().toPoint()
    target = QPoint(point.x(), round(strip.y_for(minute)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(strip, target)
    settle(app)
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert 'Before: 09:00–10:00 · 1h' in captions
    assert clock in captions and duration in captions
    # Pointer feedback is a transaction until the gesture is committed.
    assert picker.draft_start == START and picker.duration == timedelta(hours=1)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    settle(app)
    assert format_duration(picker.duration) == duration
    assert strip.before_caption == ''


def test_short_events_retain_both_clocks_and_have_separate_resize_targets(desktop):
    app, _ = desktop
    picker = make_picker(desktop, end=START + timedelta(minutes=15))
    strip = picker.strip
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert {'09:00', '09:15', '15min'} <= set(captions)
    point = strip.end_handle().center().toPoint()
    target = QPoint(point.x(), round(strip.y_for(10 * 60)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    settle(app)
    assert picker.draft_start == START and picker.duration == timedelta(hours=1)


def test_full_day_shortcut_is_remappable_and_works_from_calendar_or_timeline(desktop):
    app, _ = desktop
    picker = make_picker(desktop, full_day_shortcut='H')
    picker.activateWindow()
    picker.calendar.setFocus()
    settle(app)
    QTest.keyClick(picker.calendar, Qt.Key.Key_H)
    settle(app)
    assert picker.full_day and picker.full_day_button.isChecked()
    assert (picker.strip.first_minute, picker.strip.last_minute) == (0, 1440)
    assert '(H)' in picker.full_day_button.toolTip()
    picker.strip.setFocus()
    settle(app)
    QTest.keyClick(picker.strip, Qt.Key.Key_F)
    settle(app)
    assert picker.full_day
    QTest.keyClick(picker.strip, Qt.Key.Key_H)
    settle(app)
    assert not picker.full_day and not picker.full_day_button.isChecked()
    assert (picker.strip.first_minute, picker.strip.last_minute) != (0, 1440)


def test_full_day_shortcut_respects_year_text_entry_and_can_be_disabled(desktop):
    app, _ = desktop
    picker = make_picker(desktop, full_day_shortcut='2')
    picker.activateWindow()
    picker.month.show_year_popup()
    settle(app)
    field = picker.month.year_popup.year_input
    QTest.keyClicks(field, '2028')
    settle(app)
    assert field.text() == '2028' and not picker.full_day
    picker.month.year_popup.close()
    picker = make_picker(desktop, full_day_shortcut='')
    picker.activateWindow()
    picker.strip.setFocus()
    settle(app)
    QTest.keyClick(picker.strip, Qt.Key.Key_F)
    settle(app)
    assert not picker.full_day
    all_day = make_picker(desktop, START.replace(hour=0), START.replace(hour=0) + timedelta(days=1), all_day=True)
    assert not all_day.full_day_shortcut.isEnabled()


def test_dst_gap_and_invalid_ranges_keep_the_valid_draft(desktop):
    start = datetime(2027, 3, 28, 1, 30, tzinfo=ZONE)
    end = datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    picker = make_picker(desktop, start, end)
    assert not picker.set_range(start.replace(hour=2), end)
    assert picker.draft_start == start and picker.draft_end == end
    assert 'clocks change' in picker.error.text()
    assert not picker.set_range(end, start)
    assert picker.draft_start == start
    with pytest.raises(ValueError, match='clocks change'):
        parse_datetime('28.3.2027 2:30', PREFERENCES, start)
    after = datetime(2027, 3, 28, 4, tzinfo=ZONE)
    picker = make_picker(desktop, after, after + timedelta(minutes=90), endpoint='end')
    # Earlier-end recovery uses the event's wall clock even if the caller
    # supplies a UTC instant; it must not silently skip the missing hour.
    assert not picker.set_end(datetime(2027, 3, 28, 1, 30, tzinfo=timezone.utc))
    assert picker.draft_start == after
