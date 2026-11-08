"""The editor distinguishes exact clock durations from nominal calendar days."""
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.date_time_fields import DateTimeField, format_event_duration, parse_datetime, parse_event_duration
from debacal.date_time_picker import DateTimeRangePicker
from debacal.event_dialog import EventDialog
from debacal.event_duration import add_duration, add_elapsed, elapsed_duration
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_event


ZONE = ZoneInfo('Europe/Berlin')
DISPLAY_ZONE = ZoneInfo('America/New_York')
PREFERENCES = Preferences(locale_name='en_GB', date_format='european', time_format='24h')
CALENDARS = [Calendar('local', 'Personal', '#587c67')]


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        yield app, owned
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def editor(desktop, event=None, *, start=None, display_zone=ZONE, preferences=PREFERENCES):
    _, owned = desktop
    with patch('debacal.event_dialog.local_zone', return_value=display_zone):
        value = EventDialog(CALENDARS, event.start if event else start, event.end if event else None,
                            event=event, preferences=preferences)
    owned.append(value)
    value.title.setText('An appointment')
    return value


def nominal_event():
    start = datetime(2027, 3, 26, 9, tzinfo=ZONE)
    return Event('nominal', 'local', 'Nominal day', start, add_duration(start, 'P1D'),
                 duration_spec='P1D', timezone_id='Europe/Berlin')


@pytest.mark.parametrize('text,spec,seconds', [('1d', 'P1D', 86400), ('1 week', 'P7D', 604800),
    ('24h', '', 86400), ('1440min', '', 86400), ('1d 24h', 'P1DT24H', 172800), ('0.5d', '', 43200)])
def test_written_duration_units_distinguish_calendar_days_and_elapsed_hours(text, spec, seconds):
    duration, actual = parse_event_duration(text)
    assert actual == spec and duration == timedelta(seconds=seconds)


@pytest.mark.parametrize('month,day,count,crossing_day', [(3, 1, 40, 28), (10, 1, 40, 24)])
@pytest.mark.parametrize('existing', [False, True])
def test_schedule_document_duration_updates_controls_save_reopen_and_dst_projection(
        desktop, month, day, count, crossing_day, existing):
    start = datetime(2026, month, day, 9, tzinfo=ZONE)
    source = Event('document', 'local', 'Document duration', start, start + timedelta(days=1))
    value = editor(desktop, source if existing else None, start=start)
    value.pattern.setText(f'schedule{{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT={count}"}}')
    assert not value.error.text(), value.error.text()
    assert value.duration_spec == 'P1D' and value.duration.text() == '1d'
    assert value.value(value.end) == start + timedelta(days=1)
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.duration_spec == 'P1D'
    reopened = editor(desktop, saved)
    assert reopened.duration_spec == 'P1D' and reopened.duration.text() == '1d'
    restored, = reopened.build_events()
    query = start.replace(day=crossing_day)
    item, = expand_event(restored, query, query + timedelta(days=1))
    assert item.start.hour == item.end.hour == 9
    assert elapsed_duration(item.start, item.end) == timedelta(hours=23 if month == 3 else 25)


@pytest.mark.parametrize('month,crossing_day', [(3, 28), (10, 24)])
def test_schedule_document_can_remove_nominal_duration_without_changing_primary_range(desktop, month, crossing_day):
    start = datetime(2026, month, 1, 9, tzinfo=ZONE)
    source = Event('document', 'local', 'Document duration', start, start + timedelta(days=1),
                   duration_spec='P1D', recurrence_rule='FREQ=DAILY;COUNT=40')
    value = editor(desktop, source)
    value.pattern.setText('schedule{"rule":"FREQ=DAILY;COUNT=40"}')
    assert value.duration_spec == '' and value.duration.text() == '24h'
    saved, = value.build_events()
    assert saved.duration_spec == ''
    restored, = editor(desktop, saved).build_events()
    query = start.replace(day=crossing_day)
    item, = expand_event(restored, query, query + timedelta(days=1))
    assert elapsed_duration(item.start, item.end) == timedelta(hours=24)
    assert item.end.hour == (10 if month == 3 else 8)


def test_invalid_document_keeps_last_valid_duration_and_range(desktop):
    start = datetime(2026, 10, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.pattern.setText('schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}')
    before = value.value(value.start), value.value(value.end), value.duration_spec
    value.pattern.setText('schedule{"duration_spec":"P2D","rule":false}')
    assert (value.value(value.start), value.value(value.end), value.duration_spec) == before
    with pytest.raises(ValueError):
        value.build_events()


def test_duration_control_can_replace_document_nominal_intent_without_reintroducing_it_on_reopen(desktop):
    start = datetime(2026, 10, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.pattern.setText('schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}')
    value.duration.setText('24h')
    value.duration.editingFinished.emit()
    value.pattern.editingFinished.emit()
    assert value.duration_spec == ''
    saved, = value.build_events()
    assert saved.duration_spec == ''
    reopened = editor(desktop, saved)
    assert reopened.duration_spec == ''
    saved_again, = reopened.build_events()
    assert saved_again.duration_spec == ''
    query = start.replace(day=24)
    item, = expand_event(saved_again, query, query + timedelta(days=1))
    assert item.end.hour == 8


@pytest.mark.parametrize('month,crossing_day', [(3, 28), (10, 24)])
@pytest.mark.parametrize('method,hours', [('duration', 24), ('end', 25), ('picker', 26)])
@pytest.mark.parametrize('existing', [False, True])
def test_control_range_edits_update_document_before_revisiting_its_text(
        desktop, month, crossing_day, method, hours, existing):
    start = datetime(2026, month, 1, 9, tzinfo=ZONE)
    source = Event('document', 'local', 'Document', start, start + timedelta(days=1))
    value = editor(desktop, source if existing else None, start=start)
    value.pattern.setText('schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}')
    if method == 'duration':
        value.duration.setText('24h')
        value.duration.editingFinished.emit()
    elif method == 'end':
        value.end.set_value(add_elapsed(start, timedelta(hours=hours)))
        value.end.editingFinished.emit()
    else:
        value._accept_primary_range(start, add_elapsed(start, timedelta(hours=hours)), '')
    assert not value.error.text(), value.error.text()
    assert 'duration_spec' not in value.pattern.text()
    for _ in range(2):
        value.pattern.editingFinished.emit()
        assert value.duration_spec == ''
        assert elapsed_duration(value.value(value.start), value.value(value.end)) == timedelta(hours=hours)
    saved, = value.build_events()
    reopened = editor(desktop, saved)
    reopened.pattern.editingFinished.emit()
    restored, = reopened.build_events()
    query = start.replace(day=crossing_day)
    item, = [item for item in expand_event(restored, query, query + timedelta(days=1))
             if item.start.date() == query.date()]
    assert elapsed_duration(item.start, item.end) == timedelta(hours=hours)


@pytest.mark.parametrize('month,crossing_day,hours', [(3, 28, 47), (10, 24, 49)])
def test_duration_control_adds_nominal_intent_to_a_document_before_text_revisit(desktop, month, crossing_day, hours):
    start = datetime(2026, month, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.pattern.setText('schedule{"rule":"FREQ=DAILY;COUNT=40"}')
    value.duration.setText('2d')
    value.duration.editingFinished.emit()
    assert 'P2D' in value.pattern.text()
    value.pattern.editingFinished.emit()
    assert value.duration_spec == 'P2D'
    saved, = value.build_events()
    assert saved.duration_spec == 'P2D'
    query = start.replace(day=crossing_day)
    item, = [item for item in expand_event(saved, query, query + timedelta(days=1))
             if item.start.date() == query.date()]
    assert item.start.hour == item.end.hour == 9
    assert elapsed_duration(item.start, item.end) == timedelta(hours=hours)


def test_unchanged_range_leaves_document_text_cursor_and_undo_history_alone(desktop):
    start = datetime(2026, 10, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    text = 'schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}'
    value.pattern.setText(text)
    value.pattern.setCursorPosition(12)
    with patch.object(value.pattern, 'setText', wraps=value.pattern.setText) as rewritten:
        value._set_range(value.value(value.start), value.value(value.end), duration_spec=value.duration_spec)
        assert not rewritten.called
    assert value.pattern.text() == text and value.pattern.cursorPosition() == 12


def test_range_control_does_not_replace_pending_invalid_document_text_or_valid_draft(desktop):
    start = datetime(2026, 10, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.pattern.setText('schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}')
    valid_expression = value._draft.expression
    valid_master = value._draft._edited_master
    pending = 'schedule{"duration_spec":"P2D","rule":false}'
    value.pattern.setText(pending)
    value.duration.setText('24h')
    value.duration.editingFinished.emit()
    assert value.pattern.text() == pending
    assert value._draft.expression == valid_expression
    assert value._draft._edited_master == valid_master
    with pytest.raises(ValueError):
        value.build_events()


def test_pending_document_projection_uses_its_own_duration_without_mutating_editor_controls(desktop):
    start = datetime(2026, 10, 1, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.schedule_mode.setCurrentData('repeat')
    value.repeat.setCurrentData('RDATE')
    value.pattern.blockSignals(True)
    value.pattern.setText('schedule{"duration_spec":"P1D","rule":"FREQ=DAILY;COUNT=40"}')
    value.pattern.blockSignals(False)
    before = value.value(value.end), value.duration_spec
    saved, = value.build_events()
    assert saved.duration_spec == 'P1D' and saved.end == start + timedelta(days=1)
    assert (value.value(value.end), value.duration_spec) == before


def test_exact_twenty_four_hours_and_nominal_day_have_distinct_field_captions():
    start = datetime(2027, 3, 27, 9, tzinfo=ZONE)
    assert format_event_duration(start, add_elapsed(start, timedelta(hours=24))) == '24h'
    assert format_event_duration(start, add_duration(start, 'P1D'), duration_spec='P1D') == '1d'


@pytest.mark.parametrize('zone_name,parts', [('America/Sao_Paulo', (2018, 11, 4)),
    ('Africa/Cairo', (2026, 4, 24)), ('Pacific/Apia', (2011, 12, 30))])
def test_floating_all_day_date_can_start_on_a_missing_local_midnight(desktop, zone_name, parts):
    _, owned = desktop
    zone = ZoneInfo(zone_name)
    start = datetime(*parts, tzinfo=zone)
    end = start + timedelta(days=1)
    field = DateTimeField(PREFERENCES)
    owned.append(field)
    field.setAllDay(True)
    field.set_value(start)
    assert field.value().date() == start.date()
    date_text = f'{start.day}.{start.month}.{start.year}'
    assert parse_datetime(date_text, PREFERENCES, start, all_day=True).date() == start.date()
    with pytest.raises(ValueError, match='clocks change'):
        parse_datetime(date_text + ' 00:00', PREFERENCES, start)
    picker = DateTimeRangePicker(start, end, preferences=PREFERENCES, all_day=True)
    owned.append(picker)
    picker.accept()
    assert picker.result() == QDialog.DialogCode.Accepted
    assert picker.duration == timedelta(days=1)
    assert picker.set_start(end)
    assert picker.draft_end.date() == (end + timedelta(days=1)).date()
    assert picker.duration == timedelta(days=1)
    source = Event('floating', 'local', 'A floating date', start, end, all_day=True)
    value = editor(desktop, source)
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.start.date() == start.date() and saved.end.date() == end.date()


def test_save_detects_typed_all_day_date_change_even_when_two_midnights_share_an_instant(desktop):
    zone = ZoneInfo('Pacific/Apia')
    start = datetime(2011, 12, 30, tzinfo=zone)
    source = Event('floating', 'local', 'A floating date', start, start + timedelta(days=1), all_day=True)
    value = editor(desktop, source, display_zone=zone)
    value.start.blockSignals(True)
    value.start.set_value(start + timedelta(days=1))
    value.start.blockSignals(False)
    # Save can precede editingFinished. These different floating dates have
    # equal timestamps, so instant-only change detection would leave End stale.
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.start.date() == (start + timedelta(days=1)).date()
    assert saved.end.date() == (start + timedelta(days=2)).date()


def test_new_event_default_duration_crosses_dst_as_elapsed_minutes(desktop):
    start = datetime(2027, 3, 28, 1, 30, tzinfo=ZONE)
    value = editor(desktop, start=start)
    saved, = value.build_events()
    assert saved.end == datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    assert saved.duration_spec == '' and elapsed_duration(saved.start, saved.end) == timedelta(hours=1)
    assert value.duration.text() == '1h'


@pytest.mark.parametrize('method', ['typed', 'set_value'])
def test_invalid_initial_clock_can_be_corrected_without_losing_the_intended_duration(desktop, method):
    _, owned = desktop
    start = datetime(2027, 3, 28, 2, 30, tzinfo=ZONE)
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        value = EventDialog(CALENDARS, start, start + timedelta(hours=1), preferences=PREFERENCES)
    owned.append(value)
    value.title.setText('An appointment')
    assert '02:30' in value.start.text() and value.duration.text() == '1h'
    value.save()
    assert value.result() == QDialog.DialogCode.Rejected
    assert 'clocks change' in value.error.text()
    # 03:30 has the same UTC timestamp as the invalid 02:30 wall time. It is
    # still a real field change, and the intended hour must survive repair.
    if method == 'typed':
        value.start.setText('28.03.2027 03:30')
    else:
        value.start.set_value(start.replace(hour=3))
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.start == datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    assert saved.end == datetime(2027, 3, 28, 4, 30, tzinfo=ZONE)
    assert elapsed_duration(saved.start, saved.end) == timedelta(hours=1)


def test_new_valid_clock_range_still_rejects_zero_duration(desktop):
    start = datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
    with pytest.raises(ValueError, match='end must be after'):
        EventDialog(CALENDARS, start, start, preferences=PREFERENCES)


def test_picker_relocates_exact_range_across_spring_transition(desktop):
    _, owned = desktop
    start = datetime(2027, 3, 27, 1, 30, tzinfo=ZONE)
    picker = DateTimeRangePicker(start, start + timedelta(minutes=90), preferences=PREFERENCES)
    owned.append(picker)
    assert picker.set_start(start.replace(day=28))
    assert picker.draft_end == datetime(2027, 3, 28, 4, tzinfo=ZONE)
    assert picker.duration == timedelta(minutes=90)


def test_picker_nominal_days_follow_source_zone_until_explicit_end_edit(desktop):
    _, owned = desktop
    source = nominal_event()
    picker = DateTimeRangePicker(source.start.astimezone(DISPLAY_ZONE), source.end.astimezone(DISPLAY_ZONE),
        preferences=PREFERENCES, duration_spec=source.duration_spec, duration_zone=ZONE)
    owned.append(picker)
    target = source.start.replace(day=27).astimezone(DISPLAY_ZONE)
    assert picker.set_start(target)
    assert picker.draft_end.astimezone(ZONE) == datetime(2027, 3, 28, 9, tzinfo=ZONE)
    assert picker.duration == timedelta(hours=23) and picker.duration_spec == 'P1D'
    assert picker.set_end(add_elapsed(picker.draft_end, timedelta(hours=1)))
    assert picker.duration == timedelta(hours=24) and picker.duration_spec == ''
    picker.accept()
    assert picker.result_duration_spec == ''


def test_dragging_exact_range_through_repeated_hour_keeps_elapsed_duration(desktop):
    app, owned = desktop
    start = datetime(2026, 10, 25, 2, 10, tzinfo=ZONE)
    picker = DateTimeRangePicker(start, start.replace(minute=30), preferences=PREFERENCES)
    owned.append(picker)
    picker.show()
    app.processEvents()
    strip = picker.strip
    origin = strip.band_rect().center().toPoint()
    target = QPoint(origin.x(), round(origin.y() + strip.y_for(170) - strip.y_for(130)))
    QTest.mousePress(strip, Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(strip, target)
    QTest.mouseRelease(strip, Qt.MouseButton.LeftButton, pos=target)
    assert picker.draft_start == start.replace(minute=50)
    assert picker.draft_end.hour == 2 and picker.draft_end.minute == 10 and picker.draft_end.fold == 1
    assert picker.duration == timedelta(minutes=20)


def test_editor_preserves_imported_nominal_day_and_source_zone_when_start_changes(desktop):
    source = nominal_event()
    value = editor(desktop, source, display_zone=DISPLAY_ZONE)
    assert value.duration.text() == '1d'
    value._duration_changed()  # Leaving the unchanged field is not a resize.
    target = source.start.replace(day=27).astimezone(DISPLAY_ZONE)
    value.start.set_value(target)
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.start.timestamp() == target.timestamp()
    assert saved.end.astimezone(ZONE) == datetime(2027, 3, 28, 9, tzinfo=ZONE)
    assert saved.duration_spec == 'P1D' and saved.timezone_id == 'Europe/Berlin'


def test_series_editor_keeps_nominal_duration_on_later_dst_occurrences(desktop):
    source = replace(nominal_event(), recurrence_rule='FREQ=DAILY;COUNT=4')
    value = editor(desktop, source, display_zone=DISPLAY_ZONE)
    target = source.start.replace(day=27).astimezone(DISPLAY_ZONE)
    value.start.set_value(target)
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    items = expand_event(saved, datetime(2027, 3, 27, tzinfo=ZONE), datetime(2027, 4, 2, tzinfo=ZONE))
    assert len(items) == 4 and saved.duration_spec == 'P1D'
    assert all(item.start.astimezone(ZONE).hour == item.end.astimezone(ZONE).hour == 9 for item in items)
    assert [elapsed_duration(item.start, item.end) for item in items] == [timedelta(hours=23)] + [timedelta(days=1)] * 3


def test_occurrence_picker_resize_clears_only_that_occurrences_nominal_duration(desktop):
    source = replace(nominal_event(), recurrence_rule='FREQ=DAILY;COUNT=4')
    value = editor(desktop, source)
    item = next(item for item in expand_event(source, datetime(2027, 3, 27, tzinfo=ZONE),
                                             datetime(2027, 3, 28, tzinfo=ZONE)) if item.start.day == 27)
    value.edit_occurrence(item)
    picker = value.range_picker
    assert picker is not None
    assert picker.duration_spec == 'P1D'
    assert picker.set_end(add_elapsed(item.end, timedelta(hours=1)))
    picker.accept()
    saved, = value.build_events()
    edited = next(item for item in expand_event(saved, datetime(2027, 3, 27, tzinfo=ZONE),
                                               datetime(2027, 3, 28, tzinfo=ZONE)) if item.start.day == 27)
    assert saved.duration_spec == 'P1D' and edited.duration_spec == ''
    assert elapsed_duration(edited.start, edited.end) == timedelta(hours=24)


def test_added_occurrence_picker_can_choose_exact_duration_equal_to_the_nominal_range(desktop):
    source = replace(nominal_event(), recurrence_rule='FREQ=DAILY;COUNT=4')
    value = editor(desktop, source)
    value.add_occurrence()
    picker = value.range_picker
    start = source.start.replace(month=4, day=5)
    assert picker.set_start(start)
    assert picker.set_end(picker.draft_end)  # Explicit end means exact time, even on a 24-hour day.
    picker.accept()
    saved, = value.build_events()
    added = expand_event(saved, start, start + timedelta(days=1))[0]
    assert saved.duration_spec == 'P1D' and added.duration_spec == ''
    assert elapsed_duration(added.start, added.end) == timedelta(hours=24)


@pytest.mark.parametrize('kind', ['end', 'duration', 'preset'])
def test_editor_explicit_clock_duration_changes_clear_nominal_intent(desktop, kind):
    source = nominal_event()
    source = Event(source.id, source.calendar_id, source.title, source.start.replace(day=27),
        add_duration(source.start.replace(day=27), 'P1D'), duration_spec='P1D', timezone_id=source.timezone_id)
    prefs = Preferences(locale_name='en_GB', date_format='european', time_format='24h',
                        duration_presets_minutes=(60, 1440))
    value = editor(desktop, source, preferences=prefs)
    if kind == 'end':
        value.end.set_value(add_elapsed(source.end, timedelta(hours=1)))
    elif kind == 'duration':
        value.duration.setText('24h')
    else:
        value.duration_choices.setCurrentData(1440)
    value.save()
    assert value.result() == QDialog.DialogCode.Accepted, value.error.text()
    saved, = value.result_events
    assert saved.duration_spec == ''
    assert elapsed_duration(saved.start, saved.end) == timedelta(hours=24)
    assert value.duration.text() == '24h'


def test_explicit_day_input_creates_nominal_duration_and_all_day_toggle_restores_it(desktop):
    start = datetime(2027, 3, 27, 9, tzinfo=ZONE)
    value = editor(desktop, start=start)
    value.duration.setText('1d')
    value._duration_changed()
    saved, = value.build_events()
    assert saved.duration_spec == 'P1D' and elapsed_duration(saved.start, saved.end) == timedelta(hours=23)
    value.all_day.setChecked(True)
    floating, = value.build_events()
    assert floating.all_day and floating.duration_spec == ''
    assert (floating.end.date() - floating.start.date()).days == 2
    value.all_day.setChecked(False)
    restored, = value.build_events()
    assert restored.duration_spec == 'P1D'
    assert restored.start.timestamp() == saved.start.timestamp() and restored.end.timestamp() == saved.end.timestamp()
