"""Calendar creation targets, local list groups, and event visibility controls."""
from dataclasses import replace
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QDrag, QFont
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
        self._presentation_key = None
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
        """Update source identity immediately and repaint changed presentation.

        Sync can replace a Calendar with new transport metadata while its row
        looks identical. Keep that latest value for action handlers without
        repeatedly polishing native controls during date navigation.
        """
        self.calendar = calendar
        scheme = scheme_for(self.preferences if self.preferences is not None else self)
        writable = calendar.kind != 'ics' and not calendar.read_only
        checked = bool(current and writable)
        font = self.choice.font()
        base_font = self.choice.font()
        base_font.setBold(False)
        base_font.setUnderline(False)
        radius = corner_radius(self.preferences, 2)
        key = (calendar.name, calendar.color, calendar.visible, writable,
               bool(current), scale, scheme, radius, base_font.key())
        if (self._presentation_key == key and self.choice.isChecked() == checked
                and self.eye.isChecked() == calendar.visible
                and font.weight() == (QFont.Weight.Bold if checked else QFont.Weight.Normal)
                and font.underline() == checked):
            return False
        factor = scale / 100
        source = QColor(calendar.color)
        source = source.name() if source.isValid() else scheme.accent
        edge = source_outline(QColor(source).darker(145).name(), scheme.sidebar)
        self.swatch.setFixedSize(round(12 * factor), round(12 * factor))
        self.swatch.setStyleSheet(f'background: {source}; border: {max(1, round(factor))}px solid {edge}; '
                                 f'border-radius: {round(radius * factor)}px;')
        self.swatch.setAccessibleName(calendar.name + ' calendar color')
        self.choice.setText(calendar.name.replace('&', '&&'))
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
        self._presentation_key = key
        return True


CALENDAR_ORDER_MIME = 'application/x-debacal-calendar-order'


class CalendarList(QWidget):
    """A source-only drag target; calendar/event data never enters the payload."""
    order_changed = Signal(list)
    grouped_layout_changed = Signal(list, list)
    height_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('calendarListBody')
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self.more_expanded = False
        self._dragging = False
        self._height = None
        self._group_key = None
        self.more_button = QPushButton(self)
        self.more_button.setObjectName('calendarMore')
        self.more_button.setCheckable(True)
        self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_button.clicked.connect(self.set_expanded)
        self.more_button.hide()
        self.main_drop_target = QLabel('Main list', self)
        self.main_drop_target.setObjectName('calendarMainDropTarget')
        self.main_drop_target.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.main_drop_target.hide()
        self._pressed_row = None
        self._press_global = None
        self._drop_slot = None
        self._drop_group = None
        self._drag_global = None
        self._indicator = QWidget(self)
        self._indicator.setObjectName('calendarDropIndicator')
        self._indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._indicator_color = None
        self._indicator.hide()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._scroll_drag)
        self._expand_timer = QTimer(self)
        self._expand_timer.setSingleShot(True)
        self._expand_timer.setInterval(450)
        self._expand_timer.timeout.connect(self._expand_during_drag)

    def rows(self):
        # Keep layout-owned non-QObject items out of Python's wrapper map:
        # native moves/removal can destroy those items without invalidating a
        # borrowed wrapper. The CalendarRow QObjects have normal parent ownership.
        positioned = [(self.box.indexOf(child), child) for child in self.children()
                      if isinstance(child, CalendarRow)]
        return [row for index, row in sorted(positioned, key=lambda item: item[0]) if index >= 0]

    def ordered_ids(self):
        return [row.calendar.id for row in self.rows()]

    def group_rows(self, in_main_list):
        return [row for row in self.rows() if row.calendar.in_main_list == in_main_list]

    def set_rows(self, rows):
        """Place existing row objects without touching unchanged native layout items."""
        rows = list(rows)
        requested = set(rows)
        for previous in self.rows():
            if previous not in requested:
                self.box.removeWidget(previous)
                previous.hide()
        main = [row for row in rows if row.calendar.in_main_list]
        more = [row for row in rows if not row.calendar.in_main_list]
        show_more = bool(more) or self._dragging
        show_main_target = self._dragging and not main
        desired = main + ([self.main_drop_target] if show_main_target else [])
        desired += ([self.more_button] if show_more else []) + more
        for control, visible in ((self.more_button, show_more), (self.main_drop_target, show_main_target)):
            if not visible and self.box.indexOf(control) >= 0:
                self.box.removeWidget(control)
            control.setVisible(visible)
        for index, widget in enumerate(desired):
            if self.box.indexOf(widget) != index:
                self.box.insertWidget(index, widget)
        for row in rows:
            row.setVisible(row.calendar.in_main_list or self.more_expanded)
        first = next(iter(rows), None)
        preferences = first.preferences if first is not None else None
        scale = getattr(preferences, 'font_scale', 100) / 100
        scheme = scheme_for(preferences)
        compact = not self.more_expanded and not self._dragging
        key = len(more), self.more_expanded, compact, bool(main), scale, scheme
        if key != self._group_key:
            self._group_key = key
            noun = 'calendar' if len(more) == 1 else 'calendars'
            description = f'{len(more)} additional {noun}'
            if compact:
                label = f'{len(more)} more' if main else f'{len(more)} {noun}'
            else:
                label = f'More calendars ({len(more)})'
            self.more_button.setText(('▾ ' if self.more_expanded else '▸ ') + label)
            self.more_button.setChecked(self.more_expanded)
            self.more_button.setAccessibleName('More calendars')
            self.more_button.setAccessibleDescription(description)
            self.more_button.setToolTip('Collapse More calendars' if self.more_expanded else 'Show ' + description)
            self.more_button.setMinimumHeight(round((18 if compact else 28) * scale))
            hover = f'color: {scheme.ink};' if compact else f'background: {scheme.hover};'
            self.more_button.setStyleSheet(
                f'QPushButton#calendarMore {{ text-align: left; background: transparent; border: 0; '
                f'padding: {0 if compact else 4}px 0; color: {scheme.muted}; '
                f'font-size: {round((11 if compact else 13) * scale)}px; }} '
                f'QPushButton#calendarMore:hover {{ {hover} }} '
                f'QPushButton#calendarMore[keyboardFocus="true"]:focus {{ border: 1px solid {scheme.focus}; }}')
            self.main_drop_target.setMinimumHeight(round(28 * scale))
            self.main_drop_target.setStyleSheet(
                f'QLabel#calendarMainDropTarget {{ color: {scheme.muted}; border: 1px dashed {scheme.control_edge}; '
                f'padding: 4px; }}')
        self.box.activate()
        self.updateGeometry()
        height = self.content_height()
        if height != self._height:
            self._height = height
            self.height_changed.emit()

    def set_expanded(self, expanded):
        expanded = bool(expanded)
        if expanded != self.more_expanded:
            self.more_expanded = expanded
            self.set_rows(self.rows())

    def visible_row_count(self):
        return sum(not row.isHidden() for row in self.rows())

    def content_height(self):
        widgets = [row for row in self.rows() if not row.isHidden()]
        widgets += [control for control in (self.main_drop_target, self.more_button) if not control.isHidden()]
        return max(1, sum(max(widget.minimumHeight(), widget.sizeHint().height()) for widget in widgets)
                   + max(0, len(widgets) - 1) * self.box.spacing())

    def _begin_drag(self):
        if not self._dragging:
            self._dragging = True
            self.set_rows(self.rows())

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
        # Empty-group targets change the layout. Keep the image and pointer
        # offset from the row the user actually pressed, before revealing them.
        pixmap, hotspot = row.grab(), row.mapFromGlobal(anchor)
        self._begin_drag()
        drag = QDrag(row)
        mime = QMimeData()
        mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode('utf-8'))
        drag.setMimeData(mime)
        drag.setPixmap(pixmap)
        drag.setHotSpot(hotspot)
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
        group, slot = self._target_at(point)
        return slot + (0 if group else len(self.group_rows(True)))

    def _target_at(self, point):
        more = not self.more_button.isHidden() and point.y() >= self.more_button.geometry().top()
        rows = self.group_rows(not more)
        if more and (not self.more_expanded or point.y() <= self.more_button.geometry().bottom()):
            return False, 0
        slot = next((index for index, row in enumerate(rows) if point.y() < row.geometry().center().y()), len(rows))
        return not more, slot

    def _set_drop(self, point):
        self._drop_group, slot = self._target_at(point)
        self._drop_slot = slot + (0 if self._drop_group else len(self.group_rows(True)))
        rows = self.group_rows(self._drop_group)
        if not self._drop_group and not self.more_expanded:
            y = self.more_button.geometry().bottom() - 1
        elif not rows:
            y = (self.main_drop_target.geometry().center().y() if self._drop_group
                 else self.more_button.geometry().bottom() - 1)
        elif slot == 0:
            y = rows[0].geometry().top() + 1
        elif slot == len(rows):
            y = rows[-1].geometry().bottom() - 1
        else:
            y = (rows[slot - 1].geometry().bottom() + rows[slot].geometry().top()) / 2
        first = next(iter(self.rows()), None)
        color = scheme_for(first.preferences if first is not None else None).accent
        if color != self._indicator_color:
            self._indicator.setStyleSheet('QWidget#calendarDropIndicator { background: ' + color + '; border: 0; }')
            self._indicator_color = color
        self._indicator.setGeometry(1, max(0, round(y) - 1), max(0, self.width() - 2), 2)
        self._indicator.raise_()
        self._indicator.show()

    def _clear_drop(self):
        self._drop_slot = self._drag_global = None
        self._drop_group = None
        self._scroll_timer.stop()
        self._expand_timer.stop()
        self._indicator.hide()
        if self._dragging:
            self._dragging = False
            self.set_rows(self.rows())

    def _accept_drag(self, event):
        if self._drag_id(event) is None or not event.possibleActions() & Qt.DropAction.MoveAction:
            event.ignore()
            return False
        self._begin_drag()
        self._set_drop(event.position())
        self._drag_global = self.mapToGlobal(event.position().toPoint())
        self._scroll_timer.start()
        if self._drop_group is False and not self.more_expanded:
            if not self._expand_timer.isActive():
                self._expand_timer.start()
        else:
            self._expand_timer.stop()
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

    def move_calendar(self, calendar_id, slot, *, in_main_list=None):
        rows = self.rows()
        source = next((row for row in rows if row.calendar.id == calendar_id), None)
        if source is None:
            return False
        previous = [row.calendar.id for row in rows]
        previous_main = [row.calendar.id for row in rows if row.calendar.in_main_list]
        group = source.calendar.in_main_list if in_main_list is None else bool(in_main_list)
        grouped = self.group_rows(group)
        if in_main_list is None and not group:
            slot -= len(previous_main)
        old = grouped.index(source) if source in grouped else None
        slot = max(0, min(len(grouped), slot))
        if old is not None:
            grouped.pop(old)
            slot -= int(old < slot)
        grouped.insert(slot, source)
        other = [row for row in rows if row is not source and row.calendar.in_main_list != group]
        ordered = grouped + other if group else other + grouped
        order = [row.calendar.id for row in ordered]
        main = [row.calendar.id for row in (grouped if group else other)]
        if order == previous and main == previous_main:
            return False
        source.set_calendar(replace(source.calendar, in_main_list=group), current=source.choice.isChecked(),
                            scale=getattr(source.preferences, 'font_scale', 100))
        self.set_rows(ordered)
        if order != previous:
            self.order_changed.emit(order)
        self.grouped_layout_changed.emit(order, main)
        return True

    def dropEvent(self, event):
        identity = self._drag_id(event)
        if identity is None or not event.possibleActions() & Qt.DropAction.MoveAction:
            event.ignore()
            self._clear_drop()
            return
        group, slot = self._target_at(event.position())
        self.move_calendar(identity, slot, in_main_list=group)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._clear_drop()

    def _expand_during_drag(self):
        if self._dragging and self._drop_group is False and not self.more_expanded:
            self.set_expanded(True)
            if self._drag_global is not None:
                self._set_drop(QPointF(self.mapFromGlobal(self._drag_global)))

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
