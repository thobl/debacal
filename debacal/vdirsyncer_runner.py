"""Run maintained vdirsyncer with an application existence precondition.

Vdirsyncer's normal edit-versus-delete policy recreates the edited item without
calling its conflict command. Debacal requires review instead. This small
adapter blocks that particular Upload action for an expected-existing UID;
all HTTP, ETags, normal updates, creates and deletes remain in vdirsyncer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def install_write_guard(policy, storage_class=None):
    from aiohttp import ClientResponseError
    from http import HTTPStatus
    from vdirsyncer.exceptions import NotFoundError, PreconditionFailed
    if storage_class is None:
        from vdirsyncer.storage.dav import CalDAVStorage
        storage_class = CalDAVStorage
    from debacal.vdirsyncer_backend import _digest, atomic_write, parsed_one, raw_uid, semantic
    expected = policy.get("expected_existing", {})
    directory = Path(policy["conflict_directory"])
    failure_directory = Path(policy["failure_directory"]) if policy.get("failure_directory") else None
    original_upload, original_update = storage_class.upload, storage_class.update

    def failed_write(item, operation, error):
        if failure_directory is None or error.status < 400:
            return
        # Response exceptions include the request URL and can contain server-
        # supplied text. Retain only the standard HTTP meaning, never the URL,
        # authentication data or an unbounded error response.
        try:
            uid = raw_uid(item.raw)
            if not uid or len(uid.encode("utf-8")) > 4096:
                return
            try:
                reason = HTTPStatus(error.status).phrase
            except ValueError:
                reason = "HTTP request failed"
            atomic_write(failure_directory / (_digest(uid) + ".json"), json.dumps({
                "uid": uid, "operation": operation, "status": error.status, "reason": reason,
            }))
        except (OSError, TypeError, ValueError, UnicodeError):
            # Diagnostics are best-effort. Preserve the actual transport error
            # and vdirsyncer's per-item rollback even if writing them fails.
            pass

    def deleted_conflict(item):
        local = parsed_one(item.raw)
        event_id = expected[local.ical_uid]
        atomic_write(directory / (_digest(local.ical_uid) + ".json"), json.dumps({
            "uid": local.ical_uid, "event_id": event_id,
            "local_ics": item.raw, "remote_ics": None,
            "local_hash": semantic(local), "remote_hash": semantic(None),
            "reason": "The server deleted an event you changed locally during synchronization.",
        }))

    async def upload(self, item):
        if raw_uid(item.raw) in expected:
            deleted_conflict(item)
            raise PreconditionFailed("Expected-existing event disappeared; review the deletion conflict in Debacal.")
        try:
            return await original_upload(self, item)
        except ClientResponseError as error:
            failed_write(item, "upload", error)
            raise

    async def update(self, href, item, etag):
        try:
            return await original_update(self, href, item, etag)
        except NotFoundError:
            if raw_uid(item.raw) in expected:
                deleted_conflict(item)
            raise
        except ClientResponseError as error:
            failed_write(item, "update", error)
            raise

    storage_class.upload = upload
    storage_class.update = update


def main():
    policy_file = os.environ.get("DEBACAL_VDIR_WRITE_POLICY")
    if policy_file:
        install_write_guard(json.loads(Path(policy_file).read_text()))
    from vdirsyncer.cli import app
    app()


if __name__ == "__main__":
    try:
        main()
    except ImportError:
        print("Install vdirsyncer in the Python environment used to launch Debacal.", file=sys.stderr)
        raise SystemExit(2)
