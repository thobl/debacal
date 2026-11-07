"""Editors for events, calendar sources, shortcuts, and operation history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QDate, QTime, QDateTime, QTimeZone, QTimer, Signal, QEvent, QSize, QRectF
from PySide6.QtGui import QKeySequence, QColor, QPixmap, QIcon, QShortcut, QTextCharFormat, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QTextEdit,
    QComboBox, QDateTimeEdit, QDateEdit, QSpinBox, QCheckBox, QLabel, QPushButton, QDialogButtonBox, QButtonGroup,
    QListWidget, QListWidgetItem, QScrollArea, QWidget, QColorDialog, QAbstractSpinBox,
)

from .models import Event, Calendar
from .patterns import expand_sequence, reconstruct_sequence, PatternError
from .timeutil import local_zone
from .preferences import Preferences
from .theme import scheme_for
from .event_style import source_outline
from .calendar_appearance import CalendarDateGrid, weekend_text, weekend_fill
from .series_preview import SeriesPreview
from .event_scope import normalize_scope_modifiers, expanded_scope_bindings, DEFAULT_MODE_BINDINGS, MODE_ACTIONS


def hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty('muted', True)
    return label


def _calendar_color_icon(value, preferences):
    """Use the source list's square swatch without dimming an editing target."""
    colors = scheme_for(preferences)
    source = QColor(value)
    if not source.isValid():
        source = QColor(colors.accent)
    scale = preferences.font_scale / 100
    size = round(16 * scale)
    swatch = QPixmap(size * 2, size * 2)
    swatch.setDevicePixelRatio(2)
    swatch.fill(Qt.GlobalColor.transparent)
    painter = QPainter(swatch)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(source)
    painter.setPen(QPen(QColor(source_outline(source.darker(145).name(), colors.panel)), max(1, scale)))
    painter.drawRoundedRect(QRectF(2 * scale, 2 * scale, 12 * scale, 12 * scale), 2 * scale, 2 * scale)
    painter.end()
    icon = QIcon(swatch)
    icon.addPixmap(swatch, QIcon.Mode.Disabled)
    return icon


def _style_date_popup(field, preferences):
    scheme = scheme_for(preferences)
    field.setCalendarWidget(CalendarDateGrid(preferences, field))
    for day in range(1, 8):
        format = QTextCharFormat()
        format.setForeground(QColor(weekend_text(day - 1, preferences, scheme)))
        format.setBackground(QColor(weekend_fill(day - 1, preferences, scheme) or scheme.background))
        field.calendarWidget().setWeekdayTextFormat(Qt.DayOfWeek(day), format)


class FormDialog(QDialog):
    """Native form navigation with one explicit primary action."""
    def configure_keyboard(self, fields, buttons, primary, reserved=()):
        self._primary_action = primary
        self._validation_field = None
        for field in [*fields, *buttons]:
            if isinstance(field, QTextEdit):
                field.setTabChangesFocus(True)
            else:
                field.installEventFilter(self)
                if isinstance(field, QAbstractSpinBox):
                    field.lineEdit().installEventFilter(self)
        chain = [*fields, *buttons]
        for before, after in zip(chain, chain[1:]):
            self.setTabOrder(before, after)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.save_shortcuts = []
        reserved = {QKeySequence(value).toString(QKeySequence.SequenceFormat.PortableText) for value in reserved if value}
        def conflicts(sequence):
            return any(sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                       or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch for value in reserved)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (QLabel, QPushButton, QCheckBox)):
                mnemonic = QKeySequence.mnemonic(widget.text())
                if not mnemonic.isEmpty() and conflicts(mnemonic):
                    widget.setText(widget.text().replace('&', ''))
        for key in ('Ctrl+Return', 'Ctrl+Enter'):
            if conflicts(QKeySequence(key)):
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.save)
            self.save_shortcuts.append(shortcut)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
            if isinstance(obj, QComboBox) and obj.view().isVisible():
                return super().eventFilter(obj, event)
            spin = obj if isinstance(obj, QAbstractSpinBox) else obj.parent()
            if isinstance(spin, QDateTimeEdit) and spin.calendarWidget().isVisible():
                return super().eventFilter(obj, event)
            if isinstance(spin, QAbstractSpinBox):
                spin.interpretText()
            obj.click() if isinstance(obj, QPushButton) else self._primary_action()
            return True
        return super().eventFilter(obj, event)

    def show_error(self, message, field=None):
        self.error.setText(str(message))
        field = field or self._validation_field
        if field is not None and field.isEnabled() and field.isVisible():
            field.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(field, QLineEdit):
                field.selectAll()


class EventDialog(FormDialog):
    """One schedule editor: rules, chosen dates and alternatives share a master."""
    mode_changed = Signal(str)
    def __init__(self, calendars, start, end=None, event=None, parent=None,
                 preferences=None, edit_scope='single', series=None, default_calendar_id=None,
                 scope_shortcut='Alt+S', scope_modifiers=None, scope_bindings=None, common_fields_only=False):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self.setLocale(self.preferences.qt_locale())
        self.setWindowTitle('Edit event' if event else 'Create event')
        self.resize(1080, 740)
        self.original, self.series = event, series
        self.common_fields_only = bool(common_fields_only)
        self.result_events = []
        self.schedule_replacement = False
        self.has_group = bool(series or (event and (event.group_id or event.series_id)))
        self.scope_modifiers = normalize_scope_modifiers(scope_modifiers)
        edit_scope = edit_scope if event and edit_scope in ('single', 'group', 'future') else 'single'
        self.scope_bindings = DEFAULT_MODE_BINDINGS | (scope_bindings or {})
        self._scope_sources = {}
        source = self.schedule_source(edit_scope)
        reference = series if series and edit_scope == 'group' else event
        self._scope_name = edit_scope
        self._scope_values = {}
        self._conversion_notice = ''
        self._stored_sequence_starts = None
        self._sequence_reencode = False
        self._sequence_clicked = False
        self._setting_pattern_from_click = False
        self._click_excluded_dates = None
        self._click_anchor = None
        self._click_template = None
        outer = QVBoxLayout(self)
        columns = QHBoxLayout()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        self.form = form = QFormLayout()
        self.scope = QComboBox(self)
        self.scope.addItem('Only this occurrence' if series else 'Only this event', 'single')
        self.scope.addItem(f"All events ({self.scope_modifiers['all']})", 'group')
        self.scope.addItem(f"This and following ({self.scope_modifiers['following']})", 'future')
        self.scope.setCurrentIndex(self.scope.findData(edit_scope))
        self.scope.hide()
        self.scope_buttons = {}
        self.scope_button_group = QButtonGroup(self)
        self.scope_button_group.setExclusive(True)
        mode_widget = QWidget()
        mode_row = QHBoxLayout(mode_widget)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(0)
        for action, label in (('scope_single', 'Single'), ('scope_all', 'All'), ('scope_following', 'Following')):
            scope = MODE_ACTIONS[action]
            shortcut = self.scope_bindings.get(action, '')
            button = QPushButton(label + (f'  {shortcut}' if shortcut else ''))
            button.setCheckable(True)
            button.setChecked(scope == edit_scope)
            button.clicked.connect(lambda _checked=False, value=scope: self.scope.setCurrentIndex(self.scope.findData(value)))
            self.scope_button_group.addButton(button)
            self.scope_buttons[scope] = button
            mode_row.addWidget(button)
        self.scope_controls = mode_widget
        if event:
            form.addRow('Selection mode', mode_widget)
        else:
            mode_widget.hide()
        self.title = QLineEdit(reference.title if reference else '')
        self.calendars = list(calendars)
        self.calendar = QComboBox()
        scale = self.preferences.font_scale / 100
        self.calendar.setIconSize(QSize(round(16 * scale), round(16 * scale)))
        for cal in self.calendars:
            if cal.kind != 'ics' and not cal.read_only:
                self.calendar.addItem(_calendar_color_icon(cal.color, self.preferences), cal.name, cal.id)
        if not event and default_calendar_id:
            index = self.calendar.findData(default_calendar_id)
            if index >= 0:
                self.calendar.setCurrentIndex(index)
        if event:
            self.calendar.setCurrentIndex(self.calendar.findData(event.calendar_id))
            self.calendar.setEnabled(False)
        self.start, self.end = QDateTimeEdit(), QDateTimeEdit()
        for field, value in ((self.start, reference.start if reference else start),
                             (self.end, reference.end if reference else end or start + timedelta(hours=1))):
            field.setLocale(self.preferences.qt_locale())
            field.setCalendarPopup(True)
            _style_date_popup(field, self.preferences)
            field.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek(self.preferences.first_weekday + 1))
            field.setDisplayFormat(self.preferences.qt_datetime_pattern())
            field.setTimeZone(QTimeZone.utc())
            wall = value if reference and reference.all_day else value.astimezone(local_zone())
            field.setDateTime(QDateTime(QDate(wall.year, wall.month, wall.day),
                QTime(wall.hour, wall.minute, wall.second, wall.microsecond // 1000), QTimeZone.utc()))
        self.all_day = QCheckBox('All-&day event')
        self.all_day.setChecked(reference.all_day if reference else False)
        self.location = QLineEdit(reference.location if reference else '')
        self.description = QTextEdit()
        self.description.setPlainText(reference.description if reference else '')
        self.description.setMaximumHeight(70)
        for label, field in [('&Title', self.title), ('&Calendar', self.calendar), ('Sta&rts', self.start),
                             ('&Ends', self.end), ('', self.all_day), ('&Location', self.location), ('&Notes', self.description)]:
            form.addRow(label, field)
        self.repeat = QComboBox()
        for name, value in [('Once', ''), ('Daily', 'DAILY'), ('Weekly', 'WEEKLY'),
                            ('Monthly', 'MONTHLY'), ('Yearly', 'YEARLY'),
                            ('Custom sequence', 'RDATE'), ('Advanced rule…', 'CUSTOM')]:
            self.repeat.addItem(name, value)
        self.interval = QSpinBox()
        self.interval.setRange(1, 999)
        self.weekday_container = QWidget()
        weekrow = QHBoxLayout(self.weekday_container)
        weekrow.setContentsMargins(0, 0, 0, 0)
        weekrow.setSpacing(2)
        self.weekdays = []
        self._weekdays_auto = not bool(source and source.recurrence_rule)
        initial_weekday = (source.start if source and source.recurrence_rule else start).astimezone(local_zone()).weekday()
        for day in range(7):
            box = QCheckBox(self.preferences.weekday_name(day))
            box.setChecked(day == initial_weekday)
            self.weekdays.append(box)
            weekrow.addWidget(box)
        self.repeat_end = QComboBox()
        for name, value in [('Never', ''), ('After a number of events', 'COUNT'), ('On a date', 'UNTIL')]:
            self.repeat_end.addItem(name, value)
        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 100000)
        self.repeat_count.setValue(10)
        self.repeat_until = QDateEdit(QDate(start.year + 1, start.month, min(start.day, 28)))
        self.repeat_until.setLocale(self.preferences.qt_locale())
        self.repeat_until.setCalendarPopup(True)
        _style_date_popup(self.repeat_until, self.preferences)
        self.repeat_until.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek(self.preferences.first_weekday + 1))
        self.repeat_until.setDisplayFormat(self.preferences.qt_date_pattern())
        self.custom_rule = QLineEdit()
        self.custom_rule.setPlaceholderText('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=17')
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText('4*(+2) +3 3*+2')
        self.pattern_help = hint('')
        self.advanced_help = hint('Advanced: RRULE is the standard iCalendar recurrence rule. The regular controls generate it for you; this field is for rules the controls cannot express.')
        self.load_schedule(source)
        for label, field in [('Re&peat', self.repeat), ('E&very', self.interval), ('Days', self.weekday_container),
                             ('End condition', self.repeat_end), ('C&ount', self.repeat_count), ('&Until', self.repeat_until),
                             ('Se&quence', self.pattern), ('R&ule', self.custom_rule)]:
            form.addRow(label, field)
        content.addLayout(form)
        self.common_fields_help = hint('Changes to common fields and relative times apply to the selection. '
                                       'Select one series to change its recurrence.')
        self.common_fields_help.setVisible(self.common_fields_only)
        content.addWidget(self.common_fields_help)
        content.addWidget(self.pattern_help)
        content.addWidget(self.advanced_help)
        self.candidate = QCheckBox('Alternative slots — choosing one removes the other choices')
        self.candidate.setChecked(reference.candidate if reference else False)
        content.addWidget(self.candidate)
        content.addStretch()
        scroll.setWidget(body)
        columns.addWidget(scroll, 1)
        self.preview = SeriesPreview()
        self.preview.date_toggled.connect(self.toggle_date)
        columns.addWidget(self.preview, 1)
        outer.addLayout(columns, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('error', True)
        outer.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.buttons = buttons
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.save_button.setText('Save')
        self.cancel_button.setText('Cancel')
        self.save_button.setToolTip('Enter saves; Ctrl+Enter also saves from Notes.')
        self.pattern.textChanged.connect(self.pattern_changed)
        self.scope.currentIndexChanged.connect(self.scope_changed)
        for signal in (self.start.dateTimeChanged, self.end.dateTimeChanged, self.calendar.currentIndexChanged,
                       self.repeat.currentIndexChanged, self.interval.valueChanged, self.repeat_end.currentIndexChanged,
                       self.repeat_count.valueChanged, self.repeat_until.dateChanged, self.custom_rule.textChanged,
                       self.candidate.toggled, self.all_day.toggled):
            signal.connect(self.update_preview)
        for box in self.weekdays:
            box.toggled.connect(self.weekday_changed)
        self.scope_shortcut = QShortcut(QKeySequence(scope_shortcut), self)
        self.scope_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.scope_shortcut.setEnabled(self.has_group and bool(scope_shortcut))
        self.scope_shortcut.activated.connect(self.cycle_scope)
        self.mode_shortcuts = {}
        for action, scope in MODE_ACTIONS.items():
            shortcut = QShortcut(QKeySequence(self.scope_bindings.get(action, '')), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setEnabled(bool(event and self.scope_bindings.get(action)))
            shortcut.activated.connect(lambda value=scope: self.scope.setCurrentIndex(self.scope.findData(value)))
            self.mode_shortcuts[action] = shortcut
        self.configure_keyboard([*self.scope_buttons.values(), self.title, self.calendar, self.start, self.end,
            self.all_day, self.location, self.description, self.repeat, self.interval, *self.weekdays,
            self.repeat_end, self.repeat_count, self.repeat_until, self.pattern, self.custom_rule,
            self.candidate, self.preview.previous_button, self.preview.next_button, self.preview.first_button],
            [self.save_button, self.cancel_button], self.save,
            [*self.scope_bindings.values(), scope_shortcut] if event else ())
        self.save_button.setDefault(True)
        self.update_preview()
        self.title.setFocus()
        self.title.selectAll()

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

    def load_schedule(self, source):
        from .recurrence import event_zone
        self._authoring_zone = (event_zone(source) if source and source.schedule_expression
                                and source.schedule_syntax == 2 else None)
        self._conversion_notice = ''
        self._stored_sequence_starts = None
        self._sequence_reencode = False
        self._sequence_clicked = False
        self._setting_pattern_from_click = False
        self._click_excluded_dates = None
        self._click_anchor = None
        self._click_template = None
        self.repeat.setCurrentIndex(self.repeat.findData(''))
        self.interval.setValue(1)
        self.repeat_end.setCurrentIndex(0)
        self.repeat_count.setValue(10)
        self.pattern.clear()
        self.custom_rule.clear()
        self._weekdays_auto = not bool(source and source.recurrence_rule)
        day = source.start.astimezone(local_zone()).weekday() if source else self.start.date().toPython().weekday()
        for index, box in enumerate(self.weekdays):
            box.setChecked(index == day)
        if source and (source.schedule_expression or (source.recurrence_dates and not source.recurrence_rule)):
            self.repeat.setCurrentIndex(self.repeat.findData('RDATE'))
            from .recurrence import base_schedule_starts
            try:
                zone = self.sequence_zone()
                starts = [value.replace(tzinfo=zone) if source.all_day else value.astimezone(zone)
                          for value in base_schedule_starts(source, limit=500)]
                self._stored_sequence_starts = starts
                faithful = False
                if source.schedule_expression and source.schedule_syntax == 2:
                    try:
                        faithful = {value.timestamp() for value in expand_sequence(source.schedule_expression, starts[0])} == {
                            value.timestamp() for value in starts}
                    except PatternError:
                        pass
                if faithful:
                    expression = source.schedule_expression
                else:
                    expression = reconstruct_sequence(starts)
                    if not expression and source.schedule_expression:
                        expression = self.absolute_step(starts[0], starts[0])
                    self._sequence_reencode = True
                    self._conversion_notice = ('The sequence was refreshed from the current calendar dates. '
                                               if source.schedule_syntax == 2 else 'Existing scheduled dates are preserved in the new notation. ')
                self.pattern.setText(expression)
            except ValueError:
                self._conversion_notice = 'Existing precise times are preserved. Enter a new sequence to replace them. '
        elif source and source.recurrence_rule:
            parts = dict(part.split('=', 1) for part in source.recurrence_rule.split(';') if '=' in part)
            simple_days = not parts.get('BYDAY') or all(value in ['MO','TU','WE','TH','FR','SA','SU'] for value in parts['BYDAY'].split(','))
            allowed = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL'} | ({'BYDAY'} if parts.get('FREQ') == 'WEEKLY' and simple_days else set())
            if set(parts) <= allowed and self.repeat.findData(parts.get('FREQ')) >= 0:
                self.repeat.setCurrentIndex(self.repeat.findData(parts['FREQ']))
                self.interval.setValue(int(parts.get('INTERVAL', 1)))
                if 'BYDAY' in parts:
                    names = ['MO','TU','WE','TH','FR','SA','SU']
                    for day, box in enumerate(self.weekdays):
                        box.setChecked(names[day] in parts['BYDAY'].split(','))
                if 'COUNT' in parts:
                    self.repeat_end.setCurrentIndex(self.repeat_end.findData('COUNT'))
                    self.repeat_count.setValue(int(parts['COUNT']))
                elif 'UNTIL' in parts:
                    self.repeat_end.setCurrentIndex(self.repeat_end.findData('UNTIL'))
                    value = parts['UNTIL'][:8]
                    self.repeat_until.setDate(QDate(int(value[:4]), int(value[4:6]), int(value[6:8])))
            else:
                self.repeat.setCurrentIndex(self.repeat.findData('CUSTOM'))
            self.custom_rule.setText(source.recurrence_rule)
        self._initial_pattern = self.pattern.text()

    def cycle_scope(self):
        if self.original:
            self.scope.setCurrentIndex((self.scope.currentIndex() + 1) % self.scope.count())

    @staticmethod
    def validate_time(value):
        wall = value.replace(tzinfo=None)
        if value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) != wall:
            raise ValueError(f'The local time {wall:%d %b %Y %H:%M} falls in a daylight-saving clock change. Choose another time.')
        return value

    def value(self, field):
        self._validation_field = field
        reference = self.edit_reference
        calendar_date = bool(self.all_day.isChecked() and reference and reference.all_day)
        if calendar_date:
            from .recurrence import event_zone
            zone = event_zone(reference)
        else:
            zone = local_zone()
        value = field.dateTime().toPython().replace(tzinfo=zone)
        if self.edit_reference:
            previous = self.edit_reference.start if field is self.start else self.edit_reference.end
            previous = previous.astimezone(zone)
            displayed = previous.replace(microsecond=previous.microsecond // 1000 * 1000, tzinfo=None)
            if displayed == value.replace(tzinfo=None):
                value = previous
            elif value.microsecond == previous.microsecond // 1000 * 1000:
                value = value.replace(microsecond=previous.microsecond)
        return self.validate_time(value)

    def sequence_zone(self):
        return self._authoring_zone or local_zone()

    def sequence_calendar_day(self, value):
        """Use the same date as the local preview, keeping DATE values floating."""
        return value.date() if self.all_day.isChecked() else value.astimezone(local_zone()).date()

    def sequence_date_start(self, day, anchor):
        """Place the authoring clock on a selected local calendar date."""
        if self.all_day.isChecked():
            return self.validate_time(datetime.combine(day, anchor.timetz()))
        expected = day + (anchor.date() - self.sequence_calendar_day(anchor))
        invalid = None
        for offset in (0, -1, 1, -2, 2):
            value = datetime.combine(expected + timedelta(days=offset), anchor.timetz())
            if self.sequence_calendar_day(value) != day:
                continue
            try:
                return self.validate_time(value)
            except ValueError as exc:
                invalid = exc
        if invalid is not None:
            raise invalid
        raise ValueError('The sequence time cannot fall on this calendar date in its original timezone. Choose another date.')

    @property
    def edit_reference(self):
        return self.series if self.series and self.scope.currentData() == 'group' else self.original

    @staticmethod
    def absolute_step(value, anchor):
        day = f'{value.day}.{value.month}.{value.year}'
        if value.time() != anchor.time():
            return value.strftime('%H:%M-') + day
        return day

    def scope_changed(self, *_):
        scope = self.scope.currentData()
        if scope == self._scope_name:
            return
        try:
            source = self.schedule_source(scope)
        except (ValueError, OverflowError) as exc:
            blocked = self.scope.blockSignals(True)
            self.scope.setCurrentIndex(self.scope.findData(self._scope_name))
            self.scope.blockSignals(blocked)
            self.scope_buttons[self._scope_name].setChecked(True)
            self.error.setText(str(exc))
            return
        general = (self.start, self.end, self.title, self.location, self.description, self.all_day, self.candidate)
        schedule = (self.repeat, self.interval, self.repeat_end, self.repeat_count, self.repeat_until,
                    self.pattern, self.custom_rule, *self.weekdays)
        state_names = ('_initial_pattern', '_conversion_notice', '_stored_sequence_starts',
                       '_sequence_reencode', '_sequence_clicked', '_click_excluded_dates',
                       '_click_anchor', '_click_template', '_weekdays_auto', '_authoring_zone')
        self._scope_values[self._scope_name] = {
            'fields': (self.start.dateTime(), self.end.dateTime(), self.title.text(), self.location.text(),
                       self.description.toPlainText(), self.all_day.isChecked(), self.candidate.isChecked()),
            'schedule': (self.repeat.currentIndex(), self.interval.value(), self.repeat_end.currentIndex(),
                         self.repeat_count.value(), self.repeat_until.date(), self.pattern.text(),
                         self.custom_rule.text(), tuple(box.isChecked() for box in self.weekdays)),
            'state': {name: getattr(self, name) for name in state_names},
        }
        previous_blocks = [control.blockSignals(True) for control in (*general, *schedule)]
        try:
            cached = self._scope_values.get(scope)
            reference = self.edit_reference
            if cached is None:
                times = []
                for value in (reference.start, reference.end):
                    value = value if reference.all_day else value.astimezone(local_zone())
                    times.append(QDateTime(QDate(value.year, value.month, value.day),
                        QTime(value.hour, value.minute, value.second, value.microsecond // 1000), QTimeZone.utc()))
                values = (*times, reference.title, reference.location, reference.description,
                          reference.all_day, reference.candidate)
            else:
                values = cached['fields']
            self.start.setDateTime(values[0])
            self.end.setDateTime(values[1])
            self.title.setText(values[2])
            self.location.setText(values[3])
            self.description.setPlainText(values[4])
            self.all_day.setChecked(values[5])
            self.candidate.setChecked(values[6])
            if cached is None:
                self.load_schedule(source)
            else:
                repeat, interval, ending, count, until, pattern, rule, weekdays = cached['schedule']
                self.repeat.setCurrentIndex(repeat)
                self.interval.setValue(interval)
                self.repeat_end.setCurrentIndex(ending)
                self.repeat_count.setValue(count)
                self.repeat_until.setDate(until)
                self.pattern.setText(pattern)
                self.custom_rule.setText(rule)
                for box, checked in zip(self.weekdays, weekdays):
                    box.setChecked(checked)
                for name, value in cached['state'].items():
                    setattr(self, name, value)
            self._scope_name = scope
        finally:
            for control, blocked in zip((*general, *schedule), previous_blocks):
                control.blockSignals(blocked)
        self.update_preview()
        for value, button in self.scope_buttons.items():
            button.setChecked(value == scope)
        self.mode_changed.emit(scope)

    def sequence_starts(self):
        anchor = self.value(self.start)
        zone = self.sequence_zone()
        if self.all_day.isChecked():
            anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=zone)
        else:
            anchor = anchor.astimezone(zone)
        source = self.schedule_source()
        text = self.pattern.text()
        if source and text == self._initial_pattern and not self._sequence_clicked:
            # Moving a whole series keeps its existing spacing. Explicit dates
            # are rewritten on save rather than left pointing at the old days.
            from .recurrence import key_datetime
            original = source.start.replace(tzinfo=zone) if source.all_day else source.start.astimezone(zone)
            if self._stored_sequence_starts is not None:
                starts = self._stored_sequence_starts
            else:
                starts = [original, *(key_datetime(key, source).replace(tzinfo=zone) if source.all_day
                                      else key_datetime(key, source).astimezone(zone) for key in source.recurrence_dates)]
            delta = anchor.replace(tzinfo=None) - original.replace(tzinfo=None)
            return [self.validate_time((value.replace(tzinfo=None) + delta).replace(tzinfo=anchor.tzinfo, fold=value.fold)) for value in starts]
        self._validation_field = self.pattern
        return expand_sequence(text, anchor)

    def dates(self):
        return sorted({self.sequence_calendar_day(value) for value in self.sequence_starts()})

    def pattern_changed(self, *_):
        if not self._setting_pattern_from_click:
            self._sequence_clicked = False
            self._click_excluded_dates = None
            self._click_anchor = None
            self._click_template = None
        if self.pattern.text().strip() and self.repeat.currentData() != 'RDATE':
            self.repeat.setCurrentIndex(self.repeat.findData('RDATE'))
        self.update_preview()

    def weekday_changed(self, *_):
        self._weekdays_auto = False
        self.update_preview()

    def toggle_date(self, day):
        try:
            from .patterns import simplify_sequence
            from .recurrence import base_schedule_starts, key_datetime, recurrence_key
            if self.common_fields_only or (self.has_group and self.scope.currentData() == 'single'):
                raise ValueError('Choose All or Following to change the series dates.')
            template = self.preview.masters[0] if self.preview.masters else self.build_events()[0]
            if self.repeat.currentData() != 'RDATE':
                zone = self.sequence_zone()
                starts = [value.replace(tzinfo=zone) if template.all_day else value.astimezone(zone)
                          for value in base_schedule_starts(template, limit=500)]
            else:
                starts = self.sequence_starts()
            anchor = starts[0]
            exclusions = set(template.excluded_dates if template else ())
            matching = [value for value in starts if self.sequence_calendar_day(value) == day]
            fully_excluded = matching and all(recurrence_key(value, self.all_day.isChecked()) in exclusions for value in matching)
            if day == self.sequence_calendar_day(anchor) and not fully_excluded:
                self.error.setText('The initial event is fixed. Change Starts and Ends to move it.')
                return
            if fully_excluded:
                exclusions = {key for key in exclusions if self.sequence_calendar_day(key_datetime(key, template)) != day}
            elif matching:
                starts = [value for value in starts if self.sequence_calendar_day(value) != day]
                exclusions = {key for key in exclusions if self.sequence_calendar_day(key_datetime(key, template)) != day}
            else:
                starts.append(self.sequence_date_start(day, anchor))
            starts = [anchor, *sorted(starts[1:], key=lambda value: value.timestamp())]
            expression = simplify_sequence(starts)
            self._sequence_clicked = True
            self._click_excluded_dates = tuple(exclusions)
            self._click_anchor = anchor
            self._click_template = template
            self._setting_pattern_from_click = True
            blocked = self.repeat.blockSignals(True), self.pattern.blockSignals(True)
            try:
                self.repeat.setCurrentIndex(self.repeat.findData('RDATE'))
                self.pattern.setText(expression)
            finally:
                self.repeat.blockSignals(blocked[0])
                self.pattern.blockSignals(blocked[1])
                self._setting_pattern_from_click = False
            # setText emits nothing if the simplest result equals the initial
            # empty expression. That still represents an intentional date edit.
            self.update_preview()
        except ValueError as exc:
            self.error.setText(str(exc))

    def clicked_exclusions(self, starts):
        from .recurrence import key_datetime, recurrence_key
        if self._click_excluded_dates is None:
            return None
        anchor = starts[0]
        shift = anchor.replace(tzinfo=None) - self._click_anchor.replace(tzinfo=None)
        active = {recurrence_key(value, self.all_day.isChecked()) for value in starts}
        excluded = []
        for key in self._click_excluded_dates:
            zone = self.sequence_zone()
            value = key_datetime(key, self._click_template)
            value = value.replace(tzinfo=zone) if self.all_day.isChecked() else value.astimezone(zone)
            value = (value.replace(tzinfo=None) + shift).replace(tzinfo=anchor.tzinfo, fold=value.fold)
            moved = recurrence_key(value, self.all_day.isChecked())
            if moved in active:
                excluded.append(moved)
        return tuple(excluded)

    def recurrence_rule(self, start):
        kind = self.repeat.currentData()
        if kind in ('', 'RDATE'):
            return ''
        if kind == 'CUSTOM':
            self._validation_field = self.custom_rule
            rule = self.custom_rule.text().strip().upper().removeprefix('RRULE:')
            if not rule:
                raise ValueError('Enter a recurrence rule.')
            return rule
        rule = f'FREQ={kind};INTERVAL={self.interval.value()}'
        if kind == 'WEEKLY':
            names = ['MO','TU','WE','TH','FR','SA','SU']
            days = [names[index] for index, box in enumerate(self.weekdays) if box.isChecked()]
            if not days:
                self._validation_field = self.weekdays[0]
                raise ValueError('Choose at least one weekday.')
            rule += ';BYDAY=' + ','.join(days)
        if self.repeat_end.currentData() == 'COUNT':
            rule += f';COUNT={self.repeat_count.value()}'
        elif self.repeat_end.currentData() == 'UNTIL':
            day = self.repeat_until.date().toPython()
            if day < start.date():
                self._validation_field = self.repeat_until
                raise ValueError('The last recurrence date must not precede the start.')
            until = datetime.combine(day, datetime.max.time(), start.tzinfo).astimezone(timezone.utc)
            rule += ';UNTIL=' + until.strftime('%Y%m%dT%H%M%SZ')
        return rule

    def build_events(self):
        start, end = self.value(self.start), self.value(self.end)
        if end.timestamp() < start.timestamp():
            self._validation_field = self.end
            raise ValueError('End must not be before start.')
        source = self.schedule_source()
        kind = self.repeat.currentData()
        single = self.has_group and self.scope.currentData() == 'single'
        if kind == 'RDATE' and not single:
            zone = self.sequence_zone()
            if self.all_day.isChecked():
                start, end = start.replace(tzinfo=zone), end.replace(tzinfo=zone)
            else:
                start, end = start.astimezone(zone), end.astimezone(zone)
        configuration_changed = not single and kind == 'RDATE' and (self._sequence_clicked or not source or (not source.recurrence_dates and not source.schedule_expression) or self.pattern.text() != self._initial_pattern)
        dates_changed = configuration_changed or (not single and kind == 'RDATE' and self._sequence_reencode)
        self.schedule_replacement = bool(self.series and dates_changed and self.scope.currentData() in ('group', 'future'))
        rdates = source.recurrence_dates if source and source.recurrence_rule and kind and not single else ()
        rule = '' if single else self.recurrence_rule(start)
        if kind == 'RDATE' and dates_changed:
            from .recurrence import schedule_fields
            starts = self.sequence_starts()
            if self.all_day.isChecked() and any(value.time() != datetime.min.time() for value in starts):
                self._validation_field = self.pattern
                raise ValueError('Time entries require a timed event. Turn off All-day event, or use date-only steps.')
            fields = schedule_fields(starts, all_day=self.all_day.isChecked())
            rule, rdates = fields['recurrence_rule'], fields['recurrence_dates']
            start = starts[0]
        elif not single and kind == 'RDATE' and source:
            rdates = source.recurrence_dates
            rule = source.recurrence_rule
        align_weekday = True
        if source and self.edit_reference and kind == 'WEEKLY' and start == self.edit_reference.start:
            from .recurrence import rules_equivalent
            # A selected RDATE or detached exception need not fall on a rule
            # weekday. A title-only edit must preserve that separate anchor.
            align_weekday = not rules_equivalent(rule, source.recurrence_rule, source)
        if not single and kind == 'WEEKLY' and align_weekday:
            chosen = {index for index, box in enumerate(self.weekdays) if box.isChecked()}
            if chosen and start.weekday() not in chosen:
                offset = min((day - start.weekday()) % 7 for day in chosen)
                start += timedelta(days=offset)
                end += timedelta(days=offset)
        if self.all_day.isChecked():
            days = max(1, (end.date() - start.date()).days)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=days)
            if dates_changed:
                rdates = tuple(value[:10] for value in rdates)
        self._validation_field = self.start
        self.validate_time(start)
        self._validation_field = self.end
        self.validate_time(end)
        if kind == 'RDATE' and dates_changed:
            self._validation_field = self.pattern
            duration = end.replace(tzinfo=None) - start.replace(tzinfo=None)
            for value in starts:
                finish = (value + duration) if duration > timedelta(0) else datetime.fromtimestamp(
                    value.timestamp() + end.timestamp() - start.timestamp(), value.tzinfo)
                self.validate_time(value)
                self.validate_time(finish)
        expression = self.pattern.text() if kind == 'RDATE' and not single else ''
        if expression and dates_changed and source and self.pattern.text() == self._initial_pattern:
            from .patterns import shift_schedule_expression
            zone = self.sequence_zone()
            previous = source.start.replace(tzinfo=zone) if source.all_day else source.start.astimezone(zone)
            shift = start.replace(tzinfo=None) - previous.replace(tzinfo=None)
            expression, _ = shift_schedule_expression(expression, previous.date().isoformat(), shift, starts, syntax=2)
        values = dict(id=self.original.id if self.original else str(uuid4()),
            calendar_id=self.calendar.currentData() or 'local', title=self.title.text().strip() or 'Untitled event',
            start=start, end=end, description=self.description.toPlainText(), location=self.location.text(),
            group_id=self.original.group_id if self.original else None, candidate=self.candidate.isChecked(),
            all_day=self.all_day.isChecked(), recurrence_rule=rule, recurrence_dates=rdates,
            excluded_dates=() if configuration_changed or single else (source.excluded_dates if source else ()),
            recurrence_overrides=source.recurrence_overrides if source and not single else {},
            schedule_expression=expression,
            expression_anchor=((source.start.date() if source.all_day else source.start.astimezone(self.sequence_zone()).date()).isoformat()
                               if source and not dates_changed else start.date().isoformat()) if kind == 'RDATE' and not single else '',
            schedule_syntax=2 if kind == 'RDATE' and not single else 1,
            timezone_id=(getattr(start.tzinfo, 'key', '') if kind == 'RDATE' and dates_changed
                         else source.timezone_id if source and source.timezone_id else getattr(local_zone(), 'key', '')))
        if kind == 'RDATE' and dates_changed:
            clicked_exclusions = self.clicked_exclusions(starts)
            if clicked_exclusions is not None:
                values['excluded_dates'] = clicked_exclusions
        event = replace(self.original, **values) if self.original else Event(**values)
        if event.recurring:
            from .recurrence import recurrence_set
            self._validation_field = self.custom_rule if kind == 'CUSTOM' else self.pattern if kind == 'RDATE' else self.repeat
            recurrence_set(event)
        return [event]

    def update_preview(self, *_):
        kind = self.repeat.currentData()
        enabled = (not self.has_group or (self.series is not None and self.scope.currentData() in ('group', 'future'))) and not self.common_fields_only
        if self._weekdays_auto and kind == 'WEEKLY':
            # Reading the unvalidated wall date keeps live editing responsive
            # while the user is midway through entering a DST-gap time.
            day = self.start.date().toPython().weekday()
            for index, box in enumerate(self.weekdays):
                box.blockSignals(True)
                box.setChecked(index == day)
                box.blockSignals(False)
        self.repeat.setEnabled(enabled)
        self.repeat.setVisible(not self.common_fields_only)
        self.form.labelForField(self.repeat).setVisible(not self.common_fields_only)
        self.interval.setSuffix({'DAILY': ' day(s)', 'WEEKLY': ' week(s)', 'MONTHLY': ' month(s)', 'YEARLY': ' year(s)'}.get(kind, ''))
        for field, visible in ((self.interval, kind in ('DAILY','WEEKLY','MONTHLY','YEARLY')),
                (self.weekday_container, kind == 'WEEKLY'), (self.repeat_end, kind in ('DAILY','WEEKLY','MONTHLY','YEARLY')),
                (self.repeat_count, kind in ('DAILY','WEEKLY','MONTHLY','YEARLY') and self.repeat_end.currentData() == 'COUNT'),
                (self.repeat_until, kind in ('DAILY','WEEKLY','MONTHLY','YEARLY') and self.repeat_end.currentData() == 'UNTIL'),
                (self.custom_rule, kind == 'CUSTOM'), (self.pattern, kind == 'RDATE')):
            visible = visible and not self.common_fields_only
            field.setVisible(visible)
            field.setEnabled(enabled)
            self.form.labelForField(field).setVisible(visible)
        self.pattern_help.setVisible(kind == 'RDATE' and not self.common_fields_only)
        self.advanced_help.setVisible(kind == 'CUSTOM' and not self.common_fields_only)
        self.preview.enable_date_editing(False)
        authoring_note = ''
        if self._authoring_zone is not None and not self.all_day.isChecked() and str(self._authoring_zone) != str(local_zone()):
            authoring_note = (f'Sequence dates and times use {self._authoring_zone}. '
                              f'Starts, Ends, and the calendar show {local_zone()}.\n')
        self.pattern_help.setText(authoring_note + self._conversion_notice
            + 'The initial event is always included. Each step is evaluated from the previous event. '
            '+2d = 2 days; +3 = 3 weeks; '
            '+1m / +1y = 1 month / year.\n'
            'Dates: 13. or 3.2.; times: 10:00 or 10:00-3.2.2027. Omitted date parts roll forward. '
            'A full date is exact. Duration stays the same.\n'
            'Repeat: 4*(+2) +3 3*+2. Separate steps with spaces or commas. '
            'Click calendar dates to add or remove slots; clicks choose a compact equivalent sequence. '
            'Typing keeps your exact text. The initial event stays fixed.')
        try:
            events = self.build_events()
            if self.series and self.scope.currentData() == 'group':
                from .recurrence import updated_series
                events = [updated_series(self.series, self.edit_reference, events[0], replace_schedule=self.schedule_replacement)]
            elif self.series and self.scope.currentData() == 'future':
                from .series_operations import updated_future
                _, tail = updated_future(self.series, self.original, events[0], self.schedule_replacement)
                events = [tail] if tail else []
            self.preview.set_events(events, self.preferences, self.calendars)
            self.error.setText(self.preview.error)
            self.preview.enable_date_editing(enabled and not self.preview.error)
        except (ValueError, PatternError, OverflowError) as exc:
            self.preview.set_events([], self.preferences, self.calendars)
            self.error.setText(str(exc))

    def save(self):
        try:
            if not self.title.text().strip():
                self._validation_field = self.title
                raise ValueError('Give your event a title.')
            if self.calendar.currentData() is None:
                self._validation_field = self.calendar
                raise ValueError('Add a writable calendar first.')
            self.result_events = self.build_events()
            self.accept()
        except (ValueError, PatternError, OverflowError) as exc:
            self.result_events = []
            self.show_error(exc)


class CalendarDialog(FormDialog):
    def __init__(self, calendar=None, parent=None, allow_remote=True, credentials=None):
        super().__init__(parent)
        self.setWindowTitle('Calendar source' if calendar else 'Add a calendar')
        self.resize(490, 360)
        self.original = calendar
        self.credentials = credentials
        self.allow_remote = allow_remote
        self._active_kind = None
        outer = QVBoxLayout(self)
        self.form = form = QFormLayout()
        self.name = QLineEdit(calendar.name if calendar else '')
        self.kind = QComboBox()
        self.kind.addItem('Local calendar', 'local')
        if allow_remote:
            self.kind.addItem('ICS subscription (read only)', 'ics')
            self.kind.addItem('CalDAV / Nextcloud account', 'caldav')
        self.kind.setCurrentIndex(self.kind.findData(calendar.kind) if calendar else 0)
        self.color = QComboBox()
        for name, color in [('Fern', '#5c8267'), ('Blue', '#5a7fa2'), ('Terracotta', '#bd775a'), ('Violet', '#8d79a8'), ('Gold', '#b4994f'),
                ('Red', '#c74444'), ('Rose', '#ce6989'), ('Orange', '#e28832'), ('Lemon', '#c4b939'), ('Lime', '#8eaa39'),
                ('Teal', '#30978c'), ('Cyan', '#35a8bf'), ('Sky', '#438ed0'), ('Indigo', '#5e67b1'), ('Purple', '#9556ad'),
                ('Plum', '#934970'), ('Brown', '#8d6651'), ('Slate', '#6e7f8b'), ('Graphite', '#515963'), ('Black', '#292c32')]:
            self.add_color(name, color)
        if calendar:
            if self.color.findData(calendar.color) < 0:
                self.add_color('Current · ' + calendar.color, calendar.color)
            self.color.setCurrentIndex(self.color.findData(calendar.color))
        self.color_row = color_row = QWidget()
        colors = QHBoxLayout(color_row)
        colors.setContentsMargins(0, 0, 0, 0)
        colors.addWidget(self.color, 1)
        self.choose_color_button = QPushButton('Choose co&lor…')
        self.choose_color_button.clicked.connect(self.choose_color)
        colors.addWidget(self.choose_color_button)
        self.use_server_color = QCheckBox('Use ser&ver color')
        self.use_server_color.setChecked(calendar.use_server_color if calendar else True)
        self.url = QLineEdit(calendar.url if calendar else '')
        self.url.setPlaceholderText('https://cloud.example.org (account or collection URL)')
        self.username = QLineEdit(calendar.username if calendar else '')
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Leave blank to reuse the saved account password')
        self.remember_password = QCheckBox('Remember in system &keyring')
        self.remember_password.setChecked(True)
        for label, widget in [('&Name', self.name), ('&Type', self.kind), ('U&RL', self.url),
                              ('&Username', self.username), ('&Password', self.password),
                              ('', self.remember_password), ('', self.use_server_color), ('&Color', color_row)]:
            form.addRow(label, widget)
        form.labelForField(color_row).setBuddy(self.color)
        outer.addLayout(form)
        self.source_hint = hint('Use a server URL to discover calendars, or a direct CalDAV collection URL. Saved passwords are shared by calendars in the same account. OAuth is not included.')
        if not allow_remote:
            self.source_hint.setText('Demo calendars are temporary and offline. Choose Open my calendars in the main window to connect Nextcloud or another remote source.')
        outer.addWidget(self.source_hint)
        self.discover_button = QPushButton('&Discover calendars')
        self.discover_button.clicked.connect(self.discover)
        outer.addWidget(self.discover_button)
        self.discovered_list = QListWidget()
        self.discovered_list.setMaximumHeight(150)
        self.discovered_list.hide()
        outer.addWidget(self.discovered_list)
        self.discovered = []
        self.discovery_identity = None
        self.discovery_future = None
        self.discovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-discovery')
        self.discovery_timer = QTimer(self)
        self.discovery_timer.setInterval(100)
        self.discovery_timer.timeout.connect(self.poll_discovery)
        self.finished.connect(self.finish_discovery)
        self.error = QLabel()
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.buttons = buttons
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.save_button.setText('Save')
        self.cancel_button.setText('Cancel')
        self.save_button.setToolTip('Enter saves; Ctrl+Enter also saves from any field.')
        if calendar:
            self.kind.setEnabled(False)
            self.url.setReadOnly(True)
            self.username.setReadOnly(True)
        self.kind.currentIndexChanged.connect(self.update_source_fields)
        self.use_server_color.toggled.connect(self.update_source_fields)
        for field in (self.url, self.username, self.password):
            field.textChanged.connect(self.update_primary_action)
        self.configure_keyboard([self.name, self.kind, self.url, self.username, self.password,
            self.remember_password, self.use_server_color, self.color, self.choose_color_button,
            self.discover_button, self.discovered_list],
            [self.save_button, self.cancel_button], self.save)
        self.update_source_fields()
        self.name.setFocus()
        self.name.selectAll()

    def add_color(self, name, value):
        swatch = QPixmap(16, 16)
        swatch.fill(QColor(value))
        icon = QIcon(swatch)
        icon.addPixmap(swatch, QIcon.Mode.Disabled)
        self.color.addItem(icon, name, value)

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color.currentData()), self, 'Choose calendar color')
        if color.isValid():
            value = color.name()
            if self.color.findData(value) < 0:
                self.add_color('Custom · ' + value, value)
            self.color.setCurrentIndex(self.color.findData(value))

    def update_source_fields(self, *_):
        kind = self.kind.currentData()
        new_account = kind == 'caldav' and self.original is None
        if kind != self._active_kind:
            self.error.clear()
            self._active_kind = kind
        self.form.setRowVisible(self.name, not new_account)
        self.form.setRowVisible(self.url, kind != 'local')
        self.form.labelForField(self.url).setText('Server U&RL' if new_account else 'Subscription U&RL' if kind == 'ics' else 'Calendar U&RL')
        self.form.setRowVisible(self.username, kind == 'caldav')
        self.form.setRowVisible(self.password, kind == 'caldav')
        self.form.setRowVisible(self.remember_password, kind == 'caldav')
        self.form.setRowVisible(self.use_server_color, kind == 'caldav')
        self.url.setEnabled(kind != 'local')
        self.username.setEnabled(kind == 'caldav')
        self.password.setEnabled(kind == 'caldav')
        manual_color = kind != 'caldav' or not self.use_server_color.isChecked()
        self.color.setEnabled(manual_color)
        self.choose_color_button.setEnabled(manual_color)
        self.source_hint.setVisible(kind != 'local' or not self.allow_remote)
        self.discover_button.setVisible(new_account)
        self.update_primary_action()

    def update_primary_action(self, *_):
        new_account = self.kind.currentData() == 'caldav' and self.original is None
        busy = bool(new_account and self.discovery_future and not self.discovery_future.done())
        current = bool(self.discovered and self.discovery_identity ==
                       (self.url.text().strip(), self.username.text().strip(), self.password.text()))
        self.discover_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self.discover_button.setDefault(new_account and not current and not busy)
        self.save_button.setDefault(not busy and (not new_account or current))
        self.discovered_list.setVisible(new_account and current)

    def discover(self):
        from .discovery import discover_calendars
        from urllib.parse import urlsplit
        url = self.url.text().strip()
        if '://' not in url:
            url = 'https://' + url
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            self.show_error('Use an HTTP(S) URL without embedded credentials.', self.url)
            return
        self.url.setText(url)
        self.discovered = []
        self.discovered_list.clear()
        self.discover_button.setEnabled(False)
        self.error.setText('Discovering calendars in the background…')
        self.discovery_identity = (url, self.username.text().strip(), self.password.text())
        def work():
            password = self.discovery_identity[2]
            if not password and self.credentials:
                password = self.credentials.get(self.discovery_identity[0], self.discovery_identity[1]) or ''
            return discover_calendars(self.discovery_identity[0], self.discovery_identity[1], password)
        self.discovery_future = self.discovery_executor.submit(work)
        self.update_primary_action()
        self.discovery_timer.start()

    def poll_discovery(self):
        if not self.discovery_future or not self.discovery_future.done():
            return
        self.discovery_timer.stop()
        current = (self.kind.currentData() == 'caldav' and self.original is None
                   and self.discovery_identity == (self.url.text().strip(), self.username.text().strip(), self.password.text()))
        try:
            self.discovered = self.discovery_future.result()
            for calendar in self.discovered:
                item = QListWidgetItem(calendar.name + (' · read only' if calendar.read_only else ''))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.discovered_list.addItem(item)
            self.discovered_list.setVisible(current and bool(self.discovered))
            if current and self.discovered:
                self.discovered_list.setCurrentRow(0)
                if self.focusWidget() in (self.discover_button, self.password, self.save_button):
                    self.discovered_list.setFocus()
            if current:
                self.error.setText(f'Found {len(self.discovered)} calendar(s). Choose calendars and save.' if self.discovered else 'No event calendars found.')
        except Exception as exc:
            if current:
                self.error.setText('Discovery failed: ' + str(exc))
        self.update_primary_action()

    def finish_discovery(self, *_):
        self.discovery_timer.stop()
        self.discovery_executor.shutdown(wait=False, cancel_futures=True)

    def save(self):
        from urllib.parse import urlparse
        if not self.name.text().strip() and (self.kind.currentData() != 'caldav' or self.original):
            self.show_error('Enter a calendar name.', self.name)
            return
        if self.kind.currentData() != 'local':
            url = urlparse(self.url.text().strip())
            if not url.scheme:
                self.url.setText('https://' + self.url.text().strip())
                url = urlparse(self.url.text().strip())
            if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
                self.show_error('Use an HTTP(S) URL without embedded credentials.', self.url)
                return
        if self.kind.currentData() == 'caldav' and not self.original:
            if self.discovery_future and not self.discovery_future.done():
                self.error.setText('Calendar discovery is still running.')
                return
            if not self.discovered or self.discovery_identity != (self.url.text().strip(), self.username.text().strip(), self.password.text()):
                self.discover()
                return
            self.result_calendars = [Calendar(id=str(uuid4()), name=source.name,
                color=source.color if self.use_server_color.isChecked() else self.color.currentData(),
                server_color=source.color, use_server_color=self.use_server_color.isChecked(), kind='caldav', url=source.url,
                username=self.username.text().strip(), read_only=source.read_only)
                for index, source in enumerate(self.discovered)
                if self.discovered_list.item(index).checkState() == Qt.CheckState.Checked]
            if not self.result_calendars:
                self.show_error('Select at least one calendar.', self.discovered_list)
                return
            self.result_calendar = self.result_calendars[0]
            self.accept()
            return
        self.result_calendar = Calendar(
            id=self.original.id if self.original else str(uuid4()), name=self.name.text().strip(),
            color=self.color.currentData(), visible=self.original.visible if self.original else True,
            kind=self.kind.currentData(),
            url=self.original.url if self.original else self.url.text().strip() if self.kind.currentData() != 'local' else '',
            username=self.original.username if self.original else self.username.text().strip() if self.kind.currentData() == 'caldav' else '',
            read_only=self.original.read_only if self.original else False,
            server_color=(self.original.server_color or self.original.color) if self.original and self.original.kind == 'caldav' else '',
            use_server_color=self.use_server_color.isChecked() if self.kind.currentData() == 'caldav' else False,
        )
        self.result_calendars = [self.result_calendar]
        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, store, changed, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Operation history')
        self.resize(600, 430)
        self.store, self.changed = store, changed
        outer = QVBoxLayout(self)
        outer.addWidget(hint('Undo any independent operation. Later changes to the same event must be undone first. History is saved across restarts.'))
        self.list = QListWidget()
        outer.addWidget(self.list)
        row = QHBoxLayout()
        self.undo_btn = QPushButton('Undo selected')
        self.redo_btn = QPushButton('Redo selected')
        row.addWidget(self.undo_btn)
        row.addWidget(self.redo_btn)
        outer.addLayout(row)
        self.message = hint('')
        outer.addWidget(self.message)
        self.undo_btn.clicked.connect(lambda: self.perform(False))
        self.redo_btn.clicked.connect(lambda: self.perform(True))
        self.list.currentRowChanged.connect(self.selection)
        self.reload()

    def reload(self):
        self.list.clear()
        for op in self.store.history():
            item = QListWidgetItem(('●  ' if op['applied'] else '○  ') + op['label'] + '   ·   ' + str(op['created_at'])[:19])
            item.setData(Qt.ItemDataRole.UserRole, op)
            self.list.addItem(item)
        self.selection()

    def selection(self, *_):
        item = self.list.currentItem()
        op = item.data(Qt.ItemDataRole.UserRole) if item else {}
        self.undo_btn.setEnabled(op.get('can_undo', False))
        self.redo_btn.setEnabled(op.get('can_redo', False))

    def perform(self, redo):
        item = self.list.currentItem()
        if not item:
            return
        try:
            op = item.data(Qt.ItemDataRole.UserRole)
            (self.store.redo if redo else self.store.undo)(op['id'])
            self.changed()
            self.reload()
        except ValueError as exc:
            self.message.setText(str(exc))


class ShortcutsDialog(QDialog):
    """Compatibility dialog around the shared Preferences keyboard editor."""
    def __init__(self, bindings, defaults, parent=None, scope_modifiers=None):
        from .shortcuts import KeyboardShortcutsEditor
        super().__init__(parent)
        self.setWindowTitle('Keyboard shortcuts')
        self.resize(470, 600)
        outer = QVBoxLayout(self)
        self.editor = KeyboardShortcutsEditor(bindings, defaults, self)
        self.fields = self.editors = self.editor.fields
        self.error = self.editor.error
        outer.addWidget(self.editor)
        self.scope_modifiers = {}
        for key, value in normalize_scope_modifiers(scope_modifiers).items():
            field = QComboBox(self)
            field.addItems(('Ctrl', 'Shift', 'Alt', 'Meta'))
            field.setCurrentText(value)
            field.hide()
            self.scope_modifiers[key] = field
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        outer.addWidget(buttons)

    def restore_defaults(self, defaults=None):
        if isinstance(defaults, dict):
            self.editor.defaults = dict(defaults)
        self.editor.restore_defaults()
        for key, value in normalize_scope_modifiers().items():
            self.scope_modifiers[key].setCurrentText(value)

    def save(self):
        from .shortcuts import validate_bindings
        try:
            bindings = self.editor.value()
            self.result_scope_modifiers = normalize_scope_modifiers(
                {key: field.currentText() for key, field in self.scope_modifiers.items()})
            validate_bindings(expanded_scope_bindings(bindings, self.result_scope_modifiers))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.result_bindings = bindings
        self.accept()
