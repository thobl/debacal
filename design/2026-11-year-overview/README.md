# Year overview studies

Open [index.html](index.html) to compare three calendar-presence treatments:

- **A — Calendar tracks:** broad, stacked source-color bars below the date.
- **B — Calendar tiles:** larger blocks beside the date.
- **C — Tinted day cells:** source-color background segments with solid edges.

The gallery offers light/dark and desktop/three-column views. The November
details all use the same synthetic events, including a three-day workshop,
duplicate appointments in one calendar, and a six-calendar busy day. `+N`
counts additional calendars; it does not count individual appointments.

`options.py` subclasses the production year view for its layout, but filters and
aggregates visible calendars and removes individual event hit targets. No
production calendar rendering or interactions are changed by these studies.

Recreate all 18 screenshots and the geometry report from the repository root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python design/2026-11-year-overview/render.py
```

The renderer uses in-memory data, runs offline, and checks source deduplication,
hidden-calendar filtering, multi-day coverage, and complete month/day geometry.
