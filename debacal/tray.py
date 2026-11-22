"""The workspace's tray icon and its desktop-specific actions."""
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .icons import icon


class TrayController(QObject):
    showRequested = Signal()
    quitRequested = Signal()
    availabilityChanged = Signal(bool)
    conflictRequested = Signal()

    def __init__(self, window: QWidget, *, profile_label: str, enabled: bool = False):
        super().__init__(window)
        self._enabled = enabled
        self._visible = False
        self._title = 'Debacal' + (f' · {profile_label}' if profile_label else '')
        self._icon = QSystemTrayIcon(window)
        self._menu = QMenu(window)
        self._menu.addAction('Show Debacal', self.showRequested.emit)
        self._menu.addAction('Quit Debacal', self.quitRequested.emit)
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._activated)
        self._icon.messageClicked.connect(self.conflictRequested)
        self.set_unread(0)
        self._available = QSystemTrayIcon.isSystemTrayAvailable()
        self._update_visibility()
        # Qt exposes a capability query, but no tray-host availability signal.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.available)
        self._timer.start(1000)

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.showRequested.emit()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.available()
        self._update_visibility()

    def _update_visibility(self):
        visible = self._enabled and self._available
        if visible != self._visible:
            self._visible = visible
            if visible:
                self._icon.show()
            else:
                self._icon.hide()

    def available(self) -> bool:
        # Sample synchronously for close-to-tray as well. A short-lived host
        # can appear at close time and vanish before the next timer turn.
        available = QSystemTrayIcon.isSystemTrayAvailable()
        if available != self._available:
            self._available = available
            self._update_visibility()
            self.availabilityChanged.emit(available)
        return available

    def set_unread(self, count: int):
        self._icon.setToolTip(self._title + (f' · {count} unread' if count else ''))
        self._icon.setIcon(icon('reminders_unread' if count else 'month'))

    def notify_conflict(self, message):
        if self.available() and QSystemTrayIcon.supportsMessages():
            self._icon.showMessage('Calendar conflict', message, QSystemTrayIcon.MessageIcon.Information)

    def close(self):
        self._timer.stop()
        self._enabled = False
        self._update_visibility()
