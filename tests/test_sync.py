from dataclasses import replace
from datetime import datetime, timedelta, timezone
from queue import Empty

from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.sync import PreconditionFailed, RemoteResource, SyncEngine


class FakeTransport:
    def __init__(self):
        self.resources = {}
        self.calls = []
        self.fail = False
        self.counter = 0

    def list_resources(self):
        return list(self.resources.values())

    def new_href(self, event_id):
        return "https://calendar.test/" + event_id + ".ics"

    def get(self, href):
        return self.resources.get(href)

    def put(self, href, data, *, etag=None):
        self.calls.append(("put", href, etag))
        if self.fail:
            raise OSError("Offline")
        existing = self.resources.get(href)
        if (existing and existing.etag != etag) or (not existing and etag):
            raise PreconditionFailed()
        self.counter += 1
        item = RemoteResource(href, f'"{self.counter}"', data.decode() if isinstance(data, bytes) else data)
        self.resources[href] = item
        return item

    def delete(self, href, *, etag):
        self.calls.append(("delete", href, etag))
        if self.resources[href].etag != etag:
            raise PreconditionFailed()
        del self.resources[href]

    def close(self):
        pass


def event(title="Test"):
    start = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
    return Event("e1", "remote", title, start, start + timedelta(hours=1))


def setup(tmp_path):
    path = tmp_path / "calendar.sqlite"
    store = Store(path)
    store.save_calendar(Calendar("remote", "Remote", "#abcdef", kind="caldav", url="https://calendar.test/"))
    transport = FakeTransport()
    engine = SyncEngine(path, transport_factory=lambda *_: transport)
    return store, transport, engine


def synchronize(engine):
    engine.sync_now()
    messages = []
    while True:
        message = engine.events.get(timeout=5)
        messages.append(message)
        if message.kind == "finished":
            return messages


def test_upload_delete_undo(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        assert not [m for m in synchronize(engine) if m.kind == "error"]
        assert transport.calls[0][2] is None  # conditional creation
        assert not store.pending_changes()
        store.apply("Delete", delete_ids=["e1"])
        synchronize(engine)
        assert not transport.resources
        assert transport.calls[-1][0] == "delete"
        assert transport.calls[-1][2]  # versioned deletion
        store.undo()
        synchronize(engine)
        assert len(transport.resources) == 1
    finally:
        engine.close()
        store.close()


def test_alternative_transport_uploads_invitation_without_scheduling_envelope(tmp_path):
    store, transport, engine = setup(tmp_path)
    raw = export_ics([event()]).decode().replace('VERSION:2.0', 'VERSION:2.0\r\nMETHOD:PUBLISH')
    imported, = parse_ics(raw, 'remote')
    try:
        store.apply('Import invitation', [imported])
        assert not [message for message in synchronize(engine) if message.kind == 'error']
        remote, = transport.resources.values()
        assert 'METHOD:' not in remote.data
        assert not store.pending_changes()
        assert 'METHOD:PUBLISH' in export_ics([store.get_event(imported.id)]).decode()
    finally:
        engine.close()
        store.close()


def test_offline_keeps_durable_outbox(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        transport.fail = True
        assert any(m.kind == "error" for m in synchronize(engine))
        assert store.get_event("e1")
        assert len(store.pending_changes()) == 1
        transport.fail = False
        synchronize(engine)
        assert not store.pending_changes()
    finally:
        engine.close()
        store.close()


def test_conflict_never_overwrites_and_remote_resolution(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        synchronize(engine)
        href = transport.new_href("e1")
        server = replace(event(), title="Server edit")
        transport.resources[href] = RemoteResource(href, '"server-edit"', export_ics([server]).decode())
        store.apply("Edit", [replace(event(), title="Local edit")])
        before = len(transport.calls)
        synchronize(engine)
        assert len(transport.calls) == before
        conflict = engine.conflicts[0]
        assert conflict.local.title == "Local edit"
        assert conflict.remote.title == "Server edit"
        engine.resolve_conflict(conflict.id, "remote")
        # The queued sync starts after resolution, providing a deterministic barrier.
        synchronize(engine)
        assert not engine.conflicts
        assert store.get_event("e1").title == "Server edit"
        assert not store.pending_changes()
    finally:
        engine.close()
        store.close()


def test_remote_edit_preserves_pending_newer_local_revision(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        synchronize(engine)
        original_put = transport.put
        def racing_put(*args, **kwargs):
            store.apply("Edit during upload", [replace(event(), title="Newest")])
            return original_put(*args, **kwargs)
        transport.put = racing_put
        store.apply("Edit", [replace(event(), title="Older")])
        synchronize(engine)
        assert store.get_event("e1").title == "Newest"
        assert store.pending_changes()[0]["payload"]["title"] == "Newest"
    finally:
        engine.close()
        store.close()


def test_ics_subscription_refresh_and_failed_refresh_preserve_cache(tmp_path, monkeypatch):
    import io
    path = tmp_path / "subscription.sqlite"
    store = Store(path)
    store.save_calendar(Calendar("feed", "Feed", "#abcdef", kind="ics", url="https://calendar.test/events.ics"))
    body = export_ics([replace(event(), id="feed-event", calendar_id="feed")])
    monkeypatch.setattr("debacal.sync.urlopen", lambda *a, **k: io.BytesIO(body))
    engine = SyncEngine(path)
    try:
        assert not any(m.kind == "error" for m in synchronize(engine))
        assert store.get_event("feed-event")
        body = b"malformed feed"
        assert any(m.kind == "error" for m in synchronize(engine))
        assert store.get_event("feed-event")
        body = export_ics([])
        synchronize(engine)
        assert not store.get_event("feed-event")
    finally:
        engine.close()
        store.close()


def test_subscription_copied_id_cannot_overwrite_or_delete_a_cached_different_uid(tmp_path, monkeypatch):
    import io
    path = tmp_path / 'subscription.sqlite'
    with Store(path) as store:
        store.save_calendar(Calendar('feed', 'Feed', '#abcdef', kind='ics', url='https://calendar.test/events.ics'))
        original = replace(event(), calendar_id='feed', id='stored-id', ical_uid='original-uid')
        store.apply_remote([original])
        copied = replace(original, ical_uid='copied-uid', title='Other client copied native metadata')
        healthy = replace(original, id='healthy-id', ical_uid='healthy-uid', title='Healthy')
        body = export_ics([copied, healthy])
        monkeypatch.setattr('debacal.sync.urlopen', lambda *a, **k: io.BytesIO(body))
        engine = SyncEngine(path)
        try:
            messages = synchronize(engine)
            assert store.get_event(original.id) == original
            assert store.get_event(healthy.id).title == 'Healthy'
            assert {item.ical_uid for item in store.list_events()} == {'original-uid', 'healthy-uid'}
            assert any(item.kind == 'issue' and 'X-DEBACAL-ID' in item.message for item in messages)
            assert len(store.list_quarantined_resources('feed')) == 1
        finally:
            engine.close()


def test_caldav_adapter_uses_conditional_http_headers(monkeypatch):
    import sys
    from types import SimpleNamespace
    from debacal.sync import CalDAVTransport
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def request(self, href, method="GET", body="", headers=None):
            calls.append((method, headers))
            if method == "GET":
                return SimpleNamespace(status=200, headers={"ETag": '"new"'}, raw=export_ics([event()]))
            return SimpleNamespace(status=204)
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, "caldav", SimpleNamespace(DAVClient=Client, Calendar=lambda **kwargs: None))
    adapter = CalDAVTransport(Calendar("remote", "Remote", "#123456", kind="caldav", url="https://calendar.test/"), "")
    saved = adapter.put("https://calendar.test/e1.ics", export_ics([event()]))
    assert calls[0][1]["If-None-Match"] == "*"
    assert isinstance(saved.data, str)
    adapter.put(saved.href, export_ics([event()]), etag=saved.etag)
    assert calls[2][1]["If-Match"] == '"new"'
    adapter.delete(saved.href, etag=saved.etag)
    assert calls[-1] == ("DELETE", {"If-Match": '"new"'})
    import pytest
    with pytest.raises(ValueError, match="strong ETag"):
        adapter.delete(saved.href, etag="")


def test_external_client_stripping_extensions_preserves_local_identity(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        synchronize(engine)
        href = transport.new_href("e1")
        data = export_ics([replace(event(), title="External edit")]).decode()
        data = "\r\n".join(line for line in data.splitlines() if not line.startswith("X-DEBACAL-"))
        transport.resources[href] = RemoteResource(href, '"external"', data)
        synchronize(engine)
        assert [e.id for e in store.list_events(calendar_ids=["remote"])] == ["e1"]
        assert store.get_event("e1").title == "External edit"
        store.apply("Edit", [replace(store.get_event("e1"), title="Our edit")])
        synchronize(engine)
        assert not engine.conflicts
        assert transport.calls[-1][2] == '"external"'
    finally:
        engine.close()
        store.close()


def make_conflict(store, transport, engine, *, delete=False):
    store.apply("Create", [event()])
    synchronize(engine)
    href = transport.new_href("e1")
    transport.resources[href] = RemoteResource(href, '"external"', export_ics([replace(event(), title="Server")]).decode())
    if delete:
        store.apply("Delete", delete_ids=["e1"])
    else:
        store.apply("Edit", [replace(event(), title="Local")])
    synchronize(engine)
    return href, engine.conflicts[0]


def test_resolving_local_deletion_clears_sync_state(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        href, conflict = make_conflict(store, transport, engine, delete=True)
        engine.resolve_conflict(conflict.id, "local")
        synchronize(engine)
        assert not transport.resources
        assert not store.get_sync_state("remote", "e1")
        assert not engine.conflicts
        store.undo()
        synchronize(engine)
        assert store.get_event("e1")
        assert len(transport.resources) == 1
        assert not engine.conflicts
    finally:
        engine.close()
        store.close()


def test_changed_conflict_requires_review_before_discard(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        _, conflict = make_conflict(store, transport, engine)
        store.apply("Newest edit", [replace(event(), title="Newest")])
        engine.resolve_conflict(conflict.id, "remote")
        synchronize(engine)
        assert store.get_event("e1").title == "Newest"
        assert store.pending_changes()
        refreshed = engine.conflicts[0]
        assert refreshed.local.title == "Newest"
        engine.resolve_conflict(refreshed.id, "remote")
        synchronize(engine)
        assert store.get_event("e1").title == "Server"
        assert not store.pending_changes()
    finally:
        engine.close()
        store.close()


def test_recurring_resources_stay_one_editable_master_and_one_remote_resource(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        href = transport.new_href("series")
        raw = export_ics([event()]).decode().replace("END:VEVENT", "RRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT")
        transport.resources[href] = RemoteResource(href, '"recurring"', raw)
        synchronize(engine)
        events = store.list_events(calendar_ids=["remote"])
        assert len(events) == 1
        assert events[0].recurrence_rule == "FREQ=WEEKLY;COUNT=3"
        assert not events[0].read_only
        assert len(store.list_sync_state("remote")) == 1
        occurrences = store.query_occurrences(datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc))
        assert len(occurrences) == 3
        store.apply("Rename series", [replace(events[0], title="Renamed series")])
        synchronize(engine)
        assert len(transport.resources) == 1
        assert "RRULE:FREQ=WEEKLY;COUNT=3" in transport.resources[href].data
        assert parse_ics(transport.resources[href].data, "remote")[0].title == "Renamed series"
        del transport.resources[href]
        synchronize(engine)
        assert not store.list_events(calendar_ids=["remote"])
        assert not store.list_sync_state("remote")
        assert len(transport.calls) == 1
    finally:
        engine.close()
        store.close()


def test_server_converting_single_event_to_series_requires_explicit_resource_conflict_resolution(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        store.apply("Create", [event()])
        synchronize(engine)
        store.apply("Delete", delete_ids=["e1"])
        href = transport.new_href("e1")
        raw = export_ics([event()]).decode().replace("END:VEVENT", "RRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT")
        transport.resources[href] = RemoteResource(href, '"recurring"', raw)
        synchronize(engine)
        conflict = engine.conflicts[0]
        assert len(conflict.remote_events) == 1
        assert conflict.remote.recurring
        assert transport.resources[href]
        assert all(call[0] != "delete" for call in transport.calls)
        engine.resolve_conflict(conflict.id, "remote")
        synchronize(engine)
        assert not engine.conflicts
        assert not store.pending_changes()
        assert len(store.list_events(calendar_ids=["remote"])) == 1
        assert store.get_event("e1").recurring
    finally:
        engine.close()
        store.close()


def test_single_occurrence_edit_delete_and_undo_sync_one_series_resource(tmp_path):
    store, transport, engine = setup(tmp_path)
    try:
        master = replace(event(), recurrence_rule="FREQ=WEEKLY;COUNT=4", timezone_id="UTC")
        store.apply("Create recurring series", [master])
        synchronize(engine)
        start, end = datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 12, 15, tzinfo=timezone.utc)
        instances = store.query_occurrences(start, end)
        assert len(instances) == 4
        store.edit_occurrence(instances[1], replace(instances[1], title="Just this occurrence", start=instances[1].start + timedelta(hours=2), end=instances[1].end + timedelta(hours=2)))
        synchronize(engine)
        assert len(transport.resources) == 1
        resource = next(iter(transport.resources.values()))
        server_master = parse_ics(resource.data, "remote")[0]
        assert server_master.recurrence_rule == "FREQ=WEEKLY;COUNT=4"
        assert len(server_master.recurrence_overrides) == 1
        assert resource.data.count("RECURRENCE-ID:") == 1
        store.delete_occurrence(instances[2])
        synchronize(engine)
        server_master = parse_ics(next(iter(transport.resources.values())).data, "remote")[0]
        assert len(server_master.excluded_dates) == 1
        assert len(transport.resources) == 1
        store.undo()
        synchronize(engine)
        assert not parse_ics(next(iter(transport.resources.values())).data, "remote")[0].excluded_dates
        assert len(transport.resources) == 1
    finally:
        engine.close()
        store.close()
