"""Persisted interface schemes stay readable without changing calendar data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QPushButton, QWidget

from debacal.appearance import stylesheet
from debacal.models import Calendar
from debacal.preferences import Preferences, SETTINGS_KEY
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.theme import SCHEMES, scheme_for


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


def contrast(first, second):
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                  for value in channels]
        return sum(weight * channel for weight, channel in zip((.2126, .7152, .0722), linear))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


@pytest.mark.parametrize('name', SCHEMES)
def test_interface_roles_have_readable_text_and_distinguishable_focus(name):
    scheme = scheme_for(name)
    assert scheme.background == '#ffffff'
    for foreground in (scheme.ink, scheme.muted, scheme.weekend):
        for background in (scheme.background, scheme.sidebar, scheme.panel):
            assert contrast(foreground, background) >= 4.5, (name, foreground, background)
    assert contrast(scheme.ink, scheme.selection) >= 4.5
    assert contrast(scheme.on_accent, scheme.accent) >= 4.5
    assert contrast(scheme.notice_ink, scheme.notice_background) >= 4.5
    assert contrast(scheme.focus, scheme.background) >= 3
    assert contrast(scheme.focus, scheme.sidebar) >= 3
    assert contrast(scheme.now, scheme.background) >= 3


def test_scheme_resolution_accepts_preferences_and_widgets_with_safe_fallbacks():
    preferences = Preferences(color_scheme='warm')
    widget = SimpleNamespace(preferences=preferences)
    assert scheme_for(widget) is scheme_for(preferences) is SCHEMES['warm']
    assert scheme_for(SCHEMES['cool']) is SCHEMES['cool']
    for value in (None, 'missing', [], SimpleNamespace(color_scheme=[]),
                  SimpleNamespace(preferences=SimpleNamespace(color_scheme='missing'))):
        assert scheme_for(value) is SCHEMES['neutral']
    with pytest.raises(FrozenInstanceError):
        SCHEMES['neutral'].ink = '#ffffff'


@pytest.mark.parametrize('name', SCHEMES)
def test_color_scheme_survives_restart_and_preserves_server_colors(tmp_path, name):
    path = tmp_path / 'calendar.sqlite'
    calendar = Calendar('remote', 'Server calendar', '#112233', kind='caldav', server_color='#ffff00')
    with Store(path) as store:
        store.save_calendar(calendar)
        calendars = store.list_calendars()
        Preferences(color_scheme=name).save(store)
    with Store(path) as store:
        assert Preferences.load(store).color_scheme == name
        assert store.list_calendars() == calendars
        assert next(item for item in store.list_calendars() if item.id == 'remote').color == '#ffff00'


@pytest.mark.parametrize('invalid', ['missing', '', None, [], True])
def test_bad_saved_scheme_falls_back_without_discarding_other_preferences(tmp_path, invalid):
    with Store(tmp_path / 'calendar.sqlite') as store:
        store.set_setting(SETTINGS_KEY, {'color_scheme': invalid, 'date_format': 'iso'})
        assert Preferences.load(store) == Preferences(date_format='iso')
    with pytest.raises(ValueError):
        Preferences(color_scheme=invalid)


def test_older_positional_preferences_keep_neutral_scheme():
    old = Preferences(6, 'iso', '12h', 'both', False, 15, 'de_DE', 120, True, 6, 22)
    assert old.color_scheme == 'neutral'


def test_appearance_choice_is_saved_with_preferences_and_restored_to_default(desktop):
    widget = PreferencesDialog(Preferences(color_scheme='warm', date_format='iso'))
    desktop[1].append(widget)
    assert widget.color_scheme.currentData() == 'warm'
    assert widget.set_category('Appearance')
    widget.color_scheme.setCurrentIndex(widget.color_scheme.findData('cool'))
    widget.save()
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.result_preferences == Preferences(color_scheme='cool', date_format='iso')
    widget.restore_defaults()
    assert widget.value() == Preferences()


def test_cancel_does_not_apply_the_pending_scheme(desktop):
    original = Preferences(color_scheme='warm')
    widget = PreferencesDialog(original)
    desktop[1].append(widget)
    widget.color_scheme.setCurrentIndex(widget.color_scheme.findData('cool'))
    widget.reject()
    assert widget.result_preferences == original


@pytest.mark.parametrize('name', SCHEMES)
def test_ghost_actions_show_keyboard_focus_without_changing_geometry(desktop, name):
    app, widgets = desktop
    window = QWidget()
    widgets.append(window)
    window.setStyleSheet(stylesheet(100, name))
    layout = QHBoxLayout(window)
    action, other = QPushButton('A'), QPushButton('B')
    action.setProperty('iconOnly', True)
    layout.addWidget(action)
    layout.addWidget(other)
    window.show()
    window.activateWindow()
    action.setFocus()
    app.processEvents()
    assert action.hasFocus()
    focused, size = action.grab().toImage(), action.size()
    other.setFocus()
    app.processEvents()
    assert not action.hasFocus()
    assert action.size() == size
    assert focused != action.grab().toImage()


def test_stylesheet_keeps_the_existing_scale_call_and_accepts_preference_objects():
    assert stylesheet(120) == stylesheet(120, 'neutral')
    style = stylesheet(120, Preferences(color_scheme='warm'))
    assert 'font-size: 16px' in style
    assert SCHEMES['warm'].sidebar in style
    assert '$' not in style
