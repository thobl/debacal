"""Import previews load in the background and remain uncommitted until confirmed."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource
from debacal.ics import export_ics
from debacal.models import Calendar, Event


CALENDARS = [Calendar('local', 'Personal', '#5f8a67'),
             Calendar('subscription', 'Feed', '#a26d4d', kind='ics'),
             Calendar('readonly', 'Shared', '#8c78ac', kind='caldav', read_only=True),
             Calendar('work', 'Work', '#315ab7', kind='caldav')]


def contents(identity='invitation', title='Meeting invitation'):
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    return export_ics([Event(identity, 'source', title, start, start + timedelta(hours=1))])


def wait_ready(widget):
    elapsed = QElapsedTimer()
    elapsed.start()
    while not widget.preview_ready and elapsed.elapsed() < 3000:
        QApplication.processEvents()
        QTest.qWait(5)
    assert widget.preview_ready


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
    for index, path in enumerate(paths):
        path.write_bytes(contents(str(index)))
    widget = dialogs(CALENDARS, [*paths, paths[0]], calendar_id='work')
    assert widget.calendar.count() == 2
    assert widget.calendar.currentData() == 'work'
    assert widget.selected_paths == () and widget.selected_calendar_id == ''
    wait_ready(widget)
    assert widget.prepared_import.event_count == 2
    widget.calendar.setCurrentIndex(widget.calendar.findData('local'))
    QTest.keyClick(widget.calendar, Qt.Key.Key_Return)
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.selected_paths == tuple(paths) and widget.selected_calendar_id == 'local'
    assert widget.selected_prepared is widget.prepared_import


def test_cancelling_does_not_produce_an_import_request(dialogs, tmp_path):
    widget = dialogs(CALENDARS, [tmp_path / 'Invitation.ics'], calendar_id='local')
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    assert widget.result() == QDialog.DialogCode.Rejected
    assert widget.selected_paths == () and widget.selected_calendar_id == ''


def test_dialog_never_reads_or_stats_requested_files_on_the_gui_thread(dialogs, tmp_path):
    path = tmp_path / 'Invitation.ics'
    path.write_bytes(contents())
    ui_thread, reads = get_ident(), []
    original_open, original_stat, original_resolve = Path.open, Path.stat, Path.resolve

    def check(function):
        def guarded(source, *args, **kwargs):
            if source == path:
                assert get_ident() != ui_thread, 'UI attempted file I/O'
                reads.append(get_ident())
            return function(source, *args, **kwargs)
        return guarded

    with patch.object(Path, 'open', check(original_open)), \
         patch.object(Path, 'stat', check(original_stat)), \
         patch.object(Path, 'resolve', check(original_resolve)):
        widget = dialogs(CALENDARS, [path], calendar_id='work')
        wait_ready(widget)
        widget.save()
        assert widget.result() == QDialog.DialogCode.Accepted
        assert widget.selected_paths == (path,)
    assert reads


def test_browse_cancel_retains_files_and_new_selection_replaces_them(dialogs, tmp_path):
    first, second = tmp_path / 'First.ics', tmp_path / 'Second.ics'
    first.write_bytes(contents('first'))
    second.write_bytes(contents('second'))
    widget = dialogs(CALENDARS, [first])
    with patch('debacal.import_dialog.QFileDialog.getOpenFileNames', return_value=([], '')):
        widget.browse()
    wait_ready(widget)
    widget.save()
    assert widget.selected_paths == (first,)

    widget = dialogs(CALENDARS, [first])
    with patch('debacal.import_dialog.QFileDialog.getOpenFileNames', return_value=([str(second)], '')):
        widget.browse()
    wait_ready(widget)
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


def test_memory_contents_are_reviewed_without_requiring_a_file(dialogs):
    source = ImportSource.from_data(contents())
    widget = dialogs(CALENDARS, calendar_id='work', sources=[source])
    assert widget.files.count() == 1 and widget.files.item(0).text() == 'Standard input'
    assert widget.selected_sources == ()
    wait_ready(widget)
    assert widget.import_button.isEnabled() and widget.prepared_import.event_count == 1
    widget.save()
    assert widget.selected_sources == (source,)
    assert widget.selected_paths == () and widget.selected_calendar_id == 'work'


def test_cancelling_memory_contents_does_not_accept_sources(dialogs):
    source = ImportSource.from_data(b'not parsed before confirmation')
    widget = dialogs(CALENDARS, sources=[source])
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    assert widget.result() == QDialog.DialogCode.Rejected
    assert not widget.selected_sources and not widget.selected_paths


def test_invalid_sources_finish_preview_without_enabling_an_empty_import(dialogs):
    widget = dialogs(CALENDARS, sources=[ImportSource.from_data(b'Not a calendar.')])
    wait_ready(widget)
    assert widget.prepared_import.event_count == 0
    assert not widget.import_button.isEnabled()
    widget.save()
    assert widget.result() == QDialog.DialogCode.Rejected
    assert not widget.selected_sources and widget.selected_prepared is None


def test_changing_sources_while_loading_cannot_show_or_import_the_old_snapshot(dialogs):
    from debacal.ics import parse_ics_report
    first = ImportSource.from_data(contents('first', 'Old source'))
    second = ImportSource.from_data(contents('second', 'Current source'))
    started, release = ThreadEvent(), ThreadEvent()

    def parse(data, *args, **kwargs):
        if data == first.data:
            started.set()
            if not release.wait(timeout=3):
                raise AssertionError('Test did not release its preview worker.')
        return parse_ics_report(data, *args, **kwargs)

    with patch('debacal.import_preparation.parse_ics_report', side_effect=parse):
        widget = dialogs(CALENDARS, sources=[first])
        elapsed = QElapsedTimer()
        elapsed.start()
        try:
            while not started.is_set() and elapsed.elapsed() < 2500:
                QApplication.processEvents()
                QTest.qWait(5)
            assert started.is_set()
            assert not widget.import_button.isEnabled()
            widget.save()
            assert not widget.selected_sources
            widget.set_sources([second])
        finally:
            release.set()
        wait_ready(widget)
        assert [event.title for event in widget.prepared_import.events] == ['Current source']
        widget.save()
    assert widget.selected_sources == (second,)
    assert [event.title for event in widget.selected_prepared.events] == ['Current source']
