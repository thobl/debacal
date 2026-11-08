"""Grouped keyboard shortcut controls, migrations, and collision validation."""
from PySide6.QtCore import QEvent
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget, QFormLayout, QVBoxLayout, QLabel, QScrollArea, QKeySequenceEdit, QFrame

from .event_scope import DEFAULT_MODE_BINDINGS


RETIRED_ACTIONS = frozenset(('select_event', 'shortcuts', 'cycle_scope', 'duplicate_event', 'redo_alternative'))
LEGACY_MODE_BINDINGS = {'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'}
GROUPS = (
    ('Calendar view', ('day_view', 'week_view', 'month_view', 'year_view', 'full_day')),
    ('Navigation', ('today', 'previous', 'next', 'search')),
    ('Selection', ('scope_single', 'scope_all', 'scope_following', 'select_left', 'select_right', 'select_up', 'select_down')),
    ('Events', ('new_event', 'edit_event', 'edit_single', 'delete_event', 'copy_event', 'paste_event', 'export_selection')),
    ('History', ('undo', 'redo', 'history')),
    ('Application', ('sync', 'preferences')),
)


LABELS = {
    'scope_single': 'Select one event', 'scope_all': 'Select all in series',
    'scope_following': 'Select this and following',
    'edit_single': 'Edit one event', 'previous': 'Previous period', 'next': 'Next period',
    'edit_event': 'Edit selection', 'delete_event': 'Delete selection',
    'copy_event': 'Copy selection', 'paste_event': 'Paste events',
    'day_view': 'Day', 'week_view': 'Week', 'month_view': 'Month', 'year_view': 'Year',
    'full_day': 'Show full day', 'sync': 'Sync now',
    'export_selection': 'Export selected events',
}


def active_bindings(bindings):
    """Keep retired commands out of both stored settings and preference fields."""
    return {key: value for key, value in (bindings or {}).items() if key not in RETIRED_ACTIONS}


def migrate_selection_bindings(bindings, defaults, *, migrate_modes=True):
    """Shift former mode defaults, yielding to explicitly remapped commands.

    Old preferences saved complete maps, so default-valued entries must first be
    removed together. Otherwise a former F2/F3 assignment looks like a conflict
    with the new defaults. Custom bindings (including disabled keys) survive.
    """
    result = active_bindings(bindings)
    if not migrate_modes:
        return result
    for action, former in LEGACY_MODE_BINDINGS.items():
        if QKeySequence(result.get(action, '')).toString(QKeySequence.SequenceFormat.PortableText) == former:
            result.pop(action)
    for action in DEFAULT_MODE_BINDINGS:
        if action in result:
            continue
        candidate = QKeySequence(defaults.get(action, ''))
        if candidate.isEmpty():
            continue
        if any(value and (candidate.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                          or QKeySequence(value).matches(candidate) != QKeySequence.SequenceMatch.NoMatch)
               for other, value in result.items() if other != action):
            result[action] = ''
    return result


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
        self.defaults = active_bindings(defaults if defaults is not None else bindings)
        values = self.initial_bindings = self.defaults | active_bindings(bindings)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = self.scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName('preferencesPage')
        sections = QVBoxLayout(body)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(16)
        self.fields = {}
        self.labels = []
        self.groups = {}
        groups = [(name, tuple(key for key in actions if key in values)) for name, actions in GROUPS]
        configured = {key for _name, actions in groups for key in actions}
        groups.append(('Other', tuple(key for key in values if key not in configured)))
        for name, actions in groups:
            if not actions:
                continue
            self.groups[name] = actions
            section = QWidget()
            layout = QVBoxLayout(section)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            heading = QLabel(name)
            heading.setObjectName('sidebarSectionHeading')
            layout.addWidget(heading)
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for key in actions:
                caption = LABELS.get(key, key.replace('_', ' ').capitalize())
                field = QKeySequenceEdit(QKeySequence(values[key]))
                field.setAccessibleName(caption + ' shortcut')
                field.setToolTip('Leave empty to disable this shortcut.')
                self.fields[key] = field
                label = QLabel(caption)
                label.setBuddy(field)
                self.labels.append(label)
                form.addRow(label, field)
            layout.addLayout(form)
            sections.addWidget(section)
        if not values:
            sections.addWidget(QLabel('No shortcut bindings were provided for this workspace.'))
        sections.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('error', True)
        outer.addWidget(self.error)
        self._align_fields()

    def _align_fields(self):
        if not hasattr(self, 'labels'):
            return
        metrics = self.fontMetrics()
        width = max((metrics.horizontalAdvance(label.text()) for label in self.labels), default=0)
        for label in self.labels:
            label.setFixedWidth(width + 4)
        field_width = max(220, metrics.horizontalAdvance('Ctrl+Shift+K, Ctrl+Shift+L') + 32)
        for field in self.fields.values():
            field.setMaximumWidth(field_width)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._align_fields()

    def value(self):
        return validate_bindings({key: field.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                                  for key, field in self.fields.items()})

    def restore_defaults(self):
        for key, field in self.fields.items():
            field.setKeySequence(QKeySequence(self.defaults.get(key, '')))
        self.error.clear()
