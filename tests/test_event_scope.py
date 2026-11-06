"""The same modifier policy applies to mouse and keyboard event actions."""
import pytest
from PySide6.QtCore import Qt

from debacal.event_scope import (
    DEFAULT_SCOPE_MODIFIERS, expanded_scope_bindings,
    normalize_scope_modifiers, scope_from_modifiers,
)


@pytest.mark.parametrize(('modifiers', 'scope'), [
    (Qt.KeyboardModifier.NoModifier, 'single'),
    (Qt.KeyboardModifier.ControlModifier, 'group'),
    (Qt.KeyboardModifier.ShiftModifier, 'future'),
    (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, 'future'),
    (Qt.KeyboardModifier.AltModifier, 'single'),
    (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier, 'group'),
])
def test_default_gesture_scopes(modifiers, scope):
    assert scope_from_modifiers(modifiers) == scope
    assert scope_from_modifiers(modifiers.value) == scope


def test_configurable_modifiers_and_combined_priority():
    config = {'all': 'Alt', 'following': 'Meta'}
    assert scope_from_modifiers(Qt.KeyboardModifier.ControlModifier, config) == 'single'
    assert scope_from_modifiers(Qt.KeyboardModifier.AltModifier, config) == 'group'
    assert scope_from_modifiers(Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier, config) == 'future'
    assert config == {'all': 'Alt', 'following': 'Meta'}


@pytest.mark.parametrize('config', [{'all': 'Shift'}, {'following': 'Ctrl'}, {'all': ''}, {'all': 'Super'}])
def test_ambiguous_or_unknown_modifiers_rejected(config):
    with pytest.raises(ValueError):
        normalize_scope_modifiers(config)


def test_default_configuration_is_not_mutated():
    result = normalize_scope_modifiers()
    result['all'] = 'Alt'
    assert DEFAULT_SCOPE_MODIFIERS == {'all': 'Ctrl', 'following': 'Shift'}


def test_generated_scopes_include_all_event_operations():
    bindings = {'edit_event': 'E', 'copy_event': 'C', 'paste_event': 'V',
                'duplicate_event': 'D', 'delete_event': 'Del', 'day_view': '1'}
    expanded = expanded_scope_bindings(bindings)
    assert len(expanded) == len(bindings) + 15
    for action in ('edit_event', 'copy_event', 'paste_event', 'duplicate_event', 'delete_event'):
        assert expanded[action + ':group'] == 'Ctrl+' + bindings[action]
        assert expanded[action + ':future'] == 'Shift+' + bindings[action]
        assert expanded[action + ':future_combined'] == 'Ctrl+Shift+' + bindings[action]
    assert bindings['edit_event'] == 'E'


@pytest.mark.parametrize('shortcut', ['Ctrl+E', 'Shift+E', 'Ctrl+K, E'])
def test_scope_modifiers_and_chords_cannot_make_scopes_indistinguishable(shortcut):
    with pytest.raises(ValueError):
        expanded_scope_bindings({'edit_event': shortcut})


def test_unassigned_modifiers_and_disabled_actions_are_supported():
    expanded = expanded_scope_bindings({'edit_event': 'Ctrl+E', 'copy_event': ''},
                                       {'all': 'Alt', 'following': 'Shift'})
    assert expanded['edit_event:group'] == 'Alt+Ctrl+E'
    assert 'copy_event:group' not in expanded
