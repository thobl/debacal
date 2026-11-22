from dataclasses import replace

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from alarm_support import BASE
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.reminder_preferences import (CalendarReminderPolicy, ReminderPreferences,
                                         load_reminder_preferences, save_reminder_preferences)
from debacal.store import Store
from debacal.ui import MainWindow


def test_preference_apply_cancel_and_calendar_policy_only_changes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    with Store(tmp_path / 'calendar') as store:
        store.save_calendar(Calendar('ro', 'Subscription', '#123456', kind='ics', read_only=True))
        window = MainWindow(store, enable_sync=False)
        try:
            sources_changed = []
            monkeypatch.setattr(window, 'preferences_sources_changed', lambda: sources_changed.append(True))
            journal = store.connection.total_changes
            def edit(dialog):
                assert dialog.page_heading.text() == 'Calendar sources'
                sources = dialog.source_editor
                assert sources._selected == 'ro'
                assert 'notifications' not in dialog._initialized_categories
                sources.reminder_mode.setCurrentIndex(sources.reminder_mode.findData('mute'))
                dialog.set_category('Notifications')
                page = dialog.notification_page
                assert page.value() == ReminderPreferences()
                assert all(editor.templates_only for editor in (page.new_timed, page.new_all_day, page.import_alarms))
                assert 'desktop' in page.desktop_status.text().lower() and 'tray' in page.tray_status.text().lower()
                external = CalendarReminderPolicy('mute')
                save_reminder_preferences(store, ReminderPreferences(calendars={'elsewhere': external}), now=BASE)
                assert dialog.apply()
                expected = ReminderPreferences(calendars={'elsewhere': external, 'ro': external})
                assert load_reminder_preferences(store) == expected
                page.enabled.setChecked(True)
                previews = []
                dialog.reminderTestRequested.connect(previews.append)
                page.test_button.click()
                assert previews and previews[0].enabled and load_reminder_preferences(store) == expected
                page.delivery.setCurrentIndex(page.delivery.findData('in_app'))
                dialog.save()
                assert load_reminder_preferences(store) == replace(expected, enabled=True, delivery='in_app')
                return QDialog.DialogCode.Accepted
            monkeypatch.setattr(PreferencesDialog, 'exec', edit)
            class ContextMenu(QMenu):
                def exec(self, point):
                    next(action for action in self.actions() if action.text() == 'Reminder settings…').trigger()
            monkeypatch.setattr('debacal.ui.QMenu', ContextMenu)
            window.calendar_menu('ro', QPoint())
            assert not store.history() and not store.connection.execute('SELECT 1 FROM outbox').fetchone()
            assert not sources_changed and store.connection.total_changes == journal
            def cancel(dialog):
                dialog.notification_page.enabled.setChecked(False)
                dialog.reject()
                return dialog.result()
            monkeypatch.setattr(PreferencesDialog, 'exec', cancel)
            window.show_preferences('notifications')
            assert load_reminder_preferences(store).enabled
        finally:
            window.close()


def test_standalone_defaults_preserve_local_calendar_policy():
    app = QApplication.instance() or QApplication([])
    prefs = ReminderPreferences(enabled=True, calendars={'ro': CalendarReminderPolicy('mute')})
    dialog = PreferencesDialog(Preferences(), reminder_preferences=prefs)
    dialog.restore_defaults()
    dialog.save()
    assert dialog.result_reminders == replace(prefs, enabled=False)


def test_disabled_delivery_allows_default_and_background_edits():
    from debacal.alarms import Alarm
    app = QApplication.instance() or QApplication([])
    dialog = PreferencesDialog(Preferences())
    dialog.set_category('Notifications')
    page = dialog.notification_page
    default = Alarm('default', 'DISPLAY', '-PT15M')
    page.new_timed._change((default,))
    page.close_behavior.setCurrentIndex(page.close_behavior.findData('tray'))
    dialog.save()
    assert page.new_timed.isEnabled() and page.close_behavior.isEnabled()
    assert dialog.result_reminders == ReminderPreferences(new_timed=(default,), close_behavior='tray')


def test_preview_footer_delivery_gate_and_external_presentation(tmp_path):
    from test_reminder_dialog import workspace
    from alarm_support import FakeClock, wait_for
    path = tmp_path/'profile'
    clock = FakeClock(BASE)
    app = QApplication.instance() or QApplication([])
    with Store(path) as store, workspace(path, clock, store) as (window, transport, page):
        controller = window.reminder_controller
        assert not page() and not transport.started
        assert not (tmp_path/'profile.reminders.sqlite3').exists()
        def preview(dialog):
            page = dialog.notification_page
            page.delivery.setCurrentIndex(page.delivery.findData('both'))
            page.sound.setCurrentIndex(page.sound.findData('silent'))
            page.test_button.click()
            wait_for(lambda: transport.sent and page.test_result.text() == 'Test notification')
            assert transport.sent[-1][-1]
            controller._availability(False)
            wait_for(lambda: 'unavailable' in page.desktop_status.text())
            assert len(window.local_issues) == 1 and window.local_issues[0].action is not None
            controller._availability(True)
            wait_for(lambda: not window.local_issues)
            transport.delivered.emit(transport.sent[-1][0].key, 1)
            return QDialog.DialogCode.Rejected
        from unittest.mock import patch
        with patch.object(PreferencesDialog, 'exec', preview):
            window.show_preferences('Notifications')
        assert not page() and load_reminder_preferences(store) == ReminderPreferences()
        assert not (tmp_path/'profile.reminders.sqlite3').exists()
        external = ReminderPreferences(enabled=True, delivery='desktop')
        save_reminder_preferences(store, external, now=BASE)
        controller.reload_settings()
        wait_for(lambda: window.reminder_preferences == external)
        controller.unread_count = 3
        window._reminder_badge()
        assert 'unread' not in window.reminder_button.accessibleName()
    with Store(path) as store, workspace(path, clock, store, demo=True) as (demo, transport, page):
        assert not page() and not transport.sent and not transport.started


def test_combined_source_enable_observes_local_policy_at_commit(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    import sqlite3
    import debacal.preferences_dialog as settings
    app = QApplication.instance() or QApplication([])
    with Store(tmp_path/'calendar') as store:
        store.save_calendar(Calendar('c', 'Calendar', '#123456', enabled=False))
        original = ReminderPreferences(enabled=True)
        save_reminder_preferences(store, original, now=BASE)
        dialog = PreferencesDialog(Preferences(), store=store, reminder_preferences=original)
        editor = dialog.source_editor
        editor.select_calendar('c')
        editor._items['c'].setCheckState(0, Qt.CheckState.Checked)
        editor.reminder_mode.setCurrentIndex(editor.reminder_mode.findData('mute'))
        observed = []
        dialog.calendars_changed.connect(lambda: observed.append(load_reminder_preferences(store).calendars.get('c')))
        def fail(*args, **options):
            raise sqlite3.OperationalError('Local settings unavailable')
        with monkeypatch.context() as failing:
            failing.setattr(settings, 'save_reminder_draft', fail)
            assert not dialog.apply()
            assert not observed and not store.get_calendar('c').enabled
        assert dialog.apply()
        assert store.get_calendar('c').enabled and observed == [CalendarReminderPolicy('mute')]
        dialog.finish_sources()
        dialog.close()
        store._standalone_ui_commands.shutdown()
