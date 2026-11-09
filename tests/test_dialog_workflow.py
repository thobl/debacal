"""Keyboard-first forms and safe preview-driven custom schedule creation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import Future
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from debacal.dialogs import CalendarDialog, EventDialog
from debacal.choice_control import ChoiceControl
from debacal.discovery import DiscoveredCalendar
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import base_schedule_starts, expand_series, occurrence, recurrence_key
from debacal.shortcuts import KeyboardShortcutsEditor
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
BOUNDS = datetime(2025, 1, 1, tzinfo=ZONE), datetime(2030, 1, 1, tzinfo=ZONE)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    store = Store(':memory:')
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def show(dialog):
            widgets.append(dialog)
            dialog.show()
            dialog.activateWindow()
            app.processEvents()
            return dialog

        def event(source=None, **kwargs):
            selected = occurrence(source, source.start) if source else None
            dialog = EventDialog(store.list_calendars(), source.start if source else START,
                                 event=selected, series=source, edit_scope='group', **kwargs)
            if source is None:
                dialog.title.setText('Keyboard event')
            return show(dialog)

        def click(dialog, day):
            dialog.preview.show_month(day)
            app.processEvents()
            assert dialog.preview.date_editing
            rect = dialog.preview.grid.cell_rect(day)
            QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
            app.processEvents()
        yield app, store, show, event, click
        QApplication.clipboard().clear()
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def master(rule='FREQ=WEEKLY;COUNT=4', start=START):
    return Event('series', 'local', 'Weekly series', start, start + timedelta(hours=1),
                 recurrence_rule=rule, timezone_id=ZONE.key)


def utc(starts):
    return {value.astimezone(timezone.utc) for value in starts}


def test_event_tab_order_mnemonics_and_plain_title_field(desktop):
    _, _, _, create, _ = desktop
    dialog = create()
    assert dialog.title.placeholderText() == '' and dialog.title.hasFocus()
    dialog.calendar.setFocus()
    QTest.keyClick(dialog.calendar, Qt.Key.Key_Tab)
    assert dialog.title.hasFocus()
    for before, after in ((dialog.title, dialog.location), (dialog.location, dialog.all_day),
                          (dialog.all_day, dialog.start), (dialog.start, dialog.end),
                          (dialog.end, dialog.duration)):
        QTest.keyClick(before, Qt.Key.Key_Tab)
        assert after.hasFocus()
    QTest.keyClick(dialog.duration, Qt.Key.Key_Backtab)
    assert dialog.end.hasFocus()
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_L, Qt.KeyboardModifier.AltModifier)
    assert dialog.location.hasFocus()
    labels = {label.buddy(): label for label in dialog.findChildren(QLabel) if label.buddy()}
    assert '&Title' in labels[dialog.title].text()
    assert '&Location' in labels[dialog.location].text()


@pytest.mark.parametrize('font_scale', [100, 140])
@pytest.mark.parametrize('width', [760, 1120])
def test_schedule_mode_labels_remain_readable_in_compact_and_large_text_editors(desktop, font_scale, width):
    app, _, _, create, _ = desktop
    dialog = create(preferences=Preferences(font_scale=font_scale, locale_name='en_GB'))
    dialog.resize(width, 700)
    app.processEvents()
    for identity, caption in [('single', 'Single'), ('repeat', 'Series'), ('alternatives', 'Choices')]:
        button = dialog.schedule_mode.buttons[identity]
        assert button.isVisible() and button.text() == caption
        assert button.fontMetrics().horizontalAdvance(caption) + 4 <= button.width()
        assert button.toolTip() != caption
    assert 'removes the other choices' in dialog.schedule_mode.buttons['alternatives'].toolTip()


def test_required_choice_labels_take_priority_over_icons_and_restore_them_when_space_returns(desktop):
    app, _, show, _, _ = desktop
    choices = ChoiceControl([('Single', 'single'), ('Series', 'repeat'), ('Choices', 'alternatives')],
        Preferences(), icons={'single': 'single', 'repeat': 'series', 'alternatives': 'alternatives'},
        always_show_labels=True)
    choices.resize(choices.minimumSizeHint().width(), choices.height())
    show(choices)
    assert [button.text() for button in choices.buttons.values()] == ['Single', 'Series', 'Choices']
    assert all(button.icon().isNull() for button in choices.buttons.values())
    choices.resize(600, choices.height())
    app.processEvents()
    assert [button.text() for button in choices.buttons.values()] == ['Single', 'Series', 'Choices']
    assert all(not button.icon().isNull() for button in choices.buttons.values())


def test_enter_saves_form_inputs_but_notes_enter_inserts_newline_and_ctrl_enter_saves(desktop):
    _, _, _, create, _ = desktop
    simple = create()
    QTest.keyClick(simple.title, Qt.Key.Key_Return)
    assert simple.result() == QDialog.DialogCode.Accepted
    dialog = create()
    dialog.description.setFocus()
    QTest.keyClicks(dialog.description, 'First line')
    QTest.keyClick(dialog.description, Qt.Key.Key_Return)
    QTest.keyClicks(dialog.description, 'Second line')
    assert dialog.result() == QDialog.DialogCode.Rejected
    QTest.keyClick(dialog.description, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_events[0].description == 'First line\nSecond line'


def test_enter_commits_pending_datetime_text_before_saving(desktop):
    _, _, _, create, _ = desktop
    dialog = create()
    desired = dialog.start.dateTime().addSecs(1800)
    field = dialog.start.lineEdit()
    field.setText(desired.toString(dialog.start.displayFormat()))
    field.setFocus()
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert dialog.result_events[0].start == START + timedelta(minutes=30)


def test_focused_preview_button_uses_enter_without_saving_the_event(desktop):
    _, _, _, create, _ = desktop
    dialog = create()
    previous = dialog.preview.current_month
    dialog.preview.next_button.setFocus()
    QTest.keyClick(dialog.preview.next_button, Qt.Key.Key_Return)
    assert dialog.preview.current_month != previous
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_save_focuses_the_invalid_title_datetime_duration_or_sequence_field(desktop):
    _, _, _, create, _ = desktop
    title = create()
    title.title.clear()
    title.location.setFocus()
    title.save()
    assert title.title.hasFocus() and 'title' in title.error.text()
    end = create()
    end.end.setText('not a date')
    end.title.setFocus()
    end.save()
    assert end.end.hasFocus() and end.error.text()
    duration = create()
    duration.duration.setText('0min')
    duration.title.setFocus()
    duration.save()
    assert duration.duration.hasFocus() and duration.error.text()
    sequence = create()
    sequence.schedule_mode.setCurrentData('repeat')
    sequence.repeat.setCurrentData('RDATE')
    sequence.pattern.setText('3*(+2w')
    sequence.title.setFocus()
    sequence.save()
    assert sequence.pattern.hasFocus() and 'closing' in sequence.error.text()


def test_entering_an_earlier_end_moves_the_start_and_preserves_duration(desktop):
    _, _, _, create, _ = desktop
    dialog = create()
    dialog.end.setText('4.11.2026 8:30')
    field = dialog.end.lineEdit()
    field.setFocus()
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    result = dialog.result_events[0]
    assert result.start == START - timedelta(minutes=90)
    assert result.end == START - timedelta(minutes=30)


def test_remapped_ctrl_enter_mode_has_priority_over_optional_save_shortcut(desktop):
    _, _, _, create, _ = desktop
    dialog = create(master(), scope_bindings={'scope_all': 'Ctrl+Return'})
    dialog.scope.setCurrentIndex(dialog.scope.findData('single'))
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dialog.scope.currentData() == 'group'
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_clicking_once_preview_adds_a_date_and_detects_a_regular_sequence(desktop):
    _, _, _, create, click = desktop
    dialog = create()
    assert dialog.schedule_mode.currentData() == 'single'
    click(dialog, START.date() + timedelta(days=3))
    assert dialog.schedule_mode.currentData() == 'repeat'
    assert dialog.repeat.currentData() == 'DAILY'
    assert dialog.interval.value() == 3 and dialog.repeat_count.value() == 2
    assert dialog.sequence_starts() == [START, START + timedelta(days=3)]
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert utc(value.start for value in expand_series(dialog.result_events[0], *BOUNDS)) == utc([START, START + timedelta(days=3)])
    assert dialog.result_events[0].recurrence_rule


def test_finite_standard_preview_conversion_preserves_base_dates_exclusions_and_exception_start(desktop):
    _, store, _, create, click = desktop
    source = master()
    starts = base_schedule_starts(source)
    moved = replace(occurrence(source, starts[2]), start=starts[2] + timedelta(hours=2),
                    end=starts[2] + timedelta(hours=3))
    source = replace(source, excluded_dates=(recurrence_key(starts[1]),),
                     recurrence_overrides={moved.recurrence_id: moved.to_dict()})
    store.apply('Create', [source])
    before = utc(value.start for value in expand_series(source, *BOUNDS))
    dialog = create(source)
    assert dialog.repeat.currentData() == 'RDATE'
    extra = START + timedelta(days=3)
    click(dialog, extra.date())
    assert dialog.repeat.currentData() == 'RDATE' and dialog.schedule_replacement
    # The editable custom sequence contains actual dates, excluding the deleted
    # occurrence; exception identities must still survive the conversion.
    assert utc(dialog.sequence_starts()) == before | utc([extra])
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    store.edit_series(dialog.edit_reference, dialog.result_events[0], replace_schedule=dialog.schedule_replacement)
    changed = store.get_event(source.id)
    assert changed.excluded_dates == source.excluded_dates
    assert set(changed.recurrence_overrides) == {moved.recurrence_id}
    assert utc(value.start for value in expand_series(changed, *BOUNDS)) == before | utc([extra])


@pytest.mark.parametrize('rule', ['FREQ=WEEKLY', 'FREQ=WEEKLY;COUNT=501'])
def test_unbounded_and_large_preview_clicks_preserve_the_rule_and_add_a_date(desktop, rule):
    _, store, _, create, click = desktop
    source = master(rule)
    store.apply('Create', [source])
    original = source.to_dict()
    dialog = create(source)
    extra = START + timedelta(days=3)
    click(dialog, extra.date())
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    result = dialog.result_events[0]
    assert result.recurrence_rule == source.recurrence_rule
    assert recurrence_key(extra) in result.recurrence_dates
    store.edit_series(dialog.edit_reference, result, replace_schedule=dialog.schedule_replacement)
    saved = store.get_event(source.id)
    assert utc(value.start for value in expand_series(saved, *BOUNDS)) == (
        utc(value.start for value in expand_series(source, *BOUNDS)) | utc([extra]))
    assert source.to_dict() == original


def test_calendar_form_tab_order_and_invalid_field_focus(desktop):
    _, _, show, _, _ = desktop
    dialog = show(CalendarDialog())
    assert dialog.name.hasFocus()
    QTest.keyClick(dialog.name, Qt.Key.Key_Tab)
    assert dialog.kind.hasFocus()
    QTest.keyClick(dialog.kind, Qt.Key.Key_Tab)
    assert dialog.color.hasFocus()
    QTest.keyClick(dialog.color, Qt.Key.Key_Tab)
    assert dialog.choose_color_button.hasFocus()
    dialog.save()
    assert dialog.name.hasFocus() and 'name' in dialog.error.text()
    dialog.name.setText('Subscription')
    dialog.kind.setCurrentIndex(dialog.kind.findData('ics'))
    dialog.url.setText('https://user:secret@example.test/')
    dialog.save()
    assert dialog.url.hasFocus() and 'secret' not in dialog.error.text()


def test_calendar_color_button_enter_activates_focused_action_instead_of_save(desktop):
    _, _, show, _, _ = desktop
    dialog = show(CalendarDialog())
    dialog.name.setText('Local calendar')
    dialog.choose_color_button.setFocus()
    with patch('debacal.dialogs.QColorDialog.getColor', return_value=QColor()) as picker:
        QTest.keyClick(dialog.choose_color_button, Qt.Key.Key_Return)
    assert picker.call_count == 1 and dialog.result() == QDialog.DialogCode.Rejected


def test_existing_remote_calendar_requires_name_and_enter_saves_password_edit(desktop):
    _, _, show, _, _ = desktop
    source = Calendar('remote', 'Remote', '#123456', kind='caldav', url='https://cloud.example.test/cal/',
                      username='synthetic-user', server_color='#123456')
    dialog = show(CalendarDialog(source))
    dialog.name.clear()
    dialog.save()
    assert dialog.name.hasFocus() and 'name' in dialog.error.text()
    dialog.name.setText('Renamed')
    dialog.password.setText('synthetic-app-password')
    dialog.password.setFocus()
    QTest.keyClick(dialog.password, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_calendar.name == 'Renamed'
    assert dialog.result_calendar.server_color == source.server_color
    assert dialog.result_calendar.url == source.url


def test_caldav_enter_discovers_then_focuses_results_and_saves_checked_calendars(desktop):
    _, _, show, _, _ = desktop
    dialog = show(CalendarDialog())
    dialog.kind.setCurrentIndex(dialog.kind.findData('caldav'))
    dialog.url.setText('cloud.example.test')
    dialog.username.setText('synthetic-user')
    dialog.password.setText('synthetic-password')
    assert dialog.discover_button.isDefault() and not dialog.save_button.isDefault()
    pending = Future()
    with patch.object(dialog.discovery_executor, 'submit', return_value=pending) as submit:
        dialog.password.setFocus()
        QTest.keyClick(dialog.password, Qt.Key.Key_Return)
        assert submit.call_count == 1 and dialog.discovery_future is pending
        assert not dialog.save_button.isEnabled() and not dialog.discover_button.isEnabled()
        QTest.keyClick(dialog.password, Qt.Key.Key_Return)
        assert submit.call_count == 1 and dialog.result() == QDialog.DialogCode.Rejected
        pending.set_result([DiscoveredCalendar('Shared', 'https://cloud.example.test/cal/'),
                            DiscoveredCalendar('Other', 'https://cloud.example.test/other/')])
        dialog.poll_discovery()
    assert dialog.save_button.isDefault() and not dialog.discover_button.isDefault()
    assert dialog.discovered_list.hasFocus() and dialog.discovered_list.currentRow() == 0
    dialog.discovered_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    dialog.discovered_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    QTest.keyClick(dialog.discovered_list, Qt.Key.Key_Return)
    assert dialog.discovered_list.hasFocus() and 'at least one' in dialog.error.text()
    QTest.keyClick(dialog.discovered_list, Qt.Key.Key_Space)
    QTest.keyClick(dialog.discovered_list, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.result_calendars) == 1 and dialog.result_calendar.name == 'Shared'


def test_discovery_identity_change_restores_discover_default_and_cannot_save_stale_results(desktop):
    _, _, show, _, _ = desktop
    dialog = show(CalendarDialog())
    dialog.kind.setCurrentIndex(dialog.kind.findData('caldav'))
    dialog.url.setText('https://cloud.example.test/')
    first, second = Future(), Future()
    first.set_result([DiscoveredCalendar('First', 'https://cloud.example.test/cal/')])
    with patch.object(dialog.discovery_executor, 'submit', side_effect=[first, second]) as submit:
        dialog.discover()
        dialog.poll_discovery()
        assert dialog.save_button.isDefault()
        dialog.username.setText('different-user')
        assert dialog.discover_button.isDefault() and not dialog.save_button.isDefault()
        dialog.url.setFocus()
        QTest.keyClick(dialog.url, Qt.Key.Key_Return)
        assert submit.call_count == 2 and dialog.discovery_future is second
        assert dialog.result() == QDialog.DialogCode.Rejected
    second.cancel()


def test_grouped_shortcut_editor_uses_collision_validation(desktop):
    _, _, show, _, _ = desktop
    bindings = {'scope_single': 'F1', 'scope_all': 'F2', 'scope_following': 'F3', 'edit_event': 'E'}
    editor = show(KeyboardShortcutsEditor(bindings, bindings))
    editor.fields['edit_event'].setKeySequence('F2')
    with pytest.raises(ValueError, match='unique'):
        editor.value()
    editor.restore_defaults()
    assert editor.value() == bindings
