"""Calendar overflow popover with native cross-window dragging support."""
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .theme import corner_radius, scheme_for


class MoreButton(QPushButton):
    """The calendar heading's overflow button is also a drag destination."""

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.setObjectName('calendarMore')
        self.setProperty('iconOnly', True)
        self.setProperty('focusFrame', False)
        self.setProperty('iconName', 'more')
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName('More calendars')
        self.setToolTip('More calendars')
        self.clicked.connect(controller.set_expanded)

    def dragEnterEvent(self, event):
        self.controller._accept_more_drag(event)

    def dragMoveEvent(self, event):
        self.controller._accept_more_drag(event)

    def dragLeaveEvent(self, event):
        self.controller._leave_more_drag(event)

    def dropEvent(self, event):
        self.controller._drop_more(event)


class CalendarDropSurface(QWidget):
    """One list surface, sharing source identity and drag state with its owner."""

    def __init__(self, controller, *, in_main_list):
        super().__init__()
        self.controller = controller
        self.in_main_list = in_main_list
        self.setObjectName('calendarPopupBody')
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(4)

    def dragEnterEvent(self, event):
        self.controller._accept_drag(event, self)

    def dragMoveEvent(self, event):
        self.controller._accept_drag(event, self)

    def dragLeaveEvent(self, event):
        self.controller._leave_surface(self, event)

    def dropEvent(self, event):
        self.controller._drop_event(event, self)


class CalendarPopup(QFrame):
    """An anchored nonmodal panel; it never grabs a running native drag.

    Qt.Popup's implicit mouse grab can interrupt QDrag when the pointer crosses
    between popup and owner. A tool panel with explicit dismissal keeps both
    lists available throughout the same drag.
    """

    def __init__(self, controller):
        super().__init__(controller, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.controller = controller
        self.setObjectName('calendarPopup')
        self.setAccessibleName('More calendars')
        self._style_key = None
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(10, 10, 10, 10)
        self.outer.setSpacing(8)
        self.heading = QLabel('More calendars', self)
        self.heading.setObjectName('calendarPopupHeading')
        self.outer.addWidget(self.heading)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName('calendarPopupScroll')
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumSize(0, 0)
        self.body = CalendarDropSurface(controller, in_main_list=False)
        self.scroll.setWidget(self.body)
        self.outer.addWidget(self.scroll)
        self.empty = QLabel('No additional calendars', self.body)
        self.empty.setObjectName('calendarPopupEmpty')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.empty.hide()

    def prepare(self, preferences, anchor_button, width_hint):
        """Lay out the panel without changing either its or its owner's state."""
        scale = getattr(preferences, 'font_scale', 100) / 100
        scheme = scheme_for(preferences)
        key = preferences, scheme
        if key != self._style_key:
            self._style_key = key
            self.setStyleSheet(f'''
                QFrame#calendarPopup {{ background: {scheme.sidebar}; color: {scheme.ink};
                    border: 1px solid {scheme.control_edge}; border-radius: {corner_radius(preferences, 4) * scale}px; }}
                QWidget#calendarPopupBody, QScrollArea#calendarPopupScroll {{ background: {scheme.sidebar}; border: 0; }}
                QLabel#calendarPopupHeading {{ color: {scheme.ink}; font-weight: bold; border: 0; background: transparent; }}
                QLabel#calendarPopupEmpty {{ color: {scheme.muted}; border: 0; background: transparent; }}
            ''')
            margin = round(10 * scale)
            self.outer.setContentsMargins(margin, margin, margin, margin)
            self.outer.setSpacing(round(8 * scale))
            self.body.box.setSpacing(round(4 * scale))
            self.empty.setMinimumHeight(round(32 * scale))
        if self.font() != self.controller.font():
            self.setFont(self.controller.font())
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, bool(self.controller._dragging))
        self.body.box.activate()
        self.outer.activate()
        anchor = anchor_button.mapToGlobal(QPoint(0, anchor_button.height()))
        screen = QApplication.screenAt(anchor) or anchor_button.screen()
        available = screen.availableGeometry()
        edge = max(2, round(8 * scale))
        width = min(max(round(240 * scale), width_hint), max(1, available.width() - edge * 2))
        margins = self.outer.contentsMargins()
        chrome = margins.top() + margins.bottom() + self.heading.sizeHint().height() + self.outer.spacing()
        content = max(self.body.minimumSizeHint().height(), self.body.sizeHint().height(), round(32 * scale))
        height = min(chrome + content, round(420 * scale), max(1, available.height() - edge * 2))
        self.resize(width, height)
        # Keep the main rows exposed so a drag can cross between both lists.
        # Anchoring over them would turn a visually valid main-list drop into
        # another drop inside this panel.
        origin = self.controller.mapToGlobal(QPoint())
        right = origin.x() + self.controller.width() + edge
        left = origin.x() - width - edge
        y = anchor.y()
        if right + width <= available.right() + 1 - edge:
            x = right
        elif left >= available.left() + edge:
            x = left
        else:
            x = max(available.left() + edge, min(origin.x(), available.right() - width + 1 - edge))
            area = self.controller.parentWidget()
            while area is not None and not isinstance(area, QScrollArea):
                area = area.parentWidget()
            below = (area.viewport().mapToGlobal(QPoint(0, area.viewport().height())).y()
                     if area is not None else origin.y() + self.controller.height())
            y = max(y, below + edge)
        self.move(x, max(available.top() + edge, min(y, available.bottom() - height + 1 - edge)))

    def showEvent(self, event):
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        self.controller._popup_closed()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if not self.isVisible() or self.controller._dragging:
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            # Menus and modal dialogs above this nonmodal panel own Escape.
            # Consuming it here would leave the menu/dialog open instead.
            app = QApplication.instance()
            for active in (app.activePopupWidget(), app.activeModalWidget()):
                if active is not None and active is not self:
                    return False
            self.controller.set_expanded(False)
            self.controller.more_button.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return True
        if kind == QEvent.Type.ApplicationDeactivate:
            self.controller.set_expanded(False)
        elif kind in (QEvent.Type.Move, QEvent.Type.Resize) and watched is self.controller.window():
            self.controller.set_expanded(False)
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) and isinstance(watched, QWidget):
            button = self.controller.more_button
            inside = watched is self or self.isAncestorOf(watched) or watched is button or button.isAncestorOf(watched)
            if not inside:
                self.controller.set_expanded(False)
        return False
