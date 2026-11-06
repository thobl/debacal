Debacal — Option B refinement, 6 November 2026

Open index.html in a browser. Choose open lists (B1) or soft panels (B2), switch
the sample state, compare with the previous B, and optionally show alignment
guides. All assets are local; no server or external dependencies are needed.

This is a second design study, not an application change. It retains B's neutral
surfaces and source-tinted event cards. Both variants keep View, Selection, and
Events together, use aligned header rows, neutral mini-calendar weekends, aligned
sidebar bars and titles, shared row selection, and a 4px spacing scale. The same
weekday typography and continuous grid carry across week, month, and import.
The per-day all-day action labels are removed while the actual pinned lane and
creation gestures remain. The now marker is muted terracotta behind event cards.

B1 separates lists with whitespace. B2 adds white list surfaces and a subtle
outer border. Only that surface treatment differs; the header is the same.
The gallery explains the design choices and tradeoffs point by point.

All captures use the prior study's synthetic sample data, an in-memory Store,
disabled sync, and a fixed date. The original B is captured again with the same
selection so before/after states are comparable. No real accounts, passwords,
personal calendar files, or profiles are read or modified.

Recreate all 18 images and update embedded alignment-guide coordinates:

  QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-ui-refinement/render_refinements.py --all

Or use --variant original, open, or panels to capture one direction. A fresh
process per variant prevents palette, layout, and painter overrides from leaking
between variants. The existing app dependencies suffice. The earlier study at
design/2026-11-ui-review supplies the shared sample-data helper and original B.

Files:
  index.html             Offline comparison viewer and design decisions.
  header_styles.py       Group order, responsive unit wrapping, baseline alignment.
  sidebar_styles.py      Palette, row alignment/states, and the two list surfaces.
  calendar_styles.py     Preinstalled Qt subclasses for grid/typography refinements.
  render_refinements.py  Repeatable synthetic screenshot harness.
  images/                Six states for each variant, plus measured widget geometry.

The regular images are 1440x960; narrow is 1030x690; import is 980x680. The current
creation target is Personal. Agenda selection is shown independently and follows
the actual calendar selection signal. Search uses the query "work". Yellow sets
the Work source hue to #ffff00. Import excludes one of four example events.

Validation: native Qt capture, wide/narrow layout inspection, matched source-bar
coordinates, and calendar alignment with a visible timed scrollbar. The gallery's
state controls, image loading, comparison, alignment overlay, Fit/100% controls,
and narrow page layout were checked in headless Chromium. Static images
do not establish complete keyboard or accessibility conformance. Long-title
clipping in dense week columns remains visible. Font rasterization may vary by
desktop. The production app and its tests are outside this design-only change.
