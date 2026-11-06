"""Calendar discovery asks for an account, then uses the server calendar names."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from concurrent.futures import Future
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog

from debacal.dialogs import CalendarDialog
from debacal.discovery import DiscoveredCalendar
from debacal.models import Calendar


@pytest.fixture
def dialogs():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch("sys.excepthook", side_effect=lambda _kind, error, _traceback: errors.append(error)):
        def create(*args, **kwargs):
            dialog = CalendarDialog(*args, **kwargs)
            widgets.append(dialog)
            dialog.show()
            dialog.activateWindow()
            app.processEvents()
            return dialog
        yield create
        for dialog in reversed(widgets):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_type_switches_show_only_relevant_name_and_connection_fields(dialogs):
    dialog = dialogs()
    assert dialog.name.isVisible() and dialog.form.labelForField(dialog.name).isVisible()
    assert not dialog.url.isVisible() and not dialog.form.labelForField(dialog.url).isVisible()
    assert not dialog.username.isVisible() and not dialog.password.isVisible()
    assert not dialog.discover_button.isVisible() and not dialog.source_hint.isVisible()
    assert not hasattr(dialog, "make_default")
    assert not any("Default for" in field.text() for field in dialog.findChildren(QCheckBox))

    dialog.kind.setCurrentIndex(dialog.kind.findData("ics"))
    assert dialog.name.isVisible() and dialog.url.isVisible()
    assert "Subscription" in dialog.form.labelForField(dialog.url).text()
    assert not dialog.username.isVisible() and not dialog.password.isVisible()
    assert not dialog.remember_password.isVisible() and not dialog.use_server_color.isVisible()
    assert dialog.color.isEnabled() and not dialog.discover_button.isVisible()

    dialog.kind.setCurrentIndex(dialog.kind.findData("caldav"))
    assert not dialog.name.isVisible() and not dialog.form.labelForField(dialog.name).isVisible()
    assert "Server" in dialog.form.labelForField(dialog.url).text()
    assert all(field.isVisible() for field in (dialog.url, dialog.username, dialog.password,
                                               dialog.remember_password, dialog.use_server_color, dialog.discover_button))
    assert dialog.remember_password.isChecked() and not dialog.color.isEnabled()
    dialog.use_server_color.setChecked(False)
    assert dialog.color.isEnabled() and dialog.choose_color_button.isEnabled()

    dialog.kind.setCurrentIndex(dialog.kind.findData("local"))
    assert dialog.name.isVisible() and not dialog.url.isVisible()
    assert dialog.color.isEnabled() and not dialog.discovered_list.isVisible()


def test_remote_account_keyboard_chain_skips_hidden_name_and_inactive_color(dialogs):
    dialog = dialogs()
    dialog.kind.setCurrentIndex(dialog.kind.findData("caldav"))
    QTest.keyClick(dialog, Qt.Key.Key_T, Qt.KeyboardModifier.AltModifier)
    assert dialog.kind.hasFocus()
    for field in (dialog.url, dialog.username, dialog.password, dialog.remember_password,
                  dialog.use_server_color, dialog.discover_button, dialog.save_button, dialog.cancel_button):
        QTest.keyClick(dialog.focusWidget(), Qt.Key.Key_Tab)
        assert field.hasFocus(), type(field).__name__
    QTest.keyClick(dialog.cancel_button, Qt.Key.Key_Tab)
    assert dialog.kind.hasFocus()


def test_keyboard_discovery_uses_server_names_and_checks_calendar_choices(dialogs):
    dialog = dialogs()
    dialog.kind.setCurrentIndex(dialog.kind.findData("caldav"))
    assert dialog.name.text() == ""
    dialog.url.setText("cloud.example.test")
    dialog.username.setText("synthetic-user")
    dialog.password.setText("synthetic-password")
    future = Future()
    with patch.object(dialog.discovery_executor, "submit", return_value=future) as submit:
        dialog.password.setFocus()
        QTest.keyClick(dialog.password, Qt.Key.Key_Return)
        assert submit.call_count == 1 and dialog.result() == QDialog.DialogCode.Rejected
        assert dialog.url.text() == "https://cloud.example.test"
        future.set_result([
            DiscoveredCalendar("Work & projects", "https://cloud.example.test/work/", color="#123456"),
            DiscoveredCalendar("Shared read-only", "https://cloud.example.test/shared/", read_only=True),
        ])
        dialog.poll_discovery()
    assert dialog.discovered_list.hasFocus() and dialog.discovered_list.count() == 2
    assert dialog.discovered_list.item(0).text() == "Work & projects"
    assert not dialog.discovered_list.item(0).flags() & Qt.ItemFlag.ItemIsEditable
    assert "read only" in dialog.discovered_list.item(1).text()
    dialog.discovered_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    QTest.keyClick(dialog.discovered_list, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.result_calendars) == 1
    assert dialog.result_calendar.name == "Shared read-only" and dialog.result_calendar.read_only
    assert dialog.result_calendar.username == "synthetic-user"


def test_existing_remote_edit_keeps_name_locked_identity_and_read_only_metadata(dialogs):
    original = Calendar("remote", "Server name", "#123456", kind="caldav",
                        url="https://cloud.example.test/work/", username="synthetic-user",
                        read_only=True, server_color="#123456")
    dialog = dialogs(original)
    assert dialog.name.isVisible() and not dialog.kind.isEnabled()
    assert dialog.url.isReadOnly() and dialog.username.isReadOnly()
    assert not dialog.discover_button.isVisible()
    dialog.name.setText("Renamed locally")
    dialog.password.setText("synthetic-new-password")
    dialog.save()
    result = dialog.result_calendar
    assert result.id == original.id and result.name == "Renamed locally"
    assert (result.kind, result.url, result.username, result.read_only, result.server_color) == (
        original.kind, original.url, original.username, original.read_only, original.server_color,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_switching_to_local_during_discovery_never_shows_remote_choices_or_steals_focus(dialogs, failed):
    dialog = dialogs()
    dialog.kind.setCurrentIndex(dialog.kind.findData("caldav"))
    dialog.url.setText("https://cloud.example.test/")
    dialog.username.setText("synthetic-user")
    future = Future()
    with patch.object(dialog.discovery_executor, "submit", return_value=future):
        dialog.discover()
    dialog.kind.setCurrentIndex(dialog.kind.findData("local"))
    dialog.name.setText("Local calendar")
    dialog.name.setFocus()
    assert dialog.save_button.isEnabled()
    if failed:
        future.set_exception(ValueError("Synthetic discovery failure"))
    else:
        future.set_result([DiscoveredCalendar("Remote name", "https://cloud.example.test/work/")])
    dialog.poll_discovery()
    assert dialog.name.hasFocus() and not dialog.discovered_list.isVisible()
    assert dialog.error.text() == ""
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert (dialog.result_calendar.name, dialog.result_calendar.kind) == ("Local calendar", "local")
    assert dialog.result_calendar.url == "" and dialog.result_calendar.username == ""
