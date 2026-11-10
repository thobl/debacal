# Debacal

A native Linux desktop calendar built around immediate local edits, keyboard
navigation, interoperable recurrence, irregular event groups, and alternative time slots. See the [implementation plan](PLAN.md)
for assumptions, milestones, and open questions.

Local editing, SQLite persistence, calendar import/export, and Nextcloud sync
have automated coverage and live provider smoke checks. See the
[release review](RELEASE_REVIEW.md) for validation, performance results, and
remaining limits, and [sync design and testing](SYNC.md) for interoperability.

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
Use `--database /path/to/calendar.sqlite3` to choose another location. Calendar
data and history live in that file. Display preferences, shortcuts, calendar
visibility and other UI choices live beside it in `calendar.sqlite3.ui.sqlite3`.
This keeps those controls responsive while synchronization writes calendar data.
Close the app before backing up, moving or resetting these files; include both
to retain the complete profile. SQLite's backup API can copy each live database.
Do not discard `-wal`/`-shm` companions while the app is running.

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
the application data directory. The window title identifies the
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
The import dialog shares the event editor's two-column layout, calendar picker,
and preview navigation. Today returns to the current month; First, Previous,
Next, and Last step through occurrences, with a compact position such as 4/5.
Navigation prepares distant months in the background and leaves date inclusion
choices unchanged.
Excluding dates from a series retains its standard rule with EXDATE; explicitly
choosing a few occurrences imports only those dates, as a finite RDATE series
or an individual event. The preview does not edit the source calendar.
Unreadable files and unsupported entries are listed under **Review issues**.
Each rejected event is listed separately with its title, date, timezone, reason
and UID. You can inspect or save its original source and copy its diagnostics.
Retained local import failures also offer **Retry import…** from the sidebar's
issues dialog; this opens a new review before applying anything.
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

Run the tests without a display, or launch a disposable demo:

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

**Preferences** uses a category sidebar beside its settings. Appearance groups
compact choices into Colors, Font, Interface, and Sidebar, with one pinned
live preview. Click a preview card to choose a style; the sample changes immediately,
while Apply saves the choices to the workspace. Colors offers light/dark modes,
Neutral/Warm/Cool presets, and a custom palette with three choices: accent,
surface tint, and weekend color. Text and borders adjust automatically for
contrast. The choice is saved per profile and applies to the workspace
and calendar previews. Calendar colors still come from their sources. The
sidebar separates the month overview, calendar controls, and agenda/search;
source-colored event cards keep their selection and keyboard focus cues. Choose
a calendar by name to create in it; its eye button controls visibility separately.
Read-only sources show a lock. Drag a calendar's name or color square to reorder
the list; its order is saved per profile without changing the creation calendar.
Appearance → Sidebar → Upcoming events offers event cards, cards grouped by
day, and a per-day timeline (the default). Search always uses event cards.
All-day events use full-width cards in the timeline; timed events keep their
clock column. Muted day captions sit below the stronger Next events heading.
Calendars and Next events share the same section-heading style.
Selected events use a stronger source-colored border in both the sidebar and
calendar. Directly selected events also have a vertical bar; companions selected
through a series mode have the border only. Month-view event text keeps its
normal weight.
Choose **Appearance → Interface → Button groups** for Underlined (default) or Filled
choices. Both styles retain the same placement and toolbar separator.
Interface also sets Square, Subtle (default), or Rounded corners.
Font sets the typeface and text size consistently for controls and painted
calendar text. Scrolling over those fields scrolls the page without changing
their values; type a size or use the field's arrow buttons/keyboard instead.
DejaVu Sans is the default typeface. System follows Qt's desktop font integration,
whose default can differ between a system installation and a virtual environment.
Select a named family here to use the same typeface in both.
Sidebar sets a custom title or hides it to save space. When hidden, its Preferences
button moves to the sidebar footer while retaining its horizontal position.
Colors offers weekend text color (the default), background shading, or no
highlight. Today keeps its marker as well as the chosen weekend treatment.
Year view offers Calendar tracks (default, option A) and Tinted day cells
(option C), with selectable previews. Apply changes the appearance immediately;
both choices use the same calendar summaries and navigation.

**Calendar sources** groups connected calendars by account. Refresh an account
to discover additional calendars, then check which ones to connect. Names in
Debacal are local aliases; the source name stays visible. Calendar colors can be
overridden or reset with Use server color. Apply saves these changes, while
Cancel discards changes since the last Apply. Disconnecting a calendar hides it
and stops its syncing, while retaining cached events, history, and queued edits;
reconnecting restores access to those local changes. Saved passwords are shared
by calendars in the same account and remain in the secure keyring.

* **Day / week / month / year:** switch with the toolbar or `D`, `W`, `M`, `Y`.
  `Page Up`/`Page Down` move backward/forward by the current view's period; `T` returns to today.
  The navigation buttons keep their position as month labels change.
  Headline and Today positions stay fixed across views. Month and week share
  day-column boundaries and weekday labels, including reserved scrollbar space;
  hiding week numbers keeps the empty gutter to preserve that alignment.
  Click the mini calendar to jump.
  In Day/Week/Month view, a small **TODAY** label marks the current date.
  Week and Month use a light header tint for the selected action date;
  Day omits this redundant selection cue. Adjacent-month dates and events
  are muted instead of shading their cells.
* **Year overview:** twelve month cards summarize which visible calendars have
  events each day. Calendar tracks show broad color bars beneath each date;
  Tinted day cells show soft color segments with solid edges. Each calendar
  appears once per day, including across multi-day events, in sidebar order.
  Busy days show +N for additional calendars; hovering shows all calendar names
  and event counts. Click a month heading to open Month view. A day selects its
  date; double-click it to create an all-day event.
  Today is bold and underlined; the selection keeps its separate marker.
  Click a week number to open that week. Week numbers appear by default in
  Month and Day/Week views too, and can be switched off in Calendar preferences.
  Right-click a day to inspect its events, including calendar names and color
  swatches, or open Day view. Search and paste
  previews work in Year view too; small windows scroll the overview.
  Individual event actions are available from the day's context menu or a
  detailed view. The original [design studies](../design/2026-11-year-overview/index.html)
  show these appearances alongside the unimplemented tiles option.
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
* **Keyboard selection:** arrow keys move selection and scroll
  only as far as needed to reveal an event. Left/right skip empty days, cross
  view boundaries, and choose the closest starting time on the neighbouring
  populated day. Hidden calendars and active search filters are respected.
  Selection modes are `F1` for one
  occurrence, `F2` for all events in its series, and `F3` for this and following.
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
  and calendar color; the calendar name is in the tooltip. Top-level results
  align with the search field. Repeating series expand using the disclosure at
  the right of their card; dated occurrences are indented below it, with bounded
  **More dates…** pages of ten dates for both finite and infinite series.
  Up/Down can focus this row; Enter loads the next page while retaining the
  event selection. After 100 displayed dates, navigate the calendar and choose
  **Show dates near current view** at the bottom of the expanded series to
  inspect another range.
  A series root selects the whole
  series; a dated child selects only that occurrence, even when the calendar is
  in all-events mode. Their row highlights are independent. Ctrl+click combines
  roots and children with those meanings. Up/Down transfers focus to the results
  so Delete, copy and edit act on that selection. The query stays open while
  browsing. Enter activates the focused result or paging action; Escape clears search and
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
  Undo/redo buttons are disabled when their next operation is unavailable.
  History survives restart. Shortcuts leave text fields' normal editing intact.
* **Controls:** date navigation sits beside the current month and range. The
  labeled toolbar groups View (including full-day expansion), Selection (modes
  and selection export), and Events (new event, undo, redo, history). Hover over icons for their meaning and
  current shortcut. Import/export and adding sources sit beside Calendars; right-click
  a calendar for its source and file actions. The gear opens Preferences.
  Sync has a fixed position at the bottom of the sidebar; click
  its status text for details. A warning icon shows errors, skipped resources,
  or conflicts; click it for full details and available recovery actions such
  as retrying synchronization, editing a connection, or retrying a file import.
  Other operation errors appear there too and can be dismissed after review.
  Successful actions do not show status messages below the calendar.
* **Creation calendar:** click a writable calendar name in the sidebar to choose
  where new events are created. The selected name is highlighted and remembered.
  Its separate eye button shows or hides that calendar. Editing existing events
  retains their original calendars. Short source lists stay fully visible;
  long lists scroll. Large text in a short window can scroll the sidebar while
  sync status stays fixed.
* **Preferences:** Calendar groups date language (system, English, German), week
  start, date/time formats, week numbers, visible hours, default event duration,
  and event-duration buttons. The default applies to keyboard and double-click
  timed creation; dragged ranges and existing events keep their chosen duration.
  Duration presets count elapsed minutes. In the event dialog, `24h` means
  24 elapsed hours and `1d` means one calendar day in the event's timezone;
  the two differ when clocks change. Moving Start retains this choice.
  All-day creation starts with one whole day.
  The time grid still expands for events outside the usual visible hours.
  Interface labels remain in English.
  Language changes month and weekday names independently of date/time format
  and week start. System language follows Qt's preferred UI languages, while
  regional date/time defaults follow the OS formatting locale (`LC_TIME` on
  Linux, with `LC_ALL` taking precedence). The language tooltip lists the detected values.
  Field explanations and formatting samples live in tooltips.
  Appearance controls colors, text size, button styles, corners, and sidebar layout.
  Calendar sources shows account settings when an account is selected, including
  discovery, passwords, and connecting calendars. Select an individual calendar
  for its name/alias, color, address, and access details. Local calendars and
  read-only subscriptions show only their relevant settings.
  Sync groups automatic sync intervals and conflict notifications. Keyboard
  shortcuts groups related actions and lets you assign several independent key
  combinations to each action. Use **+** for an alternative; each field records
  one combination. Multi-step key sequences are no longer supported; saved
  multi-step assignments are disabled without taking over their first key.
  Duplicate assignments within the same context are rejected. Dialog actions
  have their own section, so Enter can edit an event in the calendar and accept
  a form in a dialog. Settings survive restart.
  Apply saves changes and updates the workspace without closing Preferences.
  Save applies and closes; Cancel discards edits made since the last Apply.
* **Event editor:** Tab/Shift+Tab move through the form; labels have Alt-key
  mnemonics. Enter saves from form fields; Notes keeps Enter for a new line, and
  Ctrl+Enter saves from anywhere. In calendar-source setup, Enter discovers a new
  CalDAV account before saving its selected calendars. Start and End accept typed
  dates and times or open a shared month-and-time-range picker from the button
  inside each field. Notes uses the remaining space in the shorter dialog, with
  Save/Cancel beneath the wider preview on the right. Moving Start
  preserves duration; setting an earlier End moves Start back by that duration.
  The time strip labels both endpoints and brackets the duration. During a drag,
  an outline retains the original range beside the live time and duration. Its
  full-day toggle uses the same configurable shortcut as the main calendar.
  Hover over the left timeline to preview a new start/end time, then click to
  choose it. The event box on the right can be moved or resized independently.
  Mouse and arrow adjustments use five-minute steps; Alt+arrow uses one minute
  and Shift+arrow one hour. Dragging near an edge pans the visible hours within
  the day. Both endpoint clocks stay on the left, with short leader lines when
  their labels would otherwise overlap.
  Duration edits and preset buttons adjust End. All-day fields show the first and
  last included dates. Repeat controls show synchronized count and final-date
  fields; choose Indefinite for an ongoing rule.
  **Edit selection** uses `E` or `Enter` by default. Dialog accept, form-field
  accept, and close (`Esc`) can all be remapped in Keyboard shortcuts; popup
  choices and multiline text retain their own contextual keyboard behavior.
  Dialog actions consistently place Cancel before the highlighted closing
  action. Persistent forms use Save, temporary date/time picking uses OK, and
  import/export use their action names. Preferences Apply updates the view
  without closing. Ordinary single events do not show series-scope buttons.

### Working with event series

Choose **One event** (`F1`), **All in series** (`F2`), or **This and following**
(`F3`) before selecting events. Changing the mode updates the affected highlights
immediately, including for Ctrl-added selections. The toolbar and event editor
use the same segmented controls and remappable keys, including while typing in
the editor. Ctrl and Shift do not change the scope of
an action. Ctrl+click toggles selection and Ctrl+drag copies.

Operations use the union of selected events, avoiding duplicate changes where
selected series overlap. Multi-event edits apply changed common fields and
relative times while retaining untouched values. Select one series to edit its
recurrence configuration. Each batch mutation is one undoable operation; a
read-only or stale selected resource rejects the batch before any event changes.
Changing the calendar in the editor moves the affected events to that writable
calendar, using the same one/all/following selection. An unchanged calendar
choice preserves each source in a mixed-calendar selection. Transfers queue the
source removal and destination upload independently and can be undone together.

A following edit splits at the selected event's original recurrence date for
rule-defined occurrences, or its actual start for independently added dates.
Coincident added copies share that boundary. The
past keeps its identity; the future gets a new calendar UID and retains a
standard recurrence rule. For example, moving an endless Monday series from
one occurrence onward to Wednesday ends the Monday series before the selection
and starts an endless Wednesday series. Rule exceptions retain their original
positions after being moved. The complete split is one undoable
operation and synchronizes as ordinary calendar resources. Splitting at the
first occurrence updates the original resource without leaving an empty past.

Copy in `F2` mode for whole series, or `F3` for their future parts. Pasting keeps
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

The [sequence specification](SEQUENCES.md) defines syntax, normalization,
move/split behavior, correctness checks, and the operation test coverage.

The event editor separates reference-range controls from a full-height month
preview. Inline **Start**, **End**, and **Duration** fields define that range.
When editing, compact scope buttons in the heading use the main view's icons and
shortcuts. Under Schedule, choose **Single**, **Series**, or **Choices**; the latter
two share Daily, Weekly, Monthly, Yearly, and Custom controls. Choices are alternative dates from which one
event can later be confirmed. Frequency choices use icons with tooltips; the
separate **+** beside the schedule modes adds an occurrence with its own
date and time.

Choosing a regular frequency from Custom starts a clean regular pattern,
removing custom additions, exclusions, and individual range changes. Returning
to Custom restores that editor scope's previous custom draft, including its
text and individual ranges. Changes become permanent only when saved.

Click a preview date to add or remove an occurrence, or edit an occurrence's
time with the range picker. **Start**, **End**, and **Duration** define the
reference range. The reference need not itself occur: removing its occurrence
is allowed. **Add occurrence** starts with the reference time and duration.
First/Previous/Next/Last navigate the preview; Last is disabled for endless rules.

Every schedule has an editable definition. For compact sequences, instructions
are separated by spaces or commas and evaluated from a cursor at the reference:

* `+0d` emits an occurrence at the current cursor without moving it. At the
  beginning it includes the reference; `2*+0d` creates two separate occurrences
  there. Every finite instruction emits a separate event, including repeated
  starts. `none` denotes an empty schedule.
* `+2w`, `+3d`, `+1m`, `+1y` advance weeks, days, months, years. Units are
  mandatory. Month/year steps skip missing dates, following standard RRULE.
* `+1m!` or `+1y!` clamps missing dates while retaining the preferred day.
  From 31 October, repeated `+1m!` gives 30 November, 31 December, 31 January,
  28 February, 31 March. **Clamp** is offered when it can affect the schedule.
* `13.` chooses the next day 13; `3.2.` chooses the next 3 February.
  `3.2.2027` is exact, even before the cursor. Partial month dates end in a dot;
  dates including a year do not. Date-only entries use the reference clock.
* `14:30-13.` chooses the next day 13 at 14:30. `14:30` alone chooses that
  clock later today, otherwise tomorrow. Clocks are explicit; h/min offsets
  and unitless relative steps are not accepted.
* `4*+2w` repeats a step; `2*(+1w +2d)` repeats a group. Nothing implicitly
  includes the reference: `+0d 4*+2w` has five occurrences.
* A terminal `every(2w)` starts at its current cursor and continues indefinitely.
  If the last prefix event is already at that start, the tail continues it:
  `+0d every(1d, count=3)` has three events; `2*+0d every(1d, count=3)` has four.
* `every(1w, on=MO+WE)` selects Mondays and Wednesdays; `every(1m, on=-1FR)`
  selects each month's last Friday. `every(2w, on=MO+WE, week_start=SU)` defines
  its fortnightly weeks from Sunday. An off-selector reference is included
  only if a finite instruction such as `+0d` emits it.
* `every(1m!, day=31)` retains a preference for the 31st even when its reference
  is a clipped date such as 30 November. Without `!`, missing dates are skipped.
* For multi-day monthly/yearly ranges, **Skip**, **Shift**, and **Cut** control
  short-month overflow. In a non-leap year, a 28–29 January all-day event skips February, shifts to
  27–28 February, or is cut to 28 February respectively. Cut trims at the month
  boundary; later months recover the original duration. Intentional cross-month
  ranges keep their normal duration. Shift is disabled if the whole range is
  longer than a selected month. The corresponding text is
  `every(1m, overflow=skip|shift|cut, span=2)` with one chosen policy value.
* `every(1m!, count=13)` clamps for thirteen tail occurrences;
  `every(1d, until=13.11.2026)` includes dates through that local day. Count
  and until bounds are mutually exclusive; count measures appointments, not
  weeks or months. Earlier prefix events are separate except for the shared
  final cursor occurrence.

```text
+0d 13. +1w 18. 2. 3.2. +2w
+0d 4*(+2w) +3w 3*+2w
+0d 10:00 11:00 9:00 10:00 11:00
+0d +3w every(1w)
every(1w, on=MO+WE, count=12)
every(1m, on=-1FR)
```

The fourth example skips the second and third weekly occurrences while keeping
the reference. Regular tails and representable finite patterns compile to
standard RRULEs, including clamping with BYMONTHDAY/BYSETPOS. Irregular sets
use standard RDATE/EXDATE and detached exceptions. Coincident occurrences also
use distinct standard identities, so other calendar clients retain both events.

Valid matching text keeps its spelling and cursor position. Moving a whole
relative series reevaluates its instructions at the new reference, retaining
its calendar intent. Graphical edits regenerate exact concise text: omit years
when clear, use clocks for changed times, prefer dates for irregular gaps,
compress repeated strides and groups. Invalid pending input cannot save an
older valid schedule instead. Saves, splits and undo/redo retain text and
canonical schedules together.

Compact notation retains separate occurrences even when they start together.
Mixed all-day/timed occurrences, huge irregular date sets, and unsupported rule
combinations use the editable `schedule{…}` form, which directly records the
canonical rule, additions, exclusions and nominal-ID-to-range moves without
enumerating an infinite rule. Previously generated fallback text is offered in
compact form when it can now be represented exactly. There is always a sequence
string; there is no separate uneditable advanced-recurrence state.
See the [full syntax, normal form and correctness contract](SEQUENCES.md).

Custom sequence definitions also synchronize between Debacal instances. The ICS
master stores the exact expression, language version and anchor in
`X-DEBACAL-PATTERN` properties alongside standard RRULE/RDATE fields. Another
instance restores the editable definition and its authoring timezone, even when
its display timezone differs. See the [metadata protocol](SYNC.md#custom-series-metadata-between-debacal-instances).

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
VEVENTs, named IANA zones, embedded VTIMEZONE rules (including Windows names),
and floating local date-times. Embedded rules travel with the event through
edits and export. See [calendar format support](CALENDAR_STANDARDS.md).

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
every five minutes while the app is open. Preferences can pause it or change the
interval; pausing also suppresses the startup attempt. **Sync** / `Ctrl+R` still
works manually. Offline changes
stay in a transactional outbox. Calendar toggles use only local data. Individual
unsupported resources are retained in quarantine and listed individually under
**Issues**, opened from the sidebar warning/sync indicator. Each entry includes
the event, calendar, date/time, timezone and reason. You can inspect or save its
original ICS, copy diagnostics, and retry synchronization. Writable local sources
also allow importing a corrected file. These actions preserve the server original;
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
Live Nextcloud checks cover discovery, initial pull, recurring creation,
series/occurrence edits, exclusion sync, and cleanup using a disposable test
resource. See [the integration helper and sync implementation](SYNC.md).
The earlier Group-cache coverage gap remains a separate, unverified inventory
question; the test account does not establish that personal calendar's coverage.

## Validation

```sh
QT_QPA_PLATFORM=offscreen python -m pytest
QT_QPA_PLATFORM=offscreen python -m debacal --demo --screenshot /tmp/debacal.png
```

Tests cover year rollover and bounded pattern parsing, overlapping intervals,
timezones, atomic history and selective undo, durable outbox races, ICS recurrence
exceptions, sync conflict handling, and Qt editing/rendering/drag workflows.

For isolated range-query latency against generated, file-backed calendars:

```sh
.venv/bin/python scripts/benchmark_ranges.py --events 6000 --series 1200 --queries 24 --output /tmp/ranges.json
.venv/bin/python scripts/benchmark_ui.py --events 6000 --series 1200 --queries 24 --output /tmp/navigation.json
```

The first command separates source selection from complete occurrence queries,
checks answers against an independent reference, and records decoding/expansion
work. The second includes Qt layout, painting and calendar visibility writes.
Both visit distinct, non-monotonic date windows with no warm-up and report the
maximum and every individual sample. The data includes retained ICS text,
timezone definitions, ended/future rules, finite custom sets and exceptions.
`--source-root OTHER_CHECKOUT` compares the same workload against another checkout.
`--max-ms LIMIT` fails if any measured operation exceeds the chosen limit.

The broader workflow benchmark also measures year view, copy/paste, moving and
editing, undo/redo, dialogs, preferences, import previews, and conflict comparison:

```sh
.venv/bin/python scripts/benchmark_workflows.py --events 10000 --series 200 --iterations 3 --output /tmp/workflows.json
```

Each iteration uses a fresh process and a separate file-backed synthetic workspace,
with network sync disabled. The JSON includes each sample, maximum timings,
event-loop gaps, and peak process memory. No median is used as a passing metric.
See [the benchmark methodology](PERFORMANCE.md) for interpretation and limitations.

Paint geometry is reused while its inputs stay unchanged. Search uses a lightweight
case-folded projection and loads complete events only for matching candidates.
Typing is coalesced over 80 ms; arrows and Enter finish any pending search immediately.
Slot confirmation acts on release without waiting for the double-click interval.

## Remaining work

* Additional provider integration and OAuth.
* Broader recurrence and timezone interoperability, including advanced custom rules.
  Detached-only imports without a master, EXRULE,
  RANGE=THISANDFUTURE and sub-minute rules currently fail explicitly; see the
  [support notes](CALENDAR_STANDARDS.md).
* Progressive handling of exceptionally large import/edit batches on the UI thread.
* Linux distribution integration, accessibility/screen-reader validation,
  and broader desktop/window-manager coverage. Wheel build/install and isolated
  X11 workflow checks are included in the release review.

The code separates pure domain logic (`models`, `patterns`, `layout`, `recurrence`), SQLite
(`store`), standards/sync (`ics`, `sync`), and the Qt workspace (`ui`, `dialogs`,
`calendar_widgets`). Event authoring has separate modules for recurrence drafts
(`event_schedule`), fields and range pickers (`date_time_fields`,
`date_time_picker`), preview/navigation (`series_preview`, `preview_navigation`),
and layout/controller (`event_dialog`). It has no web backend or external service
requirement.
