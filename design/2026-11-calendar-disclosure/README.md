# Calendar disclosure alternatives

[View the comparison](comparison.png), with collapsed states above expanded
states. The [SVG version](comparison.svg) is also available.

- **A:** Chevron embedded in a divider; additional calendars expand inline.
- **B:** Chevron beside the existing Calendars heading; no extra control row.
- **C:** Icon-only control row below the main calendars.
- **D:** Header icon opens additional calendars in a popover.

These are schematic design proposals. Option D is now implemented in the application.
Calendar rows use the same typography, color indicators, and eye controls.

The earlier divider implementation was replaced by a header overflow button
and an independently scrolling popup. See the [current native previews and
drag check](../2026-11-calendar-popup/README.md).

References: [Apple disclosure controls](https://developer.apple.com/design/human-interface-guidelines/disclosure-controls),
[PatternFly expandable sections](https://www.patternfly.org/components/expandable-section/),
[Spectrum accordion density](https://spectrum.adobe.com/fragments/components/accordion/option-details),
and [GNOME popovers](https://developer.gnome.org/hig/patterns/containers/popovers.html).

Regenerate from the checkout root:

```sh
.venv/bin/python design/2026-11-calendar-disclosure/render.py
```
