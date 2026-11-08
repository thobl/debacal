"""Import previews load in the background and remain uncommitted until confirmed."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event as ThreadEvent, Lock, get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.import_dialog import ImportDialog, _navigate_preview
from debacal.import_sources import ImportSource
from debacal.ics import export_ics
from debacal.models import Calendar, Event
from debacal.calendar_choice import CalendarChoice
from debacal.preview_controls import PreviewControls
from debacal.preview_navigation import PreviewNavigation


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


def wait_navigation(widget):
    elapsed = QElapsedTimer()
    elapsed.start()
    while (widget._navigation_future is not None or widget._page_future is not None or not widget.preview_ready) and elapsed.elapsed() < 3000:
        QApplication.processEvents()
        QTest.qWait(5)
    assert widget._navigation_future is None and widget._page_future is None and widget.preview_ready


def test_import_uses_shared_sidebar_calendar_picker_and_preview_controls(dialogs):
    widget = dialogs(CALENDARS, sources=[ImportSource.from_data(contents())])
    wait_navigation(widget)
    assert isinstance(widget.calendar, CalendarChoice)
    assert isinstance(widget.preview.controls, PreviewControls)
    assert widget.sidebar.objectName() == 'eventEditorSidebar'
    assert widget.heading.objectName() == 'brand' and widget.heading.text() == 'Import events'
    assert widget.preview_column.height() == widget.sidebar.height()
    assert widget.preview.all_button.parentWidget() is widget.selection_section
    assert widget.import_button.objectName() == 'primary'
    assert widget.buttons.parentWidget().parentWidget() is widget.preview_column
    assert not widget.preview.message.isVisible()
    assert widget.preview.position_label.text().startswith('1/1')


def test_occurrence_navigation_crosses_months_without_changing_import_selection(dialogs):
    start = datetime(2026, 11, 30, 9, tzinfo=timezone.utc)
    series = Event('series', 'source', 'Recurring appointment', start, start + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=5')
    widget = dialogs(CALENDARS, sources=[ImportSource.from_data(export_ics([series]))])
    wait_navigation(widget)
    snapshot = widget.selection.snapshot(masters=widget.prepared_import.events)
    assert widget.preview.position_label.text().startswith('1/5')
    assert not widget.preview.previous_occurrence_button.isEnabled()
    QTest.mouseClick(widget.preview.last_button, Qt.MouseButton.LeftButton)
    wait_navigation(widget)
    assert widget.preview.current_month == date(2026, 12, 1)
    assert widget.preview.focused_event.start == start + timedelta(days=4)
    assert widget.preview.position_label.text().startswith('5/5')
    assert not widget.preview.next_occurrence_button.isEnabled()
    QTest.mouseClick(widget.preview.previous_occurrence_button, Qt.MouseButton.LeftButton)
    wait_navigation(widget)
    assert widget.preview.position_label.text().startswith('4/5')
    QTest.mouseClick(widget.preview.first_button, Qt.MouseButton.LeftButton)
    wait_navigation(widget)
    assert widget.preview.current_month == date(2026, 11, 1)
    assert widget.preview.focused_event.start == start
    assert widget.selection.snapshot(masters=widget.prepared_import.events) == snapshot
    assert not hasattr(widget.preview, 'details')
    assert not widget.selected_sources


def test_import_navigation_and_position_counts_run_off_the_gui_thread(dialogs):
    ui_thread = get_ident()
    calls = []
    methods = {name: getattr(PreviewNavigation, name) for name in ('adjacent', 'last', 'position')}

    def check(name):
        def call(self, *args, **kwargs):
            assert get_ident() != ui_thread
            calls.append(name)
            return methods[name](self, *args, **kwargs)
        return call

    with patch.object(PreviewNavigation, 'adjacent', check('adjacent')), \
         patch.object(PreviewNavigation, 'last', check('last')), \
         patch.object(PreviewNavigation, 'position', check('position')):
        widget = dialogs(CALENDARS, sources=[ImportSource.from_data(contents())])
        wait_navigation(widget)
        QTest.mouseClick(widget.preview.first_button, Qt.MouseButton.LeftButton)
        wait_navigation(widget)
        QTest.mouseClick(widget.preview.last_button, Qt.MouseButton.LeftButton)
        wait_navigation(widget)
    assert set(calls) == set(methods)


def test_enter_on_preview_navigation_navigates_without_accepting_import(dialogs):
    widget = dialogs(CALENDARS, sources=[ImportSource.from_data(contents())])
    wait_navigation(widget)
    current = widget.preview.current_month
    widget.preview.next_button.setFocus()
    QTest.keyClick(widget.preview.next_button, Qt.Key.Key_Return)
    wait_navigation(widget)
    assert widget.preview.current_month != current
    assert widget.result() == QDialog.DialogCode.Rejected
    assert not widget.selected_sources


def test_unbounded_import_has_no_last_occurrence_and_keeps_browsing_available(dialogs):
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    series = Event('series', 'source', 'Ongoing', start, start + timedelta(hours=1), recurrence_rule='FREQ=WEEKLY')
    widget = dialogs(CALENDARS, sources=[ImportSource.from_data(export_ics([series]))])
    wait_navigation(widget)
    assert not widget.preview.last_button.isEnabled()
    assert '∞' in widget.preview.position_label.text()
    assert widget.preview.next_occurrence_button.isEnabled()
    assert widget.preview.next_button.isEnabled() and widget.preview.today_button.isEnabled()


def test_rapid_focus_requests_cannot_overlap_the_shared_position_cache():
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    source = Event('event', 'source', 'A source event', start, start + timedelta(hours=1))
    navigation = PreviewNavigation([source])
    started, release, second_scheduled, second_entered = (ThreadEvent() for _ in range(4))
    calls, lock = [], Lock()

    def position(reference):
        calls.append(reference.id)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=2)
        else:
            second_entered.set()
        return 1, 1

    def second():
        second_scheduled.set()
        return _navigate_preview(navigation, 'position', source, lock)

    with patch.object(navigation, 'position', side_effect=position), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_navigate_preview, navigation, 'position', source, lock)
        try:
            assert started.wait(timeout=2)
            following = executor.submit(second)
            assert second_scheduled.wait(timeout=2)
            assert not second_entered.wait(timeout=.05)
        finally:
            release.set()
        assert first.result(timeout=2)[1] == (1, 1)
        assert following.result(timeout=2)[1] == (1, 1)
    assert calls == [source.id, source.id]
