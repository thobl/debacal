"""Single-chord shortcut alternatives, grouped editing, and migrations."""
from PySide6.QtCore import QEvent, Qt, QKeyCombination
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget, QFormLayout, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QKeySequenceEdit, QFrame

from .event_scope import DEFAULT_MODE_BINDINGS
from .buttons import action_button


DEFAULT_DIALOG_BINDINGS = {'dialog_accept': 'Ctrl+Return', 'dialog_accept_field': 'Return', 'dialog_close': 'Esc'}
DIALOG_ACTIONS = frozenset(DEFAULT_DIALOG_BINDINGS)
RETIRED_ACTIONS = frozenset(('select_event', 'shortcuts', 'cycle_scope', 'duplicate_event', 'redo_alternative', 'edit_single'))
LEGACY_MODE_BINDINGS = {'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'}
GROUPS = (
    ('Calendar view', ('day_view', 'week_view', 'month_view', 'year_view', 'full_day')),
    ('Navigation', ('today', 'previous', 'next', 'search')),
    ('Selection', ('scope_single', 'scope_all', 'scope_following', 'select_left', 'select_right', 'select_up', 'select_down')),
    ('Events', ('new_event', 'edit_event', 'delete_event', 'copy_event', 'paste_event', 'export_selection')),
    ('History', ('undo', 'redo', 'history')),
    ('Dialogs', ('dialog_accept', 'dialog_accept_field', 'dialog_close')),
    ('Application', ('sync', 'preferences')),
)


LABELS = {
    'scope_single': 'Select one event', 'scope_all': 'Select all in series',
    'scope_following': 'Select this and following',
    'previous': 'Previous period', 'next': 'Next period',
    'edit_event': 'Edit selection', 'delete_event': 'Delete selection',
    'copy_event': 'Copy selection', 'paste_event': 'Paste events',
    'day_view': 'Day', 'week_view': 'Week', 'month_view': 'Month', 'year_view': 'Year',
    'full_day': 'Show full day', 'sync': 'Sync now',
    'export_selection': 'Export selected events',
    'dialog_accept': 'Accept dialog', 'dialog_accept_field': 'Accept from form fields', 'dialog_close': 'Close dialog',
}


def shortcut_keys(value, *, strict=False):
    """Normalize alternatives without truncating a retired multi-stroke binding."""
    raw_keys = (value,) if isinstance(value, str) else value if isinstance(value, (list, tuple)) else ()
    result = []
    for raw in raw_keys:
        if not isinstance(raw, str):
            if strict:
                raise ValueError('A shortcut must be a single key combination.')
            continue
        sequence = QKeySequence(raw)
        if sequence.isEmpty():
            if raw.strip() and strict:
                raise ValueError('Choose a valid single key combination.')
            continue
        if sequence.count() != 1 or sequence[0].key() in (Qt.Key.Key_unknown, Qt.Key(0)):
            if strict:
                raise ValueError('Use a single key combination per shortcut; add another shortcut for alternatives.')
            continue
        combination = sequence[0]
        key = Qt.Key.Key_Return if combination.key() == Qt.Key.Key_Enter else combination.key()
        modifiers = combination.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
        normalized = QKeySequence(QKeyCombination(modifiers, key)).toString(QKeySequence.SequenceFormat.PortableText)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def pack_shortcuts(keys):
    keys = tuple(keys)
    return '' if not keys else keys[0] if len(keys) == 1 else list(keys)


def shortcut_label(value):
    return ' or '.join(QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText).replace('Return', 'Enter')
                      for key in shortcut_keys(value))


def shortcut_sequences(value, *, include_enter_alias=True):
    """Qt registrations, including both physical Enter keys when requested."""
    result = []
    for key in shortcut_keys(value):
        sequence = QKeySequence(key)
        result.append(sequence)
        combination = sequence[0]
        if include_enter_alias and combination.key() == Qt.Key.Key_Return:
            modifiers = combination.keyboardModifiers()
            for physical_key, mask in ((Qt.Key.Key_Enter, modifiers),
                                       (Qt.Key.Key_Enter, modifiers | Qt.KeyboardModifier.KeypadModifier),
                                       (Qt.Key.Key_Return, modifiers | Qt.KeyboardModifier.KeypadModifier)):
                result.append(QKeySequence(QKeyCombination(mask, physical_key)))
    return result


def shortcut_matches(event, value, *, printable_alias=True):
    combination = event.keyCombination()
    key = Qt.Key.Key_Return if combination.key() == Qt.Key.Key_Enter else combination.key()
    modifiers = combination.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
    variants = [QKeySequence(QKeyCombination(modifiers, key))]
    char = event.text()
    command_modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
    if (printable_alias and not modifiers & command_modifiers and len(char) == 1 and char.isprintable()
            and not char.isalnum() and not char.isspace()):
        variants.append(QKeySequence(char))
    return any(candidate.matches(sequence) == QKeySequence.SequenceMatch.ExactMatch
               for candidate in variants for sequence in shortcut_sequences(value, include_enter_alias=False))


def normalize_bindings(bindings, *, strict=False):
    return {key: pack_shortcuts(shortcut_keys(value, strict=strict)) for key, value in active_bindings(bindings).items()}


def shortcut_contexts(action):
    if action in DIALOG_ACTIONS:
        return {'dialog'}
    if action in DEFAULT_MODE_BINDINGS or action == 'full_day':
        return {'workspace', 'dialog'}
    return {'workspace'}


def conflicts_with(action, value, bindings):
    keys = set(shortcut_keys(value))
    return any(shortcut_contexts(action) & shortcut_contexts(other) and keys & set(shortcut_keys(binding))
               for other, binding in bindings.items() if other != action)


def active_bindings(bindings):
    """Keep retired commands out of both stored settings and preference fields."""
    return {key: value for key, value in (bindings or {}).items() if key not in RETIRED_ACTIONS}


def migrate_selection_bindings(bindings, defaults, *, migrate_modes=True):
    """Shift former mode defaults, yielding to explicitly remapped commands.

    Old preferences saved complete maps, so default-valued entries must first be
    removed together. Otherwise a former F2/F3 assignment looks like a conflict
    with the new defaults. Custom bindings (including disabled keys) survive.
    """
    result = normalize_bindings(bindings)
    if not migrate_modes:
        return result
    for action, former in LEGACY_MODE_BINDINGS.items():
        if shortcut_keys(result.get(action, '')) == (former,):
            result.pop(action)
    for action in DEFAULT_MODE_BINDINGS:
        if action in result:
            continue
        if conflicts_with(action, defaults.get(action, ''), result):
            result[action] = ''
    return result


def validate_bindings(bindings):
    normalized = normalize_bindings(bindings, strict=True)
    for action, value in normalized.items():
        if conflicts_with(action, value, normalized):
            raise ValueError('Each action needs unique shortcuts within the same context.')
    return normalized


def migrate_workspace_bindings(bindings, defaults, *, version=0):
    """Upgrade prior defaults, then let every custom chord keep its owner."""
    raw = dict(bindings or {})
    alternative = raw.pop('redo_alternative', None)
    values = active_bindings(raw)
    if version < 2:
        for action, former in (('day_view', '1'), ('copy_event', 'C'), ('paste_event', 'V')):
            if values.get(action) == former:
                values.pop(action)
    if values.get('search') == '/':
        values['search'] = 'Ctrl+F'
    if version < 5 and values.get('redo') in (None, 'Ctrl+Y', 'Ctrl+Shift+Z'):
        if alternative and alternative not in ('Ctrl+Y', 'Ctrl+Shift+Z'):
            values['redo'] = alternative
        else:
            values.pop('redo', None)
    values = migrate_selection_bindings(values, defaults, migrate_modes=version < 6)
    if version < 7:
        for action, former in (('previous', 'H'), ('next', 'L'), ('edit_event', 'E'), ('search', 'Ctrl+F')):
            if shortcut_keys(values.get(action, '')) == (former,):
                values.pop(action)
    values = normalize_bindings(values)
    for action, value in defaults.items():
        if action not in values:
            keys = [key for key in shortcut_keys(value) if not conflicts_with(action, key, values)]
            values[action] = pack_shortcuts(keys)
    return values


class ShortcutAlternatives(QWidget):
    """One editable key combination per row with explicit alternatives."""
    def __init__(self, value, caption, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.setAccessibleName(caption + ' shortcuts')
        self.edits, self.rows = [], []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(5)
        self.set_value(value)

    def add_shortcut(self, value=''):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        field = QKeySequenceEdit(QKeySequence(value), row)
        field.setMaximumSequenceLength(1)
        field.setClearButtonEnabled(True)
        field.setAccessibleName(self.caption + ' shortcut')
        field.setToolTip('Press one key combination. Clear to disable it.')
        layout.addWidget(field, 1)
        remove = action_button('−', lambda: self.remove_shortcut(field), tooltip='Remove this shortcut')
        remove.setProperty('iconOnly', True)
        remove.setAccessibleName('Remove ' + self.caption.lower() + ' shortcut')
        remove.setFixedWidth(28)
        layout.addWidget(remove)
        add = action_button('+', lambda: self.add_shortcut().setFocus(), tooltip='Add another shortcut for this action')
        add.setProperty('iconOnly', True)
        add.setAccessibleName('Add ' + self.caption.lower() + ' shortcut')
        add.setFixedWidth(28)
        layout.addWidget(add)
        self.edits.append(field)
        self.rows.append((row, remove, add))
        self.layout.addWidget(row)
        self._update_buttons()
        self.setFocusProxy(self.edits[0])
        return field

    def _update_buttons(self):
        for index, (_row, remove, add) in enumerate(self.rows):
            for button in (remove, add):
                policy = button.sizePolicy()
                policy.setRetainSizeWhenHidden(True)
                button.setSizePolicy(policy)
            remove.setVisible(len(self.rows) > 1)
            add.setVisible(index == len(self.rows) - 1)

    def remove_shortcut(self, field):
        if field not in self.edits:
            return
        index = self.edits.index(field)
        if len(self.edits) == 1:
            field.clear()
            return
        self.edits.pop(index)
        row, _remove, _add = self.rows.pop(index)
        self.layout.removeWidget(row)
        row.deleteLater()
        self.setFocusProxy(self.edits[0])
        self._update_buttons()

    def set_value(self, value):
        for row, _remove, _add in self.rows:
            self.layout.removeWidget(row)
            row.deleteLater()
        self.edits, self.rows = [], []
        for key in shortcut_keys(value) or ('',):
            self.add_shortcut(key)

    def value(self):
        return pack_shortcuts(key for field in self.edits for key in shortcut_keys(field.keySequence().toString(), strict=True))

    def keySequence(self):
        return self.edits[0].keySequence()

    def setKeySequence(self, sequence):
        value = sequence.toString() if isinstance(sequence, QKeySequence) else sequence
        self.set_value(value)


class KeyboardShortcutsEditor(QWidget):
    def __init__(self, bindings=None, defaults=None, parent=None):
        super().__init__(parent)
        self.defaults = normalize_bindings(defaults if defaults is not None else bindings)
        values = self.initial_bindings = self.defaults | normalize_bindings(bindings)
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
                field = ShortcutAlternatives(values[key], caption)
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
        field_width = max(240, metrics.horizontalAdvance('Ctrl+Shift+PageDown') + 104)
        for field in self.fields.values():
            field.setMaximumWidth(field_width)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._align_fields()

    def value(self):
        return validate_bindings({key: field.value() for key, field in self.fields.items()})

    def restore_defaults(self):
        for key, field in self.fields.items():
            field.set_value(self.defaults.get(key, ''))
        self.error.clear()
