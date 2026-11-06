"""Testing workspaces isolate local data, sync state, and keyring identities."""
from datetime import datetime, timedelta, timezone

import pytest

from debacal.models import Event
from debacal.profiles import resolve_profile, validate_profile_name
from debacal.store import Store
from debacal.vdirsyncer_backend import VdirsyncerBackend


def test_default_profile_keeps_existing_location_and_testing_does_not_create_files(tmp_path):
    root = tmp_path / "debacal"
    normal = resolve_profile(data_root=root)
    testing = resolve_profile("testing", data_root=root)
    assert normal.name == "default" and normal.database == root / "calendar.sqlite3"
    assert normal.credential_namespace == ""
    assert testing.database == root / "profiles" / "testing" / "calendar.sqlite3"
    assert testing.credential_namespace and testing.label == "Testing"
    assert not root.exists()


@pytest.mark.parametrize("name", ["", ".", "..", "../default", "/default", "a/b", "a\\b", "a b",
                                 "with\nnewline", "ä", "x" * 65, "-testing", "_testing", None])
def test_profile_names_cannot_escape_their_directory(name, tmp_path):
    with pytest.raises(ValueError, match="profile name"):
        resolve_profile(name, data_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_profile_names_and_symlink_paths_resolve_to_one_workspace(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert validate_profile_name("Team-QA_2") == "team-qa_2"
    direct = resolve_profile("Testing", data_root=root)
    linked = resolve_profile("testing", data_root=alias)
    assert direct == linked
    normal = resolve_profile(data_root=root)
    assert resolve_profile(database=alias / "calendar.sqlite3", data_root=root) == normal


def test_custom_databases_and_named_profiles_keep_separate_credentials(tmp_path):
    normal = resolve_profile(data_root=tmp_path)
    testing = resolve_profile("testing", data_root=tmp_path)
    custom = resolve_profile(database=tmp_path / "other.sqlite3", data_root=tmp_path)
    # Explicitly using the same DB does not grant a named profile access to the
    # regular profile's saved account credentials.
    testing_override = resolve_profile("testing", normal.database, data_root=tmp_path)
    namespaces = {profile.credential_namespace for profile in (normal, testing, custom, testing_override)}
    assert len(namespaces) == 4
    assert custom == resolve_profile(database=tmp_path / "other.sqlite3", data_root=tmp_path)


def test_testing_profile_retains_its_own_data_and_sync_directory_across_restart(tmp_path):
    normal = resolve_profile(data_root=tmp_path)
    testing = resolve_profile("testing", data_root=tmp_path)
    start = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)
    with Store(normal.database) as real_store, Store(testing.database) as test_store:
        real_calendar = real_store.get_default_calendar()
        test_calendar = test_store.get_default_calendar()
        real_event = Event("same-id", real_calendar.id, "Productive event", start, start + timedelta(hours=1))
        test_event = Event("same-id", test_calendar.id, "Testing event", start, start + timedelta(hours=1))
        real_store.apply("Create real event", [real_event])
        assert test_store.list_events() == []
        test_store.apply("Create test event", [test_event])
        real_store.set_setting("workspace", "productive")
        test_store.set_setting("workspace", "testing")
        assert real_store.get_event("same-id") == real_event
        assert VdirsyncerBackend(real_store.path).root != VdirsyncerBackend(test_store.path).root
    with Store(testing.database) as restarted:
        assert restarted.list_events() == [test_event]
        assert restarted.get_setting("workspace") == "testing"
    with Store(normal.database) as restarted:
        assert restarted.list_events() == [real_event]
        assert restarted.get_setting("workspace") == "productive"
