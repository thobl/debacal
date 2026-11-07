Debacal — applied design decisions and remaining choices, 7 November 2026

Open index.html in a browser. All assets are local; no web server is required.
Applied application changes are saved in local commit db9f7aa. The review files
are separate from the application and do not activate candidate layouts.

The first section shows actual production changes: distinct source controls and
event cards without a timeline; B-like sidebar section boundaries; muted weekend
text without week/day background shading; continuous columns; and no extra
all-day creation strip or plus. Neutral/Warm/Cool are actual saved interface
preferences under Appearance. Source calendar hues and calendar behavior remain.

The other sections are isolated prototypes, not applied app layouts:
  H1/H2: compact navigation cluster beside the period.
  H3/H4: Previous/Next flank the month, Today below Previous; month and range
    start at the first vertical calendar boundary.
  H5/H6: month aligned to that boundary, navigation on the range line.
  Odd/even pairs compare filled choices without an inner border against square
    underlines with an explicit header/calendar separator.
  H7: H3 with a smaller 20px title and 28px controls, preserving alignment rows.
  S1: search field above the dynamic heading (current app baseline).
  S2: compact Agenda/Results heading beside an always-visible field.
  S3: search on demand in one fixed header slot; Ctrl+F works.
  S4: global-looking search under the overview, farther from its results.
  A1: current event cards; A2: day headings; A3: separate rails within each day.
    A2/A3 revert to ordinary cards in search, including folded series.

Headers include wide/narrow context crops and complete windows. The blue first-
column guide is an optional review annotation. metrics.json records measured
month/grid positions, rather than approximating their alignment. Search options
include both agenda and query states at 100% and 140% text size.

All captures use synthetic sample data, an in-memory Store, disabled sync, and a
fixed date of 6 November 2026. No real accounts, credentials, personal exports, or
profiles are read. Old design artifacts are not imported or patched. Current app
modules are used directly; prototype code is never imported by production code.

Reproduce using the app's existing environment from the repository root:

  QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-decisions/render.py --all
  .venv/bin/python design/2026-11-decisions/build.py

Individual capture flags: --production neutral|warm|cool; --header split-filled
(and the other H keys from render.py); --search baseline|inline|reveal|global;
--agenda baseline|grouped|day_rail. Fresh processes isolate every prototype.

The production validation includes 1,328 tests and 135 subtests, including source
color preservation, preferences persistence/isolation, cards/tree input, visible
focus, large text, performance, creation gestures, and alignment when either the
timed or all-day lane needs a scrollbar. Launch/import IPC tests require local
socket access outside this workspace's sandbox.
All 59 PNGs were verified. Gallery state switching, comparisons, alignment guide,
scale controls, and narrow browser layout were checked in headless Chromium.

These prototypes are review candidates. Dense event names still truncate in
narrow columns. The compact header trades target size for space; the grouped
agenda trades visible event count for clearer day boundaries. Font rendering
depends on the desktop. No prototype appearance is claimed to be universally
correct or to establish complete accessibility conformance.
