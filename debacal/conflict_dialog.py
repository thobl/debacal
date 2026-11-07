"""Compare the scheduling effect of two immutable conflict snapshots."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QLocale, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .preferences import Preferences
from .series_preview import SeriesPreview
from .timeutil import local_zone, now
from .theme import scheme_for


class ConflictDialog(QDialog):
    def __init__(self, conflict, preferences: Preferences, parent=None):
        super().__init__(parent)
        self.conflict = conflict
        self.preferences = preferences
        self.choice = None
        self.setWindowTitle('Resolve sync conflict')
        self.resize(1080, 830)
        outer = QVBoxLayout(self)
        explanation = QLabel(conflict.reason + '\nCompare both versions, then choose which one to keep. '
                             'A changed server version will require a fresh review.')
        explanation.setWordWrap(True)
        outer.addWidget(explanation)

        navigation = QHBoxLayout()
        self.previous_button = QPushButton('‹')
        self.previous_button.setToolTip('Previous month on both calendars')
        self.next_button = QPushButton('›')
        self.next_button.setToolTip('Next month on both calendars')
        self.month_label = QLabel()
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.previous_button.clicked.connect(lambda: self.navigate_month(-1))
        self.next_button.clicked.connect(lambda: self.navigate_month(1))
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.month_label, 1)
        navigation.addWidget(self.next_button)
        self.local_date_button = QPushButton('Show local date')
        self.remote_date_button = QPushButton('Show server date')
        for button, event in [(self.local_date_button, conflict.local),
                              (self.remote_date_button, conflict.remote)]:
            button.setEnabled(event is not None)
            if event is not None:
                button.clicked.connect(lambda checked=False, item=event: self.show_month(
                    item.start.astimezone(local_zone()).date()))
            navigation.addWidget(button)
        outer.addLayout(navigation)

        columns = QHBoxLayout()
        self.previews = []
        for label, event in [('Your local version', conflict.local), ('Server version', conflict.remote)]:
            column = QVBoxLayout()
            heading = QLabel(label)
            heading.setStyleSheet('font-weight: 600; font-size: 15px;')
            column.addWidget(heading)
            status = QLabel(event.title if event else 'Event deleted')
            status.setWordWrap(True)
            column.addWidget(status)
            preview = SeriesPreview()
            preview.set_events([event] if event else [], preferences)
            for control in (preview.previous_button, preview.next_button, preview.first_button,
                            preview.month_label):
                control.hide()
            column.addWidget(preview, 1)
            columns.addLayout(column, 1)
            self.previews.append(preview)
        self.local_preview, self.remote_preview = self.previews
        outer.addLayout(columns, 1)

        caption = QLabel('Changed fields are highlighted. Times below use your local timezone.')
        caption.setProperty('muted', True)
        outer.addWidget(caption)
        local, remote = self.describe(conflict.local), self.describe(conflict.remote)
        self.fields = list(dict.fromkeys([*local, *remote]))
        self.comparison = QTableWidget(len(self.fields), 3)
        self.comparison.setHorizontalHeaderLabels(['Field', 'Your local version', 'Server version'])
        self.comparison.verticalHeader().hide()
        self.comparison.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.comparison.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.comparison.setWordWrap(True)
        self.comparison.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for index in (1, 2):
            self.comparison.horizontalHeader().setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
        self.comparison.verticalHeader().setMaximumSectionSize(100)
        for row, field in enumerate(self.fields):
            values = [field, local.get(field, '—'), remote.get(field, '—')]
            changed = values[1] != values[2]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value or '—')
                item.setToolTip(value)
                if changed:
                    colors = scheme_for(preferences)
                    item.setBackground(QColor(colors.notice_background))
                    item.setForeground(QColor(colors.notice_ink))
                self.comparison.setItem(row, col, item)
        self.comparison.setMinimumHeight(190)
        self.comparison.setMaximumHeight(300)
        outer.addWidget(self.comparison)

        buttons = QHBoxLayout()
        self.local_button = QPushButton('Keep local' if conflict.local else 'Keep local deletion')
        self.remote_button = QPushButton('Use server version' if conflict.remote else 'Keep server deletion')
        self.later_button = QPushButton('Decide later')
        # A keyboard default must never silently resolve a conflict.
        for button in (self.local_button, self.remote_button):
            button.setAutoDefault(False)
        self.later_button.setDefault(True)
        self.local_button.clicked.connect(lambda: self.choose('local'))
        self.remote_button.clicked.connect(lambda: self.choose('remote'))
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.local_button)
        buttons.addWidget(self.remote_button)
        buttons.addStretch()
        buttons.addWidget(self.later_button)
        outer.addLayout(buttons)
        first = conflict.local or conflict.remote
        self.show_month(first.start.astimezone(local_zone()).date() if first else now().date())

    def describe(self, event):
        if event is None:
            return {'State': 'Event deleted'}
        preferences = self.preferences
        def stamp(value):
            value = value.astimezone(local_zone())
            return preferences.format_date(value.date(), weekday=True) + (
                '' if event.all_day else ' ' + preferences.format_time(value))
        values = {'State': 'Event exists', 'Title': event.title, 'Start': stamp(event.start),
                  'End (exclusive)' if event.all_day else 'End': stamp(event.end),
                  'All day': 'Yes' if event.all_day else 'No', 'Location': event.location,
                  'Notes': event.description, 'Alternative slot': 'Yes' if event.candidate else 'No'}
        if event.recurring:
            values.update({'Repeat': event.recurrence_rule or 'Chosen dates',
                           'Additional dates': '\n'.join(event.recurrence_dates),
                           'Excluded dates': '\n'.join(event.excluded_dates),
                           'Modified occurrences': str(len(event.recurrence_overrides))})
        if event.schedule_expression:
            values['Custom schedule'] = event.schedule_expression
        return values

    def show_month(self, day):
        self.current_month = day.replace(day=1)
        for preview in self.previews:
            preview.show_month(day)
        self.month_label.setText(QLocale.system().toString(
            QDate(day.year, day.month, 1), 'MMMM yyyy'))

    def navigate_month(self, delta):
        year, month = divmod(self.current_month.year * 12 + self.current_month.month - 1 + delta, 12)
        if 1 <= year <= 9999:
            self.show_month(date(year, month + 1, 1))

    def showEvent(self, event):
        super().showEvent(event)
        self.comparison.resizeRowsToContents()

    def choose(self, choice):
        self.choice = choice
        self.accept()
