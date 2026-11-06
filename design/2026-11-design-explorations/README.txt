Debacal design exploration — questions and alternatives

Open index.html in a browser. This round deliberately compares different
hypotheses rather than presenting all prior questions as defects to remove.
Start with the three native sidebar captures, then compare header alignment,
weekends/grid/all-day controls, full week, month, narrow window, search, and a
bright-yellow source. All data and assets are local. No web server is needed.

The alternatives:
  Sources & agenda: persistent source controls versus dated event objects,
    warm weekend shading, open date header, integrated all-day hover plus.
  Shared list language: an integrated slate navigation pane, aligned flat rows,
    outlined controls, cool weekend shading, continuous grid, persistent
    all-day creation strip. This consciously spends more chrome on discovery.
  Typographic: circles and text emphasis, a flatter control language with inline
    captions, muted red weekend labels, quiet date header, gesture-only all-day
    creation. This consciously accepts less obvious action/selection boundaries.

Every alternative retains B's neutral base and source-tinted main event cards.
Creation calendar and selected event are different kinds of state. Aligning all
their marks or making their selection identical is a choice, not a prerequisite
for consistency. The gallery gives 11 question-by-question answers, 34 options,
benefits/costs, a provisional recommendation, confidence, and primary sources.

These are sample-only Qt prototypes. No production files or settings changed.
They use an in-memory database, disabled sync, the earlier study's synthetic
sample fixture and one additional six-week recurring event for search hierarchy.
Date is frozen to 6 November 2026. No accounts, credentials, personal ICS files,
or user profiles are read or modified.

Reproduce from the repository root using the app's existing Python environment:

  QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-design-explorations/render_explorations.py --all
  .venv/bin/python design/2026-11-design-explorations/build_gallery.py

Or use --direction sources, shared, or type to capture one direction. Separate
processes isolate transient Qt overrides. Helpers from the previous design
studies are reused without editing those earlier artifacts.

Files:
  exploration_base.py      Shared palette/weekend/header-grid/all-day hypotheses.
  direction_sources.py     Source controls and event timeline cards.
  direction_shared.py      Shared sidebar navigation model.
  direction_type.py        Flatter typographic sidebar and header.
  render_explorations.py   Native synthetic captures and component-region crops.
  build_gallery.py         Offline viewer builder; standard-library only.
  questions.json           Explicit reasoning, alternatives, confidence, sources.
  images/                  24 native captures; no raster image editing involved.

Limitations: these are design proposals, not complete production implementations.
The hover-plus example shows an empty all-day cell; occupied-day discovery still
needs interaction review. Narrow calendar title clipping remains visible. Font
rasterization depends on the desktop. Static renders cannot establish user-test
results, universal aesthetic correctness, or complete accessibility conformance.
Firm guidance is limited to readable content, user-required source hues,
predictable control placement, distinct states, and accurate geometry. Most
appearance choices remain contextual and are explicitly identified that way.

Checked: all 24 native image states and dimensions; small-window layouts;
expanded search hierarchy; weekend text contrast and yellow-source stress;
gallery image loading, three-way comparison, scale controls, question expansion,
and narrow browser layout. The gallery checks used headless Chromium with a
temporary profile. Production-app tests were not run for these isolated mocks.
