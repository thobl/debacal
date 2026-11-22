"""The calendar color palette and custom picker used by both source editors."""
from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import QComboBox, QColorDialog, QHBoxLayout, QSizePolicy, QWidget

from .buttons import icon_button
from .calendar_choice import calendar_color_icon
from .icons import icon
from .preferences import Preferences
from .theme import scheme_for


CALENDAR_COLORS = (
    ('Fern', '#5c8267'), ('Blue', '#5a7fa2'), ('Terracotta', '#bd775a'),
    ('Violet', '#8d79a8'), ('Gold', '#b4994f'), ('Red', '#c74444'),
    ('Rose', '#ce6989'), ('Orange', '#e28832'), ('Lemon', '#c4b939'),
    ('Lime', '#8eaa39'), ('Teal', '#30978c'), ('Cyan', '#35a8bf'),
    ('Sky', '#438ed0'), ('Indigo', '#5e67b1'), ('Purple', '#9556ad'),
    ('Plum', '#934970'), ('Brown', '#8d6651'), ('Slate', '#6e7f8b'),
    ('Graphite', '#515963'), ('Black', '#292c32'),
)


class CalendarColorChoice(QWidget):
    """Choose a preset or arbitrary color without writing calendar metadata."""
    changed = Signal(str)

    def __init__(self, value=None, parent=None, *, preferences=None):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.color = QComboBox(self)
        self.color.setAccessibleName('Calendar color')
        self.color.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.color.setMinimumContentsLength(8)
        self.color.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for name, color in CALENDAR_COLORS:
            self.add_color(name, color)
        layout.addWidget(self.color, 1)
        self.choose_color_button = icon_button('palette', 'Choose calendar color', self.choose_color,
            parent=self, preferences=self.preferences)
        self.choose_color_button.setShortcut(QKeySequence('Alt+L'))
        self.choose_color_button.setToolTip('Choose calendar color · Alt+L')
        layout.addWidget(self.choose_color_button)
        self.setFocusProxy(self.color)
        QWidget.setTabOrder(self.color, self.choose_color_button)
        self.set_preferences(self.preferences)
        if value is not None:
            self.set_color(value)
        self.color.currentIndexChanged.connect(self._selection_changed)

    def add_color(self, name, value):
        self.color.addItem(calendar_color_icon(value, self.preferences), name, value)
        self.color.setItemData(self.color.count() - 1, name + ' · ' + value, Qt.ItemDataRole.ToolTipRole)

    def set_color(self, value, *, label='Current', emit=False):
        """Loading a source must not turn its server color into an override."""
        blocker = QSignalBlocker(self.color)
        index = self.color.findData(value)
        if index < 0:
            self.add_color(value, value)
            index = self.color.count() - 1
            self.color.setItemData(index, label + ' · ' + value, Qt.ItemDataRole.ToolTipRole)
        self.color.setCurrentIndex(index)
        self.color.setToolTip(value)
        blocker.unblock()
        if emit:
            self.changed.emit(value)

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.color.currentData()), self, 'Choose calendar color')
        if color.isValid():
            self.set_color(color.name(), label='Custom', emit=True)

    def _selection_changed(self, _index):
        value = self.color.currentData()
        self.color.setToolTip(value)
        self.changed.emit(value)

    def set_preferences(self, preferences):
        self.preferences = preferences
        for index in range(self.color.count()):
            self.color.setItemIcon(index, calendar_color_icon(self.color.itemData(index), preferences))
        size = round(16 * preferences.font_scale / 100)
        self.color.setIconSize(QSize(size, size))
        scale = preferences.font_scale / 100
        color = scheme_for(preferences).accent
        self.choose_color_button.setIcon(icon('palette', color))
        self.choose_color_button.setProperty('interfaceIconColor', color)
        self.choose_color_button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        self.choose_color_button.setFixedSize(round(30 * scale), round(30 * scale))
