"""Source preferences stage account changes without discarding local data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication

from debacal.calendar_sources import CalendarSourcesEditor
from debacal.discovery import DiscoveredCalendar
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
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
    store.save_calendar(remote(name='Alias', name_alias='Alias', color='#abcdef', use_server_color=False))
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
    new = next(value for value in editor._drafts.values() if value.server_name == 'New calendar')
    assert not new.enabled and new.read_only
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
        assert words in editor.group_hint.text()
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
    assert 'unavailable' in editor.group_hint.text()
