"""Categorized display, synchronization, and keyboard preferences."""
from datetime import date, datetime

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
    QCheckBox, QSpinBox, QTabWidget, QWidget,
)

from .preferences import Preferences, _system_language_locale
from .shortcuts import KeyboardShortcutsEditor
from .theme import SCHEMES, scheme_for


class PreferencesDialog(QDialog):
    def __init__(self, preferences: Preferences, parent=None, bindings=None, default_bindings=None):
        super().__init__(parent)
        self.setWindowTitle('Preferences')
        self.resize(640, 570)
        self.result_preferences = preferences
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        _, general_layout, form = self.category('General')
        self.locale = QComboBox()
        for name, value in [('System default', 'system'), ('English (United Kingdom)', 'en_GB'), ('Deutsch (Deutschland)', 'de_DE')]:
            self.locale.addItem(name, value)
        self.locale.setCurrentIndex(self.locale.findData(preferences.locale_name))
        form.addRow('Language for dates', self.locale)
        note = QLabel('Changes month and weekday names. Date format, time format, and start of week are independent. '
                      'Interface labels remain in English.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        form.addRow(note)
        language = _system_language_locale()
        language_name = QLocale.languageToString(language.language())
        self.system_locale_note = QLabel('System language: ' + language_name + '. Regional date/time settings: '
                                        + Preferences().format_date(date(2026, 11, 4)) + ' · '
                                        + Preferences().format_time(datetime(2026, 11, 4, 14, 5)) + '.')
        self.system_locale_note.setWordWrap(True)
        self.system_locale_note.setProperty('muted', True)
        self.system_locale_note.setToolTip('System language follows the operating system’s preferred UI languages. '
                                          'On Linux, LANGUAGE/LC_MESSAGES/LANG determine language, while LC_TIME '
                                          'determines regional dates and times; LC_ALL overrides locale categories.')
        form.addRow(self.system_locale_note)
        self.week_start = QComboBox()
        self.week_start.addItem('System default', 'system')
        for day in range(7):
            self.week_start.addItem(preferences.weekday_name(day, short=False), day)
        self.week_start.setCurrentIndex(self.week_start.findData(preferences.start_weekday))
        self.date_format = QComboBox()
        for name, value in [('System default', 'system'), ('European · 04.11.2026', 'european'),
                            ('ISO · 2026-11-04', 'iso'), ('US · 11/04/2026', 'us')]:
            self.date_format.addItem(name, value)
        self.date_format.setCurrentIndex(self.date_format.findData(preferences.date_format))
        self.time_format = QComboBox()
        for name, value in [('System default', 'system'), ('24-hour · 14:05', '24h'), ('12-hour · 2:05 PM', '12h')]:
            self.time_format.addItem(name, value)
        self.time_format.setCurrentIndex(self.time_format.findData(preferences.time_format))
        form.addRow('Start of week', self.week_start)
        form.addRow('Date format', self.date_format)
        form.addRow('Time format', self.time_format)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setProperty('muted', True)
        general_layout.addWidget(self.preview)
        general_layout.addStretch()

        _, appearance_layout, form = self.category('Appearance')
        self.color_scheme = QComboBox()
        self.color_scheme.setAccessibleName('Color scheme')
        for value, scheme in SCHEMES.items():
            self.color_scheme.addItem(scheme.label, value)
        self.color_scheme.setCurrentIndex(self.color_scheme.findData(preferences.color_scheme))
        form.addRow('Color scheme', self.color_scheme)
        self.control_style = QComboBox()
        self.control_style.setAccessibleName('Button group style')
        self.control_style.addItem('Underlined', 'underlined')
        self.control_style.addItem('Filled', 'filled')
        self.control_style.setCurrentIndex(self.control_style.findData(preferences.control_style))
        self.control_style.setToolTip('Changes the view and selection buttons. Their placement and the toolbar separator stay the same.')
        form.addRow('Button groups', self.control_style)
        self.agenda_style = QComboBox()
        self.agenda_style.setAccessibleName('Upcoming events layout')
        for name, value in [('Event cards', 'cards'), ('Cards grouped by day', 'grouped'),
                            ('Timeline within each day', 'timeline')]:
            self.agenda_style.addItem(name, value)
        self.agenda_style.setCurrentIndex(self.agenda_style.findData(preferences.agenda_style))
        self.agenda_style.setToolTip('Changes the upcoming events list. Search results always use event cards.')
        form.addRow('Upcoming events', self.agenda_style)
        self.font_scale = QSpinBox()
        self.font_scale.setRange(80, 140)
        self.font_scale.setSingleStep(10)
        self.font_scale.setSuffix(' %')
        self.font_scale.setValue(preferences.font_scale)
        self.week_numbers = QCheckBox('Show week numbers in calendars')
        self.week_numbers.setChecked(preferences.show_week_numbers)
        self.visible_start = QSpinBox()
        self.visible_start.setRange(0, 23)
        self.visible_start.setPrefix('From ')
        self.visible_start.setSuffix(':00')
        self.visible_start.setValue(preferences.visible_start_hour)
        self.visible_end = QSpinBox()
        self.visible_end.setRange(1, 24)
        self.visible_end.setPrefix('Until ')
        self.visible_end.setSuffix(':00')
        self.visible_end.setValue(preferences.visible_end_hour)
        form.addRow('Text size', self.font_scale)
        form.addRow('Week numbers', self.week_numbers)
        form.addRow('Default visible hours', self.visible_start)
        form.addRow('', self.visible_end)
        note = QLabel('Day and week views also include events outside these hours. Full day still shows 00:00–24:00.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        appearance_layout.addWidget(note)
        appearance_layout.addStretch()

        _, sync_layout, form = self.category('Sync')
        self.auto_sync = QCheckBox('Sync automatically while the app is open')
        self.auto_sync.setChecked(preferences.auto_sync_enabled)
        self.sync_interval = QSpinBox()
        self.sync_interval.setRange(1, 1440)
        self.sync_interval.setSuffix(' minute(s)')
        self.sync_interval.setValue(preferences.sync_interval_minutes)
        self.sync_interval.setEnabled(preferences.auto_sync_enabled)
        self.auto_sync.toggled.connect(self.sync_interval.setEnabled)
        self.conflict_notifications = QComboBox()
        for name, value in [('In the app', 'in_app'), ('Desktop notification', 'desktop'), ('Both', 'both')]:
            self.conflict_notifications.addItem(name, value)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData(preferences.conflict_notifications))
        form.addRow('Automatic syncing', self.auto_sync)
        form.addRow('Sync every', self.sync_interval)
        form.addRow('Conflict alerts', self.conflict_notifications)
        note = QLabel('Conflicts can always be reviewed in the app. Desktop alerts use your operating system’s notification settings.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        sync_layout.addWidget(note)
        sync_layout.addStretch()

        self.shortcut_editor = KeyboardShortcutsEditor(bindings, default_bindings)
        self.shortcut_fields = self.shortcut_editor.fields
        self.result_bindings = self.shortcut_editor.defaults | dict(bindings or {})
        self.tabs.addTab(self.shortcut_editor, 'Keyboard shortcuts')
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: ' + scheme_for(preferences).error + ';')
        outer.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                  | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        outer.addWidget(buttons)
        for field in (self.week_start, self.date_format, self.time_format):
            field.currentIndexChanged.connect(self.update_preview)
        self.locale.currentIndexChanged.connect(self.locale_changed)
        self.update_preview()

    def category(self, name):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        layout.addLayout(form)
        self.tabs.addTab(page, name)
        return page, layout, form

    def set_category(self, category):
        names = {'general': 0, 'appearance': 1, 'sync': 2, 'keyboard': 3, 'keyboard shortcuts': 3}
        index = names.get(str(category).strip().casefold())
        if index is None:
            return False
        self.tabs.setCurrentIndex(index)
        return True

    def value(self):
        return Preferences(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                           time_format=self.time_format.currentData(), conflict_notifications=self.conflict_notifications.currentData(),
                           auto_sync_enabled=self.auto_sync.isChecked(), sync_interval_minutes=self.sync_interval.value(),
                           locale_name=self.locale.currentData(), font_scale=self.font_scale.value(),
                           show_week_numbers=self.week_numbers.isChecked(), visible_start_hour=self.visible_start.value(),
                           visible_end_hour=self.visible_end.value(), color_scheme=self.color_scheme.currentData(),
                           agenda_style=self.agenda_style.currentData(), control_style=self.control_style.currentData())

    def locale_changed(self, *_):
        preferences = Preferences(locale_name=self.locale.currentData())
        for day in range(7):
            self.week_start.setItemText(day + 1, preferences.weekday_name(day, short=False))
        self.update_preview()

    def update_preview(self, *_):
        preferences = Preferences(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                                  time_format=self.time_format.currentData(), locale_name=self.locale.currentData())
        day = date(2026, 11, 4)
        self.preview.setText(preferences.weekday_name(day, short=False) + ', ' + preferences.format_date(day) + ' · '
                             + preferences.format_time(datetime(2026, 11, 4, 14, 5)) + '\nWeek begins '
                             + preferences.weekday_name(preferences.first_weekday, short=False) + '.')

    def restore_defaults(self):
        for field in (self.locale, self.week_start, self.date_format, self.time_format):
            field.setCurrentIndex(0)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData('in_app'))
        self.auto_sync.setChecked(True)
        self.sync_interval.setValue(1)
        self.font_scale.setValue(100)
        self.color_scheme.setCurrentIndex(self.color_scheme.findData('neutral'))
        self.control_style.setCurrentIndex(self.control_style.findData(Preferences().control_style))
        self.agenda_style.setCurrentIndex(self.agenda_style.findData(Preferences().agenda_style))
        self.week_numbers.setChecked(Preferences().show_week_numbers)
        self.visible_start.setValue(8)
        self.visible_end.setValue(18)
        self.shortcut_editor.restore_defaults()
        self.error.clear()

    def save(self):
        try:
            preferences = self.value()
        except ValueError as error:
            self.tabs.setCurrentIndex(1)
            self.error.setText(str(error))
            return
        try:
            bindings = self.shortcut_editor.value()
        except ValueError as error:
            self.tabs.setCurrentWidget(self.shortcut_editor)
            self.shortcut_editor.error.setText(str(error))
            self.error.setText(str(error))
            return
        self.result_preferences, self.result_bindings = preferences, bindings
        self.error.clear()
        self.accept()
