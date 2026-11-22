"""An anchored calendar overflow panel that stays inside the main window."""
from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .theme import corner_radius, scheme_for


def _owned_by(widget, owner):
    """Follow Qt ownership through menus/dialogs, where isAncestorOf stops."""
    while widget is not None:
        if widget is owner:
            return True
        widget = widget.parentWidget()
    return False


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
    """A child overlay: no window-manager positioning, grabs, or layout growth."""

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.setObjectName('calendarPopup')
        self.setAccessibleName('More calendars')
        self.setAutoFillBackground(False)
        self._has_been_shown = False
        self._style_key = None
        self._scheme = scheme_for(None)
        self._scale, self._caret_x, self._radius = 1, 20, 4
        self._above = False
        self.outer = QVBoxLayout(self)
        # Row membership can change while open. Only prepare(), called on a
        # fresh opening, determines this overlay's geometry.
        self.outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.outer.setContentsMargins(8, 15, 11, 11)
        self.outer.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName('calendarPopupScroll')
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumSize(0, 0)
        self.body = CalendarDropSurface(controller, in_main_list=False)
        self.scroll.setWidget(self.body)
        self.outer.addWidget(self.scroll)
        self.empty = QLabel('No other calendars', self.body)
        self.empty.setObjectName('calendarPopupEmpty')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.empty.hide()
        # prepare() reparents to the containing main window so the overlay is
        # not clipped by the sidebar's scroll viewport. Keep lifetime tied to
        # the controller even after that reparenting.
        controller.destroyed.connect(self.deleteLater)
        self.hide()

    def prepare(self, preferences, anchor_button):
        """Set a fresh opening's theme and geometry; never creates a window."""
        owner = QWidget.window(anchor_button)
        if self.parentWidget() is not owner:
            self.setParent(owner, Qt.WindowType.Widget)
        scale = getattr(preferences, 'font_scale', 100) / 100
        scheme = scheme_for(preferences)
        self._scale, self._scheme = scale, scheme
        key = preferences, scheme
        if key != self._style_key:
            self._style_key = key
            self._radius = corner_radius(preferences, 4) * scale
            self.setStyleSheet(f'''
                QFrame#calendarPopup {{ background: transparent; border: 0; color: {scheme.ink}; }}
                QWidget#calendarPopupBody, QScrollArea#calendarPopupScroll {{ background: {scheme.panel}; border: 0; }}
                QLabel#calendarPopupEmpty {{ color: {scheme.muted}; border: 0; background: transparent; }}
            ''')
            inset, caret, shadow = round(8 * scale), round(7 * scale), round(3 * scale)
            self.outer.setContentsMargins(inset, caret + inset, inset + shadow, inset + shadow)
            self.body.box.setSpacing(round(4 * scale))
            self.empty.setMinimumHeight(round(32 * scale))
        if self.font() != self.controller.font():
            self.setFont(self.controller.font())
        self.body.box.activate()
        self.outer.activate()
        anchor = anchor_button.mapTo(owner, QPoint(0, anchor_button.height()))
        screen = QApplication.screenAt(anchor_button.mapToGlobal(QPoint())) or anchor_button.screen()
        screen_rect = screen.availableGeometry()
        bounds = owner.rect().intersected(QRect(owner.mapFromGlobal(screen_rect.topLeft()), screen_rect.size()))
        if bounds.isEmpty():
            bounds = owner.rect()
        edge = max(2, round(6 * scale))
        bounds = bounds.adjusted(edge, edge, -edge, -edge)
        # Cover the complete rows beneath the panel, including their eye
        # controls; a narrower overlay leaves duplicate controls peeking out.
        width = min(max(1, self.controller.width()), max(1, bounds.width()))
        margins = self.outer.contentsMargins()
        chrome = margins.top() + margins.bottom()
        content = max(self.body.minimumSizeHint().height(), self.body.sizeHint().height(), round(32 * scale))
        desired_height = min(chrome + content, round(320 * scale))
        x = self.controller.mapTo(owner, QPoint()).x() + round(4 * scale)
        x = max(bounds.left(), min(x, bounds.right() - width + 1))
        y = anchor.y() + max(1, round(scale))
        below = max(1, bounds.bottom() - y + 1)
        minimum = chrome + round(32 * scale)
        anchor_top = anchor_button.mapTo(owner, QPoint()).y()
        self._above = below < minimum and anchor_top - bounds.top() > below
        if self._above:
            # A sidebar scrolled near the bottom may not leave one row below.
            # Keep the panel on screen rather than clipping its controls.
            height = min(desired_height, max(1, anchor_top - bounds.top() - 1))
            y = max(bounds.top(), anchor_top - height - 1)
        else:
            height = min(desired_height, below)
        inset, caret, shadow = round(8 * scale), round(7 * scale), round(3 * scale)
        self.outer.setContentsMargins(inset, inset if self._above else caret + inset,
                                      inset + shadow, inset + shadow + (caret if self._above else 0))
        self.setGeometry(x, max(bounds.top(), y), width, height)
        anchor_x = anchor_button.mapTo(owner, anchor_button.rect().center()).x()
        caret_half = round(7 * scale)
        self._caret_x = max(self._radius + caret_half + 1,
                            min(anchor_x - x, width - round(3 * scale) - self._radius - caret_half - 1))
        self.update()

    def paintEvent(self, _event):
        scale, colors = self._scale, self._scheme
        caret, shadow = 7 * scale, 3 * scale
        panel = QRectF(.5, .5 if self._above else caret + .5, max(0, self.width() - shadow - 1),
                       max(0, self.height() - caret - shadow - 1))
        shape = QPainterPath()
        shape.addRoundedRect(panel, self._radius, self._radius)
        triangle = QPainterPath()
        base = panel.bottom() - 1 if self._above else panel.top() + 1
        point = self.height() - shadow - .5 if self._above else .5
        triangle.moveTo(self._caret_x - caret, base)
        triangle.lineTo(self._caret_x, point)
        triangle.lineTo(self._caret_x + caret, base)
        triangle.closeSubpath()
        shape = shape.united(triangle)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow_color = QColor(colors.ink)
        shadow_color.setAlpha(28)
        painter.fillPath(shape.translated(2 * scale, 2 * scale), shadow_color)
        painter.setPen(QPen(QColor(colors.control_edge), max(1, scale)))
        painter.setBrush(QColor(colors.panel))
        painter.drawPath(shape)

    def showEvent(self, event):
        self._has_been_shown = True
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        if self._has_been_shown:
            self.controller._popup_closed()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if not self.isVisible() or self.controller._dragging:
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            # Menus and modal dialogs own Escape, including those parented to
            # this panel; they must close before the underlying overlay.
            app = QApplication.instance()
            for active in (app.activePopupWidget(), app.activeModalWidget()):
                if active is not None and active is not self:
                    return False
            self.controller.set_expanded(False)
            self.controller.more_button.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return True
        if kind == QEvent.Type.ApplicationDeactivate:
            self.controller.set_expanded(False)
        elif kind in (QEvent.Type.Move, QEvent.Type.Resize) and watched is self.parentWidget():
            self.controller.set_expanded(False)
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) and isinstance(watched, QWidget):
            button = self.controller.more_button
            modal = QApplication.instance().activeModalWidget()
            # A calendar action may open a main-window-owned modal dialog.
            # Its own input and dismissal must not dismiss the source panel.
            if modal is not None and _owned_by(watched, modal):
                return False
            # Unhandled right presses propagate from a name button to the
            # main window. Test their position rather than treating each
            # receiver as a new outside click. Owned menu windows also grab
            # clicks outside themselves, so only their actual bounds count.
            if kind == QEvent.Type.Wheel:
                inside = _owned_by(watched, self) or _owned_by(watched, button)
            else:
                point = event.globalPosition().toPoint()
                surfaces = [self, button]
                window = QWidget.window(watched)
                if _owned_by(window, self):
                    surfaces.append(window)
                inside = any(surface.isVisible() and surface.rect().contains(surface.mapFromGlobal(point))
                             for surface in surfaces)
            if not inside:
                self.controller.set_expanded(False)
        return False
