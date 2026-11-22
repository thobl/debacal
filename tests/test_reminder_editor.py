"""Saved reminders stay with the component and the reviewed editor draft."""
from dataclasses import replace
from datetime import timedelta
from threading import current_thread, main_thread
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from alarm_support import BASE, alarm_event, wait_for
from debacal.alarms import Alarm, signed_duration
from debacal.event_dialog import EventDialog
from debacal.models import Calendar
from debacal.recurrence import occurrence
from debacal.reminder_preferences import CalendarReminderPolicy, ReminderPreferences
from debacal.store import Store

APP = QApplication.instance() or QApplication([])
REL = Alarm('r', 'DISPLAY', '-PT5M', uid='r')
DAY = Alarm('d', 'DISPLAY', '-P1D', related='END', uid='d')
ABS = Alarm('once', 'DISPLAY', '20261120T120000Z', uid='once')
REMOTE = Alarm('remote', 'EMAIL', '+PT1H', repeat=2, repeat_interval='PT10M',
               raw_ics='BEGIN:VALARM\r\nACTION:EMAIL\r\nX-PRIVATE:keep\r\nEND:VALARM\r\n')
CALS = [Calendar('c', 'Calendar', '#445566'), Calendar('b', 'Other', '#566778')]
PREFS = ReminderPreferences(new_timed=(REL,), new_all_day=(DAY,),
                           calendars={'b': CalendarReminderPolicy('mute')})


def release_widget(widget):
    """Drain this test's top-level window while its owner and Qt are still alive."""
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    assert not isValid(widget)


def test_notes_reminders_scope_and_default_lifetime():
    dlg = EventDialog(CALS, BASE, reminder_preferences=PREFS)
    try:
        dlg.show()
        APP.processEvents()
        assert dlg.notes_choice.currentData() == 'notes'
        height = dlg.notes_section.height()
        dlg.notes_choice.buttons['reminders'].click()
        APP.processEvents()
        assert dlg.notes_section.height() == height
        initial = dlg.build_events()[0].alarms
        assert initial[0].trigger == REL.trigger and initial[0].uid != REL.uid
        assert dlg.build_events()[0].alarms == initial
        dlg.all_day.setChecked(True)
        assert dlg.build_events()[0].alarms[0].trigger == DAY.trigger
        dlg.all_day.setChecked(False)
        assert dlg.build_events()[0].alarms == initial
        dlg.calendar.setCurrentIndex(dlg.calendar.findData('b'))
        assert dlg.build_events()[0].alarms == initial
        assert 'muted' in dlg.reminders.policy_hint.text().lower()
        dlg.reminders.remove_buttons[0].click()
        dlg.all_day.setChecked(True)
        assert dlg.alarm_edit() == () and dlg.build_events()[0].alarms == ()
        dlg.title.setText('Pick a date')
        dlg.begin_calendar_occurrences()
        assert dlg.result() == dlg.PICK_OCCURRENCES
        assert dlg.notes_choice.currentData() == 'reminders' and dlg.alarm_edit() == ()
    finally:
        release_widget(dlg)


def test_scoped_summaries_single_absolute_and_drafts():
    master = alarm_event(alarms=(REL, ABS), recurrence_rule='FREQ=DAILY;COUNT=4')
    pivot = occurrence(master, BASE + timedelta(days=1))
    child = replace(occurrence(master, BASE + timedelta(days=2)), alarms=(REMOTE,))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    dlg = EventDialog(CALS, pivot.start, event=pivot, series=master, scope_bindings={'scope_all':'Ctrl+7'})
    try:
        dlg.show()
        dlg.notes_choice.setCurrentData('reminders')
        APP.processEvents()
        assert dlg.reminders.alarms() == (REL,)
        assert ABS in dlg.reminders.series_alarms and dlg.alarm_edit() is None
        dlg.reminders.remove_buttons[0].click()
        assert dlg.alarm_edit() == ()
        QTest.keyClick(dlg, Qt.Key.Key_7, Qt.KeyboardModifier.ControlModifier)
        assert dlg.scope.currentData() == 'group' and dlg.reminders.mixed and dlg.alarm_edit() is None
        dlg.title.setText('Only a title')
        dlg.save()
        assert dlg.alarm_edit() is None
        dlg.scope.setCurrentData('future')
        assert ABS not in dlg.reminders.alarms() and dlg.alarm_edit() is None
        dlg.scope.setCurrentData('single')
        assert dlg.alarm_edit() == () and ABS in dlg.reminders.series_alarms
    finally:
        release_widget(dlg)


def test_template_and_event_alarm_controls_preserve_wire_and_units():
    from debacal.reminder_editor import ReminderEditor
    editor = ReminderEditor()
    template = ReminderEditor(templates_only=True)
    try:
        editor.set_alarms((REMOTE, REL))
        editor.edit_buttons[1].click()
        form = editor.alarm_dialog
        form.duration.setText('24h')
        form.direction.setCurrentData('after')
        form.anchor.setCurrentData('END')
        form.save()
        remote, changed = editor.alarms()
        assert remote == REMOTE
        assert signed_duration(changed.trigger) == (0, 86400) and changed.related == 'END'
        editor.edit_buttons[1].click()
        editor.alarm_dialog.duration.setText('1d')
        editor.alarm_dialog.save()
        assert signed_duration(editor.alarms()[1].trigger) == (1, 0)
        editor.edit_buttons[1].click()
        form = editor.alarm_dialog
        form.kind.setCurrentData('absolute')
        form.absolute.set_value(BASE.astimezone(ZoneInfo('Europe/Berlin')))
        form.save()
        assert editor.alarms()[1].trigger == '20261121T090000Z'
        template.add_button.click()
        assert template.alarm_dialog.kind.findData('absolute') == -1
        template.alarm_dialog.reject()
        assert template.alarms() == () and not template.is_modified()
        with pytest.raises(ValueError):
            template.set_alarms((ABS,))
    finally:
        release_widget(template)
        release_widget(editor)


def test_independent_endpoint_zones_survive_unrelated_editor_save():
    master = alarm_event(start=BASE.astimezone(ZoneInfo('America/New_York')),
                         end=(BASE + timedelta(hours=1)).astimezone(ZoneInfo('Asia/Tokyo')), alarms=(DAY,))
    dlg = EventDialog(CALS, BASE, event=master)
    try:
        dlg.title.setText('Rename')
        dlg.save()
        assert dlg.result_events[0].end_timezone_id == 'Asia/Tokyo'
        assert dlg.result_events[0].timezone_id == 'America/New_York'
        assert dlg.alarm_edit() is None
    finally:
        release_widget(dlg)


def test_file_editor_hydrates_legacy_on_worker_and_rejects_raw_only_conflict(tmp_path, monkeypatch):
    import json
    from debacal import alarm_ics
    from debacal.ui import MainWindow
    from test_ui_database_actions import held_writer

    path = tmp_path / 'db'
    master = alarm_event(calendar_id='local')
    legacy = master.to_dict()
    legacy.pop('alarms')
    legacy['raw_ics'] = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e\r\nDTSTART:20261121T090000Z\r\nDTEND:20261121T100000Z\r\n'
                         'BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT5M\r\nDESCRIPTION:Alert\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    with Store(path) as store:
        store.apply_remote([master])
        def write_raw(writer, payload):
            writer.connection.execute('UPDATE events SET data=? WHERE id=?', (json.dumps(payload), master.id))
        with store._transaction():
            write_raw(store, legacy)
        window = MainWindow(store, enable_sync=False)
        opened, parsed = [], []
        hydrate = alarm_ics._retained_alarms
        def checked(*args, **kwargs):
            parsed.append(current_thread())
            assert current_thread() != main_thread()
            return hydrate(*args, **kwargs)
        monkeypatch.setattr(alarm_ics, '_retained_alarms', checked)
        def accept(dlg):
            opened.append(dlg.reminders.alarms())
            dlg.title.setText('Stale edit')
            dlg.save()
            return dlg.result()
        monkeypatch.setattr(EventDialog, 'exec', accept)
        try:
            with held_writer(path):
                window.edit_event(master.id)
                assert not opened
                wait_for(lambda: opened)
                assert opened[0][0].trigger == '-PT5M'
                assert parsed and window.database_commands.pending
            wait_for(lambda: not window.database_commands.pending)
            assert store.get_event(master.id).title == 'Stale edit'
            with store._transaction():
                write_raw(store, legacy)
            def raced(dlg):
                result = accept(dlg)
                with store._transaction():
                    write_raw(store, dict(legacy, raw_ics=legacy['raw_ics'].replace('-PT5M', '-PT1H')))
                return result
            monkeypatch.setattr(EventDialog, 'exec', raced)
            window.edit_event(master.id)
            wait_for(lambda: window.local_issues)
            assert store.get_event(master.id).title == master.title
            assert 'changed' in str(window.local_issues).lower()
        finally:
            release_widget(window)
            APP.processEvents()


def test_detached_absolute_is_editable_and_master_absolute_is_context_only():
    master = alarm_event(alarms=(ABS,), recurrence_rule='FREQ=DAILY;COUNT=3')
    own = replace(ABS, key='child', uid='child', trigger='20261121T230000Z')
    child = replace(occurrence(master, BASE + timedelta(days=1)), alarms=(own,))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    dlg = EventDialog(CALS, child.start, event=child, series=master)
    try:
        assert dlg.reminders.series_alarms == (ABS,)
        assert dlg.reminders.alarms() == (own,)
        dlg.reminders.remove_buttons[0].click()
        assert dlg.alarm_edit() == ()
    finally:
        release_widget(dlg)


@pytest.mark.parametrize('clear', [True, False])
def test_mixed_selection_writes_only_explicit_replacement(clear, monkeypatch):
    from debacal.ui import MainWindow
    a = alarm_event(calendar_id='local', alarms=(REL,))
    b = replace(a, id='b', ical_uid='b', alarms=(REMOTE,))
    with Store(':memory:') as store:
        store.apply_remote([a, b])
        window = MainWindow(store, enable_sync=False)
        window.select_event(a.id)
        window.select_event(b.id, additive=True)
        def accept(dlg):
            assert dlg.reminders.mixed and dlg.alarm_edit() is None
            if clear:
                dlg.reminders.clear_button.click()
            dlg.title.setText('Changed')
            dlg.save()
            return dlg.result()
        monkeypatch.setattr(EventDialog, 'exec', accept)
        try:
            window.edit_event()
            for source in (a, b):
                saved = store.get_event(source.id)
                assert saved.title == 'Changed'
                assert saved.alarms == (() if clear else source.alarms)
        finally:
            release_widget(window)


def test_single_representative_standalone_dialog_preserves_imported_list():
    item = occurrence(alarm_event(alarms=(REL,), recurrence_rule='FREQ=DAILY'), BASE)
    dlg = EventDialog(CALS, item.start, event=item)
    try:
        assert dlg.reminders.alarms() == (REL,) and dlg.alarm_edit() is None
    finally:
        release_widget(dlg)


def test_stale_editor_read_yields_to_navigation_and_newer_request(tmp_path, monkeypatch):
    from threading import Event
    from debacal import ui
    a = alarm_event(calendar_id='local')
    b = replace(a, id='b', ical_uid='b')
    with Store(tmp_path / 'db') as store:
        store.apply_remote([a, b])
        window = ui.MainWindow(store, enable_sync=False)
        entered, release, opened = Event(), Event(), []
        read = ui._read_file_editor
        def held(*args):
            entered.set()
            assert release.wait(3)
            return read(*args)
        def opened_dialog(dlg):
            opened.append(dlg.original.id)
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ui, '_read_file_editor', held)
        monkeypatch.setattr(EventDialog, 'exec', opened_dialog)
        try:
            window.edit_event(a.id)
            wait_for(entered.is_set)
            window.open_day((BASE + timedelta(days=3)).date())
            window.edit_event(b.id)
            release.set()
            wait_for(lambda: opened)
            assert opened == [b.id]
        finally:
            release.set()
            release_widget(window)


def test_editor_read_is_independent_of_busy_import_preparation(tmp_path, monkeypatch):
    from threading import Event
    from debacal.ui import MainWindow
    with Store(tmp_path / 'db') as store:
        source = alarm_event(calendar_id='local')
        store.apply_remote([source])
        window = MainWindow(store, enable_sync=False)
        busy, release, opened = Event(), Event(), []
        def import_task():
            busy.set()
            release.wait(5)
        window.executor.submit(import_task)
        monkeypatch.setattr(EventDialog, 'exec',
                            lambda dlg: opened.append(dlg.original.id) or QDialog.DialogCode.Rejected)
        try:
            wait_for(busy.is_set)
            window.edit_event(source.id)
            wait_for(lambda: opened, timeout=2)
            assert opened == [source.id]
        finally:
            release.set()
            release_widget(window)


def test_tab_reaches_loaded_reminder_rows_before_preview():
    dlg = EventDialog(CALS, BASE, event=alarm_event(alarms=(REL,)))
    try:
        dlg.show()
        dlg.notes_choice.setCurrentData('reminders')
        APP.processEvents()
        dlg.reminders.add_button.setFocus()
        QTest.keyClick(dlg.reminders.add_button, Qt.Key.Key_Tab)
        assert dlg.reminders.edit_buttons[0].hasFocus()
        QTest.keyClick(dlg.reminders.edit_buttons[0], Qt.Key.Key_Tab)
        assert dlg.reminders.remove_buttons[0].hasFocus()
        QTest.keyClick(dlg.reminders.remove_buttons[0], Qt.Key.Key_Tab)
        assert dlg.preview.previous_button.hasFocus()
    finally:
        release_widget(dlg)


def test_unchanged_accepted_absolute_editor_retains_leap_second_and_modification_intent():
    from debacal.reminder_editor import ReminderEditor
    leap = replace(ABS, trigger='20161231T235960Z')
    editor = ReminderEditor()
    try:
        editor.set_alarms((leap,))
        editor.edit_buttons[0].click()
        editor.alarm_dialog.save()
        assert editor.alarms() == (leap,) and not editor.is_modified()
    finally:
        release_widget(editor)


def test_timing_previews_and_picker_preserve_saved_component_reminders(monkeypatch):
    from debacal import alarm_series
    from debacal.ui import MainWindow
    from test_calendar_occurrence_mode import play_editor
    master = alarm_event(calendar_id='local', alarms=(REL, ABS), recurrence_rule='FREQ=DAILY;COUNT=4',
                         end=(BASE + timedelta(hours=1)).astimezone(ZoneInfo('Asia/Tokyo')))
    child = replace(occurrence(master, BASE + timedelta(days=2)), alarms=(REMOTE,))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    calls = []
    copy = alarm_series.copied_alarms
    def record(alarms, *args, **kwargs):
        if alarms:
            calls.append(alarms)
        return copy(alarms, *args, **kwargs)
    monkeypatch.setattr(alarm_series, 'copied_alarms', record)
    with Store(':memory:') as store:
        store.apply_remote([master])
        window = MainWindow(store, enable_sync=False)
        window.open_day(BASE.date())
        window.show()
        ref = occurrence(master, BASE + timedelta(days=1))
        def begin(state):
            dlg = state['dialog']
            dlg.notes_choice.setCurrentData('reminders')
            dlg.reminders.edit_buttons[0].click()
            dlg.reminders.alarm_dialog.duration.setText('1h')
            dlg.reminders.alarm_dialog.save()
            state['intent'] = dlg.alarm_edit()
            calls.clear()
            dlg.title.setText('Future draft')
            dlg._refresh_preview()
            assert not calls
            dlg.begin_calendar_occurrences()
        def finish(_state):
            assert not calls
            window.finish_occurrence_picking()
        def accept(state):
            dlg = state['dialog']
            assert dlg.notes_choice.currentData() == 'reminders' and dlg.alarm_edit() == state['intent']
            dlg.save()
        try:
            play_editor(window, [('dialog', begin), ('calendar', finish), ('dialog', accept)],
                        opener=lambda: window.edit_event(ref.id, scope='future'))
            head = store.get_event(master.id)
            tail, = [v for v in store.list_events() if v.id != master.id]
            assert head.alarms == (REL, ABS)
            assert [signed_duration(a.trigger) for a in tail.alarms] == [(0, -3600)]
            assert tail.end_timezone_id == 'Asia/Tokyo'
        finally:
            release_widget(window)


def test_unavailable_following_scope_on_secondary_family_preserves_active_draft(monkeypatch):
    from debacal import series_operations
    from debacal.event_dialog import EditorFamilies
    a = alarm_event(alarms=(REL,), recurrence_rule='FREQ=DAILY;COUNT=3')
    b = replace(a, id='b', ical_uid='b')
    refs = [occurrence(v, BASE + timedelta(days=1)) for v in (a, b)]
    dlg = EventDialog(CALS, BASE, event=refs[0], series=a, selected_refs=refs, selected_families=EditorFamilies((a, b)))
    partition = series_operations.partition_series
    def unavailable(source, ref):
        if source.id == 'b':
            raise ValueError('This rule changes phase when split.')
        return partition(source, ref)
    monkeypatch.setattr(series_operations, 'partition_series', unavailable)
    try:
        dlg.reminders.remove_buttons[0].click()
        dlg.scope.setCurrentData('future')
        assert dlg.scope.currentData() == 'single' and dlg.alarm_edit() == ()
        assert 'phase' in dlg.error.text()
    finally:
        release_widget(dlg)


@pytest.mark.parametrize('mode', ['dark', 'light'])
def test_rendered_reminder_rows_use_theme_surface_and_readable_text(mode):
    from collections import Counter
    from PySide6.QtWidgets import QScrollArea
    from debacal.preferences import Preferences
    from debacal.theme import _contrast, scheme_for
    prefs = Preferences(appearance_mode=mode)
    dlg = EventDialog(CALS, BASE, event=alarm_event(alarms=(REL, DAY)), preferences=prefs)
    try:
        dlg.show()
        dlg.notes_choice.setCurrentData('reminders')
        APP.processEvents()
        scroll = dlg.reminders.findChild(QScrollArea)
        pixels = scroll.grab().toImage()
        background = Counter(pixels.pixelColor(x, y).name() for x in range(2, pixels.width() - 2, 5)
                             for y in range(2, pixels.height() - 2, 4)).most_common(1)[0][0]
        scheme = scheme_for(prefs)
        assert _contrast(scheme.ink, background) >= 4.5
        assert background in (scheme.background, scheme.sidebar, scheme.panel)
    finally:
        release_widget(dlg)


@pytest.mark.parametrize('graphical', [False, True], ids=['source', 'edited-master'])
def test_changed_anchor_preview_and_picker_do_not_decode_detached_reminders(monkeypatch, graphical):
    from debacal.models import Event
    from debacal.occurrence_picker import CalendarOccurrencePicker
    from debacal.ui import MainWindow

    master = alarm_event(calendar_id='local', alarms=(REL,), recurrence_rule='FREQ=DAILY;COUNT=4')
    child = replace(occurrence(master, BASE + timedelta(days=2)), title='Detached', location='Studio',
                    alarms=(REMOTE,))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    first = occurrence(master, BASE)
    with Store(':memory:') as store:
        store.apply_remote([master])
        window = MainWindow(store, enable_sync=False)
        def edit(dlg):
            if graphical:
                assert dlg.add_calendar_occurrence(BASE + timedelta(days=6))
            counts = {}
            decode = Alarm.from_dict
            calls = []
            def count(payload):
                calls.append(payload['key'])
                return decode(payload)
            with monkeypatch.context() as instrument:
                instrument.setattr(Alarm, 'from_dict', staticmethod(count))
                dlg.start.set_value(dlg.start.value() + timedelta(minutes=10))
                counts['change'] = len(calls)
                calls.clear()
                dlg._refresh_preview()
                counts['preview'] = len(calls)
                calls.clear()
                picker = CalendarOccurrencePicker(window, dlg)
                picker.rebuild()
                counts['picker'] = len(calls)
            assert counts == {'change': 0, 'preview': 0, 'picker': 0}
            assert dlg.alarm_edit() is None
            dlg.save()
            return dlg.result()
        monkeypatch.setattr(EventDialog, 'exec', edit)
        try:
            window.edit_event(first.id, scope='group')
            saved = store.get_event(master.id)
            assert saved.start == BASE + timedelta(minutes=10)
            assert saved.alarms == (REL,)
            stored = next(Event.from_dict(p) for p in saved.recurrence_overrides.values() if p['title'] == 'Detached')
            assert stored.alarms == (REMOTE,) and stored.location == 'Studio'
            assert stored.start == child.start + timedelta(minutes=10)
            if graphical:
                assert saved.recurrence_dates
        finally:
            release_widget(window)


@pytest.mark.parametrize('navigate', [True, False], ids=['stale', 'current'])
def test_failed_editor_read_is_reported_only_in_its_requested_context(tmp_path, monkeypatch, navigate):
    from threading import Event
    from debacal import ui

    with Store(tmp_path / 'db') as store:
        source = alarm_event(calendar_id='local')
        store.apply_remote([source])
        window = ui.MainWindow(store, enable_sync=False)
        window.poll_timer.stop()
        entered, release = Event(), Event()
        def failed(*args):
            entered.set()
            assert release.wait(3)
            raise ValueError('Old selection was removed')
        monkeypatch.setattr(ui, '_read_file_editor', failed)
        try:
            window.edit_event(source.id)
            wait_for(entered.is_set)
            if navigate:
                window.open_day((BASE + timedelta(days=6)).date())
            release.set()
            wait_for(lambda: window._editor_future is None)
            assert [issue.details for issue in window.local_issues] == ([] if navigate else ['Old selection was removed'])
        finally:
            release.set()
            release_widget(window)

def test_editor_reader_can_complete_after_its_window_is_destroyed(tmp_path, monkeypatch, caplog):
    from threading import Event
    from debacal import ui

    entered, release, finished = Event(), Event(), Event()
    with Store(tmp_path / 'db') as store:
        source = alarm_event(calendar_id='local')
        store.apply_remote([source])
        window = ui.MainWindow(store, enable_sync=False)
        def held(*_args):
            entered.set()
            assert release.wait(5)
            raise ValueError('Retired reader')
        monkeypatch.setattr(ui, '_read_file_editor', held)
        window.edit_event(source.id)
        try:
            wait_for(entered.is_set)
            future = window._editor_future
            future.add_done_callback(lambda _: finished.set())
            release_widget(window)
        finally:
            release.set()
        wait_for(finished.is_set)
        APP.processEvents()
        assert 'exception calling callback' not in caplog.text
