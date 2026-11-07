"""Single-first event editor study; all changes stay in this window's memory."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from debacal.appearance import stylesheet, widget_palette
from debacal.button_focus import install_button_focus
from debacal.icons import icon
from debacal.models import Calendar, Event
from debacal.patterns import expand_sequence, simplify_sequence
from debacal.preferences import Preferences
from debacal.theme import scheme_for
from debacal.timeutil import local_zone

from widgets import MonthPicker, TimeStrip


CALENDARS = [Calendar('work', 'Work & projects', '#587c67'),
             Calendar('personal', 'Personal', '#b78264'), Calendar('community', 'Community', '#8776a2')]


def duration_text(duration):
    minutes = int(duration.total_seconds() // 60)
    days, remainder = divmod(minutes, 1440)
    hours, remainder = divmod(remainder, 60)
    return ' '.join(value for value in (f'{days}d' if days else '', f'{hours}h' if hours else '',
                                        f'{remainder}min' if remainder else '') if value)


def parse_duration(value):
    text = value.strip().lower().replace(' ', '')
    if text.isdecimal():
        text += 'min'
    if not re.fullmatch(r'(?:[0-9]+(?:min|h|d))+', text):
        raise ValueError('Use a duration such as 45min or 1h 30min.')
    minutes = sum(int(number) * {'min': 1, 'h': 60, 'd': 1440}[unit]
                  for number, unit in re.findall(r'([0-9]+)(min|h|d)', text))
    if not 1 <= minutes <= 366 * 1440:
        raise ValueError('Choose a positive duration of at most 366 days.')
    return timedelta(minutes=minutes)


def label(text, *, muted=False):
    widget = QLabel(text)
    if muted:
        widget.setProperty('muted', True)
    return widget


def flat_button(text, callback):
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.clicked.connect(lambda _=False: callback())
    return button


def field_column(caption, field):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    layout.addWidget(label(caption, muted=True))
    layout.addWidget(field)
    return widget


class PresetPreferences(QDialog):
    """An isolated Events preference study, applied only to the demo window."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.preferences = owner.preferences
        self.setWindowTitle('Preferences · Events')
        self.setFixedWidth(455)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        heading = label('Duration buttons')
        heading.setObjectName('sidebarSectionHeading')
        outer.addWidget(heading)
        description = label('Choose the durations offered in the event editor. Separate them with commas.', muted=True)
        description.setWordWrap(True)
        outer.addWidget(description)
        self.values = QLineEdit(', '.join(duration_text(value) for value in owner.presets))
        self.values.setAccessibleName('Duration presets')
        outer.addWidget(self.values)
        self.preview = QWidget()
        self.preview_row = QHBoxLayout(self.preview)
        self.preview_row.setContentsMargins(0, 0, 0, 0)
        self.preview_row.setSpacing(6)
        outer.addWidget(self.preview)
        self.error = label('')
        self.error.setProperty('error', True)
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        note = label('These buttons do not change the default length of a new event.', muted=True)
        note.setWordWrap(True)
        outer.addWidget(note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.values.textChanged.connect(self.update_preview)
        self.update_preview()

    def parsed(self):
        values = [parse_duration(part) for part in self.values.text().split(',')]
        if not 1 <= len(values) <= 6:
            raise ValueError('Choose one to six duration buttons.')
        if len(set(values)) != len(values):
            raise ValueError('Use each duration only once.')
        return values

    def update_preview(self):
        while self.preview_row.count():
            item = self.preview_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            values = self.parsed()
            self.error.clear()
            self.buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(True)
        except ValueError as exc:
            values = []
            self.error.setText(str(exc))
            self.buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)
        for duration in values:
            button = QPushButton(duration_text(duration))
            button.setEnabled(False)
            self.preview_row.addWidget(button)
        self.preview_row.addStretch()

    def apply(self):
        try:
            self.owner.presets = self.parsed()
            self.owner.rebuild_presets()
            self.error.clear()
        except ValueError as exc:
            self.error.setText(str(exc))


class EventStudy(QDialog):
    def __init__(self, *, theme='light', scene='single'):
        super().__init__()
        self.preferences = Preferences(locale_name='en_GB', date_format='european', time_format='24h',
                                       start_weekday=0, appearance_mode=theme, visible_start_hour=8, visible_end_hour=18)
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(100, self.preferences))
        self.setWindowTitle('Debacal · event creation · single first')
        self.setFixedWidth(690)
        self.resize(690, 660)
        self.event_duration = timedelta(minutes=45)
        self.presets = [timedelta(minutes=value) for value in (30, 45, 60)]
        self.serial = 0
        self.mode = 'once'
        self.selected_id = 'first'
        self.entries = []
        self._updating = False
        self._cached_series = None
        self.preferences_dialog = None
        self.pinned_end = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(13)
        heading = label('Create event')
        heading.setObjectName('heading')
        outer.addWidget(heading)
        identity = QHBoxLayout()
        identity.setSpacing(12)
        self.title_field = QLineEdit('Project conversation')
        self.title_field.setAccessibleName('Event title')
        self.calendar_field = QComboBox()
        self.calendar_field.setMinimumWidth(182)
        for calendar in CALENDARS:
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(calendar.color))
            self.calendar_field.addItem(QIcon(swatch), calendar.name, calendar.id)
        identity.addWidget(field_column('Title', self.title_field), 1)
        identity.addWidget(field_column('Calendar', self.calendar_field))
        outer.addLayout(identity)
        timing = QHBoxLayout()
        timing.setSpacing(10)
        self.date_field = QLineEdit()
        self.date_field.setAccessibleName('Date')
        self.clock_field = QLineEdit()
        self.clock_field.setAccessibleName('Start time')
        self.clock_field.setMaximumWidth(92)
        self.duration_field = QLineEdit('45min')
        self.duration_field.setAccessibleName('Duration')
        self.duration_field.setMaximumWidth(112)
        self.finish_field = QLineEdit()
        self.finish_field.setAccessibleName('Finish date and time')
        self.finish_field.setToolTip('Finish date and time; choose End date below to use the month picker')
        self.length_stack = QWidget()
        length_layout = QVBoxLayout(self.length_stack)
        length_layout.setContentsMargins(0, 0, 0, 0)
        length_layout.setSpacing(5)
        self.length_heading = label('Duration', muted=True)
        length_layout.addWidget(self.length_heading)
        length_layout.addWidget(self.duration_field)
        length_layout.addWidget(self.finish_field)
        self.finish_field.hide()
        timing.addWidget(field_column('Date', self.date_field), 2)
        timing.addWidget(field_column('Start', self.clock_field), 1)
        timing.addWidget(self.length_stack, 2)
        self.end_toggle = flat_button('Use end time', self.toggle_end)
        self.end_toggle.setStyleSheet('QPushButton {border-color:transparent; padding:5px;}')
        timing.addWidget(self.end_toggle, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(timing)
        shortcuts = QHBoxLayout()
        shortcuts.setContentsMargins(0, 0, 0, 0)
        shortcuts.setSpacing(6)
        shortcuts.addWidget(label('Duration', muted=True))
        self.presets_widget = QWidget()
        self.presets_row = QHBoxLayout(self.presets_widget)
        self.presets_row.setContentsMargins(0, 0, 0, 0)
        self.presets_row.setSpacing(6)
        shortcuts.addWidget(self.presets_widget)
        self.preset_settings = flat_button('', self.show_preset_preferences)
        self.preset_settings.setIcon(icon('settings', scheme_for(self.preferences).muted))
        self.preset_settings.setProperty('iconOnly', True)
        self.preset_settings.setToolTip('Preferences · Events · Duration buttons')
        self.preset_settings.setAccessibleName('Configure duration buttons')
        shortcuts.addWidget(self.preset_settings)
        shortcuts.addStretch(1)
        outer.addLayout(shortcuts)
        self.rebuild_presets()
        self.date_target = 'start'
        self.end_date_button = flat_button('Choose end date', self.choose_end_date)
        self.end_date_button.hide()
        outer.addWidget(self.end_date_button, 0, Qt.AlignmentFlag.AlignLeft)
        visual = QHBoxLayout()
        visual.setSpacing(22)
        self.month = MonthPicker(self)
        self.strip = TimeStrip(self)
        visual.addWidget(self.month, 1)
        visual.addWidget(self.strip)
        outer.addLayout(visual, 1)
        repeat_row = QHBoxLayout()
        repeat_row.setSpacing(8)
        repeat_row.addWidget(label('Repeat', muted=True))
        self.repeat_field = QComboBox()
        for caption, value in (('Does not repeat', 'once'), ('Daily · 6 dates', 'daily'),
                               ('Weekly · 6 dates', 'weekly'), ('Custom dates', 'custom')):
            self.repeat_field.addItem(caption, value)
        repeat_row.addWidget(self.repeat_field)
        repeat_row.addStretch(1)
        self.count_label = label('', muted=True)
        repeat_row.addWidget(self.count_label)
        outer.addLayout(repeat_row)
        self.series_panel = QWidget()
        series_layout = QVBoxLayout(self.series_panel)
        series_layout.setContentsMargins(0, 0, 0, 0)
        series_layout.setSpacing(6)
        self.sequence_field = QLineEdit()
        self.sequence_field.setAccessibleName('Custom sequence')
        self.sequence_field.setPlaceholderText('13. +1 10:30-18.')
        series_layout.addWidget(field_column('Sequence', self.sequence_field))
        self.series_note = label('Click dates to add/remove. Click a time to edit; × removes it immediately.', muted=True)
        self.series_note.setWordWrap(True)
        series_layout.addWidget(self.series_note)
        self.alternatives = QCheckBox('Alternative slots')
        series_layout.addWidget(self.alternatives)
        outer.addWidget(self.series_panel)
        self.series_panel.hide()
        self.details_toggle = flat_button('Location and notes…', self.toggle_details)
        self.details_toggle.setStyleSheet('QPushButton {border-color:transparent; text-align:left; padding:2px 0;}')
        outer.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.location_field = QLineEdit()
        self.location_field.setPlaceholderText('Location')
        self.notes_field = QLineEdit()
        self.notes_field.setPlaceholderText('Notes')
        details_layout.addWidget(self.location_field)
        details_layout.addWidget(self.notes_field)
        outer.addWidget(self.details)
        self.details.hide()
        self.error = label('')
        self.error.setProperty('error', True)
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        self.error.hide()
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(flat_button('Reset example', lambda: self.load_scene('single')))
        footer.addWidget(flat_button('Close preview', self.close))
        outer.addLayout(footer)
        self.title_field.textEdited.connect(lambda _: self.refresh())
        self.calendar_field.currentIndexChanged.connect(lambda _: self.refresh())
        self.date_field.editingFinished.connect(self.apply_date)
        self.clock_field.editingFinished.connect(self.apply_clock)
        self.duration_field.editingFinished.connect(self.apply_duration)
        self.finish_field.editingFinished.connect(self.apply_finish)
        self.repeat_field.currentIndexChanged.connect(self.change_repeat)
        self.sequence_field.editingFinished.connect(self.apply_sequence)
        self.alternatives.toggled.connect(lambda _: self.refresh())
        self.load_scene(scene)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    @property
    def source(self):
        return next(calendar for calendar in CALENDARS if calendar.id == self.calendar_field.currentData())

    @property
    def selected(self):
        return next((event for event in self.entries if event.id == self.selected_id), self.entries[0])

    @property
    def selected_day(self):
        return self.selected.start.date()

    @property
    def series_enabled(self):
        return self.mode != 'once'

    @property
    def duration(self):
        return self.event_duration

    def report(self, callback):
        try:
            callback()
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()

    def refresh(self, *, sequence=False):
        if self._updating or not self.entries:
            return
        self._updating = True
        try:
            self.entries = [replace(event, title=self.title_field.text(), calendar_id=self.source.id,
                                    end=event.start + self.event_duration,
                                    candidate=self.alternatives.isChecked()) for event in self.entries]
            if sequence:
                self.sequence_field.setText(simplify_sequence([event.start for event in self.entries]))
            self.date_field.setText(self.preferences.format_date(self.selected_day))
            self.clock_field.setText(self.selected.start.strftime('%H:%M'))
            self.duration_field.setText(duration_text(self.event_duration))
            self.finish_field.setText(self.selected.end.strftime('%d.%m.%Y %H:%M'))
            self.month.refresh()
            self.strip.refresh()
            self.series_panel.setVisible(self.series_enabled)
            self.count_label.setVisible(self.series_enabled)
            self.count_label.setText(f'{len(self.entries)} dates' if len({event.start.date() for event in self.entries}) == len(self.entries)
                                     else f'{len(self.entries)} occurrences')
            self.error.hide()
        finally:
            self._updating = False

    def load_scene(self, scene):
        self.mode = 'once'
        self.date_target = 'start'
        self.selected_id = 'first'
        self.event_duration = timedelta(minutes=45)
        self.serial = 0
        self._cached_series = None
        self.pinned_end = False
        self.finish_field.hide()
        self.duration_field.show()
        self.length_heading.setText('Duration')
        self.end_toggle.setText('Use end time')
        self.end_date_button.hide()
        start = datetime(2026, 11, 4, 9, tzinfo=local_zone())
        self.entries = [Event('first', self.source.id, self.title_field.text(), start, start + self.event_duration)]
        self.repeat_field.blockSignals(True)
        self.repeat_field.setCurrentIndex(0)
        self.repeat_field.blockSignals(False)
        self.sequence_field.clear()
        if scene in ('series', 'times'):
            self.repeat_field.setCurrentIndex(3)
            self.sequence_field.setText('6. 10:30-9. 11. 13.' if scene == 'series' else '10:30 +1d 16:00 +1')
            self.apply_sequence()
            self.select_entry(self.entries[1].id)
        if scene == 'end':
            self.toggle_end()
        self.refresh()
        self.month.show_date(start.date())
        self.month.layout().activate()
        self.layout().activate()
        self.resize(690, 780 if self.series_enabled else 660)

    def _commit(self, entries, *, selected=None):
        entries = [entries[0], *sorted(entries[1:], key=lambda event: event.start.timestamp())]
        expression = simplify_sequence([event.start for event in entries])
        self.entries = entries
        if selected is not None:
            self.selected_id = selected
        if not any(event.id == self.selected_id for event in entries):
            self.selected_id = 'first'
        self.sequence_field.setText(expression)
        if self.series_enabled:
            self.mode = 'custom'
            self.repeat_field.blockSignals(True)
            self.repeat_field.setCurrentIndex(3)
            self.repeat_field.blockSignals(False)
        self.refresh()

    def select_entry(self, identifier):
        self.selected_id = identifier
        self.refresh()

    def remove_entry(self, identifier):
        if identifier == 'first':
            return
        self.report(lambda: self._commit([event for event in self.entries if event.id != identifier]))

    def date_clicked(self, day):
        if self.date_target == 'end':
            end = datetime.combine(day, self.selected.end.timetz())
            self.set_duration(end.replace(tzinfo=None) - self.selected.start.replace(tzinfo=None))
            self.date_target = 'start'
            self.end_date_button.setText('Choose end date')
            return
        if not self.series_enabled:
            value = datetime.combine(day, self.selected.start.timetz())
            self.move_entry('first', value, value + self.event_duration)
            return
        matching = [event for event in self.entries if event.start.date() == day and event.id != 'first']
        if matching:
            identifiers = {event.id for event in matching}
            self.report(lambda: self._commit([event for event in self.entries if event.id not in identifiers]))
        elif self.entries[0].start.date() != day:
            self.add_entry(datetime.combine(day, self.selected.start.timetz()))
        else:
            self.select_entry('first')

    def add_entry(self, start):
        def add():
            if any(event.start.timestamp() == start.timestamp() for event in self.entries):
                self.select_entry(next(event.id for event in self.entries if event.start.timestamp() == start.timestamp()))
                return
            expand_sequence('', start)
            self.serial += 1
            identifier = f'added-{self.serial}'
            event = Event(identifier, self.source.id, self.title_field.text(), start, start + self.event_duration)
            self._commit([*self.entries, event], selected=identifier)
        self.report(add)

    def time_clicked(self, start):
        if self.series_enabled:
            self.add_entry(start)
        else:
            self.move_entry(self.selected_id, start, start + self.event_duration)

    def interval_chosen(self, start, end):
        if self.series_enabled:
            self.add_entry(start)
        self.move_entry(self.selected_id, start, end)

    def move_entry(self, identifier, start, end):
        def move():
            event = next(value for value in self.entries if value.id == identifier)
            duration = end.replace(tzinfo=None) - start.replace(tzinfo=None)
            if self.pinned_end and duration == self.event_duration:
                duration = event.end.replace(tzinfo=None) - start.replace(tzinfo=None)
            if duration <= timedelta(0):
                raise ValueError('Start must be before the finish. Change End or switch to duration.')
            expand_sequence('', start)
            expand_sequence('', start + duration)
            entries = [replace(value, start=start, end=start + duration) if value.id == identifier
                       else replace(value, end=value.start + duration) for value in self.entries]
            self.event_duration = duration
            self._commit(entries, selected=identifier)
        self.report(move)

    def set_duration(self, duration):
        if duration <= timedelta(0):
            self.error.setText('Duration must be positive.')
            self.error.show()
            return
        self.event_duration = duration
        self.refresh()

    def apply_date(self):
        if self._updating:
            return
        def apply():
            parsed = self.preferences.qt_locale().toDate(self.date_field.text(), self.preferences.qt_date_pattern())
            if not parsed.isValid():
                raise ValueError('Use a date such as 04.11.2026.')
            start = datetime.combine(parsed.toPython(), self.selected.start.timetz())
            self.move_entry(self.selected_id, start, start + self.event_duration)
            self.month.show_date(start.date())
        self.report(apply)

    def apply_clock(self):
        if self._updating:
            return
        def apply():
            clock = datetime.strptime(self.clock_field.text().strip(), '%H:%M').time()
            start = datetime.combine(self.selected_day, clock, local_zone())
            self.move_entry(self.selected_id, start, start + self.event_duration)
        self.report(apply)

    def apply_duration(self):
        if not self._updating:
            self.report(lambda: self.set_duration(parse_duration(self.duration_field.text())))

    def apply_finish(self):
        if self._updating:
            return
        def apply():
            end = datetime.strptime(self.finish_field.text().strip(), '%d.%m.%Y %H:%M')
            self.set_duration(end - self.selected.start.replace(tzinfo=None))
        self.report(apply)

    def apply_sequence(self):
        if self._updating or not self.series_enabled:
            return
        def apply():
            starts = expand_sequence(self.sequence_field.text(), self.entries[0].start)
            self.mode = 'custom'
            self.repeat_field.blockSignals(True)
            self.repeat_field.setCurrentIndex(3)
            self.repeat_field.blockSignals(False)
            self.entries = [replace(self.entries[0], id='first' if index == 0 else f'sequence-{index}',
                                    start=start, end=start + self.event_duration) for index, start in enumerate(starts)]
            self.selected_id = 'first'
            self.refresh()
        self.report(apply)

    def change_repeat(self, _index):
        mode = self.repeat_field.currentData()
        if mode == self.mode:
            return
        if mode == 'once':
            self._cached_series = (list(self.entries), self.sequence_field.text())
            self.entries = [self.entries[0]]
            self.selected_id = 'first'
        elif mode in ('daily', 'weekly'):
            days = 1 if mode == 'daily' else 7
            self.entries = [replace(self.entries[0], id='first' if index == 0 else f'regular-{index}',
                                    start=self.entries[0].start + timedelta(days=days * index),
                                    end=self.entries[0].end + timedelta(days=days * index)) for index in range(6)]
            self.sequence_field.setText(simplify_sequence([event.start for event in self.entries]))
        elif self._cached_series and self._cached_series[0][0].start == self.entries[0].start:
            self.entries = self._cached_series[0]
            self.sequence_field.setText(self._cached_series[1])
        self.mode = mode
        self.refresh()
        self.month.layout().activate()
        self.layout().activate()
        self.resize(690, 780 if self.series_enabled else 660)

    def toggle_end(self):
        self.pinned_end = not self.pinned_end
        if self.pinned_end:
            self.duration_field.hide()
            self.finish_field.show()
        else:
            self.finish_field.hide()
            self.duration_field.show()
            self.date_target = 'start'
        self.length_heading.setText('End' if self.pinned_end else 'Duration')
        self.end_toggle.setText('Use duration' if self.pinned_end else 'Use end time')
        self.end_date_button.setVisible(self.pinned_end)
        self.refresh()

    def choose_end_date(self):
        self.date_target = 'end'
        self.end_date_button.setText('Click the end date below…')
        self.month.show_date(self.selected.end.date())

    def rebuild_presets(self):
        while self.presets_row.count():
            item = self.presets_row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for duration in self.presets:
            button = flat_button(duration_text(duration), lambda value=duration: self.use_preset(value))
            self.presets_row.addWidget(button)

    def use_preset(self, duration):
        if self.pinned_end:
            self.toggle_end()
        self.set_duration(duration)

    def show_preset_preferences(self):
        self.preferences_dialog = PresetPreferences(self)
        self.preferences_dialog.open()

    def toggle_details(self):
        self.details.setVisible(not self.details.isVisible())
        self.details_toggle.setText('Hide location and notes' if self.details.isVisible() else 'Location and notes…')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', choices=('light', 'dark'), default='light')
    parser.add_argument('--scene', choices=('single', 'series', 'times', 'preferences', 'end'), default='single')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    install_button_focus(app)
    window = EventStudy(theme=args.theme, scene=args.scene)
    window.show()
    if args.scene == 'preferences':
        window.show_preset_preferences()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
