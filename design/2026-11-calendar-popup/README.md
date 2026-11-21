# Calendar popup

Native captures of the additional-calendar overlay, using synthetic calendars
in an in-memory profile. The compact popup stays inside the main window and
scrolls independently from the main list.

- [Normal sidebar](implemented-normal-detail.png)
- [Open popup](implemented-popup-detail.png)
- [Dragging into the popup](implemented-drag-detail.png)
- [Long scrolling popup](implemented-scroll-detail.png)

Run the local native check from the checkout root:

```sh
xvfb-run -a -s '-screen 0 1600x1200x24' bash design/2026-11-calendar-popup/native_check.sh
```

This requires X11 XTest and Openbox. It checks real mouse dragging in both
directions, hover opening, fixed geometry over refreshes and drag round trips,
Escape cancellation, empty destinations, independent scrolling, and keeping
the current creation calendar in the main list.

The script saves full native screenshots and close-ups beside its source as
`implemented-{normal,popup,drag,scroll}{,-detail}.png`.
