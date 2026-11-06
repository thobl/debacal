"""Import requests choose a destination and require confirmation before I/O."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.import_dialog import ImportDialog
from debacal.models import Calendar


CALENDARS = [Calendar('local', 'Personal', '#5f8a67'),
             Calendar('subscription', 'Feed', '#a26d4d', kind='ics'),
             Calendar('readonly', 'Shared', '#8c78ac', kind='caldav', read_only=True),
             Calendar('work', 'Work', '#315ab7', kind='caldav')]


@pytest.fixture
def dialogs():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _type, error, _traceback: errors.append(error)):
        def make(*args, **kwargs):
            dialog = ImportDialog(*args, **kwargs)
            widgets.append(dialog)
            dialog.show()
            dialog.activateWindow()
            app.processEvents()
            return dialog
        yield make
        for dialog in reversed(widgets):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_confirmed_multiple_file_request_uses_chosen_writable_calendar(dialogs, tmp_path):
    paths = [tmp_path / 'Team meetings.ics', tmp_path / 'Urlaub ü.ics']
    widget = dialogs(CALENDARS, [*paths, paths[0]], calendar_id='work')
    assert widget.calendar.count() == 2
    assert widget.calendar.currentData() == 'work'
    assert widget.selected_paths == () and widget.selected_calendar_id == ''
    widget.calendar.setCurrentIndex(widget.calendar.findData('local'))
    QTest.keyClick(widget.calendar, Qt.Key.Key_Return)
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.selected_paths == tuple(paths) and widget.selected_calendar_id == 'local'


def test_cancelling_does_not_produce_an_import_request(dialogs, tmp_path):
    widget = dialogs(CALENDARS, [tmp_path / 'Invitation.ics'], calendar_id='local')
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    assert widget.result() == QDialog.DialogCode.Rejected
    assert widget.selected_paths == () and widget.selected_calendar_id == ''


def test_dialog_never_reads_or_stats_requested_files_on_the_gui_thread(dialogs, tmp_path):
    path = tmp_path / 'Not yet readable.ics'
    with patch.object(Path, 'stat', side_effect=AssertionError('UI attempted file I/O')), \
         patch.object(Path, 'read_bytes', side_effect=AssertionError('UI attempted file I/O')), \
         patch.object(Path, 'resolve', side_effect=AssertionError('UI attempted file I/O')):
        widget = dialogs(CALENDARS, [path], calendar_id='work')
        widget.save()
        assert widget.result() == QDialog.DialogCode.Accepted
        assert widget.selected_paths == (path,)


def test_browse_cancel_retains_files_and_new_selection_replaces_them(dialogs, tmp_path):
    first, second = tmp_path / 'First.ics', tmp_path / 'Second.ics'
    widget = dialogs(CALENDARS, [first])
    with patch('debacal.import_dialog.QFileDialog.getOpenFileNames', return_value=([], '')):
        widget.browse()
    widget.save()
    assert widget.selected_paths == (first,)

    widget = dialogs(CALENDARS, [first])
    with patch('debacal.import_dialog.QFileDialog.getOpenFileNames', return_value=([str(second)], '')):
        widget.browse()
    QTest.keyClick(widget.calendar, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert widget.selected_paths == (second,)


def test_empty_request_or_no_writable_destination_cannot_be_accepted(dialogs, tmp_path):
    widget = dialogs(CALENDARS)
    assert not widget.import_button.isEnabled()
    widget.save()
    assert widget.result() == QDialog.DialogCode.Rejected
    assert widget.selected_paths == () and widget.error.text()
    widget = dialogs(CALENDARS[1:3], [tmp_path / 'Invitation.ics'], calendar_id='readonly')
    assert not widget.import_button.isEnabled() and widget.calendar.count() == 0
    widget.save()
    assert widget.result() == QDialog.DialogCode.Rejected
    assert widget.selected_paths == () and widget.selected_calendar_id == ''
