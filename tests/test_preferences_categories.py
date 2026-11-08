"""Locale, appearance, and embedded shortcut preferences remain one save."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale
from PySide6.QtWidgets import QApplication, QDialog

from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.shortcuts import KeyboardShortcutsEditor, validate_bindings
from debacal.store import Store


BINDINGS = {'new_event': 'N', 'search': 'Ctrl+F', 'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'}


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


def dialog(desktop, preferences=None, **kwargs):
    widget = PreferencesDialog(preferences or Preferences(), **kwargs)
    desktop[1].append(widget)
    return widget


def test_selected_date_language_controls_names_but_not_system_regional_defaults():
    with patch('debacal.preferences._system_locale', return_value=QLocale('en_US')):
        german = Preferences(locale_name='de_DE')
        english = Preferences(locale_name='en_GB')
        assert german.qt_locale().name() == 'de_DE'
        assert german.weekday_name(0, short=False) == 'Montag'
        assert german.month_name(5) == 'Mai'
        assert german.format_month(date(2027, 5, 1)) == 'Mai 2027'
        assert german.first_weekday == 6
        assert english.weekday_name(0, short=False) == 'Monday'
        assert english.month_name(date(2027, 5, 1)) == 'May'
        assert english.format_month(date(2027, 5, 1), year=False) == 'May'
        assert english.first_weekday == 6
        assert Preferences().first_weekday == 6
        assert Preferences(locale_name='de_DE', date_format='iso').format_date(date(2027, 5, 1)) == '2027-05-01'


def test_appended_preferences_keep_old_positional_constructors_and_persist(tmp_path):
    old = Preferences(6, 'iso', '12h', 'both', False, 15)
    assert old.locale_name == 'system' and old.font_scale == 100
    chosen = Preferences(6, 'iso', '12h', 'both', False, 15, 'de_DE', 120, True, 6, 22)
    path = tmp_path / 'calendar.sqlite'
    with Store(path) as store:
        chosen.save(store)
    with Store(path) as store:
        assert Preferences.load(store) == chosen


@pytest.mark.parametrize('values', [
    {'locale_name': 'unsupported'}, {'font_scale': 79}, {'font_scale': 141}, {'font_scale': True},
    {'show_week_numbers': 'yes'}, {'visible_start_hour': -1}, {'visible_end_hour': 25},
    {'visible_start_hour': 18, 'visible_end_hour': 18}, {'visible_start_hour': 20, 'visible_end_hour': 8},
])
def test_invalid_new_preferences_are_rejected(values):
    with pytest.raises(ValueError):
        Preferences(**values)


def test_loader_restores_valid_late_hour_pair_and_falls_back_for_invalid_settings():
    assert Preferences.from_dict({'visible_start_hour': 20, 'visible_end_hour': 23}) == Preferences(visible_start_hour=20, visible_end_hour=23)
    loaded = Preferences.from_dict({'locale_name': [], 'font_scale': 500, 'show_week_numbers': 'yes',
                                    'visible_start_hour': 23, 'visible_end_hour': 6, 'date_format': 'iso'})
    assert loaded == Preferences(date_format='iso')
    assert Preferences.from_dict({'visible_start_hour': -1, 'visible_end_hour': 22}).visible_end_hour == 22


def test_dialog_has_categories_and_updates_date_language_preview(desktop):
    widget = dialog(desktop, bindings=BINDINGS, default_bindings=BINDINGS)
    assert [widget.categories.item(index).text() for index in range(widget.categories.count())] == ['Calendar', 'Appearance', 'Calendar sources', 'Sync', 'Keyboard shortcuts']
    widget.locale.setCurrentIndex(widget.locale.findData('de_DE'))
    assert 'Mittwoch' in widget.date_format.toolTip() and 'Montag' in widget.date_format.toolTip()
    assert widget.week_start.itemText(widget.week_start.findData(0)) == 'Montag'
    assert widget.set_category('KEYBOARD') and widget.page_heading.text() == 'Keyboard shortcuts'
    assert widget.set_category('Appearance') and widget.page_heading.text() == 'Appearance'
    assert not widget.set_category('unknown')


def test_preferences_and_shortcuts_are_validated_and_saved_together(desktop):
    original = Preferences()
    widget = dialog(desktop, original, bindings=BINDINGS, default_bindings=BINDINGS)
    widget.font_scale.setValue(120)
    widget.week_numbers.setChecked(True)
    widget.visible_start.setValue(6)
    widget.visible_end.setValue(22)
    widget.shortcut_fields['scope_all'].setKeySequence('F2')
    widget.save()
    assert widget.result() != QDialog.DialogCode.Accepted
    assert widget.result_preferences == original
    assert widget.page_heading.text() == 'Keyboard shortcuts' and 'unique' in widget.error.text()
    widget.shortcut_fields['scope_all'].setKeySequence('F6')
    widget.save()
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.result_preferences == Preferences(font_scale=120, show_week_numbers=True, visible_start_hour=6, visible_end_hour=22)
    assert widget.result_bindings['scope_all'] == 'F6'


def test_invalid_hour_range_stays_open_and_locale_preview_remains_usable(desktop):
    widget = dialog(desktop)
    widget.visible_start.setValue(20)
    widget.visible_end.setValue(18)
    widget.locale.setCurrentIndex(widget.locale.findData('de_DE'))
    assert 'Mittwoch' in widget.date_format.toolTip()
    widget.save()
    assert widget.result() != QDialog.DialogCode.Accepted
    assert widget.page_heading.text() == 'Calendar' and 'hours' in widget.error.text()
    widget.visible_end.setValue(23)
    widget.save()
    assert widget.result_preferences.visible_start_hour == 20
    assert widget.result_preferences.visible_end_hour == 23


def test_restore_defaults_resets_each_category_and_shortcuts(desktop):
    original = Preferences(6, 'iso', '12h', 'both', False, 15, 'de_DE', 130, True, 6, 22)
    widget = dialog(desktop, original, bindings=BINDINGS | {'scope_all': 'F6'}, default_bindings=BINDINGS)
    widget.restore_defaults()
    widget.save()
    assert widget.result_preferences == Preferences()
    assert widget.result_bindings == BINDINGS


def test_creation_calendar_is_selected_in_the_workspace_instead_of_preferences(desktop):
    widget = dialog(desktop)
    assert not hasattr(widget, 'default_calendar')


def test_shared_shortcut_editor_accepts_empty_actions_and_retires_sequences(desktop):
    widget = KeyboardShortcutsEditor({'first': 'Ctrl+K', 'second': 'Ctrl+K, Ctrl+L'})
    desktop[1].append(widget)
    with pytest.raises(ValueError, match='single'):
        validate_bindings({'second': 'Ctrl+K, Ctrl+L'})
    assert not widget.value()['second']
    widget.fields['second'].setKeySequence('')
    assert widget.value()['second'] == ''
    assert validate_bindings({'first': 'ctrl+k'}) == {'first': 'Ctrl+K'}


def test_calendar_category_owns_functional_controls_and_selected_category_uses_shared_emphasis(desktop):
    widget = dialog(desktop)
    for field in (widget.locale, widget.week_start, widget.date_format, widget.time_format,
                  widget.visible_start, widget.visible_end, widget.week_numbers, widget.duration_presets):
        assert widget.calendar_page.isAncestorOf(field)
        assert not widget.appearance_editor.isAncestorOf(field)
    assert set(widget.appearance_editor.section_widgets) == {'Colors', 'Font', 'Interface', 'Year view', 'Sidebar'}
    for name in ('Calendar', 'Appearance', 'Calendar sources', 'Sync', 'Keyboard shortcuts'):
        assert widget.set_category(name)
        for row in range(widget.categories.count()):
            item = widget.categories.item(row)
            assert item.font().bold() == item.font().underline() == (item.text() == name)
    assert widget.set_category('events') and widget.page_heading.text() == 'Calendar'
