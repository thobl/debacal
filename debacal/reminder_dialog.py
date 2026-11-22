"""Paged view of reminders retained by this profile."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHeaderView, QHBoxLayout, QLineEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from .buttons import action_button
from .date_time_fields import parse_duration
from .dialog_base import dialog_buttons, hint, install_dialog_keyboard
from .preferences import Preferences
from .reminder_format import inbox_rows
from .timeutil import local_zone


class _ReminderRows(QTreeWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_columns()

    def fit_columns(self):
        width = self.viewport().width()
        time_share = round(width * .28)
        time_width = max(time_share, min(self.sizeHintForColumn(1), width // 2))
        self.setColumnWidth(1, time_width)
        self.setColumnWidth(2, max(round(width * .16), round(width * .22) - (time_width - time_share)))
        self.setColumnWidth(3, round(width * .18))


class ReminderDialog(QDialog):
    page_size = 50

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.preferences = getattr(parent, 'preferences', Preferences())
        self._store = getattr(parent, 'store', None)
        self.delivery_preferences = getattr(parent, 'reminder_preferences', getattr(controller, 'preferences', None))
        self.pending_action = self.action_error = None
        self.offset = 0
        self.partition = self.request = None
        self.setWindowTitle('Reminders')
        self.resize(680, 410)
        layout = QVBoxLayout(self)
        self.rows = _ReminderRows()
        self.rows.setRootIsDecorated(False)
        self.rows.setWordWrap(False)
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
        if hasattr(controller, 'actionFinished'):
            controller.actionFinished.connect(self._action_finished)
        if hasattr(controller, 'preferencesChanged'):
            controller.preferencesChanged.connect(self._settings_changed)
        self.finished.connect(self._finished)
        self.refresh()

    def _finished(self, *_):
        self.pending_action = self.action_error = None

    def _settings_changed(self, prefs):
        self.delivery_preferences = prefs
        self._selected()

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
        calendars = {cal.id: cal for cal in self._store.list_calendars(include_disabled=True)} if self._store else {}
        zone = local_zone()
        for entry, calendar, fields, tips in inbox_rows(entries[:self.page_size], self.preferences, zone, calendars):
            due = entry.reminder
            row = QTreeWidgetItem(fields)
            for column, tip in enumerate(tips):
                row.setToolTip(column, tip)
                row.setData(column, Qt.ItemDataRole.AccessibleDescriptionRole, tip)
            row.setData(2, Qt.ItemDataRole.UserRole, due.calendar_id)
            row.setData(2, Qt.ItemDataRole.UserRole + 1, calendar is None or calendar.enabled)
            row.setData(0, Qt.ItemDataRole.UserRole, due.key)
            font = row.font(0)
            font.setBold(entry.unread)
            row.setFont(0, font)
            self.rows.addTopLevelItem(row)
            if due.key == selected:
                self.rows.setCurrentItem(row)
        self.rows.blockSignals(False)
        self.rows.fit_columns()
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(len(entries) > self.page_size)
        self._selected()

    def key(self):
        row = self.rows.currentItem()
        return row.data(0, Qt.ItemDataRole.UserRole) if row else None

    def _selected(self, *_):
        key = self.key()
        identity = (self.partition, key)
        if self.pending_action and self.pending_action[1:3] != identity:
            self.pending_action = None
        if self.action_error and self.action_error[:2] != identity:
            self.action_error = None
        reason = ''
        prefs = self.delivery_preferences
        if key:
            row = self.rows.currentItem()
            cal = row.data(2, Qt.ItemDataRole.UserRole)
            policy = prefs.calendars.get(cal) if prefs else None
            if prefs and not prefs.enabled:
                reason = 'Turn on event notifications to snooze reminders.'
            elif not row.data(2, Qt.ItemDataRole.UserRole + 1):
                reason = 'Enable this calendar to snooze its reminders.'
            elif policy and policy.mode == 'mute':
                reason = 'Unmute this calendar to snooze its reminders.'
        available = bool(key) and not self.pending_action
        self.open_button.setEnabled(available)
        self.dismiss_button.setEnabled(available)
        self.snooze_button.setEnabled(available and not reason)
        self.snooze_duration.setEnabled(available and not reason)
        self.snooze_button.setToolTip(reason)
        self.snooze_button.setAccessibleDescription(reason)
        if self.request is not None:
            message = self.action_error[2] if self.action_error else ('' if self.rows.topLevelItemCount() else 'No reminders')
            self.status.setText(message)
            self.status.setVisible(bool(message))

    def _requested(self, request, action, key):
        self.pending_action = request, self.partition, key, action
        self.action_error = None
        self._selected()

    def _action_finished(self, request, partition, key, action, error):
        if self.pending_action != (request, partition, key, action) or (partition, key) != (self.partition, self.key()):
            return
        self.pending_action = None
        if error:
            self.action_error = partition, key, error
        elif action == 'open':
            self.accept()
            return
        self._selected()

    def _open(self, *_):
        if self.open_button.isEnabled() and (key := self.key()) is not None:
            self._requested(self.controller.open_reminder(key, partition=self.partition), 'open', key)

    def _dismiss(self):
        if (key := self.key()) is not None:
            self.controller.dismiss(key, partition=self.partition)

    def _snooze(self):
        if (key := self.key()) is None:
            return
        try:
            duration = parse_duration(self.snooze_duration.text())
            self._requested(self.controller.snooze(key, duration, partition=self.partition), 'snooze', key)
        except ValueError as error:
            self.status.setText(str(error))
            self.status.show()
            self.snooze_duration.setFocus()
