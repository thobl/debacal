#!/usr/bin/env python3
"""Opt-in live smoke test using environment credentials, never saved to disk.

Default: read-only account discovery and initial calendar pull. --write-test creates one UUID-named test
resource, exercises recurrence-aware create/update/delete, and removes only that
resource after verifying its UID. Existing calendar resources are never edited.
"""
from __future__ import annotations

import argparse
import getpass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lebacal.discovery import discover_calendars
from lebacal.ics import export_ics, parse_ics, patch_ics
from lebacal.models import Calendar, Event
from lebacal.sync import CalDAVTransport
from lebacal.store import Store
from lebacal.vdirsyncer_backend import VdirsyncerBackend


def check_vdirsyncer(calendar, password, event):
    """Exercise the same outbox/mirror bridge used by the desktop application."""
    uid = event.ical_uid
    with tempfile.TemporaryDirectory(prefix="lebacal-live-check-") as directory:
        with Store(Path(directory) / "calendar.sqlite3") as store:
            store.save_calendar(calendar)
            backend = VdirsyncerBackend(store.path)
            def synchronize():
                result = backend.sync(store, calendar, password)
                if result.conflicts:
                    raise RuntimeError("The live test encountered a conflict; it did not choose a version automatically.")
                return result
            def remote_item():
                synchronize()  # A fresh read-only remote pass verifies server state.
                items = backend._mirror(backend.calendar_path(calendar.id) / "snapshot", calendar.id)
                return items.get(uid, (None, None))[0]
            # Import the existing collection before creating anything. Only the
            # unpredictable test UID below ever enters this store's outbox.
            synchronize()
            created = False
            try:
                store.apply("Create live test series", [event])
                created = True
                synchronize()
                returned = remote_item()
                assert returned and returned.ical_uid == uid and returned.recurrence_rule == event.recurrence_rule
                master = store.get_event(event.id)
                store.apply("Edit live test series", [replace(master, title="Lebacal integration test updated (safe to remove)")])
                instances = [item for item in store.query_occurrences(event.start - timedelta(days=1), event.start + timedelta(days=30))
                             if item.ical_uid == uid]
                assert len(instances) == 3
                store.edit_occurrence(instances[1], replace(instances[1], title="Lebacal integration test exception (safe to remove)",
                                                            start=instances[1].start + timedelta(minutes=30),
                                                            end=instances[1].end + timedelta(minutes=30)))
                store.delete_occurrence(instances[2])
                synchronize()
                returned = remote_item()
                assert returned and returned.recurrence_rule == event.recurrence_rule
                assert len(returned.recurrence_overrides) == 1 and len(returned.excluded_dates) == 1
                assert not store.pending_changes(calendar.id)
                store.apply("Delete live test series", delete_ids=[event.id])
                synchronize()
                assert remote_item() is None
                created = False
                print("Default vdirsyncer bridge passed initial pull, recurring create, series/occurrence edits, EXDATE deletion, and cleanup.")
            finally:
                if created:
                    try:
                        current = store.get_event(event.id)
                        if current and current.ical_uid == uid:
                            store.apply("Cleanup live test series", delete_ids=[current.id])
                        synchronize()
                        if remote_item() is not None:
                            raise RuntimeError("Test resource remains")
                    except Exception:
                        print(f"Cleanup could not be verified. Remove only the test event with UID {uid}.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("LEBACAL_TEST_CALDAV_URL", ""), help="Server URL (or LEBACAL_TEST_CALDAV_URL)")
    parser.add_argument("--username", default=os.environ.get("LEBACAL_TEST_CALDAV_USERNAME", ""), help="Account name (or LEBACAL_TEST_CALDAV_USERNAME)")
    parser.add_argument("--write-test", action="store_true", help="Create/update/delete one uniquely named test event")
    parser.add_argument("--backend", choices=("vdirsyncer", "direct"), default="vdirsyncer",
                        help="Test the application's default bridge, or the explicit legacy direct transport")
    parser.add_argument("--calendar-url", help="Use this discovered calendar collection for the write test")
    args = parser.parse_args()
    server = args.url
    username = args.username
    password = os.environ.get("LEBACAL_TEST_CALDAV_PASSWORD", "")
    if not server or not username:
        parser.error("Supply --url and --username, or set LEBACAL_TEST_CALDAV_URL and LEBACAL_TEST_CALDAV_USERNAME.")
    if not password:
        password = getpass.getpass("CalDAV app password: ")
    if not password:
        parser.error("An app password is required.")
    calendars = discover_calendars(server, username, password)
    print(f"Discovery succeeded: {len(calendars)} calendar(s).")
    matching = [c for c in calendars if (not args.write_test or not c.read_only)
                and (not args.calendar_url or c.url == args.calendar_url)]
    if not matching:
        raise ValueError("No matching discovered calendar is available for this test.")
    collection = matching[0]
    calendar = Calendar("live-smoke", "Live smoke test", "#4f7865", kind="caldav", url=collection.url,
                        username=username, read_only=collection.read_only)
    if not args.write_test:
        if args.backend == "vdirsyncer":
            with tempfile.TemporaryDirectory(prefix="lebacal-read-check-") as directory:
                with Store(Path(directory) / "calendar.sqlite3") as store:
                    store.save_calendar(calendar)
                    result = VdirsyncerBackend(store.path).sync(store, calendar, password)
                    print(f"Default vdirsyncer initial pull succeeded: {result.received} event resource(s).")
        else:
            transport = CalDAVTransport(calendar, password)
            try:
                print(f"Legacy direct initial pull succeeded: {len(transport.list_resources())} event resource(s).")
            finally:
                transport.close()
        return 0
    uid = "lebacal-integration-" + str(uuid4())
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=7)
    event = Event(uid, calendar.id, "Lebacal integration test (safe to remove)", start, start + timedelta(minutes=15),
                  ical_uid=uid, recurrence_rule="FREQ=WEEKLY;COUNT=3")
    if args.backend == "vdirsyncer":
        check_vdirsyncer(calendar, password, event)
        return 0
    transport = CalDAVTransport(calendar, password)
    href = transport.new_href(uid)
    try:
        saved = transport.put(href, export_ics([event]))
        parsed = parse_ics(saved.data, calendar.id)
        assert len(parsed) == 1 and parsed[0].ical_uid == uid and parsed[0].recurrence_rule == event.recurrence_rule
        edited = replace(parsed[0], title="Lebacal integration test updated (safe to remove)",
                         excluded_dates=((start + timedelta(days=7)).isoformat(),))
        saved = transport.put(href, patch_ics(saved.data, edited), etag=saved.etag)
        returned = parse_ics(saved.data, calendar.id)[0]
        assert returned.ical_uid == uid and returned.excluded_dates == edited.excluded_dates
        transport.delete(href, etag=saved.etag)
        assert transport.get(href) is None
        print("Conditional recurrence create/read/update/delete succeeded.")
    finally:
        # An interrupted PUT may have succeeded before the response was lost.
        # Only remove our exact unpredictable UID, with its current ETag.
        try:
            remaining = transport.get(href)
            if remaining is not None:
                items = parse_ics(remaining.data, calendar.id)
                if len(items) == 1 and items[0].ical_uid == uid:
                    transport.delete(href, etag=remaining.etag)
                else:
                    print("Cleanup skipped: resource identity differs from this test.", file=sys.stderr)
        finally:
            transport.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Libraries may include request details in exception text. Never print
        # credentials or request headers from live account operations.
        print(f"Live check failed ({type(exc).__name__}); no credentials were logged.", file=sys.stderr)
        raise SystemExit(1)
