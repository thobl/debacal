# Event Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add interoperable saved reminders, optional background notification delivery, import policies, and close-to-tray behavior without slowing ordinary calendar use.

**Architecture:** Make alarms explicit canonical event data; keep delivery preferences and delivery state local. A single owner per database schedules reminders on a worker using persisted recurrence projections and an incremental source journal. Qt handles asynchronous desktop delivery and existing themed controls expose the feature.

**Tech Stack:** Existing Python >=3.11, PySide6 >=6.7,<7 with the existing Python-specific exclusion, SQLite, icalendar, python-dateutil, Jeepney >=0.9.0 on Linux, pytest. The transport uses the existing locked Jeepney client directly. PySide6 QtDBus lacks a public writer for the required unsigned Notify argument. A persistent asyncio connection receives desktop actions addressed to the original sender.

**Spec:** [Approved design](../specs/2026-11-21-notifications-design.md). Read both documents before implementation; the spec defines behavior beyond the focused examples below.

## Global Constraints

- Event notifications enabled, initially off. Delivery initially both; desktop sound initially follow desktop settings. Close window initially quit.
- New timed/all-day events initially have no saved reminders; manual imports initially Keep. Reusable defaults and calendar replacements contain relative reminders only.
- Catch-up window: last 24 hours; initially upcoming/ongoing events. First activation and re-enabling after deliberate mute start with future triggers.
- Dismissal and snoozing are local delivery state in this version. Snooze initially offers five minutes.
- Source disabled means no delivery. Eye-hidden and folded calendars still notify. Local policies produce no event history/outbox entries.
- Demo, screenshot, and automated tests do not deliver real reminders. Tests inject a controllable clock and notification service.
- No whole-calendar ICS scan or alarm queue build at startup with delivery disabled; no event scans/parsing on UI ticks. No query-result caches.
- Preserve unsupported alarm data; do not execute EMAIL/PROCEDURE, fetch sound attachments, add raw ICS editing, or add an unrelated calendar redesign.
- Keep existing dependency constraints and release version. Use local coherent commits with `git --git-dir=.git-local`; no push/release without a new request. Fixtures and published reports contain synthetic data only.

## Review Focus

1. Legacy raw alarms plus a remote upload template must not resurrect explicitly removed reminders or create false history/conflicts: Task 2.
2. A foreign detached event with no alarms, a generated Cut child, and a phone removing that child's alarms must remain distinguishable: Task 3.
3. Long offsets, sparse rules, post-end absolute alarms, and DST/calendar-day arithmetic must not be lost by an agenda-style overlap query: Tasks 1 and 6.
4. Restart, owner handoff, source mute races, and an unavailable/slow desktop service must neither replay alerts nor block the UI: Tasks 5–7.
5. Missing/disappearing tray, hidden Quit, acknowledged stdin imports, and failed deferred saves must preserve access to unfinished work: Task 11.

---

## File and responsibility map

New focused modules:

| Module | Responsibility |
| --- | --- |
| `debacal/alarms.py` | Immutable alarm values, identity, semantic normalization, trigger arithmetic |
| `debacal/alarm_ics.py` | Read/patch direct VALARMs and hydrate one legacy resource family |
| `debacal/alarm_series.py` | Scope edits, split ownership, generated exception alarm materialization |
| `debacal/reminder_preferences.py` | Local settings, eligibility boundaries, saved-event default/import transformations |
| `debacal/reminder_sources.py` | Read-only paged canonical/projection/journal access for workers |
| `debacal/reminder_schedule.py` | Compile one source and seek its next alarm triggers |
| `debacal/reminder_store.py` | Independent SQLite queue, ledger, snoozes, inbox, source cursors |
| `debacal/reminder_owner.py` | Database-identity delivery lock and takeover |
| `debacal/reminder_transport.py` | Async freedesktop notification capability discovery, delivery and actions |
| `debacal/reminder_controller.py` | Worker orchestration, due delivery, catch-up, local actions |
| `debacal/reminder_editor.py` | Reusable themed saved-reminder/template editor |
| `debacal/reminder_dialog.py` | Inbox and open/dismiss/snooze actions |
| `debacal/notification_preferences.py` | Lazy Notifications preference page |
| `debacal/tray.py` | Shared tray icon/menu and availability monitoring |

Modify existing model/ICS/store/recurrence code where each task names it. Keep `ui.py` as wiring, not the home of the scheduler, transport, or reminder widgets. New tests live in the named `tests/test_*` files; reusable synthetic factories live in `tests/alarm_support.py`. Extended measurements extend `scripts/benchmark_extended.py` and add `scripts/benchmark_reminders.py`.

## Shared values and test conventions

Task 1 defines frozen `Alarm` with fields `key: str`, `action: str`, `trigger: str | None`, `related: Literal['START','END'] = 'START'`, `repeat: int = 0`, `repeat_interval: str | None = None`, `uid: str | None = None`, `description: str = ''`, `acknowledged: datetime | None = None`, `raw_ics: str = ''`, `issue: str | None = None`. Triggers/intervals retain their signed iCalendar lexical representation; unsupported properties, alarm relationships, and malformed values survive in `raw_ics`. Keys use UID where present, otherwise a deterministic trigger identity plus duplicate ordinal; event title changes do not change identity.

Add `Event.alarms: tuple[Alarm, ...] | None = ()`: new events explicitly have no alarms. `Event.from_dict` treats an absent field in an old payload as `None` (legacy/unhydrated), without parsing. Define `AlarmEdit = tuple[Alarm, ...] | None` in `alarms.py`: `None` means untouched, including mixed selections. Never use a truthiness check to conflate these states. The prepared writer must emit the top-level alarms field even when empty; an old prepared record lacking it reads as None, while occurrence deltas inherit their base. This preserves backward readability without rebuilding the query index.

The test factory `alarm_event(*, alarms: tuple[Alarm, ...] | None = (), **changes) -> Event` returns a one-hour event on `2026-11-21T09:00:00Z`, calendar `c`, ID/UID `e`, title `Example`, with changes applied using `dataclasses.replace`. `BASE` denotes that UTC start. Each task supplies its own named scenario setup before the assertions shown below; no live calendars or services are fixtures.

For every task, first run its new test file expecting the missing interface or wrong behavior to fail, then run it again after implementation. Commands use `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q <files>`. A passing run must have no unexpected skips. Commit only that task's files, using the commit subject specified below.

## Before feature implementation: establish the baseline

- [ ] Use `using-git-worktrees` to isolate the implementation and retain an immutable source snapshot of product baseline `339afed`. Read `scripts/benchmark_extended.py` before invoking it. Set the injected/test delivery gate off for every automated workspace.
- [ ] Run the existing unchanged harness on that baseline, using the same installed dependencies for the final comparison:
  ```sh
  QT_QPA_PLATFORM=xcb xvfb-run -a .venv/bin/python scripts/benchmark_extended.py --source-root <baseline-root> --revision 339afed --rounds 5 --startup-runs 5 --output design/notification-ui-before.json
  ```
  `<baseline-root>` is the actual isolated baseline path created in the preceding step. Keep this exact harness copy and report for the final common-operation comparison; no feature-specific code is needed to measure the existing UI. Record its environment and workload parameters.

### Task 1: Structured alarms and interoperable ICS

**Files:** Create `alarms.py`, `alarm_ics.py`, `tests/alarm_support.py`, `tests/test_alarms.py`, `tests/test_alarm_ics.py`; modify `models.py`, `ics_resources.py`, `ics_fallback.py`, `prepared_storage.py`.

**Interfaces:** Produce `Alarm.to_dict() -> dict`, `Alarm.from_dict(value: dict) -> Alarm`; `alarm_semantic(alarms: tuple[Alarm, ...]) -> tuple`; `alarm_trigger(alarm: Alarm, start: datetime, end: datetime, *, repeat_index: int = 0) -> datetime`; `read_alarms(vevent: str, *, component_key: str) -> tuple[Alarm, ...]`; `write_alarms(vevent: str, alarms: tuple[Alarm, ...]) -> str`; `hydrate_alarms(event: Event) -> Event`. All times returned by trigger evaluation are UTC instants. Hydration resolves detached components from the master's retained VCALENDAR, not just a child's possibly empty raw text.

- [ ] Write `test_explicit_empty_roundtrip`, constructing a five-minute DISPLAY alarm and round-tripping through existing `export_ics`/`parse_ics`:
  ```python
  assert restored.alarms[0].trigger == '-PT5M'
  assert removed.alarms == ()
  assert Event.from_dict(original.to_dict()).alarms == original.alarms
  ```
  Add parameterized tests for START/END, positive/negative, absolute UTC, repeats, duplicate alarms, all-day/floating, DST fold/gap, and `-P1D` versus `-PT24H`. `test_malformed_neighbor_survives` asserts a malformed alarm is retained with an issue while a valid sibling and its event load; structurally broken VCALENDAR still fails normally. Preserve AUDIO/EMAIL/PROCEDURE, ACK/RELATED-TO, folded/unknown properties and unknown actions.
- [ ] Run `tests/test_alarms.py tests/test_alarm_ics.py`; confirm the intended failure.
- [ ] Implement the interfaces and explicit model/prepared-state encoding. Parse direct nested alarm blocks independently of event-property decoding. None remains None in old payloads/projections; do not add automatic raw parsing to `Event.from_dict`. Semantic comparison ignores internal keys/raw formatting but includes retained meaningful alarm metadata. Add a regression distinguishing a legacy prepared record with no field from a newly written explicit empty list.
- [ ] Rerun those files plus `tests/test_ics.py tests/test_prepared_persistence.py`; require PASS.
- [ ] Stage named files and commit: `Add structured reminders and lossless VALARM support`.

### Task 2: Canonical history, sync, and legacy hydration

**Files:** Modify `store.py`, `vdirsyncer_backend.py`, `ics_resources.py`, `event_clipboard.py`; create `tests/test_alarm_persistence.py`.

**Interfaces:** Consume Task 1. Produce `Store.get_events_with_alarms(identities: Iterable[str]) -> list[Event]` for worker/canonical editing paths only. Preserve existing store public mutation APIs. Normalize affected legacy snapshots through `hydrate_alarms` before canonical semantic comparisons, without rewriting unrelated rows or fast query projections.

- [ ] Write `test_remove_survives_remote_template_history_and_conflict`: hydrate a legacy raw-only alarm, explicitly remove it, save, patch against the original server template, undo/redo, and simulate the next remote comparison:
  ```python
  assert parsed_upload.alarms == ()
  assert undone.alarms[0].trigger == '-PT5M'
  assert redone.alarms == ()
  assert semantic(hydrated_legacy) == semantic(parsed_original)
  assert semantic(removed) != semantic(hydrated_legacy)
  ```
  Also test alarm-only outbox/history/conflict changes, preserved unknown metadata, legacy baselines/overrides, calendar transfer and clipboard staleness. Instrument ordinary query/reopen paths to assert zero alarm hydration calls.
- [ ] Run `tests/test_alarm_persistence.py`; confirm failure.
- [ ] Implement canonical hydration and alarm reconciliation **after** choosing the remote serialization template. Propagate semantic alarms through history restore, sync bases, conflict choices and accepted imports. Do not classify hydration alone as a user change; do not hold the GUI awaiting a canonical database writer.
- [ ] Rerun the new file plus `tests/test_vdirsyncer.py tests/test_calendar_edit_transfer.py tests/test_query_projection_storage.py`; require PASS.
- [ ] Commit: `Persist reminder edits through history and CalDAV sync`.

### Task 3: Recurrence scopes, generated children, and absolute ownership

**Files:** Create `alarm_series.py`, `tests/test_alarm_series.py`; modify `recurrence.py`, `prepared_recurrence.py`, `series_operations.py`, `store.py`, `selection.py`, `selection_export.py`, `event_clipboard.py`, `ics_range_policy.py`, `ics_resources.py`; adjust the two tests named below.

**Interfaces:** Produce `apply_alarm_edit(master: Event, edit: AlarmEdit) -> Event`; `split_alarms(original: Event, before: Event | None, after: Event | None) -> tuple[Event | None, Event | None]`; `materialize_alarms(master: Event, child: Event) -> Event`. Add keyword `alarm_edit: AlarmEdit = None` to scope-edit entry points (`Store.edit_selection/edit_occurrence/edit_series/edit_future` and recurrence/split helpers that propagate edits). Relative alarms follow actual instances; master absolutes belong once to the component, not each projection.

- [ ] Write `test_phone_removal_of_generated_cut_alarm_is_authoritative` and `test_alarm_scope_operation_matrix`. Parameterize Single/All/Following across title edit, alarm edit/removal, move, resize, copy, delete, confirm alternative, split and undo/redo:
  ```python
  assert foreign_detached.alarms == ()
  assert reparsed_phone_edited_child.alarms == ()
  assert len(original_uid_half_absolute_alarms) == 1
  assert future_half_absolute_alarms == ()  # unchanged reminder split
  assert explicitly_edited_future_master.alarms == requested_list
  ```
  Test sole surviving halves, RDATE PERIOD, generated/fold exceptions, different exception lists and stable child alarm UID/relationship remapping. Explicit new absolutes in All/Following appear only on the edited master. Read-only selection copy/export remains nondestructive and gives the copied component its intended reminders.
- [ ] Run `tests/test_alarm_series.py`; confirm failure.
- [ ] Implement centralized rules across all early returns/override projection paths. Foreign zero-VALARM overrides are explicit empty; regular/generated instances materialize intended relative alarms. Version generated Cut alarm verification without weakening the existing field allowlist: accept verified legacy no-alarm children and new exact UID-remapped lists; external removal/modification preserves a manual child. Update `test_detach_override_without_alarms_inherits_parent_alarm_group` and `test_one_selected_override_is_detached_with_effective_fields_alarms_and_new_identity` to the approved foreign-detached behavior; retain the regular-occurrence inheritance tests.
- [ ] Run the new file plus `tests/test_series_operations.py tests/test_import_selection.py tests/test_selection_export.py tests/test_range_policy_transport.py`; require PASS.
- [ ] Commit: `Make recurring reminder scopes and splits explicit`.

### Task 4: Local policy and pure default/import transformations

**Files:** Create `reminder_preferences.py`, `tests/test_reminder_preferences.py`; modify `ui_state.py`, `import_preparation.py`.

**Interfaces:** Produce frozen `CalendarReminderPolicy(mode: Literal['event','mute','replace'] = 'event', alarms: tuple[Alarm, ...] = ())`; frozen `ReminderPreferences(enabled=False, delivery='both', sound='default', missed='upcoming', close_behavior='quit', new_timed=(), new_all_day=(), import_mode='keep', import_alarms=(), calendars={})`, with literals `desktop/in_app/both`, `default/silent`, `upcoming/all/none`, `quit/tray`, `keep/remove/add_missing/replace`. Alarm-list fields are tuples of Alarm; `calendars` is a copied read-only `Mapping[str, CalendarReminderPolicy]`, created per instance. Produce `load_reminder_preferences(store: Store) -> ReminderPreferences`, `save_reminder_preferences(store: Store, value: ReminderPreferences, *, now: datetime) -> None`; `transform_import_reminders(report: ParseReport, mode: str, alarms: tuple[Alarm, ...]) -> ParseReport`; `count_reminder_sources(report: ParseReport) -> tuple[int, int]` (with alarms, total selected components).

- [ ] Write `test_defaults_policies_and_atomic_eligibility_boundary`: snapshot canonical events/history/outbox, change local policies, and assert:
  ```python
  assert before_canonical_state == after_canonical_state
  assert defaults.enabled is False and defaults.import_mode == 'keep'
  assert transformed_remove.events[0].alarms == ()
  assert transformed_add_missing.events[0].alarms == existing_alarms
  ```
  Test mixed detached selection, unsupported alarms counting as present, stable UIDs, relative-only template validation, timed/all-day defaults and canceled preference drafts. A saved off→on or mute→unmute transition stores `eligible_since == now` atomically with the policy in local settings.
- [ ] Run `tests/test_reminder_preferences.py`; confirm failure.
- [ ] Implement immutable JSON-safe settings in `UiState`, including policy generation and durable global/calendar eligibility timestamps under the same settings transaction. Keep them out of display Preferences and canonical calendar fields. Apply import transformations only after selection filtering; default changes never rewrite existing events.
- [ ] Rerun the new file plus `tests/test_ui_store_access.py tests/test_import_selection.py`; require PASS.
- [ ] Commit: `Separate saved-reminder defaults from local delivery policy`.

### Task 5: Read-only incremental scheduling inputs

**Files:** Create `reminder_sources.py`, `tests/test_reminder_sources.py`; modify `store.py`, `query_projection.py` only for narrow persistence/read hooks.

**Interfaces:** Produce `SourceChange(version: int, event_id: str, event: Event | None, indexed: IndexedSource | None)`. `ReminderSources(database: str)` owns a read-only SQLite connection; methods `revision() -> int`, `scan(*, after_id: str | None = None, calendar_id: str | None = None, limit: int = 128) -> tuple[SourceChange, ...]`, `changes(after: tuple[int, str], *, limit: int = 128) -> tuple[SourceChange, ...]`, `source_stamp(event_id: str) -> int | None`, `calendar_states() -> dict[str, tuple[bool, int, datetime]]`, `close() -> None`. Calendar tuple is enabled, transition generation, transition time. Each read batch pins a consistent SQLite snapshot; journal cursor is `(version,event_id)`.

- [ ] Write `test_changes_include_tombstones_and_interleaved_scan_writes`: page IDs while another connection edits/deletes/inserts, then replay the journal from the initial high water; assert reconstructed sources exactly equal final canonical sources. Assert one changed source hydrates/prepares at most that source, disabled→enabled between polls increments generation, and eye/fold changes do not. `test_reader_progresses_during_sync_writer` holds a canonical writer and verifies read-only snapshots still progress without worker startup writes.
- [ ] Run `tests/test_reminder_sources.py`; confirm failure.
- [ ] Implement keyset scans and indexed journal reads using `event_query_changes`; reuse persisted `IndexedSource` records individually, without loading all interval trees. Hydrate only canonical sources read by this worker. Add a narrow durable calendar-enabled transition table/trigger maintained with source enable writes, independent of event revision/outbox. Missing prepared records are prepared on this worker. Do not use `Store(query_index=False)` as a supposedly read-only polling connection.
- [ ] Rerun the new file plus `tests/test_sync_store_mode.py tests/test_query_projection_storage.py`; require PASS.
- [ ] Commit: `Expose incremental read-only reminder sources`.

### Task 6: Durable next-trigger scheduling and ownership

**Files:** Create `reminder_schedule.py`, `reminder_store.py`, `reminder_owner.py`, `tests/test_reminder_schedule.py`, `tests/test_reminder_store.py`, `tests/test_reminder_owner.py`.

**Interfaces:** Produce frozen `DeliveryKey(source_id: str, component_key: str, alarm_key: str, occurrence_key: str | None, trigger_utc: datetime, repeat_index: int)` and `DueReminder(key: DeliveryKey, calendar_id: str, source_revision: int, anchor_fingerprint: str, title: str, start: datetime | None, end: datetime | None, alarm: Alarm)`. Master-absolute occurrence key is None. Produce opaque serializable `AlarmSchedule`, `compile_schedule(change: SourceChange, policy: CalendarReminderPolicy, *, zone: tzinfo) -> AlarmSchedule`, `next_triggers(schedule: AlarmSchedule, after: datetime, until: datetime, *, limit: int = 128) -> tuple[DueReminder, ...]`, inclusive after/exclusive until.

`ReminderStore(database: str)` uses `<complete canonical database filename>.reminders.sqlite3`, or isolated memory for `:memory:`; methods `put_source(change: SourceChange, schedule: AlarmSchedule | None, *, cursor: tuple[int,str] | None = None) -> None`, `claim_due(now: datetime, *, limit: int = 128) -> tuple[DueReminder, ...]`, `next_wakeup() -> datetime | None`, `record_delivery(key: DeliveryKey, *, desktop_id: int | None, unread: bool = False, uncertain: bool = False) -> None`, `dismiss(key: DeliveryKey) -> None`, `snooze(key: DeliveryKey, until: datetime) -> None`, `inbox(*, limit: int = 100, offset: int = 0) -> tuple[InboxEntry, ...]`, `mark_read(keys: Iterable[DeliveryKey]) -> None`, `unread_count() -> int`, `close() -> None`. Frozen `InboxEntry` contains `reminder: DueReminder`, `status: Literal['pending','delivered','uncertain','snoozed','dismissed']`, `unread: bool`, `snoozed_until: datetime | None`; paginate without discarding undisposed entries. Claims persist an inbox/pending state before transport. `ReminderOwner(database: str).acquire() -> bool` / `.release() -> None` use canonical DB identity and a distinct private lock.

- [ ] Write `test_sparse_far_offset_and_absolute_are_not_agenda_queries`: daily, every-20-years, moved/Cut, duplicate-date and post-end examples, including an occurrence years away with a long advance trigger due now. Assert every expected DeliveryKey exactly once, independent of viewed dates. Assert nominal-day/DST results from Task 1, END anchors using actual Cut bounds, master absolute once, finite repeats and imported ACK/snooze relations. `test_restart_claim_and_owner_handoff` asserts no replay of delivered keys, interrupted claims become inspectable uncertain entries, one owner across symlinks/new instances, independent profiles and takeover after exit.
- [ ] Run the three new test files; confirm failures.
- [ ] Implement worker-only compiled source/trigger state, indexed due-time queue, and persistent next refill deadlines. Use prepared rule seeking plus separately indexed explicit exceptions; never horizon-drop sparse rules, expand from DTSTART, or scan all sources per tick. Update only affected sources, persist cursor with their changes, rebind floating/all-day queues when local zone changes. Delivery keys exclude event title; alarm/anchor fingerprints cancel stale snoozes. Preserve only bounded batches in memory. Lock acquisition is nonblocking; release on shutdown, retry takeover without a canonical write lock.
- [ ] Rerun those files plus `tests/test_prepared_recurrence.py tests/test_single_instance.py`; require PASS.
- [ ] Commit: `Schedule durable reminders with one delivery owner per profile`.

### Task 7: Background controller and asynchronous delivery

**Files:** Create `reminder_transport.py`, `reminder_controller.py`, `tests/test_reminder_controller.py`, `tests/test_reminder_transport.py`; extend `tests/alarm_support.py`.

**Interfaces:** Produce `NotificationTransport(QObject)` with `send(due: DueReminder, *, calendar_name: str, profile_label: str, silent: bool) -> None`; signals `delivered(object, int)`, `failed(object, str)`, `action(object, str)`, `availabilityChanged(bool)` (object = DeliveryKey, action = open/dismiss/snooze). Add injected `Clock.now() -> datetime` and matching fake transport. Produce `ReminderController(QObject)` constructor `(database: str, *, clock: Clock, transport: NotificationTransport, delivery_allowed: bool)`; methods `start()`, `reload_settings()`, `dismiss(key: DeliveryKey)`, `snooze(key: DeliveryKey, duration: timedelta)`, `stop()`; signals `inboxChanged()`, `issueChanged(str | None)`, `activate(object)` (DueReminder).

- [ ] Write `test_catchup_mute_restart_and_slow_transport`: use fake UTC clock/service, make a notification call remain pending while edits/preferences/calendar navigation complete. Assert enabled delivery modes, the 24-hour boundary, overdue grouping per occurrence, future-only first enable, mute/re-enable gaps, late discovery, restart/resume, and upcoming eligibility for master absolutes. Assert disabled startup makes zero source scans/queue builds; hidden/folded sources deliver and disabled sources do not. Text-only changes preserve snoozes; alarm/anchor changes, deletion and mute cancel them. Moving a delivered trigger future allows its new key; moving past does not replay it. A failing/malformed neighbor does not suppress valid reminders.
- [ ] Run `tests/test_reminder_controller.py tests/test_reminder_transport.py`; confirm failures.
- [ ] Implement worker scheduling with bounded journal/queue work and Qt signal handoff. Revalidate source and policy generations before emission; never emit while disabled or after stop. Use asynchronous Jeepney D-Bus calls, capabilities/actions and silent hint; retain inbox entries and one recoverable availability issue on missing service. Payload contains title/time/calendar/profile, never event notes. Desktop-only has no unread badge, In-app-only has no native notification, Both has both; no mode steals focus. Persist local dismiss/snooze/repeat cancellation and policy boundaries; do not retry uncertain external delivery automatically.
- [ ] Rerun those files plus `tests/test_ui_database.py tests/test_dialog_database_contention.py`; require PASS; no real D-Bus notifications.
- [ ] Commit: `Deliver reminders asynchronously with durable local actions`.

### Task 8: Saved-reminder editor and event dialog

**Files:** Create `reminder_editor.py`, `tests/test_reminder_editor.py`; modify `event_dialog.py`, `ui.py`.

**Interfaces:** Produce `ReminderEditor(QWidget)` constructor `(*, templates_only: bool = False, parent: QWidget | None = None)`, methods `set_alarms(alarms: tuple[Alarm, ...], *, mixed: bool = False)`, `alarms() -> tuple[Alarm, ...]`, `is_modified() -> bool`, signal `changed()`. EventDialog produces `alarm_edit() -> AlarmEdit`; its draft snapshot/restore carries alarm state and modification intent. MainWindow passes that intent to Task 3 store calls.

- [ ] Write `test_notes_reminders_scope_and_default_lifetime`: assert Notes initially selected, shared section height, chosen new-event defaults, and scope shortcuts still work. Assert no alarm edit on unrelated title/save, `()` on explicit remove, mixed lists remain untouched, switching scope restores its draft, and calendar change keeps saved alarms but refreshes local-rule indication. Test default timed↔all-day changes only while reminders untouched, picker handoff restoration, absolute support and series-wide absolute edit restriction.
- [ ] Run `tests/test_reminder_editor.py`; confirm failure.
- [ ] Implement themed Notes/Reminders choice in the existing Notes space. Reuse current duration/date-time controls, show unsupported imported entries inspectably without raw editing, keep tab order and Ctrl+Enter/Esc conventions. Hydrate the selected canonical family asynchronously before opening; revalidate snapshot on save. Add no permanent rows or automatic default reapplication on move/copy/edit.
- [ ] Rerun the new file plus `tests/test_dialog_workflow.py tests/test_event_dialog_schedule.py tests/test_dialog_shortcuts.py`; require PASS.
- [ ] Commit: `Edit saved reminders in the event dialog`.

### Task 9: Import reminder review and immutable choices

**Files:** Modify `import_dialog.py`, `import_preparation.py`, `ui.py`; create `tests/test_import_reminders.py`.

**Interfaces:** Consume Task 4 transforms and Task 8 editor. Extend frozen `PreparedImport` with `reminder_mode: str = 'keep'`, `reminder_alarms: tuple[Alarm, ...] = ()`; `with_reminders(mode: str, alarms: tuple[Alarm, ...]) -> PreparedImport`. Preserve that snapshot through selection/remap/deferred writes/retries.

- [ ] Write `test_selected_import_remove_survives_retry_and_caldav`: import a master/exception selection, choose Remove, queue behind a writer, change preferences, fail once, then retry the accepted snapshot:
  ```python
  assert accepted.reminder_mode == 'remove'
  assert all(event.alarms == () for event in imported_components)
  assert 'BEGIN:VALARM' not in uploaded_selected_resource
  ```
  Parameterize Keep/Add when missing/Replace, existing UID update, file/stdin/IPC, subset selection and infinite series. Count selected components without expanding recurrences; subscription refresh and ordinary sync must bypass manual defaults.
- [ ] Run `tests/test_import_reminders.py`; confirm failure.
- [ ] Implement compact selected-summary/policy UI; expose the shared relative-only editor only when needed. Transform filtered components in the worker immediately before the accepted database write, preserving immutable intent and UID identity.
- [ ] Rerun the new file plus `tests/test_import_selection.py tests/test_import_preparation.py tests/test_import_dialog.py`; require PASS.
- [ ] Commit: `Review and apply saved reminders during calendar import`.

### Task 10: Notification preferences, calendar overrides, and inbox

**Files:** Create `notification_preferences.py`, `reminder_dialog.py`, `tests/test_notification_preferences.py`, `tests/test_reminder_dialog.py`; modify `preferences_dialog.py`, `calendar_sources.py`, `dialogs.py`, `ui.py`.

**Interfaces:** Produce `NotificationPreferencesPage(QWidget)` constructor `(value: ReminderPreferences, parent: QWidget | None = None)`, `.value() -> ReminderPreferences`, signal `testRequested()`. Produce `ReminderDialog(QDialog)` constructor `(controller: ReminderController, parent: QWidget | None = None)` using Task 6 inbox rows; existing activation signal handles Open.

- [ ] Write `test_preference_apply_cancel_and_calendar_policy_only_changes`: assert all approved defaults, lazy page construction, Apply/Save/Cancel, relative-only templates, availability text, and that policy-only changes make zero canonical writes or sync requests. Test override access from calendar context editing and read-only sources. `test_inbox_actions_and_absolute_navigation` asserts unread semantics, dismiss/repeat isolation, editable five-minute snooze, occurrence reveal, master-absolute All scope, and canonical editor fallback when no occurrence exists.
- [ ] Run the two new files; confirm failures.
- [ ] Implement grouped delivery/defaults/missed-reminder/background sections in the existing preferences sidebar, reusing Task 8 editor. Calendar Sources handles policy drafts separately from source sync changes. Add one compact themed inbox access point; route availability issues through existing footer `report_error`/`dismiss_error`. Extract shared event reveal behavior from search navigation rather than fabricating a search result. Native actions and inbox actions use the same controller.
- [ ] Rerun new files plus `tests/test_preferences_apply.py tests/test_preferences_lazy_pages.py tests/test_calendar_sources.py`; require PASS.
- [ ] Commit: `Expose notification settings and reminder inbox`.

### Task 11: Tray lifecycle and shared activation/shutdown

**Files:** Create `tray.py`, `tests/test_tray_lifecycle.py`; modify `ui.py`, `ui_database.py`, `__main__.py`, `single_instance.py`.

**Interfaces:** Produce `TrayController(QObject)` constructor `(window: QWidget, *, profile_label: str)`, methods `available() -> bool`, `set_unread(count: int)`, `close()`; signals `showRequested()`, `quitRequested()`, `availabilityChanged(bool)`. Produce `MainWindow.restore_workspace() -> None`, `request_quit(*, force: bool = False) -> None`; reuse restore for normal launch/import/notification/tray activation. Retain existing request acceptance result semantics and apply admission checks before acknowledging new IPC work.

- [ ] Write `test_hidden_quit_waits_for_reviewed_writes_but_restores_unreviewed_stdin`: fake a tray and accepted stdin request; assert Quit cancels/restores for outstanding human review, reviewed writes drain before real application quit, write failure cancels quit with visible recovery, and admitted requests survive. Also test normal close-to-quit default, close-to-tray without teardown, active modal/picker, tray missing/disappearing, maximized restoration, new launch/file import, owner handoff, demo/screenshot forced quit, conflict action routing, and a hidden window actually ending the Qt loop on explicit Quit.
- [ ] Run `tests/test_tray_lifecycle.py`; confirm failures.
- [ ] Implement a shared tray with Show/Quit and profile/unread indication. Hide before entering shutdown only when configured, tray available and no inaccessible modal interaction. Separate forced-quit intent from close requests across deferred callbacks; bound/reject new IPC admissions during actual shutdown, restore access on review/failure. Call application quit after successful teardown; keep existing network shutdown nonblocking. Gate actual reminder delivery for demo/screenshot and inject disabled/fake services for tests.
- [ ] Rerun new file plus `tests/test_single_instance.py tests/test_profiles.py tests/test_ui_database.py`; require PASS.
- [ ] Commit: `Keep Debacal available in the tray with safe explicit quit`.

### Task 12: Integration, bounded-work checks, performance, and documentation

**Files:** Create `tests/test_reminder_integration.py`, `tests/test_reminder_performance.py`, `scripts/benchmark_reminders.py`; extend `scripts/benchmark_extended.py`; update concise `README.md` usage and `SYNC.md` standard/local distinction. Keep workflow timing benchmarks excluded by the existing `local_performance` marker.

**Interfaces:** Consume previous tasks. Benchmark CLI: `.venv/bin/python scripts/benchmark_reminders.py --sources 20000 --rounds 5 --output design/notification-performance.json`; output synthetic workload size, environment, source revision and per-operation samples/mean/max/event-loop gaps. Add reminder enable/index build, idle tick, single-source update, due batch, Preferences, event alarm editing, import policy and tray restore to extended interaction coverage.

- [ ] Write deterministic end-to-end tests: imported email alarm → Remove → save → undo/redo → remote-template upload → download; recurrence scopes/splits → another-instance owner delivery; disabled startup and source changes under a held sync writer. Assert no raw parse/read-all calls on disabled startup or idle tick; one-source update compiles only that source; queue query uses due index and bounded batches. Use generous completion deadlines solely for deadlock detection, not brittle CI speed thresholds.
- [ ] Run `tests/test_reminder_integration.py tests/test_reminder_performance.py -m 'not local_performance'`; confirm the intended unmet checks fail before filling integration gaps.
- [ ] Add the local benchmark, comparing final common UI/startup/import operations against the preimplementation baseline using the preserved original harness; report new-only reminder operations without invented baseline speedups. Include cold profiles/random windows and mixed operation orders, repeats, all means/maxima, index memory/disk, startup disabled/enabled and sync-held-writer stress. Use fake delivery, not thousands of native notifications. Investigate regressions; retain no query-result caches.
- [ ] Run full correctness: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q -m 'not local_performance' --strict-markers --tb=short --maxfail=5`. Run final extended measurements: `QT_QPA_PLATFORM=xcb xvfb-run -a .venv/bin/python scripts/benchmark_extended.py --rounds 5 --startup-runs 5 --output design/notification-ui-after.json`; run the reminder CLI above under the same Xvfb backend. Review screenshots in `design/` for Notes/Reminders, imports, Preferences, inbox and unavailable/tray states. No WM-specific workaround.
- [ ] Document limitations (app must run, optional tray, remote-only alarm types, local snooze/delivery rules) and performance results; run `git --git-dir=.git-local diff --check`, inspect staging for private data, commit `Validate and document event reminders and background delivery`. Finish with fresh whole-change review and verification before claiming completion; do not publish automatically.

## Execution order and gates

Run the baseline preflight before Task 1 code. Execute Tasks 1–3 first: correctness of saved alarms is prerequisite to UI and delivery. Tasks 4–7 establish policy and scheduling; Tasks 8–10 expose the verified data paths; Task 11 enables background residency. Task 12's final comparisons/review run last. UI tasks can be delegated only after their consumed interfaces exist, with focused ownership to avoid concurrent edits in `ui.py`.

For each task use its fail/pass/commit cycle and a fresh task review if that execution method is chosen. A milestone that lacks its persistence/sync tests is not ready to expose to users. If implementation evidence requires changing an approved visible workflow, ask before changing it; routine internal choices stay with the implementer.

## Self-review record

Coverage checked against every section of the approved design: saved data/standards (1–3), defaults and local policies (4), efficient inputs/scheduling (5–7), event/import/preferences/inbox (8–10), tray/IPC/shutdown (11), integration/performance (12). Review Focus cases have owning named tests. Shared APIs distinguish untouched, legacy and explicit-empty values, all cross-task types are defined, and legacy hydration is kept off ordinary query paths. This is an implementation plan, not approval to begin implementation.
