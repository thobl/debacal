"""Selection mode bindings and compatibility helpers for older settings.

These helpers have no widget state. Keyboard masks use Qt's stable modifier
bits, so callers may pass either Qt flags or integers.
"""

DEFAULT_SCOPE_MODIFIERS = {'all': 'Ctrl', 'following': 'Shift'}
SCOPE_MODIFIERS = ('Ctrl', 'Shift', 'Alt', 'Meta')
SCOPED_ACTIONS = ('edit_event', 'copy_event', 'paste_event', 'duplicate_event', 'delete_event')
SELECTION_MODES = ('single', 'group', 'future')
DEFAULT_MODE_BINDINGS = {'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'}
MODE_ACTIONS = {'scope_single': 'single', 'scope_all': 'group', 'scope_following': 'future'}
_MODIFIER_BITS = {'Shift': 0x02000000, 'Ctrl': 0x04000000,
                  'Alt': 0x08000000, 'Meta': 0x10000000}


def normalize_scope_modifiers(settings=None):
    """Return a complete, validated copy of the two modifier preferences."""
    settings = DEFAULT_SCOPE_MODIFIERS | (settings or {})
    result = {key: settings[key] for key in DEFAULT_SCOPE_MODIFIERS}
    if any(value not in SCOPE_MODIFIERS for value in result.values()):
        raise ValueError('Choose Ctrl, Shift, Alt, or Meta for each event scope.')
    if result['all'] == result['following']:
        raise ValueError('All events and following events need different modifiers.')
    return result


def scope_from_modifiers(modifiers, settings=None):
    """Resolve a gesture; following wins when both scope modifiers are held."""
    settings = normalize_scope_modifiers(settings)
    mask = getattr(modifiers, 'value', modifiers)
    if int(mask) & _MODIFIER_BITS[settings['following']]:
        return 'future'
    if int(mask) & _MODIFIER_BITS[settings['all']]:
        return 'group'
    return 'single'


def expanded_scope_bindings(bindings, settings=None):
    """Include generated scope shortcuts in collision checks.

    Event actions use one stroke, without either configured scope modifier:
    adding a modifier must always select a different operation scope.
    """
    # Persistent modes do not reserve modifier variants of ordinary actions.
    if any(key in bindings for key in DEFAULT_MODE_BINDINGS):
        return dict(bindings)
    settings = normalize_scope_modifiers(settings)
    result = dict(bindings)
    for action in SCOPED_ACTIONS:
        shortcut = bindings.get(action, '')
        if not shortcut:
            continue
        # A comma key is itself a stroke; other commas separate Qt chords.
        comma_key = shortcut == ',' or (shortcut.endswith('+,') and shortcut.count(',') == 1)
        if ',' in shortcut and not comma_key:
            raise ValueError('Event action shortcuts must use a single keystroke.')
        parts = shortcut.split('+')
        modifiers = set(parts[:-1])
        if modifiers & set(settings.values()):
            raise ValueError('Event action shortcuts must leave the scope modifiers free.')
        result[f'{action}:group'] = settings['all'] + '+' + shortcut
        result[f'{action}:future'] = settings['following'] + '+' + shortcut
        result[f'{action}:future_combined'] = settings['all'] + '+' + settings['following'] + '+' + shortcut
    return result
