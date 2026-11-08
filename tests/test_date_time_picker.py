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


def test_timeline_end_choices_and_hover_show_duration(desktop):
    app, _ = desktop
    picker = make_picker(desktop, endpoint='end')
    strip = picker.strip
    point = lambda minute: QPoint(round(strip.gutter - 10), round(strip.y_for(minute)))
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point(14 * 60 + 30))
    settle(app)
    assert picker.draft_start == START and picker.duration == timedelta(hours=5, minutes=30)
    position = point(15 * 60)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position), QPointF(strip.mapToGlobal(position)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(strip, event)
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert '15:00' in captions and '6h' in captions


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


def pointer_move(strip, point):
    point = QPointF(point)
    event = QMouseEvent(QEvent.Type.MouseMove, point, QPointF(strip.mapToGlobal(point.toPoint())),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(strip, event)


@pytest.mark.parametrize('endpoint,minute,expected', [
    ('start', 9 * 60 + 25, (START + timedelta(minutes=25), START + timedelta(minutes=85))),
    ('end', 9 * 60 + 25, (START, START + timedelta(minutes=25))),
    ('end', 8 * 60 + 40, (START - timedelta(minutes=80), START - timedelta(minutes=20))),
])
def test_timeline_hover_is_the_exact_uncommitted_click_result_even_inside_the_event(desktop, endpoint, minute, expected):
    app, _ = desktop
    picker = make_picker(desktop, endpoint=endpoint)
    strip = picker.strip
    point = QPoint(round(strip.gutter - 12), round(strip.y_for(minute)))
    pointer_move(strip, point)
    assert strip.active_range == expected
    assert picker.draft_start == START and picker.draft_end == START + timedelta(hours=1)
    assert strip._hover == minute
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    captions = {call.args[2] for call in draw.call_args_list}
    assert PREFERENCES.format_time(expected[0]) in captions
    assert PREFERENCES.format_time(expected[1]) in captions
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point)
    settle(app)
    assert (picker.draft_start, picker.draft_end) == expected


def test_timeline_clock_labels_choose_the_picker_endpoint_instead_of_resizing_the_box(desktop):
    picker = make_picker(desktop)
    strip = picker.strip
    end_label = next(rect for value, rect in strip.clock_labels() if value == picker.draft_end)
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=end_label.center().toPoint())
    assert picker.draft_start == START + timedelta(hours=1)
    assert picker.draft_end == START + timedelta(hours=2)


def test_empty_event_lane_does_not_create_or_change_a_range(desktop):
    picker = make_picker(desktop)
    strip = picker.strip
    point = QPoint(round(strip.gutter + 24), round(strip.y_for(13 * 60)))
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point)
    assert (picker.draft_start, picker.draft_end) == (START, START + timedelta(hours=1))


def test_short_event_clocks_stay_left_and_do_not_overlap(desktop):
    picker = make_picker(desktop, end=START + timedelta(minutes=5))
    picker.set_full_day(True)
    strip = picker.strip
    labels = strip.clock_labels()
    assert [value for value, _ in labels] == [START, START + timedelta(minutes=5)]
    assert all(rect.right() < strip.gutter for _value, rect in labels)
    assert not labels[0][1].intersects(labels[1][1])
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        strip.grab()
    end_calls = [call for call in draw.call_args_list if call.args[2] == '09:05']
    assert len(end_calls) == 1 and end_calls[0].args[1].right() < strip.gutter


def test_pointer_and_keyboard_use_five_minute_steps(desktop):
    app, _ = desktop
    picker = make_picker(desktop)
    strip = picker.strip
    point = QPoint(round(strip.gutter - 10), round(strip.y_for(9 * 60 + 5)))
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point)
    assert picker.draft_start == START + timedelta(minutes=5)
    strip.setFocus()
    QTest.keyClick(strip, Qt.Key.Key_Down)
    assert picker.draft_start == START + timedelta(minutes=10)
    QTest.keyClick(strip, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert picker.draft_start == START + timedelta(minutes=11)
    QTest.keyClick(strip, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert picker.draft_start == START - timedelta(minutes=49)
    picker = make_picker(desktop)
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    target = QPoint(origin.x(), round(origin.y() + strip.y_for(545) - strip.y_for(540)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    settle(app)
    assert picker.draft_start == START + timedelta(minutes=5)
    assert picker.duration == timedelta(hours=1)


@pytest.mark.parametrize('direction', [-1, 1])
def test_dragging_past_the_visible_hours_pans_without_committing_until_release(desktop, direction):
    app, _ = desktop
    picker = make_picker(desktop)
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    first, last = strip.first_minute, strip.last_minute
    target = QPoint(origin.x(), round(strip.axis_bottom + 3 if direction > 0 else strip.axis_top - 3))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, target)
    first_preview = strip.active_range
    QTest.qWait(190)
    settle(app)
    assert strip.last_minute > last if direction > 0 else strip.first_minute < first
    assert strip.active_range[0] > first_preview[0] if direction > 0 else strip.active_range[0] < first_preview[0]
    assert strip.active_range[1] - strip.active_range[0] == timedelta(hours=1)
    assert picker.draft_start == START
    preview = strip.active_range
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    settle(app)
    assert (picker.draft_start, picker.draft_end) == preview


def test_cancelling_an_edge_drag_stops_axis_updates_and_preserves_the_range(desktop):
    app, _ = desktop
    picker = make_picker(desktop)
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, QPoint(origin.x(), round(strip.axis_bottom + 3)))
    QTest.qWait(90)
    picker.reject()
    last = strip.last_minute
    QTest.qWait(100)
    settle(app)
    assert strip.last_minute == last
    assert (picker.result_start, picker.result_end) == (START, START + timedelta(hours=1))


def test_multiday_timeline_hover_edits_only_the_visible_endpoint(desktop):
    start = START.replace(hour=21)
    end = start + timedelta(hours=12)
    picker = make_picker(desktop, start, end)
    strip = picker.strip
    point = QPoint(round(strip.gutter - 10), round(strip.y_for(22 * 60)))
    pointer_move(strip, point)
    assert strip.active_range == (start + timedelta(hours=1), end)
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point)
    assert picker.draft_end == end and picker.duration == timedelta(hours=11)


def test_invalid_dst_hover_and_click_keep_the_last_valid_range(desktop):
    start = datetime(2027, 3, 28, 1, 30, tzinfo=ZONE)
    end = datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    picker = make_picker(desktop, start, end)
    strip = picker.strip
    point = QPoint(round(strip.gutter - 10), round(strip.y_for(2 * 60 + 30)))
    pointer_move(strip, point)
    assert strip.active_range == (start, end)
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=point)
    assert (picker.draft_start, picker.draft_end) == (start, end)
    assert 'clocks change' in picker.error.text()


def test_five_minute_event_can_be_moved_using_its_body_and_resized_using_its_edge(desktop):
    picker = make_picker(desktop, end=START + timedelta(minutes=5))
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    assert not strip.start_handle().contains(origin) and not strip.end_handle().contains(origin)
    target = QPoint(origin.x(), round(origin.y() + strip.y_for(550) - strip.y_for(540)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    assert picker.draft_start == START + timedelta(minutes=10)
    assert picker.duration == timedelta(minutes=5)
    edge = strip.end_handle().center().toPoint()
    target = QPoint(edge.x(), round(strip.y_for(9 * 60 + 25)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=edge)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    assert picker.duration == timedelta(minutes=15)


def test_moving_a_range_retains_its_repeated_hour_choices(desktop):
    start = datetime(2026, 10, 25, 2, 20, tzinfo=ZONE, fold=0)
    end = datetime(2026, 10, 25, 2, 40, tzinfo=ZONE, fold=1)
    picker = make_picker(desktop, start, end)
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    target = QPoint(origin.x(), round(origin.y() + strip.y_for(145) - strip.y_for(140)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    assert picker.draft_start == start.replace(minute=25) and picker.draft_start.fold == 0
    assert picker.draft_end == end.replace(minute=45) and picker.draft_end.fold == 1
