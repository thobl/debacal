"""Paged view of reminders retained by this profile."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHeaderView, QHBoxLayout, QLineEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from .buttons import action_button
from .date_time_fields import parse_duration
from .dialog_base import dialog_buttons, hint, install_dialog_keyboard
from .preferences import Preferences
from .timeutil import local_zone


class _ReminderRows(QTreeWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.viewport().width()
        for column, fraction in ((1, .28), (2, .22), (3, .18)):
            self.setColumnWidth(column, round(width * fraction))


class ReminderDialog(QDialog):
    page_size = 50

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.preferences = getattr(parent, 'preferences', Preferences())
        self._store = getattr(parent, 'store', None)
        self.offset = 0
        self.partition = self.request = None
        self.setWindowTitle('Reminders')
        self.resize(680, 410)
        layout = QVBoxLayout(self)
        self.rows = _ReminderRows()
        self.rows.setRootIsDecorated(False)
        self.rows.setAccessibleName('Reminders')
        self.rows.setHeaderLabels(['Event', 'Event time', 'Calendar', 'Status'])
        self.rows.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rows.header().setStretchLastSection(False)
        for column in (1, 2, 3):
            self.rows.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.rows.itemDoubleClicked.connect(self._open)
        self.rows.currentItemChanged.connect(self._selected)
        layout.addWidget(self.rows)
        self.status = hint('Loading reminders…')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.open_button = action_button('Open event', self._open)
        self.dismiss_button = action_button('Dismiss', self._dismiss)
        self.snooze_button = action_button('Snooze', self._snooze)
        self.snooze_duration = QLineEdit('5min')
        self.snooze_duration.setAccessibleName('Snooze duration')
        self.snooze_duration.setMaximumWidth(self.fontMetrics().horizontalAdvance('1h 30min') + 24)
        for button in (self.open_button, self.dismiss_button, self.snooze_button, self.snooze_duration):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        footer = QHBoxLayout()
        self.previous = action_button('Previous', lambda: self._page(-1))
        self.next = action_button('Next', lambda: self._page(1))
        footer.addWidget(self.previous)
        footer.addWidget(self.next)
        footer.addStretch()
        buttons = dialog_buttons(close=True)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        layout.addLayout(footer)
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self._selected()
        install_dialog_keyboard(self, self._open)
        controller.inboxPageSnapshot.connect(self._ready)
        controller.inboxChanged.connect(self.refresh)
        self.refresh()

    def _page(self, step):
        self.offset = max(0, self.offset + step * self.page_size)
        self.refresh()

    def refresh(self):
        self.request = self.controller.request_inbox(limit=self.page_size + 1, offset=self.offset, active_only=True)

    def _ready(self, request, entries, token):
        if request != self.request:
            return
        selected = self.key() if token == self.partition else None
        self.partition = token
        if not entries and self.offset:
            self._page(-1)
            return
        self.rows.blockSignals(True)
        self.rows.clear()
        calendar_names = {cal.id: cal.name for cal in self._store.list_calendars(include_disabled=True)} if self._store else {}
        for entry in entries[:self.page_size]:
            due = entry.reminder
            when = (due.start or due.key.trigger_utc).astimezone(local_zone())
            timestamp = self.preferences.format_date(when.date()) + ' ' + self.preferences.format_time(when)
            status = 'Unread' if entry.unread else entry.status.capitalize()
            fields = [due.title, timestamp, calendar_names.get(due.calendar_id, due.calendar_id), status]
            row = QTreeWidgetItem(fields)
            for column, text in enumerate(fields):
                row.setToolTip(column, text)
            if entry.snoozed_until:
                until = entry.snoozed_until.astimezone(local_zone())
                row.setToolTip(3, 'Snoozed until ' + self.preferences.format_date(until.date()) + ' '
                               + self.preferences.format_time(until))
            row.setData(0, Qt.ItemDataRole.UserRole, due.key)
            font = row.font(0)
            font.setBold(entry.unread)
            row.setFont(0, font)
            self.rows.addTopLevelItem(row)
            if due.key == selected:
                self.rows.setCurrentItem(row)
        self.rows.blockSignals(False)
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(len(entries) > self.page_size)
        self.status.setText('' if entries else 'No reminders')
        self.status.setVisible(not entries)
        self._selected()

    def key(self):
        row = self.rows.currentItem()
        return row.data(0, Qt.ItemDataRole.UserRole) if row else None

    def _selected(self, *_):
        for button in (self.open_button, self.dismiss_button, self.snooze_button):
            button.setEnabled(self.key() is not None)

    def _open(self, *_):
        if (key := self.key()) is not None:
            self.controller.open_reminder(key, partition=self.partition)
            self.accept()

    def _dismiss(self):
        if (key := self.key()) is not None:
            self.controller.dismiss(key, partition=self.partition)

    def _snooze(self):
        if (key := self.key()) is None:
            return
        try:
            duration = parse_duration(self.snooze_duration.text())
            self.controller.snooze(key, duration, partition=self.partition)
        except ValueError as error:
            self.status.setText(str(error))
            self.status.show()
            self.snooze_duration.setFocus()
