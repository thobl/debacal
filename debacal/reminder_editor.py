"""A compact list of saved alarms and a structured editor for their triggers."""
from __future__ import annotations

from dataclasses import replace
from datetime import timezone
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from .alarm_series import is_relative
from .alarms import Alarm, signed_duration, utc_stamp
from .buttons import action_button
from .choice_control import ChoiceControl
from .date_time_fields import DateTimeField, parse_event_duration
from .date_time_picker import DateTimeRangePicker
from .dialog_base import FormDialog, dialog_buttons, hint
from .preferences import Preferences
from .reminder_format import offset_magnitude
from .reminder_preferences import relative_templates
from .theme import scheme_for
from .timeutil import local_zone, now


def _summary(alarm, preferences, all_day):
    try:
        if is_relative(alarm):
            bound = alarm.related.lower() + (' of day' if all_day else '')
            direction = 'before' if alarm.trigger.startswith('-') else 'after'
            when = f'{offset_magnitude(alarm.trigger)} {direction} {bound}'
        else:
            value = utc_stamp(alarm.trigger or '').astimezone(local_zone())
            when = f'{preferences.format_date(value.date())} {preferences.format_time(value)}'
    except (ValueError, OverflowError):
        when = alarm.trigger or 'Missing trigger'
    parts = [when]
    if alarm.action != 'DISPLAY':
        parts.append(alarm.action or 'Missing action')
    if alarm.repeat or alarm.repeat_interval:
        parts.append(f'Repeat {alarm.repeat} × {alarm.repeat_interval or "unspecified interval"}')
    if alarm.issue:
        parts.append(alarm.issue)
    if alarm.description:
        parts.append(alarm.description)
    return ' · '.join(parts)


class _AlarmDialog(FormDialog):
    def __init__(self, alarm, editor):
        super().__init__(editor)
        self.original = alarm
        self.preferences = editor.preferences
        self.setWindowTitle('Edit reminder' if alarm else 'Add reminder')
        layout = QVBoxLayout(self)
        options = [('Relative', 'relative')]
        if not editor.templates_only:
            options.append(('Date and time', 'absolute'))
        self.kind = ChoiceControl(options, self.preferences)
        self.kind.setVisible(len(options) > 1)
        layout.addWidget(self.kind)
        stack = QStackedWidget()
        layout.addWidget(stack)
        relative = QWidget()
        row = QHBoxLayout(relative)
        row.setContentsMargins(0, 0, 0, 0)
        trigger = alarm.trigger if alarm and is_relative(alarm) else '-PT5M'
        self.duration = QLineEdit(offset_magnitude(trigger))
        self.duration.setAccessibleName('Reminder offset')
        self.duration.setPlaceholderText('5min, 24h, 1d')
        row.addWidget(self.duration)
        self.direction = ChoiceControl([('Before', 'before'), ('After', 'after')], self.preferences)
        self.direction.setCurrentData('before' if trigger.startswith('-') else 'after')
        row.addWidget(self.direction)
        bounds = [('Start of day', 'START'), ('End of day', 'END')] if editor.all_day else [('Start', 'START'), ('End', 'END')]
        self.anchor = ChoiceControl(bounds, self.preferences)
        self.anchor.setCurrentData(alarm.related if alarm else 'START')
        row.addWidget(self.anchor)
        stack.addWidget(relative)
        self.absolute = DateTimeField(self.preferences)
        self.absolute.setAccessibleName('Reminder date and time')
        stack.addWidget(self.absolute)
        self.kind.currentIndexChanged.connect(stack.setCurrentIndex)
        value = editor.reference_time or now()
        if alarm and not is_relative(alarm):
            self.kind.setCurrentData('absolute')
            value = utc_stamp(alarm.trigger)
        self.absolute.set_value(value.astimezone(local_zone()))
        self.absolute.picker_requested.connect(self._pick)
        self.error = hint('')
        self.error.setProperty('error', True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = dialog_buttons(QDialogButtonBox.StandardButton.Ok)
        self.accept_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.configure_keyboard([*self.kind.buttons.values(), self.duration, *self.direction.buttons.values(),
                                 *self.anchor.buttons.values(), self.absolute.lineEdit()],
                                buttons.buttons(), self.save)
        self.resize(round(460 * self.preferences.font_scale / 100), self.sizeHint().height())

    def _pick(self):
        try:
            value = self.absolute.value()
            picker = DateTimeRangePicker(value, value, preferences=self.preferences, parent=self,
                                         calendar_color=scheme_for(self.preferences).accent)
            picker.setWindowTitle('Reminder date and time')
            picker.accepted.connect(lambda: self.absolute.set_value(picker.result_start))
            self.picker = picker
            picker.open()
        except (ValueError, OverflowError) as error:
            self.show_error(error, self.absolute)
            self.error.show()

    def save(self):
        absolute = self.kind.currentData() == 'absolute'
        try:
            if absolute:
                value = self.absolute.value().astimezone(timezone.utc)
                if self.original and not is_relative(self.original) and value == utc_stamp(self.original.trigger):
                    trigger = self.original.trigger
                else:
                    trigger = f'{value.year:04d}' + value.strftime('%m%dT%H%M%SZ')
                related = 'START'
            else:
                text = self.duration.text().strip()
                if text in ('0', '0min', '0s'):
                    trigger = 'PT0S'
                else:
                    value, nominal = parse_event_duration(text)
                    if value.microseconds:
                        raise ValueError('Use a whole number of seconds.')
                    trigger = nominal or f'PT{int(value.total_seconds())}S'
                if self.direction.currentData() == 'before':
                    trigger = '-' + trigger
                related = self.anchor.currentData()
            if self.original:
                if (not absolute and is_relative(self.original)
                        and signed_duration(trigger) == signed_duration(self.original.trigger)):
                    trigger = self.original.trigger
                self.result_alarm = replace(self.original, trigger=trigger, related=related)
            else:
                uid = 'urn:uuid:' + str(uuid4())
                self.result_alarm = Alarm(uid, 'DISPLAY', trigger, related=related, uid=uid, description='Reminder')
            self.accept()
        except (ValueError, OverflowError) as error:
            self.show_error(error, self.absolute if absolute else self.duration)
            self.error.show()


class ReminderEditor(QWidget):
    changed = Signal()
    series_requested = Signal()

    def __init__(self, *, templates_only: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.templates_only = templates_only
        owner = parent
        while owner is not None and not isinstance(getattr(owner, 'preferences', None), Preferences):
            owner = owner.parentWidget()
        self.preferences = owner.preferences if owner else Preferences()
        self.all_day = False
        self.reference_time = None
        self.series_alarms = ()
        self.next_focus_widget = None
        self._alarms = ()
        self._modified = self.mixed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.rows = QVBoxLayout(body)
        self.rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(3)
        scroll.setWidget(body)
        # QScrollArea enables native Window autofill for its child on insertion.
        # Let the themed viewport paint behind the transparent rows and buttons.
        body.setAutoFillBackground(False)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        self.add_button = action_button('+ Reminder', lambda: self._edit(None))
        self.clear_button = action_button('Clear all', lambda: self._change(()))
        self.policy_hint = hint('')
        footer.addWidget(self.add_button)
        footer.addWidget(self.clear_button)
        footer.addWidget(self.policy_hint, 1)
        layout.addLayout(footer)
        self._render()

    def set_alarms(self, alarms: tuple[Alarm, ...], *, mixed: bool = False):
        self._alarms = relative_templates(alarms) if self.templates_only else tuple(alarms)
        self.mixed, self._modified = mixed, False
        self._render()

    def alarms(self) -> tuple[Alarm, ...]:
        return self._alarms

    def is_modified(self) -> bool:
        return self._modified

    def restore(self, alarms, mixed, modified):
        self.set_alarms(alarms, mixed=mixed)
        self._modified = modified

    def _render(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self.remove_buttons, self.edit_buttons = [], []
        self.clear_button.setVisible(self.mixed)
        if self.mixed:
            self.rows.addWidget(hint('Reminders vary. Editing this list replaces reminders in the selected scope.'))
        elif not self._alarms:
            self.rows.addWidget(hint('No saved reminders'))
        self._focus_order = [self.add_button, self.clear_button]
        for index, alarm in enumerate(self._alarms):
            item = QWidget()
            row = QHBoxLayout(item)
            row.setContentsMargins(0, 0, 0, 0)
            caption = _summary(alarm, self.preferences, self.all_day)
            label = QLabel(caption)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            row.addWidget(label, 1)
            edit = action_button('Edit', lambda i=index: self._edit(i))
            edit.setAccessibleName('Edit ' + caption)
            edit.setVisible(alarm.action in ('DISPLAY', 'AUDIO') and not alarm.issue and bool(alarm.trigger))
            remove = action_button('Remove', lambda i=index: self._change(self._alarms[:i] + self._alarms[i + 1:]))
            remove.setAccessibleName('Remove ' + caption)
            self.edit_buttons.append(edit)
            self.remove_buttons.append(remove)
            row.addWidget(edit)
            row.addWidget(remove)
            self.rows.addWidget(item)
            self._focus_order.extend((edit, remove))
        if self.series_alarms:
            for alarm in self.series_alarms:
                label = hint('Series-wide · ' + _summary(alarm, self.preferences, self.all_day))
                label.setTextFormat(Qt.TextFormat.PlainText)
                self.rows.addWidget(label)
            button = action_button('Edit all in series', self.series_requested.emit)
            self.rows.addWidget(button)
            self._focus_order.append(button)
        self.arrange_focus()

    def arrange_focus(self):
        widgets = self._focus_order + ([self.next_focus_widget] if self.next_focus_widget is not None else [])
        for first, following in zip(widgets, widgets[1:]):
            QWidget.setTabOrder(first, following)

    def _edit(self, index):
        alarm = self._alarms[index] if index is not None else None
        form = _AlarmDialog(alarm, self)
        self.alarm_dialog = form
        def apply():
            values = list(self._alarms)
            if index is None:
                values.append(form.result_alarm)
            elif values[index] == form.result_alarm:
                return
            else:
                values[index] = form.result_alarm
            self._change(tuple(values))
        form.accepted.connect(apply)
        form.finished.connect(form.deleteLater)
        form.open()

    def _change(self, alarms):
        self._alarms, self.mixed, self._modified = alarms, False, True
        self._render()
        self.changed.emit()
