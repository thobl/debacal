"""Shared native action buttons for the workspace and its dialogs."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton

from .icons import icon
from .preferences import Preferences
from .theme import scheme_for


def action_button(text='', action=None, *, parent=None, primary=False, tooltip=None):
    button = QPushButton(text, parent)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        button.setObjectName('primary')
    if tooltip:
        button.setToolTip(tooltip)
    if action is not None:
        button.clicked.connect(lambda _checked=False: action())
    return button


def icon_button(name, caption, action=None, *, preferences=None, parent=None, size=30):
    preferences = preferences or Preferences()
    button = action_button(action=action, parent=parent, tooltip=caption)
    color = scheme_for(preferences).accent
    scale = preferences.font_scale / 100
    button.setIcon(icon(name, color))
    button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
    button.setProperty('iconName', name)
    button.setProperty('interfaceIconColor', color)
    button.setProperty('iconOnly', True)
    button.setAccessibleName(caption)
    button.setFixedSize(round(size * scale), round(size * scale))
    return button
