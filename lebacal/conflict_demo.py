"""An explicitly offline conflict scenario for a disposable demo workspace."""
from dataclasses import replace
from datetime import timedelta
from queue import Queue
from uuid import uuid4

from .models import Event
from .sync import SyncConflict, SyncMessage
from .timeutil import now


class DemoConflictEngine:
    """Exercise the real conflict interface without a worker or server account."""

    credentials = None

    def __init__(self, store):
        self.store = store
        self.events = Queue()
        calendars = {calendar.id: calendar for calendar in store.list_calendars()}
        local = next((event for event in store.list_events()
                      if calendars[event.calendar_id].kind == "local" and not event.recurring
                      and not event.candidate and not event.all_day and not event.read_only), None)
        if local is None:
            calendar = next((item for item in calendars.values() if item.kind == "local" and not item.read_only), None)
            if calendar is None:
                raise ValueError("The offline conflict demo needs a writable local calendar.")
            start = now().replace(hour=9, minute=0, second=0, microsecond=0)
            local = Event(str(uuid4()), calendar.id, "Offline conflict example", start, start + timedelta(hours=1), location="Local room")
            store.apply("Create offline conflict example", [local])
        remote = replace(local, title=local.title + " · simulated server edit",
                         start=local.start + timedelta(hours=2), end=local.end + timedelta(hours=2),
                         location="Different room · simulated server")
        reason = "Offline demo: another client changed this event. Compare the versions; no real server is connected."
        conflict = SyncConflict("demo:" + str(uuid4()), local.calendar_id, local.id,
                                local, remote, reason, "demo:offline", (remote,))
        self._conflicts = {conflict.id: conflict}
        self._emit("conflict", reason, conflict)

    @property
    def conflicts(self):
        return list(self._conflicts.values())

    def _emit(self, kind, message, conflict=None):
        self.events.put(SyncMessage(kind, message, conflict.calendar_id if conflict else "", conflict))

    def sync_now(self):
        self._emit("finished", "Offline conflict demo: no server connection."
                   + (" A simulated change needs review." if self._conflicts else ""))

    def resolve_conflict(self, conflict_id, choice, *, expected=None):
        if choice not in {"local", "remote"}:
            raise ValueError("Choose 'local' or 'remote'.")
        conflict = self._conflicts.get(conflict_id)
        if conflict is None:
            return
        current = self.store.get_event(conflict.event_id)
        if (expected is not None and expected != conflict) or current != conflict.local:
            refreshed = replace(conflict, local=current,
                                reason="The demo versions changed while their comparison was open. Review the current versions before choosing.")
            self._conflicts[conflict_id] = refreshed
            self._emit("conflict", refreshed.reason, refreshed)
            return
        if choice == "remote":
            self.store.apply("Use simulated server version", [conflict.remote] if conflict.remote else [],
                             [conflict.event_id] if conflict.remote is None else [])
        self._conflicts.pop(conflict_id)
        self._emit("updated", "Offline demo: kept the " + ("local" if choice == "local" else "simulated server") + " version.")

    def close(self):
        pass
