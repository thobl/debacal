"""Calendar source and history dialogs; event editing has its own module."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QCheckBox, QLabel, QPushButton, QDialogButtonBox, QListWidget, QListWidgetItem,
    QWidget, QColorDialog)

from .models import Calendar
from .dialog_base import FormDialog, dialog_buttons, hint, install_dialog_keyboard
from .event_dialog import EventDialog


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
        buttons = dialog_buttons()
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.buttons = buttons
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.save_button.setText('Save')
        self.cancel_button.setText('Cancel')
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
                username=self.username.text().strip(), read_only=source.read_only, server_name=source.name)
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
            enabled=self.original.enabled if self.original else True,
            server_name=self.original.server_name if self.original else '',
            name_alias=(self.name.text().strip() if self.original and self.original.kind == 'caldav'
                        and self.name.text().strip() != (self.original.server_name or self.original.name)
                        else self.original.name_alias if self.original and self.name.text().strip() == self.original.name else ''),
        )
        self.result_calendars = [self.result_calendar]
        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, store, changed, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Operation history')
        self.resize(600, 430)
        self.store, self.changed = store, changed
        self._writing = False
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
        install_dialog_keyboard(self, self.accept)

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
        self.undo_btn.setEnabled(not self._writing and op.get('can_undo', False))
        self.redo_btn.setEnabled(not self._writing and op.get('can_redo', False))

    def perform(self, redo):
        item = self.list.currentItem()
        if not item or self._writing:
            return
        from .ui_database import submit_write
        identity = item.data(Qt.ItemDataRole.UserRole)['id']
        self._writing = True
        self.selection()

        def succeeded(_value):
            self._writing = False
            self.message.clear()
            self.changed()
            self.reload()

        def failed(error):
            self._writing = False
            self.message.setText(str(error))
            self.selection()

        submit_write(self, self.store, lambda writer: (writer.redo if redo else writer.undo)(identity),
                     on_success=succeeded, on_error=failed)
