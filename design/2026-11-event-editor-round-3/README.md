# Event editor, round 3: primary event and reusable picker

Open [the offline gallery](index.html). The main editor defines the primary
event on the left and previews its additional occurrences in a compact month
on the right. Start, End and Add occurrence open the same date/time-range picker.

These are native Qt interaction prototypes for review. They do not replace the
application’s event editor or write to saved profiles. The windows use temporary,
synthetic example events.

## Primary event and occurrences

Start and End stay attached to the primary event after extra occurrences have
been created. Preview navigation does not turn these fields into another
occurrence’s timing controls. The primary remains directly editable through
the written fields and picker buttons.

Changing the written Start keeps the current duration and moves End with it.
Editing End or Duration updates the primary event’s finish. Changing the
primary event re-evaluates the authored sequence: relative dates follow its
anchor, while explicit dates and clocks retain the sequence grammar’s meaning.
It does not silently switch which occurrence the primary fields control.

Duration has its own field and quick choices. Changing duration adjusts End.
The controls for the primary event’s timing are kept together, and the custom
sequence is available without the previous lengthy explanation in the dialog.

The month preview keeps its date-based add/remove interaction. A dedicated
indicator identifies the primary occurrence. Its navigation controls have two
separate jobs:

- Month arrows change the preview’s calendar page.
- Occurrence navigation visits the primary, previous or next occurrence.
  The first button returns to the primary event, even if another occurrence
  happens earlier in time.

Add occurrence opens the shared picker on the selected preview date, with the
primary event’s starting clock and duration as defaults. This supplies the same
date/range interaction when an added occurrence needs a different clock. The
new occurrence can have its own duration; choosing that range does not modify
the primary event.

The duration-button preference control changes only this preview window’s
temporary choices. It is not a saved application preference in this study.

## Combined date and range picker

The picker puts a minimal month beside a narrow vertical time strip. Hover
feedback shows the time at the pointer. Written timing fields remain available
in the editor for keyboard input.

For a same-day event, both boundaries can be adjusted in the strip, whichever
editor field initially opened the picker. Moving the span preserves duration.

For an event crossing dates, the Start picker opens the starting day and shows
the event continuing below the visible range. Only its start boundary is
editable there. The End picker opens the finishing day, shows the range
continuing from above, and exposes only the end boundary. Neither view creates
a false editable endpoint at midnight.

Picker changes remain a draft until Apply; Cancel leaves the parent values
alone. The same component is used for both primary fields and an added
occurrence.

## Run the prototype

From the repository root:

```sh
.venv/bin/python design/2026-11-event-editor-round-3/prototype.py
```

Open a particular initial state with one of:

```sh
--scene same-day
--scene start
--scene end
--scene occurrence
--scene changed-primary
```

Appearance options are `--theme light` or `--theme dark`, and
`--buttons underlined` or `--buttons filled`. These use the application’s theme
and control styles. The default state is `--scene editor`.

Useful checks are changing the primary date after adding a series, adjusting
either side of a same-day range, inspecting both sides of an overnight event,
adding an occurrence with another clock, and using the primary/previous/next
navigation separately from month paging.

## Screenshots and reproduction

The gallery uses only local HTML/CSS/JavaScript and PNG files. It can be opened
directly without a server or frontend dependencies. Main editor views have
light and dark screenshots; picker and interaction detail views use the light
appearance. Click an image for Fit/100% viewing or to open the original PNG.

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-event-editor-round-3/render.py
```

The generated images are:

- `images/editor-light.png` and `editor-dark.png`
- `images/picker-same-day-light.png`
- `images/picker-start-light.png` and `picker-end-light.png`
- `images/occurrence-light.png`
- `images/changed-primary-light.png`

The gallery’s text describes the proposed separation between primary event,
picker and preview. Further integration must preserve the app’s recurrence,
timezone, keyboard and undo behavior.

The native renderer exercises both button styles. It checks that primary fields
remain attached to the primary event after series creation and preview
navigation, duration updates End, and the picker’s Apply/Cancel remain
transactional. It also checks same-day endpoint/body/empty-space gestures,
painted hover-time feedback, multi-day continuation with only the real endpoint
editable, and ignoring a multi-day body drag.

Occurrence checks cover defaulting to the primary clock even when a different
preview clock is selected, preserving the primary duration when a longer added
range is chosen, date toggles, chronological navigation with an earlier extra
occurrence, returning to the primary, and thirteen same-day chips remaining
scrollable with stable width and direct removal targets.
