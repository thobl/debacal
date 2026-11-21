"""Calendar creation targets, local list groups, and event visibility controls."""
from dataclasses import replace
from time import monotonic
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QCursor, QDrag, QFont
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy, QPushButton, QScrollArea, QToolTip

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
        title = ('Hide events from ' if calendar.visible else 'Show events from ') + calendar.name
        self.eye.setToolTip(title)
        self.eye.setAccessibleName(title)
        self.eye.setFixedSize(round(28 * scale / 100), round(28 * scale / 100))
        self._presentation_key = key
        return True


CALENDAR_ORDER_MIME = 'application/x-debacal-calendar-order'


class CalendarList(QWidget):
    """Two source lists sharing order and drag state, independent of visibility.

    The main list contains pinned sources plus the current creation calendar.
    Remaining sources live in a separate popup, never in the sidebar layout.
    """
    order_changed = Signal(list)
    grouped_layout_changed = Signal(list, list)
    height_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from .calendar_popup import CalendarPopup, MoreButton
        self.setObjectName('calendarListBody')
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)
        self._rows = []
        self._dragging = False
        self._drag_selected = False
        self._height = None
        self._presentation_key = None
        self.more_button = MoreButton(self)
        policy = self.more_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.more_button.setSizePolicy(policy)
        self.more_button.hide()
        self._popup_drag_timer = QTimer(self)
        self._popup_drag_timer.setInterval(40)
        self._popup_drag_timer.timeout.connect(self._check_popup_drag)
        self.popup = CalendarPopup(self)
        self.popup_list = self.popup.body
        self.popup_scroll = self.popup.scroll
        self._popup_session = False
        self._outside_popup_since = None
        self.main_drop_target = QLabel('Drop to keep in the main list', self)
        self.main_drop_target.setObjectName('calendarMainDropTarget')
        self.main_drop_target.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.main_drop_target.hide()
        self._pressed_row = self._press_global = None
        self._drop_slot = self._drop_group = None
        self._drag_global = self._drag_surface = None
        self._indicator = self._new_indicator(self)
        self._popup_indicator = self._new_indicator(self.popup_list)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._scroll_drag)
        self._expand_timer = QTimer(self)
        self._expand_timer.setSingleShot(True)
        self._expand_timer.setInterval(350)
        self._expand_timer.timeout.connect(self._expand_during_drag)

    def _new_indicator(self, parent):
        indicator = QWidget(parent)
        indicator.setObjectName('calendarDropIndicator')
        indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        indicator.setStyleSheet('QWidget#calendarDropIndicator { background: ' + scheme_for(None).accent + '; border: 0; }')
        indicator.hide()
        return indicator

    @property
    def more_expanded(self):
        return self.popup.isVisible()

    def rows(self):
        return list(self._rows)

    def ordered_ids(self):
        return [row.calendar.id for row in self._rows]

    def group_rows(self, in_main_list):
        return [row for row in self._rows if row.calendar.in_main_list == in_main_list]

    def display_rows(self, in_main_list):
        return [row for row in self._rows
                if bool(row.calendar.in_main_list or row.choice.isChecked()) == in_main_list]

    @staticmethod
    def _place(box, widgets):
        for index, widget in enumerate(widgets):
            if box.indexOf(widget) != index:
                box.insertWidget(index, widget)
            widget.show()

    def _preferences(self):
        return self._rows[0].preferences if self._rows else None

    def set_rows(self, rows):
        rows = list(rows)
        requested = set(rows)
        for previous in self._rows:
            if previous not in requested:
                self.box.removeWidget(previous)
                self.popup_list.box.removeWidget(previous)
                previous.hide()
        self._rows = [row for row in rows if row.calendar.in_main_list]
        self._rows += [row for row in rows if not row.calendar.in_main_list]
        main, more = self.display_rows(True), self.display_rows(False)
        empty_main = self._dragging and not main
        desired_main = main + ([self.main_drop_target] if empty_main else [])
        desired_more = more or [self.popup.empty]
        # Remove changed memberships before adding widgets to the other layout.
        for box, wanted in ((self.box, desired_main), (self.popup_list.box, desired_more)):
            for widget in self._rows + [self.main_drop_target, self.popup.empty]:
                if widget not in wanted and box.indexOf(widget) >= 0:
                    box.removeWidget(widget)
        self.main_drop_target.setVisible(empty_main)
        self.popup.empty.setVisible(not more)
        drop_hint = ('Drop to hide from the main list when not selected' if self._drag_selected else
                     'Drop to hide from the main list')
        self.popup.empty.setText(drop_hint if self._dragging else
                                'The selected calendar stays in the main list.')
        self._place(self.box, desired_main)
        self._place(self.popup_list.box, desired_more)
        has_more = bool(self.group_rows(False))
        self.more_button.setVisible(has_more or self._dragging)
        if not has_more and not self._dragging:
            self.set_expanded(False)
        preferences = self._preferences()
        scale = getattr(preferences, 'font_scale', 100) / 100
        scheme = scheme_for(preferences)
        key = preferences, self._dragging
        if key != self._presentation_key:
            self._presentation_key = key
            self.more_button.setIcon(icon('more', scheme.accent))
            self.more_button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
            self.more_button.setFixedSize(round(22 * scale), round(32 * scale))
            background = scheme.selection if self._dragging else 'transparent'
            edge = scheme.accent if self._dragging else 'transparent'
            self.more_button.setProperty('calendarDropTarget', self._dragging)
            self.more_button.setStyleSheet(
                f'QPushButton#calendarMore {{ background: {background}; border: 1px solid {edge}; '
                f'padding: 0; border-radius: {corner_radius(preferences, 4) * scale}px; }} '
                f'QPushButton#calendarMore:hover, QPushButton#calendarMore:checked {{ background: {scheme.hover}; }} '
                f'QPushButton#calendarMore[keyboardFocus="true"]:focus {{ border-color: {scheme.focus}; }}')
            self.main_drop_target.setMinimumHeight(round(32 * scale))
            self.main_drop_target.setStyleSheet(
                f'QLabel#calendarMainDropTarget {{ color: {scheme.muted}; border: 1px dashed {scheme.control_edge}; padding: 4px; }}')
            for indicator in (self._indicator, self._popup_indicator):
                indicator.setStyleSheet('QWidget#calendarDropIndicator { background: ' + scheme.accent + '; border: 0; }')
        count = len(more)
        noun = 'calendar' if count == 1 else 'calendars'
        placement_hint = (drop_hint if self._dragging else
                          'Drag a calendar here to hide it from the main list.\n'
                          'The selected calendar stays in the main list until you choose another.')
        self.more_button.setToolTip('More calendars' + (f' ({count})' if count else '') +
                                    '\n' + placement_hint)
        self.more_button.setAccessibleDescription(f'{count} {noun} in this menu. ' + placement_hint.replace('\n', ' '))
        self.box.activate()
        self.popup_list.box.activate()
        self.updateGeometry()
        height = self.content_height()
        if height != self._height:
            self._height = height
            self.height_changed.emit()

    def _prepare_popup(self):
        self.popup.prepare(self._preferences(), self.more_button)

    def set_expanded(self, expanded):
        if expanded:
            if self.more_button.isHidden():
                return
            if not self.more_expanded:
                # A suspended drag reuses the original rectangle. Refreshes,
                # row migration and scrolling never reposition an open panel.
                if not (self._dragging and self._popup_session):
                    self._prepare_popup()
                    self._popup_session = True
                self.popup.show()
                self.popup.raise_()
            self.more_button.setChecked(True)
            if self._dragging:
                self._outside_popup_since = None
                self._popup_drag_timer.start()
            if not self._dragging:
                rows = self.display_rows(False)
                if rows:
                    (rows[0].choice if rows[0].choice.isEnabled() else rows[0].eye).setFocus()
        else:
            self.popup.hide()
            self._popup_session = False
            self.more_button.setChecked(False)

    def _popup_closed(self):
        self._popup_drag_timer.stop()
        self.more_button.setChecked(False)

    def _hide_popup_for_drag(self):
        """Expose the underlying main list without moving or deleting sources."""
        if self.popup.isVisible():
            self.popup.hide()

    def _check_popup_drag(self):
        if not self._dragging or not self.more_expanded:
            self._popup_drag_timer.stop()
            return
        region = self.popup.rect().translated(self.popup.mapToGlobal(self.popup.rect().topLeft()))
        region = region.united(self.more_button.rect().translated(
            self.more_button.mapToGlobal(self.more_button.rect().topLeft()))).adjusted(-6, -6, 6, 6)
        if region.contains(QCursor.pos()):
            self._outside_popup_since = None
        elif self._outside_popup_since is None:
            self._outside_popup_since = monotonic()
        elif monotonic() - self._outside_popup_since >= .08:
            self._hide_popup_for_drag()

    def hideEvent(self, event):
        self.set_expanded(False)
        super().hideEvent(event)

    def visible_row_count(self):
        return len(self.display_rows(True))

    def content_height(self):
        widgets = self.display_rows(True)
        if not self.main_drop_target.isHidden():
            widgets += [self.main_drop_target]
        return max(1, sum(max(widget.minimumHeight(), widget.sizeHint().height()) for widget in widgets)
                   + max(0, len(widgets) - 1) * self.box.spacing())

    def _begin_drag(self, source=None):
        selected = bool(source and source.choice.isChecked())
        if not self._dragging or selected != self._drag_selected:
            self._dragging = True
            self._drag_selected = selected
            self.set_rows(self.rows())
            parent = self.more_button.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().activate()

    def attach_row(self, row):
        if row not in self._rows:
            self._rows.append(row)
        row._calendar_list = self
        row.creation_requested.connect(lambda _identity: self.set_expanded(False))
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
        if row not in self._rows:
            return
        pixmap, hotspot = row.grab(), row.mapFromGlobal(anchor)
        popup_origin = self.popup.isAncestorOf(row)
        self._begin_drag(row)
        drag = QDrag(row)
        mime = QMimeData()
        mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode('utf-8'))
        drag.setMimeData(mime)
        drag.setPixmap(pixmap)
        drag.setHotSpot(hotspot)
        if popup_origin:
            self._hide_popup_for_drag()
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
        if source not in self._rows or not event.mimeData().hasFormat(CALENDAR_ORDER_MIME):
            return None
        payload = bytes(event.mimeData().data(CALENDAR_ORDER_MIME))
        return source.calendar.id if payload == source.calendar.id.encode('utf-8') else None

    def _valid_drag(self, event):
        if self._drag_id(event) is None or not event.possibleActions() & Qt.DropAction.MoveAction:
            event.ignore()
            return False
        return True

    def _target_at(self, point, surface=None):
        main = surface is None or surface is self
        rows = self.display_rows(main)
        slot = next((index for index, row in enumerate(rows) if point.y() < row.geometry().center().y()), len(rows))
        return main, slot

    def _stored_slot(self, group, display_slot):
        displayed, stored = self.display_rows(group), self.group_rows(group)
        if display_slot < len(displayed) and displayed[display_slot] in stored:
            return stored.index(displayed[display_slot])
        return len(stored)

    def slot_at(self, point):
        group, slot = self._target_at(point)
        return self._stored_slot(group, slot)

    def _set_drop(self, point, surface=None):
        surface = surface or self
        self._drop_group, slot = self._target_at(point, surface)
        self._drop_slot = self._stored_slot(self._drop_group, slot) + (0 if self._drop_group else len(self.group_rows(True)))
        rows = self.display_rows(self._drop_group)
        if not rows:
            y = surface.height() - 2
        elif slot == 0:
            y = rows[0].geometry().top() + 1
        elif slot == len(rows):
            y = rows[-1].geometry().bottom() - 1
        else:
            y = (rows[slot - 1].geometry().bottom() + rows[slot].geometry().top()) / 2
        indicator = self._indicator if self._drop_group else self._popup_indicator
        self._indicator.hide()
        self._popup_indicator.hide()
        indicator.setGeometry(1, max(0, round(y) - 1), max(0, surface.width() - 2), 2)
        indicator.raise_()
        indicator.show()

    def _clear_drop(self):
        self._drop_slot = self._drop_group = self._drag_global = self._drag_surface = None
        self._scroll_timer.stop()
        self._expand_timer.stop()
        self._popup_drag_timer.stop()
        self._indicator.hide()
        self._popup_indicator.hide()
        QToolTip.hideText()
        if self._dragging:
            self._dragging = False
            self._drag_selected = False
            self.set_rows(self.rows())
        self.set_expanded(False)

    def _accept_drag(self, event, surface=None):
        if not self._valid_drag(event):
            return False
        surface = surface or self
        self._begin_drag(event.source())
        if surface is self:
            self._hide_popup_for_drag()
        self._expand_timer.stop()
        self._set_drop(event.position(), surface)
        self._drag_surface = surface
        self._drag_global = surface.mapToGlobal(event.position().toPoint())
        self._scroll_timer.start()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        return True

    def _accept_more_drag(self, event):
        if not self._valid_drag(event):
            return False
        self._begin_drag(event.source())
        self._drop_group, self._drop_slot = False, len(self.group_rows(True))
        self._indicator.hide()
        self._popup_indicator.hide()
        self._scroll_timer.stop()
        self._drag_surface = self.more_button
        self._drag_global = self.more_button.mapToGlobal(event.position().toPoint())
        if not self.more_expanded and not self._expand_timer.isActive():
            self._expand_timer.start()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        return True

    def _leave_more_drag(self, event):
        self._expand_timer.stop()
        event.accept()

    def _leave_surface(self, surface, event):
        self._scroll_timer.stop()
        self._indicator.hide()
        self._popup_indicator.hide()
        event.accept()

    def dragEnterEvent(self, event):
        self._accept_drag(event)

    def dragMoveEvent(self, event):
        self._accept_drag(event)

    def dragLeaveEvent(self, event):
        self._leave_surface(self, event)

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

    def _drop_event(self, event, surface=None):
        identity = self._drag_id(event)
        if not self._valid_drag(event):
            self._clear_drop()
            return
        group, slot = self._target_at(event.position(), surface)
        self.move_calendar(identity, self._stored_slot(group, slot), in_main_list=group)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._clear_drop()

    def _drop_more(self, event):
        if not self._valid_drag(event):
            self._clear_drop()
            return
        self.move_calendar(self._drag_id(event), len(self.group_rows(False)), in_main_list=False)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._clear_drop()

    def dropEvent(self, event):
        self._drop_event(event)

    def _expand_during_drag(self):
        if self._dragging and self._drag_surface is self.more_button:
            QToolTip.hideText()
            self.set_expanded(True)

    def _scroll_drag(self):
        if self._drag_global is None:
            return
        surface = self._drag_surface or self
        if surface is self.more_button:
            return
        area = self.popup_scroll if surface is self.popup_list else self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is None:
            return
        point = area.viewport().mapFromGlobal(self._drag_global)
        scale = getattr(self._preferences(), 'font_scale', 100) / 100
        margin, step = round(24 * scale), max(4, round(8 * scale))
        direction = -1 if point.y() < margin else 1 if point.y() >= area.viewport().height() - margin else 0
        if direction:
            scrollbar = area.verticalScrollBar()
            scrollbar.setValue(scrollbar.value() + direction * step)
            self._set_drop(QPointF(surface.mapFromGlobal(self._drag_global)), surface)
