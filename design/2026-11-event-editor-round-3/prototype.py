"""Primary-event editor and shared range picker study; synthetic, in-memory data."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from debacal.appearance import stylesheet, widget_palette
from debacal.button_focus import install_button_focus
from debacal.dialogs import _calendar_color_icon
from debacal.icons import icon
from debacal.models import Calendar, Event
from debacal.patterns import expand_sequence, simplify_sequence
from debacal.preferences import Preferences
from debacal.theme import scheme_for
from debacal.timeutil import local_zone
from debacal.workspace_header import SegmentedControl

from picker import DateTimeRangePicker
from preview import EventPreview


CALENDARS = [Calendar('work', 'Work & projects', '#587c67'), Calendar('personal', 'Personal', '#b78264'),
             Calendar('community', 'Community', '#8776a2')]


def duration_between(start, end):
    wall = end.replace(tzinfo=None) - start.replace(tzinfo=None)
    return wall if wall > timedelta(0) else end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


def duration_text(value):
    minutes = int(value.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, rest = divmod(rest, 60)
    return ' '.join(part for part in (f'{days}d' if days else '', f'{hours}h' if hours else '',
                                       f'{rest}min' if rest else '') if part)


def parse_duration(value):
    text = value.strip().lower().replace(' ', '')
    if text.isdecimal():
        text += 'min'
    if not re.fullmatch(r'(?:[0-9]+(?:min|h|d))+', text):
        raise ValueError('Use a duration such as 45min or 1h 30min.')
    minutes = sum(int(number) * {'min': 1, 'h': 60, 'd': 1440}[unit]
                  for number, unit in re.findall(r'([0-9]+)(min|h|d)', text))
    if not 0 < minutes <= 366 * 1440:
        raise ValueError('Choose a positive duration of at most 366 days.')
    return timedelta(minutes=minutes)


def heading(text):
    widget = QLabel(text)
    widget.setObjectName('sidebarSectionHeading')
    return widget


def action_button(name, caption, callback, preferences, *, text=False):
    button = QPushButton(caption if text else '')
    button.setIcon(icon(name, scheme_for(preferences).accent))
    button.setIconSize(QSize(18, 18))
    button.setAccessibleName(caption)
    button.setToolTip(caption)
    button.setAutoDefault(False)
    if not text:
        button.setProperty('iconOnly', True)
        button.setFixedSize(30, 30)
    button.clicked.connect(lambda _checked=False: callback())
    return button


class DateTimeField(QWidget):
    """Whole-value keyboard entry with one explicit picker affordance."""
    def __init__(self, caption, callback, preferences):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.edit = QLineEdit()
        self.edit.setAccessibleName(caption)
        self.edit.setMinimumWidth(195)
        self.button = action_button('day', 'Choose ' + caption.lower(), callback, preferences)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)


class EventStudy(QDialog):
    def __init__(self, *, theme='light', scene='editor', buttons='underlined'):
        super().__init__()
        self.preferences = Preferences(start_weekday=0, locale_name='en_GB', date_format='european', time_format='24h',
                                       appearance_mode=theme, control_style=buttons,
                                       visible_start_hour=8, visible_end_hour=18)
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(100, self.preferences))
        self.setWindowTitle('Debacal · primary event and occurrence preview')
        self.resize(940, 535)
        self.presets = [timedelta(minutes=value) for value in (30, 45, 60)]
        self._updating = False
        self.picker = None
        self.overrides = {}
        self.selected_id = 'primary'
        self.mode = 'once'
        start = datetime(2026, 11, 4, 9, tzinfo=local_zone())
        self.primary = Event('primary', 'work', 'Project conversation', start, start + timedelta(minutes=45))
        self.entries = [self.primary]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(16)
        title = QLabel('Create event')
        title.setObjectName('heading')
        outer.addWidget(title)
        columns = QHBoxLayout()
        columns.setSpacing(26)
        form_widget = QWidget()
        form_widget.setMinimumWidth(355)
        form_widget.setMaximumWidth(430)
        fields = QVBoxLayout(form_widget)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(10)
        identity = QFormLayout()
        identity.setHorizontalSpacing(12)
        identity.setVerticalSpacing(9)
        self.title_field = QLineEdit(self.primary.title)
        self.calendar_field = QComboBox()
        for calendar in CALENDARS:
            self.calendar_field.addItem(_calendar_color_icon(calendar.color, self.preferences), calendar.name, calendar.id)
        identity.addRow('Title', self.title_field)
        identity.addRow('Calendar', self.calendar_field)
        fields.addLayout(identity)
        fields.addSpacing(4)
        fields.addWidget(heading('Primary event'))
        timing = QFormLayout()
        timing.setHorizontalSpacing(12)
        timing.setVerticalSpacing(8)
        self.start_field = DateTimeField('Start date and time', lambda: self.open_primary_picker('start'), self.preferences)
        self.end_field = DateTimeField('End date and time', lambda: self.open_primary_picker('end'), self.preferences)
        timing.addRow('Starts', self.start_field)
        timing.addRow('Ends', self.end_field)
        length = QWidget()
        length_row = QHBoxLayout(length)
        length_row.setContentsMargins(0, 0, 0, 0)
        length_row.setSpacing(7)
        self.duration_field = QLineEdit('45min')
        self.duration_field.setAccessibleName('Duration')
        self.duration_field.setMaximumWidth(84)
        self.duration_choices = SegmentedControl()
        self.duration_group = QButtonGroup(self)
        self.duration_buttons = []
        self.preset_settings = action_button('settings', 'Duration button preferences', self.configure_presets, self.preferences)
        length_row.addWidget(self.duration_field)
        length_row.addWidget(self.duration_choices)
        length_row.addWidget(self.preset_settings)
        length_row.addStretch(1)
        timing.addRow('Duration', length)
        fields.addLayout(timing)
        self.rebuild_presets()
        fields.addSpacing(5)
        repeat = QFormLayout()
        repeat.setHorizontalSpacing(12)
        repeat.setVerticalSpacing(8)
        self.repeat_field = QComboBox()
        for caption, value in (('Once', 'once'), ('Daily', 'daily'), ('Weekly', 'weekly'), ('Custom sequence', 'custom')):
            self.repeat_field.addItem(caption, value)
        repeat.addRow('Repeat', self.repeat_field)
        self.count_field = QSpinBox()
        self.count_field.setRange(2, 100)
        self.count_field.setValue(6)
        repeat.addRow('Count', self.count_field)
        self.sequence_field = QLineEdit()
        self.sequence_field.setAccessibleName('Custom sequence')
        self.sequence_field.setPlaceholderText('4*(+2) +3 3*+2')
        repeat.addRow('Sequence', self.sequence_field)
        self.repeat_form = repeat
        # Keep the shared field column fixed when recurrence rows appear.
        for form in (identity, timing, repeat):
            for index in range(form.rowCount()):
                item = form.itemAt(index, QFormLayout.ItemRole.LabelRole)
                if item and item.widget():
                    item.widget().setFixedWidth(65)
                    item.widget().setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        fields.addLayout(repeat)
        self.alternatives = QCheckBox('Alternative slots')
        fields.addWidget(self.alternatives)
        self.details_button = QPushButton('Location and notes…')
        self.details_button.setAutoDefault(False)
        self.details_button.setProperty('iconOnly', True)
        self.details_button.clicked.connect(self.toggle_details)
        fields.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.location_field = QLineEdit()
        self.location_field.setPlaceholderText('Location')
        self.notes_field = QTextEdit()
        self.notes_field.setPlaceholderText('Notes')
        self.notes_field.setMaximumHeight(75)
        details_layout.addWidget(self.location_field)
        details_layout.addWidget(self.notes_field)
        self.details.hide()
        fields.addWidget(self.details)
        fields.addStretch(1)
        columns.addWidget(form_widget, 1)
        preview_column = QVBoxLayout()
        preview_column.setSpacing(8)
        preview_header = QHBoxLayout()
        preview_header.addWidget(heading('Occurrences'), 1)
        self.add_button = action_button('new', 'Add occurrence', self.add_occurrence, self.preferences, text=True)
        self.add_button.setProperty('iconOnly', True)
        preview_header.addWidget(self.add_button)
        preview_column.addLayout(preview_header)
        self.preview = EventPreview(self)
        self.preview.setMinimumWidth(385)
        preview_column.addWidget(self.preview)
        preview_column.addStretch(1)
        columns.addLayout(preview_column, 1)
        outer.addLayout(columns, 1)
        self.error = QLabel()
        self.error.setProperty('error', True)
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.dialog_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.dialog_buttons.accepted.connect(self.accept)
        self.dialog_buttons.rejected.connect(self.reject)
        footer.addWidget(self.dialog_buttons)
        outer.addLayout(footer)
        self.start_field.edit.editingFinished.connect(self.apply_start)
        self.end_field.edit.editingFinished.connect(self.apply_end)
        self.duration_field.editingFinished.connect(self.apply_duration)
        self.sequence_field.editingFinished.connect(self.apply_sequence)
        self.repeat_field.currentIndexChanged.connect(self.change_repeat)
        self.count_field.valueChanged.connect(lambda _: self.rebuild())
        self.title_field.textEdited.connect(lambda _: self.rebuild())
        self.calendar_field.currentIndexChanged.connect(lambda _: self.rebuild())
        self.alternatives.toggled.connect(lambda _: self.rebuild())
        self.load_scene(scene)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    @property
    def source(self):
        return next(calendar for calendar in CALENDARS if calendar.id == self.calendar_field.currentData())

    def keyPressEvent(self, event):
        # QLineEdit has already committed its text before forwarding Return.
        # Keep a keyboard timing adjustment from activating QDialog's default
        # Save button while the person is still inspecting the live preview.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and isinstance(QApplication.focusWidget(), QLineEdit):
            event.accept()
            return
        super().keyPressEvent(event)

    @property
    def duration(self):
        return duration_between(self.primary.start, self.primary.end)

    def expression(self):
        if self.mode == 'once':
            return ''
        if self.mode in ('daily', 'weekly'):
            unit = '+1d' if self.mode == 'daily' else '+1'
            return f'{self.count_field.value() - 1}*{unit}'
        return self.sequence_field.text()

    def format_datetime(self, value):
        return self.preferences.format_date(value.date()) + ' ' + self.preferences.format_time(value)

    def parse_datetime(self, text, previous):
        if text.strip() == self.format_datetime(previous):
            return previous
        pattern = self.preferences.qt_date_pattern() + ' ' + self.preferences.qt_time_pattern()
        parsed = self.preferences.qt_locale().toDateTime(text, pattern)
        if not parsed.isValid():
            raise ValueError('Use a date and time such as 04.11.2026 09:00.')
        value = parsed.toPython().replace(tzinfo=local_zone(), fold=previous.fold)
        expand_sequence('', value)
        return value

    def report(self, callback):
        try:
            callback()
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()

    def rebuild(self):
        if self._updating:
            return
        self.report(self._rebuild)

    def _rebuild(self):
        starts = expand_sequence(self.expression(), self.primary.start)
        primary = replace(self.primary, title=self.title_field.text(), calendar_id=self.source.id,
                          candidate=self.alternatives.isChecked())
        entries = [primary]
        for index, start in enumerate(starts[1:], 1):
            duration = self.overrides.get(index, self.duration)
            end = start + duration
            expand_sequence('', end)
            entries.append(replace(primary, id=f'occ-{index}', start=start, end=end))
        self.primary, self.entries = primary, entries
        if self.selected_id not in {entry.id for entry in entries}:
            self.selected_id = 'primary'
        self.refresh()

    def refresh(self):
        self._updating = True
        try:
            self.start_field.edit.setText(self.format_datetime(self.primary.start))
            self.end_field.edit.setText(self.format_datetime(self.primary.end))
            self.duration_field.setText(duration_text(self.duration))
            self.duration_group.setExclusive(False)
            for button, duration in zip(self.duration_buttons, self.presets):
                button.setChecked(duration == self.duration)
            self.duration_group.setExclusive(True)
            for field, visible in ((self.count_field, self.mode in ('daily', 'weekly')),
                                   (self.sequence_field, self.mode == 'custom')):
                field.setVisible(visible)
                self.repeat_form.labelForField(field).setVisible(visible)
            self.alternatives.setVisible(self.mode != 'once')
            self.preview.refresh()
            self.error.hide()
        finally:
            self._updating = False

    def set_primary(self, start, end):
        def apply():
            if end.timestamp() <= start.timestamp():
                raise ValueError('The end must be after the start.')
            expand_sequence('', start)
            expand_sequence('', end)
            previous = self.primary
            self.primary = replace(previous, start=start, end=end)
            try:
                self._rebuild()
            except (ValueError, OverflowError):
                self.primary = previous
                raise
            self.preview.show_event('primary')
        self.report(apply)

    def apply_start(self):
        if not self._updating:
            def apply():
                start = self.parse_datetime(self.start_field.edit.text(), self.primary.start)
                self.set_primary(start, start + self.duration)
            self.report(apply)

    def apply_end(self):
        if not self._updating:
            self.report(lambda: self.set_primary(self.primary.start,
                self.parse_datetime(self.end_field.edit.text(), self.primary.end)))

    def apply_duration(self):
        if not self._updating:
            self.report(lambda: self.set_duration(parse_duration(self.duration_field.text())))

    def set_duration(self, value):
        self.set_primary(self.primary.start, self.primary.start + value)

    def change_repeat(self, _index):
        self.mode = self.repeat_field.currentData()
        if self.mode == 'custom' and not self.sequence_field.text():
            self.sequence_field.setText(simplify_sequence([event.start for event in self.entries]))
        self.rebuild()

    def apply_sequence(self):
        if not self._updating and self.mode == 'custom':
            self.rebuild()

    def set_occurrences(self, entries, selected_id=None):
        primary = next(event for event in entries if event.id == 'primary')
        extras = sorted((event for event in entries if event.id != 'primary'), key=lambda event: event.start.timestamp())
        ordered = [primary, *extras]
        if len({event.start.timestamp() for event in ordered}) != len(ordered):
            raise ValueError('An occurrence already starts at this time.')
        expression = simplify_sequence([event.start for event in ordered])
        self.overrides = {index: duration_between(event.start, event.end)
                          for index, event in enumerate(ordered) if index and duration_between(event.start, event.end) != self.duration}
        self.mode = 'custom' if extras else 'once'
        self.repeat_field.blockSignals(True)
        self.repeat_field.setCurrentIndex(self.repeat_field.findData(self.mode))
        self.repeat_field.blockSignals(False)
        self.sequence_field.setText(expression)
        desired = self.selected_id if selected_id is None else selected_id
        position = next((index for index, event in enumerate(ordered) if event.id == desired), 0)
        self.selected_id = 'primary' if position == 0 else f'occ-{position}'
        self._rebuild()

    def date_toggled(self, day):
        def toggle():
            matching = [event for event in self.entries if event.start.date() == day and event.id != 'primary']
            if matching:
                identifiers = {event.id for event in matching}
                self.set_occurrences([event for event in self.entries if event.id not in identifiers])
            elif day == self.primary.start.date():
                self.select_occurrence('primary')
            else:
                start = datetime.combine(day, self.primary.start.timetz())
                added = replace(self.primary, id='new', start=start, end=start + self.duration)
                self.set_occurrences([*self.entries, added], 'new')
        self.report(toggle)

    def select_occurrence(self, identifier):
        if identifier in {entry.id for entry in self.entries}:
            self.selected_id = identifier
            self.preview.refresh()
            self.preview.show_event(identifier)

    def remove_occurrence(self, identifier):
        if identifier != 'primary':
            self.report(lambda: self.set_occurrences([event for event in self.entries if event.id != identifier]))

    def open_picker(self, start, end, endpoint, callback, caption=None):
        picker = DateTimeRangePicker(start, end, endpoint=endpoint, preferences=self.preferences,
                                     calendar_color=self.source.color, parent=self)
        if caption:
            picker.setWindowTitle(caption)
        picker.accepted.connect(lambda: callback(picker.result_start, picker.result_end))
        self.picker = picker
        picker.open()
        return picker

    def open_primary_picker(self, endpoint):
        return self.open_picker(self.primary.start, self.primary.end, endpoint, self.set_primary,
                                'Primary event · ' + ('start' if endpoint == 'start' else 'end'))

    def add_occurrence(self):
        selected = next((entry for entry in self.entries if entry.id == self.selected_id), self.primary)
        start = datetime.combine(selected.start.date(), self.primary.start.timetz())
        return self.open_picker(start, start + self.duration, 'start', self.accept_new_occurrence, 'Add occurrence')

    def accept_new_occurrence(self, start, end):
        def add():
            existing = next((entry for entry in self.entries if entry.start.timestamp() == start.timestamp()), None)
            if existing:
                self.select_occurrence(existing.id)
                return
            added = replace(self.primary, id='new', start=start, end=end)
            self.set_occurrences([*self.entries, added], 'new')
            self.preview.show_event(self.selected_id)
        self.report(add)

    def edit_occurrence(self, identifier):
        if identifier == 'primary':
            return self.open_primary_picker('start')
        selected = next(entry for entry in self.entries if entry.id == identifier)
        def apply(start, end):
            changed = replace(selected, start=start, end=end)
            self.report(lambda: self.set_occurrences([changed if entry.id == identifier else entry for entry in self.entries], identifier))
        return self.open_picker(selected.start, selected.end, 'start', apply, 'Edit occurrence')

    def rebuild_presets(self):
        while self.duration_choices.buttons_layout.count():
            item = self.duration_choices.buttons_layout.takeAt(0)
            if item.widget():
                self.duration_group.removeButton(item.widget())
                item.widget().hide()
                item.widget().deleteLater()
        self.duration_buttons = []
        for value in self.presets:
            button = QPushButton(duration_text(value))
            button.setCheckable(True)
            button.setFixedSize(56, 32)
            button.setStyleSheet('padding: 0;')
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, duration=value: self.set_duration(duration))
            self.duration_group.addButton(button)
            self.duration_choices.buttons_layout.addWidget(button)
            self.duration_buttons.append(button)
        self.duration_choices.set_buttons(self.duration_buttons)
        self.duration_choices.set_preferences(self.preferences, 56, 32)

    def configure_presets(self):
        value, accepted = QInputDialog.getText(self, 'Duration buttons', 'Durations, separated by commas',
                                              text=', '.join(duration_text(value) for value in self.presets))
        if accepted:
            def apply():
                presets = [parse_duration(part) for part in value.split(',')]
                if not 1 <= len(presets) <= 4 or len(set(presets)) != len(presets):
                    raise ValueError('Choose one to four different durations.')
                self.presets = presets
                self.rebuild_presets()
                self.refresh()
            self.report(apply)

    def toggle_details(self):
        self.details.setVisible(not self.details.isVisible())
        self.details_button.setText('Hide location and notes' if self.details.isVisible() else 'Location and notes…')

    def load_scene(self, scene):
        self.overrides = {}
        self.mode = 'once'
        self.selected_id = 'primary'
        self.repeat_field.blockSignals(True)
        self.repeat_field.setCurrentIndex(0)
        self.repeat_field.blockSignals(False)
        start = datetime(2026, 11, 4, 9, tzinfo=local_zone())
        self.primary = replace(self.primary, start=start, end=start + timedelta(minutes=45))
        self.sequence_field.clear()
        self._rebuild()
        if scene in ('changed-primary', 'occurrence'):
            self.mode = 'custom'
            self.repeat_field.blockSignals(True)
            self.repeat_field.setCurrentIndex(3)
            self.repeat_field.blockSignals(False)
            self.sequence_field.setText('+1 10:30-13. +1 +2')
            self._rebuild()
        if scene == 'changed-primary':
            self.select_occurrence('occ-2')
            self.set_primary(start + timedelta(days=1), start + timedelta(days=1, minutes=45))
        elif scene in ('start', 'end'):
            start = datetime(2026, 11, 4, 21, tzinfo=local_zone())
            self.set_primary(start, start + timedelta(hours=12))
        self.preview.show_event('primary')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', choices=('light', 'dark'), default='light')
    parser.add_argument('--buttons', choices=('underlined', 'filled'), default='underlined')
    parser.add_argument('--scene', choices=('editor', 'same-day', 'start', 'end', 'occurrence', 'changed-primary'), default='editor')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    install_button_focus(app)
    window = EventStudy(theme=args.theme, scene=args.scene, buttons=args.buttons)
    window.show()
    if args.scene in ('same-day', 'start', 'end'):
        window.open_primary_picker('end' if args.scene == 'end' else 'start')
    elif args.scene == 'occurrence':
        window.add_occurrence()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
