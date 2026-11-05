"""Conflict decisions require reviewed versions, including the engine/UI boundary."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from lebacal.credentials import CredentialStore
from lebacal.ics import export_ics, parse_ics
from lebacal.models import Calendar, Event
from lebacal.store import HistoryConflict, Store
from lebacal.sync import SyncEngine
from lebacal.vdirsyncer_backend import BridgeConflict, BridgeResult, VdirsyncerBackend, _digest, semantic
from lebacal.vdirsyncer_hook import conflict


def event(title="Local version"):
    start = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
    return Event("event", "remote", title, start, start + timedelta(hours=1))


def hook_files(tmp_path):
    directory = tmp_path / "conflicts"
    left, right = tmp_path / "left.ics", tmp_path / "right.ics"
    left.write_bytes(export_ics([event()]))
    right.write_bytes(export_ics([event("Server version")]))
    return directory, left, right, directory / (_digest("event") + ".json")


def approve(marker_path, choice):
    marker = json.loads(marker_path.read_text())
    marker["choice"] = choice
    marker_path.write_text(json.dumps(marker))


def test_hook_without_decision_preserves_both_versions_and_creates_review(tmp_path):
    directory, left, right, marker_path = hook_files(tmp_path)
    before = left.read_bytes(), right.read_bytes()
    assert conflict(directory, left, right) == 1
    assert (left.read_bytes(), right.read_bytes()) == before
    marker = json.loads(marker_path.read_text())
    assert marker["local_hash"] == semantic(event())
    assert marker["remote_hash"] == semantic(event("Server version"))
    assert "choice" not in marker


@pytest.mark.parametrize("choice,title", [("local", "Local version"), ("remote", "Server version")])
def test_hook_applies_only_explicit_reviewed_choice(tmp_path, choice, title):
    directory, left, right, marker_path = hook_files(tmp_path)
    assert conflict(directory, left, right) == 1
    approve(marker_path, choice)
    assert conflict(directory, left, right) == 0
    assert left.read_bytes() == right.read_bytes()
    retained = parse_ics(left.read_bytes(), "remote")[0]
    assert retained.title == title
    assert retained.ical_uid == event().ical_uid


def test_hook_rejects_old_approval_after_server_changes_again(tmp_path):
    directory, left, right, marker_path = hook_files(tmp_path)
    conflict(directory, left, right)
    approve(marker_path, "local")
    right.write_bytes(export_ics([event("New unseen server version")]))
    before = left.read_bytes(), right.read_bytes()
    assert conflict(directory, left, right) == 1
    assert (left.read_bytes(), right.read_bytes()) == before
    marker = json.loads(marker_path.read_text())
    assert "choice" not in marker
    assert marker["remote_hash"] == semantic(event("New unseen server version"))


def test_reviewed_local_choice_preserves_new_server_only_metadata(tmp_path):
    directory, left, right, marker_path = hook_files(tmp_path)
    conflict(directory, left, right)
    approve(marker_path, "local")
    raw = right.read_text().replace("END:VEVENT", "X-SERVER-METADATA:retain-this\nEND:VEVENT")
    right.write_text(raw)
    assert conflict(directory, left, right) == 0
    assert b"X-SERVER-METADATA:retain-this" in right.read_bytes()
    assert parse_ics(right.read_bytes(), "remote")[0].title == "Local version"


def test_rule_default_normalization_does_not_hide_real_conflicting_changes():
    master = replace(event(), recurrence_rule="FREQ=WEEKLY;COUNT=3")
    equivalent = replace(master, recurrence_rule="COUNT=3;BYDAY=TH;INTERVAL=1;WKST=MO;FREQ=WEEKLY")
    assert semantic(master) == semantic(equivalent)
    assert semantic(master) != semantic(replace(equivalent, title="Different server title"))
    assert semantic(master) != semantic(replace(equivalent, candidate=True))
    assert semantic(master) != semantic(replace(equivalent, start=master.start + timedelta(hours=1), end=master.end + timedelta(hours=1)))
    assert semantic(master) != semantic(None)


def test_bootstrap_is_read_only_and_writable_pass_requires_command_decisions(tmp_path):
    from configparser import RawConfigParser
    calendar = Calendar("remote", "Remote", "#123456", kind="caldav", url="https://calendar.example/")
    backend = VdirsyncerBackend(tmp_path / "calendar.sqlite")
    root = backend.calendar_path(calendar.id)
    readonly = RawConfigParser()
    readonly.read(backend._config(calendar, root, pull=False, bootstrap=True))
    assert json.loads(readonly.get("storage remote", "read_only")) is True
    writable = RawConfigParser()
    writable.read(backend._config(calendar, root, pull=False))
    assert json.loads(writable.get("storage remote", "read_only")) is False
    policy = json.loads(writable.get("pair push", "conflict_resolution"))
    assert policy[0] == "command"
    assert "vdirsyncer_hook.py" in policy[2]


class NoKeyring:
    def get_password(self, *_):
        return None


class ReviewBridge:
    def __init__(self):
        self.local = event()
        self.remote = event("Server version")
        self.decisions = []
        self.choice = None
        self.resolved = False

    def sync(self, store, calendar, password):
        if self.choice == "remote":
            pending = [row["id"] for row in store.pending_changes(calendar.id) if row["event_id"] == self.local.id]
            store.accept_remote(calendar.id, self.local.id, self.local, pending, upserts=[self.remote])
            self.resolved = True
            self.choice = None
        conflicts = () if self.resolved else (BridgeConflict("review", self.local.id, self.local, self.remote, "Both versions changed."),)
        return BridgeResult(0, 0, conflicts)

    def set_decision(self, calendar_id, conflict_id, choice, *, expected_local, expected_remote):
        if expected_local != semantic(self.local) or expected_remote != semantic(self.remote):
            raise HistoryConflict("Displayed versions changed.")
        self.decisions.append((choice, expected_local, expected_remote))
        self.choice = choice


def synchronize(engine):
    engine.sync_now()
    messages = []
    while True:
        item = engine.events.get(timeout=5)
        messages.append(item)
        if item.kind == "finished":
            return messages


def test_engine_passes_only_current_displayed_conflict_to_backend(tmp_path):
    store = Store(tmp_path / "calendar.sqlite")
    store.save_calendar(Calendar("remote", "Remote", "#123456", kind="caldav", url="https://calendar.example/"))
    store.apply("Local edit", [event()])
    backend = ReviewBridge()
    engine = SyncEngine(store.path, vdir_backend=backend, credential_store=CredentialStore(NoKeyring()))
    try:
        synchronize(engine)
        displayed = engine.conflicts[0]
        backend.remote = event("New unseen server version")
        synchronize(engine)
        assert engine.conflicts[0] != displayed
        engine.resolve_conflict(displayed.id, "remote", expected=displayed)
        messages = synchronize(engine)
        assert backend.decisions == []
        assert store.get_event("event").title == "Local version"
        assert store.pending_changes()
        assert any(item.kind == "conflict" and "changed" in item.message for item in messages)
        reviewed = engine.conflicts[0]
        engine.resolve_conflict(reviewed.id, "remote", expected=reviewed)
        synchronize(engine)
        assert len(backend.decisions) == 1
        assert backend.decisions[0] == ("remote", semantic(backend.local), semantic(backend.remote))
        assert store.get_event("event").title == "New unseen server version"
        assert not store.pending_changes()
        assert not engine.conflicts
    finally:
        engine.close()
        engine._thread.join(timeout=2)
        store.close()


@pytest.mark.parametrize("choice", ["local", "remote"])
def test_installed_vdir_cli_guards_delete_race_and_honors_fresh_review(tmp_path, monkeypatch, choice):
    """Use the installed CLI/algorithm, real files and its real status SQLite.

    Only the network storage is replaced by another maintained filesystem
    storage. The CLI, discovery, sync actions, ETags and rollback are unmocked.
    A separate test targets the installed CalDAV upload method directly.
    """
    cli = pytest.importorskip("vdirsyncer.cli")
    from vdirsyncer.cli.utils import storage_names
    from vdirsyncer.storage.filesystem import FilesystemStorage
    from lebacal.vdirsyncer_runner import install_write_guard

    class RemoteFilesystem(FilesystemStorage):
        def __init__(self, url, **kwargs):
            super().__init__(path=url, fileext=".ics", **kwargs)

    monkeypatch.setitem(storage_names._storages, "caldav", RemoteFilesystem)
    local, remote = tmp_path / "local", tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    config = tmp_path / "audit.conf"
    status = tmp_path / "status"
    status.mkdir()
    policy = {"expected_existing": {}, "conflict_directory": str(tmp_path / "conflicts")}
    install_write_guard(policy, RemoteFilesystem)

    def configure(*, readonly=False):
        q = json.dumps
        config.write_text(
            f'[general]\nstatus_path={q(str(status))}\n[pair audit]\na="local"\nb="remote"\ncollections=null\n'
            f'[storage local]\ntype="filesystem"\npath={q(str(local))}\nfileext=".ics"\n'
            f'[storage remote]\ntype="caldav"\nurl={q(str(remote))}\nread_only={q(readonly)}\n')

    def command(*arguments):
        cli.app.main(["--config", str(config), *arguments], standalone_mode=False)

    def resources(directory):
        return {parse_ics(file.read_bytes(), "remote")[0].ical_uid: file for file in directory.glob("*.ics")}

    base = event("Shared base")
    unrelated = replace(base, id="unrelated", ical_uid="unrelated")
    (local / "event.ics").write_bytes(export_ics([base]))
    (local / "unrelated.ics").write_bytes(export_ics([unrelated]))
    configure()
    command("discover", "--no-list", "audit")
    command("sync", "audit")
    assert set(resources(remote)) == {"event", "unrelated"}

    policy["expected_existing"][base.ical_uid] = base.id
    local_edit = replace(base, title="Queued local edit")
    (local / "event.ics").write_bytes(export_ics([local_edit]))
    resources(remote)[base.ical_uid].unlink()
    healthy = replace(base, id="healthy", ical_uid="healthy", candidate=True,
                      recurrence_dates=("2026-11-07T10:00:00+00:00",))
    (local / "healthy.ics").write_bytes(export_ics([healthy]))
    with pytest.raises(SystemExit) as failed:
        command("sync", "audit")
    assert failed.value.code == 1
    assert set(resources(remote)) == {"unrelated", "healthy"}
    marker = json.loads((tmp_path / "conflicts" / (_digest(base.ical_uid) + ".json")).read_text())
    assert marker["remote_ics"] is None
    assert marker["local_hash"] == semantic(local_edit)
    assert parse_ics(resources(remote)["healthy"].read_bytes(), "remote")[0].candidate

    # A reviewed choice starts from another read-only baseline, matching the
    # application's next cycle. Existing uploads are never silently recreated.
    for file in local.glob("*.ics"):
        file.unlink()
    status = tmp_path / "reviewed-status"
    status.mkdir()
    configure(readonly=True)
    command("discover", "--no-list", "audit")
    command("sync", "audit")
    assert base.ical_uid not in resources(local)
    policy["expected_existing"].clear()
    if choice == "local":
        (local / "event.ics").write_bytes(export_ics([local_edit]))
    configure()
    command("discover", "--no-list", "audit")
    command("sync", "audit")
    if choice == "local":
        assert parse_ics(resources(remote)[base.ical_uid].read_bytes(), "remote")[0].title == local_edit.title
    else:
        assert base.ical_uid not in resources(remote)
    assert {"unrelated", "healthy"} <= set(resources(remote))
