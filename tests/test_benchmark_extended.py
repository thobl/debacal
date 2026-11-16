"""The sustained local benchmark must retain tails and compare real versions."""
from datetime import timedelta
from pathlib import Path
import sys
import types

import pytest

if not (Path(__file__).resolve().parents[1] / 'scripts/benchmark_extended.py').is_file():
    pytest.skip('Maintainer tooling is not included in the source archive', allow_module_level=True)

from scripts.benchmark_extended import (
    ANCHOR, BLOCKS, ROOT, Config, build_fixture, fixture_events, interaction_plan,
    expected_fixture_selection, isolated_environment, main, source_metadata, summary, verify_source,
)


def test_statistics_keep_average_percentile_and_actual_worst_case_separate():
    result = summary([2, 3, 100])
    assert result == {'samples': 3, 'mean_ms': 35.0, 'p95_ms': 100, 'max_ms': 100, 'min_ms': 2}
    assert summary(list(range(1, 21)))['p95_ms'] == 19
    assert summary(list(range(1, 21)))['max_ms'] == 20
    assert summary([])['samples'] == 0
    assert summary([])['max_ms'] is None


def test_every_block_is_repeated_with_reproducible_mixed_orders_and_new_date_windows():
    plan = interaction_plan(12, 81423)
    assert plan == interaction_plan(12, 81423)
    assert plan != interaction_plan(12, 81424)
    assert len({tuple(entry['blocks']) for entry in plan}) > 6
    targets = []
    for entry in plan:
        assert set(entry['blocks']) == set(BLOCKS)
        assert len(entry['blocks']) == len(BLOCKS)
        assert set(entry['views']) == {'day', 'week', 'month', 'year'}
        assert len(set(entry['offsets'])) == 4
        targets.extend(offset + entry['round'] * 2203 for offset in entry['offsets'])
    assert len(targets) == len(set(targets))
    assert targets != sorted(targets)


def test_fixture_includes_active_rules_large_overrides_custom_dates_and_payloads():
    config = Config(events=12, series=12, finite=2, active_series=3,
                    heavy_overrides=25, candidate_dates=40, payload_bytes=128)
    events = fixture_events(config)
    assert len(events) == 12 + 12 + 2 + 3 + 5
    assert len({event.id for event in events}) == len(events)
    heavy = next(event for event in events if event.id == 'bench-heavy')
    assert len(heavy.recurrence_overrides) == 25
    assert all(value['description'] for value in heavy.recurrence_overrides.values())
    candidate = next(event for event in events if event.id == 'bench-candidate')
    assert candidate.candidate and len(candidate.recurrence_dates) == 39
    assert any(event.recurrence_rule == 'FREQ=DAILY' for event in events)
    assert any(event.recurrence_rule == 'FREQ=WEEKLY' for event in events)
    assert any(event.start.year < 2020 and event.recurrence_rule for event in events)
    assert any(event.start.year > 2040 and event.recurrence_rule for event in events)
    assert any(event.raw_ics and len(event.raw_ics) > 128 for event in events)
    assert any(event.all_day and event.end - event.start >= timedelta(days=2) for event in events)


def test_fixture_uses_normal_persistence_and_contains_rules_active_in_the_view(tmp_path):
    from debacal.preferences import Preferences
    from debacal.store import Store
    database = tmp_path / 'calendar.sqlite3'
    config = Config(events=12, series=12, finite=2, active_series=3,
                    heavy_overrides=25, candidate_dates=40, payload_bytes=128)
    report = build_fixture(database, config)
    assert report['events'] == 34 and report['database_bytes'] > 0
    with Store(database) as store:
        assert len(store.event_ids()) == report['events']
        assert Preferences.load(store).font_family == 'DejaVu Sans'
        assert not Preferences.load(store).auto_sync_enabled
        assert store.get_default_calendar().id == 'cal-0'
        assert len(store.get_event('bench-heavy').recurrence_overrides) == 25
        occurrences = store.query_occurrences(ANCHOR, ANCHOR + timedelta(days=14))
        assert any(event.series_id.startswith('active-') for event in occurrences)
        assert any(event.series_id == 'bench-heavy' for event in occurrences)


def test_worker_environment_isolates_profiles_but_preserves_requested_display_backend(tmp_path, monkeypatch):
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'synthetic-private-bus')
    monkeypatch.setenv('GNOME_KEYRING_CONTROL', 'synthetic-private-keyring')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'xcb')
    environment = isolated_environment(tmp_path)
    assert environment['QT_QPA_PLATFORM'] == 'xcb'
    assert environment['DBUS_SESSION_BUS_ADDRESS'] == 'unix:path=' + str(tmp_path / 'runtime/no-session-bus')
    assert 'GNOME_KEYRING_CONTROL' not in environment
    for name in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
        path = Path(environment[name])
        assert path.is_relative_to(tmp_path)
        assert path.is_dir() and path.stat().st_mode & 0o777 == 0o700
    assert environment['TZ'] == 'UTC' and environment['PYTHONHASHSEED'] == '0'


def test_application_fingerprint_changes_with_source_but_ignores_unrelated_files(tmp_path):
    package = tmp_path / 'debacal'
    package.mkdir()
    (package / '__init__.py').write_text('VERSION = "synthetic"\n')
    first = source_metadata(tmp_path, 'revision-one')
    (tmp_path / 'unrelated.txt').write_text('not application source')
    assert source_metadata(tmp_path, 'revision-one') == first
    (package / 'model.py').write_text('VALUE = 1\n')
    second = source_metadata(tmp_path, 'revision-one')
    assert second['application_sha256'] != first['application_sha256']
    assert second['application_files'] == 2
    assert second['revision'] == 'revision-one'


def test_source_verification_rejects_mixed_editable_and_archived_modules(tmp_path, monkeypatch):
    verify_source(ROOT)
    fake = types.ModuleType('debacal._benchmark_mixed_probe')
    fake.__file__ = str(tmp_path / 'outside.py')
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    with pytest.raises(RuntimeError, match='different source roots'):
        verify_source(ROOT)


def test_selection_oracle_handles_a_single_visible_primary_and_nominal_cutoffs():
    from dataclasses import replace
    from debacal.models import Event
    primary = Event('first', 'cal-0', 'Synthetic', ANCHOR, ANCHOR + timedelta(hours=1),
                    series_id='family', recurrence_id=ANCHOR.isoformat())
    # A week ending on the primary date legitimately contains just this item.
    assert expected_fixture_selection([primary], primary, 'group') == {'first'}
    later = replace(primary, id='later', start=ANCHOR + timedelta(days=1),
                    end=ANCHOR + timedelta(days=1, hours=1),
                    recurrence_id=(ANCHOR + timedelta(days=1)).isoformat())
    moved_earlier = replace(primary, id='moved', start=ANCHOR + timedelta(days=3),
                           end=ANCHOR + timedelta(days=3, hours=1))
    unrelated = replace(primary, id='other', series_id='another-family')
    values = [primary, later, moved_earlier, unrelated]
    assert expected_fixture_selection(values, later, 'group') == {'first', 'later', 'moved'}
    assert expected_fixture_selection(values, later, 'future') == {'later'}


@pytest.mark.parametrize('options', [
    ['--rounds', '0'], ['--startup-runs', '0'], ['--events', '-1'],
    ['--calendars', '1'], ['--height', '0'], ['--import-events', '0'],
])
def test_invalid_configuration_is_rejected_before_any_worker_is_started(options):
    with pytest.raises(SystemExit) as failure:
        main(options)
    assert failure.value.code == 2
