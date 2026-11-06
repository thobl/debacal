"""Persistent mode buttons and remappable mode keys in the event editor."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog, ShortcutsDialog
from debacal.event_scope import DEFAULT_MODE_BINDINGS, expanded_scope_bindings
from debacal.models import Event
from debacal.recurrence import expand_series
from debacal.store import Store

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    dialogs = []
    errors = []
    with patch('debacal.dialogs.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(event=None, series=None, **kwargs):
            dialog = EventDialog(store.list_calendars(), event.start if event else START,
                                 event=event, series=series, **kwargs)
            dialogs.append(dialog)
            dialog.show()
            dialog.activateWindow()
            app.processEvents()
            return dialog
        yield app, store, dialogs, create
        for dialog in reversed(dialogs):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def master():
    return Event('series', 'local', 'Original', START, START + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=5')


def test_function_keys_choose_modes_inside_text_fields_and_keep_each_draft(editors):
    _, _, _, create = editors
    series = master()
    selected = expand_series(series, START, START + timedelta(days=10))[2]
    dialog = create(selected, series)
    assert not dialog.scope.isVisible()
    assert dialog.scope_controls.isVisible()
    dialog.title.setFocus()
    dialog.title.setText('Single draft')
    QTest.keyClick(dialog.title, Qt.Key.Key_F3)
    assert dialog.scope.currentData() == 'group'
    assert dialog.scope_buttons['group'].isChecked()
    dialog.title.setText('All draft')
    dialog.description.setFocus()
    QTest.keyClick(dialog.description, Qt.Key.Key_F4)
    assert dialog.scope.currentData() == 'future'
    dialog.title.setText('Following draft')
    for key, mode, title in [(Qt.Key.Key_F2, 'single', 'Single draft'), (Qt.Key.Key_F3, 'group', 'All draft'),
                             (Qt.Key.Key_F4, 'future', 'Following draft')]:
        QTest.keyClick(dialog.description, key)
        assert dialog.scope.currentData() == mode
        assert dialog.title.text() == title


def test_ordinary_existing_event_can_keep_any_global_mode(editors):
    _, _, _, create = editors
    source = Event('ordinary', 'local', 'One event', START, START + timedelta(hours=1))
    dialog = create(source, edit_scope='future')
    assert not dialog.has_group
    assert dialog.scope.currentData() == 'future'
    assert dialog.scope_controls.isVisible()
    changed = []
    dialog.mode_changed.connect(changed.append)
    QTest.mouseClick(dialog.scope_buttons['group'], Qt.MouseButton.LeftButton)
    assert changed == ['group']
    assert dialog.scope.currentData() == 'group'
    assert dialog.build_events()[0].start == source.start
    new = create()
    assert not new.scope_controls.isVisible()
    assert all(not shortcut.isEnabled() for shortcut in new.mode_shortcuts.values())


def test_mode_keys_are_remappable_and_common_field_editing_stays_limited(editors):
    _, _, _, create = editors
    series = master()
    selected = expand_series(series, START, START + timedelta(days=10))[2]
    dialog = create(selected, series, scope_bindings={'scope_all': 'Alt+A', 'scope_following': 'Alt+N'},
                    common_fields_only=True)
    assert dialog.common_fields_help.isVisible()
    assert not dialog.repeat.isVisible()
    assert not dialog.pattern.isEnabled()
    dialog.title.setFocus()
    for key, mode in [(Qt.Key.Key_A, 'group'), (Qt.Key.Key_N, 'future')]:
        QTest.keyClick(dialog.title, key, Qt.KeyboardModifier.AltModifier)
        assert dialog.scope.currentData() == mode
        dialog.start.setDateTime(dialog.start.dateTime().addSecs(3600))
        assert not dialog.repeat.isVisible() and not dialog.repeat.isEnabled()
        assert not dialog.preview.date_editing
    assert dialog.title.isEnabled() and dialog.all_day.isEnabled()


def test_new_bindings_do_not_reserve_held_modifier_scope_variants(editors):
    _, _, dialogs, _ = editors
    bindings = DEFAULT_MODE_BINDINGS | {'edit_event': 'E', 'copy_event': 'Ctrl+C', 'paste_event': 'Ctrl+V', 'other': 'Shift+E'}
    assert expanded_scope_bindings(bindings) == bindings
    dialog = ShortcutsDialog(bindings, bindings)
    dialogs.append(dialog)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.result_bindings == bindings
    duplicate = ShortcutsDialog(bindings | {'other': 'F2'}, bindings)
    dialogs.append(duplicate)
    duplicate.save()
    assert duplicate.result() != QDialog.DialogCode.Accepted
    assert 'unique' in duplicate.error.text()
