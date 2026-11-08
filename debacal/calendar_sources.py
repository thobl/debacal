"""Draft calendar subscriptions, aliases, and source colors in Preferences."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QDialog, QHeaderView, QStackedWidget, QSizePolicy,
)

from .buttons import action_button
from .calendar_choice import calendar_color_icon
from .credentials import account_key
from .discovery import discover_calendars
from .models import Calendar
from .preferences import Preferences


CALENDAR_ROLE = int(Qt.ItemDataRole.UserRole)
ACCOUNT_ROLE = CALENDAR_ROLE + 1


def collection_key(url):
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or '').lower(),
            parsed.port or (443 if parsed.scheme == 'https' else 80), unquote(parsed.path).rstrip('/'))


def source_identity(calendar):
    if calendar.kind == 'local':
        return 'local'
    return 'caldav:' + account_key(calendar) if calendar.kind == 'caldav' else 'ics'


def source_label(calendar):
    if calendar.kind == 'local':
        return 'On this device'
    if calendar.kind == 'ics':
        return 'Calendar subscriptions'
    parsed = urlsplit(calendar.url)
    deployment = parsed.path.split('/remote.php/', 1)[0] if '/remote.php/' in parsed.path else ''
    return (parsed.netloc + deployment + (' · ' + calendar.username if calendar.username else ''))


def account_address(calendar):
    parsed = urlsplit(calendar.url)
    deployment = parsed.path.split('/remote.php/', 1)[0] if '/remote.php/' in parsed.path else ''
    return parsed.scheme + '://' + parsed.netloc + deployment


class CalendarSourcesEditor(QWidget):
    """Keep source changes local until the containing Preferences applies them."""
    changed = Signal()
    credential_requested = Signal(str, str, bool)

    def __init__(self, store, preferences=None, parent=None, *, credentials=None,
                 executor=None, allow_remote=True):
        super().__init__(parent)
        self.store = store
        self.preferences = preferences or Preferences()
        self.credentials = credentials
        self.allow_remote = allow_remote
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-source-discovery')
        self._owns_executor = executor is None
        self._future = None
        self._active = True
        self._loading = False
        selected = store.get_default_calendar()
        self._selected = selected.id if selected else None
        self._selected_account = None
        self._password_account = None
        self._passwords = {}
        self._applied_passwords = {}
        self._originals = {calendar.id: calendar for calendar in store.list_calendars(include_disabled=True)}
        self._drafts = dict(self._originals)
        self._discovery = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        toolbar = QHBoxLayout()
        self.add_button = action_button('Add source…', self.add_source)
        toolbar.addWidget(self.add_button)
        toolbar.addStretch()
        outer.addLayout(toolbar)
        self.sources = QTreeWidget()
        self.sources.setHeaderHidden(True)
        self.sources.setColumnCount(2)
        self.sources.header().setStretchLastSection(False)
        self.sources.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.sources.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.sources.setMinimumHeight(180)
        self.sources.setAccessibleName('Calendar sources and connected calendars')
        self.sources.setRootIsDecorated(True)
        self.sources.setUniformRowHeights(True)
        self.sources.itemChanged.connect(self._enabled_changed)
        self.sources.currentItemChanged.connect(self._selection_changed)
        outer.addWidget(self.sources, 1)
        self.detail_pages = QStackedWidget()
        self.detail_pages.setAccessibleName('Selected calendar source settings')
        outer.addWidget(self.detail_pages)
        self.group_details = QWidget()
        group = QVBoxLayout(self.group_details)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(10)
        self.group_heading = QLabel()
        self.group_heading.setObjectName('sidebarSectionHeading')
        group.addWidget(self.group_heading)
        self.group_summary = self._information_label()
        group.addWidget(self.group_summary)
        self.account_details = QWidget()
        account_form = self.account_form = QFormLayout(self.account_details)
        account_form.setContentsMargins(0, 0, 0, 0)
        self.account_url = self._information_label(selectable=True)
        self.account_username = self._information_label(selectable=True)
        account_form.addRow('Server', self.account_url)
        account_form.addRow('User', self.account_username)
        group.addWidget(self.account_details)
        self.subscription_actions = QWidget()
        subscriptions = QHBoxLayout(self.subscription_actions)
        subscriptions.setContentsMargins(0, 0, 0, 0)
        self.enable_all_button = action_button('Connect all', lambda: self.set_account_enabled(True))
        self.disable_all_button = action_button('Disconnect all', lambda: self.set_account_enabled(False))
        subscriptions.addWidget(self.enable_all_button)
        subscriptions.addWidget(self.disable_all_button)
        subscriptions.addStretch()
        group.addWidget(self.subscription_actions)
        self.detail_pages.addWidget(self.group_details)
        self.details = QWidget()
        form = self.calendar_form = QFormLayout(self.details)
        form.setContentsMargins(0, 0, 0, 0)
        self.alias = QLineEdit()
        self.alias.setAccessibleName('Calendar name in Debacal')
        self.alias.textEdited.connect(self._name_changed)
        self.server_name = self._information_label()
        self.source_url = self._information_label(selectable=True)
        self.calendar_access = self._information_label()
        self.alias_label = QLabel('Name')
        form.addRow(self.alias_label, self.alias)
        form.addRow('Source name', self.server_name)
        colors = QHBoxLayout()
        self.color_button = action_button('Choose color…', self.choose_color)
        self.server_color_button = action_button('Use server color', self.reset_server_color)
        colors.addWidget(self.color_button)
        colors.addWidget(self.server_color_button)
        colors.addStretch()
        form.addRow('Color', colors)
        self.address_label = QLabel('Address')
        form.addRow(self.address_label, self.source_url)
        form.addRow('Access', self.calendar_access)
        self.account_button = action_button('Account settings…', self.select_account)
        self.account_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        form.addRow('', self.account_button)
        self.detail_pages.addWidget(self.details)
        self.account_credentials = QWidget()
        credentials_layout = QVBoxLayout(self.account_credentials)
        credentials_layout.setContentsMargins(0, 0, 0, 0)
        credentials_layout.setSpacing(8)
        account = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setAccessibleName('Account password')
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Use saved account password')
        self.password.textEdited.connect(self._password_changed)
        self.remember_password = QCheckBox('Remember password')
        self.remember_password.setChecked(True)
        self.remember_password.toggled.connect(self._password_changed)
        account.addWidget(QLabel('Password'))
        account.addWidget(self.password, 1)
        account.addWidget(self.remember_password)
        credentials_layout.addLayout(account)
        self.discover_button = action_button('Discover calendars', self.refresh_account)
        credentials_layout.addWidget(self.discover_button, 0, Qt.AlignmentFlag.AlignLeft)
        group.addWidget(self.account_credentials)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setProperty('muted', True)
        self.message.hide()
        outer.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll_discovery)
        self._rebuild()

    @staticmethod
    def _information_label(*, selectable=False):
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setProperty('muted', True)
        label.setWordWrap(True)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _rebuild(self, selected=None):
        selected = self._selected if selected is None else selected
        self._loading = True
        try:
            self.sources.clear()
            self._groups, self._items = {}, {}
            for calendar in self._drafts.values():
                account = source_identity(calendar)
                group = self._groups.get(account)
                if group is None:
                    group = QTreeWidgetItem([source_label(calendar), ''])
                    group.setData(0, ACCOUNT_ROLE, account)
                    group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    heading_font = group.font(0)
                    heading_font.setBold(True)
                    group.setFont(0, heading_font)
                    self.sources.addTopLevelItem(group)
                    self._groups[account] = group
                status = 'Read-only' if calendar.read_only or calendar.kind == 'ics' else ''
                item = QTreeWidgetItem(group, [calendar.name, status])
                item.setData(0, CALENDAR_ROLE, calendar.id)
                item.setData(0, ACCOUNT_ROLE, account)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if calendar.enabled else Qt.CheckState.Unchecked)
                item.setIcon(0, calendar_color_icon(calendar.color, self.preferences))
                item.setToolTip(0, calendar.url or 'Stored on this device')
                self._items[calendar.id] = item
                group.setExpanded(True)
            self.sources.resizeColumnToContents(1)
            target = (self._items.get(selected) if selected is not None else self._groups.get(self._selected_account))
            target = target or next(iter(self._items.values()), None)
            self.sources.setCurrentItem(target)
        finally:
            self._loading = False
        self._selection_changed(self.sources.currentItem())

    def _selection_changed(self, item, _previous=None):
        if self._loading:
            return
        self._selected = item.data(0, CALENDAR_ROLE) if item is not None else None
        calendar = self._drafts.get(self._selected)
        account = item.data(0, ACCOUNT_ROLE) if item is not None else None
        self._selected_account = account
        representative = next((value for value in self._drafts.values() if source_identity(value) == account), None)
        self._loading = True
        try:
            self.detail_pages.setCurrentWidget(self.details if calendar else self.group_details)
            caldav = bool(representative and representative.kind == 'caldav')
            self.account_details.setVisible(caldav)
            self.subscription_actions.setVisible(caldav)
            self.account_credentials.setVisible(caldav and calendar is None and self.allow_remote)
            self.discover_button.setEnabled(self.allow_remote and self._future is None)
            self._password_account = account if caldav and calendar is None else None
            if calendar is None:
                self.group_heading.setText(source_label(representative) if representative else 'Calendar sources')
                self.account_url.setText(account_address(representative) if caldav else '')
                self.account_username.setText(representative.username if caldav else '')
                self.account_form.setRowVisible(self.account_username, bool(caldav and representative.username))
                if caldav:
                    hint = ('Use the checkboxes above to connect calendars. Disconnecting keeps local events and queued changes.'
                            if self.allow_remote else 'Account discovery and password changes are unavailable in this workspace.')
                elif representative and representative.kind == 'ics':
                    hint = 'Subscriptions are read-only and refresh from their source. Select a subscription to change its name or color.'
                else:
                    hint = 'These calendars are stored on this device. Select a calendar to change its name or color.'
                self.group_summary.setToolTip(hint)
                self.group_details.setToolTip(hint)
                self._update_connection_summary()
            self.calendar_form.setRowVisible(self.server_name, bool(calendar and calendar.kind == 'caldav'))
            self.calendar_form.setRowVisible(self.source_url, bool(calendar and calendar.kind != 'local'))
            self.calendar_form.setRowVisible(self.account_button, bool(calendar and calendar.kind == 'caldav'))
            self.alias_label.setText('Name in Debacal' if calendar and calendar.kind == 'caldav' else 'Name')
            self.alias.setAccessibleName('Calendar name in Debacal' if calendar and calendar.kind == 'caldav' else 'Calendar name')
            self.alias.setText(calendar.name_alias if calendar and calendar.kind == 'caldav' else calendar.name if calendar else '')
            self.alias.setPlaceholderText((calendar.server_name or calendar.name) if calendar else '')
            self.server_name.setText((calendar.server_name or calendar.name) if calendar and calendar.kind == 'caldav' else '')
            self.source_url.setText(calendar.url if calendar else '')
            self.address_label.setText('Subscription' if calendar and calendar.kind == 'ics' else 'Calendar address')
            if calendar:
                self.color_button.setIcon(calendar_color_icon(calendar.color, self.preferences))
                self.color_button.setToolTip(calendar.color)
                if calendar.kind == 'ics':
                    access = 'Read-only subscription'
                elif calendar.kind == 'caldav':
                    access = 'Read-only on server' if calendar.read_only else 'Read and write on server'
                else:
                    access = 'Read-only on this device' if calendar.read_only else 'Editable on this device'
                self.calendar_access.setText(access)
            self.server_color_button.setVisible(bool(calendar and calendar.kind == 'caldav' and calendar.server_color))
            self.account_button.setText('Account settings…')
            self.account_button.setToolTip(source_label(calendar) if calendar and calendar.kind == 'caldav' else '')
            staged = self._passwords.get(self._password_account)
            self.password.setText(staged[0] if staged else '')
            self.remember_password.setChecked(staged[1] if staged else True)
        finally:
            self._loading = False

    def _update_connection_summary(self):
        calendars = [calendar for calendar in self._drafts.values() if source_identity(calendar) == self._selected_account]
        count = len(calendars)
        connected = sum(calendar.enabled for calendar in calendars)
        kind = calendars[0].kind if calendars else 'local'
        noun = ('subscription' if count == 1 else 'subscriptions') if kind == 'ics' else ('calendar' if count == 1 else 'calendars')
        state = 'connected' if kind == 'caldav' else 'enabled'
        self.group_summary.setText(f'{connected} of {count} {noun} {state}')
        self.enable_all_button.setEnabled(connected < count)
        self.disable_all_button.setEnabled(connected > 0)

    def select_account(self):
        if self._selected_account in self._groups:
            self.sources.setCurrentItem(self._groups[self._selected_account])

    def set_account_enabled(self, enabled):
        if not self._selected_account or not self._selected_account.startswith('caldav:'):
            return
        for calendar in tuple(self._drafts.values()):
            if source_identity(calendar) == self._selected_account:
                self._items[calendar.id].setCheckState(0, Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)

    def _enabled_changed(self, item, column):
        if self._loading or column != 0:
            return
        identity = item.data(0, CALENDAR_ROLE)
        if identity in self._drafts:
            self._drafts[identity] = replace(self._drafts[identity], enabled=item.checkState(0) == Qt.CheckState.Checked)
            self._update_connection_summary()

    def _name_changed(self, text):
        if self._loading or self._selected not in self._drafts:
            return
        calendar = self._drafts[self._selected]
        alias = text.strip()
        if calendar.kind == 'caldav':
            source_name = calendar.server_name or calendar.name
            updated = replace(calendar, name=alias or source_name, name_alias=alias, server_name=source_name)
        else:
            # Keep the last valid value while the user temporarily clears a name.
            if not alias:
                return
            updated = replace(calendar, name=alias)
        self._drafts[calendar.id] = updated
        self._items[calendar.id].setText(0, updated.name)

    def choose_color(self):
        calendar = self._drafts.get(self._selected)
        if calendar is None:
            return
        color = QColorDialog.getColor(QColor(calendar.color), self, 'Choose calendar color')
        if color.isValid():
            self.set_color(calendar.id, color.name())

    def set_color(self, calendar_id, color):
        if not QColor(color).isValid():
            raise ValueError('Choose a valid calendar color.')
        calendar = self._drafts[calendar_id]
        self._drafts[calendar_id] = replace(calendar, color=QColor(color).name(), use_server_color=False)
        self._items[calendar_id].setIcon(0, calendar_color_icon(self._drafts[calendar_id].color, self.preferences))
        self._selection_changed(self.sources.currentItem())

    def reset_server_color(self):
        calendar = self._drafts.get(self._selected)
        if calendar and calendar.server_color:
            self._drafts[calendar.id] = replace(calendar, use_server_color=True)
            self._items[calendar.id].setIcon(0, calendar_color_icon(self._drafts[calendar.id].color, self.preferences))
            self._selection_changed(self.sources.currentItem())

    def _password_changed(self, *_):
        if not self._loading and self._password_account:
            if self.password.text():
                self._passwords[self._password_account] = (self.password.text(), self.remember_password.isChecked())
            else:
                self._passwords.pop(self._password_account, None)

    def add_calendars(self, calendars, *, password='', remember=True):
        selected = None
        for calendar in calendars:
            existing = next((value for value in self._drafts.values()
                if value.kind == calendar.kind and value.username == calendar.username
                and value.url and collection_key(value.url) == collection_key(calendar.url)), None)
            if existing:
                source_name = calendar.server_name or calendar.name
                alias = existing.name_alias or (existing.name if not existing.server_name and existing.name != source_name else '')
                calendar = replace(existing, enabled=True, server_name=source_name, name=alias or source_name, name_alias=alias,
                    server_color=calendar.server_color, read_only=calendar.read_only)
            self._drafts[calendar.id] = calendar
            selected = selected or calendar.id
            if password and calendar.kind == 'caldav':
                self._passwords[source_identity(calendar)] = (password, bool(remember))
        self._rebuild(selected)

    def add_source(self):
        from .dialogs import CalendarDialog
        dialog = CalendarDialog(parent=self, allow_remote=self.allow_remote, credentials=self.credentials)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.add_calendars(dialog.result_calendars, password=dialog.password.text(), remember=dialog.remember_password.isChecked())
        finally:
            dialog.deleteLater()

    def refresh_account(self):
        if self._future is not None:
            return
        item = self.sources.currentItem()
        account = item.data(0, ACCOUNT_ROLE) if item is not None else None
        calendar = next((value for value in self._drafts.values() if source_identity(value) == account and value.kind == 'caldav'), None)
        if calendar is None or not self.allow_remote:
            return
        self._password_changed()
        submitted = self._passwords.get(account)
        def work():
            password = submitted[0] if submitted else self.credentials.get(calendar) if self.credentials else ''
            return discover_calendars(calendar.url, calendar.username, password or '')
        self._discovery = account, calendar
        self._future = self._executor.submit(work)
        self.discover_button.setEnabled(False)
        self._set_message('Discovering account calendars…')
        self.timer.start()

    def poll_discovery(self):
        if not self._active or self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        self.timer.stop()
        account, template = self._discovery
        try:
            found = future.result()
            for metadata in found:
                existing = next((value for value in self._drafts.values() if source_identity(value) == account
                                 and collection_key(value.url) == collection_key(metadata.url)), None)
                if existing:
                    alias = existing.name_alias or (existing.name if not existing.server_name and existing.name != metadata.name else '')
                    self._drafts[existing.id] = replace(existing, server_name=metadata.name,
                        name=alias or metadata.name, name_alias=alias, server_color=metadata.color,
                        read_only=metadata.read_only)
                else:
                    calendar = Calendar(str(uuid4()), metadata.name, metadata.color, kind='caldav', url=metadata.url,
                        username=template.username, read_only=metadata.read_only, server_color=metadata.color,
                        server_name=metadata.name, enabled=False)
                    self._drafts[calendar.id] = calendar
            self._rebuild()
            self._set_message(f'Found {len(found)} account calendars. Select which calendars to connect, then Apply.')
        except Exception as error:
            self._set_message('Discovery failed: ' + str(error), error=True)
            self._selection_changed(self.sources.currentItem())

    def _set_message(self, text, *, error=False):
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self.message.setProperty('error', error)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def validate(self):
        if self._future is not None:
            if not self._future.done():
                raise ValueError('Wait for account discovery to finish before applying source changes.')
            self.poll_discovery()
        if self._selected:
            calendar = self._drafts[self._selected]
            if calendar.kind != 'caldav' and not self.alias.text().strip():
                self.alias.setFocus()
                raise ValueError('Enter a calendar name.')

    def apply(self):
        self.validate()
        changed = self.store.apply_calendar_preferences(self._originals, self._drafts.values())
        by_account = {}
        for calendar in self._drafts.values():
            if calendar.kind == 'caldav' and calendar.enabled:
                by_account.setdefault(source_identity(calendar), calendar)
        for account, password in self._passwords.items():
            if account in by_account and self._applied_passwords.get(account) != password:
                self.credential_requested.emit(by_account[account].id, password[0], password[1])
                self._applied_passwords[account] = password
                changed = True
        self._originals = {calendar.id: calendar for calendar in self.store.list_calendars(include_disabled=True)}
        self._drafts = dict(self._originals)
        self._rebuild()
        if changed:
            self.changed.emit()
        return changed

    def set_preferences(self, preferences):
        self.preferences = preferences
        self._rebuild()

    def finish(self):
        self._active = False
        self.timer.stop()
        if self._future:
            self._future.cancel()
        self._passwords.clear()
        self._applied_passwords.clear()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
