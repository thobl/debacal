"""Native display settings with a live date, time, and week-start preview."""
from datetime import date, datetime

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QCheckBox, QSpinBox

from .preferences import Preferences


class PreferencesDialog(QDialog):
    def __init__(self, preferences: Preferences, parent=None, calendars=None, default_calendar_id=None):
        super().__init__(parent)
        self.setWindowTitle('Preferences')
        self.resize(490, 360)
        self.result_preferences = preferences
        outer = QVBoxLayout(self)
        form = QFormLayout()
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
        self.conflict_notifications = QComboBox()
        for name, value in [('In the app', 'in_app'), ('Desktop notification', 'desktop'), ('Both', 'both')]:
            self.conflict_notifications.addItem(name, value)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData(preferences.conflict_notifications))
        form.addRow('Start of week', self.week_start)
        form.addRow('Date format', self.date_format)
        form.addRow('Time format', self.time_format)
        form.addRow('Sync conflict alerts', self.conflict_notifications)
        self.auto_sync = QCheckBox('Sync automatically while the app is open')
        self.auto_sync.setChecked(preferences.auto_sync_enabled)
        self.sync_interval = QSpinBox()
        self.sync_interval.setRange(1, 1440)
        self.sync_interval.setSuffix(' minute(s)')
        self.sync_interval.setValue(preferences.sync_interval_minutes)
        self.sync_interval.setEnabled(preferences.auto_sync_enabled)
        self.auto_sync.toggled.connect(self.sync_interval.setEnabled)
        form.addRow('Automatic syncing', self.auto_sync)
        form.addRow('Sync every', self.sync_interval)
        self.default_calendar = None
        self.result_default_calendar_id = default_calendar_id
        if calendars is not None:
            self.default_calendar = QComboBox()
            for calendar in calendars:
                if calendar.kind != 'ics' and not calendar.read_only:
                    self.default_calendar.addItem(calendar.name, calendar.id)
            index = self.default_calendar.findData(default_calendar_id)
            if index >= 0:
                self.default_calendar.setCurrentIndex(index)
            form.addRow('Default event calendar', self.default_calendar)
        outer.addLayout(form)
        note = QLabel('Conflicts can always be reviewed in the app. Desktop alerts use your operating system’s notification settings.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        outer.addWidget(note)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setProperty('muted', True)
        outer.addWidget(self.preview)
        outer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                  | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        outer.addWidget(buttons)
        for field in (self.week_start, self.date_format, self.time_format):
            field.currentIndexChanged.connect(self.update_preview)
        self.update_preview()

    def value(self):
        return Preferences(self.week_start.currentData(), self.date_format.currentData(), self.time_format.currentData(),
                           self.conflict_notifications.currentData(), self.auto_sync.isChecked(), self.sync_interval.value())

    def update_preview(self, *_):
        preferences = self.value()
        self.preview.setText(preferences.format_date(date(2026, 11, 4), weekday=True) + ' · '
                             + preferences.format_time(datetime(2026, 11, 4, 14, 5)) + '\nWeek begins '
                             + preferences.weekday_name(preferences.first_weekday, short=False) + '.')

    def restore_defaults(self):
        for field in (self.week_start, self.date_format, self.time_format):
            field.setCurrentIndex(0)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData('in_app'))
        self.auto_sync.setChecked(True)
        self.sync_interval.setValue(1)

    def save(self):
        self.result_preferences = self.value()
        if self.default_calendar:
            self.result_default_calendar_id = self.default_calendar.currentData()
        self.accept()
