"""CLI launches isolate profiles and deliver imports without duplicate storage."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QUrl

from debacal.__main__ import parse_arguments
from debacal.store import Store


ROOT = Path(__file__).resolve().parents[1]


def isolated_process(tmp_path, script, *arguments):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = os.environ | {
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    result = subprocess.run([sys.executable, "-c", script, *arguments], cwd=ROOT,
                            env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    decoded = json.loads(result.stdout)
    if decoded.get("code", 0) != 0:
        pytest.fail(result.stderr)
    return decoded


def test_cli_preserves_regular_defaults_and_can_choose_testing_or_named_profiles():
    assert parse_arguments([]).profile == "default"
    assert not parse_arguments([]).new_instance
    assert parse_arguments(["--test"]).profile == "testing"
    assert parse_arguments(["--profile", "Team-QA"]).profile == "team-qa"
    with pytest.raises(SystemExit):
        parse_arguments(["--profile", "qa", "--test"])
    with pytest.raises(SystemExit):
        parse_arguments(["--profile", "../default"])


def test_cli_delivers_absolute_paths_and_local_file_urls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    second = tmp_path / "calendar with spaces.ics"
    args = parse_arguments(["--test", "--new-instance", "relative.ics",
                            QUrl.fromLocalFile(str(second)).toString()])
    assert args.files == [tmp_path / "relative.ics", second]
    assert args.profile == "testing" and args.new_instance


STARTUP_SCRIPT = r'''
import json
from pathlib import Path
import sys
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget
import debacal.__main__ as launch

mode, arguments = sys.argv[1], json.loads(sys.argv[2])
opened, imports, claims = [], [], []

class Window(QWidget):
    real_workspace_requested = Signal()

    def __init__(self, store, **options):
        super().__init__()
        self.store, self._closed = store, False
        self.demo_mode = options.get('demo_mode', False)
        opened.append({'database': str(store.path), **options})
        QTimer.singleShot(60, QApplication.instance().quit)

    def import_files(self, files):
        imports.append([str(path) for path in files])

    def closeEvent(self, event):
        self._closed = True
        super().closeEvent(event)

launch.MainWindow = Window

def forbidden(*args, **kwargs):
    raise AssertionError('This launch must not open storage or another instance coordinator')

if mode == 'forwarded':
    class ExistingWorkspace:
        def __init__(self, database, parent=None):
            claims.append({'database': str(database)})

        def start(self, files):
            claims[-1]['files'] = [str(path) for path in files]
            return False

        def close(self):
            claims[-1]['closed'] = True

    launch.InstanceCoordinator = ExistingWorkspace
    launch.Store = forbidden
    launch.MainWindow = forbidden
elif mode == 'new':
    launch.InstanceCoordinator = forbidden

code = launch.main(arguments)
print(json.dumps({'code': code, 'opened': opened, 'imports': imports, 'claims': claims}))
'''


def test_existing_instance_delivery_does_not_open_database_or_window(tmp_path):
    calendar = tmp_path / "incoming.ics"
    result = isolated_process(tmp_path, STARTUP_SCRIPT, "forwarded", json.dumps([
        "--test", str(calendar)]))
    assert result["code"] == 0 and result["opened"] == []
    assert result["imports"] == []
    assert result["claims"][0]["files"] == [str(calendar)]
    assert result["claims"][0]["closed"]
    assert not list((tmp_path / "data").rglob("*.sqlite3"))


def test_empty_standard_input_opens_the_workspace_without_importing(tmp_path):
    result = isolated_process(tmp_path, STARTUP_SCRIPT, "new", json.dumps([
        "--test", "--new-instance"]))
    assert result["code"] == 0 and len(result["opened"]) == 1
    assert result["imports"] == []


@pytest.mark.parametrize("new_instance", [False, True])
def test_testing_profile_launch_delivers_files_with_isolated_storage(tmp_path, new_instance):
    calendar = tmp_path / "incoming.ics"
    arguments = ["--test", str(calendar)]
    if new_instance:
        arguments.insert(0, "--new-instance")
    result = isolated_process(tmp_path, STARTUP_SCRIPT, "new" if new_instance else "primary",
                              json.dumps(arguments))
    assert result["code"] == 0 and len(result["opened"]) == 1
    workspace = result["opened"][0]
    database = Path(workspace["database"])
    assert database.parts[-3:] == ("profiles", "testing", "calendar.sqlite3")
    assert database.is_file()
    assert workspace["profile_label"] == "Testing"
    assert workspace["credential_namespace"]
    assert workspace["enable_sync"] and not workspace["demo_mode"]
    assert result["imports"] == [[str(calendar)]]
    assert list((tmp_path / "data").rglob("*.sqlite3")) == [database]


def test_native_file_open_events_wait_for_the_workspace_and_are_not_replayed(tmp_path):
    result = isolated_process(tmp_path, r'''
import json
import sys
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication
from debacal.__main__ import FileOpeningApplication
app = FileOpeningApplication([])
received = []
app.file_open_requested.connect(received.append)
QApplication.sendEvent(app, QFileOpenEvent(sys.argv[1]))
early = list(received)
app.enable_file_requests()
app.enable_file_requests()
QApplication.sendEvent(app, QFileOpenEvent(sys.argv[2]))
print(json.dumps({'early': early, 'received': received}))
''', str(tmp_path / "before.ics"), str(tmp_path / "after.ics"))
    assert result["early"] == []
    assert result["received"] == [str(tmp_path / "before.ics"), str(tmp_path / "after.ics")]


def test_second_command_opens_one_import_dialog_in_the_running_workspace(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = os.environ | {
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    ready, reviewed = tmp_path / "ready.json", tmp_path / "reviewed.json"
    calendar = tmp_path / "calendar to review.ics"
    calendar.write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                        "UID:review-before-import\r\nDTSTART:20261106T090000Z\r\n"
                        "DTEND:20261106T100000Z\r\nSUMMARY:Review before importing\r\n"
                        "END:VEVENT\r\nEND:VCALENDAR\r\n")
    script = r'''
import json
from pathlib import Path
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog
import debacal.__main__ as launch
from debacal.import_dialog import ImportDialog

ready, reviewed = map(Path, sys.argv[1:])
real_window = launch.MainWindow
reviews = []

def window_without_network(*args, **options):
    options['enable_sync'] = False
    window = real_window(*args, **options)
    QTimer.singleShot(0, lambda: ready.write_text(json.dumps({'database': str(window.store.path)})))
    QTimer.singleShot(8000, lambda: QApplication.instance().exit(93))
    return window

def review_and_cancel(self):
    reviews.append({
        'files': [self.files.item(index).toolTip() for index in range(self.files.count())],
        'calendar': self.calendar.currentData(),
        'title': self.windowTitle(),
    })
    reviewed.write_text(json.dumps(reviews))
    QTimer.singleShot(50, QApplication.instance().quit)
    return QDialog.DialogCode.Rejected

launch.MainWindow = window_without_network
ImportDialog.exec = review_and_cancel
raise SystemExit(launch.main(['--test']))
'''
    primary = subprocess.Popen([sys.executable, "-c", script, str(ready), str(reviewed)], cwd=ROOT,
                               env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and primary.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        if not ready.exists():
            if primary.poll() is None:
                primary.terminate()
            _stdout, stderr = primary.communicate(timeout=3)
            pytest.fail("Primary workspace did not start: " + stderr)
        secondary = subprocess.run([sys.executable, "-m", "debacal", "--test", str(calendar)], cwd=ROOT,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=6)
        assert secondary.returncode == 0, secondary.stderr
        _stdout, stderr = primary.communicate(timeout=5)
        assert primary.returncode == 0, stderr
        assert json.loads(reviewed.read_text()) == [{
            "files": [str(calendar)], "calendar": "local", "title": "Import calendar files"}]
        with Store(json.loads(ready.read_text())["database"]) as store:
            assert store.list_events() == []
            assert store.history() == []
    finally:
        if primary.poll() is None:
            primary.terminate()
            try:
                primary.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                primary.kill()
                primary.communicate(timeout=3)
