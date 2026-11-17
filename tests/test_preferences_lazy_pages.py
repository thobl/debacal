"""Opening Calendar preferences does not build hidden editors or read sources."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store


BINDINGS = {'new_event': 'N', 'search': 'Ctrl+F'}


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets = []
    yield app, widgets
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('operation', ['cancel', 'apply', 'save', 'defaults'])
def test_calendar_only_operations_do_not_initialize_hidden_editors(desktop, operation):
    app, widgets = desktop
    preferences = Preferences(color_scheme='warm', font_scale=120, sync_interval_minutes=19,
                              auto_sync_enabled=False, conflict_notifications='both')
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Unvisited preferences must not build controls or read sources')
    store = SimpleNamespace(get_default_calendar=forbidden, list_calendars=forbidden)
    with ExitStack() as stack:
        for target in ('debacal.preferences_dialog.AppearanceEditor',
                       'debacal.preferences_dialog.KeyboardShortcutsEditor',
                       'debacal.calendar_sources.CalendarSourcesEditor'):
            stack.enter_context(patch(target, side_effect=forbidden))
        widget = PreferencesDialog(preferences, store=store, bindings=BINDINGS, default_bindings=BINDINGS)
        widgets.append(widget)
        widget.show()
        app.processEvents()
        widget.visible_start.setValue(6)
        widget.update_preview()
        assert widget.value() == replace(preferences, visible_start_hour=6)
        if operation == 'cancel':
            widget.reject()
            assert widget.result_preferences == preferences
        elif operation == 'defaults':
            widget.restore_defaults()
            widget.save()
            assert widget.result_preferences == Preferences()
        elif operation == 'save':
            widget.save()
            assert widget.result() == QDialog.DialogCode.Accepted
            assert widget.result_preferences == replace(preferences, visible_start_hour=6)
        else:
            assert widget.apply()
            assert widget.result_preferences == replace(preferences, visible_start_hour=6)
        widget.finish_sources()
        widget.finish_sources()
        assert widget._initialized_categories == {'calendar'}
        assert widget._appearance_editor is widget._source_editor is widget._shortcut_editor is None


def test_direct_field_access_initializes_one_panel_without_changing_category(desktop):
    _, widgets = desktop
    widget = PreferencesDialog(Preferences(), bindings=BINDINGS, default_bindings=BINDINGS)
    widgets.append(widget)
    field = widget.font_family
    assert widget.appearance_editor.font_family is field
    assert widget._initialized_categories == {'calendar', 'appearance'}
    assert widget.page_heading.text() == 'Calendar'
    widget.shortcut_fields['search'].setKeySequence('Alt+F')
    assert widget._initialized_categories == {'calendar', 'appearance', 'keyboard shortcuts'}
    assert widget.shortcut_editor.fields is widget.shortcut_fields
    assert widget._source_editor is None


def test_visited_panels_keep_drafts_across_navigation_apply_and_defaults(desktop):
    _, widgets = desktop
    widget = PreferencesDialog(Preferences(), bindings=BINDINGS, default_bindings=BINDINGS)
    widgets.append(widget)
    widget.set_category('Appearance')
    appearance = widget.appearance_editor
    widget.font_scale.setValue(130)
    widget.control_style.setCurrentIndex(widget.control_style.findData('filled'))
    widget.set_category('Keyboard')
    shortcuts = widget.shortcut_editor
    widget.shortcut_fields['search'].setKeySequence('Alt+F')
    widget.set_category('Sync')
    widget.auto_sync.setChecked(False)
    widget.sync_interval.setValue(23)
    widget.set_category('Calendar')
    widget.visible_start.setValue(6)
    assert widget.apply()
    expected = Preferences(font_scale=130, control_style='filled', auto_sync_enabled=False,
                           sync_interval_minutes=23, visible_start_hour=6)
    assert widget.result_preferences == expected
    assert widget.result_bindings['search'] == 'Alt+F'
    widget.set_category('Appearance')
    assert widget.appearance_editor is appearance and widget.font_scale.value() == 130
    widget.set_category('Keyboard')
    assert widget.shortcut_editor is shortcuts and widget.shortcut_fields['search'].value() == 'Alt+F'
    widget.restore_defaults()
    widget.save()
    assert widget.result_preferences == Preferences()
    assert widget.result_bindings == BINDINGS
    assert widget._source_editor is None


def test_sources_snapshot_is_taken_on_first_visit_and_finished_only_once(desktop):
    _, widgets = desktop
    with Store(':memory:') as store:
        widget = PreferencesDialog(Preferences(), store=store)
        widgets.append(widget)
        assert widget._source_editor is None
        store.save_calendar(Calendar('later', 'Added after dialog opened', '#587c67'))
        editor = widget.source_editor
        assert 'later' in editor._originals
        assert widget.source_editor is editor
        with patch.object(editor, 'finish', wraps=editor.finish) as finish:
            widget.reject()
            widget.finish_sources()
            widget.finish_sources()
            assert finish.call_count == 1
        widgets.remove(widget)
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
