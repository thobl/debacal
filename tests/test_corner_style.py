"""A small theme choice reaches controls, grouped buttons, and event surfaces."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

from debacal.appearance import stylesheet
from debacal.calendar_widgets import CalendarCanvas, MonthCanvas
from debacal.choice_control import ChoiceControl
from debacal.models import Calendar, Event
from debacal.preferences import Preferences, SETTINGS_KEY
from debacal.store import Store
from debacal.theme import corner_radius


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, error, _trace: errors.append(error)):
        yield app, owned
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('style', ['square', 'subtle', 'rounded'])
def test_corner_choice_roundtrips_and_old_profiles_retain_current_shape(style):
    with Store(':memory:') as store:
        expected = Preferences(corner_style=style)
        expected.save(store)
        assert Preferences.load(store) == expected
        raw = store.get_setting(SETTINGS_KEY)
        raw.pop('corner_style')
        store.set_setting(SETTINGS_KEY, raw)
        assert Preferences.load(store).corner_style == 'subtle'
    with pytest.raises(ValueError, match='corners'):
        Preferences(corner_style='pill')


def test_bad_saved_corner_value_does_not_discard_other_preferences():
    with Store(':memory:') as store:
        store.set_setting(SETTINGS_KEY, {'corner_style': 'unknown', 'color_scheme': 'warm'})
        assert Preferences.load(store).corner_style == 'subtle'
        assert Preferences.load(store).color_scheme == 'warm'


def test_radius_resolution_is_shared_for_preferences_and_surface_objects():
    for style, factor in [('square', 0), ('subtle', 1), ('rounded', 2)]:
        pref = Preferences(corner_style=style)
        surface = type('Surface', (), {'preferences': pref})()
        assert corner_radius(pref, 3) == corner_radius(surface, 3) == 3 * factor
        rendered = stylesheet(100, pref)
        assert '$' not in rendered
        assert f'border-radius: {4 * factor}px' in rendered


def test_native_buttons_and_painted_choice_groups_both_follow_corner_choice(desktop):
    app, owned = desktop
    wrapper = QWidget()
    owned.append(wrapper)
    row = QHBoxLayout(wrapper)
    button = QPushButton('Continue')
    row.addWidget(button)
    choices = ChoiceControl([('Week', 'week'), ('Month', 'month')], Preferences(control_style='filled'))
    row.addWidget(choices)
    wrapper.resize(450, 70)
    wrapper.show()
    images, sizes = [], []
    for style in ('square', 'rounded'):
        preferences = Preferences(corner_style=style, control_style='filled')
        wrapper.setStyleSheet(stylesheet(100, preferences))
        choices.preferences = preferences
        for _ in range(5):
            app.processEvents()
        images.append((button.grab().toImage(), choices.grab().toImage()))
        sizes.append((button.size(), choices.size()))
    assert sizes[0] == sizes[1]
    assert all(first != second for first, second in zip(*images))


@pytest.mark.parametrize('surface_type', [CalendarCanvas, MonthCanvas])
def test_event_corners_change_without_changing_event_geometry(desktop, surface_type):
    app, owned = desktop
    surface = surface_type()
    owned.append(surface)
    event = Event('event', 'local', 'Planning', START, START + timedelta(hours=1))
    calendar = Calendar('local', 'Personal', '#587c67')
    surface.resize(650, 550)
    if surface_type is CalendarCanvas:
        surface.set_data([START.date()], [event], [calendar])
        surface.fit_to_viewport(550)
    else:
        surface.set_data(START.date().replace(day=1), 11, [event], [calendar])
    surface.show()
    captures = []
    for style in ('square', 'rounded'):
        surface.set_preferences(Preferences(corner_style=style))
        app.processEvents()
        captures.append(surface.grab().toImage())
    assert captures[0].size() == captures[1].size()
    assert captures[0] != captures[1]
