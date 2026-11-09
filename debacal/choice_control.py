"""Keyboard-operable choices painted with the workspace's shared group style."""
from PySide6.QtCore import Signal, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QButtonGroup, QSizePolicy

from .workspace_header import SegmentedControl
from .buttons import action_button
from .icons import icon
from .theme import scheme_for


class ChoiceControl(SegmentedControl):
    currentIndexChanged = Signal(int)
    currentChanged = Signal(object)

    def __init__(self, options, preferences, parent=None, *, icons=None, icon_only=False, always_show_labels=False):
        super().__init__(parent)
        self.preferences = preferences
        self.options = list(options)
        self.icons = icons or {}
        self.icon_only = bool(icon_only)
        self.always_show_labels = bool(always_show_labels) and not self.icon_only
        self._choice_icons = {}
        self.buttons = {}
        self.group = QButtonGroup(self)
        self._index = 0
        buttons = []
        for index, (caption, data) in enumerate(self.options):
            button = action_button(caption)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setAccessibleName(caption)
            if data in self.icons:
                button.setIcon(icon(self.icons[data], scheme_for(preferences).accent))
                self._choice_icons[data] = button.icon()
                button.setIconSize(QSize(round(18 * preferences.font_scale / 100), round(18 * preferences.font_scale / 100)))
                if self.icon_only:
                    button.setText('')
            button.setStyleSheet('padding: 0 2px;')
            button.setToolTip(caption)
            button.clicked.connect(lambda _checked=False, value=index: self.setCurrentIndex(value))
            self.group.addButton(button)
            self.buttons_layout.addWidget(button)
            self.buttons[data] = button
            buttons.append(button)
        self.set_buttons(buttons)
        self._choice_buttons = buttons
        self._option_visible = [True] * len(buttons)
        if buttons:
            buttons[0].setChecked(True)
        self.setFixedHeight(round(34 * preferences.font_scale / 100))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def count(self):
        return len(self.options)

    def minimumSizeHint(self):
        # Hidden stacked pages must not retain an earlier button-width minimum.
        if self.always_show_labels:
            labels = [button.fontMetrics().horizontalAdvance(caption) + 8
                      for (caption, data), button, visible in zip(self.options, self._choice_buttons, self._option_visible)
                      if visible]
            return QSize(max(labels, default=0) * len(labels), self.height())
        return QSize(0, self.height())

    def currentIndex(self):
        return self._index

    def currentData(self):
        return self.options[self._index][1] if self.options and self._index >= 0 else None

    def findData(self, value):
        return next((index for index, (_caption, data) in enumerate(self.options) if value == data), -1)

    def setCurrentIndex(self, index):
        if not -1 <= index < len(self.options):
            return
        changed = index != self._index
        self._index = index
        if index < 0:
            self.group.setExclusive(False)
            for button in self._choice_buttons:
                button.setChecked(False)
            self.group.setExclusive(True)
        else:
            self._choice_buttons[index].setChecked(True)
        self.update()
        if changed:
            self.currentIndexChanged.emit(index)
            self.currentChanged.emit(self.currentData())

    def setCurrentData(self, value):
        self.setCurrentIndex(self.findData(value))

    def setOptionVisible(self, value, visible):
        index = self.findData(value)
        if index < 0 or self._option_visible[index] == bool(visible):
            return
        self._option_visible[index] = bool(visible)
        self._choice_buttons[index].setVisible(visible)
        self._buttons = [button for button, shown in zip(self._choice_buttons, self._option_visible) if shown]
        self._arrange_buttons(self.size())
        self.updateGeometry()
        self.update()

    def visibleCount(self):
        return sum(self._option_visible)

    def resizeEvent(self, event):
        self._arrange_buttons(event.size())
        super().resizeEvent(event)

    def _arrange_buttons(self, size):
        choices = [(index, button) for index, button in enumerate(self._choice_buttons) if self._option_visible[index]]
        width, remainder = divmod(size.width(), max(1, len(choices)))
        captions_fit = all(self.fontMetrics().horizontalAdvance(caption) +
                           (button.iconSize().width() + 8 if data in self.icons else 4) <= width
                           for index, button in choices for caption, data in [self.options[index]])
        for position, (index, button) in enumerate(choices):
            if self.icons:
                caption, data = self.options[index]
                if self.always_show_labels:
                    chosen_icon = self._choice_icons.get(data, QIcon()) if captions_fit else QIcon()
                    if chosen_icon.cacheKey() != button.icon().cacheKey():
                        button.setIcon(chosen_icon)
                button.setText(caption if self.always_show_labels or captions_fit and not self.icon_only else '')
            button.setFixedSize(width + (position < remainder), self.height())
