"""Duration shortcuts are configurable without resetting unrelated preferences."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store


@pytest.mark.parametrize('value', [(), (0,), (30, 30), (True,), (1.5,), (10081,), tuple(range(1, 8)), '30,60'])
def test_invalid_preset_settings_are_rejected(value):
    with pytest.raises(ValueError):
        Preferences(duration_presets_minutes=value)


def test_preset_values_survive_json_and_normalize_to_an_immutable_tuple():
    preferences = Preferences(duration_presets_minutes=[15, 30, 90])
    assert preferences.duration_presets_minutes == (15, 30, 90)
    with Store(':memory:') as store:
        preferences.save(store)
        assert Preferences.load(store) == preferences
    assert Preferences.from_dict({'duration_presets_minutes': [0]}) == Preferences()


def test_events_preferences_apply_preserve_other_categories_and_validate():
    app = QApplication.instance() or QApplication([])
    original = Preferences(color_scheme='warm', font_scale=120, auto_sync_enabled=False)
    dialog = PreferencesDialog(original)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.set_category('Events')
        dialog.duration_presets.setText('15min, 1h, 1h30min')
        assert dialog.apply()
        assert dialog.result_preferences == replace(original, duration_presets_minutes=(15, 60, 90))
        assert dialog.isVisible()
        dialog.duration_presets.setText('15min, 0')
        assert not dialog.apply() and dialog.tabs.tabText(dialog.tabs.currentIndex()) == 'Events'
        assert dialog.result_preferences.duration_presets_minutes == (15, 60, 90)
        dialog.restore_defaults()
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted and dialog.result_preferences == Preferences()
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
