# Lebacal implementation plan

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
2. **Calendar workspace:** day/week/month, overlap layout, calendar toggles, search,
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
* **Resolved:** custom schedules are sequential instructions after one fixed
  initial event. `+N` defaults to weeks; `d/w/m/y` specify relative units.
  `[HH:MM-]D.[M.[YYYY]]` supplies dates, and `HH:MM` can supply the next clock
  alone. Omitted components choose the strictly next matching instant; full
  dates are exact, including earlier dates. Date-only steps restore the initial
  clock; relative steps retain the preceding clock. Calendar month/year steps
  clip their day, and nonexistent daylight-saving times fail explicitly.
* **Resolved:** `N*step` and `N*(sequence)` repeat instructions, with nested groups
  and space/comma separators. The fixed event is included in the 500-event
  budget; repeats are bounded before execution and duplicate instants are
  deduplicated. For an initial 7 November 2026 at 09:00,
  `13. +1 18. 2. 3.2. +2` produces seven events through 17 February 2027.
* **Resolved:** calendar clicks reconstruct a compact equivalent custom sequence.
  Candidate steps include partial dates, exact dates, times and relative units;
  each is checked using the original event's clock and the preceding cursor.
  Dynamic programming compares concatenations and repeated blocks, with bounded
  search for live editing. The result is expanded again to verify every original
  timestamp before replacing the text. Shorter date prefixes omit months or
  years only when their rollover still selects the intended date.
* **Resolved:** confirming one candidate removes the others as one undoable action.
  Event actions default to one occurrence; `Ctrl` selects all events, and `Shift`
  selects this and following events. These configurable modifiers apply to edits,
  deletion, mouse movement/resizing, and `C`/`V`/`D` copy/paste/duplicate. `Alt+S`
  cycles scope in the editor while native text clipboard shortcuts remain intact.
  Day view uses `1`, freeing `D` for duplication. A future edit splits canonical
  resources atomically, preserving earlier instances and assigning the future a
  new UID; one undo restores both. Clipboard copies have independent identities.
  The user confirmed that for recurring alternatives this keeps the chosen
  occurrence and removes all other choices, including future occurrences.
* **Resolved:** Nextcloud account discovery is implemented alongside generic
  CalDAV discovery. Password/app-password authentication is supported; OAuth
  remains later work. Shared account credentials now persist in the system
  keyring. Live server testing is blocked by sandbox DNS.
* **Resolved:** standard recurrence is preferred. Regular patterns become RRULE,
  finite irregular dates become RDATE, exclusions use EXDATE, and single-instance
  edits use RECURRENCE-ID. No import or sync horizon truncates stored rules.
* **Resolved:** navigation badges appear only during selection. Day and typed
  event highlights show progress; a visible selection does not recenter the view.
* **Resolved:** conflict notices have an in-app/desktop/both preference. Event
  reminders remain out of scope.
* **Resolved:** regular rules, chosen dates and advanced RRULE input configure one
  schedule model. Recurring series can also be candidate alternatives.
* **Resolved:** demo mode explicitly disables remote connections and offers a
  switch to the persistent workspace; Sync status exposes errors and progress.
* Selective undo is guarded at event granularity. Field-level semantic merging is
  deferred; overwriting subsequent edits is never a safe default.

## Additional implemented refinements

* Versioned sequence text and anchor dates survive editing, history and sync.
  The previous parser remains available for migration only. Opening older data
  reconstructs equivalent new instructions from its functional RRULE/RDATE dates
  without rewriting the source; saving records the new notation. Timed dates and
  the fixed first event are preserved, with explicit errors if a sequence cannot
  represent unusual precision losslessly.
* Per-resource parse/quarantine handling keeps healthy events and candidate uploads
  moving when another event is malformed.
* Server colors are the default, with arbitrary stable manual overrides.
* Account-level keyring credentials and writable default-calendar selection.

## Later milestones

Provider testing, packaged Linux distribution (e.g.
Flatpak), accessibility audit, broader recurrence editing, network interruption
stress tests, and performance measurements on large real-world calendars.
See [the synchronization design](SYNC.md) for the vdirsyncer evaluation and the
opt-in Nextcloud integration test.

## Reference documentation

* [Qt graphics and mouse interaction](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QWidget.html)
* [python-caldav documentation](https://caldav.readthedocs.io/stable/)
