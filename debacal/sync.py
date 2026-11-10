"""Background calendar synchronization with a durable local outbox.

Passwords are shared per account and saved in the desktop keyring when requested.
No network or keyring operation happens on the Qt/UI thread.
CalDAV uses maintained vdirsyncer by default. The optional legacy transport is
retained for targeted interoperability tests and explicit integrations.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from concurrent.futures import Future
from queue import Queue
from threading import Event as ThreadEvent, Lock, Thread
from urllib.parse import quote, urlparse, urlsplit, unquote
from urllib.request import Request, urlopen
from uuid import uuid4

from .ics import export_ics, normalize_caldav_ics, parse_ics, parse_ics_report, patch_ics
from .credentials import CredentialStore, CredentialError, account_key
from .models import Calendar, Event
from .store import EventOwnershipConflict, HistoryConflict, Store


@dataclass(frozen=True)
class RemoteResource:
    href: str
    etag: str
    data: str


@dataclass(frozen=True)
class SyncConflict:
    id: str
    calendar_id: str
    event_id: str
    local: Event | None
    remote: Event | None
    reason: str
    href: str = ""
    remote_events: tuple[Event, ...] = ()


@dataclass(frozen=True)
class SyncMessage:
    kind: str
    message: str
    calendar_id: str = ""
    conflict: SyncConflict | None = None


class PreconditionFailed(RuntimeError):
    pass


class CalDAVTransport:
    def __init__(self, calendar: Calendar, password: str):
        try:
            import caldav
        except ImportError as exc:
            raise ValueError("CalDAV needs the optional 'caldav' package. Install the project's dependencies first.") from exc
        parsed = urlparse(calendar.url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("A CalDAV calendar needs an HTTP(S) collection URL.")
        self.client = caldav.DAVClient(url=calendar.url, username=calendar.username or None,
                                       password=password or None, timeout=20)
        self.calendar = caldav.Calendar(client=self.client, url=calendar.url)
        self.url = calendar.url

    @staticmethod
    def _check(response, expected):
        status = response.status
        if status == 412:
            raise PreconditionFailed("The server version changed during synchronization.")
        if status not in expected:
            raise RuntimeError(f"Calendar server returned HTTP {status}.")

    def get(self, href: str) -> RemoteResource | None:
        response = self.client.request(href, method="GET")
        if response.status == 404:
            return None
        self._check(response, {200})
        raw = response.raw
        return RemoteResource(href, response.headers.get("ETag", ""),
                              raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)

    def list_resources(self) -> list[RemoteResource]:
        result = []
        for item in self.calendar.events():
            resource = self.get(str(item.url))
            if resource:
                result.append(resource)
        return result

    def new_href(self, event_id: str) -> str:
        return self.url.rstrip("/") + "/" + quote(event_id, safe="") + ".ics"

    @staticmethod
    def _etag(etag: str):
        if not etag or etag.startswith("W/"):
            raise ValueError("Server did not provide a strong ETag; safe synchronization is unavailable.")

    def put(self, href: str, data: bytes, *, etag: str | None = None) -> RemoteResource:
        if etag is not None:
            self._etag(etag)
        headers = {"Content-Type": "text/calendar; charset=utf-8",
                   "If-Match" if etag is not None else "If-None-Match": etag if etag is not None else "*"}
        response = self.client.request(href, method="PUT", body=data.decode(), headers=headers)
        self._check(response, {200, 201, 204})
        resource = self.get(href)
        if resource is None:
            raise RuntimeError("The server accepted the event but could not return it.")
        return resource

    def delete(self, href: str, *, etag: str):
        self._etag(etag)
        response = self.client.request(href, method="DELETE", headers={"If-Match": etag})
        self._check(response, {200, 204, 404})

    def close(self):
        self.client.close()


class SyncEngine:
    def __init__(self, store_path, *, transport_factory=None, vdir_backend=None,
                 credential_store=None, metadata_discoverer=None):
        self.store_path = store_path
        self.events: Queue[SyncMessage] = Queue()
        self._jobs = Queue()
        self._stop = ThreadEvent()
        self._lock = Lock()
        self.credentials = credential_store if credential_store is not None else CredentialStore()
        self._metadata_attempted = set()
        self._issue_count = 0
        self._conflicts: dict[str, SyncConflict] = {}
        self._queued = False
        self._worker_error = ""
        self._transport_factory = transport_factory
        self._metadata_discoverer = metadata_discoverer
        if vdir_backend is None:
            from .vdirsyncer_backend import VdirsyncerBackend
            vdir_backend = VdirsyncerBackend(store_path)
            if self._metadata_discoverer is None:
                from .discovery import discover_calendars
                self._metadata_discoverer = discover_calendars
        self._vdir_backend = vdir_backend
        self._thread = Thread(target=self._run, name="debacal-sync", daemon=True)
        self._thread.start()

    @property
    def conflicts(self) -> list[SyncConflict]:
        with self._lock:
            return list(self._conflicts.values())

    def set_password(self, calendar_id: str, password: str, remember: bool = True):
        """Queue account-wide credential persistence and return its Future."""
        future = Future()
        if self._stop.is_set():
            future.set_exception(CredentialError("The sync worker is closed. Reopen the app before saving account credentials."))
            return future
        self._jobs.put(("credential", (calendar_id, password, remember, future)))
        return future

    def _save_password(self, store, calendar_id, password, remember, future):
        try:
            calendar = store.get_calendar(calendar_id)
            if calendar is None:
                raise CredentialError("The calendar no longer exists. Add it before saving its account password.")
            persisted = self.credentials.save(calendar, password, remember=remember)
            message = ("Account password saved securely and shared with its calendars." if persisted else
                       "Account password is session-only; any previously saved password was removed.")
            self._emit("credential", message, calendar.id)
            future.set_result(persisted)
        except CredentialError as exc:
            self._emit("error", str(exc), calendar_id)
            future.set_exception(exc)

    def sync_now(self):
        with self._lock:
            if self._queued or self._stop.is_set():
                if self._worker_error:
                    self._emit("error", self._worker_error)
                    self._emit("finished", "Sync worker is unavailable; restart Debacal after fixing the error.")
                return
            self._queued = True
        self._jobs.put(("sync", None))

    def resolve_conflict(self, conflict_id: str, choice: str, *, expected: SyncConflict | None = None):
        if choice not in {"local", "remote"}:
            raise ValueError("Choose 'local' or 'remote'.")
        self._jobs.put(("resolve", (conflict_id, choice, expected)))

    def close(self):
        self._stop.set()
        self._jobs.put(("stop", None))
        # Never block shutdown for a network timeout. The worker is a daemon.
        self.credentials.clear_session()

    def _emit(self, kind, message, calendar_id="", conflict=None):
        if kind == "issue":
            self._issue_count += 1
        self.events.put(SyncMessage(kind, message, calendar_id, conflict))

    def _run(self):
        try:
            # The worker maintains durable source/query projections on writes;
            # it never displays a time window and needs no second in-memory tree.
            store = Store(self.store_path, query_index=False)
        except Exception as exc:
            self._worker_error = f"Could not start the sync worker: {exc}"
            self._stop.set()
            self._emit("error", self._worker_error)
            self._emit("finished", "Sync could not start.")
            return
        with store:
            if self._transport_factory is None and hasattr(self._vdir_backend, "saved_conflicts"):
                for calendar in store.list_calendars():
                    if calendar.kind == "caldav":
                        try:
                            self._publish_vdir_conflicts(calendar, self._vdir_backend.saved_conflicts(calendar.id))
                        except Exception:
                            self._emit("error", f"{calendar.name}: saved conflicts could not be read. Sync to refresh them.", calendar.id)
            while not self._stop.is_set():
                kind, payload = self._jobs.get()
                if kind == "stop":
                    return
                failures, connected = 0, 0
                try:
                    if kind == "credential":
                        self._save_password(store, *payload)
                    elif kind == "resolve":
                        self._resolve(store, *payload)
                    else:
                        self._issue_count = 0
                        self._metadata_attempted.clear()
                        self._emit("started", "Syncing in the background…")
                        for calendar in store.list_calendars():
                            if self._stop.is_set():
                                break
                            calendar = store.get_calendar(calendar.id)
                            if calendar is None or not calendar.enabled:
                                continue
                            if calendar.kind == "local":
                                continue
                            connected += 1
                            try:
                                if calendar.kind == "ics":
                                    self._sync_ics(store, calendar)
                                elif calendar.kind == "caldav":
                                    if self._transport_factory is None:
                                        self._sync_vdir(store, calendar)
                                    else:
                                        self._sync_caldav(store, calendar)
                            except Exception as exc:
                                failures += 1
                                self._emit("error", f"{calendar.name}: {exc}", calendar.id)
                except Exception as exc:
                    failures += 1
                    self._emit("error", str(exc))
                finally:
                    if kind == "sync":
                        with self._lock:
                            self._queued = False
                        if failures:
                            message = (f"Sync finished with {failures} calendar error(s). Local changes remain queued. "
                                       "Click the sync status at the bottom left for details.")
                        elif self.conflicts:
                            message = f"Sync finished; {len(self.conflicts)} conflict(s) need review."
                        elif self._issue_count:
                            message = f"Sync finished with {self._issue_count} resource or metadata warning(s). Healthy events synced; review the issues."
                        elif not connected:
                            message = "No connected calendars to sync."
                        else:
                            message = "All connected calendars synced."
                        self._emit("finished", message)

    def _sync_vdir(self, store, calendar):
        password = self._password(calendar)
        calendar = self._refresh_metadata(store, calendar, password)
        if not calendar.enabled:
            return
        result = self._vdir_backend.sync(store, calendar, password)
        for issue in getattr(result, "issues", ()):
            self._emit("issue", f"{calendar.name}: resource {issue.resource_id}: {issue.reason}", calendar.id)
        self._publish_vdir_conflicts(calendar, result.conflicts)
        message = f"{calendar.name}: received {result.received} event(s), sent {result.sent} change(s) with vdirsyncer."
        if result.conflicts:
            message += " Some changes still need review."
        elif getattr(result, "issues", ()):
            message += " Some resources still need review; healthy changes synced."
        self._emit("updated", message, calendar.id)

    def _publish_vdir_conflicts(self, calendar, items):
        active = set()
        for item in items:
            conflict_id = "vdirsyncer:" + calendar.id + ":" + item.id
            active.add(conflict_id)
            conflict = SyncConflict(conflict_id, calendar.id, item.event_id, item.local, item.remote,
                                    item.reason, "vdirsyncer:" + item.id,
                                    (item.remote,) if item.remote else ())
            with self._lock:
                previous = self._conflicts.get(conflict_id)
                self._conflicts[conflict_id] = conflict
            if previous != conflict:
                self._emit("conflict", f"{calendar.name}: a change needs your attention.", calendar.id, conflict)
        with self._lock:
            for conflict_id in list(self._conflicts):
                conflict = self._conflicts[conflict_id]
                if conflict.calendar_id == calendar.id and conflict.href.startswith("vdirsyncer:") and conflict_id not in active:
                    del self._conflicts[conflict_id]

    def _sync_ics(self, store: Store, calendar: Calendar):
        parsed = urlparse(calendar.url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("A subscription needs an HTTP(S) URL.")
        request = Request(calendar.url, headers={"User-Agent": "Debacal/0.1"})
        with urlopen(request, timeout=20) as response:
            data = response.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("The subscription exceeds the 10 MiB import limit.")
        report = parse_ics_report(data, calendar.id)
        report = store.refresh_subscription(report, calendar.id)
        for issue in report.issues:
            self._emit("issue", f"{calendar.name}: resource {issue.resource_id}: {issue.reason}", calendar.id)
        self._emit("updated", f"{calendar.name}: {len(report.events)} events refreshed.", calendar.id)

    def _password(self, calendar):
        if not calendar.username:
            return self.credentials.cached(calendar) or ""
        password = self.credentials.get(calendar)
        if not password:
            raise CredentialError("No saved password is available for this account. Open one of its calendars and enter the app password once.")
        return password

    def _refresh_metadata(self, store, calendar, password):
        key = account_key(calendar)
        if self._metadata_discoverer is None or key in self._metadata_attempted:
            return store.get_calendar(calendar.id) or calendar
        self._metadata_attempted.add(key)
        try:
            found = self._metadata_discoverer(calendar.url, calendar.username, password)
            def collection_key(url):
                parsed = urlsplit(url)
                return (parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), unquote(parsed.path).rstrip("/"))
            by_url = {collection_key(item.url): item for item in found}
            for current in store.list_calendars(include_disabled=True):
                if current.kind != "caldav" or account_key(current) != key:
                    continue
                metadata = by_url.get(collection_key(current.url))
                if metadata:
                    alias = (current.name_alias or (current.name if not current.server_name and current.name != metadata.name else ''))
                    changes = {'server_name': metadata.name,
                               'name_alias': alias,
                               'name': alias or metadata.name}
                    if metadata.color:
                        changes['server_color'] = metadata.color
                    refreshed = replace(current, **changes)
                    if refreshed != current:
                        store.save_calendar(refreshed)
        except Exception:
            self._emit("issue", f"{calendar.name}: server calendar colors could not be refreshed; existing colors were kept.", calendar.id)
        return store.get_calendar(calendar.id) or calendar

    def _transport(self, calendar):
        password = self._password(calendar)
        return self._transport_factory(calendar, password)

    @staticmethod
    def _record(store, calendar_id, event, resource):
        store.set_sync_state(calendar_id, event.id, resource.href, resource.etag,
                             {"event": event.to_dict(), "ics": resource.data})

    def _ownership_issue(self, store, calendar, event, raw, error):
        resource_id = hashlib.sha256((event.ical_uid or event.id).encode()).hexdigest()[:12]
        store.set_quarantined_resource(calendar.id, resource_id, raw or '', str(error))
        self._emit('issue', f'{calendar.name}: resource {resource_id}: {error}', calendar.id)

    def _conflict(self, calendar, event_id, local, remote, reason, *, href="", remote_events=None):
        with self._lock:
            existing = next((c for c in self._conflicts.values()
                             if c.calendar_id == calendar.id and c.event_id == event_id), None)
            if existing:
                return
            conflict = SyncConflict(str(uuid4()), calendar.id, event_id, local, remote, reason,
                                    href, tuple(remote_events if remote_events is not None else ([remote] if remote else [])))
            self._conflicts[conflict.id] = conflict
        self._emit("conflict", f"{calendar.name}: a change needs your attention.", calendar.id, conflict)

    @staticmethod
    def _parse_resource(calendar_id, resource, states):
        events = parse_ics(resource.data, calendar_id)
        # Other calendar clients sometimes drop our X-properties. The resource's
        # persisted href remains its identity; keep the local master ID stable
        # without depending on those extensions.
        matches = [state for state in states if state["href"] == resource.href]
        if len(events) == len(matches) == 1:
            from .recurrence import occurrence_id
            event, target_id = events[0], matches[0]["event_id"]
            overrides = {
                key: replace(Event.from_dict(payload), id=occurrence_id(target_id, key), series_id=target_id).to_dict()
                for key, payload in event.recurrence_overrides.items()
            }
            events = [replace(event, id=target_id, recurrence_overrides=overrides)]
        return events

    def _sync_caldav(self, store: Store, calendar: Calendar):
        transport = self._transport(calendar)
        try:
            resources = transport.list_resources()
            states = {s["event_id"]: s for s in store.list_sync_state(calendar.id)}
            remote = {}
            for resource in resources:
                events = self._parse_resource(calendar.id, resource, states.values())
                for event in events:
                    if event.id in remote and remote[event.id][1].href != resource.href:
                        raise ValueError("The server returned duplicate event identities; synchronization was stopped safely.")
                    remote[event.id] = (event, resource)
            changes = {c["event_id"]: c for c in store.pending_changes(calendar.id)}
            blocked_ids = set()
            for event_id, (event, resource) in remote.items():
                try:
                    with store._transaction():
                        store._assert_identity_owner(event.id, calendar.id, event.ical_uid)
                except EventOwnershipConflict as error:
                    self._ownership_issue(store, calendar, event, resource.data, error)
                    blocked_ids.add(event_id)
                    continue
                if event_id in changes:
                    continue
                if event_id in states and states[event_id]["etag"] == resource.etag and store.get_event(event_id):
                    continue  # Do not mutate local/history snapshots for our own unchanged upload.
                try:
                    with store._transaction():
                        store.apply_remote(upserts=[event], calendar_id=calendar.id)
                        self._record(store, calendar.id, event, resource)
                except EventOwnershipConflict as error:
                    self._ownership_issue(store, calendar, event, resource.data, error)
                    continue
                except HistoryConflict:
                    continue  # A newer local edit appeared while the network was busy.
            for event_id in states.keys() - remote.keys() - changes.keys():
                try:
                    prior = (states[event_id].get('base_payload') or {}).get('event') or {}
                    with store._transaction():
                        store.apply_remote(delete_ids=[event_id], calendar_id=calendar.id,
                                           expected_uids={event_id: prior.get('ical_uid') or prior.get('id')})
                        store.delete_sync_state(calendar.id, event_id)
                except EventOwnershipConflict as error:
                    self._emit('issue', f'{calendar.name}: {error}', calendar.id)
                except HistoryConflict:
                    pass
            for event_id, change in changes.items():
                if event_id in blocked_ids:
                    continue
                with self._lock:
                    blocked = any(c.event_id == event_id and c.calendar_id == calendar.id
                                  for c in self._conflicts.values())
                if blocked:
                    continue
                state = states.get(event_id)
                remote_event, resource = remote.get(event_id, (None, None))
                local = Event.from_dict(change["payload"]) if change["payload"] else None
                if state and resource and state["etag"] != resource.etag:
                    self._conflict(calendar, event_id, local, remote_event, "Both this app and the server changed the event.", href=resource.href)
                    continue
                if state and resource is None and local is not None:
                    self._conflict(calendar, event_id, local, None, "The server deleted an event you edited locally.", href=state["href"])
                    continue
                if not state and resource:
                    self._conflict(calendar, event_id, local, remote_event, "A server event already uses this identity.", href=resource.href)
                    continue
                try:
                    if local is None:
                        if resource:
                            transport.delete(resource.href, etag=resource.etag)
                        store.delete_sync_state(calendar.id, event_id)
                    else:
                        data = normalize_caldav_ics(patch_ics(resource.data, local) if resource else export_ics([local]))
                        saved = transport.put(resource.href if resource else transport.new_href(event_id),
                                              data, etag=resource.etag if resource else None)
                        self._record(store, calendar.id, local, saved)
                    store.acknowledge_change(change["id"])
                except PreconditionFailed:
                    self._conflict(calendar, event_id, local, remote_event,
                                   "The server changed again during synchronization. Refresh before resolving.",
                                   href=resource.href if resource else transport.new_href(event_id))
            self._emit("updated", f"{calendar.name}: background sync complete.", calendar.id)
        finally:
            transport.close()

    def _resolve(self, store, conflict_id, choice, expected=None):
        with self._lock:
            conflict = self._conflicts.get(conflict_id)
        if not conflict:
            return
        if expected is not None and expected != conflict:
            self._emit("conflict", "The conflict changed while its comparison was open. Review the current versions.",
                       conflict.calendar_id, conflict)
            return
        calendar = store.get_calendar(conflict.calendar_id)
        if not calendar:
            return
        if conflict.href.startswith("vdirsyncer:"):
            from .vdirsyncer_backend import semantic
            self._vdir_backend.set_decision(calendar.id, conflict.href.removeprefix("vdirsyncer:"), choice,
                                             expected_local=semantic(conflict.local), expected_remote=semantic(conflict.remote))
            self._sync_vdir(store, calendar)
            return
        transport = self._transport(calendar)
        try:
            state = store.get_sync_state(calendar.id, conflict.event_id)
            href = conflict.href or (state["href"] if state else transport.new_href(conflict.event_id))
            resource = transport.get(href)
            states = store.list_sync_state(calendar.id)
            matches = self._parse_resource(calendar.id, resource, states) if resource else []
            remote_event = next((e for e in matches if e.id == conflict.event_id), matches[0] if matches else None)
            changes = [c for c in store.pending_changes(calendar.id) if c["event_id"] == conflict.event_id]
            local_event = store.get_event(conflict.event_id)
            try:
                with store._transaction():
                    store._assert_identity_owner(conflict.event_id, calendar.id,
                                                 local_event.ical_uid if local_event else None)
                    store._assert_remote_owners(matches, [], calendar.id)
            except EventOwnershipConflict as error:
                source = remote_event or local_event or conflict.remote or conflict.local
                if source:
                    self._ownership_issue(store, calendar, source, resource.data if resource else '', error)
                return
            if local_event != conflict.local or tuple(matches) != conflict.remote_events:
                refreshed = replace(conflict, local=local_event, remote=remote_event,
                                    remote_events=tuple(matches),
                                    reason="The versions changed since the last comparison. Review the current values.")
                with self._lock:
                    self._conflicts[conflict_id] = refreshed
                self._emit("conflict", refreshed.reason, calendar.id, refreshed)
                return
            if choice == "remote":
                # Discard only the explicitly reviewed pending version. Newer edits
                # require another review instead of silently disappearing.
                store.accept_remote(calendar.id, conflict.event_id, local_event,
                                    [change["id"] for change in changes], upserts=matches,
                                    delete_ids=[] if any(e.id == conflict.event_id for e in matches) else [conflict.event_id])
            else:
                if local_event is None:
                    if resource:
                        transport.delete(href, etag=resource.etag)
                    resource, remote_event = None, None
                else:
                    data = normalize_caldav_ics(patch_ics(resource.data, local_event) if resource else export_ics([local_event]))
                    resource = transport.put(href, data, etag=resource.etag if resource else None)
                    remote_event = local_event
                for change in changes:
                    store.acknowledge_change(change["id"])
            if choice == "remote" and resource:
                for event in matches:
                    self._record(store, calendar.id, event, resource)
                if not any(e.id == conflict.event_id for e in matches):
                    store.delete_sync_state(calendar.id, conflict.event_id)
            elif resource and remote_event:
                self._record(store, calendar.id, remote_event, resource)
            else:
                store.delete_sync_state(calendar.id, conflict.event_id)
            with self._lock:
                self._conflicts.pop(conflict_id, None)
            self._emit("updated", "Conflict resolved.", calendar.id)
        finally:
            transport.close()
