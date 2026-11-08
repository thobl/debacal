"""Categorized display, synchronization, and keyboard preferences."""
from datetime import date, datetime, timedelta
from dataclasses import replace

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout,
    QCheckBox, QFrame, QHBoxLayout, QListWidget, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QWidget, QLineEdit,
)

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .preferences import Preferences, _system_language_locale
from .appearance_preferences import AppearanceEditor
from .shortcuts import KeyboardShortcutsEditor
from .theme import scheme_for
from .date_time_fields import parse_duration, format_duration
from .buttons import action_button
from .compact_controls import ElidedLabel
from .dialog_base import dialog_buttons, install_dialog_keyboard


class PreferencesDialog(QDialog):
    applied = Signal(object, object)
    calendars_changed = Signal()
    credential_requested = Signal(str, str, bool)

    def __init__(self, preferences: Preferences, parent=None, bindings=None, default_bindings=None,
                 *, store=None, credentials=None, executor=None, allow_remote=True):
        super().__init__(parent)
        install_button_focus(QApplication.instance())
        self.setPalette(widget_palette(preferences))
        self._set_style(preferences)
        self.setLocale(preferences.qt_locale())
        self.setWindowTitle('Preferences')
        bounds = self.screen().availableGeometry()
        self.resize(min(1040, max(760, bounds.width() - 48)), min(820, max(540, bounds.height() - 48)))
        self.result_preferences = preferences
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

        _, appearance_layout, _ = self.category('Appearance', scrollable=False)
        self.appearance_editor = AppearanceEditor(preferences)
        appearance_layout.addWidget(self.appearance_editor)
        for name in ('appearance_mode', 'color_scheme', 'custom_accent', 'custom_surface', 'custom_weekend',
                     'control_style', 'agenda_style', 'weekend_style', 'font_scale', 'font_family',
                     'show_sidebar_title', 'sidebar_title', 'corner_style', 'year_view_style'):
            setattr(self, name, getattr(self.appearance_editor, name))

        source_page, source_layout, _ = self.category('Calendar sources')
        self.source_editor = None
        store = store or getattr(parent, 'store', None)
        if store is not None:
            from .calendar_sources import CalendarSourcesEditor
            self.source_editor = CalendarSourcesEditor(store, preferences, self, credentials=credentials,
                                                       executor=executor, allow_remote=allow_remote)
            self.source_editor.changed.connect(self.calendars_changed.emit)
            self.source_editor.credential_requested.connect(self.credential_requested.emit)
            self.finished.connect(self.source_editor.finish)
            source_layout.addWidget(self.source_editor)
        else:
            source_page.setToolTip('Calendar sources are managed from an open calendar workspace.')
            source_layout.addStretch()

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
        self.conflict_notifications.setToolTip('Conflicts are always available in the app. Desktop alerts also follow '
                                              'your operating system’s notification settings.')
        sync_layout.addStretch()

        self.shortcut_editor = KeyboardShortcutsEditor(bindings, default_bindings)
        self.shortcut_fields = self.shortcut_editor.fields
        self.result_bindings = dict(self.shortcut_editor.initial_bindings)
        self._applied_state = preferences, dict(self.result_bindings)
        _, shortcut_layout, _ = self.category('Keyboard shortcuts', scrollable=False)
        shortcut_layout.addWidget(self.shortcut_editor)
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
        self.page_heading.setText(self.categories.item(index).text())
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
                border-left: 3px solid {colors.accent}; font-weight: 600; }}
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
        return Preferences(start_weekday=self.week_start.currentData(), date_format=self.date_format.currentData(),
                           time_format=self.time_format.currentData(), conflict_notifications=self.conflict_notifications.currentData(),
                           auto_sync_enabled=self.auto_sync.isChecked(), sync_interval_minutes=self.sync_interval.value(),
                           locale_name=self.locale.currentData(), duration_presets_minutes=self.duration_values(),
                           default_event_duration_minutes=self.default_duration_minutes(),
                           show_week_numbers=self.week_numbers.isChecked(),
                           visible_start_hour=self.visible_start.value(), visible_end_hour=self.visible_end.value(),
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
        self.appearance_editor.set_context(preferences)

    def restore_defaults(self):
        for field in (self.locale, self.week_start, self.date_format, self.time_format):
            field.setCurrentIndex(0)
        self.conflict_notifications.setCurrentIndex(self.conflict_notifications.findData('in_app'))
        self.auto_sync.setChecked(True)
        self.sync_interval.setValue(Preferences().sync_interval_minutes)
        self.week_numbers.setChecked(Preferences().show_week_numbers)
        self.visible_start.setValue(Preferences().visible_start_hour)
        self.visible_end.setValue(Preferences().visible_end_hour)
        self.default_duration.setText(format_duration(timedelta(minutes=Preferences().default_event_duration_minutes), elapsed=True))
        self.duration_presets.setText(', '.join(format_duration(timedelta(minutes=value), elapsed=True)
                                               for value in Preferences().duration_presets_minutes))
        self.appearance_editor.set_preferences(Preferences())
        self.update_preview()
        self.shortcut_editor.restore_defaults()
        self.error.clear()

    def apply(self):
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
            bindings = self.shortcut_editor.value()
        except ValueError as error:
            self.set_category('Keyboard shortcuts')
            self.shortcut_editor.error.setText(str(error))
            self.error.setText(str(error))
            self.error.show()
            return False
        if self.source_editor is not None:
            try:
                self.source_editor.validate()
                self.source_editor.apply()
            except ValueError as error:
                self.set_category('sources')
                self.error.setText(str(error))
                self.error.show()
                return False
        self.result_preferences, self.result_bindings = preferences, bindings
        self.dialog_keyboard.refresh_tooltips()
        self.error.clear()
        self.error.hide()
        self.shortcut_editor.error.clear()
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
            if self.source_editor is not None:
                self.source_editor.set_preferences(preferences)
        return True

    def save(self):
        if self.apply():
            self.accept()
