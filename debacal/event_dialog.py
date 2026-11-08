"""Event authoring: a primary range, a recurrence draft, and a live month."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time, timedelta
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .buttons import action_button, icon_button
from .calendar_choice import CalendarChoice
from .choice_control import ChoiceControl
from .date_time_fields import DateTimeField, format_duration, parse_duration, validate_datetime
from .date_time_picker import DateTimeRangePicker, range_duration
from .dialog_base import FormDialog
from .event_schedule import ScheduleDraft, count_to_until, until_to_count
from .event_scope import DEFAULT_MODE_BINDINGS, MODE_ACTIONS
from .models import Event
from .preferences import Preferences
from .recurrence import ScheduleReplacement, event_zone, key_datetime, recurrence_key, recurrence_set, updated_series
from .series_preview import SeriesPreview
from .theme import scheme_for
from .timeutil import local_zone


REGULAR_KINDS = ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')


class RangeFields(QWidget):
    """Pair endpoints on roomy forms and stack them when text needs the room."""
    def __init__(self, start, end, preferences):
        super().__init__()
        self.preferences = preferences
        self.row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(10)
        for caption, field in (('Starts', start), ('Ends', end)):
            column = QWidget()
            layout = QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            label = QLabel(caption)
            label.setBuddy(field)
            layout.addWidget(label)
            layout.addWidget(field)
            self.row.addWidget(column, 1)

    def resizeEvent(self, event):
        threshold = round(360 * self.preferences.font_scale / 100)
        direction = QBoxLayout.Direction.LeftToRight if event.size().width() >= threshold else QBoxLayout.Direction.TopToBottom
        self.row.setDirection(direction)
        super().resizeEvent(event)


class DurationFields(QWidget):
    """Give longer preset lists a full row instead of clipping their captions."""
    def __init__(self, field, choices, settings):
        super().__init__()
        self.field, self.choices, self.settings = field, choices, settings
        self.row = QGridLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setHorizontalSpacing(7)
        self.row.setVerticalSpacing(5)
        self.row.addWidget(field, 0, 0)
        self.row.addWidget(settings, 0, 2)
        self.row.setColumnStretch(1, 1)
        self._wide = None
        self._arrange()

    def _arrange(self):
        metrics = self.choices.fontMetrics()
        needed = max((metrics.horizontalAdvance(caption) + 12 for caption, _ in self.choices.options), default=0)
        wide = self.width() >= self.field.maximumWidth() + self.settings.width() + 14 + needed * self.choices.count()
        if wide == self._wide:
            return
        self._wide = wide
        self.row.removeWidget(self.choices)
        self.row.addWidget(self.choices, 0 if wide else 1, 1 if wide else 0, 1, 1 if wide else 3)

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
        self.has_group = bool(series or (event and (event.series_id or event.group_id)))
        self.scope_bindings = DEFAULT_MODE_BINDINGS | (scope_bindings or {})
        self._new_id = str(uuid4())
        self._loading = True
        self._validation_field = None
        self._schedule_error = None
        self._scope_sources, self._scope_values = {}, {}
        self._scope_name = edit_scope if event and edit_scope in ('single', 'group', 'future') else 'single'
        self._repeat_draft = None
        self._custom_draft = None
        self._regular_values = None
        self._reset_schedule = False
        self._repeat_reset = False
        self._draft = ScheduleDraft.from_event()
        self._duration = timedelta(hours=1)
        self._last_start, self._last_end = start, end or start + self._duration
        self._timed_range = None
        self.result_events = []
        self.schedule_replacement = False
        self.range_picker = None
        self.selected_preview_event = None
        scale = self.preferences.font_scale / 100
        available = self.screen().availableGeometry()
        width = min(max(960, round(920 * scale)), max(760, available.width() - 48))
        self.resize(width, min(round(790 * scale), max(540, available.height() - 48)))
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
        self.heading.setContentsMargins(18, 16, 18, 14)
        left.addWidget(self.heading)
        self.scope_controls = ChoiceControl([('One', 'single'), ('All', 'group'), ('Following', 'future')], self.preferences, self)
        self.scope = self.scope_controls
        self.scope_buttons = self.scope.buttons
        self.scope.setCurrentData(self._scope_name)
        if event:
            scope_box, scope_layout = self._section(left)
            scope_layout.addWidget(self._heading('Apply to'))
            scope_layout.addWidget(self.scope_controls)
            self._divider(left)
        else:
            self.scope_controls.hide()
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
        self.calendar.setEnabled(event is None)
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
        timing.addWidget(RangeFields(self.start, self.end, self.preferences))
        self.duration = QLineEdit()
        self.duration.setAccessibleName('Duration')
        self.duration.setMaximumWidth(round(110 * scale))
        self.duration.setToolTip('Changing duration adjusts the end.')
        self.duration_choices = ChoiceControl([('30min', 30), ('45min', 45), ('1h', 60)], self.preferences)
        self.duration_preferences = self._icon_button('settings', 'Duration button preferences', self.configure_durations)
        self.duration_fields = DurationFields(self.duration, self.duration_choices, self.duration_preferences)
        timing.addWidget(QLabel('Duration'))
        timing.addWidget(self.duration_fields)
        self._divider(sections)
        self.schedule_section, scheduling = self._section(sections)
        scheduling.addWidget(self._heading('Schedule'))
        self.schedule_mode = ChoiceControl([('Single', 'single'), ('Repeat', 'repeat'), ('Alternatives', 'alternatives')], self.preferences,
                                          icons={'single': 'single', 'repeat': 'series', 'alternatives': 'alternatives'})
        scheduling.addWidget(self.schedule_mode)
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
        frequency_row.addSpacing(round(14 * scale))
        self.add_occurrence_button = icon_button('new', 'Add occurrence at another date or time', self.add_occurrence,
                                                preferences=self.preferences)
        frequency_row.addWidget(self.add_occurrence_button)
        frequency_row.addStretch(1)
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
        self.repeat_count.setToolTip('Total occurrences, including the primary event.')
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
        self.pattern.setAccessibleName('Sequence')
        self.pattern.setPlaceholderText('4*(+2) +3 3*+2')
        self.pattern.setToolTip('Steps after the primary event, e.g. +2w or 13. Continue indefinitely with a final every(1w).')
        self.custom_input = QStackedWidget()
        sequence_page = QWidget()
        sequence_layout = QVBoxLayout(sequence_page)
        sequence_layout.setContentsMargins(0, 0, 0, 0)
        sequence_layout.setSpacing(4)
        sequence_layout.addWidget(QLabel('Sequence'))
        sequence_layout.addWidget(self.pattern)
        self.custom_input.addWidget(sequence_page)
        self.advanced_page = QWidget()
        advanced = QVBoxLayout(self.advanced_page)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(4)
        self.advanced_summary = QLabel('Imported custom recurrence')
        self.advanced_summary.setToolTip('This recurrence is preserved. Adjust dates in the preview or choose a regular pattern.')
        advanced.addWidget(self.advanced_summary)
        self.convert_sequence_button = action_button('Edit as sequence', lambda: self._enter_custom(restore=False))
        advanced.addWidget(self.convert_sequence_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.custom_input.addWidget(self.advanced_page)
        custom.addWidget(self.custom_input)
        self.recurrence_details.addWidget(self.custom_controls)
        scheduling.addWidget(self.recurrence_details)
        self._divider(sections)
        self.notes_section, notes = self._section(sections)
        notes.addWidget(self._heading('Notes'))
        self.description = QTextEdit()
        self.description.setMinimumHeight(round(90 * scale))
        self.description.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        notes.addWidget(self.description, 1)
        sections.setStretch(sections.indexOf(self.notes_section), 1)
        self._divider(left)
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 10, 18, 10)
        footer_layout.setSpacing(6)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('error', True)
        self.error.hide()
        footer_layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        footer_layout.addWidget(self.buttons)
        left.addWidget(footer)
        outer.addWidget(self.sidebar)
        self.preview_column = QWidget()
        self.preview_column.setObjectName('eventEditorPreview')
        right = QVBoxLayout(self.preview_column)
        right.setContentsMargins(18, 16, 18, 12)
        self.preview = SeriesPreview()
        self.preview.month_label.setObjectName('heading')
        right.addWidget(self.preview, 1)
        outer.addWidget(self.preview_column, 1)
        self._connect()
        self._load_scope(self._scope_name, start, end)
        self._loading = False
        self._update_preview()
        self.configure_keyboard([*self.scope_buttons.values(), self.calendar, self.title, self.location,
            self.all_day, self.start.lineEdit(), self.end.lineEdit(), self.duration, *self.schedule_mode.buttons.values(),
            *self.repeat.buttons.values(), self.interval, self.repeat_count, self.repeat_until.lineEdit(),
            self.pattern, self.convert_sequence_button, self.add_occurrence_button, self.description,
            self.preview.previous_button, self.preview.today_button, self.preview.next_button, self.preview.first_button,
            self.preview.previous_occurrence_button, self.preview.next_occurrence_button, self.preview.last_button],
            [self.save_button, self.cancel_button], self.save,
            [self.scope_bindings[action] for action in MODE_ACTIONS if self.scope_bindings.get(action)] if event else ())
        self.mode_shortcuts = {}
        for action, scope in MODE_ACTIONS.items():
            shortcut = QShortcut(QKeySequence(self.scope_bindings.get(action, '')), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.setEnabled(bool(event and self.scope_bindings.get(action)))
            shortcut.activated.connect(lambda value=scope: self.scope.setCurrentData(value))
            self.mode_shortcuts[action] = shortcut
            self.scope_buttons[scope].setToolTip(self.scope_buttons[scope].text() + ' · ' + self.scope_bindings.get(action, ''))
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
        self.pattern.textChanged.connect(self.pattern_changed)
        self.pattern.editingFinished.connect(self._finish_pattern)
        self.title.textChanged.connect(self._update_preview)
        self.location.textChanged.connect(self._update_preview)
        self.description.textChanged.connect(self._update_preview)
        self.calendar.currentIndexChanged.connect(self._update_preview)
        self.preview.date_toggled.connect(self.toggle_date)
        self.preview.occurrence_selected.connect(self._preview_selected)
        self.preview.occurrence_remove_requested.connect(self.remove_occurrence)
        self.preview.occurrence_edit_requested.connect(self.edit_occurrence)

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
        return event_zone(source) if source and source.schedule_expression and source.schedule_syntax == 2 else local_zone()

    def _load_scope(self, scope, start=None, end=None):
        self._schedule_error = None
        reference = self.edit_reference
        source = self.schedule_source(scope)
        if reference:
            start, end = reference.start, reference.end
        elif end is None:
            end = start + timedelta(hours=1)
        all_day = bool(reference and reference.all_day)
        if not all_day:
            start, end = start.astimezone(local_zone()), end.astimezone(local_zone())
        self.all_day.setChecked(all_day)
        self._timed_range = None
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
        if self._draft.kind in ('custom', 'advanced') or self._draft.expression:
            self._remember_custom()
        self.schedule_mode.setCurrentData('alternatives' if reference and reference.candidate else
                                          'repeat' if source and source.recurring else 'single')
        self.selected_preview_event = None

    def _set_range(self, start, end):
        duration = range_duration(start, end)
        if duration < timedelta(0) or (not duration and not (self.original and self.original.start == self.original.end)):
            raise ValueError('The end must be after the start.')
        was_loading = self._loading
        self._loading = True
        try:
            all_day = self.all_day.isChecked()
            self.start.setAllDay(all_day)
            self.end.setAllDay(all_day)
            self.start.set_value(start)
            self.end.set_value(end - timedelta(days=1) if all_day else end)
            self._last_start, self._last_end, self._duration = start, end, duration
            self.duration.setText(format_duration(duration, all_day=all_day) if duration else '0min')
            self._sync_presets()
        finally:
            self._loading = was_loading

    def value(self, field):
        self._validation_field = field
        value = field.value()
        if self.all_day.isChecked() and field is self.end:
            value += timedelta(days=1)
        return validate_datetime(value)

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
            self._set_range(start, start + self._duration)
            self._sync_ending()
            self._update_preview()
        self._report(change, self.start)

    def _end_changed(self):
        if self._loading:
            return
        def change():
            end = self.value(self.end)
            start = self.value(self.start)
            if end.timestamp() <= start.timestamp():
                start = end - self._duration
            self._set_range(start, end)
            self._sync_ending()
            self._update_preview()
        self._report(change, self.end)

    def _duration_changed(self):
        if self._loading:
            return
        def change():
            duration = parse_duration(self.duration.text(), all_day=self.all_day.isChecked())
            start = self.value(self.start)
            self._set_range(start, start + duration)
            self._update_preview()
        self._report(change, self.duration)

    def _all_day_changed(self, enabled):
        if self._loading:
            return
        def change():
            if enabled:
                self._timed_range = self._last_start, self._last_end
                first = self._last_start.date()
                final = self._last_end.date() - (timedelta(days=1) if self._last_end.time() == time.min else timedelta())
                start = datetime.combine(first, time.min, self._last_start.tzinfo)
                end = datetime.combine(final + timedelta(days=1), time.min, self._last_start.tzinfo)
            else:
                previous = self._timed_range
                clock = previous[0].timetz() if previous else time(9, tzinfo=local_zone())
                start = datetime.combine(self._last_start.date(), clock)
                end = start + (range_duration(*previous) if previous else timedelta(hours=1))
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
                    return format_duration(timedelta(minutes=minutes), all_day=self.all_day.isChecked())
                hours, remainder = divmod(minutes, 60)
                return f'{hours}h' + (f'{remainder:02}' if remainder else '')
            options = [(caption(value), value) for value in values]
            control = ChoiceControl(options, self.preferences)
            for value, button in control.buttons.items():
                button.setToolTip(format_duration(timedelta(minutes=value), all_day=self.all_day.isChecked()))
            control.currentChanged.connect(self._preset_changed)
            self.duration_fields.replace_choices(control)
            old.hide()
            old.deleteLater()
            self.duration_choices = control
        minutes = self._duration.total_seconds() / 60
        self.duration_choices.setCurrentIndex(self.duration_choices.findData(minutes))

    def _preset_changed(self, minutes):
        if not self._loading and minutes is not None:
            self.duration.setText(format_duration(timedelta(minutes=minutes), all_day=self.all_day.isChecked()))
            self._duration_changed()

    def configure_durations(self):
        from .preferences_dialog import PreferencesDialog
        window = self.window()
        while window.parentWidget() is not None:
            window = window.parentWidget().window()
        if hasattr(window, 'show_preferences'):
            window.show_preferences(category='events')
            self.preferences = window.preferences
            self._sync_presets()
        else:
            dialog = PreferencesDialog(self.preferences, self)
            dialog.set_category('events')
            dialog.applied.connect(lambda preferences, _bindings: self._set_duration_preferences(preferences))
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._set_duration_preferences(dialog.result_preferences)

    def _set_duration_preferences(self, preferences):
        self.preferences = preferences
        was_loading, self._loading = self._loading, True
        try:
            self._sync_presets()
        finally:
            self._loading = was_loading

    def _can_edit_schedule(self):
        return not self.common_fields_only and (not self.has_group or (self.series is not None and self.scope.currentData() != 'single'))

    def _sync_schedule_controls(self):
        was_loading, self._loading = self._loading, True
        try:
            draft = self._draft
            self.repeat.setCurrentData(draft.kind if draft.kind in REGULAR_KINDS else 'RDATE')
            self.interval.setValue(draft.interval)
            self.repeat_count.setValue(draft.count or 10)
            self.no_end.setChecked(draft.ending == 'never')
            self.pattern.setText(draft.expression)
            self.custom_input.setCurrentIndex(1 if draft.kind == 'advanced' else 0)
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
                count = until_to_count(start, draft.kind, draft.interval, draft.until, draft.weekdays)
                self.repeat_count.setValue(count)
                self.repeat_until.set_value(datetime.combine(draft.until, time.min, start.tzinfo))
            elif draft.ending != 'never':
                day = count_to_until(start, draft.kind, draft.interval, draft.count or self.repeat_count.value(), draft.weekdays)
                self.repeat_until.set_value(datetime.combine(day, time.min, start.tzinfo))
        finally:
            self._loading = was_loading

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
                                        count=self.repeat_count.value())
            self._report(self._sync_schedule_controls)
        self._update_preview()

    def _frequency_changed(self, kind):
        if self._loading:
            return
        if self.schedule_mode.currentData() == 'single':
            self.schedule_mode.setCurrentData('repeat')
        if kind == 'RDATE':
            return self._enter_custom()
        if self._draft.kind in ('custom', 'advanced'):
            self._remember_custom()
            def reset():
                values = self._regular_values or dict(interval=1, count=6, indefinite=False)
                draft = ScheduleDraft.from_event(authoring_zone=self.sequence_zone())
                draft.set_regular(kind, interval=values['interval'], count=values['count'],
                                  ending='never' if values['indefinite'] else 'count')
                start = draft.schedule_anchor(self.value(self.start), all_day=self.all_day.isChecked())
                if draft.ending != 'never':
                    count_to_until(start, kind, draft.interval, draft.count, draft.weekdays)
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
                                    indefinite=self.no_end.isChecked())

    def _remember_custom(self):
        draft = deepcopy(self._draft)
        if draft.kind != 'advanced':
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
                ending='never' if self.no_end.isChecked() else ending)
            start = draft.schedule_anchor(self.value(self.start), all_day=self.all_day.isChecked())
            if draft.ending == 'until':
                until_to_count(start, kind, draft.interval, draft.until, draft.weekdays)
            elif draft.ending != 'never':
                count_to_until(start, kind, draft.interval, draft.count, draft.weekdays)
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
            draft.set_expression(self.pattern.text(), self.value(self.start), all_day=self.all_day.isChecked(),
                                 detect=not self.pattern.hasFocus())
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
                draft.set_expression(self.pattern.text(), self.value(self.start), all_day=self.all_day.isChecked())
                self._draft = draft
                self._sync_schedule_controls()
                self._remember_custom()
                self._update_preview()
            self._report(finish, self.pattern)

    def sequence_starts(self):
        return self._draft.starts(self.value(self.start), all_day=self.all_day.isChecked())

    def dates(self):
        return sorted({value.date() if self.all_day.isChecked() else value.astimezone(local_zone()).date()
                       for value in self.sequence_starts()})

    def build_events(self):
        start, end = self.value(self.start), self.value(self.end)
        point = self.original and self.original.start == self.original.end and not self.all_day.isChecked()
        if end.timestamp() < start.timestamp() or (end.timestamp() == start.timestamp() and not point):
            self._validation_field = self.end
            raise ValueError('The end must be after the start.')
        source = self.schedule_source()
        single = self.has_group and self.scope.currentData() == 'single'
        if not single and self._schedule_error and self.schedule_mode.currentData() != 'single':
            message, self._validation_field = self._schedule_error
            raise ValueError(message)
        if not single and self.schedule_mode.currentData() != 'single' and self._draft.kind != 'advanced':
            if self.pattern.text() != self._draft.expression:
                self._validation_field = self.pattern
                self._draft.set_expression(self.pattern.text(), start, all_day=self.all_day.isChecked(), detect=not self.pattern.hasFocus())
        if single:
            fields = ScheduleDraft.from_event().fields(start, all_day=self.all_day.isChecked())
        else:
            self._validation_field = self.repeat if self._draft.kind == 'advanced' else self.pattern
            fields = self._draft.fields(start, end, all_day=self.all_day.isChecked())
        self.schedule_replacement = bool(self.series and self.scope.currentData() != 'single' and
            (self._draft.configuration_changed or (source and source.recurring and self._draft.kind == 'once')))
        if self.schedule_replacement and self._reset_schedule:
            self.schedule_replacement = ScheduleReplacement.RESET
        stored_start, stored_end = ((start, end) if single else
                                   self._draft.primary_range(start, end, all_day=self.all_day.isChecked()))
        values = dict(id=self.original.id if self.original else self._new_id,
                      calendar_id=self.calendar.currentData() or 'local', title=self.title.text().strip() or 'Untitled event',
                      start=stored_start, end=stored_end, location=self.location.text(), description=self.description.toPlainText(),
                      all_day=self.all_day.isChecked(), candidate=self.schedule_mode.currentData() == 'alternatives', **fields)
        if self._draft.kind == 'once' or single:
            values['schedule_syntax'] = 1
        event = replace(self.original, **values) if self.original else Event(**values)
        if event.recurring:
            recurrence_set(event)
            if not single and (self._draft.kind == 'custom' or self._draft.expression):
                duration = range_duration(event.start, event.end)
                for value in self._draft.validation_starts(start, all_day=self.all_day.isChecked()):
                    validate_datetime(value)
                    validate_datetime(value + duration)
        return [event]

    def _project_preview(self, event):
        if self.series and self.scope.currentData() == 'group':
            return updated_series(self.series, self.edit_reference, event, replace_schedule=self.schedule_replacement)
        if self.series and self.scope.currentData() == 'future':
            from .series_operations import updated_future
            _, tail = updated_future(self.series, self.original, event, self.schedule_replacement)
            return tail
        return event

    def _update_preview(self, *_):
        if self._loading:
            return
        editable = self._can_edit_schedule()
        repeated = self.schedule_mode.currentData() != 'single'
        self.schedule_mode.setEnabled(editable)
        self.schedule_mode.setVisible(not self.common_fields_only)
        self.repeat.setVisible(repeated and not self.common_fields_only)
        self.frequency_row.itemAt(1).spacerItem().changeSize(round(14 * self.preferences.font_scale / 100) if repeated else 0, 0)
        self.repeat.setEnabled(editable)
        regular = repeated and self._draft.kind in REGULAR_KINDS
        self.recurrence_details.setVisible(repeated and not self.common_fields_only)
        self.recurrence_details.setCurrentWidget(self.regular_controls if regular else self.custom_controls)
        self.regular_controls.setEnabled(editable)
        self.repeat_count.setEnabled(editable and not self.no_end.isChecked())
        self.repeat_until.setEnabled(editable and not self.no_end.isChecked())
        self.repeat_count.setToolTip('Not used for an indefinite pattern.' if self.no_end.isChecked()
                                    else 'Total occurrences, including the primary event.')
        self.repeat_until.setToolTip('Not used for an indefinite pattern.' if self.no_end.isChecked()
                                    else 'Final recurrence date')
        self.custom_controls.setEnabled(editable)
        self.custom_input.setCurrentIndex(1 if self._draft.kind == 'advanced' else 0)
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
            primary = replace(primary, recurrence_id=recurrence_key(nominal, projected.all_day))
        elif projected and self.series and self.scope.currentData() == 'group':
            primary = replace(primary, recurrence_id=recurrence_key(projected.start, projected.all_day))
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
                full_day_shortcut=self.scope_bindings.get('full_day', 'F'))
            picker.accepted.connect(lambda: self._accept_primary_range(picker.result_start, picker.result_end))
            self.range_picker = picker
            picker.open()
        self._report(open_picker, self.start if endpoint == 'start' else self.end)

    def _accept_primary_range(self, start, end):
        def accept():
            self._set_range(start, end)
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
            picker = DateTimeRangePicker(start, start + self._duration, preferences=self.preferences,
                calendar_color=self._calendar_color(), parent=self, all_day=self.all_day.isChecked(),
                full_day_shortcut=self.scope_bindings.get('full_day', 'F'))
            picker.setWindowTitle('Add occurrence')
            picker.accepted.connect(lambda: self._add_range(picker.result_start, picker.result_end))
            self.range_picker = picker
            picker.open()
        self._report(open_picker)

    def _add_range(self, start, end):
        def add():
            master = self._project_preview(self.build_events()[0])
            self._draft.add_occurrence(master, start, end,
                reference_start=self.value(self.start), reference_end=self.value(self.end))
            self._after_graphical()
        self._report(add)

    def edit_occurrence(self, occurrence):
        if self.preview.is_primary(occurrence):
            self.open_range_picker('start')
            return
        if not self._can_edit_schedule():
            return
        start, end = occurrence.start, occurrence.end
        if not occurrence.all_day:
            start, end = start.astimezone(local_zone()), end.astimezone(local_zone())
        picker = DateTimeRangePicker(start, end, preferences=self.preferences,
            calendar_color=self._calendar_color(), parent=self, all_day=self.all_day.isChecked(),
            full_day_shortcut=self.scope_bindings.get('full_day', 'F'))
        picker.setWindowTitle('Edit occurrence')
        def apply():
            master = self._project_preview(self.build_events()[0])
            self._draft.edit_occurrence(master, occurrence, picker.result_start, picker.result_end,
                reference_start=self.value(self.start), reference_end=self.value(self.end))
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
            start_text=self.start.text(), end_text=self.end.text(), title=self.title.text(), location=self.location.text(),
            notes=self.description.toPlainText(), all_day=self.all_day.isChecked(), timed_range=self._timed_range,
            mode=self.schedule_mode.currentData(), draft=deepcopy(self._draft), repeat_draft=deepcopy(self._repeat_draft),
            custom_draft=deepcopy(self._custom_draft), regular_values=deepcopy(self._regular_values),
            reset_schedule=self._reset_schedule, repeat_reset=self._repeat_reset,
            schedule_error=self._schedule_error, interval=self.interval.value(), count=self.repeat_count.value(),
            until_text=self.repeat_until.text(), pattern_text=self.pattern.text())

    def _restore(self, state):
        self.all_day.setChecked(state['all_day'])
        self._timed_range = state['timed_range']
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
        start_changed = start != self._last_start
        end_changed = end != self._last_end
        if start_changed and not end_changed:
            end = start + self._duration
        elif end_changed and end.timestamp() <= start.timestamp():
            start = end - self._duration
        previous_duration = format_duration(self._duration, all_day=self.all_day.isChecked()) if self._duration else '0min'
        if self.duration.text().strip() != previous_duration:
            self._validation_field = self.duration
            end = start + parse_duration(self.duration.text(), all_day=self.all_day.isChecked())
        self._set_range(start, end)
        self._sync_ending()

    def show_error(self, message, field=None):
        super().show_error(message, field)
        target = field or self._validation_field
        if target is not None:
            self.controls_scroll.ensureWidgetVisible(target)
