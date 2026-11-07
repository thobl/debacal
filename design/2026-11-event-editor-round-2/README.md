# Event editor, round 2: single first

Open [the offline gallery](index.html). This round refines one flow rather than
offering three large layouts. It starts with one event and reveals the series
controls when Repeat is enabled. The gallery leads with a full-size single-event
view; its tabs show how that same dialog expands.

## What changes in the application

Automatically reconstructed sequences now use explicit clocks for time changes.
They no longer introduce relative `h` or `min` steps. Relative date units remain,
and existing saved offset expressions stay readable with their original meaning.
Opening a saved series or changing its title does not rewrite that text.

The larger event-editor changes shown here are **prototypes for review**, not a
replacement for the production dialog. This includes the minimal date month,
time strip, start/duration fields and configurable duration buttons.

## The proposed flow

For one event, the month simply chooses a date. It does not need an event card
inside that date. A narrow vertical time strip supplies direct start/duration
selection without the full calendar’s day heading or other events. Written
date, starting-time and duration fields remain available for keyboard entry.
The quick duration buttons provide a few common values.

Enabling Repeat reveals the series controls. Date clicks add or remove extra
dates in one step. Different starting times can be set for individual
occurrences, while the custom sequence stays editable and the visual changes
produce a compact equivalent expression. Starting-time edits affect the chosen
occurrence; duration remains common to the series in this prototype.

Removing an occurrence does not require selecting it first:

- Clicking a date toggles that date’s extra occurrences together.
- Clicking the visible × on a time chip removes exactly that occurrence.
- Clicking the chip’s body selects it for time editing.
- Clicking empty time-strip space adds another time in series mode.

The first event remains the protected anchor. The chip × is a direct remove
control, distinct from the obsolete calendar marker that used to indicate an
excluded date. Keeping separate body and remove targets avoids making every
attempt to edit a time destructive. The remove target’s size is worth assessing
in the native prototype, especially with several times on the same date.

Start + duration is the default. The optional End view explores supplying an
explicit finish instead. Date choices use the same month preview rather than a
second date-popup calendar.

The duration-button preferences window is a mockup: it changes the temporary
prototype only. It does not change a saved profile or write application settings.

## Run the prototype

From the repository root:

```sh
.venv/bin/python design/2026-11-event-editor-round-2/prototype.py
```

Optional starting states:

```sh
.venv/bin/python design/2026-11-event-editor-round-2/prototype.py --scene series
.venv/bin/python design/2026-11-event-editor-round-2/prototype.py --scene times
.venv/bin/python design/2026-11-event-editor-round-2/prototype.py --scene end
.venv/bin/python design/2026-11-event-editor-round-2/prototype.py --scene preferences
```

Add `--theme dark` to inspect the dark appearance. Closing the window discards
its temporary changes. The prototype uses synthetic events and does not access
saved calendars, credentials or network sync.

Useful comparisons are creating one event without enabling Repeat, adding and
removing dates, placing several different times on one date, removing an
unselected occurrence directly, and changing the duration presets.

## Review assets and reproduction

The gallery is self-contained HTML/CSS/JavaScript with local PNG files. It needs
no server or frontend dependencies and makes no network requests. The normal
single and series states have light/dark screenshots. The time, preferences and
end details are shown in light appearance.

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-event-editor-round-2/render.py
```

The native renderer checks date/time keyboard entry, dragging a start and
duration, moving/resizing the selected time span, one-click date toggles,
one-click removal of an unselected occurrence while preserving its neighbors,
and adding another time on the same day. It also verifies independent starting
time edits, compact sequence roundtrips, removing a regular-series date,
returning to the compact single-event layout, applying valid duration presets,
rejecting invalid presets, preserving the current duration when presets change,
and choosing a fixed end through the same month picker.

The gallery passed 144 headless Chromium checks at 1440 px and 390 px widths.
All seven images loaded at their recorded native sizes. Scene and appearance
switching, disabled dark detail states, keyboard tab navigation, image zoom,
fit/native sizing and Escape worked. Neither width had horizontal page overflow,
and no browser script or resource errors were reported.

Prototype limit: calendar weeks expand to show up to six times per date. More
than six uses an overflow menu. A production version should preserve direct
removal for crowded dates too, for example through a scrollable expanded date.

These studies do not establish all-day, overnight or timezone-edge-case
behavior. Those cases must retain the application’s recurrence and timezone
rules when this flow is integrated into the production editor.
