"""Show button focus for keyboard navigation, without a mouse-click outline."""
from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QAbstractButton, QWidget


class _ButtonFocus(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mouse_active = False
        self._release = QTimer(self)
        self._release.setSingleShot(True)
        self._release.timeout.connect(self._finish_mouse)

    def _finish_mouse(self):
        self._mouse_active = False

    def eventFilter(self, button, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self._mouse_active = True
            self._release.stop()
            if isinstance(button, QWidget):
                # Application-wide filtering also sees custom widgets whose
                # Python attributes can hide native QWidget methods.
                QWidget.window(button).setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, False)
        elif kind == QEvent.Type.MouseButtonRelease:
            # A click can disable its own button and move focus during the
            # release handler. That automatic move remains a mouse action.
            self._release.start(0)
        elif kind == QEvent.Type.KeyPress:
            self._finish_mouse()
            self._release.stop()
        if not isinstance(button, QAbstractButton):
            return False
        if kind == QEvent.Type.FocusIn:
            visible = not self._mouse_active and event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        elif kind in (QEvent.Type.FocusOut, QEvent.Type.MouseButtonPress):
            visible = False
            if kind == QEvent.Type.MouseButtonPress:
                # Qt's native style also uses this window flag after keyboard
                # navigation; clear it when switching back to a mouse click.
                QWidget.window(button).setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, False)
                button.update()
        elif kind == QEvent.Type.KeyPress and event.key() not in (
                Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            visible = True
        else:
            return False
        if bool(button.property('keyboardFocus')) != visible:
            button.setProperty('keyboardFocus', visible)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()
            if button.parentWidget() is not None:
                # Single-choice groups paint focus on their shared surface.
                button.parentWidget().update()
        return False


def install_button_focus(app):
    """Install once for all application windows and their native dialogs."""
    if app is not None and not hasattr(app, '_debacal_button_focus'):
        app._debacal_button_focus = _ButtonFocus(app)
        app.installEventFilter(app._debacal_button_focus)
