# Debacal

A native Linux desktop calendar built around immediate local edits, keyboard
navigation, interoperable recurrence, irregular event groups, and alternative time slots. See the [implementation plan](docs/PLAN.md)
for assumptions, milestones, and open questions.

This is an initial working application, with real SQLite persistence and tests.
It is ready to try with local calendars. Nextcloud discovery is implemented; live
CalDAV still needs provider testing. See [sync design and testing](docs/SYNC.md).

## Run

Python 3.11+ and a Linux desktop are required. With [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra dev
uv run debacal --demo
uv run debacal --demo-conflict
uv run debacal
```

Or use a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/debacal --demo
```

`--demo` is explicitly offline and uses a disposable database; your calendars are
untouched. Choose **Open my calendars** to leave the demo, or launch without
`--demo` to connect and synchronize a remote calendar. Normal mode
starts with an empty Personal calendar and stores data under Qt's platform data
directory, normally `~/.local/share/debacal/debacal/calendar.sqlite3` on Linux.
Use `--database /path/to/calendar.sqlite3` to choose another location. Back up the
database with the app closed (or use SQLite's backup API for a live database).

Only one window normally runs for each profile/database. Calling `debacal`
again brings its existing window forward. Use `--new-instance` to explicitly
allow another window using the same data; profiles are the better choice for
separate environments.

```sh
debacal --test                    # Persistent testing profile
debacal --profile work            # Another named profile
debacal appointment.ics           # Review a file before importing
debacal --test one.ics two.ics    # Import into the testing profile
debacal < appointment.ics         # Import ICS contents from standard input
cat appointment.ics | debacal     # Piped contents work too
```

`--test` is shorthand for `--profile testing`. Named profiles start with an empty
Personal calendar and retain their own events, settings, history, sync mirrors,
and keyring credentials. They do not copy or reuse your normal accounts or
passwords. Their data is stored under `profiles/NAME/calendar.sqlite3` within
the application data directory. The window title and sidebar identify the
profile. Unlike the offline disposable demo, testing profiles can connect to
test calendar servers and sync normally. `--database` overrides a profile's
database path; alternative databases also use separate keyring credentials.

Opening one or more `.ics` files shows an import dialog with the selected
creation calendar as its destination. Choose a different writable calendar or
cancel before anything is imported. A compact source field leaves room for a
month preview showing the incoming events, recurring dates, and connected
all-day spans. Tooltips show event details. Click an event to include/exclude
that occurrence, or click a day to toggle events starting on that date.
Multi-day events remain whole. **All**, **None**, and **This month** set the
starting selection; exclusions stay visible, dimmed and crossed out. Choices
persist while navigating months. Changing the destination updates the preview color.
Excluding dates from a series retains its standard rule with EXDATE; explicitly
choosing a few occurrences imports only those dates, as a finite RDATE series
or an individual event. The preview does not edit the source calendar.
Unreadable files and unsupported entries are listed under **Review issues**.
Imports can repair events lacking UID identifiers by assigning stable IDs;
this is reported as an informational warning. Remote sync retains strict UID
validation, and importing the same file again uses the same generated IDs.
The same dialog opens if Debacal is already
running; file paths are delivered to that window instead of starting another
sync engine. Requests received during another dialog wait their turn. File reads
and preview preparation run in the background, and an invalid file does not
prevent other files from being imported. Confirmation imports the reviewed
snapshot without rereading files. Imports can be undone.

Redirected or piped ICS contents open the same review dialog, labeled
**Standard input**, and can be combined with file arguments. This also works
with `--test`, other profiles, and an already-running window. Empty input starts
the app normally. Interactive terminal input is left alone unless you use
`debacal -`; finish a pasted calendar with Ctrl+D. Each input is limited to 20 MB.

`--demo-conflict` opens an offline sample conflict: click **Review conflicts** to
compare calendar previews and try a decision using disposable data.

In the development environment used to create this app, PySide6, pytest, and
python-dateutil were already available, but package download DNS was blocked.
A `.venv` with system packages allows offline local/ICS use:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
.venv/bin/python -m debacal --demo
```

On a fresh machine, install dependencies using one of the standard commands above.
The maintained `icalendar` parser is used when installed; a smaller offline
parser supports ordinary events and named IANA zones. `python-dateutil` expands
preserved recurrence for views. CalDAV reports an actionable error when its
client dependency is missing. CalDAV sync now uses vdirsyncer; updating the project
dependencies is required after upgrading from the previous version. A missing
dependency is shown in Sync status rather than silently disabling sync.
Debacal runs vdirsyncer through its own Python environment, including after
moving the project directory.
Exporting a newly created named-timezone RRULE series or custom sequence also
needs `icalendar` to generate its VTIMEZONE definition; local editing still works
without it. Existing imported definitions are retained. UTC events export offline.

## Workflow

Choose **Preferences → Appearance → Color scheme** for Neutral, Warm, or Cool
interface colors. The choice is saved per profile and applies to the workspace
and calendar previews. Calendar colors still come from their sources. The
sidebar separates the month overview, calendar controls, and agenda/search;
source-colored event cards keep their selection and keyboard focus cues. Choose
a calendar by name to create in it; its eye button controls visibility separately.
Read-only sources show a lock. Drag a calendar's name or color square to reorder
the list; its order is saved per profile without changing the creation calendar.
Preferences → Appearance → Upcoming events offers event cards, cards grouped by
day, and a per-day timeline (the default). Search always uses event cards.

* **Day / week / month / year:** switch with the toolbar or `D`, `W`, `M`, `Y`.
  `H`/`L` move backward/forward by the current view's period; `T` returns to today.
  The navigation buttons keep their position as month labels change.
  Click the mini calendar to jump.
* **Year overview:** twelve month cards show calendar-colored event markers.
  Click a month heading to open Month view, or a marker to select its event;
  Ctrl+click adds to the selection. Double-click a day to create an all-day event.
  Click a week number to open that week. Week numbers appear by default in
  Month and Day/Week views too, and can be switched off in Appearance preferences.
  Right-click a day to inspect its events or open Day view. Search and paste
  previews work in Year view too; small windows scroll the overview.
* **Select events:** click an event; Ctrl+click adds or removes events from the
  selection. Highlights keep the calendar color, with a thicker border and bar.
  Escape clears the selection. Clicking empty space clears it too.
* **Create:** `N`, the New event button, double-click in any view, or drag an
  empty day/week time slot. A single click selects without creating. Double-click
  an existing event to open the same editor as `E`, including the
  one/all/following controls. Double-click empty space to create a new event.
  In month view, an empty body starts a timed event at 09:00.
  Double-click a day heading or the day/week all-day strip
  to start an all-day event. All-day events stay above the timed grid; multi-day
  bars connect adjacent dates and continue across month week rows. The timed grid
  fits 08:00–18:00 and expands to include visible events. The vertical expansion
  button (`F`) in the View group shows all
  24 hours; small windows can still scroll. Drag an event to move the selection;
  Ctrl+drag copies it into the selected creation calendar; move previews keep
  each event's original calendar color and copy previews use the target color.
  A small **+** marks a copy while dragging, including when
  Ctrl is pressed or released without moving. Drag a timed event's bottom edge
  to resize. Hover over empty timed space for a light snapped-time guide;
  the hover guide uses a readable color independent of calendars. Creation drags
  show their starting time and the selected creation calendar's color.
* **Keyboard selection:** `G`, day number `1`–`7`, event number, `Enter`.
  Badges appear only during selection, highlighting the chosen day and typed
  event. Multi-digit event numbers work. Arrow keys move selection and scroll
  only as far as needed to reveal an event. Selection modes are `F2` for one
  occurrence, `F3` for all events in its series, and `F4` for this and following.
  The highlight shows the affected events; `E`, Delete, moving, resizing and
  copying use that same selection. The mode persists until changed.
* **Copy / paste:** `Ctrl+C` copies the selection;
  `Ctrl+V` previews it. Moving the pointer repositions the preview; click or
  Enter places it, and Escape cancels without changing the calendar. Before any
  pointer movement, the initial position retains the copied day/time relative
  to the displayed page: copying in one week and pasting two weeks later shifts
  the selection by two weeks. The preview includes every visible occurrence of
  copied series. Text fields retain their native clipboard behavior.
* **Search:** click the search icon beside **Next events**, or press `Ctrl+F`;
  the input immediately receives focus. Type a title, notes, or location.
  Empty search controls collapse back to the agenda heading when focus leaves;
  a populated query stays visible. Up/Down cycles through
  matches, wrapping at either end; clicking a result also reveals and highlights
  its event in the calendar. Result rows show a prominent title, smaller date/time
  and calendar color; the calendar name is in the tooltip. Repeating series expand
  into dated occurrences with bounded **More…** pages: infinite series show ten
  dates per page, finite schedules show 100. A series root selects the whole
  series; a dated child selects only that occurrence, even when the calendar is
  in all-events mode. Their row highlights are independent. Ctrl+click combines
  roots and children with those meanings. Up/Down transfers focus to the results
  so Delete, copy and edit act on that selection. The query stays open while
  browsing. Enter returns focus to the calendar; Escape clears search and
  restores the next ten events from visible calendars over the next 30 days.
  Search and upcoming events share a sidebar panel, so searching does not resize
  the calendar. The funnel beside the search field optionally filters the main
  calendar to matching events too. Clearing the query restores the full calendar;
  the filter toggle is remembered. Matching series titles include the whole
  series; an occurrence-only title, place, or note match includes that occurrence.
  Selections hidden by the filter are cleared.
  `/` remains a calendar shortcut for search with the default bindings.
* **History:** `Ctrl+Z` undoes; `Ctrl+Y` redoes. Operation history can undo an earlier
  independent action. Subsequent changes to the same event must be undone first.
  History survives restart. Shortcuts leave text fields' normal editing intact.
* **Controls:** date navigation sits beside the current month and range. The
  labeled toolbar groups View (including full-day expansion), Selection (modes
  and selection export), and Events (new event, undo, redo, history). Hover over icons for their meaning and
  current shortcut. Import/export and adding sources sit beside Calendars; right-click
  a calendar for its source and file actions. The gear opens Preferences.
  Sync has a fixed position at the bottom of the sidebar; click
  its status text for details. Conflicts show a review icon beside the status.
* **Creation calendar:** click a writable calendar name in the sidebar to choose
  where new events are created. The selected name is highlighted and remembered.
  Its separate eye button shows or hides that calendar. Editing existing events
  retains their original calendars. Short source lists stay fully visible;
  long lists scroll. Large text in a short window can scroll the sidebar while
  sync status stays fixed.
* **Preferences:** General groups date language (system, English, German), week
  start, date format, and time format. Interface labels remain in English.
  Language changes month and weekday names independently of date/time format
  and week start. System language follows Qt's preferred UI languages, while
  regional date/time defaults follow the OS formatting locale (`LC_TIME` on
  Linux, with `LC_ALL` taking precedence). Preferences shows the detected values.
  Appearance controls text size, week numbers across calendar views, and the usual
  visible hours; the time grid still expands for events outside those hours.
  Sync groups automatic sync intervals and conflict notifications. Keyboard
  shortcuts (`F1`) lets you remap actions and selection-mode keys, with duplicate
  and ambiguous sequences rejected. Settings survive restart.
* **Event editor:** Tab/Shift+Tab move through the form; labels have Alt-key
  mnemonics. Enter saves from form fields; Notes keeps Enter for a new line, and
  Ctrl+Enter saves from anywhere. In calendar-source setup, Enter discovers a new
  CalDAV account before saving its selected calendars. Clicking the series
  preview switches a one-off or finite recurrence to a custom sequence, preserving
  its existing dates; unbounded or oversized rules require an explicit finite
  schedule before conversion.

### Working with event series

Choose **One event** (`F2`), **All in series** (`F3`), or **This and following**
(`F4`) before selecting events. Changing the mode updates the affected highlights
immediately, including for Ctrl-added selections. The toolbar and event editor
use the same segmented controls and remappable keys, including while typing in
the editor. `Alt+S` also cycles modes. Ctrl and Shift do not change the scope of
an action. Ctrl+click toggles selection and Ctrl+drag copies.

Operations use the union of selected events, avoiding duplicate changes where
selected series overlap. Multi-event edits apply changed common fields and
relative times while retaining untouched values. Select one series to edit its
recurrence configuration. Each batch mutation is one undoable operation; a
read-only or stale selected resource rejects the batch before any event changes.

A following edit splits at the selected event's original recurrence date. The
past keeps its identity; the future gets a new calendar UID and retains a
standard recurrence rule. For example, moving an endless Monday series from
one occurrence onward to Wednesday ends the Monday series before the selection
and starts an endless Wednesday series. Moved exceptions and excluded dates are
partitioned by their original recurrence IDs. The complete split is one undoable
operation and synchronizes as ordinary calendar resources. Splitting at the
first occurrence updates the original resource without leaving an empty past.

Copy in `F3` mode for whole series, or `F4` for their future parts. Pasting keeps
exactly the captured selection even if the mode has since changed. It stages a
preview rather than writing immediately. **Paste here…** in a context menu sets
the initial preview position to that event's time. All-day copies retain their
calendar-date span. Pasting and Ctrl-drag use the chosen creation calendar.
Copies get fresh identities
and independent group links. Clipboard data also
includes standard iCalendar for other applications, and standard ICS can be
pasted from other applications.

Normal daily, weekly, monthly, yearly, finite and irregular schedules support
following operations. A rule whose phase cannot be retained safely, or a split
requiring more than 10,000 historical rule instances, produces an explanatory
error before changing any events. Moving and resizing preserve individual
exception content; moving also preserves their different durations.

### Irregular groups and candidate slots

The event editor shows a month calendar preview with timed occurrence blocks.
Use its month arrows to inspect the whole schedule. **Starts** and **Ends** define
the fixed initial event; **Custom sequence** adds occurrences by following the
instructions below. The initial event is always included, so the sequence need
not repeat its date. Its duration applies to the additional occurrences.

Instructions are separated by spaces or commas. Each runs from the result of the
previous instruction:

* `+2` adds two weeks. Units can be explicit: `+3d`, `+2w`, `+1m`, `+1y` for
  days, weeks, calendar months, and calendar years. Relative steps keep the
  previous event's local clock. Month/year shifts clip invalid days: 31 January
  plus one month becomes 28 February in a non-leap year.
* `13.` chooses the next day 13; `3.2.` (also `3.2`) chooses the next 3 February. Omitted
  month/year values roll forward until the matching instant is strictly later
  than the cursor. A day 31 skips months without that day; 29 February skips
  non-leap years. Date-only instructions use the initial event's clock.
* `14:30-13.` adds the next day 13 at 14:30. `14:30` alone chooses that clock on
  the same day if still ahead, otherwise the next day. Clocks use 24-hour `HH:MM`.
* A full date such as `3.2.2027.` is exact, even if earlier than the cursor or
  initial event. The next relative instruction runs from that exact date.
* `4*+2` repeats a step four times; `2*(+1w +2d)` repeats a group. Groups may nest.
  Repeat counts add occurrences after the initial event. `+0` is invalid.

```text
13. +1 18. 2. 3.2. +2
4*(+2) +3 3*+2
14:00-13. +1 18.
```

Starting at **7 November 2026, 09:00**, the first example adds 13 November,
20 November, 18 December, 2 January, 3 February, and 17 February: seven events
including the initial one. Duplicate instants appear once, while each instruction
still updates the cursor. Expansion is bounded to 500 events including the fixed
event; repeat budgets are checked before execution. Nonexistent local times during
a daylight-saving clock change are rejected. During a repeated hour, absolute
dates retain the initial event's choice, while relative and time-only steps retain
the previous event's choice.

The text remains editable when reopening a saved custom series. Title edits
preserve it; moving a series keeps relative text when equivalent and rewrites
absolute instructions to reflect the actual new instants. Older syntax is never
silently interpreted as the new language: opening it reconstructs an equivalent
sequence from its stored standard dates, and saving records the new version.
Opening alone does not change the saved events. Clicking preview dates adds or
removes dates and reconstructs a compact equivalent sequence. It compares short
day/month dates, relative steps, clock changes and repeated groups, omitting a
year whenever the shorter form selects exactly the same date. Regular gaps and
repeated blocks use multipliers when that makes the expression shorter. Calendar
clicks remain available after simplification. Hand-entered text keeps its
formatting until a calendar click changes the selection.

Saving creates one undoable operation. Regular schedules use standard RRULE;
irregular schedules use standard RDATE, retaining their structure in other calendar
clients. The unified **Schedule** control also supports daily, weekly (including
weekday selection), monthly and yearly schedules with interval/count/end-date
controls. **Advanced rule** exposes standard RRULE text for schedules beyond those
controls.

Custom sequence definitions also synchronize between Debacal instances. The ICS
master stores the exact expression, language version and anchor in
`X-DEBACAL-PATTERN` properties alongside standard RRULE/RDATE fields. Another
instance restores the editable definition and its authoring timezone, even when
its display timezone differs. See the [metadata protocol](docs/SYNC.md#custom-series-metadata-between-debacal-instances).

For alternatives, check **Alternative slots**. Regular repeating and chosen-date
series can both be alternatives. Hover over or select the chosen
occurrence and click its inline check mark: that actual slot remains as an
ordinary event and other choices disappear as one undoable action, including
the remaining occurrences of a candidate series. Right-click an event for whole-group editing or deletion.
The editor always shows a scope selector for linked events: this event, all
events, or this and following events. Group editing applies common fields and a relative
time shift. Single-occurrence edits produce standard RECURRENCE-ID exceptions;
single-occurrence deletion produces EXDATE. Both are undoable without flattening
the master series.

### Import, subscriptions, and CalDAV

**Import .ics** asks for a writable destination; **Export .ics** beside Calendars
exports a chosen calendar. **Export selected events** beside the Selection heading
(`Ctrl+Shift+E`) and an event's context menu export the selection instead. This
follows the one/all/following mode and preserves standard recurrence where applicable.
Exporting separate occurrences gives them independent identities so they remain
separate when imported. Exports leave the source events and clipboard unchanged.
Imports preserve recurring masters and exceptions; occurrences are
expanded only for the displayed date range. An import is undoable and does not
truncate an infinite series to a fixed window. The offline codec supports ordinary
VEVENTs and named IANA zones; unsupported custom timezone definitions fail clearly.

**Add calendar** supports local calendars, read-only ICS URLs, and CalDAV accounts.
Enter a server/account URL (including a Nextcloud base URL), username and app
password, then **Discover calendars**. Select which discovered calendars to add;
their names come from the server. No account-level calendar name or default-calendar
choice is requested. Choose the creation calendar afterward by clicking its name
in the sidebar. Local calendars and ICS subscriptions still have a name field.
Remote work runs in a separate thread. **Remember in the system keyring** saves
the account password through Secret Service, KWallet, or the platform credential
store. Calendars in the same account share that password, including after restart.
If the keyring is unavailable or locked, the app reports the save failure and
continues with a session password. No plaintext password file is used. OAuth is
not included yet.

Calendar colors follow the server by default. Right-click a calendar to choose
an unrestricted custom color or switch back to the server color. Password-only
edits preserve both the current color and the override preference.

Automatic syncing defaults to an attempt about 1.5 seconds after startup, then
every minute while the app is open. Preferences can pause it or change the
interval; pausing also suppresses the startup attempt. **Sync** / `Ctrl+R` still
works manually. Offline changes
stay in a transactional outbox. Calendar toggles use only local data. Individual
unsupported resources are retained in quarantine and listed in Sync status;
healthy events and uploads continue. A skipped resource is never treated as a
server deletion. Repairing it on the server or updating the parser retries it
on the next sync. Floating UNTIL values are interpreted in DTSTART’s timezone
and serialized as UTC when required. Candidate slots use the same upload path
as ordinary events. Conditional
ETags protect updates/deletes; conflicts compare local and server calendar
previews and details, with **Keep local**, **Use server**, or **Decide later**.
Recurring series remain a single resource during sync.
The default CalDAV backend is vdirsyncer. The Sync button immediately shows
activity; **Sync status** retains pending counts and per-calendar errors. Conflict
notification delivery is configurable as in-app, desktop, or both.
The read-only bootstrap refreshes a disposable mirror, preserving SQLite and its
pending edits. No automatic conflict winner is configured; writable conflicts
require review. Inventory uses a full PROPFIND listing and preserves non-event
resources such as VTODO without displaying them as events.
Live Nextcloud verification was again blocked by sandbox DNS;
see [the integration helper and sync implementation](docs/SYNC.md) for a reproducible
live conflict test. The read-only audit found only the first 562 of 1,442 supplied
export masters in the live Group cache. Current remote coverage and the cause of
that gap remain unverified; see the audit details in the sync documentation.

## Validation

```sh
QT_QPA_PLATFORM=offscreen python -m pytest
QT_QPA_PLATFORM=offscreen python -m debacal --demo --screenshot /tmp/debacal.png
```

Tests cover year rollover and bounded pattern parsing, overlapping intervals,
timezones, atomic history and selective undo, durable outbox races, ICS recurrence
exceptions, sync conflict handling, and Qt editing/rendering/drag workflows.

For a disposable large-calendar interaction benchmark:

```sh
.venv/bin/python scripts/benchmark_ui.py --events 10000 --series 200 --iterations 3
```

This creates synthetic data in memory with synchronization disabled, then measures
view switches, calendar visibility, typing/search completion, and slot confirmation,
including paints. `--output /tmp/timings.json` saves the results; `--source-root`
compares another checkout with the same workload, and `--profile` saves cProfile data.
Timing depends on the machine and Qt backend. Tests separately check bounded reads,
layout reuse, and correct invalidation after edits, undo, and remote writes.

Calendar pages and paint geometry are reused when their inputs stay unchanged.
An event-only SQLite revision invalidates those caches across sync connections;
visibility changes do not rebuild the event search index. Search uses a lightweight
case-folded projection and loads complete events only for matching candidates.
Existing databases build that projection once on upgrade without changing events.
Typing is coalesced over 80 ms; arrows and Enter finish any pending search immediately.
Slot confirmation acts on release without waiting for the double-click interval.

## Remaining work

* Live provider integration tests and OAuth.
* Broader recurrence and timezone interoperability, including advanced custom rules.
  Custom non-IANA timezone definitions, detached-only imports without a master,
  EXRULE, RANGE=THISANDFUTURE and sub-minute rules currently fail explicitly.
* Progressive handling of exceptionally large import/edit batches on the UI thread.
* Distribution packaging, accessibility audit, event copying,
  and automated end-to-end checks on a Linux desktop.

The code separates pure domain logic (`models`, `patterns`, `layout`, `recurrence`), SQLite
(`store`), standards/sync (`ics`, `sync`), and the Qt workspace (`ui`, `dialogs`,
`calendar_widgets`). It has no web backend or external service requirement.
