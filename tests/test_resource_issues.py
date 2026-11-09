"""Rejected resources remain identifiable and inspectable without server edits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timezone
import hashlib
from queue import Queue
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from debacal.models import Calendar
from debacal.ics import parse_ics_report
from debacal.preferences import Preferences
from debacal.resource_diagnostics import (DIAGNOSTIC_PREFIX_LIMIT, SOURCE_PREVIEW_LIMIT,
                                          resource_diagnostics, resource_when)
from debacal.status_dialog import IssueAction, ResourceSourceDialog, StatusDialog, StatusIssue
from debacal.store import Store
from debacal.sync import SyncMessage
from debacal.ui import MainWindow


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def raw_event(title='Synthetic event', uid='synthetic', zone='W. Europe Standard Time'):
    return ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:' + uid + '\r\nSUMMARY:' + title +
            '\r\nDTSTART;TZID="' + zone + '":20261104T090000\r\n' +
            'DTEND;TZID="' + zone + '":20261104T100000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()


class Engine:
    def __init__(self):
        self.events = Queue()
        self.conflicts = []
        self.calls = 0

    def sync_now(self):
        self.calls += 1

    def close(self):
        pass


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        app.processEvents()
        try:
            yield app, window, store
        finally:
            app.clipboard().clear()
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert not errors


def test_metadata_reads_master_and_folded_properties_without_alarm_identity():
    raw = (b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nRECURRENCE-ID:20261105T090000\r\n'
           b'SUMMARY:Detached title\r\nUID:master-uid\r\nEND:VEVENT\r\n'
           b'BEGIN:VEVENT\r\nBEGIN:VALARM\r\nSUMMARY:Alarm title\r\nUID:alarm-uid\r\nEND:VALARM\r\n'
           b'SUMMARY:Folded\\, title\r\n continued\\nnext\r\nUID:master-uid\r\n'
           b'DTSTART;TZID="Example;Zone:One":20261104T090000\r\nEND:VEVENT\r\nEND:VCALENDAR')
    metadata = resource_diagnostics(raw)
    assert metadata['title'] == 'Folded, titlecontinued next'
    assert metadata['uid'] == 'master-uid'
    assert metadata['start'] == '20261104T090000'
    assert metadata['timezones'] == ['Example;Zone:One']
    assert 'Alarm' not in str(metadata)
    assert 'Detached title' not in str(metadata)
    assert '2026' in resource_when(metadata, Preferences())


def test_metadata_is_bounded_and_can_identify_damaged_partial_event():
    raw = (b'BEGIN:VEVENT\nSUMMARY:Partial\xff\nUID:recoverable\nDTSTART;VALUE=DATE:20261104\n' +
           b'DESCRIPTION:' + b'x' * (DIAGNOSTIC_PREFIX_LIMIT + 1000))
    metadata = resource_diagnostics(raw)
    assert metadata['title'] == 'Partial\ufffd'
    assert metadata['uid'] == 'recoverable'
    assert metadata['all_day'] and metadata['truncated']
    assert len(str(metadata)) < 1500
    assert 'All day' in resource_when(metadata, Preferences())


def test_store_projects_once_preserves_original_and_survives_restart(tmp_path):
    path = tmp_path / 'test.sqlite3'
    original = raw_event() + b'\xff'
    with Store(path) as store:
        store.set_quarantined_resource('local', 'bad', original, 'Unsupported timezone definition')
        with patch('debacal.resource_diagnostics.resource_diagnostics', side_effect=AssertionError('parsed twice')):
            for _ in range(3):
                row, = store.quarantined_resource_summaries()
                assert 'raw_ics' not in row
                assert row['diagnostics']['title'] == 'Synthetic event'
        assert store.get_quarantined_resource('local', 'bad')['raw_ics'] == original
    with Store(path) as store:
        assert store.quarantined_resource_summaries()[0]['diagnostics']['title'] == 'Synthetic event'
        assert store.get_quarantined_resource('local', 'bad')['raw_ics'] == original
        store.delete_quarantined_resource('local', 'bad')
        assert not store.quarantined_resource_summaries()


def test_old_quarantine_schema_backfills_bounded_metadata_once(tmp_path):
    path = tmp_path / 'old.sqlite3'
    original = raw_event('Older retained event') + b'x' * DIAGNOSTIC_PREFIX_LIMIT
    with Store(path) as store:
        store.connection.execute('DROP TABLE quarantined_resources')
        store.connection.execute('CREATE TABLE quarantined_resources '
                                 '(calendar_id TEXT,resource_id TEXT,raw_ics BLOB,reason TEXT,updated_at TEXT,'
                                 'PRIMARY KEY(calendar_id,resource_id))')
        store.connection.execute('INSERT INTO quarantined_resources VALUES(?,?,?,?,?)',
                                 ('local', 'old', original, 'Test failure', '2026-11-04T09:00:00Z'))
        store.connection.commit()
    with Store(path) as store:
        row, = store.quarantined_resource_summaries()
        assert row['diagnostics']['title'] == 'Older retained event'
        assert row['diagnostics']['truncated']
        assert row['updated_at'] == '2026-11-04T09:00:00Z'
        assert store.get_quarantined_resource('local', 'old')['raw_ics'] == original
    with patch('debacal.resource_diagnostics.resource_diagnostics', side_effect=AssertionError('backfilled twice')):
        with Store(path) as store:
            assert len(store.quarantined_resource_summaries()) == 1


def test_corrected_file_import_clears_only_the_repaired_uid():
    with Store(':memory:') as store:
        resource_id = hashlib.sha256(b'fixed-uid').hexdigest()[:12]
        store.set_quarantined_resource('local', resource_id, raw_event(uid='fixed-uid'), 'Prior failure')
        store.set_quarantined_resource('local', 'still-bad', raw_event(uid='other-uid'), 'Other failure')
        report = parse_ics_report(raw_event(uid='fixed-uid', zone='UTC'), 'local')
        store.import_report(report, calendar_id='local')
        rows = store.quarantined_resource_summaries()
        assert [row['resource_id'] for row in rows] == ['still-bad']
        assert len(store.list_events()) == 1


def test_each_failed_event_is_listed_and_refresh_never_loads_raw_source(workspace):
    _, window, store = workspace
    store.save_calendar(Calendar('remote', 'Synthetic source', '#587c67', kind='caldav', url='https://example.invalid/'))
    store.set_quarantined_resource('remote', 'first', raw_event('First failed event', 'first-uid'), 'Unsupported timezone definition')
    store.set_quarantined_resource('remote', 'second', raw_event('Second failed event', 'second-uid'), 'Unsupported timezone definition')
    window._sync_issues['remote'] = 'Synthetic source: resource second: Unsupported timezone definition'
    traces = []
    store.connection.set_trace_callback(traces.append)
    with patch.object(store, 'get_quarantined_resource', side_effect=AssertionError('raw loaded')), \
         patch.object(store, 'list_quarantined_resources', side_effect=AssertionError('all raw loaded')), \
         patch('debacal.resource_diagnostics.resource_diagnostics', side_effect=AssertionError('reparsed')):
        for _ in range(3):
            issues = window.status_issues()
            window.synchronization_details()
            window.update_sync_status()
    assert [issue.title for issue in issues] == ['First failed event', 'Second failed event']
    assert 'Synthetic source' in issues[0].subtitle
    assert 'W. Europe Standard Time' in issues[0].details
    assert 'first-uid' in issues[0].details
    assert 'raw_ics' not in '\n'.join(traces)
    assert '2 issue(s)' in window.warning_button.toolTip()
    window._sync_issues['remote'] = 'Server calendar colors could not be refreshed'
    assert len(window.status_issues()) == 3


def test_retry_keeps_dialog_open_and_success_removes_only_repaired_resources(workspace):
    app, window, store = workspace
    store.save_calendar(Calendar('remote', 'Remote', '#587c67', kind='caldav', url='https://example.invalid/'))
    window.engine = Engine()
    for resource in ('one', 'two'):
        store.set_quarantined_resource('remote', resource, raw_event(resource), 'Invalid resource')
    dialog = StatusDialog(window.status_issues, window.synchronization_details, window,
                          show_issues=True, retry_sync=window.sync_now)
    try:
        dialog.show()
        app.processEvents()
        retry = next(button for button in dialog.findChildren(QPushButton)
                     if button.text() == 'Retry synchronization')
        QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
        assert dialog.isVisible() and window.engine.calls == 1
        store.delete_quarantined_resource('remote', 'one')
        window.engine.events.put(SyncMessage('started', 'Sync started'))
        window.engine.events.put(SyncMessage('finished', 'Sync completed'))
        window.poll()
        dialog.refresh()
        assert dialog.list.count() == 1
        assert dialog.list.item(0).text().startswith('two')
        store.delete_quarantined_resource('remote', 'two')
        dialog.refresh()
        window.update_sync_status()
        assert dialog.empty.isVisible() and not window.warning_button.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_metadata_warning_survives_later_resource_warnings(workspace):
    _, window, store = workspace
    store.save_calendar(Calendar('remote', 'Remote', '#587c67', kind='caldav', url='https://example.invalid/'))
    window.engine = Engine()
    store.set_quarantined_resource('remote', 'bad', raw_event(), 'Invalid resource')
    window.engine.events.put(SyncMessage('issue', 'Server colors could not be refreshed', 'remote'))
    for _ in range(300):
        window.engine.events.put(SyncMessage('issue', 'Remote: resource bad: Invalid resource', 'remote'))
    window.poll()
    issues = window.status_issues()
    assert len(issues) == 2
    assert any(issue.details == 'Server colors could not be refreshed' for issue in issues)
    window.engine.events.put(SyncMessage('started', 'Starting new synchronization'))
    window.poll()
    assert len(window.status_issues()) == 1


def test_view_copy_and_export_are_explicit_read_only_actions(workspace, tmp_path):
    app, window, store = workspace
    original = raw_event('Read-only failed event') + b'\xff'
    store.save_calendar(Calendar('feed', 'Read-only feed', '#587c67', kind='ics', url='https://example.invalid/feed.ics', read_only=True))
    store.set_quarantined_resource('feed', 'bad', original, 'Unsupported timezone definition')
    window.engine = Engine()
    issue, = window.status_issues()
    assert 'read-only' in issue.details
    assert issue.action_label == 'View original…' and not issue.close_on_action
    assert 'Edit calendar connection' not in str(issue)
    next(action for action in issue.extra_actions if action.label == 'Copy details').callback()
    assert 'Read-only failed event' in app.clipboard().text()
    assert 'W. Europe Standard Time' in app.clipboard().text()
    assert 'BEGIN:VEVENT' not in app.clipboard().text()
    with patch('debacal.ui.ResourceSourceDialog.exec', return_value=0):
        issue.action()
    destination = tmp_path / 'retained.ics'
    with patch('debacal.ui.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
        next(action for action in issue.extra_actions if action.label == 'Save original…').callback()
    assert destination.read_bytes() == original
    assert store.get_quarantined_resource('feed', 'bad')['raw_ics'] == original
    assert store.count_pending_changes() == 0 and window.engine.calls == 0
    with patch('debacal.ui.QFileDialog.getSaveFileName', return_value=('', '')), \
         patch.object(store, 'get_quarantined_resource', side_effect=AssertionError('cancel loaded source')):
        window.export_quarantined_resource('feed', 'bad')


def test_source_preview_is_plain_text_read_only_and_bounded(workspace):
    _, window, _ = workspace
    raw = b'<b>Not HTML</b>\n' + b'x' * SOURCE_PREVIEW_LIMIT
    dialog = ResourceSourceDialog(raw, window)
    try:
        assert dialog.source.isReadOnly()
        assert dialog.source.toPlainText().startswith('<b>Not HTML</b>')
        assert len(dialog.source.toPlainText()) == SOURCE_PREVIEW_LIMIT
    finally:
        dialog.deleteLater()


def test_local_import_without_uid_is_identifiable_and_fixed_source_can_be_imported(workspace):
    _, window, store = workspace
    store.set_quarantined_resource('local', 'import-row-1', b'BEGIN:VEVENT\nSUMMARY:Missing UID\nEND:VEVENT', 'Missing event UID')
    issue, = window.status_issues()
    assert issue.title == 'Missing UID'
    assert 'import-row-1' in issue.details
    assert 'corrected file' in issue.details
    assert any(action.label == 'Import corrected file…' for action in issue.extra_actions)


def test_extra_action_failure_is_reported_and_original_issue_remains(workspace):
    app, window, _ = workspace
    def fail():
        raise ValueError('Synthetic action failure')
    original = StatusIssue('one', 'Retained issue', 'Details', extra_actions=(IssueAction('Try action', fail),))
    errors = []
    dialog = StatusDialog(lambda: [original], lambda: '', window, show_issues=True, action_error=errors.append)
    try:
        dialog.show()
        app.processEvents()
        button = dialog.extra_actions.findChild(QPushButton)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert errors == ['Synthetic action failure']
        assert dialog.isVisible() and dialog.list.count() == 1
    finally:
        dialog.close()
        dialog.deleteLater()
