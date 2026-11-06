# Calendar synchronization

## Default backend: vdirsyncer

CalDAV synchronization uses **vdirsyncer 0.21** for remote storage, conditional
writes and file synchronization. The app bridges its SQLite events into
filesystem/CalDAV pairs, password commands and conflict command hooks.
([Configuration](https://vdirsyncer.pimutils.org/en/stable/config.html),
[password commands](https://vdirsyncer.pimutils.org/en/stable/keyring.html))

The Python CalDAV transport remains an explicit diagnostic/test path; it is not
silently substituted when vdirsyncer is unavailable. Account discovery uses
python-caldav with a generic read-only DAV fallback.

Install or update the project dependencies and use the persistent workspace:

```sh
uv sync --extra dev
uv run debacal
```

Demo mode is explicitly temporary and offline. It disables Sync and remote source
creation, and provides **Open my calendars** to switch into a real workspace.

## When synchronization runs

In a connected workspace, automatic syncing defaults to a first attempt about
1.5 seconds after startup and then every minute while the app remains open.
**Preferences → Automatic syncing** pauses or resumes this behavior;
**Sync every** accepts 1–1,440 minutes. The setting survives restart. Pausing
automatic syncing also suppresses the startup attempt. **Sync** / `Ctrl+R`
continues to work manually while automatic syncing is paused.

Demo workspaces never contact a server. To explore a conflict with temporary
sample data, run:

```sh
uv run debacal --demo-conflict
```

Click **Review conflicts** to inspect the local and server versions in calendar
previews and details. Decisions affect only the disposable demonstration data.

## Local edits and the file bridge

SQLite remains the UI's immediate source of truth. A local operation commits
its event changes, undo data and outbox records atomically. A worker bridges
those records into complete, per-UID ICS files and calls the maintained CLI.
The UI thread does not wait for network synchronization.

Private state lives beside the database in `<database>.sync/`, with one hashed
calendar directory. It contains working ICS files, remote snapshots, vdirsyncer
configuration/status, identity mapping and durable conflict comparisons. It
contains calendar data, but no saved password. The directory is private to the
user; keep it with the database when backing up to preserve pending conflict
choices. The SQLite outbox survives failed subprocesses and interrupted uploads.

A fresh read-only remote pass establishes a current working mirror and
synchronization baseline on each run before staging pending changes. Its snapshot
is a diagnostic copy, rather than a separately maintained synchronization pair.
This imports existing server events and distinguishes deletion from an item
that has never been seen. The bridge
stages only captured pending UIDs; it must not rewrite unrelated snapshot items
as local edits against a stale baseline. This prevents unrelated remote deletions
from being resurrected during an upload. A completed upload acknowledges only
the exact outbox record it captured, preserving edits made while it was running.

The bootstrap mirror is disposable; SQLite and its pending outbox are protected
from its refresh. Generated configurations do not select an automatic winner:
the read-only bootstrap uses `conflict_resolution = null`, and the writable pass
uses the review command hook. Older bridge versions used `b wins` only while
rebuilding that disposable read-only mirror. That setting did not authorize
replacing local SQLite edits and is no longer generated.

The initial implementation fetches the full collection rather than providing
incremental sync-token optimization. This costs network traffic, but runs in the
worker and leaves local editing and calendar visibility independent of the server.
The collection inventory uses an unfiltered PROPFIND listing instead of a
VEVENT-filtered calendar query. Non-event resources such as VTODO are preserved
in the mirror and ignored by the event view.

## Conflicts, credentials and status

The bridge compares local pending versions with the previous remote versions.
Vdirsyncer's conflict command captures races during the file synchronization.
Neither path silently picks a side. Durable comparisons feed the app's
side-by-side review; a decision is revalidated against current versions before
being applied. Delete/edit races also require review.
The review combines calendar previews with event details and offers **Keep
local**, **Use server**, and **Decide later**. The last option leaves both versions
available for a later decision.

Preferences offer **In-app**, **Desktop**, or **Both** for conflict notifications.
Desktop delivery uses the OS tray facility; an unavailable facility falls back
to the in-app notice. The persistent Review conflicts button remains available.
These are synchronization notices, not event reminders.

Passwords are stored per account through the system keyring when Remember is
checked. The account identity combines normalized server origin, login and the
Nextcloud deployment prefix; calendars belonging to that account reuse the same
credential on restart. Keyring I/O runs on the worker. A missing/locked secure
backend produces an actionable message and keeps the typed password for the
session. No plaintext fallback is used. Unchecking Remember removes a previously
saved credential while retaining the current session value.

Vdirsyncer's password-fetch helper receives the account password through its
child-process environment, never through saved config or command arguments.
OAuth remains future work.

**Sync** immediately shows activity. **Sync status** records recent operations,
per-calendar cached event series counts, errors and pending changes. A missing command, missing password,
authentication rejection, offline failure or worker startup error is visible;
a failed run is not replaced by a generic success message during view refresh.

## Individual resource failures

Parsing and validation operate per UID. Invalid resources are retained verbatim
in private quarantine and reported using a short resource identifier and a
sanitized reason. Healthy siblings can still be imported, viewed and synchronized.
The bridge leaves skipped server files intact and excludes their UIDs from
deletion inference; an unidentifiable bad resource conservatively protects cached
events from deletion. A bad queued local resource remains in the outbox while
other healthy writes continue. Unrelated unresolved conflicts also do not block
healthy uploads.

Naive UNTIL boundaries from exporters are interpreted in the DTSTART timezone
and converted to the standard UTC representation. Other unsupported rules fail
at that UID rather than aborting the calendar. Structural corruption is reported
rather than being mistaken for an empty calendar. A fixed resource is retried on
the next sync and removed from quarantine when it becomes valid.

The supplied `large-calendar.ics` is used as a local regression fixture when
present. This private calendar file is excluded from version control. It contains
1,638 event components and 1,442 UID masters. The previously
failing UNTIL rule now parses, and a candidate upload is tested beside the entire
collection and beside intentionally invalid resources. Tests never alter the
provided file or publish its contents.

A read-only audit on 5 November 2026 found 562 masters in the live Group cache, matching exactly the
first 562 masters of that export; 880 export masters were absent from the cache.
For November 2026, the export expands to 73 occurrences and the cached subset to
23. Independent recurrence expansion agrees with the export. This establishes a
coverage gap before display, but does not establish the current remote inventory:
the export may contain resources changed or deleted since it was taken. DNS
failure prevented a fresh server comparison, so the cause of the prefix gap
remains unverified. The inventory change above must be checked against a live
server before claiming that it restores those 880 resources.

## Discovery and recurring resources

Add calendar accepts a Nextcloud server/account URL or a direct CalDAV collection.
Discovery follows the principal and calendar-home properties, including well-known
and Nextcloud DAV endpoints. Users choose discovered event calendars; names,
colors and read-only access are retained. Public ICS URL subscriptions and file
import/export remain supported. Google-style OAuth is not yet integrated.

Colors use server metadata by default; manual overrides remain local and survive
password changes or metadata refresh. A writable default calendar is stored in
preferences.

One event master represents one UID resource. RRULE, RDATE, EXDATE and detached
RECURRENCE-ID exceptions remain standard iCalendar. Views expand occurrences;
sync never flattens the series. The editor's regular controls, chosen dates and
advanced rule field are different ways to configure this same schedule model.
Custom authoring text and its anchor date are retained in SQLite/history and
written into the master VEVENT's proprietary properties whenever a custom
definition exists. These fields travel through the same CalDAV resource as the
standard recurrence, including when a definition compiles to a regular RRULE.
New named-zone RRULE exports generate VTIMEZONE through the declared `icalendar`
dependency. Definitions cover 1970–2100 by default and extend to master dates;
imported definitions and unknown event metadata are retained.

## Custom series metadata between Debacal instances

Each authored series carries these properties on its master VEVENT:

| Property | Meaning |
| --- | --- |
| `X-DEBACAL-PATTERN` | Exact custom sequence text, including its formatting. |
| `X-DEBACAL-PATTERN-VERSION` | Language version: `2` is the current sequential syntax; `1` is the legacy syntax. |
| `X-DEBACAL-PATTERN-ANCHOR` | Initial calendar date in ISO `YYYY-MM-DD` form. |

For example, part of a fortnightly resource is:

```ics
DTSTART;TZID=Europe/Berlin:20261107T090000
DTEND;TZID=Europe/Berlin:20261107T100000
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=5
X-DEBACAL-PATTERN:4*+2
X-DEBACAL-PATTERN-VERSION:2
X-DEBACAL-PATTERN-ANCHOR:2026-11-07
```

The initial time and authoring zone come from DTSTART/TZID, with VTIMEZONE
included for named zones. Date-only all-day schedules retain calendar-date
semantics. Metadata uses normal iCalendar TEXT escaping and UTF-8 line folding;
Unicode whitespace inside a definition is preserved as text. These properties
are per series, rather than a global VCALENDAR setting. Detached exceptions use
the master definition.

Another Debacal instance restores the definition even when its local calendar
IDs or display timezone differ. Sequence clock times continue to use the series'
original authoring zone; the editor identifies that zone while showing Starts
and Ends in the local display zone. For a current version-2 definition matching
the standard schedule, a title-only edit keeps the definition, anchor, authoring
zone and event instants intact. Legacy version-1 definitions are upgraded to the
current notation when edited. Changes to the definition alone also enter the
outbox and synchronize.

RRULE/RDATE/EXDATE remain the functional schedule other clients understand.
If another client changes those dates without updating the proprietary text,
the editor reconstructs an equivalent definition for finite schedules of up to
500 occurrences. Larger or unbounded schedules show a notice and retain their
standard recurrence until a replacement definition is entered. If a client
removes the proprietary fields, a clean pull accepts their removal and retains
the standard schedule; it does not silently restore an old annotation.

To check sharing, save a custom series in instance A and finish Sync, then sync
the same calendar in instance B. Choose **All in series** (`F3`), then edit (`E`) to view
and edit its custom definition. The X-properties are also visible in an ICS
export or the downloaded master resource. A queued change must finish uploading
before another instance can receive it. Integration tests use separate databases
and calendar IDs sharing a simulated remote through the actual sync bridge,
including edits in both directions and different display timezones.

A recurring candidate series is supported. It currently represents alternative
slots for one scheduling decision: confirming an occurrence keeps that actual
slot, clears recurrence, and removes other candidate choices in the same group
and calendar. One undo restores the entire choice set. Confirmed siblings remain.

## Live testing and current validation

To exercise a real conflict through the UI:

1. Create a disposable test event, sync it, and verify the calendar has no pending
   changes or sync errors.
2. Pause **Automatic syncing** in preferences.
3. Edit that event locally, for example moving it to 11:00 and changing its title.
4. In Nextcloud, edit the same event to a different version, for example 12:00 and
   another title. Save the server edit before continuing.
5. Click **Sync** manually, then **Review conflicts**. Compare the two calendar
   previews and choose **Keep local**, **Use server**, or **Decide later**.
6. After resolving it, sync again and verify the resulting event. Remove the test
   event when finished and re-enable **Automatic syncing**.

Both edits must begin from the same previously synchronized event. Pausing
automatic syncing keeps an intervening background upload from replacing that
shared baseline during the test.

```sh
.venv/bin/python scripts/check_caldav.py \
  --url https://cloud.example.org --username YOUR_USERNAME
```

The helper prompts for the password (or accepts
`DEBACAL_TEST_CALDAV_PASSWORD` from the environment). Default operation is discovery
and a read-only pull through the default vdirsyncer bridge.
Use `--write-test` to exercise a uniquely named test resource and clean up only
that resource. The helper's default write backend is vdirsyncer; the explicit
`--backend direct` diagnostic checks the older transport separately.

The parser and CLI dependencies are installed. This sandbox could not resolve
the configured Nextcloud host, before authentication, so live server verification
remains outstanding. The keyring package is present, while the sandbox has no usable desktop keyring
service; credential persistence is exercised with injected secure-backend tests.
Tests exercise real files/configuration and the conflict helper with a simulated
vdirsyncer process, plus the installed vdirsyncer CLI, discovery, status database
and synchronization algorithm over disposable filesystem pairs. These latter
tests cover a deletion between the bootstrap and writable passes, a healthy
upload beside that conflict, and both explicit resolutions. A small adapter
blocks vdirsyncer's implicit recreation of expected-existing events; HTTP,
ETags and normal storage operations remain in vdirsyncer. CalDAV method dispatch
is checked separately with injected storage delegates. Codec, outbox races,
failures, recurrence and UI workflows also have regression coverage.
