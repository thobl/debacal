"""Reusable keyboard shortcut controls and collision validation."""
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget, QFormLayout, QVBoxLayout, QLabel, QScrollArea, QKeySequenceEdit


LABELS = {
    'scope_single': 'Select one event', 'scope_all': 'Select all in series',
    'scope_following': 'Select this and following', 'cycle_scope': 'Cycle selection mode',
    'edit_single': 'Edit one event (optional)', 'previous': 'Previous page', 'next': 'Next page',
    'export_selection': 'Export selected events',
}


def validate_bindings(bindings):
    normalized = {key: QKeySequence(value).toString(QKeySequence.SequenceFormat.PortableText)
                  for key, value in bindings.items()}
    used = [value for value in normalized.values() if value]
    if len(used) != len(set(used)):
        raise ValueError('Each action needs a unique shortcut.')
    sequences = [QKeySequence(value) for value in used]
    for index, sequence in enumerate(sequences):
        for other in sequences[index + 1:]:
            if (sequence.matches(other) != QKeySequence.SequenceMatch.NoMatch
                    or other.matches(sequence) != QKeySequence.SequenceMatch.NoMatch):
                raise ValueError('One shortcut cannot be the start of another shortcut.')
    return normalized


class KeyboardShortcutsEditor(QWidget):
    def __init__(self, bindings=None, defaults=None, parent=None):
        super().__init__(parent)
        self.defaults = dict(defaults if defaults is not None else bindings or {})
        values = self.defaults | dict(bindings or {})
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        note = QLabel('Selection modes have their own shortcuts. Copy and paste keep their usual behavior in text fields. '
                      'Leave a field empty to disable an action.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        outer.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        self.fields = {}
        for key, value in values.items():
            field = QKeySequenceEdit(QKeySequence(value))
            self.fields[key] = field
            form.addRow(LABELS.get(key, key.replace('_', ' ').capitalize()), field)
        if not values:
            form.addRow(QLabel('No shortcut bindings were provided for this workspace.'))
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #b54735;')
        outer.addWidget(self.error)

    def value(self):
        return validate_bindings({key: field.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                                  for key, field in self.fields.items()})

    def restore_defaults(self):
        for key, field in self.fields.items():
            field.setKeySequence(QKeySequence(self.defaults.get(key, '')))
        self.error.clear()
