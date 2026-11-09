"""Event authoring: a primary range, a recurrence draft, and a live month."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .buttons import action_button, icon_button
from .calendar_choice import CalendarChoice
from .choice_control import ChoiceControl
from .date_time_fields import (DateTimeField, format_duration, format_event_duration,
                               parse_event_duration, validate_datetime)
from .date_time_picker import DateTimeRangePicker, range_duration, relocate_range, validate_range_order
from .event_duration import add_duration, add_elapsed, end_for, normalized_spec
from .dialog_base import FormDialog, dialog_buttons
from .event_schedule import ScheduleDraft, count_to_until, until_to_count
from .event_scope import DEFAULT_MODE_BINDINGS, MODE_ACTIONS
from .models import Event
from .timezones import timezone_identifier
from .preferences import Preferences
from .patterns import PatternError, compile_sequence, relative_sequence_steps, sequence_tail
from .range_policies import occupied_span, range_policy_options, shift_range_fits, static_range_policy_fits
from .recurrence import (ScheduleReplacement, canonicalize_authored_primary, event_zone, key_datetime,
                         occurrence_id, recurrence_key, recurrence_set, updated_series)
from .series_preview import SeriesPreview
from .theme import scheme_for
from .timeutil import local_zone
from .shortcuts import shortcut_keys, shortcut_label, shortcut_sequences


REGULAR_KINDS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')


class RangeFields(QWidget):
    """Compact inline endpoint labels, stacked when the text needs more room."""
    def __init__(self, start, end, preferences):
        super().__init__()
        self.preferences = preferences
        self.fields = start, end
        self.labels = []
        self._arranging = False
        self.row = QGridLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setHorizontalSpacing(8)
        self.row.setVerticalSpacing(8)
        for caption, field in (('Start', start), ('End', end)):
            label = QLabel(caption)
            label.setBuddy(field)
            self.labels.append(label)
            field.lineEdit().textChanged.connect(self._arrange)
        self._arrange()

    def _arrange(self, *_):
        if self._arranging:
            return
        self._arranging = True
        try:
            scale = self.preferences.font_scale / 100
            widths = []
            for field in self.fields:
                field.ensurePolished()
                caption = self.preferences.format_date(date(2026, 11, 28))
                if not field.isAllDay():
                    caption += ' ' + self.preferences.format_time(time(23, 55))
                width = (field.lineEdit().fontMetrics().horizontalAdvance(caption)
                         + round(18 * scale) + field.button.width() + 2)
                width = max(round(164 * scale), min(round(210 * scale), width))
                widths.append(width)
                field.setMinimumWidth(width)
                field.setMaximumWidth(16777215)
                field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            for label in self.labels:
                label.ensurePolished()
            needed = sum(widths)
            needed += sum(label.fontMetrics().horizontalAdvance(label.text()) for label in self.labels) + 24
            self._wide = self.width() >= needed
            for field, label in zip(self.fields, self.labels):
                self.row.removeWidget(label)
                self.row.removeWidget(field)
            for column in range(5):
                self.row.setColumnStretch(column, 0)
            for index, (field, label) in enumerate(zip(self.fields, self.labels)):
                label.setMinimumWidth(0)
                label.setMaximumWidth(16777215)
                if self._wide:
                    self.row.addWidget(label, 0, index * 2)
                    self.row.addWidget(field, 0, index * 2 + 1)
                else:
                    label.setFixedWidth(label.fontMetrics().horizontalAdvance('Duration'))
                    self.row.addWidget(label, index, 0)
                    self.row.addWidget(field, index, 1)
            self.row.setColumnStretch(1, 1)
            if self._wide:
                self.row.setColumnStretch(3, 1)
        finally:
            self._arranging = False

    def resizeEvent(self, event):
        self._arrange()
        super().resizeEvent(event)

    def minimumSizeHint(self):
        # A wide arrangement must not force its parent to stay wide before a
        # resize can reflow the fields into their two-row arrangement.
        width = max(field.minimumWidth() for field in self.fields)
        width += max(label.fontMetrics().horizontalAdvance('Duration') for label in self.labels) + 8
        return QSize(width, super().minimumSizeHint().height())


class DurationFields(QWidget):
    """Inline duration entry and presets, with a full preset row when needed."""
    def __init__(self, field, choices):
        super().__init__()
        self.field, self.choices = field, choices
        self.row = QGridLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setHorizontalSpacing(7)
        self.row.setVerticalSpacing(5)
        self.label = QLabel('Duration')
        self.label.setBuddy(field)
        self.row.addWidget(self.label, 0, 0)
        self.row.addWidget(field, 0, 1)
        self.row.setColumnStretch(2, 1)
        self._wide = None
        self._arrange()

    def _arrange(self):
        metrics = self.choices.fontMetrics()
        needed = max((metrics.horizontalAdvance(caption) + 12 for caption, _ in self.choices.options), default=0)
        wide = self.width() >= self.label.sizeHint().width() + self.field.maximumWidth() + 21 + needed * self.choices.count()
        if wide == self._wide:
            return
        self._wide = wide
        self.row.removeWidget(self.choices)
        self.row.addWidget(self.choices, 0 if wide else 1, 2 if wide else 0, 1, 1 if wide else 3)

    def replace_choices(self, control):
        self.row.removeWidget(self.choices)
        self.choices = control
        self._wide = None
        self._arrange()

    def resizeEvent(self, event):
        self._arrange()
        super().resizeEvent(event)


class RegularFields(QWidget):
    """Keep the finite bounds compact, with Indefinite beside Until when roomy."""
    def __init__(self, preferences):
        super().__init__()
        self.preferences = preferences
        self._wide = None

    def configure(self, layout, until_row, indefinite):
        self.grid, self.until_row, self.indefinite = layout, until_row, indefinite
        self._arrange()

    def _arrange(self):
        if not hasattr(self, 'grid'):
            return
        wide = self.width() >= round(400 * self.preferences.font_scale / 100)
        if wide == self._wide:
            return
        self._wide = wide
        if wide:
            self.until_row.removeWidget(self.indefinite)
            self.grid.addWidget(self.indefinite, 1, 3)
        else:
            self.grid.removeWidget(self.indefinite)
            self.until_row.addWidget(self.indefinite)

    def resizeEvent(self, event):
        self._arrange()
        super().resizeEvent(event)


class EventDialog(FormDialog):
    """Keep primary timing independent of preview selection and occurrence edits."""
    mode_changed = Signal(str)

    def __init__(self, calendars, start, end=None, event=None, parent=None,
                 preferences=None, edit_scope='single', series=None, default_calendar_id=None,
                 scope_bindings=None, common_fields_only=False):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences))
        self.setLocale(self.preferences.qt_locale())
        self.setWindowTitle('Edit event' if event else 'Create event')
        install_button_focus(QApplication.instance())
        self.original, self.series = event, series
        self.calendars = list(calendars)
        self.common_fields_only = bool(common_fields_only)
        self.has_group = bool((series and series.recurring)
                              or (event and (event.series_id or event.group_id)))
        self._scope_available = bool(event and (self.has_group or event.recurring))
        self.scope_bindings = DEFAULT_MODE_BINDINGS | (scope_bindings or {})
        self._new_id = str(uuid4())
        self._loading = True
        self.calendar_change_requested = False
        self._validation_field = None
        self._schedule_error = None
        self._scope_sources, self._scope_values = {}, {}
        self._scope_name = edit_scope if self._scope_available and edit_scope in ('single', 'group', 'future') else 'single'
        self._repeat_draft = None
        self._custom_draft = None
        self._regular_values = None
        self._reset_schedule = False
        self._repeat_reset = False
        self._draft = ScheduleDraft.from_event()
        self._duration = timedelta(minutes=self.preferences.default_event_duration_minutes)
        self._range_needs_repair = False
        self.duration_spec, self.duration_zone = '', local_zone()
        self._last_start, self._last_end = start, end or add_elapsed(start, self._duration)
        self._timed_range = None
        self._timed_duration = None
        self.result_events = []
        self.schedule_replacement = False
        self.range_picker = None
        self.selected_preview_event = None
        scale = self.preferences.font_scale / 100
        available = self.screen().availableGeometry()
        width = min(max(1060, round(1120 * scale)), max(760, available.width() - 48))
        self.resize(width, min(round(700 * min(scale, 1.2)), max(540, available.height() - 48)))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName('eventEditorSidebar')
        self.sidebar.setFixedWidth(max(360, min(round(460 * scale), width // 2)))
        left = QVBoxLayout(self.sidebar)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        self.heading = QLabel('Edit event' if event else 'Create event')
        self.heading.setObjectName('brand')
        self.header = QWidget()
        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(18, 16, 18, 14)
        header_row.setSpacing(10)
        header_row.addWidget(self.heading, 1)
        self.scope_controls = ChoiceControl([('Select one event', 'single'), ('Select all in a series', 'group'),
                                             ('Select this and following events', 'future')], self.preferences, self,
                                           icons={'single': 'single', 'group': 'series', 'future': 'following'}, icon_only=True)
        self.scope_controls.setFixedSize(round(32 * scale) * 3, round(32 * scale))
        self.scope = self.scope_controls
        self.scope_buttons = self.scope.buttons
        self.scope.setCurrentData(self._scope_name)
        header_row.addWidget(self.scope_controls, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        left.addWidget(self.header)
        if self._scope_available:
            self.scope_controls.show()
        else:
            self.scope_controls.hide()
            self.scope_controls.setEnabled(False)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setObjectName('eventEditorScroll')
        self.controls_scroll.viewport().setObjectName('eventEditorSidebarViewport')
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('eventEditorBody')
        sections = QVBoxLayout(body)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(0)
        self.controls_scroll.setWidget(body)
        left.addWidget(self.controls_scroll, 1)
        self.metadata_section, metadata = self._section(sections)
        self.form = QGridLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(8)
        target = event.calendar_id if event else default_calendar_id
        self.calendar = CalendarChoice(self.calendars, self.preferences, selected_id=target)
        self.calendar.setEnabled(not (event and event.read_only))
        if self.common_fields_only:
            self.calendar.setToolTip('Choose a calendar to move all selected events; leave unchanged to keep their calendars.')
        self.title = QLineEdit()
        self.location = QLineEdit()
        for index, (caption, field) in enumerate((('Calendar', self.calendar), ('&Title', self.title), ('&Location', self.location))):
            label = QLabel(caption)
            label.setBuddy(field)
            self.form.addWidget(label, index, 0)
            self.form.addWidget(field, index, 1)
        self.form.setColumnStretch(1, 1)
        metadata.addLayout(self.form)
        self._divider(sections)
        self.time_section, timing = self._section(sections)
        time_heading = QHBoxLayout()
        time_heading.addWidget(self._heading('Date and time'), 1)
        self.all_day = QCheckBox('All day')
        time_heading.addWidget(self.all_day)
        timing.addLayout(time_heading)
        self.start, self.end = DateTimeField(self.preferences), DateTimeField(self.preferences)
        self.start.setAccessibleName('Start date and time')
        self.end.setAccessibleName('End date and time')
        self.range_fields = RangeFields(self.start, self.end, self.preferences)
        timing.addWidget(self.range_fields)
        self.duration = QLineEdit()
        self.duration.setAccessibleName('Duration')
        self.duration.setMaximumWidth(round(110 * scale))
        self.duration.setToolTip('Changing duration adjusts the end. Hours and minutes are elapsed time; '
                                 'whole days and weeks follow calendar dates in the event’s timezone.')
        self.duration_choices = ChoiceControl([('30min', 30), ('45min', 45), ('1h', 60)], self.preferences)
        self.duration_fields = DurationFields(self.duration, self.duration_choices)
        timing.addWidget(self.duration_fields)
        self._divider(sections)
        self.schedule_section, scheduling = self._section(sections)
        scheduling.addWidget(self._heading('Schedule'))
        self.schedule_mode = ChoiceControl([('Single', 'single'), ('Series', 'repeat'), ('Choices', 'alternatives')], self.preferences,
                                          icons={'single': 'single', 'repeat': 'series', 'alternatives': 'alternatives'},
                                          always_show_labels=True)
        self.schedule_mode.buttons['single'].setToolTip('Single event — create one event.')
        self.schedule_mode.buttons['repeat'].setToolTip('Event series — keep every occurrence in the schedule.')
        self.schedule_mode.buttons['alternatives'].setToolTip('Alternative choices — confirming one removes the other choices.')
        self.schedule_mode_row = QHBoxLayout()
        self.schedule_mode_row.setContentsMargins(0, 0, 0, 0)
        self.schedule_mode_row.setSpacing(round(14 * scale))
        self.schedule_mode_row.addWidget(self.schedule_mode, 1)
        self.add_occurrence_button = icon_button('new', 'Add occurrence at another date or time', self.add_occurrence,
                                                preferences=self.preferences)
        self.schedule_mode_row.addWidget(self.add_occurrence_button)
        scheduling.addLayout(self.schedule_mode_row)
        self.repeat = ChoiceControl([('Daily', 'DAILY'), ('Weekly', 'WEEKLY'), ('Monthly', 'MONTHLY'),
                                     ('Yearly', 'YEARLY'), ('Custom', 'RDATE')], self.preferences,
                                    icons={'DAILY': 'day', 'WEEKLY': 'week', 'MONTHLY': 'month', 'YEARLY': 'year', 'RDATE': 'sequence'},
                                    icon_only=True)
        self.repeat.setCurrentData('WEEKLY')
        frequency_row = QHBoxLayout()
        self.frequency_row = frequency_row
        frequency_row.setContentsMargins(0, 0, 0, 0)
        frequency_row.setSpacing(0)
        self.repeat.setFixedWidth(self.repeat.height() * self.repeat.count())
        frequency_row.addWidget(self.repeat)
        frequency_row.addStretch(1)
        self.overflow_policy = ChoiceControl([('Skip', 'skip'), ('Shift', 'shift'), ('Cut', 'cut')], self.preferences,
            icons={'skip': 'overflow_skip', 'shift': 'overflow_shift', 'cut': 'overflow_cut'}, icon_only=True)
        self.overflow_policy.setAccessibleName('Short-month handling')
        self.overflow_policy.setCurrentIndex(-1)
        frequency_row.addWidget(self.overflow_policy)
        scheduling.addLayout(frequency_row)
        self.recurrence_details = QStackedWidget()
        self.recurrence_details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.regular_controls = RegularFields(self.preferences)
        limits = QGridLayout(self.regular_controls)
        limits.setContentsMargins(0, 0, 0, 0)
        limits.setHorizontalSpacing(8)
        limits.setVerticalSpacing(4)
        self.interval = QSpinBox()
        self.interval.setRange(1, 999)
        self.interval.setKeyboardTracking(False)
        self.interval.setMaximumWidth(round(60 * scale))
        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 100000)
        self.repeat_count.setKeyboardTracking(False)
        self.repeat_count.setValue(10)
        self.repeat_count.setMaximumWidth(round(85 * scale))
        self.repeat_count.setToolTip('Total occurrences.')
        self.repeat_until = DateTimeField(self.preferences)
        self.repeat_until.setAllDay(True)
        self.repeat_until.setAccessibleName('Last recurrence date')
        self.repeat_until.setMaximumWidth(round(145 * scale))
        until_heading = QWidget()
        until_row = QHBoxLayout(until_heading)
        until_row.setContentsMargins(0, 0, 0, 0)
        until_row.setSpacing(4)
        until_row.addWidget(QLabel('Until'), 1)
        self.no_end = QCheckBox('Indefinite')
        until_row.addWidget(self.no_end)
        limits.addWidget(QLabel('Every'), 0, 0)
        limits.addWidget(QLabel('Count'), 0, 1)
        limits.addWidget(until_heading, 0, 2)
        limits.addWidget(self.interval, 1, 0)
        limits.addWidget(self.repeat_count, 1, 1)
        limits.addWidget(self.repeat_until, 1, 2)
        limits.setColumnStretch(2, 1)
        self.regular_controls.configure(limits, until_row, self.no_end)
        self.recurrence_details.addWidget(self.regular_controls)
        self.custom_controls = QWidget()
        custom = QVBoxLayout(self.custom_controls)
        custom.setContentsMargins(0, 0, 0, 0)
        custom.setSpacing(4)
        self.pattern = QLineEdit()
        self.pattern.setMaxLength(2147483647)
        self.pattern.setAccessibleName('Sequence')
        self.pattern.setPlaceholderText('+0d 4*+2w +3w 3*+2w')
        self.pattern.setToolTip('Include the reference with +0d. Steps: +2w, 13., 2.3., +1m!. '
                               'Repeat: every(1w), every(1d, count=5), every(1d, until=13.11.2026).')
        self.custom_input = QStackedWidget()
        sequence_page = QWidget()
        sequence_layout = QVBoxLayout(sequence_page)
        sequence_layout.setContentsMargins(0, 0, 0, 0)
        sequence_layout.setSpacing(4)
        sequence_layout.addWidget(QLabel('Sequence'))
        sequence_layout.addWidget(self.pattern)
        self.custom_input.addWidget(sequence_page)
        custom.addWidget(self.custom_input)
        self.recurrence_details.addWidget(self.custom_controls)
        scheduling.addWidget(self.recurrence_details)
        self._divider(sections)
        self.notes_section, notes = self._section(sections)
        notes.addWidget(self._heading('Notes'))
        self.description = QTextEdit()
        self.description.setMinimumHeight(round(96 * scale))
        self.description.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        notes.addWidget(self.description, 1)
        sections.setStretch(sections.indexOf(self.notes_section), 1)
        outer.addWidget(self.sidebar)
        self.preview_column = QWidget()
        self.preview_column.setObjectName('eventEditorPreview')
        right = QVBoxLayout(self.preview_column)
        right.setContentsMargins(18, 16, 18, 12)
        self.preview = SeriesPreview()
        self.preview.month_label.setObjectName('heading')
        right.addWidget(self.preview, 1)
        self._divider(right)
        self.footer = QWidget()
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        footer_layout.setSpacing(6)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('error', True)
        self.error.hide()
        footer_layout.addWidget(self.error)
        self.buttons = dialog_buttons()
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        footer_layout.addWidget(self.buttons)
        right.addWidget(self.footer)
        outer.addWidget(self.preview_column, 1)
        self._connect()
        self._load_scope(self._scope_name, start, end)
        self._loading = False
        self._update_preview()
        self.configure_keyboard([*self.scope_buttons.values(), self.calendar, self.title, self.location,
            self.all_day, self.start.lineEdit(), self.end.lineEdit(), self.duration, *self.schedule_mode.buttons.values(),
            *self.repeat.buttons.values(), self.interval, self.repeat_count, self.repeat_until.lineEdit(),
            *self.overflow_policy.buttons.values(), self.pattern, self.add_occurrence_button, self.description,
            self.preview.previous_button, self.preview.today_button, self.preview.next_button, self.preview.first_button,
            self.preview.previous_occurrence_button, self.preview.next_occurrence_button, self.preview.last_button],
            [self.save_button, self.cancel_button], self.save,
            [self.scope_bindings[action] for action in MODE_ACTIONS if self.scope_bindings.get(action)] if self._scope_available else ())
        self.mode_shortcuts = {}
        for action, scope in MODE_ACTIONS.items():
            binding = self.scope_bindings.get(action, '')
            shortcut = QShortcut(self)
            shortcut.setKeys(shortcut_sequences(binding))
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.setEnabled(bool(self._scope_available and shortcut_keys(binding)))
            shortcut.activated.connect(lambda value=scope: self.scope.setCurrentData(value))
            self.mode_shortcuts[action] = shortcut
            label = shortcut_label(binding)
            caption = self.scope_buttons[scope].accessibleName()
            self.scope_buttons[scope].setToolTip(caption + (' (' + label + ')' if label else ''))
        self.title.setFocus()
        self.title.selectAll()

    def resizeEvent(self, event):
        if hasattr(self, 'sidebar'):
            self.sidebar.setFixedWidth(max(360, min(round(460 * self.preferences.font_scale / 100), event.size().width() // 2)))
        super().resizeEvent(event)

    @staticmethod
    def _heading(caption):
        label = QLabel(caption)
        label.setObjectName('sidebarSectionHeading')
        return label

    @staticmethod
    def _section(parent_layout):
        widget = QWidget()
        widget.setObjectName('eventEditorSection')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(8)
        parent_layout.addWidget(widget)
        return widget, layout

    @staticmethod
    def _divider(layout):
        line = QFrame()
        line.setObjectName('sourceDivider')
        line.setFixedHeight(1)
        layout.addWidget(line)

    def _icon_button(self, name, caption, action):
        return icon_button(name, caption, action, preferences=self.preferences)

    def _connect(self):
        self.scope.currentChanged.connect(self.scope_changed)
        self.start.dateTimeChanged.connect(lambda _: self._start_changed())
        self.end.dateTimeChanged.connect(lambda _: self._end_changed())
        self.start.editingFinished.connect(self._start_changed)
        self.end.editingFinished.connect(self._end_changed)
        self.start.picker_requested.connect(lambda: self.open_range_picker('start'))
        self.end.picker_requested.connect(lambda: self.open_range_picker('end'))
        self.duration.editingFinished.connect(self._duration_changed)
        self.duration_choices.currentChanged.connect(self._preset_changed)
        self.all_day.toggled.connect(self._all_day_changed)
        self.schedule_mode.currentChanged.connect(self._schedule_mode_changed)
        self.repeat.currentChanged.connect(self._frequency_changed)
        self.interval.valueChanged.connect(lambda _: self._regular_changed('count'))
        self.repeat_count.valueChanged.connect(lambda _: self._regular_changed('count'))
        self.repeat_until.editingFinished.connect(lambda: self._regular_changed('until'))
        self.repeat_until.dateTimeChanged.connect(lambda _: self._regular_changed('until'))
        self.repeat_until.picker_requested.connect(self.open_until_picker)
        self.no_end.toggled.connect(lambda _: self._regular_changed('count'))
        self.overflow_policy.currentChanged.connect(self._overflow_policy_changed)
        self.pattern.textChanged.connect(self.pattern_changed)
        self.pattern.editingFinished.connect(self._finish_pattern)
        self.title.textChanged.connect(self._update_preview)
        self.location.textChanged.connect(self._update_preview)
        self.description.textChanged.connect(self._update_preview)
        self.calendar.activated.connect(self._calendar_selected)
        self.calendar.currentIndexChanged.connect(self._calendar_selected)
        self.calendar.currentIndexChanged.connect(self._update_preview)
        self.preview.date_toggled.connect(self.toggle_date)
        self.preview.occurrence_selected.connect(self._preview_selected)
        self.preview.occurrence_remove_requested.connect(self.remove_occurrence)
        self.preview.occurrence_edit_requested.connect(self.edit_occurrence)

    def _calendar_selected(self, *_):
        if not self._loading and self.original is not None:
            self.calendar_change_requested = True

    @property
    def edit_reference(self):
        return self.series if self.series and self.scope.currentData() == 'group' else self.original

    def schedule_source(self, scope=None):
        scope = scope or self.scope.currentData()
        if scope == 'future' and self.series:
            if scope not in self._scope_sources:
                from .series_operations import partition_series
                _, tail = partition_series(self.series, self.original)
                if tail is None:
                    raise ValueError('There are no events at or after this occurrence.')
                self._scope_sources[scope] = tail
            return self._scope_sources[scope]
        return self.series or self.original

    def sequence_zone(self):
        source = self.schedule_source()
        return (event_zone(source) if source and (source.timezone_definitions or
                source.schedule_expression and source.schedule_syntax == 2) else local_zone())

    def _load_scope(self, scope, start=None, end=None):
        self._schedule_error = None
        reference = self.edit_reference
        source = self.schedule_source(scope)
        if reference:
            start, end = reference.start, reference.end
            self.calendar.setCurrentIndex(self.calendar.findData(reference.calendar_id))
        self.calendar_change_requested = False
        if reference is None and end is None:
            end = add_elapsed(start, timedelta(minutes=self.preferences.default_event_duration_minutes))
        all_day = bool(reference and reference.all_day)
        self._floating_time = bool(reference and reference.floating_time)
        if not all_day:
            if self._floating_time:
                start, end = start.replace(tzinfo=local_zone()), end.replace(tzinfo=local_zone())
            else:
                start, end = start.astimezone(local_zone()), end.astimezone(local_zone())
        self.all_day.setChecked(all_day)
        self.duration_spec = reference.duration_spec if reference and not all_day else ''
        self.duration_zone = event_zone(reference) if reference else local_zone()
        self._timed_range = None
        self._timed_duration = None
        self._set_range(start, end)
        self.title.setText(reference.title if reference else '')
        self.location.setText(reference.location if reference else '')
        self.description.setPlainText(reference.description if reference else '')
        self._draft = ScheduleDraft.from_event(source, authoring_zone=self.sequence_zone())
        self._draft.bind_primary_reference(start, end)
        self._repeat_draft = None
        self._custom_draft = None
        self._regular_values = None
        self._reset_schedule = self._repeat_reset = False
        self._report(self._sync_schedule_controls)
        if self._draft.kind in REGULAR_KINDS:
            self._remember_regular()
        if self._draft.kind == 'custom':
            self._remember_custom()
        self.schedule_mode.setCurrentData('alternatives' if reference and reference.candidate else
                                          'repeat' if source and source.recurring else 'single')
        self.selected_preview_event = None

    def _duration_caption(self):
        if self._range_needs_repair:
            return format_duration(self._duration, elapsed=True)
        return format_event_duration(self._last_start, self._last_end,
                                     all_day=self.all_day.isChecked(), duration_spec=self.duration_spec,
                                     floating_time=self.is_floating_time())

    def is_floating_time(self):
        return bool(getattr(self, '_floating_time', False) and not self.all_day.isChecked())

    def _relocate_range(self, target, *, endpoint='start'):
        if self._range_needs_repair:
            return ((target, self._duration_end(target, self._duration, self.duration_spec))
                    if endpoint == 'start' else (add_elapsed(target, -self._duration), target))
        return relocate_range(self._last_start, self._last_end, target, endpoint=endpoint,
                              all_day=self.all_day.isChecked(), duration_spec=self.duration_spec,
                              duration_zone=self.duration_zone, floating_time=self.is_floating_time())

    def _duration_end(self, start, duration, spec):
        if self.all_day.isChecked() or self.is_floating_time():
            return start + duration
        return (add_duration(start.astimezone(self.duration_zone), spec).astimezone(start.tzinfo)
                if spec else add_elapsed(start, duration))

    def _set_range(self, start, end, *, duration_spec=None):
        duration = range_duration(start, end, all_day=self.all_day.isChecked(), floating_time=self.is_floating_time())
        needs_repair = False
        if self._loading and not self.all_day.isChecked():
            try:
                validate_datetime(start, floating_time=self.is_floating_time())
                validate_datetime(end, floating_time=self.is_floating_time())
            except ValueError:
                # A caller can supply a nonexistent local clock. Keep those
                # fields editable and let Save identify the clock error. The
                # wall range expresses the intended duration until corrected.
                needs_repair = True
                wall = end.replace(tzinfo=None) - start.replace(tzinfo=None)
                duration = wall if wall > timedelta(0) else timedelta(
                    minutes=self.preferences.default_event_duration_minutes)
            except OverflowError:
                pass  # Reading the fields reports unsupported UTC boundaries.
        if duration < timedelta(0) or (not duration and not (
                self.original and not self.all_day.isChecked()
                and range_duration(self.original.start, self.original.end,
                                   floating_time=self.original.floating_time) == timedelta(0))):
            raise ValueError('The end must be after the start.')
        was_loading = self._loading
        self._loading = True
        try:
            all_day = self.all_day.isChecked()
            self.duration_spec = '' if all_day else normalized_spec(
                self.duration_spec if duration_spec is None else duration_spec)
            self.start.setAllDay(all_day)
            self.end.setAllDay(all_day)
            self.start.setFloatingTime(self.is_floating_time())
            self.end.setFloatingTime(self.is_floating_time())
            self.start.set_value(start)
            self.end.set_value(end - timedelta(days=1) if all_day else end)
            self._last_start, self._last_end, self._duration = start, end, duration
            self._range_needs_repair = needs_repair
            self.duration.setText(self._duration_caption())
            self._sync_presets()
        finally:
            self._loading = was_loading

        if (not was_loading and hasattr(self, '_draft') and self.pattern.text() == self._draft.expression):
            committed = self._draft.commit_reference_range(start, end, all_day=self.all_day.isChecked(),
                                                          duration_spec=self.duration_spec)
            if self._draft.range_policy is not None:
                self._draft.refresh_range_policy(start, end, all_day=self.all_day.isChecked())
                committed = True
            if committed:
                self._sync_schedule_controls()

    def value(self, field):
        self._validation_field = field
        value = field.value()
        if self.all_day.isChecked() and field is self.end:
            value += timedelta(days=1)
        return validate_datetime(value, all_day=self.all_day.isChecked(), floating_time=self.is_floating_time())

    validate_time = staticmethod(validate_datetime)

    def _report(self, callback, field=None):
        try:
            callback()
            return True
        except (ValueError, OverflowError) as exc:
            self._validation_field = field or self._validation_field
            self.preview.enable_date_editing(False)
            self.error.setText(str(exc))
            self.error.show()
            return False

    def _start_changed(self):
        if self._loading:
            return
        def change():
            start = self.value(self.start)
            self._set_range(*self._relocate_range(start))
            self._sync_ending()
            self._update_preview()
        self._report(change, self.start)

    def _end_changed(self):
        if self._loading:
            return
        def change():
            end = self.value(self.end)
            start = self.value(self.start)
            changed = (end.replace(tzinfo=None) != self._last_end.replace(tzinfo=None) if self.all_day.isChecked() or self.is_floating_time()
                       else end.timestamp() != self._last_end.timestamp())
            if range_duration(start, end, all_day=self.all_day.isChecked(), floating_time=self.is_floating_time()) <= timedelta(0):
                start = self._relocate_range(end, endpoint='end')[0]
            self._set_range(start, end, duration_spec='' if changed else self.duration_spec)
            self._sync_ending()
            self._update_preview()
        self._report(change, self.end)

    def _duration_changed(self):
        if self._loading:
            return
        def change():
            if self.duration.text().strip() == self._duration_caption():
                return
            duration, spec = parse_event_duration(self.duration.text(), all_day=self.all_day.isChecked())
            start = self.value(self.start)
            self._set_range(start, self._duration_end(start, duration, spec), duration_spec=spec)
            self._update_preview()
        self._report(change, self.duration)

    def _all_day_changed(self, enabled):
        if self._loading:
            return
        def change():
            if enabled:
                self._timed_range = self._last_start, self._last_end
                self._timed_duration = self.duration_spec, self.duration_zone
                first = self._last_start.date()
                final = max(first, self._last_end.date() - (
                    timedelta(days=1) if self._last_end.time() == time.min else timedelta()))
                start = datetime.combine(first, time.min, self._last_start.tzinfo)
                end = datetime.combine(final + timedelta(days=1), time.min, self._last_start.tzinfo)
            else:
                previous = self._timed_range
                self.duration_spec, self.duration_zone = self._timed_duration or ('', local_zone())
                clock = previous[0].timetz() if previous else time(9, tzinfo=local_zone())
                start = datetime.combine(self._last_start.date(), clock)
                end = (relocate_range(*previous, start, duration_spec=self.duration_spec,
                                      duration_zone=self.duration_zone, floating_time=self.is_floating_time())[1] if previous else
                       add_elapsed(start, timedelta(minutes=self.preferences.default_event_duration_minutes)))
            self._set_range(start, end)
            self._sync_ending()
            self._update_preview()
        self._report(change, self.all_day)

    def _preset_values(self):
        presets = self.preferences.duration_presets_minutes
        if self.all_day.isChecked():
            return [value for value in presets if value % 1440 == 0] or [1440, 2880, 10080]
        return list(presets)

    def _sync_presets(self):
        values = self._preset_values()
        current = [data for _caption, data in self.duration_choices.options]
        if current != values:
            old = self.duration_choices
            def caption(minutes):
                if self.all_day.isChecked() or minutes < 60:
                    return format_duration(timedelta(minutes=minutes), all_day=self.all_day.isChecked(), elapsed=True)
                hours, remainder = divmod(minutes, 60)
                return f'{hours}h' + (f'{remainder:02}' if remainder else '')
            options = [(caption(value), value) for value in values]
            control = ChoiceControl(options, self.preferences)
            for value, button in control.buttons.items():
                button.setToolTip(format_duration(timedelta(minutes=value), all_day=self.all_day.isChecked(), elapsed=True))
            control.currentChanged.connect(self._preset_changed)
            self.duration_fields.replace_choices(control)
            old.hide()
            old.deleteLater()
            self.duration_choices = control
        minutes = self._duration.total_seconds() / 60
        self.duration_choices.setCurrentIndex(-1 if self.duration_spec else self.duration_choices.findData(minutes))

    def _preset_changed(self, minutes):
        if not self._loading and minutes is not None:
            self.duration.setText(format_duration(timedelta(minutes=minutes), all_day=self.all_day.isChecked(), elapsed=True))
            self._duration_changed()

    def _set_duration_preferences(self, preferences):
        self.preferences = preferences
        was_loading, self._loading = self._loading, True
        try:
            self._sync_presets()
        finally:
            self._loading = was_loading

    def _can_edit_schedule(self):
        return not self.common_fields_only and (not self.has_group or (self.series is not None and self.scope.currentData() != 'single'))

    def _overflow_context(self):
        draft = self._draft
        if draft.kind in ('MONTHLY', 'YEARLY'):
            return draft.kind, draft.interval, None, True
        try:
            tail = sequence_tail(draft.expression)
            if tail is not None:
                if tail.unit not in ('m', 'y') or tail.weekdays:
                    return None
                return tail.frequency, tail.interval, tail.preferred_day, True
            steps = relative_sequence_steps(draft.expression)
        except PatternError:
            return None
        if not steps:
            return None
        calendar = [(amount, unit) for amount, unit, _clamp in steps if amount and unit in ('m', 'y')]
        if not calendar:
            return None
        interval, unit = calendar[0]
        positive = [(amount, unit) for amount, unit, _clamp in steps if amount]
        uniform = all(value == calendar[0] for value in positive)
        return ('MONTHLY' if unit == 'm' else 'YEARLY'), interval, None, uniform

    def _range_policy_safety(self, start, kind, interval, span, preferred_day):
        """Check prospective choices using the draft's active bound and zone."""
        draft = self._draft
        count = draft.count if draft.kind in REGULAR_KINDS and draft.ending == 'count' else None
        until = draft.until if draft.kind in REGULAR_KINDS and draft.ending == 'until' else None
        try:
            tail = sequence_tail(draft.expression)
            if tail is not None:
                compiled = compile_sequence(draft.expression, start, all_day=self.all_day.isChecked())
                parts = dict(part.split('=', 1) for part in compiled.recurrence_rule.split(';') if '=' in part)
                positive_days = [int(value) for value in parts.get('BYMONTHDAY', '').split(',')
                                 if value and int(value) > 0]
                start = compiled.tail_start.replace(hour=int(parts.get('BYHOUR', compiled.tail_start.hour)),
                    minute=int(parts.get('BYMINUTE', compiled.tail_start.minute)),
                    second=int(parts.get('BYSECOND', compiled.tail_start.second)))
                preferred_day = tail.preferred_day or (min(positive_days) if positive_days else preferred_day)
                if draft.kind not in REGULAR_KINDS:
                    count, until = tail.count, tail.until
        except PatternError:
            pass
        end = self._relocate_range(start)[1]
        if not self.all_day.isChecked():
            start, end = start.astimezone(self.duration_zone), end.astimezone(self.duration_zone)
        prototype = self._prototype(start, end)
        return {policy: static_range_policy_fits(prototype, kind, interval, policy=policy, span=span,
            preferred_day=preferred_day, count=count, until=until) for policy in ('skip', 'shift')}

    def _sync_overflow_policy(self, editable, repeated):
        context = self._overflow_context()
        relevant = allow_cut = False
        shift_fits = True
        safe = {'skip': True, 'shift': True}
        if context is not None:
            kind, interval, preferred_day, regular = context
            start = self._draft.schedule_anchor(self._last_start, all_day=self.all_day.isChecked())
            end = self._relocate_range(start)[1]
            event_span = occupied_span(start, end, self.all_day.isChecked())
            relevant, allow_cut = range_policy_options(start, end, kind, interval,
                all_day=self.all_day.isChecked(), preferred_day=preferred_day,
                range_policy=self._draft.range_policy or '',
                span=self._draft.range_span if self._draft.overflow else None)
            span = self._draft.range_span if self._draft.overflow else event_span
            shift_fits = shift_range_fits(start, kind, interval, span=span if allow_cut else 1)
            allow_cut = allow_cut and regular
            if not regular and event_span > 1:
                # Mixed finite programs currently express start-date steps,
                # not a single month-contained range policy.
                relevant = allow_cut = False
            if allow_cut and span > 1:
                safe = self._range_policy_safety(start, kind, interval, span, preferred_day)
                relevant = relevant or not all(safe.values())
            if allow_cut:
                if not all(safe.values()):
                    problem = 'Clock changes can make this duration extend beyond the end of a month.'
                elif kind == 'MONTHLY':
                    problem = 'Not every month in this series has room for the full date range.'
                else:
                    problem = 'In some years, this month is too short for the full date range.'
                skip = 'Omit occurrences where the full range does not fit.'
                shift = 'Move the whole event earlier to fit, keeping its duration.'
            else:
                problem = ('Not every month in this series has the chosen starting day.'
                           if kind == 'MONTHLY' else
                           'The chosen starting date does not exist every year.')
                skip = 'Omit occurrences when that date is missing.'
                shift = ('Use the last day of that month instead.' if span == event_span == 1 else
                         'Move the start to the last day of that month, keeping its duration.')
            self.overflow_policy.buttons['skip'].setToolTip(f'Skip · {problem} {skip}')
            period = 'months' if kind == 'MONTHLY' else 'years'
            self.overflow_policy.buttons['cut'].setToolTip(
                f'Cut · {problem} Keep the start and trim the part beyond the end of the month. '
                f'Skip {period} without the starting date.')
            if shift_fits:
                shift_tip = f'{problem} {shift}'
            else:
                shift_tip = ('The full date range cannot fit in every month in this series.'
                             if kind == 'MONTHLY' else
                             'In some years, the full date range cannot fit in this month.')
                shift_tip += ' Choose Skip or Cut.' if allow_cut else ' Choose Skip or a shorter range.'
            self.overflow_policy.buttons['shift'].setToolTip(f'Shift · {shift_tip}')
            duration_problem = ('This duration can extend beyond the end of a month in this series. '
                                'Use a calendar-day duration such as 2d, choose Cut, or choose an earlier end.')
            if not safe['skip']:
                self.overflow_policy.buttons['skip'].setToolTip('Skip · ' + duration_problem)
            if shift_fits and not safe['shift']:
                self.overflow_policy.buttons['shift'].setToolTip('Shift · ' + duration_problem)
        blocked = self.overflow_policy.blockSignals(True)
        try:
            self.overflow_policy.setOptionVisible('cut', allow_cut)
            self.overflow_policy.setFixedWidth(self.overflow_policy.height() * self.overflow_policy.visibleCount())
            policy = self._draft.range_policy
            if allow_cut and not self._draft.overflow:
                policy = None
            self.overflow_policy.setCurrentData(policy if policy != 'cut' or allow_cut else 'shift')
        finally:
            self.overflow_policy.blockSignals(blocked)
        self.overflow_policy.setVisible(repeated and relevant and not self.common_fields_only)
        self.overflow_policy.setEnabled(editable)
        self.overflow_policy.buttons['skip'].setEnabled(editable and safe['skip'])
        self.overflow_policy.buttons['shift'].setEnabled(editable and shift_fits and safe['shift'])

    def _sync_schedule_controls(self):
        was_loading, self._loading = self._loading, True
        try:
            draft = self._draft
            self.repeat.setCurrentData(draft.kind if draft.kind in REGULAR_KINDS else 'RDATE')
            self.interval.setValue(draft.interval)
            self.repeat_count.setValue(draft.count or 10)
            self.no_end.setChecked(draft.ending == 'never')
            self.overflow_policy.setCurrentData(draft.range_policy)
            # Resetting an unchanged QLineEdit also moves its cursor and clears
            # its undo history. Live validation must leave the user's edit alone.
            if self.pattern.text() != draft.expression:
                self.pattern.setText(draft.expression)
            if draft.until:
                self.repeat_until.set_value(datetime.combine(draft.until, time.min, self._last_start.tzinfo))
            self._sync_ending()
        finally:
            self._loading = was_loading

    def _sync_ending(self):
        draft = self._draft
        if draft.kind not in REGULAR_KINDS:
            return
        was_loading, self._loading = self._loading, True
        try:
            start = draft.schedule_anchor(self.value(self.start), all_day=self.all_day.isChecked())
            if draft.ending == 'until' and draft.until:
                count = until_to_count(start, draft.kind, draft.interval, draft.until, draft.weekdays,
                                       **self._regular_options(draft))
                self.repeat_count.setValue(count)
                self.repeat_until.set_value(datetime.combine(draft.until, time.min, start.tzinfo))
            elif draft.ending != 'never':
                day = count_to_until(start, draft.kind, draft.interval, draft.count or self.repeat_count.value(), draft.weekdays,
                                     **self._regular_options(draft))
                self.repeat_until.set_value(datetime.combine(day, time.min, start.tzinfo))
        finally:
            self._loading = was_loading

    @staticmethod
    def _regular_options(draft):
        regular = draft.regular()
        return dict(clamp=regular.clamp, preferred_day=regular.preferred_day,
                    selectors=regular.selectors, week_start=regular.week_start,
                    overflow=regular.overflow, range_span=regular.range_span)

    def _schedule_mode_changed(self, mode):
        if self._loading:
            return
        if mode == 'single':
            self._schedule_error = None
            self._repeat_draft = deepcopy(self._draft)
            self._repeat_reset = self._reset_schedule
            self._reset_schedule = False
            self._draft = ScheduleDraft.from_event(authoring_zone=self.sequence_zone())
            self._sync_schedule_controls()
        elif self._draft.kind == 'once':
            if self._repeat_draft:
                self._draft = deepcopy(self._repeat_draft)
                self._reset_schedule = self._repeat_reset
            else:
                self._draft.set_regular(self.repeat.currentData() if self.repeat.currentData() in REGULAR_KINDS else 'WEEKLY',
                                        count=self.repeat_count.value(), range_policy='skip')
                self._draft.refresh_range_policy(self.value(self.start), self.value(self.end),
                                                 all_day=self.all_day.isChecked())
            self._report(self._sync_schedule_controls)
        self._update_preview()

    def _frequency_changed(self, kind):
        if self._loading:
            return
        if self.schedule_mode.currentData() == 'single':
            self.schedule_mode.setCurrentData('repeat')
        if kind == 'RDATE':
            return self._enter_custom()
        if self._draft.kind == 'custom':
            self._remember_custom()
            def reset():
                values = self._regular_values or dict(interval=1, count=6, indefinite=False, range_policy='skip')
                draft = ScheduleDraft.from_event(authoring_zone=self.sequence_zone())
                draft.set_regular(kind, interval=values['interval'], count=values['count'],
                                  ending='never' if values['indefinite'] else 'count',
                                  range_policy=values['range_policy'] or 'skip')
                draft.refresh_range_policy(self.value(self.start), self.value(self.end), all_day=self.all_day.isChecked())
                start = draft.schedule_anchor(self.value(self.start), all_day=self.all_day.isChecked())
                if draft.ending != 'never':
                    count_to_until(start, kind, draft.interval, draft.count, draft.weekdays, **self._regular_options(draft))
                self._draft = draft
                self._reset_schedule = True
                self._schedule_error = None
                self._sync_schedule_controls()
                self._remember_regular()
            if not self._report(reset, self.repeat):
                self._sync_schedule_controls()
                return False
        else:
            self._regular_changed('count')
        self._update_preview()
        return True

    def _remember_regular(self):
        self._regular_values = dict(interval=self.interval.value(), count=self.repeat_count.value(),
                                    indefinite=self.no_end.isChecked(), range_policy=self._draft.range_policy)

    def _remember_custom(self):
        draft = deepcopy(self._draft)
        draft.kind = 'custom'
        self._custom_draft = dict(draft=draft, text=self.pattern.text(), reset=self._reset_schedule)

    def _enter_custom(self, *, restore=True):
        if self._loading:
            return False
        if self._draft.kind in REGULAR_KINDS:
            self._remember_regular()
        def change():
            if restore and self._custom_draft is not None:
                state = self._custom_draft
                self._draft = deepcopy(state['draft'])
                self._reset_schedule = state['reset']
                self._sync_schedule_controls()
                self.pattern.blockSignals(True)
                self.pattern.setText(state['text'])
                self.pattern.blockSignals(False)
            else:
                master = self._project_preview(self.build_events()[0])
                draft = deepcopy(self._draft)
                draft.use_sequence(master, reference_start=self.value(self.start), reference_end=self.value(self.end))
                self._draft = draft
                self._sync_schedule_controls()
                self._remember_custom()
            self._schedule_error = None
            self._update_preview()
        if self._report(change, self.pattern):
            return True
        self.repeat.blockSignals(True)
        self.repeat.setCurrentData(self._draft.kind if self._draft.kind in REGULAR_KINDS else 'RDATE')
        self.repeat.blockSignals(False)
        return False

    def _regular_changed(self, ending):
        if self._loading or self.repeat.currentData() not in REGULAR_KINDS:
            return
        def change():
            kind = self.repeat.currentData()
            draft = deepcopy(self._draft)
            draft.set_regular(kind, interval=self.interval.value(), count=self.repeat_count.value(),
                until=self.repeat_until.value().date() if ending == 'until' else None,
                weekdays=draft.weekdays if kind == 'WEEKLY' else (),
                ending='never' if self.no_end.isChecked() else ending,
                range_policy=(self.overflow_policy.currentData()
                              if self.overflow_policy.currentData() is not None else
                              'skip' if self._draft.kind != kind or self._draft.source is None else None))
            draft.refresh_range_policy(self.value(self.start), self.value(self.end), all_day=self.all_day.isChecked())
            start = draft.schedule_anchor(self.value(self.start), all_day=self.all_day.isChecked())
            if draft.ending == 'until':
                until_to_count(start, kind, draft.interval, draft.until, draft.weekdays, **self._regular_options(draft))
            elif draft.ending != 'never':
                count_to_until(start, kind, draft.interval, draft.count, draft.weekdays, **self._regular_options(draft))
            self._draft = draft
            self._schedule_error = None
            self._sync_schedule_controls()
            self._remember_regular()
            self._update_preview()
        field = self.repeat_until if ending == 'until' else self.repeat_count
        if not self._report(change, field):
            self._schedule_error = (self.error.text(), field)
            self.repeat.blockSignals(True)
            self.repeat.setCurrentData(self._draft.kind if self._draft.kind in REGULAR_KINDS else 'RDATE')
            self.repeat.blockSignals(False)

    def _overflow_policy_changed(self, policy):
        if self._loading or policy is None:
            return
        def change():
            draft = deepcopy(self._draft)
            start, end = self.value(self.start), self.value(self.end)
            if self.pattern.text() != draft.expression:
                self._read_expression(draft, detect=False)
                self._adopt_expression_duration(draft)
                end = self.value(self.end)
            draft.set_range_policy(policy, start, end, all_day=self.all_day.isChecked())
            self._draft = draft
            self._schedule_error = None
            self._sync_schedule_controls()
            self._remember_regular() if draft.kind in REGULAR_KINDS else self._remember_custom()
            self._update_preview()
        if not self._report(change, self.pattern):
            self._sync_overflow_policy(self._can_edit_schedule(), self.schedule_mode.currentData() != 'single')

    def pattern_changed(self, *_):
        if self._loading:
            return
        if self.schedule_mode.currentData() == 'single':
            self._loading = True
            self.schedule_mode.setCurrentData('repeat')
            self.repeat.setCurrentData('RDATE')
            self._draft.kind = 'custom'
            self._loading = False
        def change():
            draft = deepcopy(self._draft)
            self._read_expression(draft, detect=not self.pattern.hasFocus())
            self._adopt_expression_duration(draft)
            self._draft = draft
            self._schedule_error = None
            was_loading, self._loading = self._loading, True
            try:
                if self.schedule_mode.currentData() == 'single':
                    self.schedule_mode.setCurrentData('repeat')
                self._sync_schedule_controls()
                self._remember_custom()
            finally:
                self._loading = was_loading
            self._update_preview()
        self._report(change, self.pattern)

    def _finish_pattern(self):
        if not self._loading and self._draft.kind == 'custom':
            def finish():
                draft = deepcopy(self._draft)
                self._read_expression(draft)
                self._adopt_expression_duration(draft)
                self._draft = draft
                self._sync_schedule_controls()
                self._remember_custom()
                self._update_preview()
            self._report(finish, self.pattern)

    def _prototype(self, start, end, *, duration_spec=None, duration_zone=None):
        """One range/content projection for text validation and event creation."""
        spec = self.duration_spec if duration_spec is None else duration_spec
        zone = duration_zone or self.duration_zone
        source = self.schedule_source()
        floating = bool(source and source.floating_time and not self.all_day.isChecked())
        return Event(self.original.id if self.original else self._new_id,
            self.calendar.currentData() or 'local', self.title.text().strip() or 'Untitled event',
            start.astimezone(zone) if spec else start, end.astimezone(zone) if spec else end,
            location=self.location.text(), description=self.description.toPlainText(),
            all_day=self.all_day.isChecked(), candidate=self.schedule_mode.currentData() == 'alternatives',
            duration_spec=spec, timezone_id=timezone_identifier(zone) if spec or source and source.timezone_definitions else '',
            timezone_definitions=source.timezone_definitions if source else {}, floating_time=floating)

    def _read_expression(self, draft, *, detect=True):
        start, end = self.value(self.start), self.value(self.end)
        prototype = self._prototype(start, end)
        self._validation_field = self.pattern
        draft.set_expression(self.pattern.text(), start, all_day=self.all_day.isChecked(),
                             detect=detect, prototype=prototype)

    def _adopt_expression_duration(self, draft):
        start = self.value(self.start)
        state = draft.document_duration(start)
        if state is not None:
            end, spec, zone = state
            # _set_range can refresh controls, so commit the already validated
            # document and its duration as one state change under the load guard.
            was_loading, self._loading = self._loading, True
            try:
                self.duration_zone = zone
                self._set_range(start, end, duration_spec=spec)
            finally:
                self._loading = was_loading

    def sequence_starts(self):
        return self._draft.starts(self.value(self.start), all_day=self.all_day.isChecked())

    def dates(self):
        return sorted({value.date() if self.all_day.isChecked() else value.astimezone(local_zone()).date()
                       for value in self.sequence_starts()})

    def build_events(self):
        start, end = self.value(self.start), self.value(self.end)
        duration_spec, duration_zone = self.duration_spec, self.duration_zone
        point = (self.original and range_duration(self.original.start, self.original.end,
                                                 floating_time=self.original.floating_time) == timedelta(0)
                 and not self.all_day.isChecked())
        try:
            validate_range_order(start, end, all_day=self.all_day.isChecked(), allow_point=bool(point),
                                 floating_time=self.is_floating_time())
        except ValueError:
            self._validation_field = self.end
            raise
        source = self.schedule_source()
        draft = self._draft
        single = self.has_group and self.scope.currentData() == 'single'
        if not single and self._schedule_error and self.schedule_mode.currentData() != 'single':
            message, self._validation_field = self._schedule_error
            raise ValueError(message)
        if not single and self.schedule_mode.currentData() != 'single':
            if self.pattern.text() != draft.expression:
                self._validation_field = self.pattern
                # Previewing or validating pending text is a pure projection.
                # Only the explicit editor handlers commit a new draft.
                draft = deepcopy(draft)
                self._read_expression(draft, detect=not self.pattern.hasFocus())
                state = draft.document_duration(start)
                if state is not None:
                    end, duration_spec, duration_zone = state
        if single:
            fields = ScheduleDraft.from_event().fields(start, all_day=self.all_day.isChecked())
        else:
            self._validation_field = self.pattern
            prototype = self._prototype(start, end, duration_spec=duration_spec, duration_zone=duration_zone)
            fields = draft.fields(start, end, all_day=self.all_day.isChecked(), prototype=prototype)
        self.schedule_replacement = bool(self.series and self.scope.currentData() != 'single' and
            (draft.configuration_changed or (source and source.recurring and draft.kind == 'once')))
        if self.schedule_replacement and self._reset_schedule:
            self.schedule_replacement = ScheduleReplacement.RESET
        stored_start, stored_end = ((start, end) if single else
                                   draft.primary_range(start, end, all_day=self.all_day.isChecked(),
                                                       duration_spec=duration_spec))
        if duration_spec:
            stored_start, stored_end = stored_start.astimezone(duration_zone), stored_end.astimezone(duration_zone)
        values = dict(id=self.original.id if self.original else self._new_id,
                      calendar_id=self.calendar.currentData() or 'local', title=self.title.text().strip() or 'Untitled event',
                      start=stored_start, end=stored_end, location=self.location.text(), description=self.description.toPlainText(),
                      all_day=self.all_day.isChecked(), candidate=self.schedule_mode.currentData() == 'alternatives', **fields)
        values['duration_spec'] = duration_spec
        values['floating_time'] = bool(source and source.floating_time and not self.all_day.isChecked())
        if source:
            values['timezone_definitions'] = source.timezone_definitions
            if single or source.timezone_definitions:
                values['timezone_id'] = source.timezone_id
        if duration_spec:
            values['timezone_id'] = timezone_identifier(duration_zone)
        event = replace(self.original, **values) if self.original else Event(**values)
        if self.original is None and self.series is None and event.recurrence_overrides:
            # A new draft has shared content controls and individual ranges.
            # Header changes made after adding a range apply to every copy.
            overrides = {}
            for key, payload in event.recurrence_overrides.items():
                item = Event.from_dict(payload)
                overrides[key] = replace(item, id=occurrence_id(event.id, key),
                    calendar_id=event.calendar_id, ical_uid=event.ical_uid,
                    series_id=event.id, recurrence_id=key, title=event.title,
                    location=event.location, description=event.description,
                    candidate=event.candidate).to_dict()
            event = replace(event, recurrence_overrides=overrides)
        if self.original is None:
            event = canonicalize_authored_primary(event)
        if event.recurring:
            recurrence_set(event)
            if not single and (draft.kind == 'custom' or draft.expression):
                for value in draft.validation_starts(start, all_day=self.all_day.isChecked()):
                    validate_datetime(value, all_day=event.all_day, floating_time=event.floating_time)
                    validate_datetime(end_for(event, value), all_day=event.all_day, floating_time=event.floating_time)
        return [event]

    def _project_preview(self, event):
        projected = event
        if self.series and self.scope.currentData() == 'group':
            projected = updated_series(self.series, self.edit_reference, event, replace_schedule=self.schedule_replacement)
        elif self.series and self.scope.currentData() == 'future':
            from .series_operations import updated_future
            _, projected = updated_future(self.series, self.original, event, self.schedule_replacement)
        return (replace(projected, calendar_id=event.calendar_id)
                if projected is not None and projected.calendar_id != event.calendar_id else projected)

    def _update_preview(self, *_):
        if self._loading:
            return
        editable = self._can_edit_schedule()
        repeated = self.schedule_mode.currentData() != 'single'
        self.schedule_mode.setEnabled(editable)
        self.schedule_mode.setVisible(not self.common_fields_only)
        self.repeat.setVisible(repeated and not self.common_fields_only)
        self.repeat.setEnabled(editable)
        regular = repeated and self._draft.kind in REGULAR_KINDS
        self.recurrence_details.setVisible(repeated and not self.common_fields_only)
        self.recurrence_details.setCurrentWidget(self.regular_controls if regular else self.custom_controls)
        self.regular_controls.setEnabled(editable)
        self.repeat_count.setEnabled(editable and not self.no_end.isChecked())
        self.repeat_until.setEnabled(editable and not self.no_end.isChecked())
        self.repeat_count.setToolTip('Not used for an indefinite pattern.' if self.no_end.isChecked()
                                    else 'Total occurrences.')
        self.repeat_until.setToolTip('Not used for an indefinite pattern.' if self.no_end.isChecked()
                                    else 'Final recurrence date')
        self.custom_controls.setEnabled(editable)
        self._sync_overflow_policy(editable, repeated)
        self.pattern.setEnabled(editable)
        self.add_occurrence_button.setEnabled(editable)
        self.add_occurrence_button.setVisible(not self.common_fields_only)
        if not self._report(self._refresh_preview):
            self.preview.set_events([], self.preferences, self.calendars)
            self.preview.enable_date_editing(False)

    def _refresh_preview(self):
        event = self.build_events()[0]
        projected = self._project_preview(event)
        self.preview.set_events([projected] if projected else [], self.preferences, self.calendars)
        primary = replace(event, start=self.value(self.start), end=self.value(self.end))
        if projected and self.series and self.scope.currentData() == 'future' and self.original.recurrence_id:
            source = self.schedule_source()
            zone = event_zone(projected)
            def wall(value):
                return value.replace(tzinfo=None) if projected.all_day else value.astimezone(zone).replace(tzinfo=None)
            shift = wall(projected.start) - wall(source.start)
            nominal = (wall(key_datetime(self.original.recurrence_id, source)) + shift).replace(tzinfo=zone)
            primary = replace(primary, recurrence_id=recurrence_key(nominal, projected.all_day, floating_time=projected.floating_time))
        elif projected and self.series and self.scope.currentData() == 'group':
            primary = replace(primary, recurrence_id=recurrence_key(projected.start, projected.all_day, floating_time=projected.floating_time))
        self.preview.set_primary(primary)
        self.preview.enable_date_editing(self._can_edit_schedule() and not self.preview.error)
        self.error.setText(self.preview.error)
        self.error.setVisible(bool(self.preview.error))

    def toggle_date(self, day):
        if not self._can_edit_schedule():
            return
        def toggle():
            master = self._project_preview(self.build_events()[0])
            self._draft.toggle_date(master, day, display_zone=local_zone(),
                                    reference_start=self.value(self.start), reference_end=self.value(self.end))
            self._after_graphical()
        self._report(toggle)

    def remove_occurrence(self, occurrence):
        if self._can_edit_schedule():
            def remove():
                master = self._project_preview(self.build_events()[0])
                self._draft.remove_occurrence(master, occurrence,
                    reference_start=self.value(self.start), reference_end=self.value(self.end))
                self._after_graphical()
            self._report(remove)

    def _after_graphical(self):
        was_loading, self._loading = self._loading, True
        try:
            if self.schedule_mode.currentData() == 'single':
                self.schedule_mode.setCurrentData('repeat')
            self._sync_schedule_controls()
            self._remember_custom()
        finally:
            self._loading = was_loading
        self._update_preview()

    def _preview_selected(self, event):
        self.selected_preview_event = event

    def _calendar_color(self):
        return next((calendar.color for calendar in self.calendars if calendar.id == self.calendar.currentData()),
                    scheme_for(self.preferences).accent)

    def open_range_picker(self, endpoint):
        def open_picker():
            self._commit_pending_range()
            start, end = self.value(self.start), self.value(self.end)
            picker = DateTimeRangePicker(start, end, endpoint=endpoint, preferences=self.preferences,
                calendar_color=self._calendar_color(), parent=self, all_day=self.all_day.isChecked(),
                full_day_shortcut=self.scope_bindings.get('full_day', 'F'),
                duration_spec=self.duration_spec, duration_zone=self.duration_zone, floating_time=self.is_floating_time())
            picker.accepted.connect(lambda: self._accept_primary_range(
                picker.result_start, picker.result_end, picker.result_duration_spec))
            self.range_picker = picker
            picker.open()
        self._report(open_picker, self.start if endpoint == 'start' else self.end)

    def _accept_primary_range(self, start, end, duration_spec=''):
        def accept():
            self._set_range(start, end, duration_spec=duration_spec)
            self._sync_ending()
            self._update_preview()
        self._report(accept)

    def open_until_picker(self):
        def open_picker():
            value = self.repeat_until.value()
            picker = DateTimeRangePicker(value, value + timedelta(days=1), preferences=self.preferences,
                calendar_color=self._calendar_color(), parent=self, all_day=True,
                full_day_shortcut=self.scope_bindings.get('full_day', 'F'))
            picker.accepted.connect(lambda: self.repeat_until.set_value(picker.result_start))
            self.range_picker = picker
            picker.open()
        self._report(open_picker, self.repeat_until)

    def add_occurrence(self):
        if not self._can_edit_schedule():
            return
        def open_picker():
            primary_start = self.value(self.start)
            selected = self.selected_preview_event
            if selected:
                day = selected.start.date() if selected.all_day else selected.start.astimezone(local_zone()).date()
            else:
                day = primary_start.date()
            start = datetime.combine(day, primary_start.timetz())
            picker = DateTimeRangePicker(start, self._relocate_range(start)[1], preferences=self.preferences,
                calendar_color=self._calendar_color(), parent=self, all_day=self.all_day.isChecked(),
                full_day_shortcut=self.scope_bindings.get('full_day', 'F'),
                duration_spec=self.duration_spec, duration_zone=self.duration_zone, floating_time=self.is_floating_time())
            picker.setWindowTitle('Add occurrence')
            picker.accepted.connect(lambda: self._add_range(
                picker.result_start, picker.result_end, duration_spec=picker.result_duration_spec))
            self.range_picker = picker
            picker.open()
        self._report(open_picker)

    def _add_range(self, start, end, *, duration_spec=None):
        def add():
            master = self._project_preview(self.build_events()[0])
            self._draft.add_occurrence(master, start, end,
                reference_start=self.value(self.start), reference_end=self.value(self.end), duration_spec=duration_spec)
            self._after_graphical()
        self._report(add)

    def edit_occurrence(self, occurrence):
        if self.schedule_mode.currentData() == 'single':
            self.open_range_picker('start')
            return
        if not self._can_edit_schedule():
            return
        start, end = occurrence.start, occurrence.end
        if not occurrence.all_day:
            if occurrence.floating_time:
                start, end = start.replace(tzinfo=local_zone()), end.replace(tzinfo=local_zone())
            else:
                start, end = start.astimezone(local_zone()), end.astimezone(local_zone())
        picker = DateTimeRangePicker(start, end, preferences=self.preferences,
            calendar_color=self._calendar_color(), parent=self, all_day=self.all_day.isChecked(),
            full_day_shortcut=self.scope_bindings.get('full_day', 'F'),
            duration_spec=occurrence.duration_spec, duration_zone=event_zone(occurrence), floating_time=occurrence.floating_time)
        picker.setWindowTitle('Edit occurrence')
        def apply():
            master = self._project_preview(self.build_events()[0])
            self._draft.edit_occurrence(master, occurrence, picker.result_start, picker.result_end,
                reference_start=self.value(self.start), reference_end=self.value(self.end),
                duration_spec=picker.result_duration_spec)
            self._after_graphical()
        picker.accepted.connect(lambda: self._report(apply))
        self.range_picker = picker
        picker.open()

    def scope_changed(self, scope):
        if self._loading or not self.original or scope == self._scope_name:
            return
        try:
            self.schedule_source(scope)
        except ValueError as exc:
            self.scope.blockSignals(True)
            self.scope.setCurrentData(self._scope_name)
            self.scope.blockSignals(False)
            self.error.setText(str(exc))
            self.error.show()
            return
        self._scope_values[self._scope_name] = self._snapshot()
        self._loading = True
        try:
            self._scope_name = scope
            if scope in self._scope_values:
                self._report(lambda: self._restore(self._scope_values[scope]))
            else:
                self._load_scope(scope)
        finally:
            self._loading = False
        self._update_preview()
        self.mode_changed.emit(scope)

    def _snapshot(self):
        return dict(start=self._last_start, end=self._last_end, duration=self._duration,
            duration_spec=self.duration_spec, duration_zone=self.duration_zone, timed_duration=self._timed_duration,
            calendar_id=self.calendar.currentData(), calendar_change_requested=self.calendar_change_requested,
            start_text=self.start.text(), end_text=self.end.text(), title=self.title.text(), location=self.location.text(),
            notes=self.description.toPlainText(), all_day=self.all_day.isChecked(), timed_range=self._timed_range,
            mode=self.schedule_mode.currentData(), draft=deepcopy(self._draft), repeat_draft=deepcopy(self._repeat_draft),
            custom_draft=deepcopy(self._custom_draft), regular_values=deepcopy(self._regular_values),
            reset_schedule=self._reset_schedule, repeat_reset=self._repeat_reset,
            schedule_error=self._schedule_error, interval=self.interval.value(), count=self.repeat_count.value(),
            until_text=self.repeat_until.text(), pattern_text=self.pattern.text())

    def _restore(self, state):
        self.calendar.setCurrentIndex(self.calendar.findData(state['calendar_id']))
        self.calendar_change_requested = state['calendar_change_requested']
        self.all_day.setChecked(state['all_day'])
        self._timed_range = state['timed_range']
        self._timed_duration = state['timed_duration']
        self.duration_spec, self.duration_zone = state['duration_spec'], state['duration_zone']
        self._set_range(state['start'], state['end'])
        self.start.setText(state['start_text'])
        self.end.setText(state['end_text'])
        self.title.setText(state['title'])
        self.location.setText(state['location'])
        self.description.setPlainText(state['notes'])
        self._draft, self._repeat_draft = state['draft'], state['repeat_draft']
        self._custom_draft, self._regular_values = state['custom_draft'], state['regular_values']
        self._reset_schedule, self._repeat_reset = state['reset_schedule'], state['repeat_reset']
        self._schedule_error = state['schedule_error']
        self.schedule_mode.setCurrentData(state['mode'])
        self._sync_schedule_controls()
        self.interval.setValue(state['interval'])
        self.repeat_count.setValue(state['count'])
        self.repeat_until.setText(state['until_text'])
        self.pattern.setText(state['pattern_text'])

    def save(self):
        try:
            if not self.title.text().strip():
                self._validation_field = self.title
                raise ValueError('Give your event a title.')
            if self.calendar.currentData() is None:
                self._validation_field = self.calendar
                raise ValueError('Add a writable calendar first.')
            self._commit_pending_range()
            self.result_events = self.build_events()
            self.accept()
        except (ValueError, OverflowError) as exc:
            self.result_events = []
            self.show_error(exc)
            self.error.show()

    def _commit_pending_range(self):
        """Reconcile typed values even when Return saves before focus changes."""
        start, end = self.value(self.start), self.value(self.end)
        all_day = self.all_day.isChecked()
        start_changed = ((start.replace(tzinfo=None) != self._last_start.replace(tzinfo=None)) if all_day or self.is_floating_time() else
                         start.timestamp() != self._last_start.timestamp() or
                         self._range_needs_repair and start.replace(tzinfo=None) != self._last_start.replace(tzinfo=None))
        end_changed = (end.replace(tzinfo=None) != self._last_end.replace(tzinfo=None) if all_day or self.is_floating_time() else
                       end.timestamp() != self._last_end.timestamp())
        if start_changed and not end_changed:
            end = self._relocate_range(start)[1]
        elif end_changed and range_duration(start, end, all_day=all_day, floating_time=self.is_floating_time()) <= timedelta(0):
            start = self._relocate_range(end, endpoint='end')[0]
        spec = '' if end_changed else self.duration_spec
        previous_duration = self._duration_caption()
        if self.duration.text().strip() != previous_duration:
            self._validation_field = self.duration
            duration, spec = parse_event_duration(self.duration.text(), all_day=self.all_day.isChecked())
            end = self._duration_end(start, duration, spec)
        self._set_range(start, end, duration_spec=spec)
        self._sync_ending()

    def show_error(self, message, field=None):
        super().show_error(message, field)
        target = field or self._validation_field
        if target is not None:
            self.controls_scroll.ensureWidgetVisible(target)
