# Lebacal

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
uv run lebacal --demo
uv run lebacal --demo-conflict
uv run lebacal
```

Or use a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/lebacal --demo
```

`--demo` is explicitly offline and uses a disposable database; your calendars are
untouched. Choose **Open my calendars** to leave the demo, or launch without
`--demo` to connect and synchronize a remote calendar. Normal mode
starts with an empty Personal calendar and stores data under Qt's platform data
directory, normally `~/.local/share/lebacal/lebacal/calendar.sqlite3` on Linux.
Use `--database /path/to/calendar.sqlite3` to choose another location. Back up the
database with the app closed (or use SQLite's backup API for a live database).
`--demo-conflict` opens an offline sample conflict: click **Review conflicts** to
compare calendar previews and try a decision using disposable data.

In the development environment used to create this app, PySide6, pytest, and
python-dateutil were already available, but package download DNS was blocked.
A `.venv` with system packages allows offline local/ICS use:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
.venv/bin/python -m lebacal --demo
```

On a fresh machine, install dependencies using one of the standard commands above.
The maintained `icalendar` parser is used when installed; a smaller offline
parser supports ordinary events and named IANA zones. `python-dateutil` expands
preserved recurrence for views. CalDAV reports an actionable error when its
client dependency is missing. CalDAV sync now uses vdirsyncer; updating the project
dependencies is required after upgrading from the previous version. A missing
executable is shown in Sync status rather than silently disabling sync.
Exporting a newly created named-timezone RRULE series or custom sequence also
needs `icalendar` to generate its VTIMEZONE definition; local editing still works
without it. Existing imported definitions are retained. UTC events export offline.

## Workflow

* **Day / week / month:** switch with the toolbar or `1`, `W`, `M`. `H`/`L`
  move backward/forward; `T` returns to today. Click the mini calendar to jump.
* **Select a day:** click a day heading or an empty calendar area. The selected
  day is highlighted and follows view changes and the mini calendar. With no
  event selected, Left/Right selects the previous/next day.
* **Create:** `N`, the New event button, double-click in any view, or drag an
  empty day/week time slot. A single click selects without creating. Double-click
  always creates a new event, including over an existing event; use `E` or the
  right-click menu to edit. In month view, the body starts a timed event at 09:00.
  Double-click a day heading, its **+ All day** area, or the day/week all-day strip
  to start an all-day event. The day/week heading remains available while scrolling.
  Drag a timed event to move it; drag its bottom edge to resize.
* **Keyboard selection:** `G`, day number `1`–`7`, event number, `Enter`.
  Badges appear only during selection, highlighting the chosen day and typed
  event. Multi-digit event numbers work. Arrow keys move selection and scroll
  only as far as needed to reveal an event. `E` edits one occurrence, `Ctrl+E`
  edits all events, and `Shift+E` edits this and following events. The same scope
  modifiers apply to deletion, mouse dragging/resizing, copy, paste and duplicate.
* **Copy / paste / duplicate:** `C`, `V`, `D`. Hold `Ctrl` for all events or
  `Shift` for this and following events. `D` creates a copy at the same time;
  `V` pastes on the selected day at the copied event's time. The pointer's
  position has no effect. Day view is `1`. Text fields keep native
  `Ctrl+C` / `Ctrl+V` behavior.
* **Search:** `Ctrl+F`, then title, notes, or location. Up/Down cycles through
  matches, wrapping at either end; clicking a result also reveals and highlights
  its event in the calendar. The query stays open while browsing. Enter keeps the
  current match selected and returns focus to the calendar; Escape closes search.
  `/` remains a calendar shortcut for search with the default bindings.
* **History:** `Ctrl+Z` / `Ctrl+Shift+Z`; Operation history can undo an earlier
  independent action. Subsequent changes to the same event must be undone first.
  History survives restart. Shortcuts leave text fields' normal editing intact.
* **Configuration:** Keyboard shortcuts (or `F1`) lets you change the bindings;
  it also configures the all-events and following-events modifiers, and the
  editor's scope-switch shortcut. Configuration survives restart, and generated
  modifier combinations are checked for shortcut collisions.
* **Preferences:** choose a writable default calendar for new events, any start
  weekday, system/European/ISO/US date
  format, and system/12-hour/24-hour time. Defaults follow the OS locale. The
  mini calendar, views, editor popups and displayed dates/times use these choices.
  The default calendar has a star in the sidebar. Editing an existing event
  retains its original calendar. Automatic syncing can be paused or enabled,
  with an interval from 1 to 1,440 minutes; manual **Sync** remains available.

### Working with event series

An action without an additional scope modifier affects one event. Hold `Ctrl`
for all events, or `Shift` for this and following events. Holding both uses this
and following. The footer shows the current scope; mouse drag previews show it
before applying the change. Right-click menus offer all three scopes explicitly.
The editor shows its scope and lets you cycle it with `Alt+S`, including while
typing in a text field. You can also choose it directly from the scope control.

A following edit splits at the selected event's original recurrence date. The
past keeps its identity; the future gets a new calendar UID and retains a
standard recurrence rule. For example, moving an endless Monday series from
one occurrence onward to Wednesday ends the Monday series before the selection
and starts an endless Wednesday series. Moved exceptions and excluded dates are
partitioned by their original recurrence IDs. The complete split is one undoable
operation and synchronizes as ordinary calendar resources. Splitting at the
first occurrence updates the original resource without leaving an empty past.

Use `Ctrl+C`, then `Ctrl+V`, to copy and paste a complete series; use `Shift+C`
and `Shift+V` for its future part. Plain `V` takes one occurrence from the copied
selection. Paste cannot recover events outside the scope originally copied.
It aligns the copied selected occurrence with the selected calendar day, keeping
its clock time. Explicit **Paste … here** actions in an event's context menu use
that event's date and time. All-day copies retain their calendar-date span.
Pasting uses the writable default calendar; duplication uses the source calendar
when writable. Each copy
gets fresh event identities and independent group links. Clipboard data also
includes standard iCalendar for other applications, and standard ICS can be
pasted from other applications. Duplication preserves the existing clipboard.

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

Custom sequence definitions also synchronize between Lebacal instances. The ICS
master stores the exact expression, language version and anchor in
`X-LEBACAL-PATTERN` properties alongside standard RRULE/RDATE fields. Another
instance restores the editable definition and its authoring timezone, even when
its display timezone differs. See the [metadata protocol](docs/SYNC.md#custom-series-metadata-between-lebacal-instances).

For alternatives, check **Alternative slots**. Regular repeating and chosen-date
series can both be alternatives. Select the chosen
occurrence and click **Confirm this slot**: that actual slot remains as an
ordinary event and other choices disappear as one undoable action, including
the remaining occurrences of a candidate series. Right-click an event for whole-group editing or deletion.
The editor always shows a scope selector for linked events: this event, all
events, or this and following events. Group editing applies common fields and a relative
time shift. Single-occurrence edits produce standard RECURRENCE-ID exceptions;
single-occurrence deletion produces EXDATE. Both are undoable without flattening
the master series.

### Import, subscriptions, and CalDAV

**Import .ics** asks for a writable destination; **Export .ics** exports a chosen
calendar. Imports preserve recurring masters and exceptions; occurrences are
expanded only for the displayed date range. An import is undoable and does not
truncate an infinite series to a fixed window. The offline codec supports ordinary
VEVENTs and named IANA zones; unsupported custom timezone definitions fail clearly.

**Add calendar** supports local calendars, read-only ICS URLs, and CalDAV accounts.
Enter a server/account URL (including a Nextcloud base URL), username and app
password, then **Discover calendars**. Select which discovered calendars to add.
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
QT_QPA_PLATFORM=offscreen python -m lebacal --demo --screenshot /tmp/lebacal.png
```

Tests cover year rollover and bounded pattern parsing, overlapping intervals,
timezones, atomic history and selective undo, durable outbox races, ICS recurrence
exceptions, sync conflict handling, and Qt editing/rendering/drag workflows.

## Remaining work

* Live provider integration tests and OAuth.
* Broader recurrence and timezone interoperability, including advanced custom rules.
  Custom non-IANA timezone definitions, detached-only imports without a master,
  EXRULE, RANGE=THISANDFUTURE and sub-minute rules currently fail explicitly.
* Larger-calendar profiling and avoiding expensive work on the UI thread for
  unusually large batches and global search.
* Distribution packaging, accessibility audit, event copying,
  and automated end-to-end checks on a Linux desktop.

The code separates pure domain logic (`models`, `patterns`, `layout`, `recurrence`), SQLite
(`store`), standards/sync (`ics`, `sync`), and the Qt workspace (`ui`, `dialogs`,
`calendar_widgets`). It has no web backend or external service requirement.
