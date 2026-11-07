# Design round 4

Open [the offline gallery](index.html) in a browser. Screenshots come from native
Qt widgets with synthetic events in an in-memory database. Sync is disabled.

Applied in the application:

- S3 on-demand search, with synchronous first-click focus and Ctrl+F.
- Preferences → Appearance → Upcoming events: A1 cards, A2 day groups, A3 day
  timeline, defaulting to A3. Search keeps ordinary cards and series folding.
- Read-only calendar locks, persistent per-profile source ordering by drag and
  drop, and the selected creation calendar's underline without its focus box.

Still proposals:

| Options | Navigation | Controls |
| --- | --- | --- |
| N1 / N2 | H3/H4 placement; arrows flank reserved-width month | Underlined / flush filled |
| N3 / N4 | Same placement at H7 compact density | Underlined / flush filled |
| N5 / N6 | H5/H6 with date range before the navigation buttons | Underlined / flush filled |
| L1 | Outer headings; separate clock column before event cards | A3 closest to current app |
| L2 | Event frames share the source/search/headings edge | Clock inside card frame |
| L3 | Utility lane for squares, locks, clocks and disclosures | Shared second content edge |

The header gallery compares May and September in English/German at 1440 × 960
and 1030 × 690. Next and Previous positions are asserted equal across both
months. In the standard split layout the label column is 211 px; “May 2026” is
119 px and “September 2026” is 205 px. The resulting visible gap is intentional
and left for review. N5/N6 remove that month-dependent gap by putting navigation
after the fixed-width date range.

Filled groups have zero layout margins. One clipped rounded surface paints the
selected segment up to the outer outline, with no separate selected border.
Underlined groups retain a separator between controls and the calendar.

Sidebar studies include agenda/search states at 100% and 140% text sizes. The
measurement overlay uses actual Qt coordinates. L3 explicitly prioritizes a
shared content edge over title/search width; its Calendars heading remains on
the outer edge. Search occurrences retain native indentation and folding.

## Reproduce

From the project root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-round-4/render.py all
.venv/bin/python design/2026-11-round-4/build.py
```

`metrics.json` records measured native geometry. The renderer checks stable
navigation, month/grid alignment, fitting range labels, accessible controls and
edge-to-edge filled groups. Gallery code is isolated from production imports.

Validation: full suite passed (1,369 tests and 135 subtests); the 47 focused
agenda/search/calendar-order checks also passed after final integration.
Headless Chromium verified every gallery option, local images, comparison
controls, guides, native-size toggles and narrow-browser layout.
