# Debacal implementation plan

## Goal

A Linux desktop calendar where interactions commit to local SQLite immediately.
Network latency must never be part of editing or changing views. No email,
invitations, or reminder notifications in the initial scope.

## Architecture

* Python + PySide6: native desktop application, custom-painted calendar grid.
* SQLite: events, calendar visibility, transactional history, durable sync outbox.
* Pure domain modules: pattern expansion, overlapping-event layout, ICS conversion.
* Worker thread: ICS downloads and CalDAV synchronization, with its own connection.
  UI polls messages; no network work on the UI thread.
* Existing `icalendar`, `python-dateutil`, and `caldav` libraries handle parsing,
  recurrence expansion and account discovery. vdirsyncer is the default CalDAV
  file-sync backend; SQLite outbox records are bridged in a worker.
* Recurring masters and standard exceptions are stored once, expanded per view,
  and synchronized as a complete UID resource. Finite irregular dates use RDATE.
* Persisted display preferences follow the OS locale by default and allow any
  week start, European/ISO/US dates, and 12-hour/24-hour time overrides.
* No cloud service or browser runtime. Account passwords can persist in the operating system keyring.

## Stages and acceptance criteria

1. **Local-first core:** CRUD survives restart; a multi-event edit is atomic; undo
   and redo survive restart. Selective undo rejects actions whose event state has
   changed since the operation, while unrelated changes do not block it.
2. **Calendar workspace:** day/week/month/year, overlap layout, calendar toggles, search,
   create by dragging, move/resize, and configurable keyboard actions with hints.
3. **Efficient groups:** a bounded pattern parser and visual calendar preview; atomic batch
   creation; candidate groups; choosing one removes alternatives in one undoable action.
4. **Interoperability:** import/export ICS; read-only URL subscriptions; account
   discovery including Nextcloud, background outbox retries, conditional writes,
   conflict choices, RRULE/RDATE/EXDATE and single-occurrence overrides.
5. **Hardening:** automated domain and offscreen UI checks, safe error paths,
   manual desktop validation and packaging guidance.

## Assumptions and open questions

* **Resolved:** week start and date/time display are configurable, with system
  defaults. The system timezone is used for display; recurring events retain
  their named source zone.
* **Resolved:** the [sequence specification](SEQUENCES.md) is the single source
  for syntax, evaluation, normalization, and interaction behavior. The reference
  is explicit (`+0d` includes it), relative steps always specify `d/w/m/y`, and
  `N*step`/`N*(sequence)` repeat instructions. Standard recurring tails use
  `every(...)`; weekday selectors, bounds, exclusions, and coincident occurrences
  retain distinct standard identities. Unsupported compact combinations use an
  exact editable schedule document. There is always an authoring string.
* **Resolved:** calendar clicks reconstruct a compact equivalent custom sequence.
  Candidate steps include partial dates, exact dates, times and relative units;
  each is checked using the original event's clock and the preceding cursor.
  Dynamic programming compares concatenations and repeated blocks, with bounded
  search for live editing. The result is expanded again to verify every original
  timestamp before replacing the text. Shorter date prefixes omit months or
  years only when their rollover still selects the intended date.
* **Resolved:** confirming one candidate removes the others as one undoable action.
  Persistent selection modes use remappable `F1`/`F2`/`F3` keys for one occurrence,
  all events, and this and following. Calendar and search highlights show the
  exact affected union. Ctrl+click toggles selections and Ctrl+drag copies;
  paste stages a movable preview until click/Enter confirmation. The editor uses
  the same mode keys and segmented controls, including inside text fields.
  Day view uses `D`; copy and paste use Ctrl+C/Ctrl+V. A future edit splits canonical
  resources atomically, preserving earlier instances and assigning the future a
  new UID; one undo restores both. Clipboard copies have independent identities.
  The user confirmed that for recurring alternatives this keeps the chosen
  occurrence and removes all other choices, including future occurrences.
* **Resolved:** Nextcloud account discovery is implemented alongside generic
  CalDAV discovery. Password/app-password authentication is supported; OAuth
  remains later work. Shared account credentials now persist in the system
  keyring. Live Nextcloud discovery, initial pull, recurring writes, edits,
  exclusions, and cleanup passed the final review.
* **Resolved:** standard recurrence is preferred. Regular patterns become RRULE,
  finite irregular dates become RDATE, exclusions use EXDATE, and single-instance
  edits use RECURRENCE-ID. No import or sync horizon truncates stored rules.
* **Resolved:** event highlights show the affected selection; keyboard navigation
  follows actual events across empty days. A visible selection does not recenter
  the view.
* **Resolved:** conflict notices have an in-app/desktop/both preference. Event
  reminders remain out of scope.
* **Resolved:** regular controls and custom sequence input configure one schedule
  model. There is no separate raw RRULE editor. Recurring series can also be
  candidate alternatives.
* **Resolved:** demo mode explicitly disables remote connections and offers a
  switch to the persistent workspace; Sync status exposes errors and progress.
* Selective undo is guarded at event granularity. Field-level semantic merging is
  deferred; overwriting subsequent edits is never a safe default.

## Additional implemented refinements

* Versioned sequence text and anchor dates survive editing, history and sync.
  Obsolete syntax is not interpreted. Data without matching current text derives
  a verified expression from its canonical calendar, using an exact schedule
  document when necessary, without silently changing its events.
* Per-resource parse/quarantine handling keeps healthy events and candidate uploads
  moving when another event is malformed.
* Server colors are the default, with arbitrary stable manual overrides.
* Account-level keyring credentials and creation-calendar selection in the source list.

## Later milestones

Additional providers, packaged Linux desktop integration (e.g. Flatpak),
accessibility validation, and large real-world-calendar measurements.
The [release review](RELEASE_REVIEW.md) records completed automated/native/provider
checks, synthetic performance baselines, and the remaining limitations.
See [the synchronization design](SYNC.md) for the vdirsyncer evaluation and the
opt-in Nextcloud integration test.

## Reference documentation

* [Qt graphics and mouse interaction](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QWidget.html)
* [python-caldav documentation](https://caldav.readthedocs.io/stable/)
