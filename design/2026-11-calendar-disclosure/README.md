# Calendar disclosure alternatives

[View the comparison](comparison.png), with collapsed states above expanded
states. The [SVG version](comparison.svg) is also available.

- **A:** Chevron embedded in a divider; additional calendars expand inline.
- **B:** Chevron beside the existing Calendars heading; no extra control row.
- **C:** Icon-only control row below the main calendars.
- **D:** Header icon opens additional calendars in a popover.

These are schematic design proposals. Option A is implemented in the application.
Calendar rows use the same typography, color indicators, and eye controls.

The implemented divider has no permanent caption. A **More calendars** label
appears while dragging; the target keeps its size throughout the interaction.
Current native captures are saved beside these examples as `implemented-*.png`.

To check real mouse dragging under Xvfb and Openbox, including both empty-group
destinations and hover expansion, run from the checkout root:

```sh
xvfb-run -a -s '-screen 0 1600x1200x24' bash design/2026-11-calendar-disclosure/native_check.sh
```

The check uses synthetic calendars in an in-memory database. It also requires
the X11 XTest library and keeps window-manager logs local.

References: [Apple disclosure controls](https://developer.apple.com/design/human-interface-guidelines/disclosure-controls),
[PatternFly expandable sections](https://www.patternfly.org/components/expandable-section/),
[Spectrum accordion density](https://spectrum.adobe.com/fragments/components/accordion/option-details),
and [GNOME popovers](https://developer.gnome.org/hig/patterns/containers/popovers.html).

Regenerate from the checkout root:

```sh
.venv/bin/python design/2026-11-calendar-disclosure/render.py
```
