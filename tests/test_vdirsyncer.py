"""Bridge integration tests with a CLI stand-in modeling vdirsyncer's actions.

The stand-in operates real mirrors/configuration/conflict command files and
models the upstream three-way algorithm; no test account credentials are used.
"""
from configparser import RawConfigParser
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import os
import subprocess
from subprocess import CompletedProcess
import sys

import pytest

from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.vdirsyncer_backend import VdirsyncerBackend
from debacal.vdirsyncer_hook import conflict as conflict_command


def sample(title="Original", id="event"):
    start = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
    return Event(id, "remote", title, start, start + timedelta(hours=1))


def test_inventory_quarantines_copied_native_ids_across_files_and_keeps_healthy_items(tmp_path):
    for uid in ('original', 'copy', 'healthy'):
        item = replace(sample(id='healthy' if uid == 'healthy' else 'copied-id'), ical_uid=uid)
        (tmp_path / (uid + '.ics')).write_bytes(export_ics([item]))
    report = VdirsyncerBackend._mirror_report(tmp_path, 'remote')
    assert set(report.items) == {'healthy'}
    assert {item.uid for item in report.rejected} == {'original', 'copy'}
    assert all('X-DEBACAL-ID' in item.reason for item in report.rejected)
    assert all('BEGIN:VCALENDAR' in item.raw_ics for item in report.rejected)


@pytest.mark.parametrize('duplicate', [False, True])
def test_oversized_inventory_resources_are_read_with_a_bounded_limit(tmp_path, monkeypatch, duplicate):
    from debacal import vdirsyncer_backend as bridge
    size = 256
    monkeypatch.setattr(bridge, 'MAX_IMPORT_BYTES', size)
    path = tmp_path / 'huge.ics'
    path.write_bytes(b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:oversized\r\nDESCRIPTION:' + b'x' * 4096)
    if duplicate:
        (tmp_path / 'duplicate.ics').write_bytes(path.read_bytes())
    report = VdirsyncerBackend._mirror_report(tmp_path, 'remote')
    assert not report.items
    assert all(item.uid == 'oversized' for item in report.rejected)
    assert all(len(item.raw_ics.encode()) == size + 1 for item in report.rejected)
    assert any('maximum size' in item.reason for item in report.rejected)


class FakeCLI:
    def __init__(self):
        self.remote = {}
        self.status = {}
        self.calls = []
        self.fail_pull = False
        self.fail_push = False
        self.before_push = None
        self.discovered = {}

    @staticmethod
    def hash(data):
        return hashlib.sha256(data.replace("\r\n", "\n").encode()).hexdigest() if data is not None else None

    def __call__(self, arguments, *, env, **kwargs):
        config_path = Path(arguments[arguments.index("--config") + 1])
        config = RawConfigParser()
        config.read(config_path)
        def value(section, key):
            return json.loads(config.get(section, key))
        pair = arguments[-1]
        assert "password =" not in config_path.read_text()
        assert "DEBACAL_VDIR_PASSWORD" in env
        assert kwargs.get("pass_fds")
        os.fstat(kwargs["pass_fds"][0])  # The subprocess retains the live calendar lock.
        assert not config.has_option("storage remote", "item_types"), "Use complete PROPFIND inventory, not server event filtering"
        assert not config.has_option("storage remote", "start_date")
        assert not config.has_option("storage remote", "end_date")
        if not value("storage remote", "read_only"):
            assert not config.has_option("pair push", "partial_sync"), "Upstream rejects partial_sync for two writable storages"
        command = arguments[arguments.index("--config") + 2]
        if command == "discover":
            assert "--no-list" in arguments
            self.discovered[config_path] = config_path.read_text()
            return CompletedProcess(arguments, 0, "", "")
        assert self.discovered.get(config_path) == config_path.read_text(), "Discover matching config before sync"
        self.calls.append((pair, tuple(arguments)))
        mirror = Path(value("storage local", "path"))
        mirror.mkdir(exist_ok=True)
        if (pair == "pull" and self.fail_pull) or (pair == "push" and self.fail_push):
            return CompletedProcess(arguments, 1, "", "Network is offline")
        if pair == "pull":
            assert value("storage remote", "read_only") is True
            self.write(mirror, self.remote)
            return CompletedProcess(arguments, 0, "", "")
        if value("storage remote", "read_only"):
            # First visit establishes the writable pair's status without
            # allowing any server mutation.
            self.write(mirror, self.remote)
            self.status = {uid: (self.hash(data), self.hash(data)) for uid, data in self.remote.items()}
            return CompletedProcess(arguments, 0, "", "")
        if self.before_push:
            callback, self.before_push = self.before_push, None
            callback()
        local = self.read(mirror)
        failed = False
        conflict_dir = Path(value("pair push", "conflict_resolution")[-1])
        for uid in set(local) | set(self.remote) | set(self.status):
            a, b = local.get(uid), self.remote.get(uid)
            old_a, old_b = self.status.get(uid, (None, None))
            changed_a, changed_b = self.hash(a) != old_a, self.hash(b) != old_b
            if a is not None and b is not None:
                if changed_a and changed_b and self.hash(a) != self.hash(b):
                    left, right = mirror.parent / "a.tmp", mirror.parent / "b.tmp"
                    left.write_text(a)
                    right.write_text(b)
                    if conflict_command(conflict_dir, left, right):
                        failed = True
                        continue
                    local[uid], self.remote[uid] = left.read_text(), right.read_text()
                elif changed_a and not changed_b:
                    self.remote[uid] = a
                elif changed_b:
                    local[uid] = b
            elif a is not None:
                if changed_a:
                    policy_file = env.get("DEBACAL_VDIR_WRITE_POLICY")
                    if policy_file:
                        import asyncio
                        from types import SimpleNamespace
                        from vdirsyncer.exceptions import PreconditionFailed
                        from debacal.vdirsyncer_runner import install_write_guard
                        class Remote:
                            async def upload(self, item):
                                return "new", "etag"
                            async def update(self, href, item, etag):
                                return "etag"
                        install_write_guard(json.loads(Path(policy_file).read_text()), Remote)
                        try:
                            asyncio.run(Remote().upload(SimpleNamespace(raw=a)))
                        except PreconditionFailed:
                            failed = True
                            continue
                    self.remote[uid] = a
                else:
                    local.pop(uid, None)
            elif b is not None:
                if changed_b:
                    local[uid] = b
                else:
                    self.remote.pop(uid, None)
            self.status[uid] = self.hash(local.get(uid)), self.hash(self.remote.get(uid))
        self.write(mirror, local)
        return CompletedProcess(arguments, int(failed), "", "Conflict" if failed else "")

    @staticmethod
    def ident(data):
        from debacal.vdirsyncer_backend import raw_uid
        if uid := raw_uid(data):
            return uid
        from debacal.ics_fallback import unfolded, text
        return next((text(line.partition(":")[2]) for line in unfolded(data) if line.startswith("UID:")), FakeCLI.hash(data))

    @staticmethod
    def write(directory, values):
        remaining = dict(values)
        for path in directory.glob("*.ics"):
            current = path.read_text()
            uid = FakeCLI.ident(current)
            if uid not in remaining:
                path.unlink()
                continue
            data = remaining.pop(uid)
            if current != data.replace("\r\n", "\n"):
                path.write_text(data)
        for uid, data in remaining.items():
            (directory / (hashlib.sha256(uid.encode()).hexdigest() + ".ics")).write_text(data)

    @staticmethod
    def read(directory):
        # Upstream vdirsyncer treats RRULE as opaque item text. It does not
        # validate recurrence semantics while identifying a resource by UID.
        return {FakeCLI.ident(path.read_text()): path.read_text() for path in directory.glob("*.ics")}


def setup(tmp_path):
    store = Store(tmp_path / "calendar.sqlite")
    calendar = Calendar("remote", "Remote", "#abcdef", kind="caldav", url="https://calendar.test/", username="test")
    store.save_calendar(calendar)
    cli = FakeCLI()
    backend = VdirsyncerBackend(store.path, runner=cli)
    return store, calendar, cli, backend


def sync(store, calendar, backend):
    return backend.sync(store, calendar, "session-secret")


@pytest.mark.parametrize('arrival', ['bootstrap', 'during-upload'])
def test_copied_native_id_cannot_replace_cached_uid_and_healthy_upload_continues(tmp_path, arrival):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        original = replace(sample(), ical_uid='original-uid')
        cli.remote[original.ical_uid] = export_ics([original]).decode()
        sync(store, calendar, backend)
        cached = store.get_event(original.id)
        copied = replace(original, ical_uid='copy-uid', title='Copied native identity')
        copied_raw = export_ics([copied]).decode()
        healthy = sample('Healthy local change', id='healthy')
        store.apply('Create healthy event', [healthy])

        def copy_on_server():
            cli.remote.pop(original.ical_uid)
            cli.remote[copied.ical_uid] = copied_raw

        if arrival == 'bootstrap':
            copy_on_server()
        else:
            cli.before_push = copy_on_server
        result = sync(store, calendar, backend)
        assert store.get_event(original.id) == cached
        assert store.get_event(healthy.id) == healthy
        assert not store.pending_changes()
        assert cli.remote[copied.ical_uid] == copied_raw
        assert healthy.ical_uid in cli.remote
        assert any('X-DEBACAL-ID' in issue.reason for issue in result.issues)
    finally:
        store.close()


def test_repaired_remote_resources_clear_stale_quarantine_errors(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        original = sample()
        healthy = export_ics([original]).decode()
        cli.remote[original.ical_uid] = healthy.replace('END:VEVENT', 'RRULE:FREQ=BOGUS\r\nEND:VEVENT')
        result = sync(store, calendar, backend)
        assert len(result.issues) == len(store.list_quarantined_resources(calendar.id)) == 1
        directory = backend.calendar_path(calendar.id) / 'quarantine'
        assert len(list(directory.glob('*.ics'))) == 1
        store.set_quarantined_resource(calendar.id, 'import-review', 'Unrelated import source', 'Review imported file')
        cli.remote[original.ical_uid] = healthy
        result = sync(store, calendar, backend)
        assert not result.issues
        assert [item['resource_id'] for item in store.list_quarantined_resources(calendar.id)] == ['import-review']
        assert not list(directory.glob('*.ics'))
        assert store.get_event(original.id).title == original.title
    finally:
        store.close()


def test_standalone_remote_duration_mode_change_conflicts_with_a_local_title_edit(tmp_path):
    from zoneinfo import ZoneInfo
    store, calendar, cli, backend = setup(tmp_path)
    try:
        start = datetime(2027, 1, 2, 9, tzinfo=ZoneInfo('Europe/Berlin'))
        nominal = replace(sample(), start=start, end=start + timedelta(days=1), duration_spec='P1D')
        cli.remote[nominal.ical_uid] = export_ics([nominal]).decode()
        sync(store, calendar, backend)
        source = store.get_event(nominal.id)
        store.apply('Local title edit', [replace(source, title='Local title')])
        exact = replace(source, duration_spec='')
        remote_data = export_ics([exact]).decode()
        cli.remote[nominal.ical_uid] = remote_data
        result = sync(store, calendar, backend)
        assert len(result.conflicts) == 1 and result.sent == 0
        assert cli.remote[nominal.ical_uid] == remote_data
        assert store.get_event(nominal.id).title == 'Local title'
        assert store.get_event(nominal.id).duration_spec == 'P1D'
        assert store.pending_changes()
    finally:
        store.close()


def test_installed_module_runs_despite_a_broken_console_launcher(tmp_path, monkeypatch):
    pytest.importorskip("vdirsyncer")
    broken = tmp_path / "vdirsyncer"
    broken.write_text('#!/nonexistent/moved-environment/bin/python\nraise SystemExit(99)\n')
    broken.chmod(0o755)
    monkeypatch.setattr('debacal.vdirsyncer_backend.shutil.which', lambda _name: str(broken))
    backend = VdirsyncerBackend(tmp_path / 'calendar.sqlite')
    command = backend._command()
    assert command == [sys.executable, '-m', 'vdirsyncer']
    result = subprocess.run([*command, '--version'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and 'version' in result.stdout


def test_module_command_prefix_does_not_leak_into_write_guard_arguments(tmp_path, monkeypatch):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        monkeypatch.setattr(backend, '_command', lambda: [sys.executable, '-m', 'vdirsyncer'])
        store.apply('Create', [sample()])
        result = sync(store, calendar, backend)
        assert result.sent == 1 and not store.pending_changes()
        baseline, write = [arguments for _pair, arguments in cli.calls]
        assert baseline[:3] == (sys.executable, '-m', 'vdirsyncer')
        assert write[0] == sys.executable and Path(write[1]).name == 'vdirsyncer_runner.py'
        assert write[2] == '--config' and '-m' not in write
    finally:
        store.close()


def test_explicit_broken_launcher_explains_missing_interpreter_and_keeps_changes(tmp_path):
    store, calendar, _cli, backend = setup(tmp_path)
    broken = tmp_path / 'external-vdirsyncer'
    broken.write_text('#!/nonexistent/moved-environment/bin/python\n')
    broken.chmod(0o755)
    backend = VdirsyncerBackend(store.path, executable=broken)
    try:
        store.apply('Create', [sample()])
        with pytest.raises(ValueError, match='launcher or its Python interpreter'):
            sync(store, calendar, backend)
        assert store.get_event('event') is not None
        assert len(store.pending_changes()) == 1
    finally:
        store.close()


def test_existing_remote_events_are_imported_before_any_local_write(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    cli.remote["event"] = export_ics([sample()]).decode()
    result = sync(store, calendar, backend)
    assert result.received == 1
    assert store.get_event("event").title == "Original"
    assert [kind for kind, _ in cli.calls] == ["push"]  # Read-only baseline downloads existing items.
    assert not store.pending_changes()
    config = (backend.calendar_path(calendar.id) / "push.conf").read_text()
    assert "session-secret" not in config
    assert "password.fetch" in config


def test_first_download_then_local_delete_uses_established_vdir_baseline(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    cli.remote["event"] = export_ics([sample()]).decode()
    sync(store, calendar, backend)
    store.apply("Delete downloaded event", delete_ids=["event"])
    result = sync(store, calendar, backend)
    assert not result.conflicts
    assert not cli.remote
    assert not store.pending_changes()


def test_local_create_edit_delete_and_undo_use_one_uid_resource(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create", [sample()])
    assert sync(store, calendar, backend).sent == 1
    assert not store.pending_changes()
    store.apply("Rename", [replace(sample(), title="Changed")])
    sync(store, calendar, backend)
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Changed"
    store.apply("Delete", delete_ids=["event"])
    sync(store, calendar, backend)
    assert not cli.remote
    assert not store.pending_changes()
    store.undo()
    sync(store, calendar, backend)
    assert len(cli.remote) == 1


def test_offline_outbox_survives_and_newer_edits_during_upload_are_not_acknowledged(tmp_path):
    import pytest
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create", [sample()])
    cli.fail_push = True
    with pytest.raises(ValueError, match="remain queued"):
        sync(store, calendar, backend)
    assert store.pending_changes()
    cli.fail_push = False
    cli.before_push = lambda: store.apply("Newest edit", [replace(sample(), title="Newest")])
    sync(store, calendar, backend)
    assert store.get_event("event").title == "Newest"
    assert store.pending_changes()[0]["payload"]["title"] == "Newest"
    sync(store, calendar, backend)
    assert not store.pending_changes()
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Newest"


def make_conflict(store, calendar, cli, backend, *, delete=False):
    store.apply("Create", [sample()])
    sync(store, calendar, backend)
    cli.remote["event"] = export_ics([replace(sample(), title="Server edit")]).decode()
    if delete:
        store.apply("Delete", delete_ids=["event"])
    else:
        store.apply("Local edit", [replace(sample(), title="Local edit")])
    return sync(store, calendar, backend).conflicts[0]


def test_conflict_is_durable_and_server_choice_is_atomic(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    conflict = make_conflict(store, calendar, cli, backend)
    assert conflict.local.title == "Local edit"
    assert conflict.remote.title == "Server edit"
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Server edit"
    restarted = VdirsyncerBackend(store.path, runner=cli)
    assert sync(store, calendar, restarted).conflicts[0].id == conflict.id
    restarted.set_decision(calendar.id, conflict.id, "remote")
    assert not sync(store, calendar, restarted).conflicts
    assert store.get_event("event").title == "Server edit"
    assert not store.pending_changes()


def test_local_conflict_choice_is_staged_after_review_and_resolved_by_vdir(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    conflict = make_conflict(store, calendar, cli, backend)
    backend.set_decision(calendar.id, conflict.id, "local")
    assert not sync(store, calendar, backend).conflicts
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Local edit"
    assert not store.pending_changes()


def test_delete_against_server_edit_requires_review_and_deletes_only_after_choice(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    conflict = make_conflict(store, calendar, cli, backend, delete=True)
    assert conflict.local is None
    assert "deleted locally" in conflict.reason
    backend.set_decision(calendar.id, conflict.id, "local")
    result = sync(store, calendar, backend)
    assert not result.conflicts
    assert not cli.remote
    assert not store.pending_changes()


def test_remote_delete_against_local_edit_requires_review(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create", [sample()])
    sync(store, calendar, backend)
    cli.remote.clear()
    store.apply("Local edit", [replace(sample(), title="Local edit")])
    conflict = sync(store, calendar, backend).conflicts[0]
    assert conflict.remote is None
    assert not cli.remote
    backend.set_decision(calendar.id, conflict.id, "remote")
    sync(store, calendar, backend)
    assert store.get_event("event") is None
    assert not store.pending_changes()


def test_recurrence_and_exceptions_remain_one_file_through_bridge(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    master = replace(sample(), recurrence_rule="FREQ=WEEKLY;COUNT=4", candidate=True, group_id="candidates")
    store.apply("Create series", [master])
    sync(store, calendar, backend)
    instances = store.query_occurrences(datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 15, tzinfo=timezone.utc))
    store.edit_occurrence(instances[1], replace(instances[1], title="Exception"))
    store.delete_occurrence(instances[2])
    sync(store, calendar, backend)
    remote = parse_ics(cli.remote["event"], "remote")[0]
    assert remote.recurrence_rule == master.recurrence_rule
    assert len(remote.recurrence_overrides) == 1
    assert len(remote.excluded_dates) == 1
    assert remote.candidate and remote.group_id == "candidates"
    assert len(cli.remote) == 1


def test_fresh_server_change_during_push_is_captured_by_durable_conflict_hook(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create", [sample()])
    sync(store, calendar, backend)
    store.apply("Local edit", [replace(sample(), title="Local edit")])
    cli.before_push = lambda: cli.remote.update(event=export_ics([replace(sample(), title="Racing server edit")]).decode())
    result = sync(store, calendar, backend)
    assert result.conflicts[0].remote.title == "Racing server edit"
    assert store.pending_changes()
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Racing server edit"


def test_server_delete_after_baseline_never_silently_recreates_local_edit(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create baseline", [sample(), sample(id="unrelated")])
    sync(store, calendar, backend)
    store.apply("Local edit", [replace(sample(), title="Local edited value")])
    cli.before_push = lambda: cli.remote.pop("event")
    result = sync(store, calendar, backend)
    assert len(result.conflicts) == 1
    assert result.conflicts[0].remote is None
    assert result.conflicts[0].local.title == "Local edited value"
    assert "event" not in cli.remote
    assert store.pending_changes()
    backend.set_decision(calendar.id, result.conflicts[0].id, "local")
    result = sync(store, calendar, backend)
    assert not result.conflicts
    assert parse_ics(cli.remote["event"], "remote")[0].title == "Local edited value"
    assert not store.pending_changes()


def test_server_local_same_value_and_distinct_value_conflict_matrix(tmp_path):
    cases = [
        ("server-only", None, "Server value", "Server value", False),
        ("local-only", "Local value", None, "Local value", False),
        ("same-result", "Same value", "Same value", "Same value", False),
        ("different-results", "Local value", "Server value", "Local value", True),
    ]
    for name, local_title, remote_title, expected_title, needs_review in cases:
        store, calendar, cli, backend = setup(tmp_path / name)
        store.apply("Create baseline", [sample()])
        sync(store, calendar, backend)
        if local_title:
            store.apply("Local mutation", [replace(sample(), title=local_title)])
        if remote_title:
            cli.remote["event"] = export_ics([replace(sample(), title=remote_title)]).decode()
        result = sync(store, calendar, backend)
        assert bool(result.conflicts) == needs_review, name
        assert store.get_event("event").title == expected_title, name
        assert bool(store.pending_changes()) == needs_review, name
        remote = parse_ics(cli.remote["event"], "remote")[0]
        assert remote.title == (remote_title if needs_review else expected_title), name


def test_installed_vdir_upload_guard_blocks_recreation_before_http_delegate(tmp_path, monkeypatch):
    import asyncio
    import pytest
    dav = pytest.importorskip("vdirsyncer.storage.dav")
    from vdirsyncer.vobject import Item
    from vdirsyncer.exceptions import PreconditionFailed
    from debacal.vdirsyncer_runner import install_write_guard
    calls = []
    async def delegate(self, item):
        calls.append(item)
        return "new", "etag"
    monkeypatch.setattr(dav.CalDAVStorage, "upload", delegate)
    monkeypatch.setattr(dav.CalDAVStorage, "update", dav.CalDAVStorage.update)
    install_write_guard({"expected_existing": {"event": "event"}, "conflict_directory": str(tmp_path)}, dav.CalDAVStorage)
    storage = object.__new__(dav.CalDAVStorage)
    with pytest.raises(PreconditionFailed):
        asyncio.run(storage.upload(Item(export_ics([sample()]).decode())))
    assert not calls
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert asyncio.run(storage.upload(Item(export_ics([sample(id="new")]).decode()))) == ("new", "etag")
    assert len(calls) == 1


def test_unrelated_remote_deletion_during_upload_is_not_resurrected(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create A", [sample()])
    sync(store, calendar, backend)
    cli.remote["event"] = export_ics([replace(sample(), title="Server edit before pull")]).decode()
    store.apply("Create unrelated B", [sample(id="other")])
    cli.before_push = lambda: cli.remote.pop("event")
    result = sync(store, calendar, backend)
    assert not result.conflicts
    assert "event" not in cli.remote
    assert "other" in cli.remote
    assert store.get_event("event") is None
    assert not store.pending_changes()


def test_relative_database_paths_produce_absolute_vdir_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store("relative.sqlite")
    calendar = Calendar("remote", "Remote", "#abcdef", kind="caldav", url="https://calendar.test/", username="test")
    store.save_calendar(calendar)
    backend = VdirsyncerBackend(store.path, runner=FakeCLI())
    sync(store, calendar, backend)
    assert backend.root.is_absolute()
    config = RawConfigParser()
    config.read(backend.calendar_path(calendar.id) / "push.conf")
    assert Path(json.loads(config.get("general", "status_path"))).is_absolute()
    assert Path(json.loads(config.get("storage local", "path"))).is_absolute()


def test_default_sync_engine_uses_vdir_and_reports_missing_backend_truthfully(tmp_path):
    from debacal.sync import SyncEngine
    store, calendar, cli, backend = setup(tmp_path)
    cli.remote["event"] = export_ics([sample()]).decode()
    engine = SyncEngine(store.path, vdir_backend=backend)
    engine.set_password(calendar.id, "session-secret")
    try:
        engine.sync_now()
        messages = []
        while not messages or messages[-1].kind != "finished":
            messages.append(engine.events.get(timeout=5))
        assert any(m.kind == "updated" and "vdirsyncer" in m.message for m in messages)
        assert store.get_event("event")
        cli.fail_push = True
        store.apply("Local edit", [replace(sample(), title="Queued")])
        engine.sync_now()
        messages = []
        while not messages or messages[-1].kind != "finished":
            messages.append(engine.events.get(timeout=5))
        assert any(m.kind == "error" for m in messages)
        assert "error" in messages[-1].message
        assert store.pending_changes()
    finally:
        engine.close()


def test_stale_displayed_conflict_cannot_authorize_unseen_server_version(tmp_path):
    import pytest
    from debacal.store import HistoryConflict
    from debacal.vdirsyncer_backend import semantic
    store, calendar, cli, backend = setup(tmp_path)
    displayed = make_conflict(store, calendar, cli, backend)
    cli.remote["event"] = export_ics([replace(sample(), title="Unseen server version")]).decode()
    current = sync(store, calendar, backend).conflicts[0]
    assert current.id == displayed.id
    with pytest.raises(HistoryConflict, match="comparison was open"):
        backend.set_decision(calendar.id, displayed.id, "remote",
                             expected_local=semantic(displayed.local), expected_remote=semantic(displayed.remote))
    assert store.get_event("event").title == "Local edit"
    assert store.pending_changes()


def test_saved_conflicts_restore_without_network_or_password_after_restart(tmp_path):
    from debacal.sync import SyncEngine
    store, calendar, cli, backend = setup(tmp_path)
    make_conflict(store, calendar, cli, backend)
    calls_before = len(cli.calls)
    engine = SyncEngine(store.path, vdir_backend=backend)
    try:
        message = engine.events.get(timeout=5)
        assert message.kind == "conflict"
        assert engine.conflicts[0].local.title == "Local edit"
        assert len(cli.calls) == calls_before
    finally:
        engine.close()


def malformed_resource(uid="broken"):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:" + uid +
            "\r\nSUMMARY:Private source text must not enter diagnostics\r\nDTSTART:20261110T090000Z\r\n"
            "DTEND:20261110T100000Z\r\nRRULE:FREQ=DAILY;UNTIL=not-a-date\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def candidate_event():
    return replace(sample(id="candidate"), candidate=True, group_id="alternatives",
                   recurrence_dates=("2026-11-12T09:00:00+00:00", "2026-11-16T09:00:00+00:00"),
                   schedule_expression="10.11., 12.11., 16.11.", expression_anchor="2026-11-10")


def test_bad_remote_recurrence_is_quarantined_and_candidate_upload_continues(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    cli.remote["broken"] = export_ics([sample(id="broken")]).decode()
    sync(store, calendar, backend)
    bad_data = malformed_resource()
    cli.remote["broken"] = bad_data
    store.apply("Create candidates", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1
    assert len(result.issues) == 1
    assert cli.remote["broken"] == bad_data
    assert store.get_event("broken") is not None  # A rejected source is never inferred deleted.
    returned = parse_ics(cli.remote["candidate"], "remote")[0]
    assert returned.candidate and returned.group_id == "alternatives"
    assert returned.schedule_expression == candidate_event().schedule_expression
    assert returned.expression_anchor == candidate_event().expression_anchor
    assert not store.pending_changes()
    assert store.list_quarantined_resources(calendar.id)
    assert all("Private source" not in issue.reason for issue in result.issues)
    working = backend.calendar_path(calendar.id) / "working"
    raw_files = [file.read_bytes() for file in working.glob("*.ics")]
    assert bad_data.encode() in raw_files  # The rejected file was never rewritten.


def test_bad_remote_cannot_be_overwritten_by_queued_change_but_other_uid_uploads(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create", [sample(id="broken")])
    sync(store, calendar, backend)
    store.apply("Edit protected event", [replace(sample(id="broken"), title="Queued local edit")])
    cli.remote["broken"] = malformed_resource()
    store.apply("Create candidates", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1
    assert cli.remote["broken"] == malformed_resource()
    assert "candidate" in cli.remote
    assert [row["event_id"] for row in store.pending_changes()] == ["broken"]
    assert result.issues


def test_invalid_pending_resource_does_not_block_healthy_pending_uid(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    store.apply("Create old pending event", [sample(id="legacy")])
    row = store.pending_changes()[0]
    payload = dict(row["payload"], recurrence_rule="FREQ=INVALID")
    with store.connection:
        store.connection.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    store.apply("Create candidates", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1
    assert "candidate" in cli.remote and "legacy" not in cli.remote
    assert [row["event_id"] for row in store.pending_changes()] == ["legacy"]
    assert result.issues


def test_unresolved_other_uid_conflict_does_not_block_candidate_upload(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    reviewed = make_conflict(store, calendar, cli, backend)
    server_before = cli.remote["event"]
    store.apply("Create candidates despite other conflict", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1
    assert len(result.conflicts) == 1 and result.conflicts[0].id == reviewed.id
    assert cli.remote["event"] == server_before
    assert parse_ics(cli.remote["candidate"], "remote")[0].candidate
    assert [row["event_id"] for row in store.pending_changes()] == ["event"]


def test_anonymous_caldav_does_not_request_a_missing_password(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    anonymous = replace(calendar, id="public", username="")
    store.save_calendar(anonymous)
    backend.sync(store, anonymous, "")
    config = (backend.calendar_path(anonymous.id) / "push.conf").read_text()
    assert "password.fetch" not in config
    assert "username =" not in config


def test_complete_inventory_ignores_and_preserves_tasks_while_events_sync(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    task = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:task\r\nSUMMARY:Opaque task\r\n"
            "STATUS:NEEDS-ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
    cli.remote["task"] = task
    cli.remote["event"] = export_ics([sample()]).decode()
    result = sync(store, calendar, backend)
    assert result.received == 1 and not result.issues
    assert not store.list_quarantined_resources(calendar.id)
    store.apply("Create candidate", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1 and not result.issues
    assert cli.remote["task"] == task
    assert task.encode() in [p.read_bytes() for p in (backend.calendar_path(calendar.id) / "working").glob("*.ics")]
    config = RawConfigParser()
    config.read(backend.calendar_path(calendar.id) / "push.conf")
    assert isinstance(json.loads(config.get("pair push", "conflict_resolution")), list)


def test_installed_vdir_default_inventory_dispatches_to_propfind(monkeypatch):
    import asyncio
    import pytest
    dav = pytest.importorskip("vdirsyncer.storage.dav")
    calls = []
    async def propfind_list(self):
        calls.append("PROPFIND")
        yield "all-resources", "etag"
    monkeypatch.setattr(dav.DAVStorage, "list", propfind_list)
    storage = object.__new__(dav.CalDAVStorage)
    storage.item_types = ()
    storage.start_date = storage.end_date = None
    from types import SimpleNamespace
    storage.session = SimpleNamespace(get_default_headers=lambda: {})
    # A REPORT call would need a session and fail this test; the installed
    # implementation must delegate its unfiltered listing to DAVStorage.
    async def collect():
        return [item async for item in storage.list()]
    assert asyncio.run(collect()) == [("all-resources", "etag")]
    assert calls == ["PROPFIND"]


def test_unclosed_remote_component_is_never_inferred_deleted_and_other_uid_sends(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    cli.remote["broken"] = export_ics([sample(id="broken")]).decode()
    sync(store, calendar, backend)
    truncated = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:broken\nDTSTART:20261110T090000Z\n"
                 "DTEND:20261110T100000Z\nBEGIN:VALARM\nACTION:DISPLAY\nEND:VCALENDAR\n")
    cli.remote["broken"] = truncated
    store.apply("Create healthy candidates", [candidate_event()])
    result = sync(store, calendar, backend)
    assert result.sent == 1 and result.issues
    assert store.get_event("broken") is not None
    assert cli.remote["broken"] == truncated
    assert parse_ics(cli.remote["candidate"], "remote")[0].candidate


def test_large_calendar_import_query_and_candidate_bridge(tmp_path):
    from debacal.ics import parse_ics_report
    # A self-contained bulk import must keep healthy resources queryable and
    # allow an unrelated queued UID to upload. No local calendar export is used.
    examples = []
    for index in range(1442):
        item = sample(id=f'bulk-{index}', title=f'Sample appointment {index}')
        offset = timedelta(days=index % 45)
        examples.append(replace(item, start=item.start + offset, end=item.end + offset,
                                recurrence_rule='FREQ=WEEKLY;COUNT=4' if index % 60 == 0 else ''))
    report = parse_ics_report(export_ics(examples), "remote")
    assert len(report.events) == 1442
    assert not report.issues
    store, calendar, cli, backend = setup(tmp_path)
    store.apply_remote(upserts=report.events)
    visible = store.query_occurrences(datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert len(visible) > 0
    cli.remote = {event.ical_uid: event.raw_ics for event in report.events}
    store.apply("Create candidates beside imported resources", [candidate_event()])
    result = sync(store, calendar, backend)
    assert not result.issues
    assert result.sent == 1
    assert not store.pending_changes()
    assert parse_ics(cli.remote["candidate"], "remote")[0].candidate


def test_zero_duration_server_series_does_not_block_pull_or_upload(tmp_path):
    store, calendar, cli, backend = setup(tmp_path)
    try:
        # Synthetic shape matching the reported server event: explicit equal
        # endpoints on a recurring timed master, alongside an ordinary event.
        cli.remote['point'] = ('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n'
            'UID:point\nSUMMARY:Point\nDTSTART:20261110T090000Z\n'
            'DTEND:20261110T090000Z\nRRULE:FREQ=WEEKLY;COUNT=2\n'
            'END:VEVENT\nEND:VCALENDAR\n')
        cli.remote['event'] = export_ics([sample()]).decode()
        result = sync(store, calendar, backend)
        assert not result.conflicts
        point = next(event for event in store.list_events(calendar_ids=[calendar.id]) if event.ical_uid == 'point')
        assert point.start == point.end
        assert len(store.query_occurrences(point.start, point.start + timedelta(days=8), [calendar.id])) == 3
        assert 'DTEND:20261110T090000Z' in cli.remote['point']  # A pull never changes the server resource.
        store.apply('Create unrelated local event', [sample('Local addition', 'local-addition')])
        result = sync(store, calendar, backend)
        assert result.sent == 1
        assert not result.conflicts and not store.pending_changes(calendar.id)
        assert 'local-addition' in cli.remote and 'point' in cli.remote
        # Editing the point emits the canonical zero-duration representation.
        store.apply('Rename point', [replace(point, title='Renamed point')])
        result = sync(store, calendar, backend)
        assert result.sent == 1 and not result.conflicts
        returned = parse_ics(cli.remote['point'], calendar.id)[0]
        assert returned.start == returned.end == point.start
        assert returned.recurrence_rule == point.recurrence_rule
        assert 'DTEND' not in cli.remote['point']
    finally:
        store.close()
