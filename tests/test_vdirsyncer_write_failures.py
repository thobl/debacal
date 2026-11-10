"""Private per-resource diagnostics supplement vdirsyncer's transport errors."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from aiohttp import ClientResponseError
import pytest

from debacal.ics import export_ics
from debacal.models import Event
from debacal.vdirsyncer_backend import _digest
from debacal.vdirsyncer_runner import install_write_guard


def item(uid="rejected-item"):
    start = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
    event = Event("local-id", "calendar", "Imported event", start, start + timedelta(hours=1))
    return SimpleNamespace(raw=export_ics([replace(event, ical_uid=uid)]).decode())


def response_error(status=415):
    # Neither the request URL nor the server-supplied message is safe to copy
    # into persisted diagnostics, even when the message resembles a reason.
    request = SimpleNamespace(real_url="https://user:password@calendar.test/private/event.ics")
    return ClientResponseError(request, (), status=status,
                               message="Unsupported Media Type https://calendar.test/private/ password")


def guarded_remote(tmp_path, error=None, *, diagnostics=True, policy=None):
    calls = []

    class Remote:
        async def upload(self, event):
            calls.append(("upload", event))
            if error is not None:
                raise error
            return "created-href", "created-etag"

        async def update(self, href, event, etag):
            calls.append(("update", href, event, etag))
            if error is not None:
                raise error
            return "updated-etag"

    options = {"conflict_directory": str(tmp_path / "conflicts"), "expected_existing": {}}
    if diagnostics:
        options["failure_directory"] = str(tmp_path / "write-failures")
    options.update(policy or {})
    install_write_guard(options, Remote)
    return Remote(), calls


def invoke(remote, operation, event):
    coroutine = remote.upload(event) if operation == "upload" else remote.update("original-href", event, "original-etag")
    return asyncio.run(coroutine)


@pytest.mark.parametrize("operation", ["upload", "update"])
@pytest.mark.parametrize("status,reason", [(415, "Unsupported Media Type"), (500, "Internal Server Error"),
                                          (599, "HTTP request failed")])
def test_failed_write_records_uid_and_safe_http_reason_then_reraises(tmp_path, operation, status, reason):
    error = response_error(status)
    remote, calls = guarded_remote(tmp_path, error)
    event = item()
    with pytest.raises(ClientResponseError) as caught:
        invoke(remote, operation, event)
    assert caught.value is error
    assert calls == ([("upload", event)] if operation == "upload" else
                     [("update", "original-href", event, "original-etag")])
    directory = tmp_path / "write-failures"
    path = directory / (_digest("rejected-item") + ".json")
    data = path.read_text()
    assert json.loads(data) == {"uid": "rejected-item", "operation": operation, "status": status, "reason": reason}
    assert "password" not in data and "calendar.test" not in data and "private" not in data
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize("operation", ["upload", "update"])
def test_installed_caldav_put_preserves_body_headers_and_records_rejection(tmp_path, monkeypatch, operation):
    from vdirsyncer.storage.dav import CalDAVStorage
    from vdirsyncer.vobject import Item

    # Use the maintained storage's upload/update/_put implementation. Replace
    # only its HTTP boundary; no remote calendar or network is involved.
    storage = CalDAVStorage(connector=None, url="https://calendar.test/calendar/")
    error, requests = response_error(), []

    async def request(method, href, **kwargs):
        requests.append((method, href, kwargs))
        raise error

    monkeypatch.setattr(storage.session, "request", request)
    # install_write_guard changes these class attributes. Register their
    # originals so that the real storage is restored after the test.
    monkeypatch.setattr(CalDAVStorage, "upload", CalDAVStorage.upload)
    monkeypatch.setattr(CalDAVStorage, "update", CalDAVStorage.update)
    install_write_guard({"expected_existing": {}, "conflict_directory": str(tmp_path / "conflicts"),
                         "failure_directory": str(tmp_path / "write-failures")}, CalDAVStorage)
    event = Item(item().raw.replace("Imported event", "Imported café"))
    coroutine = (storage.upload(event) if operation == "upload" else
                 storage.update("/calendar/existing.ics", event, '"server-etag"'))
    with pytest.raises(ClientResponseError) as caught:
        asyncio.run(coroutine)
    assert caught.value is error
    (method, href, arguments), = requests
    assert method == "PUT"
    assert href == ("/calendar/rejected-item.ics" if operation == "upload" else "/calendar/existing.ics")
    assert arguments["data"] == event.raw.encode("utf-8")
    headers = arguments["headers"]
    assert headers["Content-Type"] == "text/calendar"
    if operation == "upload":
        assert headers["If-None-Match"] == "*" and "If-Match" not in headers
    else:
        assert headers["If-Match"] == '"server-etag"' and "If-None-Match" not in headers
    marker = json.loads((tmp_path / "write-failures" / (_digest("rejected-item") + ".json")).read_text())
    assert marker == {"uid": "rejected-item", "operation": operation, "status": 415,
                      "reason": "Unsupported Media Type"}


@pytest.mark.parametrize("operation,expected", [("upload", ("created-href", "created-etag")),
                                               ("update", "updated-etag")])
def test_successful_transport_is_unchanged_and_has_no_failure_record(tmp_path, operation, expected):
    remote, calls = guarded_remote(tmp_path)
    assert invoke(remote, operation, item()) == expected
    assert len(calls) == 1
    assert not (tmp_path / "write-failures").exists()


@pytest.mark.parametrize("operation", ["upload", "update"])
def test_optional_diagnostics_do_not_change_failure_behavior(tmp_path, operation):
    error = response_error()
    remote, _calls = guarded_remote(tmp_path, error, diagnostics=False)
    with pytest.raises(ClientResponseError) as caught:
        invoke(remote, operation, item())
    assert caught.value is error
    assert not (tmp_path / "write-failures").exists()


@pytest.mark.parametrize("error", [RuntimeError("Network unavailable"), response_error(302)])
def test_non_rejection_errors_are_not_misreported_as_resource_failures(tmp_path, error):
    remote, _calls = guarded_remote(tmp_path, error)
    with pytest.raises(type(error)) as caught:
        invoke(remote, "upload", item())
    assert caught.value is error
    assert not (tmp_path / "write-failures").exists()


def test_diagnostics_io_error_does_not_mask_transport_failure(tmp_path):
    blocked = tmp_path / "file-not-directory"
    blocked.write_text("Existing file")
    error = response_error()
    remote, _calls = guarded_remote(tmp_path, error,
                                    policy={"failure_directory": str(blocked / "write-failures")})
    with pytest.raises(ClientResponseError) as caught:
        invoke(remote, "upload", item())
    assert caught.value is error
    assert blocked.read_text() == "Existing file"


@pytest.mark.parametrize("uid", ["", "x" * 4097])
def test_missing_or_unbounded_uid_keeps_original_error_without_diagnostic(tmp_path, uid):
    error = response_error()
    remote, _calls = guarded_remote(tmp_path, error)
    event = SimpleNamespace(raw="BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:" + uid +
                            "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    with pytest.raises(ClientResponseError) as caught:
        invoke(remote, "upload", event)
    assert caught.value is error
    assert not (tmp_path / "write-failures").exists()


def test_expected_existing_upload_still_records_deletion_conflict_before_transport(tmp_path):
    from vdirsyncer.exceptions import PreconditionFailed
    remote, calls = guarded_remote(tmp_path, response_error(),
                                  policy={"expected_existing": {"rejected-item": "local-id"}})
    with pytest.raises(PreconditionFailed):
        invoke(remote, "upload", item())
    assert not calls
    conflict = json.loads((tmp_path / "conflicts" / (_digest("rejected-item") + ".json")).read_text())
    assert conflict["uid"] == "rejected-item"
    assert conflict["event_id"] == "local-id"
    assert conflict["remote_ics"] is None
    assert not (tmp_path / "write-failures").exists()
