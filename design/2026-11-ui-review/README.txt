Debacal design review — 6 November 2026

Open index.html in a browser. Everything is local and works without a web server
or external assets. Choose A/B/C, a sample state, and optionally compare with
the current interface. “100%” allows inspection at the screenshot's original
size; “Fit” scales the image to the browser width.

This is a review artifact, not an implementation. The production application is
unchanged. Prototypes apply process-local Qt palette, stylesheet, layout, and
paint overrides. They run against an in-memory database with synthetic events,
disable synchronization, and freeze the date to 6 November 2026. No credentials,
personal exports, or profile data are used or written.

Recreate all 20 screenshots from the repository root:

  QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-ui-review/render_mockups.py --all

Or pass --concept current, a, b, or c to render one direction. Each direction
must run in a separate process so that its Qt overrides cannot leak into another.
The renderer requires the app's existing PySide6 dependencies. No new dependencies.

Examples: 1440 x 960 week/month/yellow, 1030 x 690 smaller week, 980 x 680 import.
The bright-yellow state changes only the synthetic Work calendar color, to test
legibility when a server provides a difficult source color. Import has one of
four events excluded to show the date inclusion state. These are static images;
they do not claim to demonstrate new interaction behavior or formal accessibility
conformance. Rendering can vary slightly with the installed desktop font and Qt.

Files:
  index.html          Self-contained comparison viewer and design rationale.
  audit-data.json     Code-level review, measured contrast samples, sources.
  mockup_base.py      Shared synthetic sample data and process-local palette tools.
  concept_a.py        Familiar refinement, smallest layout change.
  concept_b.py        Calendar first, explicit two-row header and neutral chrome.
  concept_c.py        Clear and compact, source stripes and light event cards.
  render_mockups.py   Repeatable capture of the current app and all proposals.
  images/             Synthetic screenshots for every direction and state.

Principles: proximity and similarity (Nielsen Norman Group), restrained desktop
typography and attention (GNOME HIG), text/indicator contrast and use of color
(W3C). Direct source links and caveats are included in the viewer and audit JSON.
