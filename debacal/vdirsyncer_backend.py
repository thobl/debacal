"""Bridge SQLite's transactional outbox to maintained vdirsyncer CLI storage.

A fresh read-only remote pass establishes the working mirror and vdirsyncer's
status before each outbox write pass. SQLite retains the cross-pass baseline
and every queued mutation, so failures and remote deletes remain recoverable.
The snapshot mirror is an immutable copy of the remote pass for diagnostics.
Passwords are supplied only to child processes through password.fetch, never to
configuration files, command arguments, or diagnostics.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timezone
import hashlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .ics import export_ics, parse_ics, parse_ics_report, patch_ics
from .ics_resources import ParseReport, ResourceIssue, RejectedResource, _safe_reason, reject_duplicate_identities
from .import_sources import MAX_IMPORT_BYTES
from .models import Calendar, Event
from .store import EventOwnershipConflict, HistoryConflict, Store


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def semantic(event: Event | None) -> str:
    """Compare visible content across server normalization and local identities."""
    if event is None:
        return "deleted"
    def normalized(item):
        from .recurrence import canonical_rule, normalized_rule
        values = item.to_dict()
        for key in ("raw_ics", "id", "calendar_id", "series_id", "recurrence_id", "read_only"):
            values.pop(key, None)
        def endpoint(value):
            if item.all_day:
                return value.date().isoformat()
            return (value.replace(tzinfo=None) if item.floating_time else value.astimezone(timezone.utc)).isoformat()
        values['start'], values['end'] = endpoint(item.start), endpoint(item.end)
        from .ics_resources import _timezone_signature
        from .ics_fallback import unfolded
        if item.timezone_id and item.timezone_id not in item.timezone_definitions:
            # A retained producer alias can become an ordinary IANA name when
            # fixed detached ranges export as UTC and no longer need another
            # resource's conflicting unprefixed definition. Compare its actual
            # scheduling rules rather than that harmless spelling change.
            from .timezones import resolve_timezone, timezone_identifier
            values['timezone_id'] = timezone_identifier(resolve_timezone(item.timezone_id))
        # Only this item's scheduling zone can affect its future generated
        # occurrences. Context used solely by fixed detached ranges can be
        # normalized to UTC by other clients without changing the schedule.
        values['timezone_definitions'] = {identifier: _timezone_signature(unfolded(definition))
                                           for identifier, definition in sorted(item.timezone_definitions.items())
                                           if identifier == item.timezone_id}
        nominal = bool(item.duration_spec and not item.series_id)
        if nominal and not item.recurring and not item.floating_time:
            from .recurrence import event_zone
            start = item.start.astimezone(event_zone(item))
            nominal = not (start.fold and start.replace(fold=0).timestamp() != start.timestamp())
        if item.all_day or (not nominal and (not item.recurring or
                not item.recurrence_rule and not (item.schedule_expression and item.schedule_syntax == 2))):
            values.pop("timezone_id", None)
            values.pop('timezone_definitions', None)
        if not item.schedule_expression:
            values.pop("schedule_syntax", None)
        if not nominal:
            # Detached/fixed occurrences export exact UTC endpoints. Their
            # optional nominal intent is unrepresentable there. Ordinary
            # standalone DURATION retains intent for later copies and moves.
            values.pop('duration_spec', None)
        values["recurrence_rule"] = normalized_rule(canonical_rule(item.recurrence_rule, item), item)
        values["recurrence_dates"] = sorted(item.recurrence_dates)
        values["excluded_dates"] = sorted(item.excluded_dates)
        values["recurrence_overrides"] = {key: normalized(Event.from_dict(payload))
                                          for key, payload in sorted(item.recurrence_overrides.items())}
        return values
    return _digest(json.dumps(normalized(event), sort_keys=True, separators=(",", ":")))


def parsed_one(data: str | bytes, calendar_id="bridge") -> Event:
    events = parse_ics(data, calendar_id)
    if len(events) != 1:
        raise ValueError("Each synchronized ICS file must contain exactly one event UID.")
    return events[0]


def _private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path: Path, data: str | bytes):
    _private_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class BridgeConflict:
    id: str
    event_id: str
    local: Event | None
    remote: Event | None
    reason: str


@dataclass(frozen=True)
class BridgeResult:
    received: int
    sent: int
    conflicts: tuple[BridgeConflict, ...] = ()
    issues: tuple[ResourceIssue, ...] = ()


@dataclass(frozen=True)
class MirrorReport:
    items: dict[str, tuple[Event, str]]
    files: dict[str, tuple[Path, ...]]
    rejected: tuple[RejectedResource, ...] = ()
    unknown_uid: bool = False
    opaque_uids: frozenset[str] = frozenset()


def raw_uid(data):
    from .ics_fallback import text, unfolded
    stack = []
    for line in unfolded(data):
        left, _, value = line.partition(":")
        if left.upper() == "BEGIN":
            stack.append(value.upper())
            continue
        if left.upper() == "END":
            if stack:
                stack.pop()
            continue
        if stack == ["VCALENDAR", "VEVENT"] and left.split(";")[0].upper() == "UID":
            return text(value)
    return ""


def _read_mirror_resource(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_IMPORT_BYTES + 1)
    return raw.decode('utf-8-sig', errors='replace'), len(raw) > MAX_IMPORT_BYTES


class VdirsyncerBackend:
    def __init__(self, store_path, *, runner=None, executable=None):
        self.root = Path(str(Path(store_path).resolve()) + ".sync")
        self.runner = runner or subprocess.run
        self._injected_runner = runner is not None
        self.executable = executable
        self._conflict_issues = []

    def calendar_path(self, calendar_id):
        return self.root / _digest(calendar_id)[:24]

    def _command(self):
        if self.executable:
            return [str(self.executable)]
        if not self._injected_runner and find_spec("vdirsyncer") is not None:
            # Console scripts embed an absolute interpreter path. Running the
            # installed module survives moving this checkout/virtualenv and
            # keeps discovery and our write guard in the same environment.
            return [sys.executable, "-m", "vdirsyncer"]
        local = Path(sys.executable).with_name("vdirsyncer")
        found = str(local) if local.is_file() else shutil.which("vdirsyncer")
        if not found and not self._injected_runner:
            raise ValueError("vdirsyncer is not installed in this Python environment or on PATH. "
                             "Install the project dependencies, then restart Debacal.")
        return [found or "vdirsyncer"]

    @contextmanager
    def _locked(self, path):
        import fcntl
        _private_directory(path)
        with (path / ".lock").open("a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another Debacal process is already syncing this calendar.") from None
            try:
                yield stream.fileno()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _config(self, calendar, path, *, pull, bootstrap=False):
        pair = "pull" if pull else "push"
        mirror = path / ("snapshot" if pull else "working")
        status = path / ("pull-status" if pull else "push-status")
        conflicts = path / "conflicts"
        for directory in (mirror, status, conflicts):
            _private_directory(directory)
        helper = str(Path(__file__).with_name("vdirsyncer_hook.py"))
        q = json.dumps
        read_only = pull or bootstrap or calendar.read_only
        conflict = None if pull or bootstrap else ["command", sys.executable, helper, "conflict", str(conflicts)]
        partial = 'partial_sync = "revert"\n' if read_only else ""
        authentication = f"username = {q(calendar.username)}\n" if calendar.username else ""
        if calendar.username:
            authentication += f"password.fetch = {q(['command', sys.executable, helper, 'password'])}\n"
        text = (f"[general]\nstatus_path = {q(str(status))}\n\n"
                f"[pair {pair}]\na = \"local\"\nb = \"remote\"\ncollections = null\n"
                f"conflict_resolution = {q(conflict)}\n{partial}\n"
                f"[storage local]\ntype = \"filesystem\"\npath = {q(str(mirror))}\nfileext = \".ics\"\n\n"
                f"[storage remote]\ntype = \"caldav\"\nurl = {q(calendar.url)}\n{authentication}"
                f"read_only = {q(read_only)}\n")
        config = path / (pair + ".conf")
        atomic_write(config, text)
        return config

    def _run(self, calendar, path, password, *, pull, force_delete=False, bootstrap=False):
        config = self._config(calendar, path, pull=pull, bootstrap=bootstrap)
        command = self._command()
        options = ["--config", str(config), "sync"]
        if pull or force_delete:
            # Pull storage is read-only on the server. Force permits its local
            # cache to become empty when the user emptied the remote calendar.
            options.append("--force-delete")
        options.append("pull" if pull else "push")
        arguments = [*command, *options]
        environment = os.environ.copy()
        environment["DEBACAL_VDIR_PASSWORD"] = password
        if not (pull or bootstrap or calendar.read_only):
            # Preserve the maintained CLI while preventing its implicit
            # recreation of an edited event concurrently deleted on the server.
            arguments = [sys.executable, str(Path(__file__).with_name("vdirsyncer_runner.py")), *options]
            environment["DEBACAL_VDIR_WRITE_POLICY"] = str(path / "write-policy.json")
        try:
            discover = [*command, "--config", str(config), "discover", "--no-list", "pull" if pull else "push"]
            discovery = self.runner(discover, env=environment, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=120, check=False,
                                    pass_fds=(self._active_lock_fd,))
            if discovery.returncode:
                output = ((discovery.stderr or "") + "\n" + (discovery.stdout or "")).strip()
                return discovery.returncode, output.replace(password, "[redacted]")[-2500:] if password else output[-2500:]
            result = self.runner(arguments, env=environment, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=120, check=False,
                                 pass_fds=(self._active_lock_fd,))
        except FileNotFoundError:
            raise ValueError("The vdirsyncer launcher or its Python interpreter could not be found. "
                             "If its virtual environment was moved, recreate or reinstall that environment; "
                             "install vdirsyncer in the Python environment used to launch Debacal.") from None
        except PermissionError:
            raise ValueError("The vdirsyncer launcher could not be executed. Check its execute permission "
                             "or install vdirsyncer in the Python environment used to launch Debacal.") from None
        except subprocess.TimeoutExpired:
            raise ValueError("vdirsyncer timed out after two minutes. Local changes remain queued.") from None
        finally:
            environment.pop("DEBACAL_VDIR_PASSWORD", None)
            environment.pop("DEBACAL_VDIR_WRITE_POLICY", None)
        output = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
        if password:
            output = output.replace(password, "[redacted]")
        return result.returncode, output[-2500:]

    @staticmethod
    def _mirror_report(path, calendar_id):
        items, files, rejected, opaque_uids = {}, {}, [], set()
        unknown = False
        for file in sorted(path.glob("*.ics")):
            data, oversized = _read_mirror_resource(file)
            if oversized:
                # Inspect a bounded prefix for identity, never load a whole
                # oversized server resource merely to reject it afterward.
                uid = raw_uid(data)
                files.setdefault(uid, []).append(file)
                unknown |= not uid
                rejected.append(RejectedResource(uid, data, _digest(uid or file.name)[:12],
                                                 'Oversized event resource; maximum size is 20 MB.'))
                continue
            # Inventory via PROPFIND includes tasks and journals. Debacal does
            # not edit these resources; preserve their files unchanged instead
            # of filtering the server listing or treating them as malformed.
            try:
                from .ics_resources import _blocks, _uid_of
                blocks = _blocks(data)[1]
                if blocks and not any(block[0] == "BEGIN:VEVENT" for block in blocks):
                    non_events = [block for block in blocks if block[0] in {
                        "BEGIN:VTODO", "BEGIN:VJOURNAL", "BEGIN:VFREEBUSY", "BEGIN:VAVAILABILITY", "BEGIN:VPOLL"}]
                    if non_events:
                        opaque_uids.update(uid for block in non_events if (uid := _uid_of(block)))
                        continue
            except ValueError:
                pass  # Malformed VEVENT resources follow the quarantine path below.
            uid = raw_uid(data)
            files.setdefault(uid, []).append(file)
            unknown |= not uid
            resource_id = _digest(uid or file.name)[:12]
            try:
                report = parse_ics_report(data, calendar_id)
                if report.rejected:
                    rejected.extend(report.rejected)
                    continue
                if len(report.events) > 1:
                    raise ValueError("Multiple UID resources in a mirror item")
                if not report.events and "STATUS:CANCELLED" not in data.upper():
                    raise ValueError("No supported event resource in mirror item")
                if report.events:
                    event = report.events[0]
                    items[event.ical_uid] = (event, data)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                rejected.append(RejectedResource(uid, data, resource_id, _safe_reason(exc)))
        for uid, paths in files.items():
            if len(paths) > 1:
                items.pop(uid, None)
                for file in paths:
                    rejected.append(RejectedResource(uid, _read_mirror_resource(file)[0], _digest(uid)[:12],
                                                     "Duplicate event UID requires review."))
        protected = {item.uid for item in rejected}
        for uid in protected:
            items.pop(uid, None)
        identities = reject_duplicate_identities(ParseReport(tuple(event for event, _data in items.values())))
        for collision in identities.rejected:
            _event, data = items.pop(collision.uid)
            rejected.append(replace(collision, raw_ics=data))
        return MirrorReport(items, {uid: tuple(paths) for uid, paths in files.items()}, tuple(rejected), unknown, frozenset(opaque_uids))

    @classmethod
    def _mirror(cls, path, calendar_id):
        return cls._mirror_report(path, calendar_id).items

    @staticmethod
    def _quarantine(store, calendar_id, path, rejected):
        for item in rejected:
            atomic_write(path / "quarantine" / (item.resource_id + ".ics"), item.raw_ics)
            store.set_quarantined_resource(calendar_id, item.resource_id, item.raw_ics, item.reason)

    @staticmethod
    def _state_event(state):
        payload = (state or {}).get("base_payload") or {}
        if payload.get("event"):
            return Event.from_dict(payload["event"])
        return None

    @staticmethod
    def _identify(event, event_id):
        from .recurrence import occurrence_id
        overrides = {key: replace(Event.from_dict(payload), id=occurrence_id(event_id, key), series_id=event_id).to_dict()
                     for key, payload in event.recurrence_overrides.items()}
        return replace(event, id=event_id, recurrence_overrides=overrides)

    @classmethod
    def _identify_inventory(cls, items, id_by_uid, existing):
        identified = {uid: (cls._identify(event, id_by_uid.get(uid, event.id)), data)
                      for uid, (event, data) in items.items()}
        report = reject_duplicate_identities(ParseReport(tuple(event for event, _data in identified.values())),
                                            existing_events=existing)
        rejected = tuple(replace(item, raw_ics=identified[item.uid][1]) for item in report.rejected)
        rejected_ids = {identified[item.uid][0].id for item in rejected}
        protected = {item.uid for item in rejected} | {event.ical_uid for event in existing if event.id in rejected_ids}
        for item in rejected:
            identified.pop(item.uid, None)
        return identified, rejected, protected

    @staticmethod
    def _record(store, calendar_id, event, data):
        store.set_sync_state(calendar_id, event.id, "vdirsyncer:" + event.ical_uid, semantic(event),
                             {"backend": "vdirsyncer", "event": event.to_dict(), "ics": data})

    def _marker(self, path, uid):
        return path / "conflicts" / (_digest(uid) + ".json")

    def _mark(self, path, uid, event_id, local, remote, local_data, remote_data, reason):
        file = self._marker(path, uid)
        previous = json.loads(file.read_text()) if file.exists() else {}
        marker = {"uid": uid, "event_id": event_id, "local_ics": local_data, "remote_ics": remote_data,
                  "local_hash": semantic(local), "remote_hash": semantic(remote), "reason": reason}
        if previous.get("local_hash") == marker["local_hash"] and previous.get("remote_hash") == marker["remote_hash"]:
            marker["choice"] = previous.get("choice", "")
        atomic_write(file, json.dumps(marker))
        return marker

    def set_decision(self, calendar_id, conflict_id, choice, *, expected_local=None, expected_remote=None):
        path = self.calendar_path(calendar_id)
        with self._locked(path):
            file = path / "conflicts" / (conflict_id + ".json")
            if not file.exists():
                raise ValueError("This conflict has already been resolved. Sync to refresh the comparison.")
            marker = json.loads(file.read_text())
            if ((expected_local is not None and marker.get("local_hash") != expected_local)
                    or (expected_remote is not None and marker.get("remote_hash") != expected_remote)):
                raise HistoryConflict("The conflict changed while its comparison was open. Review the current versions before choosing.")
            marker["choice"] = choice
            atomic_write(file, json.dumps(marker))

    def _conflicts(self, path, calendar_id, id_by_uid):
        result = []
        for file in sorted((path / "conflicts").glob("*.json")):
            try:
                marker = json.loads(file.read_text())
                uid = marker["uid"]
                local = parsed_one(marker["local_ics"], calendar_id) if marker.get("local_ics") else None
                remote = parsed_one(marker["remote_ics"], calendar_id) if marker.get("remote_ics") else None
                event_id = marker.get("event_id") or id_by_uid.get(uid) or (local or remote).id
                local = self._identify(local, event_id) if local else None
                remote = self._identify(remote, event_id) if remote else None
                result.append(BridgeConflict(file.stem, event_id, local, remote,
                                             marker.get("reason", "Both this app and the server changed the event.")))
            except (ValueError, TypeError, KeyError, AttributeError):
                self._conflict_issues.append(ResourceIssue(file.stem[:12], "Stored conflict comparison requires review."))
        return tuple(result)

    def saved_conflicts(self, calendar_id):
        path = self.calendar_path(calendar_id)
        if not path.exists():
            return ()
        manifest = path / "bridge.json"
        id_by_uid = json.loads(manifest.read_text()) if manifest.exists() else {}
        return self._conflicts(path, calendar_id, id_by_uid)

    @staticmethod
    def _write_working(path, desired):
        working = path / "working"
        _private_directory(working)
        remaining = dict(desired)
        for file in working.glob("*.ics"):
            current = file.read_text()
            uid = parsed_one(current).ical_uid
            if uid not in remaining:
                file.unlink()
                continue
            data = remaining.pop(uid)
            if current != data.replace("\r\n", "\n"):
                atomic_write(file, data)
        for uid, data in remaining.items():
            atomic_write(working / (_digest(uid) + ".ics"), data)
        return working

    def sync(self, store: Store, calendar: Calendar, password: str) -> BridgeResult:
        self._command()  # Fail before touching local mirror state if unavailable.
        if calendar.username and not password:
            raise ValueError("Enter the calendar's app password for this session, then sync again.")
        path = self.calendar_path(calendar.id)
        with self._locked(path) as lock_fd:
            self._active_lock_fd = lock_fd
            self._conflict_issues = []
            # The durable source of truth is SQLite, including every pending
            # mutation. Start each network cycle with a fresh read-only vdir
            # baseline. This avoids interpreting snapshot refreshes as local
            # writes against old vdir status, which can resurrect remote deletes.
            working = path / "working"
            _private_directory(working)
            for file in working.glob("*.ics"):
                file.unlink()
            if (path / "push-status").exists():
                shutil.rmtree(path / "push-status")
            code, output = self._run(calendar, path, password, pull=False, bootstrap=True)
            if code:
                raise ValueError("vdirsyncer could not read the calendar. Local changes remain queued. " + output)
            mirror_report = self._mirror_report(working, calendar.id)
            remote = mirror_report.items
            protected_uids = {item.uid for item in mirror_report.rejected}
            issues = [ResourceIssue(item.resource_id, item.reason) for item in mirror_report.rejected]
            self._quarantine(store, calendar.id, path, mirror_report.rejected)
            def finish(received, sent, conflicts):
                unique = {item.resource_id: item for item in issues + self._conflict_issues}
                # A complete successful inventory retries quarantined items.
                # Once repaired (or removed remotely), they must stop looking
                # like unresolved errors in the application and local mirror.
                for file in (path / 'quarantine').glob('*.ics'):
                    if file.stem not in unique:
                        # Reviewed imports use the same database issue table;
                        # only clear records owned by this backend's mirror.
                        store.delete_quarantined_resource(calendar.id, file.stem)
                        file.unlink(missing_ok=True)
                return BridgeResult(received, sent, conflicts, tuple(unique.values()))
            snapshot = path / "snapshot"
            _private_directory(snapshot)
            for file in snapshot.glob("*.ics"):
                file.unlink()
            for file in working.glob("*.ics"):
                atomic_write(snapshot / file.name, file.read_bytes())
            states = {s["event_id"]: s for s in store.list_sync_state(calendar.id)}
            manifest_path = path / "bridge.json"
            id_by_uid = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            existing = store.list_events(calendar_ids=[calendar.id])
            for event in existing:
                id_by_uid[event.ical_uid] = event.id
            for state in states.values():
                event = self._state_event(state)
                if event:
                    id_by_uid[event.ical_uid] = state["event_id"]
            owners = existing + store.get_events(event.id for event, _data in remote.values())
            remote, identity_issues, identity_protected = self._identify_inventory(remote, id_by_uid, owners)
            protected_uids.update(identity_protected)
            issues.extend(ResourceIssue(item.resource_id, item.reason) for item in identity_issues)
            self._quarantine(store, calendar.id, path, identity_issues)
            def ownership_issue(event, data, error):
                resource_id = _digest(event.ical_uid)[:12]
                issues.append(ResourceIssue(resource_id, str(error)))
                protected_uids.add(event.ical_uid)
                self._quarantine(store, calendar.id, path,
                                 (RejectedResource(event.ical_uid, data or '', resource_id, str(error)),))
            changes = list(store.pending_changes(calendar.id))
            if calendar.read_only and changes:
                raise ValueError("This calendar is read-only. Pending local changes cannot be uploaded; review its access settings.")
            staged = {}
            for change in changes:
                uid = change["event_id"]
                local = None
                try:
                    local = Event.from_dict(change["payload"]) if change["payload"] else None
                    base = self._state_event(states.get(change["event_id"]))
                    uid = (local or base).ical_uid if local or base else next((uid for uid, eid in id_by_uid.items() if eid == change["event_id"]), change["event_id"])
                    id_by_uid[uid] = change["event_id"]
                    if uid in mirror_report.opaque_uids:
                        issues.append(ResourceIssue(_digest(uid)[:12], "A non-event resource uses this UID; its queued event change was retained."))
                        continue
                    if uid in protected_uids or (mirror_report.unknown_uid and base is not None and uid not in remote):
                        issues.append(ResourceIssue(_digest(uid)[:12], "Source event requires review; its queued change was retained."))
                        continue
                    current, data = remote.get(uid, (None, None))
                    local_data = (patch_ics(data, local) if data else export_ics([local])).decode() if local else None
                    canonical_local = parsed_one(local_data, calendar.id) if local_data else None
                    staged[uid] = (change, local, local_data)
                    marker_path = self._marker(path, uid)
                    known_marker = json.loads(marker_path.read_text()) if marker_path.exists() else None
                    differs = semantic(current) != semantic(base)
                    conflict = (differs or known_marker is not None) and semantic(canonical_local) != semantic(current)
                    if not conflict:
                        marker_path.unlink(missing_ok=True)
                        continue
                    reason = "Both this app and the server changed the event."
                    if current is None:
                        reason = "The server deleted an event you changed locally."
                    elif local is None:
                        reason = "The server changed an event you deleted locally."
                    marker = self._mark(path, uid, change["event_id"], canonical_local, current, local_data, data, reason)
                    if marker.get("choice") == "remote":
                        store.accept_remote(calendar.id, change["event_id"], local, [change["id"]],
                                            upserts=[current] if current else [], delete_ids=[] if current else [change["event_id"]])
                        if current:
                            self._record(store, calendar.id, current, data)
                        else:
                            store.delete_sync_state(calendar.id, change["event_id"])
                        marker_path.unlink(missing_ok=True)
                        staged.pop(uid)
                    elif marker.get("choice") == "local":
                        continue  # The hook revalidates actual server values before resolving.
                except EventOwnershipConflict as error:
                    ownership_issue(current or local or base, data or (local.raw_ics if local else ''), error)
                    staged.pop(uid, None)
                except HistoryConflict:
                    issues.append(ResourceIssue(_digest(uid)[:12], "A newer local edit was retained for the next sync."))
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    reason = _safe_reason(exc)
                    issues.append(ResourceIssue(_digest(uid)[:12], reason))
                    raw = local.raw_ics if local else (change.get("payload") or {}).get("raw_ics", "")
                    self._quarantine(store, calendar.id, path,
                                     (RejectedResource(uid, raw, _digest(uid)[:12], reason),))
                    staged.pop(uid, None)
            atomic_write(manifest_path, json.dumps(id_by_uid))

            pending_uids = set(staged)
            received = 0
            for uid, (event, data) in remote.items():
                if uid in pending_uids:
                    continue
                try:
                    with store._transaction():
                        store.apply_remote(upserts=[event], calendar_id=calendar.id)
                        self._record(store, calendar.id, event, data)
                    received += 1
                except EventOwnershipConflict as error:
                    ownership_issue(event, data, error)
                except HistoryConflict:
                    pass
            remote_ids = {event.id for event, _ in remote.values()}
            pending_ids = {c[0]["event_id"] for c in staged.values()}
            for event_id, state in states.items():
                previous = self._state_event(state)
                protected = mirror_report.unknown_uid or (previous is not None and previous.ical_uid in protected_uids)
                if not protected and event_id not in remote_ids and event_id not in pending_ids:
                    try:
                        with store._transaction():
                            store.apply_remote(delete_ids=[event_id], calendar_id=calendar.id,
                                               expected_uids={event_id: previous.ical_uid} if previous else None)
                            store.delete_sync_state(calendar.id, event_id)
                    except EventOwnershipConflict as error:
                        if previous:
                            ownership_issue(previous, (state.get('base_payload') or {}).get('ics', ''), error)
                        else:
                            issues.append(ResourceIssue(_digest(event_id)[:12], str(error)))
                    except HistoryConflict:
                        pass
            conflicts = self._conflicts(path, calendar.id, id_by_uid)
            for conflict in conflicts:
                marker = json.loads((path / "conflicts" / (conflict.id + ".json")).read_text())
                if marker.get("choice") != "local":
                    staged.pop(marker["uid"], None)
            if not staged:
                return finish(received, 0, conflicts)
            # Stage only healthy captured UIDs. Opaque/quarantined files and
            # all unrelated remote resources stay byte-for-byte unchanged.
            deleted = set()
            for uid, (_, local, data) in staged.items():
                files = mirror_report.files.get(uid, ())
                if local is None:
                    for file in files:
                        file.unlink(missing_ok=True)
                    deleted.add(uid)
                else:
                    target = files[0] if files else working / (_digest(uid) + ".ics")
                    atomic_write(target, data)
            force_delete = bool(deleted and not any(working.glob("*.ics")) and set(mirror_report.files) <= deleted)
            atomic_write(path / "write-policy.json", json.dumps({
                "expected_existing": {uid: change["event_id"] for uid, (change, local, _) in staged.items()
                                      if local is not None and uid in remote},
                "conflict_directory": str(path / "conflicts"),
            }))
            code, output = self._run(calendar, path, password, pull=False, force_delete=force_delete)
            final_report = self._mirror_report(working, calendar.id)
            owners = store.list_events(calendar_ids=[calendar.id]) + store.get_events(
                event.id for event, _data in final_report.items.values())
            final, identity_issues, identity_protected = self._identify_inventory(
                final_report.items, id_by_uid, owners)
            final_protected = {item.uid for item in final_report.rejected} | identity_protected
            issues.extend(ResourceIssue(item.resource_id, item.reason) for item in final_report.rejected)
            issues.extend(ResourceIssue(item.resource_id, item.reason) for item in identity_issues)
            self._quarantine(store, calendar.id, path, final_report.rejected)
            self._quarantine(store, calendar.id, path, identity_issues)
            if code:
                conflicts = self._conflicts(path, calendar.id, id_by_uid)
                if conflicts:
                    return finish(received, 0, conflicts)
                raise ValueError("vdirsyncer could not upload the calendar. Local changes remain queued. " + output)
            sent = 0
            for uid, (change, local, proposed_data) in staged.items():
                if uid in final_protected or (final_report.unknown_uid and uid not in final):
                    continue
                actual, actual_data = final.get(uid, (None, None))
                expected = parsed_one(proposed_data, calendar.id) if proposed_data else None
                if semantic(actual) != semantic(expected):
                    self._mark(path, uid, change["event_id"], expected, actual, proposed_data, actual_data,
                               "The server changed during synchronization. Review these versions before continuing.")
                    continue
                if actual:
                    actual = self._identify(actual, change["event_id"])
                    try:
                        self._record(store, calendar.id, actual, actual_data)
                    except EventOwnershipConflict as error:
                        ownership_issue(actual, actual_data, error)
                        continue
                else:
                    store.delete_sync_state(calendar.id, change["event_id"])
                store.acknowledge_change(change["id"])
                self._marker(path, uid).unlink(missing_ok=True)
                sent += 1
            # Vdirsyncer can receive unrelated edits/deletions while uploading
            # captured mutations. Reflect that final mirror immediately, while
            # Store's pending guard protects edits made during the subprocess.
            for uid, (event, data) in final.items():
                if uid in staged:
                    continue
                event = self._identify(event, id_by_uid.get(uid, event.id))
                try:
                    with store._transaction():
                        store.apply_remote(upserts=[event], calendar_id=calendar.id)
                        self._record(store, calendar.id, event, data)
                except EventOwnershipConflict as error:
                    ownership_issue(event, data, error)
                except HistoryConflict:
                    pass
            for uid in remote.keys() - final.keys() - staged.keys() - final_protected:
                if final_report.unknown_uid:
                    continue
                event_id = remote[uid][0].id
                try:
                    with store._transaction():
                        store.apply_remote(delete_ids=[event_id], calendar_id=calendar.id,
                                           expected_uids={event_id: uid})
                        store.delete_sync_state(calendar.id, event_id)
                except EventOwnershipConflict as error:
                    ownership_issue(remote[uid][0], remote[uid][1], error)
                except HistoryConflict:
                    pass
            return finish(received, sent, self._conflicts(path, calendar.id, id_by_uid))
