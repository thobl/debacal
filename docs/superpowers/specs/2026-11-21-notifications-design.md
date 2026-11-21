# Event reminders and notification delivery

Status: draft for user review; implementation is not approved.

## Intent and agreed requirements

Provide flexible reminders without expanding the normal event form or requiring
people to use notifications. Support reminders authored in Debacal and those in
imported or synced calendars.

The user confirmed:

- Notifications can be completely disabled in Debacal.
- New-event defaults and manual-import defaults are separately configurable.
- Import review reports existing reminders and supports removing or adding them.
- Event edits and import choices change the saved event, including its synced
  alarms. Importing an invitation into CalDAV with its reminders removed must
  upload an event without those reminders, so another client receives that edit.
- Calendar-wide delivery overrides are local preferences. They can, for example,
  ignore saved reminders and notify five minutes before each event.
- Closing the window can optionally keep Debacal running in the system tray.
  Users can change this behavior in Preferences.
- The existing UI style and interaction patterns should be retained. Sharing the
  event editor's Notes space with reminder controls is the preferred starting point.

The defaults and detailed behaviors below are proposals for approval, rather than
additional requirements already agreed by the user.

## Approaches

| Approach | Trade-off | Decision |
| --- | --- | --- |
| Deliver only while the calendar window is open | Smallest lifecycle change, but omits the requested tray option | Rejected |
| Run a separate reminder daemon | Could run independently of the application, but adds service installation, profile ownership, and shutdown coordination | Unnecessary for the requested behavior |
| Keep the existing process running with an optional tray mode | Reuses sync, profile activation, and the application's existing worker infrastructure | Recommended implementation of the user's choice |

Use the existing Python/Qt application. Store standard event alarms separately
from local delivery preferences. A hidden window continues syncing and scheduling;
there is no separate service or automatic login startup in this feature.

## Three separate layers

### Saved event reminders

Reminders are part of the canonical event and serialize as standard `VALARM`
components. Adding, changing, or removing them participates in normal save,
undo/redo, conflict detection, export, and CalDAV upload. An empty list explicitly
means no saved alarms; it must not silently inherit the server template's alarms.

Imported and synced alarms already survive as retained ICS text, but are absent
from the event model. Raw-only changes are insufficient: history and sync semantic
comparisons omit raw text, and upload patching uses the server's template. Add
structured alarm intent, with preservation of unsupported alarm properties and
components, and reconcile the actual `VALARM` blocks during serialization.

Retain alarm identity, trigger semantics, descriptions, repeats, acknowledgements,
and unknown metadata. Unknown or malformed alarms must not make an otherwise
valid event disappear. Surface a concise issue for a malformed or unknown
time-based alarm that cannot be delivered, retaining its data for other clients.
Recognized remote-only actions remain labeled and inspectable without repeated
error alerts.

### Creation and import defaults

Defaults initialize or transform saved events; they are not permanent local
event-specific overrides. Later preference changes do not rewrite existing events.

Proposed initial defaults preserve existing behavior:

- New timed events: no saved reminders.
- New all-day events: no saved reminders, configured independently of timed events.
- Manual imports: keep the reminders supplied by the file.

Users can configure one or multiple default reminders, including five minutes
before the start. Existing-event editing, moving, copying, and syncing do not
reapply creation defaults. Changing the destination calendar preserves the draft's
saved reminders and updates the local-override indication. Changing a new draft
between timed and all-day uses the corresponding default only while its reminders
have not been edited. Reusable creation/import defaults and calendar replacement
rules contain relative reminders; absolute alarms are event-specific.

Manual import supports these policies:

| Policy | Effect on the saved selection |
| --- | --- |
| Keep | Preserve all supplied alarms |
| Remove | Remove all alarms, including those on selected detached occurrences |
| Add when missing | Add the chosen reminder list only where the effective event/occurrence has no alarms |
| Replace | Replace existing alarm lists with the chosen list |

An unsupported alarm action still counts as an existing alarm for Keep, Remove,
and Add when missing. The user can choose Replace to request a usable new list.

Apply the policy to the accepted, filtered import snapshot before the database
write. Preserve event UIDs and the existing matching-update behavior. Carry the
choice through deferred imports and retries. Command-line files, stdin, and
desktop file opening all use this same review flow.

Subscription refresh and ordinary CalDAV sync preserve remote event alarms;
they do not apply the manual-import default. Local calendar delivery rules cover
those sources without repeatedly modifying them.

### Local delivery rules

Precedence is:

1. Global event notifications disabled: deliver nothing.
2. Calendar disabled in the source configuration: deliver nothing.
3. Calendar delivery mode: use saved event reminders, mute the calendar, or replace
   them locally with a specified reminder list.

The eye button and folding a calendar into the additional-calendars menu do not
mute its reminders. Calendar-wide overrides work for writable and read-only
sources. They are keyed by stable calendar ID in local profile settings, produce
no event outbox entries, and do not trigger unnecessary synchronization.

The event editor shows when a local override affects delivery. Editing its saved
reminders remains possible; those edits affect other clients even when this
Debacal installation follows a different local rule.

Global off affects event-reminder delivery, not the ability to edit saved alarms.
Existing sync-conflict alerts remain a separate setting.

## Event editor, import review, and preferences

Use the existing Notes area, keeping its height and opening on Notes:

```text
Notes | Reminders

Reminders:
  5 minutes before start                  [remove]
  1 day before start                      [remove]
  [+ Reminder]
```

Follow the application's configured button style, fonts, keyboard handling, and
spacing. No extra always-visible reminder rows above the Notes area. Offer start-
and end-relative reminders and an absolute date/time using existing duration and
date/time controls. All-day labels identify the start/end-of-day anchor clearly.
Show imported settings faithfully, including unsupported types, without exposing
raw ICS editing as the normal workflow.

Use the existing selection scopes: one event, all events, and this and following.
An untouched mixed selection preserves its individual reminders. Only an explicit
reminder change applies a new list to the selected scope; changing a title or
time alone preserves the relevant reminder intent.

Import review gains a compact Reminders section showing how many selected events
or series contain alarms and the chosen import policy. Counts reflect the selected
snapshot, including selected exceptions, without expanding infinite series just
to count dates. Advanced controls appear when Add or Replace needs a reminder list.

Add a Notifications category in Preferences with:

- Event notifications enabled, initially off to avoid unexpectedly activating
  alarms retained by previous versions.
- Delivery: desktop, in-app, or both; initially both when enabled.
- Desktop sound: follow desktop settings or silent; initially follow desktop settings.
- Separate defaults for new timed events, new all-day events, and manual imports.
- Missed reminders: upcoming/ongoing events, all recent reminders, or none;
  initially upcoming/ongoing events.
- Close window: quit or keep running in the tray; initially quit.
- A Test notification action and clear delivery/tray availability information.

Calendar Sources holds each calendar's local delivery mode and replacement list.
Provide access from the calendar's existing context/edit flow. Apply/Save/Cancel
semantics match the rest of Preferences.

An in-app reminder list retains reminders until dismissed and offers open event,
dismiss, and snooze. Use one compact access point rather than a new permanent
sidebar section. Snooze accepts a duration, with five minutes as its initial
choice. Desktop actions are offered when the notification server supports them;
the same operations remain available in the app.

All delivery modes record reminders in that list. In-app delivery adds an unread
badge to its access point and, when hidden, to the tray indication; it does not
raise the window or open a modal dialog. Desktop delivery sends the native alert
without an additional in-app badge. Both does both. In Desktop-only mode a missing
desktop service produces an actionable availability issue and a retained list
entry, rather than silently changing the chosen presentation. The sound setting
governs desktop sound; In-app-only does not play a separate system sound.

Notifications initially show event title, time, and calendar. Notes remain in
the event editor rather than being copied into desktop notifications.

## Standards and recurring events

Support standard relative triggers before or after event start/end, absolute UTC
triggers, multiple alarms, and finite `REPEAT`/`DURATION` alarms. Preserve signed
duration intent, including nominal days versus elapsed hours through DST. Use
actual occurrence bounds for moved, detached, floating, all-day, and Cut events.
Do not reuse the event-duration validator's nonnegative-only representation.

Newly authored reminders use DISPLAY alarms. Imported DISPLAY and AUDIO alarms
produce local reminders; AUDIO can request the system's default notification
sound, subject to the user's sound preference. Preserve external sound attachment
metadata without downloading it. EMAIL, legacy PROCEDURE, proximity, and unknown
actions remain preserved and inspectable but are not executed by this feature.

On the wire, detached events have authoritative alarm lists. A foreign detached
event containing no `VALARM` means no alarms. When Debacal generates a move or
Cut exception that should retain relative alarms, serialize those alarms into
that exception. Any internal inheritance marker is valid only while the exported
alarm list still matches its expected content. A phone deleting that list must
remain an explicit removal on the next Debacal import.

Generated Cut recognition must understand verified generated alarm lists rather
than treating every nested alarm as a manual event. Preserve externally changed
children and distinguish older verified generated children from the new form.
Materialized child alarms receive stable child-specific UIDs, including updated
internal alarm relationships where necessary.

Changing all reminders applies to the entire chosen scope, including its existing
exceptions. Changing unrelated fields preserves exception-specific alarms.

An absolute alarm belongs to its component and fires at its fixed UTC time,
plus explicit repeats; it is not a new alarm for every recurrence. On a split,
keep a master absolute alarm on the original-UID half, or the sole surviving
half. Do not shift its timestamp or duplicate it onto generated children or the
future half. Existing detached absolute alarms follow their occurrence. This
split ownership rule is a Debacal policy, not a requirement imposed by iCalendar.

An explicit reminder-list edit in All or Following scope assigns any newly
added absolute alarms once to the edited master component, not once to each
exception. Relative alarms apply throughout the chosen scope. Old exception
alarm lists are replaced only when the user explicitly changes reminders.
For Following, the new absolute alarm belongs to the new future master; the
unchanged-alarm split rule above governs alarms the user did not edit. In Single
scope, a master absolute alarm is shown as series-wide and requires switching
scope to edit; a newly added occurrence absolute alarm belongs to that exception.

A master absolute alarm opens the series editor in All scope. Its calendar
navigation target is the first occurrence ending after the trigger time, or the
last earlier occurrence. If no occurrence exists, open the canonical event editor
without inventing a date. For upcoming/ongoing catch-up eligibility, that series
qualifies if it has any occurrence that has not ended at delivery time. On-time
absolute alarms always fire at their explicit time, including post-event alarms.

Dismissal and snoozing are local delivery state in this version. Preserve and
respect imported standard acknowledgement/snooze metadata. Synchronizing new
dismissal/snooze actions across devices is outside this version's scope.
Dismiss acknowledges that alarm for the occurrence and cancels its remaining
repeats. Distinct alarms and future occurrences stay scheduled. Snooze replaces
those remaining repeats with one local alert at the chosen later time.

## Delivery, restarts, and performance

Use the freedesktop D-Bus notification service for Linux desktop delivery,
independently of system-tray availability. Discover its capabilities and use
asynchronous calls. When desktop delivery is unavailable, retain the reminder
in the in-app list and show one actionable issue through the existing footer.
Notification clicks restore Debacal and reveal the relevant occurrence.

Keep scheduling and transport separate. Maintain compact alarm data and a
next-due queue/index, updating only affected events or calendar policies. Read
and parse old raw alarms on a bounded worker migration path; do not reparse ICS,
expand entire recurring series, or scan every event on each UI tick. Preserve
the current fast event-query path and avoid database waits on the UI thread.
With delivery disabled, do not build an alarm queue or scan all retained ICS at
startup solely for this feature. Hydrate an older event's alarms when needed for
editing or background sync comparison, and initialize scheduling on a worker
when enabled. The exact migration/storage mechanism is left to the builder.

There is one delivery owner per canonical profile/database, including when
`--new-instance` is used. Other windows can edit reminders; they must not produce
duplicates. A surviving instance can take ownership after the previous owner
exits. Separate profiles are independent; identify profiles in tray/notification
presentation where needed. Demo, screenshot, and automated test runs do not
deliver real reminders. Tests inject a controllable clock and notification service.

Persist delivery identity and state so normal restart, refresh, sync, and owner
handoff do not replay delivered alarms. Keys distinguish occurrences, alarms,
triggers, and explicit repeats. Isolate a failing event/alarm or unavailable
notification service so other reminders continue. Do not promise transactional
exactly-once delivery across a process crash and an external desktop service;
retain uncertain deliveries for inspection in the in-app list.

After ordinary quit/start or system resume, the proposed catch-up window is the
last 24 hours. The default delivers eligible overdue reminders only for events
that have not ended; group multiple overdue triggers for the same occurrence.
The other modes include ended events in that window or deliver no catch-up.
The first activation and re-enabling after a deliberate mute start with future
triggers, rather than replaying the time during which notifications were off.
Record a durable eligibility boundary for global and calendar enable/mute changes.
Already-past triggers first discovered by a late sync, import, or edit use the
same missed policy only within the last 24 hours and after that boundary. Initial
backfill and deliberately muted periods do not generate a catch-up burst.

If an edit or sync moves an already delivered trigger into the future, its new
future trigger may notify again. Moving it into the past does not replay it.
Unrelated text/metadata changes preserve delivery state. Snoozes are local and
survive restart; deletion, muting, or a change to their alarm/anchor cancels them
and re-evaluates the normal saved/local rule. Title/location-only changes preserve
the snooze. Dismissed/delivered repetitions otherwise remain excluded.

## Tray lifecycle

When close-to-tray is enabled and a tray is available, closing hides the main
window without entering shutdown. Sync, reminders, pending saves, credentials,
and the single-instance listener remain live. Keep an active modal editor,
import review, or occurrence picker accessible instead of hiding an unfinished
interaction.

The normal tray icon has Show Debacal and Quit Debacal actions. Show and an
already-running `debacal` launch use the same restore path, including file/stdin
imports and active modal dialogs. Distinguish event-reminder activation from the
existing conflict notification action.

If no tray is available, the proposed fallback is normal close-to-quit behavior,
clearly reported in Preferences. Never leave an inaccessible hidden process.
If the tray disappears while hidden, restore the window. Tray residency is
independent of the global event-notification switch.

Explicit Quit bypasses tray hiding, drains user-confirmed imports and database
commands, then tears down and explicitly exits the active application's loop.
If acknowledged file/stdin requests still require human import review, cancel
the quit attempt, restore the window, and let the user finish or cancel those
reviews before quitting again. Preserve queued stdin in memory during this flow;
this feature does not add a durable import spool or silently discard the request.
Closing an already hidden Qt window alone is not sufficient. If a pending save
fails, restore the window and expose recovery. Bound or reject new IPC work once
actual shutdown begins; do not lose requests already acknowledged as accepted.
Network sync shutdown retains its existing nonblocking cancellation behavior.
Demo handoff, screenshot completion, and final cleanup always use real shutdown.

## Acceptance and implementation freedom

Required checks include:

- Invitation with alarms → import Remove → save and undo/redo → CalDAV upload
  and download: alarms remain removed, including detached cases. The phone
  receives the event without those saved alarms; its own independent local
  notification rules are outside Debacal's control.
- Keep/Add/Replace import behavior, mixed selections, existing UID updates,
  deferred imports, and retry snapshots preserve the selected policy.
- Reminder-only changes survive stale edit detection, history, conflicts, and
  remote-template upload patching; unsupported metadata survives ordinary edits.
- Global/calendar delivery changes leave canonical ICS, event history, and
  the event outbox unchanged. Hidden/folded calendars still notify.
- Editing, moving, copying, deleting, splitting, and undoing recurring events
  preserve the stated alarm scope, including explicit empty exception lists.
- DST folds/gaps, nominal-day offsets, all-day/floating events, long offsets,
  absolute alarms, repeats, sparse series, and Cut exceptions round-trip.
- Disable/re-enable, restart/resume, snooze, sync races, owner handoff, transport
  failures, and a malformed neighboring alarm do not produce silent global failure.
- Tray hide/restore/Quit, missing/disappearing tray, pending saves/imports,
  relaunch with file/stdin, and isolated profiles behave as specified.
- Large alarm sets and imports exercise cold queries and changed-source updates,
  measure startup and worst-case UI interactions, and verify bounded work.
  Extended timings remain local; CI uses deterministic correctness/work bounds.

The builder chooses the precise alarm classes, migration layout, queue/index
implementation, service/lease mechanism, thread boundaries, and themed controls
within these contracts. Prefer existing workers, sidecar preferences, recurrence
projection, and test infrastructure. No unrelated calendar redesign is included.

## References

- [RFC 5545: alarm components and triggers](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.6.6)
- [RFC 9074: alarm identity, acknowledgement, and snooze](https://www.rfc-editor.org/rfc/rfc9074.html)
- [freedesktop Desktop Notifications](https://specifications.freedesktop.org/notification/latest-single/)
- [Qt system tray example](https://doc.qt.io/qt-6/qtwidgets-desktop-systray-example.html)
- Existing code: `event_dialog.py`, `import_dialog.py`, `import_preparation.py`,
  `models.py`, `ics_resources.py`, `ics_range_policy.py`, `store.py`,
  `prepared_storage.py`, `preferences_dialog.py`, `calendar_sources.py`,
  `ui.py`, `__main__.py`, and `single_instance.py`.
