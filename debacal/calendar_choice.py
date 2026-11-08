"""One calendar picker for creation, importing, and exporting."""
from PySide6.QtCore import QRectF, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QComboBox

from .event_style import source_outline
from .preferences import Preferences
from .theme import corner_radius, scheme_for


def calendar_color_icon(value, preferences):
    """Use the source list's square swatch, including disabled edit targets."""
    colors = scheme_for(preferences)
    source = QColor(value)
    if not source.isValid():
        source = QColor(colors.accent)
    scale = preferences.font_scale / 100
    size = round(16 * scale)
    swatch = QPixmap(size * 2, size * 2)
    swatch.setDevicePixelRatio(2)
    swatch.fill(Qt.GlobalColor.transparent)
    painter = QPainter(swatch)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(source)
    painter.setPen(QPen(QColor(source_outline(source.darker(145).name(), colors.panel)), max(1, scale)))
    painter.drawRoundedRect(QRectF(2 * scale, 2 * scale, 12 * scale, 12 * scale), corner_radius(preferences, 2) * scale, corner_radius(preferences, 2) * scale)
    painter.end()
    result = QIcon(swatch)
    result.addPixmap(swatch, QIcon.Mode.Disabled)
    return result


class CalendarChoice(QComboBox):
    """Keep names, swatches, ordering, and destination filtering consistent."""
    def __init__(self, calendars, preferences=None, parent=None, *, selected_id=None,
                 writable_only=True, accessible_name='Calendar'):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self.writable_only = writable_only
        self._calendars = {}
        self.setAccessibleName(accessible_name)
        self.setObjectName('calendarPicker')
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(10)
        self.set_calendars(calendars, selected_id=selected_id)

    @property
    def selected_calendar(self):
        return self._calendars.get(self.currentData())

    def set_calendars(self, calendars, *, selected_id=None):
        calendars = tuple(calendars)
        previous = self.currentData()
        selected_id = self.currentData() if selected_id is None else selected_id
        blocker = QSignalBlocker(self)
        self.clear()
        self._calendars = {}
        for calendar in calendars:
            if self.writable_only and (calendar.read_only or calendar.kind == 'ics'):
                continue
            self._calendars[calendar.id] = calendar
            self.addItem(calendar_color_icon(calendar.color, self.preferences), calendar.name, calendar.id)
            self.setItemData(self.count() - 1, calendar.name + (' · Read-only' if calendar.read_only or calendar.kind == 'ics' else ''),
                             Qt.ItemDataRole.ToolTipRole)
        index = self.findData(selected_id)
        if index >= 0:
            self.setCurrentIndex(index)
        size = round(16 * self.preferences.font_scale / 100)
        self.setIconSize(QSize(size, size))
        blocker.unblock()
        if previous != self.currentData():
            self.currentIndexChanged.emit(self.currentIndex())
            self.currentTextChanged.emit(self.currentText())

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.set_calendars(self._calendars.values())
