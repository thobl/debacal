"""Issues live in the sidebar footer without a calendar message strip."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timedelta, timezone
from queue import Queue
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from debacal.models import Calendar, Event
from debacal.status_dialog import StatusDialog
from debacal.store import Store
from debacal.sync import SyncMessage
from debacal.ui import MainWindow

START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('debacal.ui.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False, profile_label='Testing')
        window.show()
        window.go_date(START.date())
        settle(app)
        try:
            yield app, window, store
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert not errors


def geometry(window):
    return window.stack.geometry(), window.scroll.viewport().size(), window.canvas.hour_height


def test_routine_actions_are_silent_and_removed_ui_does_not_reserve_space(workspace):
    app, window, store = workspace
    assert not hasattr(window, 'notice') and not hasattr(window, 'message_stack')
    assert window.findChild(QLabel, 'profileBadge') is None
    assert window.stack.mapTo(window, window.stack.rect().bottomLeft()).y() >= window.height() - 10
    source = Event('entry', 'local', 'Synthetic entry', START, START + timedelta(hours=1))
    window.apply_events('Create', [source])
    window.select_event(source.id)
    window.copy_event()
    window.delete_event()
    window.undo()
    window.redo()
    settle(app)
    assert not window.local_issues and not window.status_issues()
    assert not window.warning_button.isVisible()


def test_error_details_are_not_lost_and_review_actions_do_not_resize_calendar(workspace):
    app, window, _ = workspace
    baseline = geometry(window)
    called = []
    text = 'A detailed failure\n' * 20
    identifier = window.report_error(text, title='Export failed', action_label='Try export again',
                                     action=lambda: called.append('retry'))
    assert window.report_error(text, title='Export failed') == identifier
    settle(app)
    assert len(window.local_issues) == 1 and window.warning_button.isVisible()
    assert geometry(window) == baseline
    dialog = StatusDialog(window.status_issues, window.synchronization_details, window, show_issues=True)
    try:
        dialog.show()
        settle(app)
        assert text in dialog.details.toPlainText()
        assert dialog.action_button.text() == 'Try export again'
        QTest.mouseClick(dialog.action_button, Qt.MouseButton.LeftButton)
        assert called == ['retry'] and not dialog.isVisible() and not dialog.timer.isActive()
    finally:
        dialog.deleteLater()
    dialog = StatusDialog(window.status_issues, window.synchronization_details, window, show_issues=True)
    try:
        dialog.show()
        QTest.mouseClick(dialog.dismiss_button, Qt.MouseButton.LeftButton)
        settle(app)
        assert not window.local_issues and not window.warning_button.isVisible()
        assert dialog.empty.isVisible()
        assert geometry(window) == baseline
    finally:
        dialog.close()
        dialog.deleteLater()


class Engine:
    def __init__(self):
        self.events = Queue()
        self.conflicts = []
        self.calls = 0
    def sync_now(self):
        self.calls += 1
    def close(self):
        pass


def test_sync_issues_share_footer_and_can_be_retried_or_reviewed(workspace):
    app, window, store = workspace
    store.save_calendar(Calendar('remote', 'Synthetic remote', '#587c67', kind='caldav', url='https://calendar.invalid/'))
    window.engine = Engine()
    window.engine.events.put(SyncMessage('error', 'Authentication failed', 'remote'))
    window.engine.events.put(SyncMessage('finished', 'Sync finished with errors'))
    window.poll()
    settle(app)
    assert not window.local_issues
    assert window.warning_button.isVisible()
    assert 'Authentication failed' in window.status_issues()[0].details
    assert window.status_issues()[0].action_label == 'Edit calendar connection…'
    dialog = StatusDialog(window.status_issues, window.synchronization_details, window,
                          show_issues=True, retry_sync=window.sync_now)
    try:
        dialog.show()
        retry = next(button for button in dialog.findChildren(QPushButton)
                     if button.text() == 'Retry synchronization')
        QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
        assert window.engine.calls == 1
        window.engine.events.put(SyncMessage('started', 'Starting synchronization'))
        window.engine.events.put(SyncMessage('finished', 'Finished'))
        window.poll()
        dialog.refresh()
        assert not window.status_issues() and dialog.empty.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_warning_button_opens_issue_details_directly(workspace):
    _, window, _ = workspace
    window.report_error('Synthetic failure')
    with patch.object(StatusDialog, 'exec', return_value=0) as opened:
        window.warning_button.click()
        assert opened.call_count == 1
