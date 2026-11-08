"""Shared keyboard behavior and small controls for native dialogs."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QLabel, QWidget, QTextEdit, QPushButton, QCheckBox,
                              QLineEdit, QComboBox, QAbstractSpinBox, QDateTimeEdit)


def hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty('muted', True)
    return label


class FormDialog(QDialog):
    """Native form navigation with one explicit primary action."""
    def configure_keyboard(self, fields, buttons, primary, reserved=()):
        self._primary_action = primary
        self._validation_field = None
        for field in [*fields, *buttons]:
            if isinstance(field, QTextEdit):
                field.setTabChangesFocus(True)
            else:
                field.installEventFilter(self)
                if isinstance(field, QAbstractSpinBox):
                    field.lineEdit().installEventFilter(self)
        chain = [*fields, *buttons]
        for before, after in zip(chain, chain[1:]):
            self.setTabOrder(before, after)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.save_shortcuts = []
        reserved = {QKeySequence(value).toString(QKeySequence.SequenceFormat.PortableText) for value in reserved if value}
        def conflicts(sequence):
            return any(sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                       or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch for value in reserved)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, (QLabel, QPushButton, QCheckBox)):
                mnemonic = QKeySequence.mnemonic(widget.text())
                if not mnemonic.isEmpty() and conflicts(mnemonic):
                    widget.setText(widget.text().replace('&', ''))
        for key in ('Ctrl+Return', 'Ctrl+Enter'):
            if conflicts(QKeySequence(key)):
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.save)
            self.save_shortcuts.append(shortcut)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
            if isinstance(obj, QComboBox) and obj.view().isVisible():
                return super().eventFilter(obj, event)
            spin = obj if isinstance(obj, QAbstractSpinBox) else obj.parent()
            if isinstance(spin, QDateTimeEdit) and spin.calendarWidget().isVisible():
                return super().eventFilter(obj, event)
            if isinstance(spin, QAbstractSpinBox):
                spin.interpretText()
            obj.click() if isinstance(obj, QPushButton) else self._primary_action()
            return True
        return super().eventFilter(obj, event)

    def show_error(self, message, field=None):
        self.error.setText(str(message))
        field = field or self._validation_field
        if field is not None and field.isEnabled() and field.isVisible():
            field.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(field, QLineEdit):
                field.selectAll()
