"""Categorized display, synchronization, and keyboard preferences."""
from datetime import date, datetime, timedelta

from PySide6.QtCore import QLocale, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
    QCheckBox, QFrame, QScrollArea, QSpinBox, QTabWidget, QWidget, QLineEdit,
)

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .preferences import Preferences, _system_language_locale
from .appearance_preferences import AppearanceEditor
from .shortcuts import KeyboardShortcutsEditor
from .theme import scheme_for
from .date_time_fields import parse_duration, format_duration


class PreferencesDialog(QDialog):
    applied = Signal(object, object)

    def __init__(self, preferences: Preferences, parent=None, bindings=None, default_bindings=None):
        super().__init__(parent)
        install_button_focus(QApplication.instance())
        self.setPalette(widget_palette(preferences))
        self.setStyleSheet(stylesheet(preferences.font_scale, preferences))
        self.setLocale(preferences.qt_locale())
        self.setWindowTitle('Preferences')
        bounds = self.screen().availableGeometry()
        self.resize(min(760, max(440, bounds.width() - 64)), min(720, max(440, bounds.height() - 64)))
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

        _, appearance_layout, _ = self.category('Appearance')
        self.appearance_editor = AppearanceEditor(preferences)
        appearance_layout.addWidget(self.appearance_editor)
        for name in ('appearance_mode', 'color_scheme', 'custom_accent', 'custom_surface', 'custom_weekend',
                     'control_style', 'agenda_style', 'weekend_style', 'font_scale', 'week_numbers',
                     'visible_start', 'visible_end', 'show_sidebar_title', 'sidebar_title'):
            setattr(self, name, getattr(self.appearance_editor, name))

        self.events_page, events_layout, form = self.category('Events')
        self.duration_presets = QLineEdit(', '.join(format_duration(timedelta(minutes=value))
                                                   for value in preferences.duration_presets_minutes))
        self.duration_presets.setAccessibleName('Duration buttons')
        self.duration_presets.setPlaceholderText('30min, 45min, 1h')
        form.addRow('Duration buttons', self.duration_presets)
        note = QLabel('Separate the durations with commas. These buttons do not change the default length of a new event.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        events_layout.addWidget(note)
        events_layout.addStretch()

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
        self._applied_state = preferences, dict(self.result_bindings)
        self.tabs.addTab(self.shortcut_editor, 'Keyboard shortcuts')
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: ' + scheme_for(preferences).error + ';')
        outer.addWidget(self.error)
        buttons = self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                                  | QDialogButtonBox.StandardButton.Apply
                                                  | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        outer.addWidget(buttons)
        for field in (self.week_start, self.date_format, self.time_format):
            field.currentIndexChanged.connect(self.update_preview)
        self.locale.currentIndexChanged.connect(self.locale_changed)
        self.update_preview()

    def category(self, name):
        page = QWidget()
        page.setObjectName('preferencesPage')
        layout = QVBoxLayout(page)
        form = QFormLayout()
        layout.addLayout(form)
        scroll = QScrollArea()
        scroll.setObjectName('preferences' + name)
        scroll.setAccessibleName(name + ' preferences')
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, name)
        return page, layout, form

    def set_category(self, category):
        name = str(category).strip().casefold()
        name = 'keyboard shortcuts' if name == 'keyboard' else name
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index).casefold() == name:
                self.tabs.setCurrentIndex(index)
                return True
        return False

    def duration_values(self):
        values = [parse_duration(part) for part in self.duration_presets.text().split(',')]
        if any(value % timedelta(minutes=1) for value in values):
            raise ValueError('Duration buttons must use whole minutes.')
        minutes = tuple(int(value.total_seconds() // 60) for value in values)
        return Preferences(duration_presets_minutes=minutes).duration_presets_minutes

    def value(self):
        return Preferences(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                           time_format=self.time_format.currentData(), conflict_notifications=self.conflict_notifications.currentData(),
                           auto_sync_enabled=self.auto_sync.isChecked(), sync_interval_minutes=self.sync_interval.value(),
                           locale_name=self.locale.currentData(), duration_presets_minutes=self.duration_values(),
                           **self.appearance_editor.values())

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
        self.appearance_editor.set_context(preferences)

    def restore_defaults(self):
        for field in (self.locale, self.week_start, self.date_format, self.time_format):
            field.setCurrentIndex(0)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData('in_app'))
        self.auto_sync.setChecked(True)
        self.sync_interval.setValue(1)
        self.duration_presets.setText(', '.join(format_duration(timedelta(minutes=value))
                                               for value in Preferences().duration_presets_minutes))
        self.appearance_editor.set_preferences(Preferences())
        self.shortcut_editor.restore_defaults()
        self.error.clear()

    def apply(self):
        try:
            preferences = self.value()
        except ValueError as error:
            try:
                self.duration_values()
            except ValueError:
                self.set_category('Events')
                self.duration_presets.setFocus()
            else:
                self.tabs.setCurrentIndex(1)
                target = self.appearance_editor.focus_invalid_field()
                if target is not None:
                    self.tabs.widget(1).ensureWidgetVisible(target)
            self.error.setText(str(error))
            return False
        try:
            bindings = self.shortcut_editor.value()
        except ValueError as error:
            self.tabs.setCurrentWidget(self.shortcut_editor)
            self.shortcut_editor.error.setText(str(error))
            self.error.setText(str(error))
            return False
        self.result_preferences, self.result_bindings = preferences, bindings
        self.error.clear()
        self.shortcut_editor.error.clear()
        if (preferences, bindings) != self._applied_state:
            self._applied_state = preferences, dict(bindings)
            self.applied.emit(preferences, dict(bindings))
            self.setLocale(preferences.qt_locale())
            self.setStyleSheet(stylesheet(preferences.font_scale, preferences))
            self.setPalette(widget_palette(preferences))
            self.error.setStyleSheet('color: ' + scheme_for(preferences).error + ';')
        return True

    def save(self):
        if self.apply():
            self.accept()
