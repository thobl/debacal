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
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import CalendarDialog, EventDialog, ShortcutsDialog
from debacal.discovery import DiscoveredCalendar
from debacal.models import Calendar, Event
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
    with patch('debacal.dialogs.local_zone', return_value=ZONE), \
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
    app, _, _, create, _ = desktop
    dialog = create()
    assert dialog.title.placeholderText() == '' and dialog.title.hasFocus()
    QTest.keyClick(dialog.title, Qt.Key.Key_Tab)
    assert dialog.calendar.hasFocus()
    QTest.keyClick(dialog.calendar, Qt.Key.Key_Tab)
    assert dialog.start.hasFocus()
    dialog.location.setFocus()
    QTest.keyClick(dialog.location, Qt.Key.Key_Tab)
    assert dialog.description.hasFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_Tab)
    assert dialog.repeat.hasFocus()
    QTest.keyClick(dialog.repeat, Qt.Key.Key_Backtab)
    assert dialog.description.hasFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_L, Qt.KeyboardModifier.AltModifier)
    assert dialog.location.hasFocus()
    assert dialog.form.labelForField(dialog.title).buddy() is dialog.title
    assert dialog.form.labelForField(dialog.repeat).buddy() is dialog.repeat


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


def test_save_focuses_the_invalid_title_end_or_sequence_field(desktop):
    _, _, _, create, _ = desktop
    title = create()
    title.title.clear()
    title.location.setFocus()
    title.save()
    assert title.title.hasFocus() and 'title' in title.error.text()
    end = create()
    end.end.setDateTime(end.start.dateTime().addSecs(-60))
    end.save()
    assert end.end.hasFocus() and 'End' in end.error.text()
    sequence = create()
    sequence.pattern.setText('3*(+2')
    sequence.title.setFocus()
    sequence.save()
    assert sequence.pattern.hasFocus() and 'closing' in sequence.error.text()
    rule = create()
    rule.repeat.setCurrentIndex(rule.repeat.findData('CUSTOM'))
    rule.custom_rule.setText('FREQ=UNKNOWN')
    rule.title.setFocus()
    rule.save()
    assert rule.custom_rule.hasFocus() and rule.error.text()


def test_remapped_ctrl_enter_mode_has_priority_over_optional_save_shortcut(desktop):
    _, _, _, create, _ = desktop
    dialog = create(master(), scope_bindings={'scope_all': 'Ctrl+Return'})
    dialog.scope.setCurrentIndex(dialog.scope.findData('single'))
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dialog.scope.currentData() == 'group'
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_clicking_once_preview_adds_a_date_and_switches_to_custom_sequence(desktop):
    _, _, _, create, click = desktop
    dialog = create()
    assert dialog.repeat.currentData() == ''
    click(dialog, START.date() + timedelta(days=3))
    assert dialog.repeat.currentData() == 'RDATE'
    assert dialog.sequence_starts() == [START, START + timedelta(days=3)]
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert dialog.result_events[0].schedule_syntax == 2
    assert dialog.result_events[0].schedule_expression


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
    assert dialog.repeat.currentData() == 'WEEKLY'
    extra = START + timedelta(days=3)
    click(dialog, extra.date())
    assert dialog.repeat.currentData() == 'RDATE' and dialog.schedule_replacement
    assert utc(dialog.sequence_starts()) == utc([*starts, extra])
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    store.edit_series(dialog.edit_reference, dialog.result_events[0], replace_schedule=dialog.schedule_replacement)
    changed = store.get_event(source.id)
    assert changed.excluded_dates == source.excluded_dates
    assert set(changed.recurrence_overrides) == {moved.recurrence_id}
    assert utc(value.start for value in expand_series(changed, *BOUNDS)) == before | utc([extra])


@pytest.mark.parametrize('rule,expected', [('FREQ=WEEKLY', 'unbounded'), ('FREQ=WEEKLY;COUNT=501', '500')])
def test_unbounded_and_over_limit_preview_clicks_never_truncate_a_regular_schedule(desktop, rule, expected):
    _, _, _, create, click = desktop
    source = master(rule)
    original = source.to_dict()
    dialog = create(source)
    click(dialog, START.date() + timedelta(days=3))
    assert dialog.repeat.currentData() == 'WEEKLY'
    assert dialog.pattern.text() == '' and expected in dialog.error.text()
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


def test_shortcuts_dialog_uses_shared_editor_and_collision_validation(desktop):
    _, _, show, _, _ = desktop
    bindings = {'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4', 'edit_event': 'E'}
    dialog = show(ShortcutsDialog(bindings, bindings))
    assert isinstance(dialog.editor, KeyboardShortcutsEditor)
    assert dialog.fields is dialog.editor.fields
    dialog.fields['edit_event'].setKeySequence('F2')
    dialog.save()
    assert 'unique' in dialog.error.text() and dialog.result() == QDialog.DialogCode.Rejected
    dialog.restore_defaults(bindings)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted and dialog.result_bindings == bindings
