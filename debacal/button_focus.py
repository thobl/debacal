"""Show button focus for keyboard navigation, without a mouse-click outline."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton


class _ButtonFocus(QObject):
    def eventFilter(self, button, event):
        if not isinstance(button, QAbstractButton):
            return False
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            visible = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                         Qt.FocusReason.ShortcutFocusReason)
        elif kind in (QEvent.Type.FocusOut, QEvent.Type.MouseButtonPress):
            visible = False
            if kind == QEvent.Type.MouseButtonPress:
                # Qt's native style also uses this window flag after keyboard
                # navigation; clear it when switching back to a mouse click.
                button.window().setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, False)
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
