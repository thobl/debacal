"""Calendar creation targets and independent visibility controls."""
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSizePolicy, QPushButton

from .compact_controls import ElidedButton
from .event_style import source_outline
from .icons import icon
from .theme import scheme_for


class CalendarRow(QWidget):
    creation_requested = Signal(str)
    visibility_requested = Signal(str, bool)
    context_requested = Signal(str, object)

    def __init__(self, calendar, parent=None):
        super().__init__(parent)
        self.calendar = calendar
        self.preferences = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.swatch = QLabel()
        self.swatch.setFixedSize(12, 12)
        layout.addWidget(self.swatch)
        self.choice = ElidedButton()
        self.choice.setObjectName('calendarChoice')
        self.choice.setCheckable(True)
        self.choice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.choice.clicked.connect(lambda: self.creation_requested.emit(self.calendar.id))
        layout.addWidget(self.choice, 1)
        self.eye = QPushButton()
        self.eye.setObjectName('calendarVisibility')
        self.eye.setProperty('iconOnly', True)
        self.eye.setIconSize(QSize(18, 18))
        self.eye.setCheckable(True)
        self.eye.clicked.connect(lambda checked: self.visibility_requested.emit(self.calendar.id, checked))
        layout.addWidget(self.eye)
        for target in (self, self.choice):
            target.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            target.customContextMenuRequested.connect(lambda point, source=target:
                self.context_requested.emit(self.calendar.id, source.mapToGlobal(point)))
        self.set_calendar(calendar)

    def set_preferences(self, preferences):
        """Update per-row colors and scale without changing its source selection."""
        self.preferences = preferences
        self.set_calendar(self.calendar, current=self.choice.isChecked(), scale=preferences.font_scale)

    def set_calendar(self, calendar, current=False, scale=100):
        self.calendar = calendar
        scheme = scheme_for(self.preferences if self.preferences is not None else self)
        factor = scale / 100
        writable = calendar.kind != 'ics' and not calendar.read_only
        source = QColor(calendar.color)
        source = source.name() if source.isValid() else scheme.accent
        edge = source_outline(QColor(source).darker(145).name(), scheme.sidebar)
        self.swatch.setFixedSize(round(12 * factor), round(12 * factor))
        self.swatch.setStyleSheet(f'background: {source}; border: {max(1, round(factor))}px solid {edge}; '
                                 f'border-radius: {round(2 * factor)}px;')
        self.swatch.setAccessibleName(calendar.name + ' calendar color')
        self.choice.setText(calendar.name.replace('&', '&&'))
        checked = bool(current and writable)
        group = self.choice.group()
        exclusive = bool(group and group.exclusive())
        if exclusive and not checked:
            group.setExclusive(False)
        self.choice.setChecked(checked)
        if exclusive and not checked:
            group.setExclusive(True)
        font = self.choice.font()
        font.setBold(checked)
        font.setUnderline(checked)
        self.choice.setFont(font)
        self.choice.setMinimumHeight(round(32 * factor))
        self.setMinimumHeight(round(32 * factor))
        self.layout().setSpacing(round(8 * factor))
        self.choice.setEnabled(writable)
        self.choice.setAccessibleName('Create events in ' + calendar.name if writable else calendar.name + ' (read only)')
        self.choice.setToolTip(('Create new events in ' + calendar.name + ('. Current creation calendar.' if current else '.')
                               if writable else calendar.name + ' is read only.') + '\nRight-click for calendar actions.')
        self.eye.setChecked(calendar.visible)
        self.eye.setIcon(icon('eye' if calendar.visible else 'eye_off', scheme.muted))
        self.eye.setIconSize(QSize(round(18 * factor), round(18 * factor)))
        title = ('Hide ' if calendar.visible else 'Show ') + calendar.name
        self.eye.setToolTip(title)
        self.eye.setAccessibleName(title)
        self.eye.setFixedSize(round(28 * scale / 100), round(28 * scale / 100))
