"""Persistent selection keys upgrade without taking customized shortcuts."""
import pytest

from debacal.event_scope import DEFAULT_MODE_BINDINGS, MODE_ACTIONS, SELECTION_MODES
from debacal.shortcuts import active_bindings, migrate_selection_bindings, normalize_bindings, validate_bindings


def test_default_mode_keys_match_toolbar_selection_order():
    assert DEFAULT_MODE_BINDINGS == {'scope_single': 'F1', 'scope_all': 'F2', 'scope_following': 'F3'}
    assert tuple(MODE_ACTIONS.values()) == SELECTION_MODES


@pytest.mark.parametrize('saved', [
    {},
    {'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'},
    {'shortcuts': 'F1', 'cycle_scope': 'Alt+S', 'scope_single': 'F2', 'scope_all': 'F3', 'scope_following': 'F4'},
])
def test_previous_default_bindings_upgrade_together(saved):
    original = dict(saved)
    migrated = migrate_selection_bindings(saved, DEFAULT_MODE_BINDINGS)
    assert DEFAULT_MODE_BINDINGS | migrated == DEFAULT_MODE_BINDINGS
    assert saved == original


@pytest.mark.parametrize(('saved', 'expected'), [
    ({'history': 'F1'}, {'scope_single': '', 'scope_all': 'F2', 'scope_following': 'F3'}),
    ({'history': 'F1, H'}, {'scope_single': 'F1', 'scope_all': 'F2', 'scope_following': 'F3'}),
    ({'history': 'F2'}, {'scope_single': 'F1', 'scope_all': '', 'scope_following': 'F3'}),
    ({'history': 'F3'}, {'scope_single': 'F1', 'scope_all': 'F2', 'scope_following': ''}),
    ({'scope_all': 'F1'}, {'scope_single': '', 'scope_all': 'F1', 'scope_following': 'F3'}),
    ({'scope_following': 'F2'}, {'scope_single': 'F1', 'scope_all': '', 'scope_following': 'F2'}),
    ({'scope_single': 'F6', 'scope_all': '', 'scope_following': 'Alt+F'},
     {'scope_single': 'F6', 'scope_all': '', 'scope_following': 'Alt+F'}),
])
def test_upgrade_respects_custom_and_disabled_bindings(saved, expected):
    original = dict(saved)
    result = DEFAULT_MODE_BINDINGS | migrate_selection_bindings(saved, DEFAULT_MODE_BINDINGS)
    assert {key: result[key] for key in DEFAULT_MODE_BINDINGS} == expected
    for key, value in saved.items():
        if key not in DEFAULT_MODE_BINDINGS:
            assert result[key] == normalize_bindings({key: value})[key]
    validate_bindings(result)
    assert saved == original


def test_retired_commands_stay_retired_without_remigrating_new_profile_keys():
    saved = {'scope_single': 'F2', 'scope_all': 'F7', 'scope_following': 'F3',
             'shortcuts': 'F8', 'cycle_scope': 'Alt+S', 'select_event': 'G', 'today': 'T'}
    expected = {'scope_single': 'F2', 'scope_all': 'F7', 'scope_following': 'F3', 'today': 'T'}
    assert migrate_selection_bindings(saved, DEFAULT_MODE_BINDINGS, migrate_modes=False) == expected
    assert active_bindings(saved) == expected


def test_disabled_custom_selection_actions_are_not_reenabled_on_upgrade():
    saved = {key: '' for key in DEFAULT_MODE_BINDINGS}
    assert migrate_selection_bindings(saved, DEFAULT_MODE_BINDINGS) == saved
