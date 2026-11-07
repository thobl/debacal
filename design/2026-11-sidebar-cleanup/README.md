# Sidebar cleanup: background review

Open [the offline gallery](index.html). Both alternatives use the updated native
application with synthetic calendars, an in-memory database, fixed dates and
network sync disabled.

- **A · Current surfaces:** overview and footer use the canvas background;
  calendars and agenda use the source-panel background.
- **B · One sidebar surface:** the complete left column uses the existing
  calendars-and-agenda background. Headings, spacing and thin section rules stay.

The gallery compares Light/Dark appearances, a normal footer, a synthetic import
warning, and the title-hidden state. Footer details make the background transition
easy to inspect; full sidebar and full app views show it in context.

The right message bar, G-selection workflow and profile note are removed
from the application. **Decision: B is now applied in the application.** The
footer controls also have equal spacing above and below for vertical centering.
The gallery preserves the comparison that informed the decision; its renderer
can still reproduce both surface alternatives.

I would choose B because the common surface makes the left column read as one
unit. The warning icon and its color distinguish issues without needing a fourth
surface treatment. A is useful if the overview should remain a separate block.

## Reproduce

From the project root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-sidebar-cleanup/render.py
.venv/bin/python design/2026-11-sidebar-cleanup/build.py
```

`metrics.json` records the native section positions and reclaimed bottom space.
The renderer checks that both background variants retain identical geometry,
that no profile note or main message bar remains, and that the proposed uniform
surface reaches the overview, native date cells and footer.

The miniature uses an explicit surface role for that preview instance. Date
popups elsewhere retain the normal canvas surface. No application globals or
user preferences are modified by the renderer.

Validation: the application suite passed (1,612 tests and 141 subtests).
Headless Chromium verified all twelve background/state combinations, local
images, the native-size toggle and narrow-browser layout.
