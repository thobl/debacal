"""Shared keyboard behavior and small controls for native dialogs."""
from PySide6.QtCore import Qt, QEvent, QObject
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QWidget, QTextEdit, QPlainTextEdit, QPushButton, QCheckBox,
                              QLineEdit, QComboBox, QAbstractSpinBox, QKeySequenceEdit, QDialogButtonBox)
from shiboken6 import isValid

from .shortcuts import DEFAULT_DIALOG_BINDINGS, shortcut_keys, shortcut_label, shortcut_matches


def dialog_buttons(primary=QDialogButtonBox.StandardButton.Save, *, text=None, apply=False, close=False, parent=None):
    """Shared closing actions: neutral Cancel, then the highlighted accept.

    Apply is reserved for keeping a preferences dialog open. Temporary pickers
    use OK, persistent forms use Save, and explicit actions can name the accept
    button Import/Export without changing its role, position, or appearance.
    """
    if close:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent)
    else:
        standard = primary | QDialogButtonBox.StandardButton.Cancel
        if apply:
            standard |= QDialogButtonBox.StandardButton.Apply
        buttons = QDialogButtonBox(standard, parent)
        accept = buttons.button(primary)
        if text is not None:
            accept.setText(text)
        accept.setObjectName('primary')
    # Use one explicit desktop layout across the application instead of letting
    # custom buttons and platform defaults change the order between dialogs.
    buttons.setStyleSheet(f'QDialogButtonBox {{ button-layout: {QDialogButtonBox.ButtonLayout.GnomeLayout.value}; }}')
    buttons.setCenterButtons(False)
    buttons.setContentsMargins(0, 0, 0, 0)
    for button in buttons.buttons():
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
    return buttons


def hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty('muted', True)
    return label


def inherited_bindings(widget):
    """Nested dialogs use the same configured actions as their workspace."""
    while widget is not None:
        for name in ('result_bindings', 'scope_bindings', 'bindings'):
            values = getattr(widget, name, None)
            if values is not None:
                return DEFAULT_DIALOG_BINDINGS | values
        widget = widget.parentWidget()
    return dict(DEFAULT_DIALOG_BINDINGS)


class DialogKeyboard(QObject):
    """Context-aware dialog actions, including children created after opening.

    Enter in an editor accepts a form; multiline text and interactive previews
    keep their own Enter behavior. The explicit accept shortcut works anywhere.
    Popups and nested dialogs handle their own keys before their parent form.
    """
    def __init__(self, dialog, primary, *, reserved=(), field_widgets=()):
        super().__init__(dialog)
        self.dialog, self.primary = dialog, primary
        self.reserved = {key for value in reserved for key in shortcut_keys(value)}
        self.field_widgets = tuple(field_widgets)
        self._watching = False
        for button in dialog.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        dialog.installEventFilter(self)
        if dialog.isVisible():
            self._watch(True)
        self.refresh_tooltips()

    def _watch(self, visible):
        if visible != self._watching:
            self._watching = visible
            app = QApplication.instance()
            if visible:
                app.installEventFilter(self)
            else:
                app.removeEventFilter(self)

    def bindings(self):
        values = inherited_bindings(self.dialog)
        return {action: [key for key in shortcut_keys(values.get(action, '')) if key not in self.reserved]
                for action in DEFAULT_DIALOG_BINDINGS}

    def refresh_tooltips(self):
        bindings = self.bindings()
        for name in ('save_button', 'import_button', 'accept_button', 'apply_button'):
            button = getattr(self.dialog, name, None)
            if button is not None:
                keys = shortcut_label(bindings.get('dialog_accept', ''))
                text = button.text().replace('&', '')
                if keys:
                    text += ' · ' + keys + ' from any field'
                form = shortcut_label(bindings.get('dialog_accept_field', ''))
                if form:
                    text += '; ' + form + ' from form fields'
                button.setToolTip(text)

    def _form_target(self, widget):
        while widget is not None and widget is not self.dialog:
            if isinstance(widget, (QTextEdit, QPlainTextEdit, QKeySequenceEdit)) or widget.property('shortcutCapture'):
                return None
            if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox, QPushButton, QCheckBox)):
                return widget
            if widget in self.field_widgets:
                return widget
            widget = widget.parentWidget()
        return None

    def eventFilter(self, obj, event):
        # Qt can still deliver destruction events after Python has cleared a
        # parent-owned dialog/helper's attributes during garbage collection.
        dialog = getattr(self, 'dialog', None)
        if dialog is None or not isValid(dialog):
            return False
        if obj is dialog and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self._watch(event.type() == QEvent.Type.Show)
            return False
        if event.type() not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        if not isinstance(obj, QWidget) or not self.dialog.isVisible() or obj.window() is not self.dialog:
            return False
        if QApplication.activePopupWidget() is not None:
            return False
        widget = obj
        while widget is not None and widget is not self.dialog:
            if isinstance(widget, QKeySequenceEdit) or widget.property('shortcutCapture'):
                return False
            widget = widget.parentWidget()
        bindings = self.bindings()
        target = self._form_target(obj)
        action = None
        for printable_alias in (False, True):
            action = next((name for name in ('dialog_close', 'dialog_accept', 'dialog_accept_field')
                           if (name != 'dialog_accept_field' or target is not None)
                           and shortcut_matches(event, bindings.get(name, ''), printable_alias=printable_alias)), None)
            if action is not None:
                break
        if action:
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if action == 'dialog_close':
                self.dialog.reject()
            elif action == 'dialog_accept_field' and isinstance(target, QPushButton):
                if target.isEnabled():
                    target.click()
            else:
                spin = target if isinstance(target, QAbstractSpinBox) else target.parentWidget() if target else None
                if isinstance(spin, QAbstractSpinBox):
                    spin.interpretText()
                self.primary()
            event.accept()
            return True
        # QDialog has hard-coded Escape/default-button fallbacks. Do not let
        # those silently bypass a remapped or disabled action.
        if (event.type() == QEvent.Type.KeyPress and obj is self.dialog
                and event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
            event.accept()
            return True
        return False


def install_dialog_keyboard(dialog, primary, *, reserved=(), field_widgets=()):
    dialog.dialog_keyboard = DialogKeyboard(dialog, primary, reserved=reserved, field_widgets=field_widgets)
    return dialog.dialog_keyboard


class FormDialog(QDialog):
    """Native form navigation with one explicit primary action."""
    def configure_keyboard(self, fields, buttons, primary, reserved=()):
        self._primary_action = primary
        self._validation_field = None
        for field in [*fields, *buttons]:
            if isinstance(field, (QTextEdit, QPlainTextEdit)):
                field.setTabChangesFocus(True)
        chain = [*fields, *buttons]
        for before, after in zip(chain, chain[1:]):
            self.setTabOrder(before, after)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        reserved = {key for value in reserved for key in shortcut_keys(value)}
        def conflicts(sequence):
            return any(sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                       or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch for value in reserved)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (QLabel, QPushButton, QCheckBox)):
                mnemonic = QKeySequence.mnemonic(widget.text())
                if not mnemonic.isEmpty() and conflicts(mnemonic):
                    widget.setText(widget.text().replace('&', ''))
        install_dialog_keyboard(self, primary, reserved=reserved, field_widgets=fields)

    def show_error(self, message, field=None):
        self.error.setText(str(message))
        field = field or self._validation_field
        if field is not None and field.isEnabled() and field.isVisible():
            field.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(field, QLineEdit):
                field.selectAll()
