"""Import warnings identify each failed resource and permit reviewed retries."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
import hashlib
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource
from debacal.models import Calendar
from debacal.status_dialog import ResourceSourceDialog, StatusDialog, StatusIssue
from debacal.store import Store
from debacal.ui import MainWindow


CALENDARS = [Calendar('local', 'Personal', '#5f8a67'), Calendar('other', 'Other', '#315ab7')]


def calendar_data():
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0']
    for identity, title, zone in [('first-uid', 'First rejected event', 'Synthetic/Unknown'),
                                  ('second-uid', 'Second rejected event', 'Synthetic/Unknown')]:
        lines += ['BEGIN:VEVENT', 'UID:' + identity, 'SUMMARY:' + title,
                  'DTSTART;TZID=' + zone + ':20261104T090000',
                  'DTEND;TZID=' + zone + ':20261104T100000', 'END:VEVENT']
    lines += ['BEGIN:VEVENT', 'UID:healthy', 'SUMMARY:Healthy event',
              'DTSTART:20261104T110000Z', 'DTEND:20261104T120000Z', 'END:VEVENT', 'END:VCALENDAR']
    return ('\r\n'.join(lines) + '\r\n').encode()


def wait_ready(dialog):
    elapsed = QElapsedTimer()
    elapsed.start()
    while not dialog.preview_ready and elapsed.elapsed() < 3000:
        QApplication.processEvents()
        QTest.qWait(5)
    assert dialog.preview_ready


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        yield app, widgets
        app.clipboard().clear()
        for widget in reversed(widgets):
            widget.close()
            if isinstance(widget, MainWindow):
                widget.executor.shutdown(wait=True)
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def import_dialog(desktop, data=None):
    app, widgets = desktop
    dialog = ImportDialog(CALENDARS, calendar_id='other',
                          sources=[ImportSource.from_data(calendar_data() if data is None else data, 'Synthetic.ics')])
    widgets.append(dialog)
    dialog.show()
    app.processEvents()
    wait_ready(dialog)
    return dialog


def test_same_reason_in_one_file_is_reported_for_each_rejected_uid(desktop):
    dialog = import_dialog(desktop)
    assert dialog.prepared_import.event_count == 1
    assert len(dialog.prepared_import.items[0].report.rejected) == 2
    assert len(dialog._warnings) == 1  # The old text-only view deduplicated these.
    assert dialog.issues_button.text() == 'Review issues (2)'
    issues = dialog._import_issues()
    assert [issue.title for issue in issues] == ['First rejected event', 'Second rejected event']
    for issue, uid in zip(issues, ['first-uid', 'second-uid']):
        zone = 'Synthetic/Unknown'
        assert uid in issue.details and zone in issue.details
        assert 'Synthetic.ics' in issue.subtitle and '09:00' in issue.subtitle
        assert 'Import destination: Other' in issue.details
        assert 'not been imported' in issue.details
        assert 'continue syncing' not in issue.details
        assert issue.action_label == 'View original…' and not issue.close_on_action


def test_import_inspector_reuses_issue_selection_without_sync_controls(desktop, monkeypatch):
    app, _ = desktop
    owner = import_dialog(desktop)
    inspected = []
    def inspect(dialog):
        inspected.append(dialog)
        dialog.show()
        app.processEvents()
        assert dialog.windowTitle() == 'Import issues'
        assert dialog.tabs.count() == 1 and dialog.sync_details is None
        assert not dialog.tabs.tabBar().isVisible()
        assert not any(button.text() in {'Retry synchronization', 'Preferences…'}
                       for button in dialog.findChildren(QPushButton))
        assert dialog.list.count() == 2
        QTest.keyClick(dialog.list, Qt.Key.Key_Down)
        assert 'second-uid' in dialog.details.toPlainText()
        assert 'Synthetic/Unknown' in dialog.details.toPlainText()
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(StatusDialog, 'exec', inspect)
    owner._show_issues()
    assert len(inspected) == 1
    assert owner.result() == QDialog.DialogCode.Rejected
    assert not owner.selected_sources and owner.selected_prepared is None


def test_import_issue_view_save_and_copy_use_the_selected_resource_only(desktop, monkeypatch, tmp_path):
    app, _ = desktop
    owner = import_dialog(desktop)
    issue = owner._import_issues()[1]
    expected = owner.prepared_import.items[0].report.rejected[1].raw_ics
    shown = []
    def inspect(dialog):
        assert dialog.source.isReadOnly()
        shown.append(dialog.source.toPlainText())
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(ResourceSourceDialog, 'exec', inspect)
    issue.action()
    assert 'second-uid' in shown[0] and 'first-uid' not in shown[0]
    next(action for action in issue.extra_actions if action.label == 'Copy details').callback()
    assert 'second-uid' in app.clipboard().text() and 'Synthetic.ics' in app.clipboard().text()
    assert 'BEGIN:VEVENT' not in app.clipboard().text()
    destination = tmp_path / 'source.ics'
    monkeypatch.setattr('debacal.import_dialog.QFileDialog.getSaveFileName', lambda *args: (str(destination), ''))
    next(action for action in issue.extra_actions if action.label == 'Save original…').callback()
    assert destination.read_bytes() == (expected if isinstance(expected, bytes) else expected.encode())
    assert not owner.selected_sources


def test_whole_file_errors_remain_identifiable_in_shared_inspector(desktop):
    dialog = import_dialog(desktop, b'Not a calendar')
    issue, = dialog._import_issues()
    assert issue.title == 'Synthetic.ics'
    assert 'VCALENDAR' in issue.details
    assert not issue.action and not issue.extra_actions
    assert not dialog.import_button.isEnabled()


def test_import_inspector_reports_save_failure_without_closing_review(desktop, monkeypatch, tmp_path):
    app, _ = desktop
    owner = import_dialog(desktop)
    destination = tmp_path / 'missing-directory' / 'resource.ics'
    monkeypatch.setattr('debacal.import_dialog.QFileDialog.getSaveFileName', lambda *args: (str(destination), ''))
    def inspect(dialog):
        dialog.show()
        app.processEvents()
        save = next(button for button in dialog.extra_actions.findChildren(QPushButton)
                    if button.text() == 'Save original…')
        QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        assert dialog.isVisible()
        assert dialog.list.count() == 3
        assert dialog.list.currentItem().text() == 'Action failed'
        assert 'missing-directory' in dialog.details.toPlainText()
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(StatusDialog, 'exec', inspect)
    owner._show_issues()
    assert not destination.exists() and not owner.selected_sources


def test_status_dialog_keeps_existing_sync_tab_when_supplier_is_provided(desktop):
    _, widgets = desktop
    dialog = StatusDialog(lambda: [StatusIssue('one', 'Issue', 'Details')], lambda: 'Sync information')
    widgets.append(dialog)
    assert dialog.tabs.count() == 2
    assert dialog.tabs.currentIndex() == 1
    assert dialog.sync_details.toPlainText() == 'Sync information'


def test_retry_retained_local_import_opens_review_with_original_bytes_and_destination(desktop, monkeypatch):
    app, widgets = desktop
    with Store(':memory:') as store:
        store.save_calendar(CALENDARS[1])
        raw = calendar_data()
        store.set_quarantined_resource('other', 'retained', raw, 'Prior parser limitation')
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.show()
        issue, = window.status_issues()
        retry = next(action for action in issue.extra_actions if action.label == 'Retry import…')
        reviewed = []
        def inspect(dialog):
            reviewed.append(dialog)
            assert dialog._sources[0].data == raw
            assert dialog.calendar.currentData() == 'other'
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ImportDialog, 'exec', inspect)
        retry.callback()
        window._import_timer.stop()
        assert len(window._import_requests) == 1
        window._process_import_request()
        assert len(reviewed) == 1
        assert not store.list_events() and store.count_pending_changes() == 0
        assert store.get_quarantined_resource('other', 'retained')['raw_ics'] == raw
        window.close()
        window.executor.shutdown(wait=True)
        widgets.remove(window)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_retry_import_rechecks_calendar_write_access(desktop):
    app, widgets = desktop
    with Store(':memory:') as store:
        store.set_quarantined_resource('local', 'retained', calendar_data(), 'Prior parser limitation')
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        issue, = window.status_issues()
        retry = next(action for action in issue.extra_actions if action.label == 'Retry import…')
        store.save_calendar(replace(store.get_calendar('local'), read_only=True))
        assert not any(action.label == 'Retry import…' for action in window.status_issues()[0].extra_actions)
        with pytest.raises(ValueError, match='writable local calendar'):
            retry.callback()
        assert not window._import_requests and not store.list_events()
        window.close()
        window.executor.shutdown(wait=True)
        widgets.remove(window)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_accepted_retry_clears_old_failure_only_after_review_confirmation(desktop, monkeypatch):
    app, widgets = desktop
    raw = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:retry-uid\r\n'
           b'SUMMARY:Readable after parser update\r\nDTSTART:20261104T110000Z\r\n'
           b'DTEND:20261104T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    resource_id = hashlib.sha256(b'retry-uid').hexdigest()[:12]
    with Store(':memory:') as store:
        store.set_quarantined_resource('local', resource_id, raw, 'Prior parser limitation')
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        def review(dialog):
            assert not store.list_events()
            assert store.get_quarantined_resource('local', resource_id) is not None
            wait_ready(dialog)
            assert dialog.calendar.currentData() == 'local'
            dialog.save()
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(ImportDialog, 'exec', review)
        window.retry_quarantined_import('local', resource_id)
        window._import_timer.stop()
        assert not store.list_events()
        window._process_import_request()
        elapsed = QElapsedTimer()
        elapsed.start()
        while not store.list_events() and elapsed.elapsed() < 3000:
            window.poll()
            app.processEvents()
            QTest.qWait(5)
        assert len(store.list_events()) == 1
        assert store.get_quarantined_resource('local', resource_id) is None
        window.close()
        window.executor.shutdown(wait=True)
        widgets.remove(window)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
