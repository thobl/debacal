"""Dialog saves remain responsive and publish only committed database changes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, Thread
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from debacal.dialogs import HistoryDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store


@pytest.fixture
def desktop(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'dialogs.sqlite3'
    store, writer = Store(path), Store(path, query_index=False)
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        yield app, store, writer, widgets
        commands = getattr(store, '_standalone_ui_commands', None)
        if commands is not None:
            commands.shutdown()
            commands.executor.shutdown(wait=True)
        for widget in widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        writer.close()
        store.close()
        assert not errors


@contextmanager
def syncing(writer, operation=None):
    """Hold an actual sync write until the GUI has completed its assertions."""
    acquired, release = ThreadEvent(), ThreadEvent()
    outcome, errors = {}, []

    def write():
        try:
            with writer._transaction():
                if operation is not None:
                    operation(writer)
                acquired.set()
                outcome['released_by_gui'] = release.wait(2)
        except BaseException as error:
            errors.append(error)
            acquired.set()

    thread = Thread(target=write, name='synthetic-dialog-sync')
    thread.start()
    assert acquired.wait(5)
    try:
        assert not errors
        yield
    finally:
        release.set()
        thread.join(6)
    assert not thread.is_alive()
    assert not errors
    assert outcome['released_by_gui'], 'The dialog waited for the sync writer.'


def finish(app, predicate):
    for _ in range(400):
        app.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError('The captured dialog write did not complete.')


def preferences_dialog(desktop):
    app, store, _writer, widgets = desktop
    dialog = PreferencesDialog(Preferences(), store=store, allow_remote=False)
    widgets.append(dialog)
    dialog.show()
    app.processEvents()
    return dialog


def test_preferences_save_waits_for_source_commit_without_blocking_or_losing_state(desktop):
    app, store, writer, _widgets = desktop
    dialog = preferences_dialog(desktop)
    dialog.source_editor.alias.setText('Renamed calendar')
    dialog.source_editor._name_changed('Renamed calendar')
    dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
    applied, ticks = [], []
    dialog.applied.connect(lambda preferences, _bindings: (preferences.save(store), applied.append(preferences)))
    timer = QTimer(dialog)
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))

    with syncing(writer):
        dialog.save()
        assert dialog._applying and dialog.isVisible()
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isEnabled()
        assert store.get_calendar('local').name != 'Renamed calendar'
        assert not applied
        dialog.reject()
        dialog.close()
        assert dialog.isVisible()
        timer.start()
        QTest.qWait(20)
        timer.stop()
        assert ticks, 'The GUI timer must continue while the save is queued.'
        assert not applied

    finish(app, lambda: not dialog._applying)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert store.get_calendar('local').name == 'Renamed calendar'
    assert len(applied) == 1
    assert Preferences.load(store).control_style == 'filled'


def test_failed_source_commit_keeps_preferences_open_and_unapplied(desktop):
    app, store, writer, _widgets = desktop
    calendar = Calendar('remote', 'Remote', '#446677', kind='caldav',
                        url='https://cloud.example/calendar/', username='synthetic')
    store.save_calendar(calendar)
    dialog = preferences_dialog(desktop)
    editor = dialog.source_editor
    editor.sources.setCurrentItem(editor._items[calendar.id])
    editor.alias.setText('My alias')
    editor._name_changed('My alias')
    dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
    applied = []
    dialog.applied.connect(lambda *_: applied.append(True))

    with syncing(writer, lambda connection: connection.connection.execute(
            'DELETE FROM calendars WHERE id=?', (calendar.id,))):
        dialog.save()
        assert dialog._applying
    finish(app, lambda: not dialog._applying)
    assert dialog.isVisible() and dialog.result() != QDialog.DialogCode.Accepted
    assert not applied
    assert 'calendar changed' in dialog.error.text()
    assert dialog.save_button.isEnabled()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isEnabled()
    assert editor.alias.text() == 'My alias'
    dialog.reject()
    assert not dialog.isVisible()


def test_appearance_only_apply_never_submits_a_canonical_source_write(desktop):
    _app, store, writer, _widgets = desktop
    dialog = preferences_dialog(desktop)
    dialog.control_style.setCurrentIndex(dialog.control_style.findData('filled'))
    dialog.applied.connect(lambda preferences, _bindings: preferences.save(store))
    with syncing(writer), patch('debacal.ui_database.submit_write', side_effect=AssertionError('No source changed')):
        assert dialog.apply()
        assert Preferences.load(store).control_style == 'filled'
        assert dialog.isVisible() and not dialog._applying


@pytest.mark.parametrize('redo', [False, True])
def test_history_toggle_is_queued_without_blocking_the_dialog(desktop, redo):
    app, store, writer, widgets = desktop
    start = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
    event = Event('synthetic', 'local', 'Synthetic appointment', start, start + timedelta(hours=1))
    operation = store.apply('Create synthetic appointment', [event])
    if redo:
        store.undo(operation)
    changed = []
    dialog = HistoryDialog(store, lambda: changed.append(True))
    widgets.append(dialog)
    dialog.show()
    dialog.list.setCurrentRow(0)
    with syncing(writer):
        dialog.perform(redo)
        assert dialog._writing and not changed
        assert not dialog.undo_btn.isEnabled() and not dialog.redo_btn.isEnabled()
        assert (store.get_event(event.id) is None) == redo
    finish(app, lambda: not dialog._writing)
    assert changed == [True]
    assert (store.get_event(event.id) is not None) == redo
    assert store.history()[0]['applied'] == redo
    assert dialog.list.count() == 1
