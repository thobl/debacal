"""Calendar creation targets and independent visibility controls."""
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QDrag
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy, QPushButton, QScrollArea

from .compact_controls import ElidedButton
from .event_style import source_outline
from .icons import icon
from .theme import corner_radius, scheme_for


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
        self.choice.setProperty('focusFrame', False)
        self.choice.setCheckable(True)
        self.choice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.choice.clicked.connect(lambda: self.creation_requested.emit(self.calendar.id))
        layout.addWidget(self.choice, 1)
        self.lock = QLabel()
        self.lock.setObjectName('calendarReadOnly')
        self.lock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lock)
        self.eye = QPushButton()
        self.eye.setObjectName('calendarVisibility')
        self.eye.setProperty('iconOnly', True)
        self.eye.setIconSize(QSize(18, 18))
        self.eye.setCheckable(True)
        self.eye.clicked.connect(lambda checked: self.visibility_requested.emit(self.calendar.id, checked))
        layout.addWidget(self.eye)
        for target in (self, self.choice, self.lock):
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
                                 f'border-radius: {round(corner_radius(self.preferences, 2) * factor)}px;')
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
        self.setAccessibleName(calendar.name + (' calendar' if writable else ' calendar (read only)'))
        self.lock.setFixedSize(round(16 * factor), round(18 * factor))
        self.lock.setPixmap(icon('lock', scheme.muted).pixmap(QSize(round(14 * factor), round(14 * factor))))
        self.lock.setToolTip('Read-only calendar. Events cannot be created or changed here.')
        self.lock.setAccessibleName(calendar.name + ' (read only)')
        self.lock.setVisible(not writable)
        self.eye.setChecked(calendar.visible)
        self.eye.setIcon(icon('eye' if calendar.visible else 'eye_off', scheme.muted))
        self.eye.setIconSize(QSize(round(18 * factor), round(18 * factor)))
        title = ('Hide ' if calendar.visible else 'Show ') + calendar.name
        self.eye.setToolTip(title)
        self.eye.setAccessibleName(title)
        self.eye.setFixedSize(round(28 * scale / 100), round(28 * scale / 100))


CALENDAR_ORDER_MIME = 'application/x-debacal-calendar-order'


class CalendarList(QWidget):
    """A source-only drag target; calendar/event data never enters the payload."""
    order_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('calendarListBody')
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self._pressed_row = None
        self._press_global = None
        self._drop_slot = None
        self._drag_global = None
        self._indicator = QWidget(self)
        self._indicator.setObjectName('calendarDropIndicator')
        self._indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._indicator_color = None
        self._indicator.hide()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._scroll_drag)

    def rows(self):
        return [self.box.itemAt(index).widget() for index in range(self.box.count())
                if isinstance(self.box.itemAt(index).widget(), CalendarRow)]

    def ordered_ids(self):
        return [row.calendar.id for row in self.rows()]

    def attach_row(self, row):
        """Watch drag gestures without changing native clicks or eye actions."""
        row._calendar_list = self
        for target in (row, row.choice, row.swatch, row.lock):
            target.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            row = watched if isinstance(watched, CalendarRow) else watched.parentWidget()
            if isinstance(row, CalendarRow) and getattr(row, '_calendar_list', None) is self:
                self._pressed_row = row
                self._press_global = event.globalPosition().toPoint()
        elif event.type() == QEvent.Type.MouseButtonRelease:
            self._pressed_row = self._press_global = None
        elif (event.type() == QEvent.Type.MouseMove and self._pressed_row is not None
              and event.buttons() & Qt.MouseButton.LeftButton):
            if (event.globalPosition().toPoint() - self._press_global).manhattanLength() >= QApplication.startDragDistance():
                row, anchor = self._pressed_row, self._press_global
                self._pressed_row = self._press_global = None
                row.choice.setDown(False)
                self.start_drag(row, anchor)
                return True
        return super().eventFilter(watched, event)

    def start_drag(self, row, anchor):
        if row not in self.rows():
            return
        drag = QDrag(row)
        mime = QMimeData()
        mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode('utf-8'))
        drag.setMimeData(mime)
        drag.setPixmap(row.grab())
        drag.setHotSpot(row.mapFromGlobal(anchor))
        try:
            drag.exec(Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)
        finally:
            row.choice.setDown(False)
            self._clear_drop()
            drag.deleteLater()

    def _drag_id(self, event):
        source = event.source()
        if not isinstance(source, CalendarRow) or getattr(source, '_calendar_list', None) is not self:
            return None
        if source not in self.rows() or not event.mimeData().hasFormat(CALENDAR_ORDER_MIME):
            return None
        payload = bytes(event.mimeData().data(CALENDAR_ORDER_MIME))
        return source.calendar.id if payload == source.calendar.id.encode('utf-8') else None

    def slot_at(self, point):
        """Insertion slots include both endpoints, independent of row height."""
        rows = self.rows()
        return next((index for index, row in enumerate(rows) if point.y() < row.geometry().center().y()), len(rows))

    def _set_drop(self, point):
        self._drop_slot = self.slot_at(point)
        rows = self.rows()
        if not rows:
            return
        if self._drop_slot == 0:
            y = rows[0].geometry().top() + 1
        elif self._drop_slot == len(rows):
            y = rows[-1].geometry().bottom() - 1
        else:
            y = (rows[self._drop_slot - 1].geometry().bottom() + rows[self._drop_slot].geometry().top()) / 2
        color = scheme_for(rows[0].preferences).accent
        if color != self._indicator_color:
            self._indicator.setStyleSheet('QWidget#calendarDropIndicator { background: ' + color + '; border: 0; }')
            self._indicator_color = color
        self._indicator.setGeometry(1, max(0, round(y) - 1), max(0, self.width() - 2), 2)
        self._indicator.raise_()
        self._indicator.show()

    def _clear_drop(self):
        self._drop_slot = self._drag_global = None
        self._scroll_timer.stop()
        self._indicator.hide()

    def _accept_drag(self, event):
        if self._drag_id(event) is None or not event.possibleActions() & Qt.DropAction.MoveAction:
            event.ignore()
            return False
        self._set_drop(event.position())
        self._drag_global = self.mapToGlobal(event.position().toPoint())
        self._scroll_timer.start()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        return True

    def dragEnterEvent(self, event):
        self._accept_drag(event)

    def dragMoveEvent(self, event):
        self._accept_drag(event)

    def dragLeaveEvent(self, event):
        self._clear_drop()
        event.accept()

    def move_calendar(self, calendar_id, slot):
        rows = self.rows()
        source = next((row for row in rows if row.calendar.id == calendar_id), None)
        if source is None:
            return False
        previous = [row.calendar.id for row in rows]
        old = rows.index(source)
        rows.pop(old)
        slot = max(0, min(len(rows) + 1, slot)) - int(old < slot)
        rows.insert(slot, source)
        order = [row.calendar.id for row in rows]
        if order == previous:
            return False
        for index, row in enumerate(rows):
            self.box.insertWidget(index, row)
        self.order_changed.emit(order)
        return True

    def dropEvent(self, event):
        identity = self._drag_id(event)
        if identity is None or not event.possibleActions() & Qt.DropAction.MoveAction:
            event.ignore()
            self._clear_drop()
            return
        self.move_calendar(identity, self.slot_at(event.position()))
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._clear_drop()

    def _scroll_drag(self):
        if self._drag_global is None:
            return
        area = self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is None:
            return
        point = area.viewport().mapFromGlobal(self._drag_global)
        row = next(iter(self.rows()), None)
        scale = getattr(getattr(row, 'preferences', None), 'font_scale', 100) / 100
        margin, step = round(24 * scale), max(4, round(8 * scale))
        direction = -1 if point.y() < margin else 1 if point.y() >= area.viewport().height() - margin else 0
        if direction:
            scrollbar = area.verticalScrollBar()
            scrollbar.setValue(scrollbar.value() + direction * step)
            self._set_drop(QPointF(self.mapFromGlobal(self._drag_global)))
