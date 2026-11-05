from dataclasses import replace
import io
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from lebacal.credentials import CredentialStore, CredentialError, account_key
from lebacal.discovery import DiscoveredCalendar
from lebacal.models import Calendar
from lebacal.store import Store
from lebacal.sync import SyncEngine


class MemoryKeyring:
    def __init__(self):
        self.values = {}
        self.reads = []
        self.writes = []
        self.deletes = []
        self.fail_write = False

    def get_password(self, service, username):
        self.reads.append((service, username, get_ident()))
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.writes.append((service, username, get_ident()))
        if self.fail_write:
            raise RuntimeError("Backend diagnostics must never expose the submitted secret")
        self.values[service, username] = password

    def delete_password(self, service, username):
        self.deletes.append((service, username))
        self.values.pop((service, username), None)


def calendar(key="work", **changes):
    return replace(Calendar(key, key.capitalize(), "#123456", kind="caldav", username="alice",
                            url=f"https://cloud.example/remote.php/dav/calendars/alice/{key}/"), **changes)


def test_account_credentials_are_shared_across_collections_and_default_ports():
    a = calendar()
    b = calendar("personal", url="https://CLOUD.EXAMPLE:443/remote.php/dav/calendars/alice/personal/")
    assert account_key(a) == account_key(b)
    backend = MemoryKeyring()
    credentials = CredentialStore(backend)
    assert credentials.save(a, "example-app-password")
    assert credentials.get(b) == "example-app-password"
    assert len(backend.writes) == 1
    assert CredentialStore(backend).get(b) == "example-app-password"


def test_different_servers_logins_ports_and_nextcloud_deployments_are_isolated():
    a = calendar()
    variants = [a, replace(a, username="bob"), replace(a, username="Alice"),
                replace(a, url=a.url.replace("cloud.example", "other.example")),
                replace(a, url=a.url.replace("https:", "http:")),
                replace(a, url=a.url.replace("cloud.example", "cloud.example:444")),
                replace(a, url=a.url.replace("/remote.php/", "/instance-one/remote.php/")),
                replace(a, url=a.url.replace("/remote.php/", "/instance-two/remote.php/"))]
    assert len({account_key(item) for item in variants}) == len(variants)


def test_session_override_precedes_keyring_and_duplicate_saves_are_skipped():
    backend = MemoryKeyring()
    credentials = CredentialStore(backend)
    credentials.save(calendar(), "saved")
    credentials.save(calendar("personal"), "saved")
    assert len(backend.writes) == 1
    credentials.set_session(calendar(), "temporary")
    assert credentials.get(calendar("personal")) == "temporary"
    assert backend.reads == []


def test_unchecking_remember_removes_old_saved_password_and_keeps_session():
    backend = MemoryKeyring()
    credentials = CredentialStore(backend)
    credentials.save(calendar(), "old")
    assert credentials.save(calendar(), "new", remember=False) is False
    assert credentials.get(calendar("personal")) == "new"
    assert CredentialStore(backend).get(calendar()) is None
    assert len(backend.deletes) == 1


def test_persistence_failure_retains_shared_session_without_echoing_secret():
    backend = MemoryKeyring()
    backend.fail_write = True
    credentials = CredentialStore(backend)
    with pytest.raises(CredentialError) as caught:
        credentials.save(calendar(), "never-echo-this-password")
    assert "never-echo-this-password" not in str(caught.value)
    assert "session only" in str(caught.value)
    assert credentials.get(calendar("personal")) == "never-echo-this-password"
    assert backend.values == {}


def test_plaintext_backend_is_never_used_as_an_automatic_fallback(monkeypatch):
    class Plaintext(MemoryKeyring):
        priority = 100
    Plaintext.__module__ = "keyrings.alt.file"
    backend = Plaintext()
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: backend))
    credentials = CredentialStore()
    with pytest.raises(CredentialError):
        credentials.save(calendar(), "example-secret")
    assert backend.writes == []
    assert credentials.get(calendar()) == "example-secret"


def test_chainer_uses_secure_os_backend_without_plaintext_fallback(monkeypatch):
    class Plaintext(MemoryKeyring):
        priority = 100
    Plaintext.__module__ = "keyrings.alt.file"
    class SecretService(MemoryKeyring):
        priority = 5
    SecretService.__module__ = "keyring.backends.SecretService"
    plain, secure = Plaintext(), SecretService()
    class Chainer:
        backends = [plain, secure]
    Chainer.__module__ = "keyring.backends.chainer"
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: Chainer()))
    CredentialStore().save(calendar(), "example-secret")
    assert len(secure.writes) == 1
    assert plain.writes == []


def test_forgetting_account_clears_all_sibling_calendar_access():
    backend = MemoryKeyring()
    credentials = CredentialStore(backend)
    credentials.save(calendar(), "example-secret")
    credentials.forget(calendar("personal"))
    assert credentials.get(calendar()) is None
    assert CredentialStore(backend).get(calendar()) is None


class CaptureBridge:
    def __init__(self, issues=()):
        self.calls = []
        self.issues = issues

    def sync(self, store, item, password):
        self.calls.append((item, password))
        return SimpleNamespace(received=0, sent=0, conflicts=(), issues=self.issues)


def synchronize(engine):
    engine.sync_now()
    messages = []
    while True:
        item = engine.events.get(timeout=5)
        messages.append(item)
        if item.kind == "finished":
            return messages


def setup_engine(tmp_path, *, backend=None, metadata=None, bridge=None):
    store = Store(tmp_path / "calendar.sqlite")
    store.save_calendar(calendar())
    store.save_calendar(calendar("personal", use_server_color=False))
    keyring = backend or MemoryKeyring()
    credentials = CredentialStore(keyring)
    capture = bridge or CaptureBridge()
    engine = SyncEngine(store.path, vdir_backend=capture, credential_store=credentials,
                        metadata_discoverer=metadata)
    return store, keyring, credentials, capture, engine


def close_engine(store, engine):
    engine.close()
    engine._thread.join(timeout=2)
    store.close()


def test_engine_saves_once_on_worker_and_reloads_shared_password_after_restart(tmp_path):
    store, backend, credentials, bridge, engine = setup_engine(tmp_path)
    try:
        assert engine.set_password("work", "example-secret").result(timeout=5)
        assert engine.set_password("personal", "example-secret").result(timeout=5)
        assert len(backend.writes) == 1
        assert backend.writes[0][2] != get_ident()
        assert not any(item.kind == "error" for item in synchronize(engine))
        assert [password for _, password in bridge.calls] == ["example-secret", "example-secret"]
        engine.close()
        engine._thread.join(timeout=2)
        restarted_bridge = CaptureBridge()
        engine = SyncEngine(store.path, vdir_backend=restarted_bridge, credential_store=CredentialStore(backend))
        assert not any(item.kind == "error" for item in synchronize(engine))
        assert [password for _, password in restarted_bridge.calls] == ["example-secret", "example-secret"]
    finally:
        close_engine(store, engine)


def test_engine_persistence_failure_reports_error_but_session_sync_succeeds(tmp_path):
    backend = MemoryKeyring()
    backend.fail_write = True
    store, _, _, bridge, engine = setup_engine(tmp_path, backend=backend)
    try:
        with pytest.raises(CredentialError):
            engine.set_password("work", "example-secret").result(timeout=5)
        messages = synchronize(engine)
        assert any(item.kind == "error" and "session only" in item.message for item in messages)
        assert len(bridge.calls) == 2
        assert "All connected calendars synced" in messages[-1].message
    finally:
        close_engine(store, engine)


def test_account_metadata_refreshes_once_and_preserves_manual_color(tmp_path):
    calls = []
    def discover(url, username, password):
        calls.append(get_ident())
        return [DiscoveredCalendar("Work", calendar().url, "#aabbcc"),
                DiscoveredCalendar("Personal", calendar("personal").url, "#ccbbaa")]
    store, _, credentials, _, engine = setup_engine(tmp_path, metadata=discover)
    try:
        credentials.set_session(calendar(), "example-secret")
        synchronize(engine)
        assert len(calls) == 1 and calls[0] != get_ident()
        assert store.get_calendar("work").color == "#aabbcc"
        assert store.get_calendar("personal").server_color == "#ccbbaa"
        assert store.get_calendar("personal").color == "#123456"
    finally:
        close_engine(store, engine)


def test_metadata_failure_and_resource_issues_report_partial_success(tmp_path):
    def discover(*_):
        raise RuntimeError("raw-secret-diagnostics")
    issue = SimpleNamespace(resource_id="a1b2c3d4e5f6", reason="Unsupported recurrence")
    store, _, credentials, bridge, engine = setup_engine(tmp_path, metadata=discover, bridge=CaptureBridge((issue,)))
    try:
        credentials.set_session(calendar(), "example-secret")
        messages = synchronize(engine)
        assert len(bridge.calls) == 2
        assert sum(item.kind == "issue" for item in messages) == 3
        assert all("raw-secret-diagnostics" not in item.message for item in messages)
        assert "warning" in messages[-1].message
        assert "All connected calendars synced" not in messages[-1].message
    finally:
        close_engine(store, engine)


def test_subscription_quarantines_one_bad_uid_without_dropping_healthy_or_cached_events(tmp_path, monkeypatch):
    body = [""]
    def feed(healthy_title="Healthy", broken_end="20261110T110000Z"):
        return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
                "BEGIN:VEVENT\r\nUID:healthy\r\nSUMMARY:" + healthy_title + "\r\n"
                "DTSTART:20261110T090000Z\r\nDTEND:20261110T100000Z\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:broken\r\nSUMMARY:Cached version\r\n"
                "DTSTART:20261110T100000Z\r\nDTEND:" + broken_end + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    monkeypatch.setattr("lebacal.sync.urlopen", lambda *_args, **_kwargs: io.BytesIO(body[0].encode()))
    store = Store(tmp_path / "feed.sqlite")
    store.save_calendar(Calendar("feed", "Feed", "#123456", kind="ics", url="https://feed.example/calendar.ics"))
    engine = SyncEngine(store.path, vdir_backend=CaptureBridge(), credential_store=CredentialStore(MemoryKeyring()))
    try:
        body[0] = feed()
        assert not any(item.kind == "error" for item in synchronize(engine))
        before = {event.ical_uid: event for event in store.list_events(calendar_ids=["feed"])}
        body[0] = feed("Updated healthy event", "20261110T090000Z")
        messages = synchronize(engine)
        after = {event.ical_uid: event for event in store.list_events(calendar_ids=["feed"])}
        assert after["healthy"].title == "Updated healthy event"
        assert after["broken"] == before["broken"]
        quarantined = store.list_quarantined_resources("feed")
        assert len(quarantined) == 1
        assert "UID:broken" in quarantined[0]["raw_ics"]
        assert any(item.kind == "issue" for item in messages)
        assert "warning" in messages[-1].message
        assert not any(item.kind == "error" for item in messages)
        body[0] = feed()
        synchronize(engine)
        assert store.list_quarantined_resources("feed") == []
    finally:
        close_engine(store, engine)
