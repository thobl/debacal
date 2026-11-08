"""Keyboard-operable choices painted with the workspace's shared group style."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QButtonGroup, QPushButton, QSizePolicy

from .workspace_header import SegmentedControl


class ChoiceControl(SegmentedControl):
    currentIndexChanged = Signal(int)
    currentChanged = Signal(object)

    def __init__(self, options, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.options = list(options)
        self.buttons = {}
        self.group = QButtonGroup(self)
        self._index = 0
        buttons = []
        for index, (caption, data) in enumerate(self.options):
            button = QPushButton(caption)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet('padding: 0 2px;')
            button.setToolTip(caption)
            button.clicked.connect(lambda _checked=False, value=index: self.setCurrentIndex(value))
            self.group.addButton(button)
            self.buttons_layout.addWidget(button)
            self.buttons[data] = button
            buttons.append(button)
        self.set_buttons(buttons)
        if buttons:
            buttons[0].setChecked(True)
        self.setFixedHeight(round(34 * preferences.font_scale / 100))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def count(self):
        return len(self.options)

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
            for button in self._buttons:
                button.setChecked(False)
            self.group.setExclusive(True)
        else:
            self._buttons[index].setChecked(True)
        self.update()
        if changed:
            self.currentIndexChanged.emit(index)
            self.currentChanged.emit(self.currentData())

    def setCurrentData(self, value):
        self.setCurrentIndex(self.findData(value))

    def resizeEvent(self, event):
        count = max(1, len(self._buttons))
        width, remainder = divmod(event.size().width(), count)
        for index, button in enumerate(self._buttons):
            button.setFixedSize(width + (index < remainder), self.height())
        super().resizeEvent(event)
