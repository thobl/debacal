# Event creation dialog: visual time editing

Open [the offline review gallery](index.html) to compare three native Qt
prototypes. Screenshots show the same synthetic series in light and dark
appearances. Click an image for a larger view, including a 100% size option.

## Application changes and proposals

The production changes accompanying this review are:

- Relative sequence steps support `min` for minutes and `h` for hours, including
  multipliers. For example, from 09:00, `2*+45min` adds 09:45 and 10:30. `m`
  continues to mean months, and an omitted unit continues to mean weeks.
- The calendar selector includes calendar-color swatches, and preview event
  blocks follow the selected calendar's color.
- Obsolete added/skipped-date symbols and their legend are removed from the
  preview. The ability to add and remove dates remains.

The larger layouts in this directory are **prototypes awaiting a choice**. They
do not replace the production dialog. Start + duration, visual per-occurrence
time editing and integrated start-date selection are explored here together.
The prototype windows use synthetic events and do not save or sync changes.

## Options

| Option | Date selection | Visual time editing | Main tradeoff |
| --- | --- | --- | --- |
| A · Month + day planner | Persistent month | Vertical time column for the selected day | The day planner shares the dialog width with the month. |
| B · Month / week | Month view in a shared preview | Switch to a full week grid | More room for a week, but distant dates need navigation or view changes. |
| C · Month + time ribbons | Persistent month | Horizontal rulers for individual dates | Compares scattered dates clearly; its time direction differs from the main calendar. |

A is my suggested starting point if keeping the current month-based date
workflow is the priority. B may suit users who primarily arrange a busy week.
C deserves a comparison when the main task is changing hours across dates that
are far apart. These are interaction tradeoffs, rather than one objectively
correct layout.

All three studies use the same direction:

- Start + duration is the default. Changing the start keeps the duration and
  shifts the end. An explicit End option supports specifying the finish.
- The existing preview supplies date selection; a second date popup is not
  needed.
- Visual time adjustment has a simple clock-text fallback for precise values.
- The duration is clearly identified as common to the series.
- An event chip selects one occurrence for time editing; calendar date toggling
  and occurrence selection are separate targets, including for multiple events
  on the same day.
- Visual changes update a compact equivalent sequence. Custom syntax remains
  an editable representation rather than becoming a list of full dates.

## Run interactively

From the project root:

```sh
.venv/bin/python design/2026-11-event-editor/prototype.py --option a
.venv/bin/python design/2026-11-event-editor/prototype.py --option b
.venv/bin/python design/2026-11-event-editor/prototype.py --option c
```

Each option also accepts `--theme light` or `--theme dark`:

```sh
.venv/bin/python design/2026-11-event-editor/prototype.py --option a --theme dark
```

Close the preview window to discard its temporary changes. These experiments
are isolated from saved calendars, profiles, credentials and network sync.

Useful comparisons are choosing dates across several weeks, adjusting one
occurrence without moving its neighbors, placing several times on the same
day, changing the anchor date while keeping duration, and switching between
Duration and End.

After selecting an occurrence, Up/Down shifts it by 15 minutes; Shift+Up/Down
uses one hour. Delete removes a selected extra occurrence, while the first
event remains the anchor. Reset restores the synthetic example. The clock
fallback accepts a whole time value, and duration accepts human text as well
as the preset buttons.

## Review assets

The gallery is self-contained HTML/CSS/JavaScript and local PNG files. It makes
no network requests and needs no web server or frontend dependencies.

- `images/a-light.png`, `b-light.png`, `c-light.png`: layout overviews.
- `images/a-dark.png`, `b-dark.png`, `c-dark.png`: the same layouts in dark mode.
- `images/a-selected.png`, `b-week.png`, `c-selected.png`: time-editing details
  in the light appearance.
- `images/a-end.png`: the optional explicit End control.

These are native widget screenshots, not promises of completed production
behavior. All-day, overnight and daylight-saving edge cases must retain the
application's recurrence and timezone rules when a layout is integrated.

## Reproduce and validate

From the project root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-event-editor/render.py
```

The renderer produces the ten screenshots at 1240 × 800 and exercises all
three prototypes: pointer selection of event chips, arrow-key time changes,
Delete, multiple times on the same day, compact expression roundtrips, moving
the first date while keeping duration, and the explicit End mode's fixed finish.
It also checks pointer drags and Enter committing an exact occurrence time
without closing the dialog.
Option C's ribbons move starting times; their blocks do not have resize handles.
The common duration can be changed with its text field or presets.

The offline gallery passed 90 headless Chromium checks at desktop (1440 px) and
narrow (390 px) widths: all ten local images loaded at their native size, light
and dark switching worked, detail tabs supported keyboard navigation, image
zoom supported fit/100% modes and Escape, and neither layout overflowed the
page horizontally. No browser script or resource errors were reported.
