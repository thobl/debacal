"""Background imports keep standard UID ownership when reaching the workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.ics_resources import ParseReport
from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        try:
            yield store, window
        finally:
            window.close()
            window.executor.shutdown(wait=True, cancel_futures=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def event(identifier, uid):
    start = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    return Event(identifier, 'local', 'Imported event', start, start + timedelta(hours=1), ical_uid=uid)


def queue(window, events):
    future = Future()
    future.set_result(ParseReport(tuple(events)))
    future.calendar_id = 'local'
    window.tasks.put(('import', future))


def test_import_poll_preserves_existing_id_after_native_metadata_was_removed(workspace):
    store, window = workspace
    original = event('native-id', 'stable-uid')
    store.apply_remote([original])
    incoming = replace(original, id='uid-derived-id', title='Updated by another client')
    queue(window, [incoming])
    window.poll()
    assert store.get_event(original.id).title == incoming.title
    assert store.get_event(incoming.id) is None
    assert store.event_ids() == {original.id}
    assert not window.local_issues
    window.undo()
    assert store.get_event(original.id) == original


def test_separate_import_sources_cannot_overwrite_each_others_uid(workspace):
    store, window = workspace
    original = event('copied-native-id', 'first-uid')
    conflicting = event(original.id, 'different-uid')
    healthy = event('healthy-id', 'healthy-uid')
    queue(window, [original])
    queue(window, [conflicting, healthy])
    window.poll()
    assert store.get_event(original.id) == original
    assert store.get_event(healthy.id) == healthy
    assert store.event_ids() == {original.id, healthy.id}
    assert len(store.list_quarantined_resources()) == 1
    assert window.local_issues


def test_import_poll_does_not_write_into_a_newly_disabled_destination(workspace):
    store, window = workspace
    incoming = event('incoming', 'incoming-uid')
    calendar = store.get_calendar('local')
    store.save_calendar(replace(calendar, enabled=False))
    queue(window, [incoming])
    window.poll()
    assert store.get_event(incoming.id) is None
    assert window.local_issues
