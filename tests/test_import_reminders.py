"""Manual reminder choices travel with the exact accepted source to its writer."""
from dataclasses import replace
from threading import get_ident

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.alarms import Alarm
from debacal.ics import parse_ics_report
from debacal.import_dialog import ImportDialog
from debacal.import_preparation import count_reminder_sources, prepare_sources, transform_import_reminders
from debacal.import_selection import ImportSelection
from debacal.import_sources import ImportSource
from debacal.models import Calendar, Event
from debacal.reminder_preferences import ReminderPreferences, save_reminder_preferences
from debacal.store import Store
from debacal.sync import RemoteResource
from debacal.ui import MainWindow
from test_import_requests import accept_request, desktop, finish_parsing, wait_for
from test_sync import setup as transport_setup, synchronize
from test_ui_database_actions import START, held_writer


TEMPLATE = Alarm('template', 'DISPLAY', '-PT17M', uid='template')
WIRE = b'''BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:family
DTSTART:20261104T090000Z
DTEND:20261104T100000Z
RRULE:FREQ=WEEKLY
SUMMARY:Selected series
X-INVITATION:blue
BEGIN:VALARM
ACTION:EMAIL
TRIGGER:-PT3M
DESCRIPTION:Retained alarm
X-DEVICE:watch
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:family
RECURRENCE-ID:20261111T090000Z
DTSTART:20261111T093000Z
DTEND:20261111T103000Z
SUMMARY:Detached
END:VEVENT
BEGIN:VEVENT
UID:excluded
DTSTART:20261104T110000Z
DTEND:20261104T120000Z
SUMMARY:Do not import
END:VEVENT
END:VCALENDAR'''


def test_policy_snapshot_survives_filter_and_counts_effective_components(monkeypatch):
    # Later source supersedes the first version in the preview. The unbounded
    # master contributes exactly one component; the detached child contributes one.
    def unexpected_expansion(*_args, **_kwargs):
        pytest.fail('Reminder counts expanded the unbounded recurrence')
    monkeypatch.setattr('debacal.import_preparation.expand_series', unexpected_expansion)
    earlier = WIRE.replace(b'UID:excluded', b'UID:first-only')
    prepared = prepare_sources((ImportSource.from_data(earlier), ImportSource.from_data(WIRE)))
    draft = [TEMPLATE]
    accepted = prepared.with_reminders('replace', draft)
    draft.clear()
    assert prepared.reminder_mode == 'keep' and prepared.reminder_alarms == ()
    assert accepted.reminder_counts == (1, 4)
    selection = ImportSelection()
    selection.set_selected([e for e in prepared.events if e.ical_uid != 'family'], False)
    selected = accepted.with_selection(selection.snapshot(masters=prepared.events))
    assert selected.reminder_counts == (1, 2)
    assert (selected.reminder_mode, selected.reminder_alarms) == ('replace', (TEMPLATE,))


@pytest.mark.parametrize(('mode', 'source_kind', 'master_trigger', 'child_trigger'), [
    ('remove', 'stdin', None, None), ('keep', 'file', '-PT3M', None),
    ('add_missing', 'ipc_sources', '-PT3M', '-PT17M'), ('replace', 'file', '-PT17M', '-PT17M'),
])
def test_selected_import_remove_survives_retry_and_caldav(tmp_path, monkeypatch, mode, source_kind,
                                                       master_trigger, child_trigger):
    app = QApplication.instance() or QApplication([])
    store, transport, engine = transport_setup(tmp_path)
    window = MainWindow(store, enable_sync=False)
    path = tmp_path / 'Invitation.ics'
    wire = WIRE
    if mode == 'remove':
        child_alarm = b'BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT14M\nDESCRIPTION:Child alarm\nEND:VALARM'
        wire = wire.replace(b'SUMMARY:Detached', b'SUMMARY:Detached\n' + child_alarm)
    path.write_bytes(wire)
    user_thread = get_ident()
    accepted, attempts = [], []
    count_threads, transform_threads = [], []
    def count(report):
        count_threads.append(get_ident())
        return count_reminder_sources(report)
    def transform(report, policy, alarms):
        assert all(e.calendar_id == 'remote' and e.ical_uid != 'excluded' for e in report.events)
        if any(e.ical_uid == 'family' for e in report.events):
            transform_threads.append(get_ident())
        return transform_import_reminders(report, policy, alarms)
    original_write = Store.import_report
    original = replace(parse_ics_report(WIRE, 'remote').events[0], id='known-owner', title='Old revision')
    store.apply_remote([original])
    save_reminder_preferences(store, ReminderPreferences(import_alarms=(TEMPLATE,)), now=START)

    def review(dialog):
        wait_for(app, lambda: dialog.preview_ready)
        assert dialog.reminder_mode.currentData() == 'keep'
        assert dialog.reminder_editor.isHidden()
        dialog.reminder_mode.setCurrentIndex(dialog.reminder_mode.findData(mode))
        assert dialog.reminder_editor.templates_only
        assert dialog.reminder_editor.isHidden() == (mode in ('keep', 'remove'))
        dialog._toggle_occurrence(next(e for e in dialog.prepared_import.events if e.ical_uid == 'excluded'))
        wait_for(app, lambda: dialog.preview_ready)
        expected = '2 of 2' if mode == 'remove' else '1 of 2'
        assert ('1 of 3' if source_kind == 'ipc_sources' else expected) in dialog.reminder_summary.text()
        result = accept_request(dialog)
        accepted.append(dialog.selected_prepared)
        return result

    def fail_once(writer, report, **kwargs):
        if any(e.ical_uid == 'family' for e in report.events):
            attempts.append((get_ident(), report))
            if len(attempts) == 1:
                raise ValueError('Temporary write failure')
        return original_write(writer, report, **kwargs)

    try:
        monkeypatch.setattr(ImportDialog, 'exec', review)
        monkeypatch.setattr(Store, 'import_report', fail_once)
        monkeypatch.setattr('debacal.import_preparation.count_reminder_sources', count)
        monkeypatch.setattr('debacal.import_preparation.transform_import_reminders', transform)
        with held_writer(store.path):
            if source_kind == 'file':
                assert window.import_files([path], calendar_id='remote')
            elif source_kind == 'stdin':
                assert window.import_data(wire, calendar_id='remote')
            else:
                # IPC delivers the listed paths and stdin in one review.
                peer = WIRE[:WIRE.index(b'BEGIN:VEVENT')] + WIRE[WIRE.rindex(b'BEGIN:VEVENT'):]
                path.write_bytes(peer.replace(b'UID:excluded', b'UID:peer'))
                assert window.import_sources((ImportSource.from_path(path), ImportSource.from_data(WIRE)),
                                             calendar_id='remote')
            window._process_import_request()
            assert accepted[0].reminder_mode == mode
            wait_for(app, lambda: window.database_commands.pending > 0)
            save_reminder_preferences(store, ReminderPreferences(import_mode='replace'), now=START)
            path.write_bytes(b'Changed after review')
            window.close()
            assert not window._closed
        wait_for(app, lambda: bool(window.local_issues) and not window._import_jobs)
        assert not window._closed and window._import_memory_bytes >= len(WIRE)
        issue, = window.local_issues
        assert issue.action
        if source_kind == 'ipc_sources':
            sibling = next(e for e in store.list_events() if e.ical_uid == 'peer')
            store.apply('Edit imported sibling', [replace(sibling, title='Changed independently')])
        issue.action()
        wait_for(app, lambda: not window._import_jobs and len(attempts) == 2)
        assert not window.local_issues and window._import_memory_bytes == 0
        assert len(accepted) == 1 and len(transform_threads) == 2
        assert count_threads and user_thread not in count_threads + transform_threads
        assert transform_threads == [thread for thread, _ in attempts]
        assert attempts[0][1] == attempts[1][1]
        imported = {e.ical_uid: e for e in store.list_events()}
        if source_kind == 'ipc_sources':
            assert imported.pop('peer').title == 'Changed independently'
        updated, = imported.values()
        assert (updated.id, updated.ical_uid, updated.calendar_id) == ('known-owner', 'family', 'remote')
        child, = map(Event.from_dict, updated.recurrence_overrides.values())
        assert [alarm.trigger for alarm in updated.alarms] == ([master_trigger] if master_trigger else [])
        assert [alarm.trigger for alarm in child.alarms] == ([child_trigger] if child_trigger else [])
        assert not [message for message in synchronize(engine) if message.kind == 'error']
        uploaded, = (resource for resource in transport.resources.values() if 'UID:family' in resource.data)
        assert uploaded.data.count('BEGIN:VALARM') == bool(master_trigger) + bool(child_trigger)
        assert 'X-INVITATION:blue' in uploaded.data
        assert ('X-DEVICE:watch' in uploaded.data) == (mode in ('keep', 'add_missing'))
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.close()
        store.close()


def test_equal_errors_hold_separate_retry_actions_and_release_capacity(desktop, monkeypatch):
    app, window, store = desktop
    data = WIRE.replace(b'UID:family', b'UID:second')
    monkeypatch.setattr('debacal.ui.MAX_PENDING_IMPORT_BYTES', len(WIRE) + len(data))
    monkeypatch.setattr(ImportDialog, 'exec', accept_request)
    original_write = Store.import_report
    def reject(*_args, **_kwargs):
        raise ValueError('Try later')
    monkeypatch.setattr(Store, 'import_report', reject)
    for source in (WIRE, data):
        assert window.import_data(source)
        window._process_import_request()
        finish_parsing(window)
    first, second = window.local_issues
    assert first.details == second.details and first.key != second.key
    assert not window.can_accept_import(1)
    first.action()
    finish_parsing(window)
    assert {issue.key for issue in window.local_issues} == {first.key, second.key}
    assert second.dismiss is None
    for i in range(55):
        window.report_error(f'Independent error {i}')
    assert {first.key, second.key} <= {issue.key for issue in window.local_issues}
    discard, = second.extra_actions
    assert 'Discard' in discard.label
    discard.callback()
    second.action()  # A stale status dialog must never resurrect discarded work.
    assert window.can_accept_import(len(data)) and not window._import_jobs
    monkeypatch.setattr(Store, 'import_report', original_write)
    first.action()
    finish_parsing(window)
    assert {e.ical_uid for e in store.list_events()} == {'family', 'excluded'}
    assert window._import_memory_bytes == 0 and all(issue.key != first.key for issue in window.local_issues)


def test_destination_permissions_changed_after_accept_still_owns_stdin_retry(desktop, monkeypatch):
    app, window, store = desktop
    calendar = Calendar('team', 'Team', '#123456', kind='caldav')
    store.save_calendar(calendar)
    def review(dialog):
        result = accept_request(dialog)
        store.save_calendar(replace(calendar, read_only=True))
        return result
    monkeypatch.setattr(ImportDialog, 'exec', review)
    assert window.import_data(WIRE, calendar_id='team')
    window._process_import_request()
    finish_parsing(window)
    assert not store.list_events() and window._import_memory_bytes == len(WIRE)
    issue, = window.local_issues
    store.save_calendar(calendar)
    issue.action()
    finish_parsing(window)
    assert {event.calendar_id for event in store.list_events()} == {'team'}
    assert window._import_memory_bytes == 0 and not window.local_issues


def test_file_capacity_checked_before_acceptance_and_released_on_discard(desktop, tmp_path, monkeypatch):
    app, window, store = desktop
    wire = WIRE + b'\r\n'
    path = tmp_path / 'Later.ics'
    path.write_bytes(wire)
    save_reminder_preferences(store, ReminderPreferences(import_mode='add_missing', import_alarms=(TEMPLATE,)), now=START)
    monkeypatch.setattr('debacal.ui.MAX_PENDING_IMPORT_BYTES', len(wire))
    monkeypatch.setattr(ImportDialog, 'exec', accept_request)
    def reject(*_args, **_kwargs):
        raise ValueError('Try later')
    monkeypatch.setattr(Store, 'import_report', reject)
    assert window.import_data(WIRE)
    window._process_import_request()
    finish_parsing(window)
    retained, = window.local_issues
    def review(dialog):
        wait_for(app, lambda: dialog.preview_ready)
        assert dialog.reminder_mode.currentData() == 'add_missing'
        assert dialog.reminder_editor.alarms() == (TEMPLATE,)
        dialog.save()
        assert dialog.selected_prepared is None and dialog.error.text()
        assert not window._import_jobs
        retained.extra_actions[0].callback()
        return accept_request(dialog)
    monkeypatch.setattr(ImportDialog, 'exec', review)
    assert window.import_files([path])
    window._process_import_request()
    finish_parsing(window)
    assert window._import_memory_bytes == len(wire)
    remaining, = window.local_issues
    remaining.extra_actions[0].callback()
    assert window.can_accept_import(len(wire)) and not window.local_issues


def test_sync_and_subscription_keep_remote_alarms_under_remove_import_default(tmp_path):
    store, transport, engine = transport_setup(tmp_path)
    try:
        save_reminder_preferences(store, ReminderPreferences(import_mode='remove'), now=START)
        store.save_calendar(Calendar('feed', 'Feed', '#123456', kind='ics', enabled=False))
        report = parse_ics_report(WIRE, 'feed')
        store.refresh_subscription(report, 'feed')
        family = next(e for e in report.events if e.ical_uid == 'family')
        assert store.get_event(family.id).alarms[0].action == 'EMAIL'
        href = 'https://calendar.test/invited.ics'
        wire = WIRE[:WIRE.rindex(b'BEGIN:VEVENT')] + b'END:VCALENDAR'
        transport.resources[href] = RemoteResource(href, '"remote"', wire.decode())
        assert not [m for m in synchronize(engine) if m.kind == 'error']
        remote = next(e for e in store.list_events() if e.calendar_id == 'remote')
        assert remote.alarms[0].action == 'EMAIL'
    finally:
        engine.close()
        store.close()


@pytest.mark.parametrize('subset', [False, True])
def test_identical_duplicate_owned_by_last_source_in_accepted_selection(subset):
    from test_import_preparation import calendar, event
    shared = event('shared', extra='X-DEBACAL-ID:native-shared\r\nX-DEBACAL-CALENDAR:destination')
    prepared = prepare_sources((
        ImportSource.from_data(calendar(shared, event('unique'), event('omitted')), 'Earlier'),
        ImportSource.from_data(calendar(shared), 'Later')))
    selection = ImportSelection()
    if subset:
        selection.toggle(next(e for e in prepared.events if e.ical_uid == 'omitted'))
    accepted = prepared.with_selection(selection.snapshot(masters=prepared.events))
    earlier, later = (item.remap('destination') for item in accepted.items)
    assert [e.ical_uid for e in earlier.events] == (['unique'] if subset else ['unique', 'omitted'])
    assert [(e.ical_uid, e.id) for e in later.events] == [('shared', 'native-shared')]


def test_retry_recovers_unique_resource_and_diagnostic_without_overwriting_later_uid(desktop, monkeypatch):
    from test_import_preparation import calendar, event
    app, window, store = desktop
    older = ImportSource.from_data(calendar(
        event('shared', 'Older'), event('unique', 'Recover this'),
        event('bad', dates='DTSTART:20261106T140000Z\r\nDTEND:20261106T110000Z')), 'Earlier')
    reviewed = ImportSource.from_data(calendar(event('shared', 'Reviewed', extra=(
        'BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT5M\r\nDESCRIPTION:Reviewed alarm\r\nEND:VALARM'))), 'Later')
    def review(dialog):
        wait_for(app, lambda: dialog.preview_ready)
        assert '1 of 2' in dialog.reminder_summary.text()
        return accept_request(dialog)
    monkeypatch.setattr(ImportDialog, 'exec', review)
    original_write = Store.import_report
    failed = False
    def fail_earlier_once(writer, report, **kwargs):
        nonlocal failed
        if not failed and any(e.ical_uid == 'unique' for e in report.events):
            failed = True
            raise ValueError('First source failed temporarily')
        return original_write(writer, report, **kwargs)
    monkeypatch.setattr(Store, 'import_report', fail_earlier_once)
    assert window.import_sources((older, reviewed))
    window._process_import_request()
    finish_parsing(window)
    later, = store.list_events()
    assert later.title == 'Reviewed' and later.alarms[0].trigger == '-PT5M'
    update = replace(later, title='Edited after review', alarms=(TEMPLATE,))
    store.apply('Independent edit', [update])
    failure, = window.local_issues
    failure.action()
    finish_parsing(window)
    assert store.get_event(later.id) == update
    assert {e.ical_uid for e in store.list_events()} == {'shared', 'unique'}
    diagnostic, = store.list_quarantined_resources()
    assert 'UID:bad' in diagnostic['raw_ics']
    assert any('unsupported event resource' in issue.details for issue in window.local_issues)
    assert window._import_memory_bytes == 0
