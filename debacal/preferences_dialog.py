"""Categorized display, synchronization, and keyboard preferences."""
from datetime import date, datetime, timedelta
from dataclasses import replace
import sqlite3

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
    QCheckBox, QFrame, QHBoxLayout, QListWidget, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QWidget, QLineEdit,
)

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .reminder_preferences import ReminderPreferences, save_reminder_draft
from .timeutil import now
from .preferences import Preferences, _system_language_locale
from .appearance_preferences import AppearanceEditor
from .shortcuts import KeyboardShortcutsEditor, normalize_bindings, validate_bindings
from .theme import scheme_for
from .date_time_fields import parse_duration, format_duration
from .buttons import action_button
from .compact_controls import ElidedLabel
from .dialog_base import dialog_buttons, install_dialog_keyboard


class PreferencesDialog(QDialog):
    applied = Signal(object, object)
    remindersApplied = Signal(object)
    reminderTestRequested = Signal(object)
    calendars_changed = Signal()
    credential_requested = Signal(str, str, bool)

    def __init__(self, preferences: Preferences, parent=None, bindings=None, default_bindings=None,
                 *, store=None, credentials=None, executor=None, allow_remote=True, reminder_preferences=None,
                 reminder_clock=now):
        super().__init__(parent)
        install_button_focus(QApplication.instance())
        self.setPalette(widget_palette(preferences))
        self._set_style(preferences)
        self.setLocale(preferences.qt_locale())
        self.setWindowTitle('Preferences')
        bounds = self.screen().availableGeometry()
        self.resize(min(1040, max(760, bounds.width() - 48)), min(820, max(540, bounds.height() - 48)))
        self.result_preferences = preferences
        self._applying = False
        self._draft_preferences = preferences
        self._appearance_editor = self._source_editor = self._shortcut_editor = None
        self._source_store = store or getattr(parent, 'store', None)
        self._source_options = dict(credentials=credentials, executor=executor, allow_remote=allow_remote)
        self._sources_finished = False
        self.result_reminders = self._draft_reminders = reminder_preferences or ReminderPreferences()
        self._reminder_clock = reminder_clock
        self.notification_page = None
        self._desktop_available = self._tray_available = None
        self._shortcut_defaults = normalize_bindings(default_bindings if default_bindings is not None else bindings)
        self.result_bindings = self._shortcut_defaults | normalize_bindings(bindings)
        self._draft_bindings = dict(self.result_bindings)
        self._applied_state = preferences, dict(self.result_bindings)
        self._lazy_pages = {}
        self._initialized_categories = {'calendar'}
        self.finished.connect(self.finish_sources)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName('preferencesSidebar')
        self.sidebar.setFixedWidth(round(190 * preferences.font_scale / 100))
        navigation = QVBoxLayout(self.sidebar)
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.setSpacing(0)
        title = self.navigation_title = ElidedLabel('Preferences')
        title.setObjectName('brand')
        title.setContentsMargins(18, 18, 18, 18)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip('Preferences')
        navigation.addWidget(title)
        self.categories = QListWidget()
        self.categories.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.categories.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.categories.setObjectName('preferencesCategories')
        self.categories.setAccessibleName('Preferences categories')
        navigation.addWidget(self.categories, 1)
        outer.addWidget(self.sidebar)
        right = QWidget()
        right.setObjectName('preferencesContent')
        content = QVBoxLayout(right)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.page_heading = QLabel()
        self.page_heading.setObjectName('heading')
        self.page_heading.setContentsMargins(22, 18, 22, 14)
        content.addWidget(self.page_heading)
        separator = QFrame()
        separator.setObjectName('sourceDivider')
        separator.setFixedHeight(1)
        content.addWidget(separator)
        self.pages = QStackedWidget()
        self.pages.setMinimumSize(0, 0)
        self.pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        content.addWidget(self.pages, 1)
        outer.addWidget(right, 1)
        self.categories.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.pages.currentChanged.connect(self._page_changed)

        self.calendar_page, calendar_layout, form = self.category('Calendar')
        self._calendar_forms = [form]
        formatting_heading = QLabel('Date and time formatting')
        formatting_heading.setObjectName('sidebarSectionHeading')
        calendar_layout.insertWidget(0, formatting_heading)
        self.locale = QComboBox()
        for name, value in [('System default', 'system'), ('English (United Kingdom)', 'en_GB'), ('Deutsch (Deutschland)', 'de_DE')]:
            self.locale.addItem(name, value)
        self.locale.setCurrentIndex(self.locale.findData(preferences.locale_name))
        form.addRow('Language for dates', self.locale)
        language = _system_language_locale()
        language_name = QLocale.languageToString(language.language())
        self.locale.setToolTip('Changes month and weekday names independently of date/time format and start of week. '
                               'Interface labels remain in English.\nSystem language: ' + language_name
                               + '. Regional defaults: ' + Preferences().format_date(date(2026, 11, 4)) + ' · '
                               + Preferences().format_time(datetime(2026, 11, 4, 14, 5)) + '.\n'
                               'On Linux, LANGUAGE/LC_MESSAGES/LANG determine language and LC_TIME determines '
                               'regional dates and times; LC_ALL overrides locale categories.')
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
        form.addRow('Date format', self.date_format)
        form.addRow('Time format', self.time_format)
        form = self.calendar_section(calendar_layout, 'Week and visible hours')
        form.addRow('Start of week', self.week_start)
        self.week_numbers = QCheckBox('Show week numbers')
        self.week_numbers.setChecked(preferences.show_week_numbers)
        form.addRow(self.week_numbers)
        self.visible_start = QSpinBox()
        self.visible_start.setRange(0, 23)
        self.visible_start.setAccessibleName('Start of visible hours')
        self.visible_start.setSuffix(':00')
        self.visible_start.setValue(preferences.visible_start_hour)
        self.visible_end = QSpinBox()
        self.visible_end.setRange(1, 24)
        self.visible_end.setAccessibleName('End of visible hours')
        self.visible_end.setSuffix(':00')
        self.visible_end.setValue(preferences.visible_end_hour)
        hours = QHBoxLayout()
        hours.addWidget(self.visible_start)
        hours.addWidget(QLabel('to'))
        hours.addWidget(self.visible_end)
        hours.addStretch(1)
        form.addRow('Visible hours', hours)
        self.visible_start.setToolTip('The grid also includes events outside these hours. Full day shows all 24 hours.')
        self.visible_end.setToolTip(self.visible_start.toolTip())

        form = self.calendar_section(calendar_layout, 'Event durations')
        self.default_duration = QLineEdit(format_duration(
            timedelta(minutes=preferences.default_event_duration_minutes), elapsed=True))
        self.default_duration.setAccessibleName('Default event duration')
        self.default_duration.setMaximumWidth(round(180 * preferences.font_scale / 100))
        self.default_duration.setPlaceholderText('1h')
        self.default_duration.setToolTip('Used for a new timed event when no end is chosen. Dragged ranges keep their '
                                        'chosen duration; all-day events default to one day.')
        form.addRow('Default duration', self.default_duration)
        self.duration_presets = QLineEdit(', '.join(format_duration(timedelta(minutes=value), elapsed=True)
                                                   for value in preferences.duration_presets_minutes))
        self.duration_presets.setAccessibleName('Duration buttons')
        self.duration_presets.setPlaceholderText('30min, 45min, 1h')
        self.duration_presets.setToolTip('Separate the duration buttons with commas, for example 30min, 45min, 1h. '
                                        'Choosing a button while editing changes that event’s duration.')
        form.addRow('Duration buttons', self.duration_presets)
        calendar_layout.addStretch()

        for name, scrollable in (('Appearance', False), ('Calendar sources', True),
                                 ('Notifications', True), ('Sync', True), ('Keyboard shortcuts', False)):
            self._lazy_pages[name.casefold()] = self.category(name, scrollable=scrollable)
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(22, 10, 22, 14)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: ' + scheme_for(preferences).error + ';')
        self.error.hide()
        footer_layout.addWidget(self.error)
        buttons = self.buttons = dialog_buttons(apply=True)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        reset = QWidget()
        reset_layout = QHBoxLayout(reset)
        reset_layout.setContentsMargins(14, 10, 14, 14)
        self.restore_button = action_button('Defaults', self.restore_defaults,
            tooltip='Restore preference and shortcut defaults. Calendar connections are kept.')
        reset_layout.addWidget(self.restore_button)
        navigation.addWidget(reset)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        footer_layout.addWidget(buttons)
        content.addWidget(footer)
        for field in (self.week_start, self.date_format, self.time_format):
            field.currentIndexChanged.connect(self.update_preview)
        self.week_numbers.toggled.connect(self.update_preview)
        self.visible_start.valueChanged.connect(self.update_preview)
        self.visible_end.valueChanged.connect(self.update_preview)
        self.locale.currentIndexChanged.connect(self.locale_changed)
        self.categories.setCurrentRow(0)
        self._page_changed(self.pages.currentIndex())
        self._align_calendar_labels()
        self.update_preview()
        install_dialog_keyboard(self, self.save)

    def _ensure_category(self, name):
        if name in self._initialized_categories:
            return
        self._initialized_categories.add(name)
        page, layout, form = self._lazy_pages[name]
        if name == 'appearance':
            self._appearance_editor = AppearanceEditor(self._draft_preferences, defer_font_samples=True)
            layout.addWidget(self._appearance_editor)
            self._appearance_editor.finish_font_samples()
            self.update_preview()
        elif name == 'calendar sources':
            if self._source_store is None:
                page.setToolTip('Calendar sources are managed from an open calendar workspace.')
                layout.addStretch()
            else:
                from .calendar_sources import CalendarSourcesEditor
                self._source_editor = CalendarSourcesEditor(self._source_store, self._draft_preferences, self,
                                                            reminder_policies=self._draft_reminders.calendars,
                                                            **self._source_options)
                self._source_editor.changed.connect(self.calendars_changed.emit)
                self._source_editor.credential_requested.connect(self.credential_requested.emit)
                layout.addWidget(self._source_editor)
        elif name == 'notifications':
            from .notification_preferences import NotificationPreferencesPage
            self.notification_page = NotificationPreferencesPage(self._draft_reminders, self)
            self.notification_page.set_availability(self._desktop_available, self._tray_available)
            self.notification_page.testRequested.connect(self._test_reminder)
            layout.addWidget(self.notification_page)
            layout.addStretch()
        elif name == 'keyboard shortcuts':
            self._shortcut_editor = KeyboardShortcutsEditor(self._draft_bindings, self._shortcut_defaults)
            layout.addWidget(self._shortcut_editor)
        elif name == 'sync':
            preferences = self._draft_preferences
            self._auto_sync = QCheckBox('Sync automatically while the app is open')
            self._auto_sync.setChecked(preferences.auto_sync_enabled)
            self._sync_interval = QSpinBox()
            self._sync_interval.setRange(1, 1440)
            self._sync_interval.setSuffix(' minute(s)')
            self._sync_interval.setValue(preferences.sync_interval_minutes)
            self._sync_interval.setEnabled(preferences.auto_sync_enabled)
            self._auto_sync.toggled.connect(self._sync_interval.setEnabled)
            self._conflict_notifications = QComboBox()
            for label, value in [('In the app', 'in_app'), ('Desktop notification', 'desktop'), ('Both', 'both')]:
                self._conflict_notifications.addItem(label, value)
            self._conflict_notifications.setCurrentIndex(
                self._conflict_notifications.findData(preferences.conflict_notifications))
            form.addRow('Automatic syncing', self._auto_sync)
            form.addRow('Sync every', self._sync_interval)
            form.addRow('Conflict alerts', self._conflict_notifications)
            self._conflict_notifications.setToolTip('Conflicts are always available in the app. Desktop alerts also follow '
                                                   'your operating system’s notification settings.')
            layout.addStretch()

    @property
    def appearance_editor(self):
        self._ensure_category('appearance')
        return self._appearance_editor

    @property
    def source_editor(self):
        self._ensure_category('calendar sources')
        return self._source_editor

    @source_editor.setter
    def source_editor(self, editor):
        self._source_editor = editor
        self._initialized_categories.add('calendar sources')

    @property
    def shortcut_editor(self):
        self._ensure_category('keyboard shortcuts')
        return self._shortcut_editor

    @property
    def shortcut_fields(self):
        return self.shortcut_editor.fields

    def finish_sources(self, *_):
        """Finish only an initialized source panel, once per dialog lifetime."""
        if self._source_editor is not None and not self._sources_finished:
            self._sources_finished = True
            finish = getattr(self._source_editor, 'finish', None)
            if finish is not None:
                finish()

    def category(self, name, *, scrollable=True):
        page = QWidget()
        page.setObjectName('preferencesPage')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 16, 22, 16)
        form = QFormLayout()
        layout.addLayout(form)
        if scrollable:
            scroll = QScrollArea()
            scroll.setObjectName('preferences' + name)
            scroll.setAccessibleName(name + ' preferences')
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        else:
            self.pages.addWidget(page)
        self.categories.addItem(name)
        self.categories.item(self.categories.count() - 1).setToolTip(name)
        return page, layout, form

    def calendar_section(self, layout, title):
        separator = QFrame()
        separator.setObjectName('sourceDivider')
        separator.setFixedHeight(1)
        layout.addSpacing(8)
        layout.addWidget(separator)
        heading = QLabel(title)
        heading.setObjectName('sidebarSectionHeading')
        layout.addWidget(heading)
        form = QFormLayout()
        layout.addLayout(form)
        self._calendar_forms.append(form)
        return form

    def _align_calendar_labels(self):
        if not hasattr(self, '_calendar_forms'):
            return
        labels = [form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
                  for form in self._calendar_forms for row in range(form.rowCount())
                  if form.itemAt(row, QFormLayout.ItemRole.LabelRole) is not None
                  and form.itemAt(row, QFormLayout.ItemRole.FieldRole) is not None]
        for label in labels:
            label.ensurePolished()
        width = max((label.fontMetrics().horizontalAdvance(label.text()) for label in labels), default=0)
        for label in labels:
            label.setFixedWidth(width + 4)

    def resizeEvent(self, event):
        if hasattr(self, 'sidebar'):
            self._resize_sidebar(event.size().width())
        super().resizeEvent(event)

    def _resize_sidebar(self, width=None):
        desired = round(190 * self.result_preferences.font_scale / 100)
        self.sidebar.setFixedWidth(min(desired, max(146, (width or self.width()) // 3)))

    def _page_changed(self, index):
        if index < 0 or index >= self.categories.count():
            return
        self.categories.setCurrentRow(index)
        heading = self.categories.item(index).text()
        self._ensure_category(heading.casefold())
        self.page_heading.setText(heading)
        for row in range(self.categories.count()):
            item = self.categories.item(row)
            font = item.font()
            font.setBold(row == index)
            font.setUnderline(row == index)
            item.setFont(font)

    def _set_style(self, preferences):
        colors = scheme_for(preferences)
        style = stylesheet(preferences.font_scale, preferences) + f'''
            QWidget#preferencesSidebar, QListWidget#preferencesCategories {{ background: {colors.sidebar}; }}
            QWidget#preferencesSidebar {{ border-right: 1px solid {colors.line}; }}
            QListWidget#preferencesCategories {{ border: none; border-radius: 0; outline: 0; }}
            QListWidget#preferencesCategories::item {{ padding: 11px 14px; border-left: 3px solid transparent; }}
            QListWidget#preferencesCategories::item:selected {{ background: {colors.selection}; color: {colors.ink};
                border-left: 3px solid {colors.accent}; font-weight: bold; }}
            QListWidget#preferencesCategories::item:hover:!selected {{ background: {colors.hover}; }}
        '''
        if style != self.styleSheet():
            self.setStyleSheet(style)
            self._align_calendar_labels()

    def set_category(self, category):
        name = str(category).strip().casefold()
        name = {'keyboard': 'keyboard shortcuts', 'sources': 'calendar sources',
                'general': 'calendar', 'events': 'calendar'}.get(name, name)
        for index in range(self.categories.count()):
            if self.categories.item(index).text().casefold() == name:
                self.categories.setCurrentRow(index)
                return True
        return False

    def duration_values(self):
        values = [parse_duration(part) for part in self.duration_presets.text().split(',')]
        if any(value % timedelta(minutes=1) for value in values):
            raise ValueError('Duration buttons must use whole minutes.')
        minutes = tuple(int(value.total_seconds() // 60) for value in values)
        return Preferences(duration_presets_minutes=minutes).duration_presets_minutes

    def default_duration_minutes(self):
        value = parse_duration(self.default_duration.text())
        if value % timedelta(minutes=1):
            raise ValueError('Default event duration must use whole minutes.')
        minutes = int(value.total_seconds() // 60)
        return Preferences(default_event_duration_minutes=minutes).default_event_duration_minutes

    def value(self):
        values = dict(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                      time_format=self.time_format.currentData(), locale_name=self.locale.currentData(),
                      duration_presets_minutes=self.duration_values(),
                      default_event_duration_minutes=self.default_duration_minutes(),
                      show_week_numbers=self.week_numbers.isChecked(),
                      visible_start_hour=self.visible_start.value(), visible_end_hour=self.visible_end.value())
        if self._appearance_editor is not None:
            values.update(self._appearance_editor.values())
        if 'sync' in self._initialized_categories:
            values.update(conflict_notifications=self._conflict_notifications.currentData(),
                          auto_sync_enabled=self._auto_sync.isChecked(), sync_interval_minutes=self._sync_interval.value())
        return replace(self._draft_preferences, **values)

    def locale_changed(self, *_):
        preferences = Preferences(locale_name=self.locale.currentData())
        for day in range(7):
            self.week_start.setItemText(day + 1, preferences.weekday_name(day, short=False))
        self.update_preview()

    def update_preview(self, *_):
        preferences = Preferences(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                                  time_format=self.time_format.currentData(), locale_name=self.locale.currentData())
        day = date(2026, 11, 4)
        sample = (preferences.weekday_name(day, short=False) + ', ' + preferences.format_date(day) + ' · '
                  + preferences.format_time(datetime(2026, 11, 4, 14, 5)) + '\nWeek begins '
                  + preferences.weekday_name(preferences.first_weekday, short=False) + '.')
        self.date_format.setToolTip(sample)
        self.time_format.setToolTip(sample)
        self.week_start.setToolTip('Week begins ' + preferences.weekday_name(preferences.first_weekday, short=False) + '.')
        try:
            preferences = replace(preferences, show_week_numbers=self.week_numbers.isChecked(),
                                  visible_start_hour=self.visible_start.value(), visible_end_hour=self.visible_end.value())
        except ValueError:
            pass  # Date formatting remains usable while a range is incomplete.
        if self._appearance_editor is not None:
            self._appearance_editor.set_context(preferences)

    def set_reminder_availability(self, desktop, tray):
        self._desktop_available, self._tray_available = desktop, tray
        if self.notification_page:
            self.notification_page.set_availability(desktop, tray)

    def reminder_draft(self):
        value = self.notification_page.value() if self.notification_page else self._draft_reminders
        policies = getattr(self._source_editor, 'reminder_policies', value.calendars)
        return replace(value, calendars=policies)

    def _test_reminder(self):
        self.reminderTestRequested.emit(self.reminder_draft())

    def show_test_reminder(self, due):
        if self.notification_page:
            self.notification_page.test_result.setText(due.title)
            self.notification_page.test_result.show()

    def restore_defaults(self):
        self._draft_reminders = ReminderPreferences(calendars=self.result_reminders.calendars)
        if self.notification_page:
            self.notification_page.set_value(self._draft_reminders)
        defaults = Preferences()
        self._draft_preferences = defaults
        for field in (self.locale, self.week_start, self.date_format, self.time_format):
            field.setCurrentIndex(0)
        if 'sync' in self._initialized_categories:
            self._conflict_notifications.setCurrentIndex(self._conflict_notifications.findData('in_app'))
            self._auto_sync.setChecked(True)
            self._sync_interval.setValue(defaults.sync_interval_minutes)
        self.week_numbers.setChecked(Preferences().show_week_numbers)
        self.visible_start.setValue(Preferences().visible_start_hour)
        self.visible_end.setValue(Preferences().visible_end_hour)
        self.default_duration.setText(format_duration(timedelta(minutes=Preferences().default_event_duration_minutes), elapsed=True))
        self.duration_presets.setText(', '.join(format_duration(timedelta(minutes=value), elapsed=True)
                                               for value in Preferences().duration_presets_minutes))
        if self._appearance_editor is not None:
            self._appearance_editor.set_preferences(defaults)
        self.update_preview()
        self._draft_bindings = dict(self._shortcut_defaults)
        if self._shortcut_editor is not None:
            self._shortcut_editor.restore_defaults()
        self.error.clear()

    def apply(self, *, close_after=False):
        if self._applying:
            return False
        try:
            preferences = self.value()
        except ValueError as error:
            invalid_duration = None
            try:
                self.default_duration_minutes()
            except ValueError:
                invalid_duration = self.default_duration
            if invalid_duration is None:
                try:
                    self.duration_values()
                except ValueError:
                    invalid_duration = self.duration_presets
            if invalid_duration is not None:
                self.set_category('Calendar')
                invalid_duration.setFocus()
                self.pages.currentWidget().ensureWidgetVisible(invalid_duration)
            else:
                if self.visible_start.value() >= self.visible_end.value():
                    self.set_category('Calendar')
                    self.visible_end.setFocus()
                    self.pages.currentWidget().ensureWidgetVisible(self.visible_end)
                else:
                    self.set_category('Appearance')
                    target = self.appearance_editor.focus_invalid_field()
                    if target is not None:
                        self.appearance_editor.controls_scroll.ensureWidgetVisible(target)
            self.error.setText(str(error))
            self.error.show()
            return False
        try:
            bindings = (self._shortcut_editor.value() if self._shortcut_editor is not None
                        else validate_bindings(self._draft_bindings))
        except ValueError as error:
            self.set_category('Keyboard shortcuts')
            self.shortcut_editor.error.setText(str(error))
            self.error.setText(str(error))
            self.error.show()
            return False
        bindings = dict(bindings)
        try:
            reminders = self.reminder_draft()
        except ValueError as error:
            self.set_category('Notifications')
            self.error.setText(str(error))
            self.error.show()
            return False
        completed = False

        original = self.result_reminders
        value = reminders
        persist = None
        if reminders != original and self._source_store:
            when = self._reminder_clock()
            def persist(writer):
                nonlocal value
                value = save_reminder_draft(writer, original, reminders, now=when)

        def saved(_changed=False):
            nonlocal completed
            self._set_applying(False)
            if reminders != original:
                self._draft_reminders = self.result_reminders = value
                if self.notification_page:
                    self.notification_page.set_value(value)
                if self._source_editor is not None:
                    self._source_editor.reminder_policies = dict(value.calendars)
                    self._source_editor._selection_changed(self._source_editor.sources.currentItem())
                self.remindersApplied.emit(value)
            self._finish_apply(preferences, bindings)
            completed = True
            if close_after:
                self.accept()

        def failed(error):
            self._set_applying(False)
            self.set_category('sources' if self._source_editor is not None else 'Notifications')
            self.error.setText(str(error))
            self.error.show()

        # Source drafts commonly complete synchronously without a write.
        # Guard reentrancy immediately, but only disable the large control
        # tree if a source operation remains pending after submission.
        self._applying = True
        try:
            if self._source_editor is not None:
                options = {'save_reminders': persist} if persist else {}
                self._source_editor.apply(on_success=saved, on_error=failed, **options)
                if self._applying:
                    self._set_applying(True)
            else:
                if persist:
                    persist(self._source_store)
                saved()
        except (ValueError, sqlite3.Error, OSError) as error:
            failed(error)
        return completed

    def _set_applying(self, applying):
        self._applying = applying
        for control in (self.buttons, self.pages, self.categories, self.restore_button):
            control.setEnabled(not applying)
        self.save_button.setText('Saving…' if applying else 'Save')

    def _finish_apply(self, preferences, bindings):
        self.result_preferences, self.result_bindings = preferences, bindings
        self._draft_preferences, self._draft_bindings = preferences, dict(bindings)
        self.dialog_keyboard.refresh_tooltips()
        self.error.clear()
        self.error.hide()
        if self._shortcut_editor is not None:
            self._shortcut_editor.error.clear()
        if (preferences, bindings) != self._applied_state:
            self._applied_state = preferences, dict(bindings)
            self.applied.emit(preferences, dict(bindings))
            self.setLocale(preferences.qt_locale())
            self._set_style(preferences)
            self._resize_sidebar()
            palette = widget_palette(preferences)
            if palette != self.palette():
                self.setPalette(palette)
            self.error.setStyleSheet('color: ' + scheme_for(preferences).error + ';')
            if self._source_editor is not None:
                self._source_editor.set_preferences(preferences)

    def save(self):
        self.apply(close_after=True)

    def accept(self):
        if not self._applying:
            super().accept()

    def reject(self):
        if not self._applying:
            super().reject()

    def closeEvent(self, event):
        if self._applying:
            event.ignore()
        else:
            super().closeEvent(event)


def _appearance_field(name):
    return property(lambda dialog: getattr(dialog.appearance_editor, name))


for _name in ('appearance_mode', 'color_scheme', 'custom_accent', 'custom_surface', 'custom_weekend',
              'control_style', 'agenda_style', 'weekend_style', 'font_scale', 'font_family',
              'show_sidebar_title', 'sidebar_title', 'corner_style', 'year_view_style'):
    setattr(PreferencesDialog, _name, _appearance_field(_name))


def _sync_field(name):
    def field(dialog):
        dialog._ensure_category('sync')
        return getattr(dialog, '_' + name)
    return property(field)


for _name in ('auto_sync', 'sync_interval', 'conflict_notifications'):
    setattr(PreferencesDialog, _name, _sync_field(_name))
