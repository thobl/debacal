"""One place to inspect calendar issues and take an explicit recovery action."""
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                              QListWidgetItem, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget)


@dataclass(frozen=True, slots=True)
class StatusIssue:
    key: str
    title: str
    details: str
    action_label: str = ''
    action: Callable | None = field(default=None, compare=False, repr=False)
    dismiss: Callable | None = field(default=None, compare=False, repr=False)
    close_on_action: bool = True


class StatusDialog(QDialog):
    def __init__(self, issue_supplier, sync_supplier, parent=None, *, show_issues=False,
                 retry_sync=None, preferences=None, action_error=None):
        super().__init__(parent)
        self.setWindowTitle('Calendar status and issues')
        self.resize(760, 500)
        self._issue_supplier, self._sync_supplier = issue_supplier, sync_supplier
        self._action_error = action_error
        self._issues = {}
        self._signature = None
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        issues_page = QWidget()
        page = QVBoxLayout(issues_page)
        self.empty = QLabel('No outstanding issues.')
        page.addWidget(self.empty)
        columns = QHBoxLayout()
        self.list = QListWidget()
        self.list.setAccessibleName('Warnings and errors')
        self.list.setMaximumWidth(280)
        columns.addWidget(self.list, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName('Issue details')
        columns.addWidget(self.details, 2)
        page.addLayout(columns, 1)
        actions = QHBoxLayout()
        self.action_button = QPushButton()
        self.action_button.setAutoDefault(False)
        self.action_button.clicked.connect(self._run_action)
        actions.addWidget(self.action_button)
        self.dismiss_button = QPushButton('Dismiss')
        self.dismiss_button.setAutoDefault(False)
        self.dismiss_button.clicked.connect(self._dismiss)
        actions.addWidget(self.dismiss_button)
        actions.addStretch()
        page.addLayout(actions)
        self.tabs.addTab(issues_page, 'Issues')
        self.sync_details = QTextEdit()
        self.sync_details.setReadOnly(True)
        self.sync_details.setAccessibleName('Synchronization details')
        self.tabs.addTab(self.sync_details, 'Synchronization')
        footer = QHBoxLayout()
        if retry_sync is not None:
            button = QPushButton('Retry synchronization')
            button.setAutoDefault(False)
            button.clicked.connect(retry_sync)
            footer.addWidget(button)
        if preferences is not None:
            button = QPushButton('Preferences…')
            button.setAutoDefault(False)
            button.clicked.connect(lambda: self._open_preferences(preferences))
            footer.addWidget(button)
        footer.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        footer.addWidget(close)
        outer.addLayout(footer)
        self.list.currentItemChanged.connect(self._select)
        self.refresh()
        self.tabs.setCurrentIndex(0 if show_issues else 1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def refresh(self):
        values = self._issue_supplier()
        signature = tuple((value.key, value.title, value.details, value.action_label) for value in values)
        if signature != self._signature:
            current = self.list.currentItem()
            selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
            self._signature = signature
            self._issues = {value.key: value for value in values}
            self.list.clear()
            for value in values:
                item = QListWidgetItem(value.title)
                item.setData(Qt.ItemDataRole.UserRole, value.key)
                item.setToolTip(value.details)
                self.list.addItem(item)
                if value.key == selected:
                    self.list.setCurrentItem(item)
            if self.list.currentItem() is None and values:
                self.list.setCurrentRow(0)
            self.empty.setVisible(not values)
            self.list.setVisible(bool(values))
            self.details.setVisible(bool(values))
            self._select(self.list.currentItem())
        text = self._sync_supplier()
        if self.sync_details.toPlainText() != text:
            scrollbar = self.sync_details.verticalScrollBar()
            position = scrollbar.value()
            self.sync_details.setPlainText(text)
            scrollbar.setValue(position)

    def _selected(self):
        item = self.list.currentItem()
        return self._issues.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _select(self, *_):
        issue = self._selected()
        self.details.setPlainText(issue.title + '\n\n' + issue.details if issue else '')
        self.action_button.setText(issue.action_label if issue else '')
        self.action_button.setVisible(bool(issue and issue.action))
        self.dismiss_button.setVisible(bool(issue and issue.dismiss))

    def _run_action(self):
        issue = self._selected()
        if issue is None or issue.action is None:
            return
        if issue.close_on_action:
            self.accept()
        try:
            issue.action()
        except Exception as exc:
            if self._action_error is not None:
                self._action_error(str(exc))
        self.refresh()

    def _dismiss(self):
        issue = self._selected()
        if issue is not None and issue.dismiss is not None:
            issue.dismiss()
            self.refresh()

    def _open_preferences(self, callback):
        self.accept()
        callback()

    def done(self, result):
        self.timer.stop()
        super().done(result)
