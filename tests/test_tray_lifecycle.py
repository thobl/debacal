"""Workspace ownership across tray hiding, import review and graceful exit."""
from dataclasses import replace
import json
import tempfile
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from PySide6.QtNetwork import QLocalSocket

from debacal.import_dialog import ImportDialog
from debacal.single_instance import InstanceCoordinator, InstanceError, _request_bytes
from debacal.store import Store
from debacal.ui import MainWindow
from test_import_requests import accept_request, calendar_contents, write_calendar
from test_launch import isolated_process
from test_ui_database_actions import held_writer, until


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    # Exercise the real tray menu, signal wiring and visibility policy, without
    # registering anything on the desktop or sending real notifications.
    for method in ('show', 'hide', 'showMessage'):
        monkeypatch.setattr(QSystemTrayIcon, method, lambda *_: None)
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, 'supportsMessages', lambda: True)
    with Store(tmp_path / 'calendar.sqlite3') as store:
        window = MainWindow(store, enable_sync=False, profile_label='Testing')
        window.show()
        app.processEvents()
        try:
            yield window
        finally:
            if not window._closed:
                monkeypatch.setattr(ImportDialog, 'exec', lambda _: QDialog.DialogCode.Rejected)
                while window._import_requests:
                    if window._failed_review_issue:
                        window._failed_review_issue.extra_actions[0].callback()
                    else:
                        window._process_import_request()
                for key in list(window._failed_imports):
                    window._discard_import(key)
                window.reminder_preferences = replace(window.reminder_preferences, close_behavior='quit')
                window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def use_tray(window):
    window.apply_reminder_preferences(replace(window.reminder_preferences, close_behavior='tray'))


def test_hidden_quit_waits_for_reviewed_writes_but_restores_unreviewed_stdin(workspace, monkeypatch):
    window = workspace
    use_tray(window)
    window.close()
    assert not window.isVisible() and not window._closed and window.poll_timer.isActive()
    data = calendar_contents()
    assert window.import_data(data)
    window.request_quit(force=True)
    assert window.isVisible() and not window._closed and window._import_memory_bytes == len(data)

    def review(dialog):
        window.request_quit(force=True)
        assert not window._closed and window._import_dialog_active
        return accept_request(dialog)

    monkeypatch.setattr(ImportDialog, 'exec', review)
    with held_writer(window.store.path):
        window._process_import_request()
        window.close()
        window.request_quit(force=True)
        assert not window.isVisible() and not window._closed
        until(lambda: window.database_commands.pending > 0)
    until(lambda: window._closed)
    assert [event.title for event in window.store.list_events()] == ['From standard input']


@pytest.mark.parametrize('failure', ['commit', 'success callback'])
def test_write_failure_cancels_hidden_quit_and_reopens_admission(workspace, failure):
    window = workspace
    use_tray(window)
    def fail(*_):
        raise ValueError('Synthetic failure')
    with held_writer(window.store.path):
        window.write_database(fail if failure == 'commit' else lambda _: None,
                              on_success=fail, on_error=lambda error: window.report_error(str(error)))
        window.close()
        window.request_quit(force=True)
        assert not window.can_accept_import(1) and not window.isVisible()
    until(lambda: bool(window.local_issues))
    assert window.isVisible() and window.can_accept_import(1) and not window._closed
    assert 'Synthetic failure' in window.local_issues[-1].details


def test_failed_accepted_stdin_stays_recoverable_on_repeated_quit(workspace, monkeypatch):
    window = workspace
    use_tray(window)
    data = calendar_contents()
    monkeypatch.setattr(ImportDialog, 'exec', accept_request)
    def fail(*_, **__):
        raise ValueError('Synthetic import failure')
    monkeypatch.setattr(Store, 'import_report', fail)
    assert window.import_data(data)
    with held_writer(window.store.path):
        window._process_import_request()
        window.close()
        window.request_quit(force=True)
    until(lambda: bool(window.local_issues))
    assert window.isVisible()
    window.close()
    window.request_quit(force=True)
    assert window.isVisible() and not window._closed and window._import_memory_bytes == len(data)
    issue, = window.local_issues
    issue.extra_actions[0].callback()
    window.request_quit(force=True)
    assert window._closed


def test_tray_restores_maximized_workspace_modal_and_picker(workspace):
    window = workspace
    use_tray(window)
    window.showMaximized()
    modal = QDialog(window)
    modal.setWindowModality(Qt.WindowModality.ApplicationModal)
    modal.show()
    QApplication.processEvents()
    try:
        window.close()
        assert window.isVisible() and QApplication.activeModalWidget() is modal
    finally:
        modal.reject()
    window.occurrence_picker = object()
    try:
        window.close()
        assert window.isVisible() and not window._closed
    finally:
        window.occurrence_picker = None
    window.close()
    assert not window.isVisible()
    window.tray.showRequested.emit()
    assert window.isVisible() and window.isMaximized()


def test_missing_disappearing_tray_and_default_close(workspace, monkeypatch):
    window = workspace
    use_tray(window)
    window.close()
    assert not window._closed and not window.isVisible()
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: False)
    until(window.isVisible)
    window.close()
    assert window._closed


def test_tray_actions_profile_badge_and_conflict_route(workspace, monkeypatch):
    from debacal.sync import SyncMessage
    window = workspace
    icon = window.findChild(QSystemTrayIcon)
    show, quit_action = icon.contextMenu().actions()
    assert [show.text(), quit_action.text()] == ['Show Debacal', 'Quit Debacal']
    assert 'Testing' in icon.toolTip()
    use_tray(window)
    window.close()
    window.reminder_controller = SimpleNamespace(unread_count=3, stop=lambda: None)
    for enabled, delivery, badge in [(True, 'in_app', True), (False, 'both', False), (True, 'desktop', False)]:
        window._reminder_settings_changed(replace(window.reminder_preferences, enabled=enabled, delivery=delivery))
        assert ('3 unread' in icon.toolTip()) == badge
    clicked = []
    monkeypatch.setattr(window, 'show_conflicts', lambda: clicked.append(window.isVisible()))
    window.preferences = replace(window.preferences, conflict_notifications='desktop')
    window.notify_conflict(SyncMessage('conflict', 'Synthetic conflict'))
    icon.messageClicked.emit()
    assert clicked == [True]
    window.close()
    show.trigger()
    assert window.isVisible()
    quit_action.trigger()
    assert window._closed


def test_default_close_tears_down_with_tray_available(workspace):
    workspace.close()
    assert workspace._closed and not workspace.poll_timer.isActive()


def send_frame(server_name, frame):
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    assert socket.waitForConnected(1000)
    socket.write(frame)
    until(lambda: socket.bytesAvailable() or socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
    return bytes(socket.readAll())


def test_peer_reservations_survive_file_acceptance_and_hidden_quit(workspace, tmp_path, monkeypatch):
    window = workspace
    use_tray(window)
    path = write_calendar(tmp_path / 'file.ics')
    peer = calendar_contents()
    monkeypatch.setattr('debacal.ui.MAX_PENDING_IMPORT_BYTES', len(path.read_bytes()) + len(peer) - 1)
    with tempfile.TemporaryDirectory() as runtime:
        coordinator = InstanceCoordinator(window.store.path, runtime_dir=runtime)
        assert coordinator.start()
        window.bind_instance(coordinator)
        def send(frame):
            return json.loads(send_frame(coordinator.server_name, frame))
        request_id, frame = _request_bytes([], peer)
        observed = []
        def review(dialog):
            until(lambda: dialog.preview_ready)
            assert send(frame) == {'ok': True, 'request': request_id}
            dialog.save()
            observed.append((dialog.selected_prepared, bool(dialog.error.text())))
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ImportDialog, 'exec', review)
        assert window.import_files([path])
        window._process_import_request()
        assert observed == [(None, True)] and not window.can_accept_import(len(path.read_bytes()))
        window.close()
        window.request_quit(force=True)
        assert window.isVisible() and not window._closed
        # Show can deliver nested events, so ownership must already be in
        # the GUI queue with precisely one reservation when it is emitted.
        transfers = []
        coordinator.request_received.connect(lambda _: transfers.append((
            window._import_memory_bytes, coordinator.pending_imports, len(window._import_requests))))
        coordinator.enable_requests()
        coordinator._drain_requests()
        assert transfers == [(len(peer), (0, 0), 1)]
        monkeypatch.setattr(ImportDialog, 'exec', lambda _: QDialog.DialogCode.Rejected)
        window._process_import_request()
        with held_writer(window.store.path):
            window.write_database(lambda store: store.set_setting('saved', True))
            window.close()
            window.request_quit(force=True)
            assert send(frame) == {'ok': True, 'request': request_id}
            for files in ([], [path]):
                declined = send(_request_bytes(files)[1])
                assert declined['ok'] is False and 'shutting down' in declined['error']
            assert window.receive_file_open(path) and window.isVisible()
            assert send(_request_bytes([])[1])['ok']
        until(lambda: window.database_commands.pending == 0)
        assert not window._closed
        window.request_quit(force=True)
        assert window._closed and not window.receive_file_open(path)
        coordinator.close()
        successor = InstanceCoordinator(window.store.path, runtime_dir=runtime)
        assert successor.start()
        successor.close()


def test_unreviewable_root_path_is_rejected_before_ownership(workspace):
    with tempfile.TemporaryDirectory() as runtime:
        coordinator = InstanceCoordinator(workspace.store.path, runtime_dir=runtime)
        with pytest.raises((InstanceError, ValueError)):
            coordinator.start(['/'])
        assert coordinator.start()
        try:
            frame = json.dumps({'version': 1, 'request': 'a' * 32, 'files': ['/']}).encode() + b'\n'
            raw = send_frame(coordinator.server_name, frame)
            assert not raw or json.loads(raw)['ok'] is False
            assert coordinator.pending_imports == (0, 0)
        finally:
            coordinator.close()


def test_hidden_explicit_quit_actually_exits_qt_loop(tmp_path):
    result = isolated_process(tmp_path, r'''
import json, sys
from dataclasses import replace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QSystemTrayIcon
import debacal.__main__ as launch
QSystemTrayIcon.isSystemTrayAvailable = lambda: True
QSystemTrayIcon.show = lambda *_: None
windows, hidden = [], []
class Window(launch.MainWindow):
    def __init__(self, store, **options):
        super().__init__(store, **(options | {'enable_sync': False}))
        windows.append(self)
        self.apply_reminder_preferences(replace(self.reminder_preferences, close_behavior='tray'))
        def hide_then_quit():
            self.close()
            hidden.append(not self._closed and not self.isVisible())
            if hidden[-1]:
                QTimer.singleShot(25, self.tray.quitRequested.emit)
        QTimer.singleShot(25, hide_then_quit)
launch.MainWindow = Window
code = launch.main(['--test'], delivery_allowed=False)
print(json.dumps({'code': code, 'closed': windows[0]._closed, 'hidden': hidden}))
''')
    assert result == {'closed': True, 'code': 0, 'hidden': [True]}


@pytest.mark.parametrize('supply', ['file', 'stdin'])
def test_screenshot_rejects_imports_before_storage_or_ipc(tmp_path, monkeypatch, capsys, supply):
    import debacal.__main__ as launch
    def forbidden(*_, **__):
        pytest.fail('Screenshot with import inputs attempted startup')
    monkeypatch.setattr(launch, 'FileOpeningApplication', forbidden)
    monkeypatch.setattr(launch, 'read_standard_input', lambda **_: b'data' if supply == 'stdin' else None)
    args = ['--screenshot', str(tmp_path / 'capture.png')]
    assert launch.main(args + (['example.ics'] if supply == 'file' else [])) == 1
    assert 'screenshot' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('during_drain', ['qt_exit', 'native_open'])
@pytest.mark.parametrize('saved', [False, True])
def test_isolated_screenshot_waits_for_durable_write_and_preserves_status(tmp_path, saved, during_drain):
    result = isolated_process(tmp_path, r'''
import json, sys
from pathlib import Path
import threading
from PySide6.QtCore import QTimer
import debacal.__main__ as launch
from debacal.store import Store
path = Path(sys.argv[1])
database = path.with_suffix('.sqlite3')
states = []
class Window(launch.MainWindow):
    def grab(self):
        states.append(self._delivery_allowed)
        self.receive_file_open(path.with_suffix('.ics'))
        release = threading.Event()
        def write(writer):
            if not release.wait(3):
                raise ValueError('Timed out waiting for next Qt turn')
            writer.set_setting('capture_write', True)
        self.write_database(write, force_async=True)
        from PySide6.QtWidgets import QApplication
        def interrupt_capture():
            if sys.argv[2] == 'qt_exit':
                QApplication.instance().exit(0)
            else:
                from PySide6.QtGui import QFileOpenEvent
                QApplication.sendEvent(QApplication.instance(), QFileOpenEvent(str(path.with_suffix('.ics'))))
            QTimer.singleShot(30, release.set)
        QTimer.singleShot(0, interrupt_capture)
        def detect_stranded_capture():
            states.append('needed another quit')
            self.request_quit(force=True)
        QTimer.singleShot(1100, detect_stranded_capture)
        return super().grab()
    def receive_file_open(self, path):
        accepted = super().receive_file_open(path)
        states.append(accepted)
        return accepted
def forbidden(*_, **__):
    raise ValueError('Screenshot attempted IPC')
launch.InstanceCoordinator = forbidden
launch.MainWindow = Window
status = launch.main(['--database', str(database), '--screenshot', str(path)])
with Store(database) as store:
    print(json.dumps({'status': status, 'durable': store.get_setting('capture_write', False), 'states': states}))
''', str(tmp_path / ('capture.png' if saved else 'capture.unknown')), during_drain)
    expected = [False, False, False] if during_drain == 'native_open' else [False, False]
    assert result == {'status': 0 if saved else 1, 'durable': True, 'states': expected}


def test_demo_handoff_drains_old_workspace_and_replacement_owns_exit(tmp_path):
    result = isolated_process(tmp_path, r'''
import json, threading
from dataclasses import replace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QSystemTrayIcon
import debacal.__main__ as launch
from debacal.store import Store
QSystemTrayIcon.isSystemTrayAvailable = lambda: True
QSystemTrayIcon.show = lambda *_: None
states = []
class Window(launch.MainWindow):
    def __init__(self, store, **options):
        super().__init__(store, **(options | {'enable_sync': False}))
        self.apply_reminder_preferences(replace(self.reminder_preferences, close_behavior='tray'))
        if self.demo_mode:
            def switch():
                release = threading.Event()
                def write(writer):
                    if not release.wait(3):
                        raise ValueError('No Qt turn during handoff')
                    writer.set_setting('handoff', True)
                self.write_database(write, force_async=True)
                self.real_workspace_requested.emit()
                QTimer.singleShot(30, release.set)
            QTimer.singleShot(10, switch)
            self.workspaceClosed.connect(lambda: states.append(['demo closed', self.store.get_setting('handoff', False)]))
        else:
            def quit_replacement():
                states.append(['persistent active', self.isVisible() and not self._closed])
                self.request_quit(force=True)
            QTimer.singleShot(100, quit_replacement)
launch.MainWindow = Window
code = launch.main(['--demo', '--test'], delivery_allowed=False)
print(json.dumps({'code': code, 'states': states}))
''')
    assert result['states'] == [['demo closed', True], ['persistent active', True]]


def test_tray_host_can_disappear_between_first_hide_and_next_availability_poll(workspace, monkeypatch):
    window = workspace
    use_tray(window)
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: False)
    availability = []
    window.tray.availabilityChanged.connect(availability.append)
    until(lambda: availability == [False])
    # A newly started tray host is visible to the close decision before the
    # periodic host check. If it immediately exits, Show still has to work.
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: True)
    window.close()
    assert not window._closed and not window.isVisible()
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', lambda: False)
    until(window.isVisible, timeout=2.5)


def test_review_keeps_workspace_accessible_if_hidden_after_admission(workspace, monkeypatch):
    window = workspace
    use_tray(window)
    visible = []
    def review(_dialog):
        visible.append(window.isVisible())
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(ImportDialog, 'exec', review)
    assert window.import_data(calendar_contents())
    window.close()
    window._process_import_request()
    assert visible == [True]


@pytest.mark.parametrize('interrupt', ['quit', 'exit'])
@pytest.mark.parametrize('decision', ['import', 'cancel'])
def test_acknowledged_stdin_review_survives_qt_loop_exit(tmp_path, interrupt, decision):
    result = isolated_process(tmp_path, r'''
import json, sys
from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication
import debacal.__main__ as launch
from debacal.import_dialog import ImportDialog
from debacal.single_instance import _request_bytes
body = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:acknowledged\r\n'
        b'DTSTART:20261106T090000Z\r\nDTEND:20261106T100000Z\r\nSUMMARY:Reviewed after exit\r\n'
        b'END:VEVENT\r\nEND:VCALENDAR\r\n')
request_id, frame = _request_bytes([], body)
result = {'ack': False, 'resumed': False}
class Window(launch.MainWindow):
    def bind_instance(self, coordinator):
        super().bind_instance(coordinator)
        self.peer = QLocalSocket(self)
        self.review_monitor = QTimer(self)
        self.review_monitor.setInterval(10)
        self.interrupted = False
        def inspect():
            if self.peer.canReadLine():
                result['ack'] = json.loads(bytes(self.peer.readLine())) == {'ok': True, 'request': request_id}
            dialog = QApplication.activeModalWidget()
            if not result['ack'] or not isinstance(dialog, ImportDialog) or not dialog.preview_ready:
                return
            if not self.interrupted:
                self.interrupted = True
                # This draft choice distinguishes the live review from a fresh
                # dialog or an empty replacement after returning to app.exec().
                dialog.reminder_mode.setCurrentIndex(dialog.reminder_mode.findData('remove'))
                if sys.argv[1] == 'quit':
                    QApplication.instance().quit()
                else:
                    QApplication.instance().exit(0)
                return
            result['resumed'] = (self.isVisible() and dialog.isVisible()
                                 and self._import_memory_bytes == len(body)
                                 and dialog._sources[0].data == body
                                 and dialog.reminder_mode.currentData() == 'remove')
            self.review_monitor.stop()
            button = dialog.import_button if sys.argv[2] == 'import' else dialog.cancel_button
            button.click()
            QTimer.singleShot(50, lambda: self.request_quit(force=True))
        self.review_monitor.timeout.connect(inspect)
        self.workspaceClosed.connect(lambda: result.update(
            titles=[e.title for e in self.store.list_events()], remaining=self._import_memory_bytes))
        def send():
            self.peer.connectToServer(coordinator.server_name)
            self.peer.write(frame)
            self.review_monitor.start()
            QTimer.singleShot(2200, lambda: self.request_quit(force=True))
        QTimer.singleShot(0, send)
launch.MainWindow = Window
result['code'] = launch.main(['--test'], delivery_allowed=False)
print(json.dumps(result))
''', interrupt, decision)
    titles = ['Reviewed after exit'] if decision == 'import' else []
    assert result == {'code': 0, 'ack': True, 'resumed': True, 'titles': titles, 'remaining': 0}


@pytest.mark.parametrize(('operation', 'failed_read'), [('retry', 'list_calendars'), ('discard', 'get_default_calendar')])
def test_failed_review_keeps_recovery_until_retry_or_discard(workspace, monkeypatch, operation, failed_read):
    from sqlite3 import OperationalError
    from debacal.status_dialog import StatusDialog
    from PySide6.QtWidgets import QPushButton

    window = workspace
    first = calendar_contents('first', 'First invitation')
    second = calendar_contents('second', 'Second invitation')
    charge = len(first) + len(second)
    monkeypatch.setattr('debacal.ui.MAX_PENDING_IMPORT_BYTES', charge)
    assert window.import_data(first, label='First invitation')
    assert window.import_data(second, label='Second invitation')
    window._import_timer.stop()
    read = getattr(window.store, failed_read)
    def fail_once(*args, **kwargs):
        monkeypatch.setattr(window.store, failed_read, read)
        raise OperationalError('Synthetic temporarily unavailable calendars')
    monkeypatch.setattr(window.store, failed_read, fail_once)
    window._process_import_request()

    reviewed = []
    def review(dialog):
        reviewed.append(dialog._sources[0].label)
        return accept_request(dialog)
    monkeypatch.setattr(ImportDialog, 'exec', review)
    status = StatusDialog(window.status_issues, parent=window)
    try:
        status.show()
        QApplication.processEvents()
        assert 'temporarily unavailable' in status.details.toPlainText()
        status.dismiss_button.click()
        window.request_quit(force=True)
        assert window.isVisible() and not window._closed
        assert status.list.count() == 1 and not status.dismiss_button.isVisible()
        issue, = window.local_issues
        for i in range(55):
            window.report_error(f'Independent issue {i}')
        # Remove all dismissible rows through the existing action API. An old
        # caller trying to dismiss the pinned key must also leave it intact.
        for current in window.local_issues:
            window.dismiss_error(current.key)
        status.refresh()
        assert window.local_issues == (issue,) and window.warning_button.isVisible()
        assert window.status_issue_counts()[0] == 1 and status.list.count() == 1
        assert window._import_memory_bytes == charge and not window.can_accept_import(1)
        assert 'First invitation' in status.list.currentItem().text()
        discard, = issue.extra_actions
        if operation == 'retry':
            status.action_button.click()
            assert window._import_memory_bytes == charge
        else:
            button, = [button for button in status.extra_actions.findChildren(QPushButton)
                       if button.text() == 'Discard import']
            button.click()
            assert window._import_memory_bytes == len(second)
        # Actions from an older status dialog cannot operate on the next head.
        discard.callback()
        issue.action()
        until(lambda: not window._import_requests and not window._import_jobs)
        assert window._import_memory_bytes == 0 and not window.local_issues
        expected = ['First invitation', 'Second invitation'] if operation == 'retry' else ['Second invitation']
        assert reviewed == expected
        assert sorted(e.title for e in window.store.list_events()) == expected
        window.request_quit(force=True)
        assert window._closed
    finally:
        status.close()
        status.deleteLater()


def test_repeated_calendar_read_failure_still_allows_discard_in_calendar_status(workspace, monkeypatch):
    from sqlite3 import OperationalError
    from debacal.status_dialog import StatusDialog
    from PySide6.QtWidgets import QPushButton

    window = workspace
    data = calendar_contents()
    assert window.import_data(data)
    window._import_timer.stop()
    read = window.store.list_calendars
    def unavailable(*_, **__):
        raise OperationalError('Synthetic unreadable calendar list')
    monkeypatch.setattr(window.store, 'list_calendars', unavailable)
    window._process_import_request()
    original, = window.local_issues

    def retry(dialog):
        assert 'Synthetic unreadable calendar list' in dialog.sync_details.toPlainText()
        assert not dialog.dismiss_button.isVisible()
        dialog.action_button.click()
    monkeypatch.setattr(StatusDialog, 'exec', retry)
    window.show_issues()
    window._import_timer.stop()
    window._process_import_request()
    current, = window.local_issues
    assert current.key != original.key
    original.extra_actions[0].callback()
    original.action()
    assert window._import_memory_bytes == len(data) and window.local_issues == (current,)

    def discard(dialog):
        assert 'Synthetic unreadable calendar list' in dialog.details.toPlainText()
        monkeypatch.setattr(window.store, 'list_calendars', read)
        dialog.refresh()
        assert 'cached event series' in dialog.sync_details.toPlainText()
        monkeypatch.setattr(window.store, 'list_calendars', unavailable)
        dialog.refresh()
        button, = [button for button in dialog.extra_actions.findChildren(QPushButton)
                   if button.text() == 'Discard import']
        button.click()
        assert dialog.list.count() == 0
    monkeypatch.setattr(StatusDialog, 'exec', discard)
    window.show_issues()
    assert not window._import_requests and window._import_memory_bytes == 0
    window.request_quit(force=True)
    assert window._closed
