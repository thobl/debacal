"""Locale resolution, explicit formats, persistence, and native preferences UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QDialog

from debacal.preferences import Preferences, format_date, format_time
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store


class PreferencesTests(unittest.TestCase):
    def test_system_defaults_follow_os_locale(self):
        with patch('debacal.preferences._system_locale', return_value=QLocale('en_US')):
            preferences = Preferences()
            self.assertEqual(preferences.first_weekday, 6)
            self.assertEqual(preferences.week_start(date(2026, 11, 4)), date(2026, 11, 1))
            self.assertIn('PM', preferences.format_time(time(14, 5)))
        with patch('debacal.preferences._system_locale', return_value=QLocale('de_DE')):
            preferences = Preferences()
            self.assertEqual(preferences.first_weekday, 0)
            self.assertEqual(preferences.week_start(date(2026, 11, 4)), date(2026, 11, 2))
            self.assertEqual(preferences.format_time(time(14, 5)), '14:05')

    def test_explicit_formats_override_system(self):
        with patch('debacal.preferences._system_locale', return_value=QLocale('en_US')):
            preferences = Preferences(start_weekday=0, date_format='european', time_format='24h')
            self.assertEqual(preferences.format_date(date(2026, 11, 4)), '04.11.2026')
            self.assertEqual(preferences.format_time(time(14, 5)), '14:05')
            self.assertEqual(preferences.first_weekday, 0)
            self.assertEqual(preferences.qt_datetime_pattern(), 'dd.MM.yyyy   HH:mm')
            self.assertEqual(format_date(date(2026, 11, 4), preferences), '04.11.2026')
            self.assertEqual(format_time(datetime(2026, 11, 4, 14, 5), preferences), '14:05')
            self.assertEqual(Preferences(date_format='iso').format_date(date(2026, 11, 4)), '2026-11-04')
            self.assertEqual(Preferences(date_format='us').format_date(date(2026, 11, 4)), '11/04/2026')
            self.assertEqual(Preferences(time_format='12h').format_time(time(0, 5)), '12:05 AM')
            self.assertEqual(Preferences(time_format='12h').format_time(time(14, 5)), '2:05 PM')

    def test_all_week_starts_cross_month_and_year_correctly(self):
        day = date(2027, 1, 1)
        for first in range(7):
            with self.subTest(first=first):
                start = Preferences(start_weekday=first).week_start(day)
                self.assertEqual(start.weekday(), first)
                self.assertLessEqual(start, day)
                self.assertLess((day - start).days, 7)

    def test_settings_survive_restart(self):
        preferences = Preferences(start_weekday=6, date_format='iso', time_format='12h', conflict_notifications='both')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.sqlite3'
            with Store(path) as store:
                preferences.save(store)
            with Store(path) as store:
                self.assertEqual(Preferences.load(store), preferences)

    def test_invalid_settings_fall_back_per_field(self):
        self.assertEqual(Preferences.from_dict({'start_weekday': 9, 'date_format': 'iso', 'time_format': 'invalid'}),
                         Preferences(date_format='iso'))
        self.assertEqual(Preferences.from_dict(None), Preferences())
        self.assertEqual(Preferences.from_dict({'date_format': [], 'start_weekday': True}), Preferences())
        for values in [{'start_weekday': -1}, {'start_weekday': 7}, {'start_weekday': False},
                       {'date_format': 'other'}, {'time_format': 'other'}, {'conflict_notifications': 'other'}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Preferences(**values)

    def test_localized_weekday_names(self):
        with patch('debacal.preferences._system_locale', return_value=QLocale('de_DE')):
            self.assertEqual(Preferences().weekday_name(6, short=False), 'Sonntag')
            self.assertIn('Mi', Preferences(date_format='iso').format_date(date(2026, 11, 4), weekday=True))


class PreferencesDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_live_preview_and_save(self):
        dialog = PreferencesDialog(Preferences())
        dialog.week_start.setCurrentIndex(dialog.week_start.findData(6))
        dialog.date_format.setCurrentIndex(dialog.date_format.findData('iso'))
        dialog.time_format.setCurrentIndex(dialog.time_format.findData('24h'))
        self.assertIn('2026-11-04', dialog.preview.text())
        self.assertIn('14:05', dialog.preview.text())
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_preferences, Preferences(6, 'iso', '24h'))
        dialog.close()

    def test_restore_defaults_uses_system_for_each_setting(self):
        dialog = PreferencesDialog(Preferences(6, 'iso', '12h', 'both'))
        dialog.restore_defaults()
        dialog.save()
        self.assertEqual(dialog.result_preferences, Preferences())
        dialog.close()

    def test_conflict_alert_preference_can_choose_desktop_or_both(self):
        dialog = PreferencesDialog(Preferences())
        self.assertEqual(dialog.conflict_notifications.currentData(), 'in_app')
        dialog.conflict_notifications.setCurrentIndex(dialog.conflict_notifications.findData('desktop'))
        dialog.save()
        self.assertEqual(dialog.result_preferences.conflict_notifications, 'desktop')
        dialog.conflict_notifications.setCurrentIndex(dialog.conflict_notifications.findData('both'))
        dialog.save()
        self.assertEqual(dialog.result_preferences.conflict_notifications, 'both')
        dialog.close()


if __name__ == '__main__':
    unittest.main()
