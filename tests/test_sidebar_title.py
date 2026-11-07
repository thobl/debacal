"""Custom and hidden sidebar titles preserve access to application preferences."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtWidgets import QApplication, QDialog
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.ui import MainWindow


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('scale', [80, 140])
def test_hiding_title_reclaims_space_and_keeps_settings_accessible(mode, scale):
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        preferences = Preferences(appearance_mode=mode, font_scale=scale)
        preferences.save(store)
        window = MainWindow(store, enable_sync=False)
        window.resize(1030, 690)
        window.show()
        settle(app)
        try:
            previous = window.mini.mapTo(window.sidebar, QPoint()).y()
            window.apply_preferences(replace(preferences, show_sidebar_title=False), window.bindings)
            settle(app)
            assert not window.brand_bar.isVisible()
            assert window.mini.mapTo(window.sidebar, QPoint()).y() < previous
            assert window.preferences_button.isVisible()
            assert window.preferences_button.parentWidget() is window.sidebar_footer
            with patch.object(PreferencesDialog, 'exec', return_value=QDialog.DialogCode.Rejected) as dialog:
                window.preferences_button.click()
                assert dialog.call_count == 1
            title = 'A calendar with a long custom title ' * 3
            window.apply_preferences(replace(preferences, sidebar_title=title), window.bindings)
            settle(app)
            assert window.brand_bar.isVisible()
            assert window.brand_label.text() == window.brand_label.toolTip() == title
            assert window.preferences_button.parentWidget() is window.brand_bar
            assert window.mini.mapTo(window.sidebar, QPoint()).y() == previous
            assert window.width() == 1030
            assert Preferences.load(store).sidebar_title == title
        finally:
            window.close()
            window.executor.shutdown(wait=True)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
