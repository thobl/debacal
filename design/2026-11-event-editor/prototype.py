"""Disposable, native time-editing studies. No database, files or network sync.

Run from the repository root with --option a, b or c. These studies deliberately
use finite custom sequences; they are not a replacement for the real editor.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from debacal.appearance import stylesheet, widget_palette
from debacal.button_focus import install_button_focus
from debacal.calendar_widgets import CalendarCanvas, DayHeader, display_scale, text
from debacal.event_style import event_colors, paint_event_card
from debacal.models import Calendar, Event
from debacal.patterns import expand_sequence, simplify_sequence
from debacal.preferences import Preferences
from debacal.series_preview import SeriesPreview, _PreviewGrid
from debacal.theme import scheme_for
from debacal.timeutil import local_zone
from debacal.workspace_header import SegmentedControl


NAMES = {'a': 'Month + day planner', 'b': 'One calendar, two views', 'c': 'Month + time ribbons'}
CALENDARS = [Calendar('work', 'Work & projects', '#587c67'),
             Calendar('personal', 'Personal', '#b78264'), Calendar('community', 'Community', '#8776a2')]


def label(value, *, muted=False, heading=False):
    result = QLabel(value)
    result.setWordWrap(True)
    if muted:
        result.setProperty('muted', True)
    if heading:
        result.setObjectName('sidebarSectionHeading')
    return result


def row(*widgets):
    result = QWidget()
    layout = QHBoxLayout(result)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in widgets:
        layout.addWidget(widget)
    return result


def duration_text(value):
    minutes = int(value.total_seconds() // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return ' '.join(part for part in (f'{days}d' if days else '', f'{hours}h' if hours else '',
                                       f'{minutes}min' if minutes else '') if part)


def parse_duration(value):
    value = value.strip().lower().replace(' ', '')
    if not re.fullmatch(r'(?:[0-9]+(?:min|h|d))+', value):
        raise ValueError('Use a duration such as 45min, 1h30min or 2d.')
    minutes = sum(int(amount) * {'min': 1, 'h': 60, 'd': 1440}[unit]
                  for amount, unit in re.findall(r'([0-9]+)(min|h|d)', value))
    if not 1 <= minutes <= 366 * 1440:
        raise ValueError('Choose a duration between one minute and 366 days.')
    return timedelta(minutes=minutes)


def parse_clock(value):
    try:
        parsed = datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError as exc:
        raise ValueError('Use a time such as 09:30.') from exc
    return parsed


class ReviewMonthGrid(_PreviewGrid):
    """Prototype-only chip selection, separate from the date-toggle gesture."""
    def event_rects(self, day):
        rect = self.cell_rect(day)
        scale = display_scale(self)
        capacity = max(1, int((rect.height() - 22 * scale) / (16 * scale)))
        return [(QRectF(rect.x() + 3, rect.y() + 21 * scale + index * 16 * scale,
                        rect.width() - 6, 14 * scale), event)
                for index, event in enumerate(self.preview.events_on(day)[:capacity])]

    def paintEvent(self, event):
        super().paintEvent(event)
        owner = self.preview.owner
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = event_colors(owner.source.color, owner.preferences)
        for offset in range(42):
            day = self.preview.grid_start + timedelta(days=offset)
            for rect, item in self.event_rects(day):
                if item.id == owner.selected_id:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(QColor(colors.selected_outline), 2))
                    painter.drawRoundedRect(rect, 3, 3)
                    painter.fillRect(QRectF(rect.left() + 3, rect.top() + 3, 2, rect.height() - 6),
                                     QColor(colors.selected_outline))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.date_at(event.position())
        if day is None:
            return
        owner = self.preview.owner
        if not owner.date_target:
            for rect, item in self.event_rects(day):
                if rect.contains(event.position()):
                    owner.select(item.id)
                    return
        owner.click_date(day)


class ReviewMonth(SeriesPreview):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        previous = self.grid
        self.grid = ReviewMonthGrid(self)
        self.layout().replaceWidget(previous, self.grid)
        previous.hide()
        previous.deleteLater()
        self.instructions.setText('')

    def _update_instructions(self):
        if hasattr(self, 'owner'):
            self.instructions.setText('Empty date: add · date number: remove · event: edit time')


class ReviewCanvas(CalendarCanvas):
    """Use Debacal's actual time grid; only the first event resizes the series."""
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.drag and self.drag[0] == 'resize' and self.drag[1].id != 'first':
            _, item, point, position = self.drag
            self.drag = ('move', item, point, position)

    def keyPressEvent(self, event):
        if self.parent_editor.handle_time_key(event):
            return
        super().keyPressEvent(event)


class DayPlanner(QWidget):
    def __init__(self, owner, *, week=False):
        super().__init__()
        self.owner, self.week = owner, week
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = DayHeader()
        self.header.set_preferences(owner.preferences)
        self.header.setFixedHeight(64)
        layout.addWidget(self.header)
        self.canvas = ReviewCanvas()
        self.canvas.parent_editor = owner
        self.canvas.fixed_all_day = True
        self.canvas.set_preferences(owner.preferences)
        self.canvas.selected.connect(owner.select)
        self.canvas.move_requested.connect(owner.move)
        self.canvas.create_requested.connect(owner.create)
        self.canvas.edit_requested.connect(owner.select)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.canvas)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll, 1)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.canvas.fit_to_viewport(event.size().height())
        return super().eventFilter(watched, event)

    def refresh(self):
        day = self.owner.selected.start.date()
        days = ([self.owner.preferences.week_start(day) + timedelta(days=index) for index in range(7)]
                if self.week else [day])
        self.header.days = days
        self.header.update()
        self.canvas.set_data(days, self.owner.entries, CALENDARS)
        self.canvas.set_creation_calendar(self.owner.source)
        self.canvas.set_selection({self.owner.selected_id})
        self.canvas.fit_to_viewport(self.scroll.viewport().height())


class TimeRibbons(QWidget):
    """A compact horizontal alternative; date labels stay in one shared axis."""
    gutter = 112
    row_height = 57

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.days, self.hits, self.rows = [], [], []
        self.lanes = {}
        self.drag = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def refresh(self):
        self.days = sorted({event.start.date() for event in self.owner.entries})
        self.rows, self.lanes = [], {}
        top = 30
        for day in self.days:
            ends = []
            for item in sorted((value for value in self.owner.entries if value.start.date() == day),
                               key=lambda value: value.start):
                lane = next((index for index, end in enumerate(ends) if end <= item.start), len(ends))
                if lane == len(ends):
                    ends.append(item.end)
                else:
                    ends[lane] = item.end
                self.lanes[item.id] = lane
            height = max(self.row_height, 16 + len(ends) * 41)
            self.rows.append((day, top, height))
            top += height
        self.setMinimumHeight(top + 2)
        self.update()

    def x_at(self, minute):
        return self.gutter + (minute - 8 * 60) / (12 * 60) * (self.width() - self.gutter - 12)

    def minute_at(self, x):
        return max(0, min(1425, round((8 * 60 + (x - self.gutter) /
                                    max(1, self.width() - self.gutter - 12) * 720) / 15) * 15))

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = scheme_for(self.owner.preferences)
        painter.fillRect(self.rect(), QColor(colors.background))
        self.hits = []
        for hour in range(8, 21, 2):
            x = self.x_at(hour * 60)
            text(painter, QRectF(x - 20, 0, 40, 25), f'{hour:02d}:00', QColor(colors.muted), 8,
                 align=Qt.AlignmentFlag.AlignCenter)
            painter.setPen(QPen(QColor(colors.line), 1))
            painter.drawLine(QPointF(x, 28), QPointF(x, self.height()))
        style = event_colors(self.owner.source.color, self.owner.preferences)
        for day, top, height in self.rows:
            text(painter, QRectF(2, top + 4, self.gutter - 10, 20), day.strftime('%a %d %b'),
                 QColor(colors.ink), 9, day == self.owner.selected.start.date())
            painter.setPen(QPen(QColor(colors.line), 1))
            painter.drawLine(QPointF(0, top + height - 1), QPointF(self.width(), top + height - 1))
            for item in self.owner.entries:
                if item.start.date() != day:
                    continue
                minute = item.start.hour * 60 + item.start.minute
                width = max(39, self.x_at(minute + self.owner.duration.total_seconds() / 60) - self.x_at(minute))
                rect = QRectF(self.x_at(minute), top + 5 + self.lanes[item.id] * 41, width, 34)
                self.hits.append((rect, item))
                paint_event_card(painter, rect, style, selected=item.id == self.owner.selected_id)
                painter.save()
                painter.setClipRect(rect.adjusted(3, 0, -2, 0))
                text(painter, rect.adjusted(7, 0, 0, 0), item.start.strftime('%H:%M'), QColor(style.ink), 8,
                     item.id == self.owner.selected_id)
                painter.restore()
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        for rect, item in self.hits:
            if rect.contains(event.position()):
                self.owner.select(item.id)
                self.drag = (item, self.minute_at(event.position().x()) - (item.start.hour * 60 + item.start.minute))
                return
        day = next((day for day, top, height in self.rows if top <= event.position().y() < top + height), None)
        if event.position().x() >= self.gutter and day is not None:
            start = datetime.combine(day, time.min, local_zone())
            start += timedelta(minutes=self.minute_at(event.position().x()))
            self.owner.create(start, start + self.owner.duration)

    def mouseMoveEvent(self, event):
        if self.drag:
            item, offset = self.drag
            start = item.start.replace(hour=0, minute=0) + timedelta(minutes=self.minute_at(event.position().x()) - offset)
            self.owner.move(item.id, start, start + self.owner.duration)

    def mouseReleaseEvent(self, _):
        self.drag = None

    def keyPressEvent(self, event):
        if not self.owner.handle_time_key(event):
            super().keyPressEvent(event)


class EditorStudy(QDialog):
    def __init__(self, option='a', theme='light'):
        super().__init__()
        self.option = option
        self.preferences = Preferences(locale_name='en_GB', time_format='24h', date_format='european',
                                       start_weekday=0, visible_start_hour=8, visible_end_hour=19,
                                       appearance_mode=theme)
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(100, self.preferences))
        self.setWindowTitle(f'Debacal event editor · {option.upper()} · {NAMES[option]}')
        self.resize(1240, 800)
        self.entries = []
        self.duration = timedelta(minutes=45)
        self.selected_id = 'first'
        self.date_target = None
        self.serial = 0
        self._refreshing = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(14)
        heading = label('Create event')
        heading.setObjectName('heading')
        subtitle = label(f'{option.upper()} · {NAMES[option]}', muted=True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(row(heading, subtitle))
        content = QHBoxLayout()
        content.setSpacing(22)
        form = QWidget()
        form.setFixedWidth(267)
        fields = QVBoxLayout(form)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(8)
        self.title_field = QLineEdit('Project conversations')
        self.title_field.textEdited.connect(lambda _: self.refresh())
        self.calendar_field = QComboBox()
        for calendar in CALENDARS:
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(calendar.color))
            self.calendar_field.addItem(QIcon(swatch), calendar.name, calendar.id)
        self.calendar_field.currentIndexChanged.connect(lambda _: self.refresh())
        fields.addWidget(label('Title', heading=True))
        fields.addWidget(self.title_field)
        fields.addWidget(label('Calendar', heading=True))
        fields.addWidget(self.calendar_field)
        fields.addSpacing(10)
        fields.addWidget(label('First event', heading=True))
        self.start_date_button = QPushButton()
        self.start_date_button.setToolTip('Choose the first date in this preview calendar')
        self.start_date_button.clicked.connect(lambda: self.pick_date('first'))
        self.start_clock = QLineEdit()
        self.start_clock.setFixedWidth(72)
        self.start_clock.setToolTip('Type a whole time, or move the first event in the preview')
        self.start_clock.editingFinished.connect(self.change_first_clock)
        fields.addWidget(row(self.start_date_button, self.start_clock))
        self.timing_control, self.timing_buttons = self.segmented(('Duration', 'End'), self.change_timing_mode, 104)
        fields.addWidget(self.timing_control)
        self.duration_field = QLineEdit('45min')
        self.duration_field.editingFinished.connect(self.change_duration)
        self.end_date_button = QPushButton()
        self.end_date_button.clicked.connect(lambda: self.pick_date('end'))
        self.end_clock = QLineEdit()
        self.end_clock.setFixedWidth(72)
        self.end_clock.editingFinished.connect(self.change_end)
        self.end_controls = row(self.end_date_button, self.end_clock)
        fields.addWidget(self.duration_field)
        fields.addWidget(self.end_controls)
        self.end_controls.hide()
        presets = []
        for minutes, caption in ((30, '30min'), (45, '45min'), (60, '1h')):
            button = QPushButton(caption)
            button.clicked.connect(lambda _=False, value=minutes: self.set_duration(timedelta(minutes=value)))
            presets.append(button)
        self.presets = row(*presets)
        fields.addWidget(self.presets)
        fields.addWidget(label('Duration applies to every occurrence.', muted=True))
        fields.addSpacing(10)
        fields.addWidget(label('Repeat', heading=True))
        self.repeat_field = QComboBox()
        self.repeat_field.addItems(['Once', 'Daily', 'Weekly', 'Custom sequence'])
        self.repeat_field.currentIndexChanged.connect(self.change_repeat)
        fields.addWidget(self.repeat_field)
        self.sequence_field = QLineEdit()
        self.sequence_field.setToolTip('Each instruction starts from the previous event. The first event is included.')
        self.sequence_field.editingFinished.connect(self.apply_sequence)
        fields.addWidget(self.sequence_field)
        fields.addWidget(label('+30min · +2h · +2d · +3 = 3 weeks\n10:30 · 13. · 4*(+2)', muted=True))
        fields.addSpacing(10)
        fields.addWidget(label('Selected occurrence', heading=True))
        self.selected_label = label('')
        self.selected_clock = QLineEdit()
        self.selected_clock.setFixedWidth(72)
        self.selected_clock.setToolTip('Exact starting time of this occurrence')
        self.selected_clock.editingFinished.connect(self.change_selected_clock)
        fields.addWidget(row(self.selected_label, self.selected_clock))
        self.remove_button = QPushButton('Remove occurrence')
        self.remove_button.clicked.connect(self.remove_selected)
        fields.addWidget(self.remove_button)
        self.alternatives = QCheckBox('Alternative slots')
        fields.addWidget(self.alternatives)
        fields.addStretch(1)
        fields.addWidget(label('Location and notes are omitted\nfrom these layout studies.', muted=True))
        content.addWidget(form)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet('color:' + scheme_for(self.preferences).line)
        content.addWidget(divider)
        self.month = ReviewMonth(self)
        preview_area = QWidget()
        preview_layout = QVBoxLayout(preview_area)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(10)
        self.day_planner = self.week_planner = self.ribbons = None
        if option == 'a':
            split = QHBoxLayout()
            split.setSpacing(16)
            split.addWidget(self.month, 3)
            day_box = QVBoxLayout()
            day_box.setSpacing(8)
            day_box.addWidget(label('Time on selected date', heading=True))
            self.day_planner = DayPlanner(self)
            day_box.addWidget(self.day_planner, 1)
            split.addLayout(day_box, 2)
            preview_layout.addLayout(split, 1)
        elif option == 'b':
            self.view_control, self.view_buttons = self.segmented(('Month', 'Week'), self.change_view, 92)
            preview_layout.addWidget(self.view_control)
            self.pages = QStackedWidget()
            self.pages.addWidget(self.month)
            self.week_planner = DayPlanner(self, week=True)
            self.pages.addWidget(self.week_planner)
            preview_layout.addWidget(self.pages, 1)
        else:
            preview_layout.addWidget(self.month, 3)
            preview_layout.addWidget(label('Times by date', heading=True))
            self.ribbons = TimeRibbons(self)
            self.ribbon_scroll = QScrollArea()
            self.ribbon_scroll.setWidgetResizable(True)
            self.ribbon_scroll.setMinimumHeight(195)
            self.ribbon_scroll.setWidget(self.ribbons)
            preview_layout.addWidget(self.ribbon_scroll, 2)
        preview_layout.addWidget(label('Drag blocks to set their times · arrows adjust by 15min' if option == 'c' else
                                       'Drag to move · resize the first event to set duration · arrows adjust by 15min', muted=True))
        content.addWidget(preview_area, 1)
        outer.addLayout(content, 1)
        self.error = label('')
        self.error.setProperty('error', True)
        self.error.hide()
        outer.addWidget(self.error)
        self.feedback = label('Prototype · sample events only', muted=True)
        reset = QPushButton('Reset example')
        reset.clicked.connect(self.reset)
        close = QPushButton('Close preview')
        close.setObjectName('primary')
        close.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.feedback, 1)
        footer.addWidget(reset)
        footer.addWidget(close)
        outer.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.reset()

    def segmented(self, names, callback, width):
        control = SegmentedControl()
        group = QButtonGroup(control)
        buttons = []
        for index, name in enumerate(names):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setFixedSize(width, 32)
            button.clicked.connect(lambda _=False, value=index: callback(value))
            group.addButton(button)
            control.buttons_layout.addWidget(button)
            buttons.append(button)
        buttons[0].setChecked(True)
        control.set_buttons(buttons)
        control.set_preferences(self.preferences, width, 32)
        return control, buttons

    @property
    def source(self):
        identifier = self.calendar_field.currentData()
        return next(value for value in CALENDARS if value.id == identifier)

    @property
    def selected(self):
        return next((item for item in self.entries if item.id == self.selected_id), self.entries[0])

    def reset(self):
        self.duration = timedelta(minutes=45)
        first = datetime(2026, 11, 4, 9, tzinfo=local_zone())
        self.entries = [Event('first', 'work', 'Project conversations', first, first + self.duration)]
        self.selected_id = 'first'
        self.sequence_field.setText('+2h +1d 15:00 +2')
        self.apply_sequence()
        self.month.show_month(first.date())
        if self.option == 'b':
            self.change_view(1)

    def report(self, action):
        try:
            action()
        except (ValueError, OverflowError) as exc:
            self.error.setText(str(exc))
            self.error.show()

    def refresh(self, *, reencode=False):
        if self._refreshing or not self.entries:
            return
        self._refreshing = True
        try:
            self.entries = [replace(item, calendar_id=self.source.id, title=self.title_field.text(),
                                    end=item.start + self.duration) for item in self.entries]
            first = self.entries[0]
            self.start_date_button.setText(first.start.strftime('%a %d %b'))
            self.start_clock.setText(first.start.strftime('%H:%M'))
            self.duration_field.setText(duration_text(self.duration))
            self.end_clock.setText(first.end.strftime('%H:%M'))
            self.end_date_button.setText(first.end.strftime('%a %d %b'))
            if reencode:
                self.sequence_field.setText(simplify_sequence([item.start for item in self.entries]))
            self.repeat_field.blockSignals(True)
            self.repeat_field.setCurrentIndex(3 if len(self.entries) > 1 else 0)
            self.repeat_field.blockSignals(False)
            self.month.set_events(self.entries, self.preferences, CALENDARS)
            self.month.summary.setText(f'{len(self.entries)} occurrences · first event included')
            if self.day_planner:
                self.day_planner.refresh()
            if self.week_planner:
                self.week_planner.refresh()
            if self.ribbons:
                self.ribbons.refresh()
            self.selection_details()
            self.error.hide()
        finally:
            self._refreshing = False

    def select(self, identifier):
        self.selected_id = identifier
        self.selection_details()
        self.month.grid.update()
        if self.day_planner:
            if self.day_planner.canvas.days != [self.selected.start.date()]:
                self.day_planner.refresh()
            else:
                self.day_planner.canvas.set_selection({identifier})
            self.day_planner.canvas.setFocus()
        if self.week_planner:
            day = self.preferences.week_start(self.selected.start.date())
            if not self.week_planner.canvas.days or self.week_planner.canvas.days[0] != day:
                self.week_planner.refresh()
            else:
                self.week_planner.canvas.set_selection({identifier})
            self.change_view(1)
            self.week_planner.canvas.setFocus()
        if self.ribbons:
            self.ribbons.update()
            self.ribbons.setFocus()
            top = next(top for day, top, _height in self.ribbons.rows if day == self.selected.start.date())
            self.ribbon_scroll.ensureVisible(0, top + 20 + self.ribbons.lanes[self.selected_id] * 41, 0, 35)

    def selection_details(self):
        suffix = 'First event' if self.selected_id == 'first' else 'One occurrence'
        self.selected_label.setText(self.selected.start.strftime('%a %d %b') + '\n' + suffix)
        self.selected_clock.setText(self.selected.start.strftime('%H:%M'))
        self.remove_button.setEnabled(self.selected_id != 'first')

    def apply_sequence(self):
        def apply():
            starts = expand_sequence(self.sequence_field.text(), self.entries[0].start)
            self.entries = [replace(self.entries[0], id='first' if index == 0 else f'step-{index}',
                                    start=start, end=start + self.duration) for index, start in enumerate(starts)]
            self.selected_id = 'first'
            self.refresh()
        self.report(apply)

    def change_repeat(self, index):
        if self._refreshing or not self.entries:
            return
        if index in (0, 1, 2):
            self.sequence_field.setText(('', '4*+1d', '4*+1')[index])
            self.apply_sequence()
        else:
            self.sequence_field.setFocus()

    def move(self, identifier, start, end):
        def apply():
            item = next(value for value in self.entries if value.id == identifier)
            duration = end.replace(tzinfo=None) - start.replace(tzinfo=None)
            if identifier == 'first':
                if self.timing_buttons[1].isChecked() and duration == self.duration:
                    duration = item.end.replace(tzinfo=None) - start.replace(tzinfo=None)
                    if duration <= timedelta(0):
                        raise ValueError('The first event must start before the fixed finish. Choose another finish or switch to Duration.')
                delta = start - item.start
                self.entries = [replace(value, start=value.start + delta, end=value.end + delta)
                                for value in self.entries]
                if duration > timedelta(0):
                    self.duration = duration
            else:
                self.entries = [replace(value, start=start, end=start + self.duration)
                                if value.id == identifier else value for value in self.entries]
            self.entries = [self.entries[0], *sorted(self.entries[1:], key=lambda value: value.start.timestamp())]
            self.selected_id = identifier
            self.refresh(reencode=True)
        self.report(apply)

    def create(self, start, end):
        if any(item.start == start for item in self.entries):
            return
        self.serial += 1
        identifier = f'added-{self.serial}'
        self.entries.append(Event(identifier, self.source.id, self.title_field.text(), start, start + self.duration))
        self.entries = [self.entries[0], *sorted(self.entries[1:], key=lambda value: value.start.timestamp())]
        self.selected_id = identifier
        self.refresh(reencode=True)

    def pick_date(self, target):
        self.date_target = target
        self.feedback.setText('Choose the first date in the preview; duration stays the same.' if target == 'first'
                              else 'Choose the finish date in the preview; the duration updates for the series.')
        if self.option == 'b':
            self.change_view(0)
        self.month.show_month(self.entries[0].start.date() if target == 'first' else self.entries[0].end.date())

    def click_date(self, day):
        if self.date_target:
            target, self.date_target = self.date_target, None
            self.feedback.setText('Prototype · sample events only')
            if target == 'first':
                item = self.entries[0]
                start = datetime.combine(day, item.start.timetz())
                self.move('first', start, start + self.duration)
            else:
                finish = datetime.combine(day, self.entries[0].end.timetz())
                self.set_duration(finish.replace(tzinfo=None) - self.entries[0].start.replace(tzinfo=None))
            return
        matching = [item for item in self.entries if item.start.date() == day and item.id != 'first']
        if matching:
            self.entries = [item for item in self.entries if item not in matching]
            self.selected_id = 'first'
            self.refresh(reencode=True)
        else:
            start = datetime.combine(day, self.entries[0].start.timetz())
            self.create(start, start + self.duration)

    def remove_selected(self):
        if self.selected_id != 'first':
            self.entries = [item for item in self.entries if item.id != self.selected_id]
            self.selected_id = 'first'
            self.refresh(reencode=True)

    def set_duration(self, duration):
        if duration <= timedelta(0):
            self.error.setText('The finish must be after the start.')
            self.error.show()
            return
        self.duration = duration
        self.refresh()

    def change_duration(self):
        self.report(lambda: self.set_duration(parse_duration(self.duration_field.text())))

    def change_first_clock(self):
        def apply():
            first = self.entries[0]
            start = datetime.combine(first.start.date(), parse_clock(self.start_clock.text()), local_zone())
            self.move('first', start, start + self.duration)
        self.report(apply)

    def change_end(self):
        def apply():
            first = self.entries[0]
            end = datetime.combine(first.end.date(), parse_clock(self.end_clock.text()), local_zone())
            self.set_duration(end.replace(tzinfo=None) - first.start.replace(tzinfo=None))
        self.report(apply)

    def change_selected_clock(self):
        def apply():
            item = self.selected
            start = datetime.combine(item.start.date(), parse_clock(self.selected_clock.text()), local_zone())
            self.move(item.id, start, start + self.duration)
        self.report(apply)

    def change_timing_mode(self, index):
        self.timing_buttons[index].setChecked(True)
        if index == 0:
            self.end_controls.hide()
            self.duration_field.show()
            self.presets.show()
        else:
            self.duration_field.hide()
            self.presets.hide()
            self.end_controls.show()

    def change_view(self, index):
        self.view_buttons[index].setChecked(True)
        self.pages.setCurrentIndex(index)

    def handle_time_key(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            minutes = 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 15
            delta = timedelta(minutes=minutes * (-1 if event.key() == Qt.Key.Key_Up else 1))
            item = self.selected
            self.move(item.id, item.start + delta, item.end + delta)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
        else:
            return False
        event.accept()
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--option', choices=NAMES, default='a')
    parser.add_argument('--theme', choices=('light', 'dark'), default='light')
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    install_button_focus(app)
    window = EditorStudy(args.option, args.theme)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
