"""Calendar creation targets and independent visibility controls."""
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSizePolicy, QPushButton

from .compact_controls import ElidedButton
from .icons import icon


class CalendarRow(QWidget):
    creation_requested = Signal(str)
    visibility_requested = Signal(str, bool)
    context_requested = Signal(str, object)

    def __init__(self, calendar, parent=None):
        super().__init__(parent)
        self.calendar = calendar
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.swatch = QLabel()
        self.swatch.setFixedWidth(5)
        self.swatch.setFixedHeight(18)
        layout.addWidget(self.swatch)
        self.choice = ElidedButton()
        self.choice.setObjectName('calendarChoice')
        self.choice.setCheckable(True)
        self.choice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.choice.clicked.connect(lambda: self.creation_requested.emit(self.calendar.id))
        layout.addWidget(self.choice, 1)
        self.eye = QPushButton()
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

    def set_calendar(self, calendar, current=False, scale=100):
        self.calendar = calendar
        writable = calendar.kind != 'ics' and not calendar.read_only
        self.swatch.setStyleSheet(f'background: {calendar.color}; border-radius: 2px;')
        self.choice.setText(calendar.name.replace('&', '&&'))
        checked = bool(current and writable)
        group = self.choice.group()
        exclusive = bool(group and group.exclusive())
        if exclusive and not checked:
            group.setExclusive(False)
        self.choice.setChecked(checked)
        if exclusive and not checked:
            group.setExclusive(True)
        self.choice.setEnabled(writable)
        self.choice.setAccessibleName('Create events in ' + calendar.name if writable else calendar.name + ' (read only)')
        self.choice.setToolTip(('Create new events in ' + calendar.name + ('. Current creation calendar.' if current else '.')
                               if writable else calendar.name + ' is read only.') + '\nRight-click for calendar actions.')
        self.eye.setChecked(calendar.visible)
        self.eye.setIcon(icon('eye' if calendar.visible else 'eye_off'))
        title = ('Hide ' if calendar.visible else 'Show ') + calendar.name
        self.eye.setToolTip(title)
        self.eye.setAccessibleName(title)
        self.eye.setFixedSize(round(28 * scale / 100), round(28 * scale / 100))
