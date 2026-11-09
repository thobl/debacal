"""File-open requests stay reviewed, serialized, and resilient to bad files."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as ThreadEvent, get_ident
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.ics import export_ics
from debacal.import_dialog import ImportDialog
from debacal.import_sources import ImportSource
from debacal.import_preparation import PreparedSource
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, \
            patch('sys.excepthook', side_effect=lambda _type, error, _traceback: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        app.processEvents()
        try:
            yield app, window, store
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            assert not errors


def wait_for(app, predicate, timeout=2500):
    elapsed = QElapsedTimer()
    elapsed.start()
    while not predicate() and elapsed.elapsed() < timeout:
        app.processEvents()
        QTest.qWait(5)
    assert predicate()


def write_calendar(path, identity='invitation', title='Invitation'):
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    event = Event(identity, 'source', title, start, start + timedelta(hours=1))
    path.write_bytes(export_ics([event]))
    return path


def finish_parsing(window):
    # One worker handles file reads in order. This barrier follows every import
    # job and its completion callback, without a timing-based sleep.
    window.executor.submit(lambda: None).result(timeout=3)
    window.poll()


def accept_request(dialog):
    wait_for(QApplication.instance(), lambda: dialog.preview_ready)
    dialog.save()
    return dialog.result()


def test_file_open_reviews_destination_before_background_import_and_undo(desktop, tmp_path):
    app, window, store = desktop
    path = write_calendar(tmp_path / 'Meeting invitation.ics')
    store.save_calendar(Calendar('work', 'Work', '#315ab7'))
    window.set_creation_calendar('work')
    history = len(store.history())
    opened, reader_threads = [], []
    ui_thread = get_ident()
    original_open = Path.open

    def read(source, *args, **kwargs):
        if source == path:
            reader_threads.append(get_ident())
        return original_open(source, *args, **kwargs)

    def review(dialog):
        opened.append(dialog._paths)
        assert not store.list_events() and len(store.history()) == history
        assert dialog.calendar.currentData() == 'work'
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review), patch.object(Path, 'open', read):
        window.import_files([path])
        assert not store.list_events() and not opened
        wait_for(app, lambda: bool(opened))
        finish_parsing(window)
    events = store.list_events()
    assert opened == [(path,)]
    assert len(events) == 1 and (events[0].title, events[0].calendar_id) == ('Invitation', 'work')
    assert reader_threads and all(thread != ui_thread for thread in reader_threads)
    assert len(store.history()) == history + 1
    window.undo()
    assert not store.list_events()


def test_cancelled_file_preview_does_not_change_calendar(desktop, tmp_path):
    app, window, store = desktop
    path = write_calendar(tmp_path / 'Invitation.ics')
    with patch.object(ImportDialog, 'exec', return_value=QDialog.DialogCode.Rejected) as dialog:
        window.import_files([path])
        wait_for(app, lambda: dialog.call_count == 1)
        assert not store.list_events() and not store.history()


def test_request_arriving_during_review_waits_for_current_dialog_to_finish(desktop, tmp_path):
    app, window, store = desktop
    first = write_calendar(tmp_path / 'First.ics', 'first', 'First')
    second = write_calendar(tmp_path / 'Second.ics', 'second', 'Second')
    opened, depths = [], []
    depth = 0

    def review(dialog):
        nonlocal depth
        depth += 1
        depths.append(depth)
        opened.append(dialog._paths)
        try:
            if dialog._paths == (first,):
                window.import_files([second])
                # A second file-open IPC message can be delivered from the
                # nested dialog event loop. It must not create a nested import.
                window._process_import_request()
                app.processEvents()
                assert opened == [(first,)]
                return QDialog.DialogCode.Rejected
            return accept_request(dialog)
        finally:
            depth -= 1

    with patch.object(ImportDialog, 'exec', review):
        window.import_files([first])
        wait_for(app, lambda: len(opened) == 2)
        finish_parsing(window)
    assert opened == [(first,), (second,)] and depths == [1, 1]
    assert [event.title for event in store.list_events()] == ['Second']


def test_unrelated_modal_postpones_import_until_it_closes(desktop, tmp_path):
    app, window, store = desktop
    path = write_calendar(tmp_path / 'Invitation.ics')
    modal = QDialog(window)
    modal.setWindowModality(Qt.WindowModality.ApplicationModal)
    modal.show()
    app.processEvents()
    assert QApplication.activeModalWidget() is modal
    opened = []

    def review(dialog):
        opened.append(dialog._paths)
        return accept_request(dialog)

    try:
        with patch.object(ImportDialog, 'exec', review):
            window.import_files([path])
            for _ in range(3):
                app.processEvents()
            assert not opened and not store.list_events()
            modal.reject()
            wait_for(app, lambda: len(opened) == 1)
            finish_parsing(window)
        assert len(store.list_events()) == 1
    finally:
        modal.close()
        modal.deleteLater()


@pytest.mark.parametrize('incremental', [False, True])
def test_bad_files_do_not_block_other_files_or_create_empty_history_entries(desktop, tmp_path, incremental):
    app, window, store = desktop
    missing = tmp_path / 'Missing.ics'
    invalid = tmp_path / 'Invalid.ics'
    invalid.write_bytes(b'This is not a calendar.')
    first = write_calendar(tmp_path / 'First.ics', 'first', 'First')
    second = write_calendar(tmp_path / 'Second.ics', 'second', 'Second')
    opened = []
    good_file_started, continue_reading = ThreadEvent(), ThreadEvent()
    original_remap = PreparedSource.remap

    def remap(source, *args, **kwargs):
        if incremental and source.label == first.name:
            good_file_started.set()
            if not continue_reading.wait(timeout=3):
                raise AssertionError('Import test did not release its worker.')
        return original_remap(source, *args, **kwargs)

    def review(dialog):
        opened.append(dialog._paths)
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review), patch.object(PreparedSource, 'remap', remap), \
            patch.object(window, 'report_error', wraps=window.report_error) as reports:
        window.import_files([missing, invalid, first, second])
        wait_for(app, lambda: len(opened) == 1)
        try:
            if incremental:
                wait_for(app, good_file_started.is_set)
                window.poll()
                assert not store.list_events()
                assert any('Invalid.ics' in issue.details for issue in window.local_issues)
        finally:
            continue_reading.set()
        finish_parsing(window)
    assert {event.title for event in store.list_events()} == {'First', 'Second'}
    errors = [call.args[0] for call in reports.call_args_list if call.args[0].startswith('Import failed')]
    assert len(errors) == 2 and 'Missing.ics' in errors[0] and 'Invalid.ics' in errors[1]
    # Later successful imports retain both failures without producing routine
    # success notifications; each error remains inspectable in the footer.
    assert [issue.details for issue in window.local_issues] == errors
    assert all('Imported 1 event resource(s).' not in issue.details for issue in window.local_issues)
    assert window.warning_button.isVisible()
    assert all(error in window._sync_messages for error in errors)
    assert len(store.history()) == 2
    window.undo()
    assert [event.title for event in store.list_events()] == ['First']
    window.undo()
    assert not store.list_events()


def test_toolbar_import_opens_the_same_request_dialog_with_explicit_destination(desktop):
    app, window, store = desktop
    store.save_calendar(Calendar('work', 'Work', '#315ab7'))
    seen = []

    def review(dialog):
        seen.append((dialog._paths, dialog.calendar.currentData()))
        return QDialog.DialogCode.Rejected

    with patch.object(ImportDialog, 'exec', review):
        window.import_ics('work')
        wait_for(app, lambda: bool(seen))
    assert seen == [((), 'work')]


def calendar_contents(identity='stdin', title='From standard input'):
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    return export_ics([Event(identity, 'source', title, start, start + timedelta(hours=1))])


def test_stdin_contents_preview_in_worker_and_require_confirmation_without_temp_files(desktop):
    app, window, store = desktop
    from debacal.ics import parse_ics_report
    store.save_calendar(Calendar('work', 'Work', '#315ab7'))
    window.set_creation_calendar('work')
    data = calendar_contents(title='Réunion · Prüfung')
    source, parser_threads, opened = ImportSource.from_data(data), [], []
    ui_thread = get_ident()

    def parse(*args, **kwargs):
        parser_threads.append(get_ident())
        return parse_ics_report(*args, **kwargs)

    def review(dialog):
        opened.append(dialog._sources)
        assert dialog.files.item(0).text() == 'Standard input'
        assert dialog.calendar.currentData() == 'work'
        wait_for(app, lambda: dialog.preview_ready)
        assert not store.list_events() and parser_threads
        assert all(thread != ui_thread for thread in parser_threads)
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review), \
            patch('debacal.import_preparation.parse_ics_report', side_effect=parse), \
            patch('tempfile.mkstemp', side_effect=AssertionError('Memory import created a temporary file')):
        assert window.import_data(data)
        assert not opened and not store.list_events()
        wait_for(app, lambda: bool(opened))
        finish_parsing(window)
    assert opened == [(source,)]
    assert parser_threads and all(thread != ui_thread for thread in parser_threads)
    assert len(parser_threads) == 1, 'Confirmation must use the preview snapshot without parsing again.'
    assert [(event.title, event.calendar_id) for event in store.list_events()] == [('Réunion · Prüfung', 'work')], window.local_issues
    assert not window.local_issues
    assert window._import_memory_bytes == 0
    window.undo()
    assert not store.list_events()


def test_cancelled_stdin_preview_releases_memory_without_committing(desktop):
    app, window, store = desktop
    with patch.object(ImportDialog, 'exec', return_value=QDialog.DialogCode.Rejected) as dialog:
        assert window.import_data(calendar_contents())
        wait_for(app, lambda: dialog.call_count == 1)
    assert not store.list_events() and not store.history()
    assert not window._import_requests and window._import_memory_bytes == 0


def test_mixed_file_and_stdin_sources_share_one_review_and_keep_failure_details(desktop, tmp_path):
    app, window, store = desktop
    path = write_calendar(tmp_path / 'File invitation.ics', 'file', 'From file')
    sources = (ImportSource.from_data(b'Not a calendar.'), ImportSource.from_path(path))
    opened = []

    def review(dialog):
        opened.append(dialog._sources)
        assert dialog._paths == (path,)
        assert not store.list_events()
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review):
        assert window.import_sources(sources)
        wait_for(app, lambda: bool(opened))
        finish_parsing(window)
    assert opened == [sources]
    assert [event.title for event in store.list_events()] == ['From file']
    assert len(window.local_issues) == 1
    assert 'Import failed for Standard input' in window.local_issues[0].details
    assert not window.local_issues[0].action
    assert window._import_memory_bytes == 0 and len(store.history()) == 1


def test_stdin_request_arriving_during_an_import_waits_without_nested_review(desktop):
    app, window, store = desktop
    first = ImportSource.from_data(calendar_contents('first', 'First'), 'First request')
    second = ImportSource.from_data(calendar_contents('second', 'Second'), 'Second request')
    opened, depths = [], []
    depth = 0

    def review(dialog):
        nonlocal depth
        depth += 1
        depths.append(depth)
        opened.append(dialog._sources)
        try:
            if dialog._sources == (first,):
                window.import_sources([second])
                window._process_import_request()
                app.processEvents()
                assert opened == [(first,)]
                return QDialog.DialogCode.Rejected
            return accept_request(dialog)
        finally:
            depth -= 1

    with patch.object(ImportDialog, 'exec', review):
        assert window.import_sources([first])
        wait_for(app, lambda: len(opened) == 2)
        finish_parsing(window)
    assert opened == [(first,), (second,)] and depths == [1, 1]
    assert [event.title for event in store.list_events()] == ['Second']
    assert window._import_memory_bytes == 0


def test_pending_stdin_memory_is_bounded_and_cancellation_allows_another_request(desktop):
    app, window, store = desktop
    data = calendar_contents()
    opened = []
    with patch('debacal.ui.MAX_PENDING_IMPORT_BYTES', len(data) * 2), \
            patch.object(ImportDialog, 'exec', side_effect=lambda: opened.append(True) or QDialog.DialogCode.Rejected):
        assert window.import_data(data)
        assert window.import_data(data)
        assert not window.import_data(data)
        assert 'Too many calendar imports' in window.local_issues[-1].details
        assert window._import_memory_bytes == len(data) * 2
        wait_for(app, lambda: len(opened) == 2)
        assert window._import_memory_bytes == 0
        assert window.import_data(data)
        wait_for(app, lambda: len(opened) == 3)
    assert window._import_memory_bytes == 0 and not store.list_events()


def test_stdin_preview_holds_capacity_while_loading_and_before_confirmation(desktop):
    app, window, store = desktop
    from debacal.ics import parse_ics_report
    data = calendar_contents()
    started, release = ThreadEvent(), ThreadEvent()

    def parse(*args, **kwargs):
        started.set()
        if not release.wait(timeout=3):
            raise AssertionError('Test did not release the parsing worker.')
        return parse_ics_report(*args, **kwargs)

    def review(dialog):
        try:
            wait_for(app, started.is_set)
            assert not window.can_accept_import(len(data))
            assert not window.import_data(data)
            assert not store.list_events()
        finally:
            release.set()
        wait_for(app, lambda: dialog.preview_ready)
        assert not window.can_accept_import(len(data))
        return accept_request(dialog)

    with patch('debacal.ui.MAX_PENDING_IMPORT_BYTES', len(data)), \
            patch.object(ImportDialog, 'exec', review), \
            patch('debacal.import_preparation.parse_ics_report', side_effect=parse):
        try:
            assert window.import_data(data)
            wait_for(app, started.is_set)
        finally:
            release.set()
        finish_parsing(window)
        assert window.can_accept_import(len(data)) and window._import_memory_bytes == 0
    assert len(store.list_events()) == 1


def test_confirmation_imports_the_exact_preview_snapshot_after_file_changes(desktop, tmp_path):
    app, window, store = desktop
    path = write_calendar(tmp_path / 'Changing invitation.ics', title='Shown in the preview')
    reader_threads, ui_thread = [], get_ident()
    original_open = Path.open

    def read(source, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if source == path and 'r' in mode:
            reader_threads.append(get_ident())
        return original_open(source, *args, **kwargs)

    def review(dialog):
        wait_for(app, lambda: dialog.preview_ready)
        assert not store.list_events() and not store.history()
        assert [event.title for event in dialog.prepared_import.events] == ['Shown in the preview']
        write_calendar(path, title='Changed on disk after preview')
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review), patch.object(Path, 'open', read):
        window.import_files([path])
        wait_for(app, lambda: bool(store.list_events()))
        finish_parsing(window)
    assert [event.title for event in store.list_events()] == ['Shown in the preview']
    assert len(reader_threads) == 1 and reader_threads[0] != ui_thread


def click_import_event(app, dialog, event):
    canvas = dialog.preview.canvas
    canvas.grab()
    rect = next(rect for rect, item in canvas.hits if item.id == event.id)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    wait_for(app, lambda: dialog.preview_ready)


def test_import_preview_deselection_keeps_standard_series_with_one_excluded_date(desktop):
    app, window, store = desktop
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    series = Event('series', 'source', 'Weekly meeting', start, start + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY;COUNT=3')
    excluded = []

    def review(dialog):
        dialog.show()
        wait_for(app, lambda: dialog.preview_ready)
        occurrences = sorted(dialog._page.events, key=lambda event: event.start)
        excluded.append(occurrences[1].start)
        # Data readiness can precede the queued layout after the initial
        # loading message disappears. Compare completed layouts on both sides
        # of the click so initial loading is not mistaken for a selection jump.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        app.processEvents()
        geometry = dialog.preview.canvas.geometry()
        click_import_event(app, dialog, occurrences[1])
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        app.processEvents()
        assert not dialog.selection.is_selected(occurrences[1])
        assert dialog.preview.canvas.geometry() == geometry
        assert not hasattr(dialog.preview, 'details') and not store.list_events()
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review):
        window.import_data(export_ics([series]))
        wait_for(app, lambda: bool(store.list_events()))
        finish_parsing(window)
    imported = store.list_events()[0]
    assert imported.recurrence_rule == series.recurrence_rule and len(imported.excluded_dates) == 1
    dates = store.query_occurrences(start - timedelta(days=1), start + timedelta(days=30))
    assert [event.start for event in dates] == [start, start + timedelta(days=14)]
    assert excluded[0] not in [event.start for event in dates]


def test_chosen_dates_survive_month_navigation_and_import_as_finite_recurrence(desktop):
    app, window, store = desktop
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    series = Event('series', 'source', 'Selected dates', start, start + timedelta(hours=1),
                   recurrence_rule='FREQ=WEEKLY')
    selected = []

    def review(dialog):
        dialog.show()
        wait_for(app, lambda: dialog.preview_ready)
        QTest.mouseClick(dialog.preview.none_button, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog.preview_ready)
        assert not dialog.import_button.isEnabled()
        QTest.mouseClick(dialog.preview.next_button, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog._page.month.month == 12)
        december = next(event for event in dialog._page.events if event.start.month == 12)
        selected.append(december.start)
        click_import_event(app, dialog, december)
        QTest.mouseClick(dialog.preview.previous_button, Qt.MouseButton.LeftButton)
        wait_for(app, lambda: dialog._page.month.month == 11)
        november = next(event for event in dialog._page.events if event.start.month == 11)
        selected.append(november.start)
        click_import_event(app, dialog, november)
        assert dialog.selection.is_selected(december) and dialog.selection.is_selected(november)
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review):
        window.import_data(export_ics([series]))
        wait_for(app, lambda: bool(store.list_events()))
        finish_parsing(window)
    imported = store.list_events()[0]
    assert not imported.recurrence_rule and len(imported.recurrence_dates) == 1
    occurrences = store.query_occurrences(start - timedelta(days=1), start + timedelta(days=730))
    assert sorted(event.start for event in occurrences) == sorted(selected)


def test_date_checkbox_excludes_all_events_starting_that_day_without_splitting_spans(desktop):
    app, window, store = desktop
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    events = [Event('timed', 'source', 'Morning', start, start + timedelta(hours=1)),
              Event('point', 'source', 'Reminder', start + timedelta(hours=3), start + timedelta(hours=3)),
              Event('span', 'source', 'Several days', start.replace(hour=0), start.replace(hour=0) + timedelta(days=4), all_day=True),
              Event('later', 'source', 'Keep this date', start + timedelta(days=6), start + timedelta(days=6,hours=1))]

    def review(dialog):
        dialog.show()
        wait_for(app, lambda: dialog.preview_ready)
        canvas = dialog.preview.canvas
        cell = canvas.day_rect(start.date())
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(cell.left() + 14), round(cell.top() + 7)))
        wait_for(app, lambda: dialog.preview_ready)
        assert len(dialog._import_choice.events) == 1
        return accept_request(dialog)

    with patch.object(ImportDialog, 'exec', review):
        window.import_data(export_ics(events))
        wait_for(app, lambda: bool(store.list_events()))
        finish_parsing(window)
    assert [event.title for event in store.list_events()] == ['Keep this date']
