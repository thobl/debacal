"""Source preferences stage account changes without discarding local data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox

from debacal.calendar_sources import CalendarSourcesEditor
from debacal.calendar_color_choice import CALENDAR_COLORS, CalendarColorChoice
from debacal.dialogs import CalendarDialog
from debacal.discovery import DiscoveredCalendar
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.search_panel import SearchPanel
from debacal.store import Store
from debacal.sync import SyncEngine


START = datetime(2026, 11, 8, 9, tzinfo=timezone.utc)


def remote(identity='work', **values):
    return replace(Calendar(identity, identity.capitalize(), '#446677', kind='caldav',
        url='https://cloud.example/remote.php/dav/calendars/alice/' + identity + '/',
        username='alice', server_name=identity.capitalize(), server_color='#446677'), **values)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    owned, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _type, value, _trace: errors.append(value)):
        def create(**values):
            editor = CalendarSourcesEditor(store, Preferences(), **values)
            owned.append(editor)
            editor.resize(650, 600)
            editor.show()
            app.processEvents()
            return editor
        yield app, store, create, owned
        for editor in owned:
            editor.finish()
            editor.close()
            editor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def settle_discovery(app, editor):
    for _ in range(200):
        app.processEvents()
        editor.poll_discovery()
        if editor._future is None:
            return
        QTest.qWait(5)
    raise AssertionError('Calendar discovery did not finish.')


def test_grouped_sources_and_drafts_do_not_change_storage_until_apply(desktop):
    _, store, create, _ = desktop
    for calendar in (remote(), remote('personal'), remote('other', username='bob')):
        store.save_calendar(calendar)
    editor = create()
    assert editor.sources.topLevelItemCount() == 3
    assert editor._items['work'].parent() is editor._items['personal'].parent()
    assert editor._items['work'].parent() is not editor._items['other'].parent()
    editor._items['work'].setCheckState(0, Qt.CheckState.Unchecked)
    editor._name_changed('Work alias')
    editor.set_color('work', '#abcdef')
    assert store.get_calendar('work').enabled
    assert store.get_calendar('work').color == '#446677'
    editor.finish()
    assert store.get_calendar('work').enabled


def test_disabling_subscription_preserves_events_history_and_outbox(desktop):
    _, store, create, _ = desktop
    calendar = remote()
    store.save_calendar(calendar)
    store.set_default_calendar(calendar.id)
    event = Event('meeting', calendar.id, 'Kept locally', START, START + timedelta(hours=1))
    store.apply('Create meeting', [event])
    before = store.pending_changes(), store.history()
    editor = create()
    editor._items[calendar.id].setCheckState(0, Qt.CheckState.Unchecked)
    assert editor.apply()
    assert calendar.id not in {value.id for value in store.list_calendars()}
    assert store.get_calendar(calendar.id).visible
    assert store.get_event(event.id) == event
    assert (store.pending_changes(), store.history()) == before
    assert store.get_default_calendar().id == 'local'
    with pytest.raises(ValueError, match='disabled'):
        store.apply('Edit disabled', [replace(event, title='Cannot edit')])
    editor._items[calendar.id].setCheckState(0, Qt.CheckState.Checked)
    assert editor.apply()
    assert calendar.id in {value.id for value in store.list_calendars()}
    assert store.get_event(event.id) == event
    assert store.pending_changes() == before[0]


def test_alias_manual_color_and_server_reset_are_applied_together(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'])
    editor.alias.setText('My team')
    editor.alias.textEdited.emit('My team')
    editor.set_color('work', '#abcdef')
    assert editor.apply()
    saved = store.get_calendar('work')
    assert (saved.name, saved.name_alias, saved.server_name) == ('My team', 'My team', 'Work')
    assert saved.color == '#abcdef' and not saved.use_server_color
    editor.reset_server_color()
    editor.alias.clear()
    editor.alias.textEdited.emit('')
    assert editor.apply()
    saved = store.get_calendar('work')
    assert saved.name == 'Work' and saved.name_alias == ''
    assert saved.color == saved.server_color == '#446677'


def test_color_chooser_is_shared_with_calendar_dialog_and_preserves_current_color(desktop):
    _, store, create, _ = desktop
    source = remote(color='#a1B2c3', use_server_color=False, read_only=True)
    store.save_calendar(source)
    editor = create()
    editor.select_calendar(source.id)
    editor.set_preferences(replace(Preferences(), font_scale=130))
    dialog = CalendarDialog(source, parent=editor)
    try:
        assert isinstance(editor.color_choice, CalendarColorChoice)
        assert isinstance(dialog.color_row, CalendarColorChoice)
        assert editor.color.currentData() == dialog.color.currentData() == '#a1B2c3'
        assert editor.color.isEnabled() and editor.color_button.isEnabled()
        assert editor.color_button.text() == dialog.choose_color_button.text()
        assert editor.color.iconSize() == dialog.color.iconSize()
        for index, (name, value) in enumerate(CALENDAR_COLORS):
            for combo in (editor.color, dialog.color):
                assert (combo.itemText(index), combo.itemData(index)) == (name, value)
                swatch = combo.itemIcon(index).pixmap(16, 16).toImage()
                assert swatch.pixelColor(swatch.width() // 2, swatch.height() // 2) == QColor(value)
        assert editor._drafts[source.id] == source
    finally:
        dialog.close()
        dialog.deleteLater()


def test_preset_and_server_color_choices_are_drafts_and_reload_without_overrides(desktop):
    _, store, create, _ = desktop
    source = remote(read_only=True)
    store.save_calendar(source)
    editor = create()
    editor.select_calendar(source.id)
    changes = QSignalSpy(editor.color_choice.changed)
    editor.color.setCurrentIndex(editor.color.findData('#30978c'))
    assert changes.count() == 1
    assert editor._drafts[source.id].color == '#30978c'
    assert not editor._drafts[source.id].use_server_color
    assert store.get_calendar(source.id) == source
    assert editor.apply()
    assert store.get_calendar(source.id).color == '#30978c'
    editor.server_color_button.click()
    assert changes.count() == 1
    assert editor.color.currentData() == source.server_color
    assert editor._drafts[source.id].use_server_color
    assert store.get_calendar(source.id).color == '#30978c'
    assert editor.apply()
    assert store.get_calendar(source.id).color == source.server_color
    assert store.get_calendar(source.id).use_server_color
    editor.select_calendar('local')
    editor.select_calendar(source.id)
    assert changes.count() == 1
    assert editor._drafts[source.id].use_server_color


def test_custom_color_and_picker_cancel_remain_local_until_apply(desktop):
    _, store, create, _ = desktop
    source = remote()
    store.save_calendar(source)
    editor = create()
    editor.select_calendar(source.id)
    with patch('debacal.calendar_color_choice.QColorDialog.getColor', return_value=QColor('#a14fe7')) as picker:
        editor.color_button.click()
    assert picker.call_count == 1
    initial, parent, _title = picker.call_args.args
    assert initial == QColor(source.color) and parent is editor.color_choice
    assert editor.color.currentData() == '#a14fe7'
    assert editor.color.currentText() == '#a14fe7'
    assert editor._drafts[source.id].color == '#a14fe7'
    before = dict(editor._drafts)
    with patch('debacal.calendar_color_choice.QColorDialog.getColor', return_value=QColor()):
        editor.choose_color()
    assert editor._drafts == before and editor.color.currentData() == '#a14fe7'
    editor.finish()
    assert store.get_calendar(source.id) == source


def test_explicit_custom_choice_of_current_server_color_still_creates_override(desktop):
    _, store, create, _ = desktop
    source = remote()
    store.save_calendar(source)
    editor = create()
    editor.select_calendar(source.id)
    with patch('debacal.calendar_color_choice.QColorDialog.getColor', return_value=QColor(source.color)):
        editor.color_button.click()
    assert editor._drafts[source.id].color == source.color
    assert not editor._drafts[source.id].use_server_color
    assert store.get_calendar(source.id).use_server_color


def test_source_color_keyboard_order_reaches_combo_custom_picker_and_server_reset(desktop):
    app, store, create, _ = desktop
    store.save_calendar(remote())
    editor = create()
    editor.select_calendar('work')
    editor.activateWindow()
    app.processEvents()
    editor.alias.setFocus()
    for field in (editor.color, editor.color_button, editor.server_color_button, editor.in_main_list):
        QTest.keyClick(editor.focusWidget(), Qt.Key.Key_Tab)
        assert field.hasFocus()


@pytest.mark.parametrize('width', [800, 580])
def test_source_colors_fit_constrained_preferences_at_large_font_and_work_from_keyboard(desktop, width):
    app, store, _, _ = desktop
    source = remote(read_only=True)
    store.save_calendar(source)
    dialog = PreferencesDialog(Preferences(font_scale=140), store=store)
    try:
        dialog.resize(width, 650)
        dialog.set_category('sources')
        editor = dialog.source_editor
        editor.select_calendar(source.id)
        dialog.show()
        dialog.activateWindow()
        for _ in range(5):
            app.processEvents()
        assert dialog.width() <= max(width, 640)
        viewport = dialog.pages.currentWidget().viewport()
        assert dialog.pages.currentWidget().horizontalScrollBar().maximum() == 0
        controls = (editor.color, editor.color_button, editor.server_color_button)
        for control in controls:
            assert control.isVisible() and control.isEnabled()
            assert viewport.rect().contains(control.mapTo(viewport, QPoint(0, 0)))
            assert viewport.rect().contains(control.mapTo(viewport, control.rect().bottomRight()))
        assert editor.server_color_button.accessibleName() == 'Use server color'
        assert editor.server_color_button.toolTip() == 'Use server color'
        assert editor.color_button.accessibleName() == 'Choose calendar color'
        assert editor.color_button.shortcut().toString() == 'Alt+L'

        def assert_color_text_fits():
            option = QStyleOptionComboBox()
            editor.color.initStyleOption(option)
            content = editor.color.style().subControlRect(QStyle.ComplexControl.CC_ComboBox,
                option, QStyle.SubControl.SC_ComboBoxEditField, editor.color)
            available = content.width() - editor.color.iconSize().width() - 4
            assert editor.color.fontMetrics().horizontalAdvance(editor.color.currentText()) <= available

        assert editor.color.currentText() == source.server_color
        assert_color_text_fits()
        editor.color.setCurrentIndex(editor.color.findData('#bd775a'))
        assert editor.color.currentText() == 'Terracotta'
        assert_color_text_fits()
        editor.reset_server_color()

        editor.alias.setFocus()
        QTest.keyClick(editor.alias, Qt.Key.Key_Tab)
        assert editor.color.hasFocus()
        QTest.keyClick(editor.color, Qt.Key.Key_Up)
        assert not editor._drafts[source.id].use_server_color
        QTest.keyClick(editor.color, Qt.Key.Key_Tab)
        assert editor.color_button.hasFocus()
        with patch('debacal.calendar_color_choice.QColorDialog.getColor', return_value=QColor('#a14fe7')):
            QTest.keyClick(editor.color_button, Qt.Key.Key_Return)
        assert editor.color.currentData() == '#a14fe7'
        assert editor.color.toolTip() == '#a14fe7'
        assert_color_text_fits()
        QTest.keyClick(editor.color_button, Qt.Key.Key_Tab)
        assert editor.server_color_button.hasFocus()
        QTest.keyClick(editor.server_color_button, Qt.Key.Key_Return)
        assert editor.color.currentData() == source.server_color
        assert editor._drafts[source.id].use_server_color
        assert dialog.isVisible()
        assert store.get_calendar(source.id) == source
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_main_list_choice_is_editable_for_read_only_calendars_and_staged_until_apply(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote(read_only=True, in_main_list=False))
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'])
    assert editor.in_main_list.isVisible() and editor.in_main_list.isEnabled()
    assert not editor.in_main_list.isChecked()
    editor.in_main_list.setChecked(True)
    assert editor._drafts['work'].in_main_list
    assert not store.get_calendar('work').in_main_list
    assert editor.apply()
    saved = store.get_calendar('work')
    assert saved.in_main_list and saved.visible and saved.enabled and saved.read_only
    editor.in_main_list.setChecked(False)
    editor.finish()
    assert store.get_calendar('work').in_main_list


def test_main_list_choice_is_not_an_account_level_setting(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'])
    assert editor.in_main_list.isVisible()
    editor.select_account()
    assert editor.group_details.isVisible() and not editor.in_main_list.isVisible()


def test_apply_rebases_local_edits_onto_concurrent_sync_metadata(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'])
    editor.alias.setText('Alias')
    editor.alias.textEdited.emit('Alias')
    store.save_calendar(replace(store.get_calendar('work'), name='Server renamed', server_name='Server renamed', server_color='#998877'))
    assert editor.apply()
    saved = store.get_calendar('work')
    assert saved.name == saved.name_alias == 'Alias'
    assert saved.server_name == 'Server renamed'
    assert saved.server_color == saved.color == '#998877'


def test_discovery_uses_worker_credentials_and_adds_unconnected_calendars(desktop, monkeypatch):
    app, store, create, _ = desktop
    store.save_calendar(remote(name='Alias', name_alias='Alias', color='#abcdef', use_server_color=False,
                               in_main_list=False))
    threads = []
    class Credentials:
        def get(self, calendar):
            threads.append(get_ident())
            return 'synthetic-password'
    def discover(url, username, password):
        threads.append(get_ident())
        assert username == 'alice' and password == 'synthetic-password'
        return [DiscoveredCalendar('Server name', remote().url, '#998877'),
                DiscoveredCalendar('New calendar', remote('new').url, '#112233', True)]
    monkeypatch.setattr('debacal.calendar_sources.discover_calendars', discover)
    editor = create(credentials=Credentials())
    editor.sources.setCurrentItem(editor._items['work'])
    editor.refresh_account()
    settle_discovery(app, editor)
    assert threads and all(thread != get_ident() for thread in threads)
    work = editor._drafts['work']
    assert work.name == 'Alias' and work.server_name == 'Server name'
    assert work.color == '#abcdef' and work.server_color == '#998877'
    assert not work.in_main_list
    new = next(value for value in editor._drafts.values() if value.server_name == 'New calendar')
    assert not new.enabled and new.read_only
    assert new.in_main_list
    assert store.get_calendar(new.id) is None
    editor._items[new.id].setCheckState(0, Qt.CheckState.Checked)
    assert editor.apply()
    assert store.get_calendar(new.id).enabled
    assert store.get_calendar(new.id).read_only


def test_passwords_are_draft_only_and_emitted_once_per_account_on_apply(desktop):
    _, store, create, _ = desktop
    editor = create()
    editor.add_calendars([remote(), remote('personal')], password='synthetic-password', remember=False)
    changes = QSignalSpy(editor.credential_requested)
    assert store.get_calendar('work') is None and changes.count() == 0
    assert editor.apply()
    assert changes.count() == 1
    assert changes.at(0) == ['work', 'synthetic-password', False]
    assert 'synthetic-password' not in ''.join(row[0] for row in store.connection.execute('SELECT data FROM calendars'))
    assert not editor.apply()
    assert changes.count() == 1


def test_discovery_completion_after_cancel_does_not_apply_changes(desktop, monkeypatch):
    app, store, create, _ = desktop
    store.save_calendar(remote())
    entered, release = ThreadEvent(), ThreadEvent()
    def discover(*_):
        entered.set()
        assert release.wait(3)
        return [DiscoveredCalendar('New', remote('new').url)]
    monkeypatch.setattr('debacal.calendar_sources.discover_calendars', discover)
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'])
    editor.refresh_account()
    assert entered.wait(2)
    with pytest.raises(ValueError, match='discovery'):
        editor.validate()
    future = editor._future
    editor.finish()
    release.set()
    future.result(timeout=2)
    editor.poll_discovery()
    assert len(store.list_calendars(include_disabled=True)) == 2
    assert len(editor._drafts) == 2


def test_disabled_source_is_removed_from_cached_search_and_upcoming(desktop):
    _, store, create, owned = desktop
    store.save_calendar(remote())
    event = Event('meeting', 'work', 'Synthetic needle', START, START + timedelta(hours=1))
    store.apply('Create', [event])
    results = SearchPanel(store, Preferences())
    try:
        assert results.populate('needle') == 1
        editor = create()
        editor._items['work'].setCheckState(0, Qt.CheckState.Unchecked)
        editor.apply()
        results.populate('needle')
        assert results.selection_targets() == []
        results.populate_upcoming(START, days=1)
        assert results.selection_targets() == []
        assert store.list_events() == [event]
    finally:
        results.close()


def test_sync_metadata_keeps_alias_and_changes_server_name_and_color(desktop):
    _, store, _, _ = desktop
    calendar = remote(name='My team', name_alias='My team')
    store.save_calendar(calendar)
    engine = object.__new__(SyncEngine)
    engine._metadata_attempted = set()
    engine._metadata_discoverer = lambda *_: [DiscoveredCalendar('Server renamed', calendar.url, '#998877')]
    engine._emit = lambda *_: None
    engine._refresh_metadata(store, calendar, '')
    saved = store.get_calendar('work')
    assert saved.name == saved.name_alias == 'My team'
    assert saved.server_name == 'Server renamed' and saved.color == '#998877'


def test_details_follow_source_type_without_irrelevant_disabled_fields(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    store.save_calendar(remote('readonly', read_only=True, server_color=''))
    store.save_calendar(Calendar('subscription', 'Public holidays', '#667788', kind='ics',
                                 url='https://example.test/holidays.ics'))
    editor = create()

    editor.sources.setCurrentItem(editor._items['local'])
    assert editor.details.isVisible() and editor.alias.isEnabled()
    assert editor.alias_label.text() == 'Name'
    assert not editor.server_name.isVisible() and not editor.source_url.isVisible()
    assert not editor.server_color_button.isVisible() and not editor.account_button.isVisible()
    assert not editor.account_credentials.isVisible()
    assert editor.calendar_access.text() == 'Editable on this device'

    editor.sources.setCurrentItem(editor._items['subscription'])
    assert editor.details.isVisible() and editor.color_button.isEnabled()
    assert editor.source_url.isVisible() and editor.source_url.text().endswith('holidays.ics')
    assert not editor.server_name.isVisible() and not editor.account_button.isVisible()
    assert editor.calendar_access.text() == 'Read-only subscription'

    editor.sources.setCurrentItem(editor._items['readonly'])
    assert editor.alias.isEnabled() and editor.color_button.isEnabled()
    assert editor.server_name.isVisible() and editor.account_button.isVisible()
    assert not editor.server_color_button.isVisible()
    assert not editor.account_credentials.isVisible()
    assert editor.calendar_access.text() == 'Read-only on server'
    editor.account_button.click()
    assert editor.sources.currentItem() is editor._items['readonly'].parent()
    assert editor.group_details.isVisible() and not editor.details.isVisible()
    assert editor.account_credentials.isVisible() and editor.discover_button.isEnabled()
    assert editor.account_username.text() == 'alice'
    assert editor.account_url.text() == 'https://cloud.example'


def test_group_details_have_only_relevant_information_and_actions(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    store.save_calendar(Calendar('subscription', 'Public holidays', '#667788', kind='ics',
                                 url='https://example.test/holidays.ics'))
    editor = create()
    for identity, words in [('local', 'stored on this device'), ('subscription', 'read-only')]:
        editor.sources.setCurrentItem(editor._items[identity].parent())
        assert editor.group_details.isVisible() and not editor.details.isVisible()
        assert words in editor.group_summary.toolTip()
        assert not editor.account_details.isVisible()
        assert not editor.account_credentials.isVisible()
        assert not editor.subscription_actions.isVisible()
        assert editor.group_summary.text() == ('1 of 1 subscription enabled' if identity == 'subscription' else '1 of 1 calendar enabled')


def test_account_connection_actions_are_drafts_and_selection_survives_apply(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    store.save_calendar(remote('personal', enabled=False))
    editor = create()
    account = editor._items['work'].parent()
    editor.sources.setCurrentItem(account)
    assert editor.group_summary.text() == '1 of 2 calendars connected'
    editor.enable_all_button.click()
    assert all(editor._drafts[identity].enabled for identity in ('work', 'personal'))
    assert not store.get_calendar('personal').enabled
    assert editor.group_summary.text() == '2 of 2 calendars connected'
    assert not editor.enable_all_button.isEnabled()
    editor.disable_all_button.click()
    assert editor.group_summary.text() == '0 of 2 calendars connected'
    assert store.get_calendar('work').enabled
    assert editor.apply()
    assert editor.sources.currentItem() is editor._groups[editor._selected_account]
    assert editor.group_details.isVisible() and editor.account_credentials.isVisible()
    assert not store.get_calendar('work').enabled and not store.get_calendar('personal').enabled


def test_account_password_draft_is_shared_and_hidden_for_calendar_selections(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    store.save_calendar(remote('personal'))
    store.save_calendar(remote('other', username='bob'))
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'].parent())
    editor.password.setText('synthetic-password')
    editor.password.textEdited.emit('synthetic-password')
    editor.remember_password.setChecked(False)
    editor.sources.setCurrentItem(editor._items['personal'])
    assert not editor.account_credentials.isVisible() and editor.password.text() == ''
    editor.account_button.click()
    assert editor.password.text() == 'synthetic-password'
    assert not editor.remember_password.isChecked()
    editor.sources.setCurrentItem(editor._items['other'].parent())
    assert editor.password.text() == ''
    editor.sources.setCurrentItem(editor._items['personal'].parent())
    assert editor.password.text() == 'synthetic-password'


def test_account_discovery_preserves_account_selection_and_connection_summary(desktop, monkeypatch):
    app, store, create, _ = desktop
    store.save_calendar(remote())
    monkeypatch.setattr('debacal.calendar_sources.discover_calendars', lambda *_: [
        DiscoveredCalendar('Work', remote().url, '#446677'),
        DiscoveredCalendar('New', remote('new').url, '#112233'),
    ])
    editor = create()
    editor.sources.setCurrentItem(editor._items['work'].parent())
    editor.discover_button.click()
    settle_discovery(app, editor)
    assert editor.sources.currentItem() is editor._groups[editor._selected_account]
    assert editor.group_summary.text() == '1 of 2 calendars connected'
    assert editor.account_credentials.isVisible() and editor.discover_button.isEnabled()
    assert 'Found 2 account calendars' in editor.message.text()


def test_remote_disabled_workspace_omits_unavailable_account_controls(desktop):
    _, store, create, _ = desktop
    store.save_calendar(remote())
    editor = create(allow_remote=False)
    editor.sources.setCurrentItem(editor._items['work'].parent())
    assert editor.account_details.isVisible()
    assert not editor.account_credentials.isVisible()
    assert 'unavailable' in editor.group_summary.toolTip()
