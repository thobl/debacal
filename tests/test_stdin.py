"""Standard input is optional, bounded, and preserves calendar bytes."""
from io import BytesIO, StringIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from debacal.__main__ import parse_arguments, read_standard_input
from debacal.import_sources import MAX_IMPORT_BYTES
from debacal.store import Store


def test_redirected_standard_input_retains_utf8_and_line_endings():
    data = 'BEGIN:VCALENDAR\r\nSUMMARY:Café 🗓\r\nEND:VCALENDAR\r\n'.encode()
    assert read_standard_input(BytesIO(data)) == data
    assert read_standard_input(StringIO(data.decode())) == data


def test_interactive_terminal_is_not_read_unless_explicitly_requested():
    class Terminal(BytesIO):
        def isatty(self):
            return True

    stream = Terminal(b'calendar bytes')
    assert read_standard_input(stream) is None
    assert stream.tell() == 0
    assert read_standard_input(stream, force=True) == b'calendar bytes'


def test_empty_standard_input_does_not_request_an_import():
    assert read_standard_input(BytesIO()) is None
    assert read_standard_input(BytesIO(), force=True) is None


def test_standard_input_size_limit_and_read_errors_are_actionable():
    with pytest.raises(ValueError, match='20 MB'):
        read_standard_input(BytesIO(b'x' * (MAX_IMPORT_BYTES + 1)))

    class Unreadable(BytesIO):
        def read(self, *_args):
            raise OSError('input unavailable')

    with pytest.raises(ValueError, match='Could not read standard input: input unavailable'):
        read_standard_input(Unreadable())


def test_explicit_stdin_marker_combines_with_file_arguments(tmp_path):
    file = tmp_path / 'other.ics'
    args = parse_arguments(['--test', '-', str(file)])
    assert args.stdin and args.files == [file]
    assert not parse_arguments([]).stdin
    with pytest.raises(SystemExit):
        parse_arguments(['-', '-'])


REVIEW_STDIN_SCRIPT = r'''
import hashlib
import json
from pathlib import Path
import sys
from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
import debacal.__main__ as launch
from debacal.import_dialog import ImportDialog

ready, reviewed = map(Path, sys.argv[1:3])
accept = sys.argv[3] == 'accept'
expected = int(sys.argv[4])
arguments = json.loads(sys.argv[5])
real_window = launch.MainWindow
reviews = []

def make_window(*args, **options):
    options['enable_sync'] = False
    window = real_window(*args, **options)
    QTimer.singleShot(0, lambda: ready.write_text(json.dumps({'database': str(window.store.path)})))
    QTimer.singleShot(9000, lambda: QApplication.instance().exit(93))
    return window

def finish(window):
    window.poll()
    events = window.store.list_events()
    if len(events) != expected:
        QTimer.singleShot(20, lambda: finish(window))
        return
    reviewed.write_text(json.dumps({
        'reviews': reviews, 'titles': sorted(event.title for event in events),
        'history': len(window.store.history()),
    }))
    QApplication.instance().quit()

def review(self):
    window = self.parentWidget()
    elapsed = QElapsedTimer()
    elapsed.start()
    while not self.preview_ready and elapsed.elapsed() < 5000:
        QApplication.processEvents()
        QTest.qWait(5)
    assert self.preview_ready
    reviews.append({
        'sources': [{'path': str(source.path) if source.path is not None else None,
                     'label': source.label,
                     'sha256': hashlib.sha256(source.data).hexdigest() if source.data is not None else None}
                    for source in self._sources],
        'calendar': self.calendar.currentData(),
        'events_before_confirming': len(window.store.list_events()),
        'preview_titles': sorted(event.title for event in self.prepared_import.events),
    })
    if accept:
        self.save()
    QTimer.singleShot(30, lambda: finish(window))
    return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

launch.MainWindow = make_window
ImportDialog.exec = review
raise SystemExit(launch.main(arguments))
'''


def ics_bytes(uid, title, *, large=False):
    description = ('detail-' * 14000 + r'\nSecond paragraph') if large else 'Calendar input'
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n'
            f'UID:{uid}\r\nDTSTART:20261106T090000Z\r\nDTEND:20261106T100000Z\r\n'
            f'SUMMARY:{title}\r\nDESCRIPTION:{description}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()


@pytest.mark.parametrize('running,accept,new_instance', [
    (False, False, False), (False, True, False),
    (True, False, False), (True, True, False),
    (False, True, True),
])
def test_redirected_stdin_opens_the_same_review_and_import_flow(tmp_path, running, accept, new_instance):
    root = Path(__file__).resolve().parents[1]
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    environment = os.environ | {
        'QT_QPA_PLATFORM': 'offscreen', 'XDG_RUNTIME_DIR': str(runtime),
        'XDG_DATA_HOME': str(tmp_path / 'data'), 'XDG_CONFIG_HOME': str(tmp_path / 'config'),
        'XDG_CACHE_HOME': str(tmp_path / 'cache'),
    }
    ready, reviewed = tmp_path / 'ready.json', tmp_path / 'reviewed.json'
    incoming = tmp_path / 'redirected.ics'
    data = ics_bytes('stdin-calendar', 'Café 🗓 from stdin', large=True)
    assert len(data) > 64 * 1024
    incoming.write_bytes(data)
    argument = tmp_path / 'argument.ics'
    arguments = ['--test']
    if accept:
        argument.write_bytes(ics_bytes('argument-calendar', 'From file argument'))
        arguments.append(str(argument))
    if new_instance:
        arguments.append('--new-instance')
    expected = 2 if accept else 0
    command = [sys.executable, '-c', REVIEW_STDIN_SCRIPT, str(ready), str(reviewed),
               'accept' if accept else 'cancel', str(expected), json.dumps(['--test'] if running else arguments)]
    primary = None
    with incoming.open('rb') as input_stream:
        try:
            input_source = subprocess.DEVNULL if running else subprocess.PIPE if new_instance else input_stream
            primary = subprocess.Popen(command, cwd=root, env=environment, stdin=input_source,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if running:
                deadline = time.monotonic() + 5
                while not ready.exists() and primary.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                assert ready.exists(), 'Primary workspace did not start'
                secondary = subprocess.run([sys.executable, '-m', 'debacal', *arguments], cwd=root,
                                           env=environment, stdin=input_stream, capture_output=True,
                                           text=True, timeout=7)
                assert secondary.returncode == 0, secondary.stderr
            _stdout, stderr = primary.communicate(input=data if new_instance else None, timeout=10)
            assert primary.returncode == 0, stderr
            result = json.loads(reviewed.read_text())
            assert len(result['reviews']) == 1
            review = result['reviews'][0]
            assert review['calendar'] == 'local' and review['events_before_confirming'] == 0
            assert review['preview_titles'] == (['Café 🗓 from stdin', 'From file argument'] if accept
                                               else ['Café 🗓 from stdin'])
            stdin = next(source for source in review['sources'] if source['path'] is None)
            assert stdin == {'path': None, 'label': 'Standard input', 'sha256': hashlib.sha256(data).hexdigest()}
            assert len(review['sources']) == (2 if accept else 1)
            if accept:
                assert result['titles'] == ['Café 🗓 from stdin', 'From file argument']
                assert result['history'] == 2
            else:
                assert result['titles'] == [] and result['history'] == 0
            with Store(json.loads(ready.read_text())['database']) as store:
                assert len(store.list_events()) == expected
            assert set(tmp_path.rglob('*.ics')) == ({incoming, argument} if accept else {incoming})
        finally:
            if primary is not None and primary.poll() is None:
                primary.terminate()
                try:
                    primary.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    primary.kill()
                    primary.communicate(timeout=3)
