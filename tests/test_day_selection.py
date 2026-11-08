"""Selected calendar dates drive commands independently of mouse hovering."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.event_clipboard import EventClipboard
from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo("Europe/Berlin")
FIXED_NOW = datetime(2026, 11, 4, 13, 47, 29, tzinfo=ZONE)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    callback_errors = []
    with patch("debacal.ui.local_zone", return_value=ZONE), \
            patch("debacal.event_dialog.local_zone", return_value=ZONE), \
            patch("debacal.calendar_widgets.local_zone", return_value=ZONE), \
            patch("debacal.ui.now", return_value=FIXED_NOW), \
            patch("debacal.calendar_widgets.now", return_value=FIXED_NOW), \
            patch("sys.excepthook", side_effect=lambda _, value, __: callback_errors.append(value)):
        store = Store(":memory:")
        window = MainWindow(store, enable_sync=False)
        window.go_date(FIXED_NOW.date())
        window.show()
        app.processEvents()
        try:
            yield window, store, app
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            store.close()
            assert callback_errors == []


def event(**changes):
    start = datetime(2026, 10, 20, 16, 17, 23, 456789, tzinfo=ZONE)
    return replace(Event("source", "local", "Synthetic event", start,
                         start + timedelta(hours=1, minutes=7, seconds=4)), **changes)


def clipboard(source):
    return EventClipboard.capture([source], source, "single")


@pytest.mark.parametrize("view,chosen", [("day", date(2026, 11, 4)),
                                         ("week", date(2026, 11, 6)),
                                         ("month", date(2026, 11, 18))])
def test_selected_day_paste_ignores_pointer_and_preserves_precise_local_clock(workspace, view, chosen):
    window, store, _ = workspace
    window.set_view(view)
    window.select_day(chosen)
    selection = clipboard(event())
    surface = window.month if view == "month" else window.canvas
    position = QPoint(20, 40) if view == "month" else QPoint(window.canvas.gutter + 20, window.canvas.all_day_height + 12 * 64)
    with patch("PySide6.QtGui.QCursor.pos", return_value=surface.mapToGlobal(position)), \
            patch.object(window, "read_event_clipboard", return_value=selection):
        target = window.paste_target(selection)
        assert target == selection.reference.start.replace(year=chosen.year, month=chosen.month, day=chosen.day)
        window.paste_events(scope="single")
        assert store.list_events() == [] and window.pending_paste is not None
        window.confirm_paste()
    pasted = store.list_events()
    assert len(pasted) == 1
    value = pasted[0]
    assert value.start.astimezone(ZONE).date() == chosen
    assert value.start.astimezone(ZONE).time() == selection.reference.start.time()
    assert value.end.timestamp() - value.start.timestamp() == pytest.approx(4024)
    assert window.selected_day == chosen
    assert len(store.history()) == 1
    window.undo()
    assert store.list_events() == []


def test_foreign_zone_clipboard_keeps_its_displayed_local_clock_and_named_zone(workspace):
    window, store, _ = workspace
    source_zone = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 20, 10, 11, 37, 123456, tzinfo=source_zone)
    source = event(start=start, end=start + timedelta(minutes=41, seconds=12), timezone_id=source_zone.key)
    chosen = date(2026, 11, 18)
    window.set_view("month")
    window.select_day(chosen)
    with patch.object(window, "read_event_clipboard", return_value=clipboard(source)):
        window.paste_events(scope="single")
        assert store.list_events() == []
        window.confirm_paste()
    pasted = store.list_events()[0]
    local = pasted.start.astimezone(ZONE)
    assert local.date() == chosen and local.time() == source.start.astimezone(ZONE).time()
    assert pasted.timezone_id == source_zone.key
    assert pasted.end.timestamp() - pasted.start.timestamp() == pytest.approx(2472)


def test_explicit_context_paste_target_overrides_selected_day_and_source_clock(workspace):
    window, store, _ = workspace
    window.select_day(date(2026, 11, 6))
    target = datetime(2026, 11, 12, 8, 45, 19, tzinfo=ZONE)
    with patch.object(window, "read_event_clipboard", return_value=clipboard(event())):
        window.paste_events(scope="single", target=target)
        assert store.list_events() == []
        window.confirm_paste()
    pasted = store.list_events()[0]
    assert pasted.start == target
    assert pasted.end - pasted.start == event().end - event().start
    assert window.selected_day == target.date()


def test_all_day_paste_keeps_calendar_date_and_span_across_dst(workspace):
    window, store, _ = workspace
    start = datetime(2026, 10, 20, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(days=2), all_day=True)
    chosen = date(2027, 3, 27)
    window.go_date(chosen)
    window.set_view("month")
    with patch.object(window, "read_event_clipboard", return_value=clipboard(source)):
        window.paste_events(scope="single")
        assert store.list_events() == []
        window.confirm_paste()
    pasted = store.list_events()[0]
    assert pasted.all_day and pasted.start.date() == chosen
    assert pasted.start.hour == pasted.end.hour == 0
    assert pasted.end.date() == chosen + timedelta(days=2)
    assert pasted.end.timestamp() - pasted.start.timestamp() == 47 * 3600
    assert window.selected_day == chosen


@pytest.mark.parametrize("widget_name,view,chosen", [("header", "week", date(2026, 11, 6)),
                                                     ("canvas", "week", date(2026, 11, 7)),
                                                     ("month", "month", date(2026, 11, 18))])
def test_day_selection_signals_clear_event_and_update_all_surfaces(workspace, widget_name, view, chosen):
    window, store, _ = workspace
    source = event(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))
    store.apply("Create", [source])
    window.refresh()
    window.select_event(source.id)
    window.set_view(view)
    getattr(window, widget_name).day_selected.emit(chosen)
    assert window.selected_id is None and window.selected_day == chosen
    assert window.mini.selectedDate().toPython() == chosen
    assert all(widget.selected_day == chosen for widget in (window.header, window.canvas, window.month))


def test_mouse_day_selection_clears_pending_shortcut_and_allows_keyboard_paste(workspace):
    window, store, app = workspace
    window.activateWindow()
    window.canvas.setFocus()
    app.processEvents()
    window._shortcut_pending = ['Ctrl+K']
    chosen = date(2026, 11, 6)
    index = window.canvas.days.index(chosen)
    width = (window.header.width() - window.canvas.gutter) / len(window.canvas.days)
    point = QPoint(int(window.canvas.gutter + (index + 0.5) * width), 20)
    with patch.object(window, "read_event_clipboard", return_value=clipboard(event())):
        QTest.mouseClick(window.header, Qt.MouseButton.LeftButton, pos=point)
        assert window.selected_day == chosen and not window._shortcut_pending
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert store.list_events() == [] and window.pending_paste is not None
        QTest.keyClick(window.canvas, Qt.Key.Key_Return)
    pasted = store.list_events()
    assert len(pasted) == 1 and pasted[0].start.astimezone(ZONE).date() == chosen


def test_navigation_view_mini_calendar_and_today_share_one_selected_date(workspace):
    window, _, _ = workspace
    window.go_date(date(2027, 1, 31))
    window.set_view("month")
    window.navigate(1)
    assert window.selected_day == window.anchor == date(2027, 2, 28)
    window.set_view("week")
    window.navigate(-1)
    assert window.selected_day == window.anchor == date(2027, 2, 21)
    window.set_view("day")
    window.navigate(1)
    assert window.selected_day == window.anchor == date(2027, 2, 22)
    assert window.canvas.days == [date(2027, 2, 22)]
    window.mini.clicked.emit(QDate(2027, 5, 9))
    assert window.selected_day == window.anchor == date(2027, 5, 9)
    window.today()
    assert window.selected_day == window.anchor == FIXED_NOW.date()
    assert window.mini.selectedDate().toPython() == FIXED_NOW.date()


def test_month_page_navigation_keeps_selected_day_number_inside_new_month(workspace):
    window, _, _ = workspace
    window.set_view("month")
    window.go_date(date(2026, 11, 4))
    # December's first date is already visible in November's overflow cells.
    # Paging to December keeps day1 inside the new nominal month.
    chosen = date(2026, 12, 1)
    assert chosen in window.dates()
    window.month.day_selected.emit(chosen)
    assert window.anchor.month == 11 and window.selected_day == chosen
    window.navigate(1)
    assert window.anchor.month == 12 and window.selected_day == chosen
    assert window.mini.selectedDate().toPython() == chosen
    window.navigate(-1)
    assert window.anchor.month == 11 and window.selected_day == date(2026, 11, 1)


def test_changing_views_reveals_selected_day_and_refresh_preserves_visible_selection(workspace):
    window, _, _ = workspace
    chosen = date(2026, 11, 6)
    window.select_day(chosen)
    window.refresh()
    assert window.selected_day == chosen
    for view in ("month", "week", "day"):
        window.set_view(view)
        assert window.selected_day == window.anchor == chosen
    assert window.canvas.days == [chosen]


def test_selecting_event_updates_day_but_multiday_segment_can_choose_another_day(workspace):
    window, store, _ = workspace
    start = datetime(2026, 11, 4, 22, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(days=2, hours=3))
    store.apply("Create", [source])
    window.refresh()
    window.select_day(date(2026, 11, 6))
    window.select_event(source.id)
    assert window.selected_day == date(2026, 11, 4)
    window.select_event(source.id, day=date(2026, 11, 5))
    assert window.selected_day == date(2026, 11, 5) and window.selected_id == source.id


def test_moving_selected_event_updates_paste_day_within_the_same_visible_week(workspace):
    window, store, _ = workspace
    start = datetime(2026, 11, 4, 10, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(hours=1))
    store.apply("Create", [source])
    window.refresh()
    window.select_event(source.id)
    target = datetime(2026, 11, 6, 12, tzinfo=ZONE)
    window.move_event(source.id, target, target + timedelta(hours=1), scope="single")
    assert window.selected_id == source.id and window.selected_day == target.date()
    selection = clipboard(event())
    with patch.object(window, "read_event_clipboard", return_value=selection):
        window.paste_events(scope="single")
        assert len(store.list_events()) == 1 and window.pending_paste is not None
        window.confirm_paste()
    pasted = next(value for value in store.list_events() if value.id != source.id)
    assert pasted.start.astimezone(ZONE).date() == target.date()
    assert pasted.start.astimezone(ZONE).time() == selection.reference.start.time()
    window.undo()
    assert store.list_events() == [store.get_event(source.id)]
    assert store.get_event(source.id).start == target


def test_all_day_event_selection_uses_calendar_date_instead_of_foreign_zone_conversion(workspace):
    window, store, _ = workspace
    zone = ZoneInfo("Pacific/Auckland")
    start = datetime(2026, 11, 5, tzinfo=zone)
    source = event(start=start, end=start + timedelta(days=1), timezone_id=zone.key, all_day=True)
    store.apply("Create", [source])
    window.go_date(start.date())
    window.select_event(source.id)
    assert source.start.astimezone(ZONE).date() != source.start.date()
    assert window.selected_day == source.start.date()


def test_all_day_event_remains_visible_in_fixed_lane_while_timed_hours_are_scrolled(workspace):
    window, store, app = workspace
    start = datetime(2026, 11, 4, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(days=1), all_day=True)
    store.apply("Create", [source])
    window.refresh()
    window.resize(window.minimumWidth(), window.minimumHeight())
    window.full_day = True
    window.fit_calendar()
    app.processEvents()
    bar = window.scroll.verticalScrollBar()
    bar.setValue(500)
    assert bar.value() > 0
    before = bar.value()
    window.select_event(source.id)
    window.ensure_event_visible(source.id)
    assert bar.value() == before
    assert window.all_day_canvas.isVisible()
    assert source.id in {value.id for _, value in window.all_day_canvas.hits}
    window.ensure_event_visible(source.id)
    assert bar.value() == before


@pytest.mark.parametrize("origin", ["direct", "header", "canvas", "month", "all_day_canvas"])
def test_all_day_creation_reaches_real_editor_checked_with_exact_day_span(workspace, origin):
    window, store, _ = workspace
    start = datetime(2027, 3, 27, tzinfo=ZONE)
    end = datetime(2027, 3, 29, tzinfo=ZONE)
    inspected = []

    def accept(dialog):
        inspected.append(dialog.all_day.isChecked())
        assert dialog.all_day.isChecked()
        dialog.title.setText("Synthetic all-day event")
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        return dialog.result()

    with patch.object(EventDialog, "exec", new=accept):
        if origin == "direct":
            window.create_event(start, end, all_day=True)
        else:
            getattr(window, origin).create_all_day_requested.emit(start, end)
    assert inspected == [True]
    created = store.list_events()
    assert len(created) == 1 and created[0].all_day
    assert created[0].start.date() == start.date() and created[0].end.date() == end.date()
    assert created[0].start.hour == created[0].end.hour == 0
    assert len(store.history()) == 1
    window.undo()
    assert store.list_events() == []


def test_keyboard_creation_defaults_to_selected_day(workspace):
    window, _, _ = workspace
    chosen = date(2026, 11, 6)
    window.select_day(chosen)
    observed = []

    def reject(dialog):
        observed.append((dialog.start.dateTime().date().toPython(), dialog.all_day.isChecked()))
        return QDialog.DialogCode.Rejected

    with patch.object(EventDialog, "exec", new=reject):
        window.create_event()
    assert observed == [(chosen, False)]


def test_paste_into_nonexistent_clock_time_rejects_without_an_operation(workspace):
    window, store, _ = workspace
    start = datetime(2027, 3, 21, 2, 30, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(minutes=20))
    window.go_date(date(2027, 3, 28))
    with patch.object(window, "read_event_clipboard", return_value=clipboard(source)):
        window.paste_events(scope="single")
    assert store.list_events() == [] and store.history() == []
    assert "daylight" in window.local_issues[-1].details.lower()


def test_paste_retains_repeated_hour_choice_and_duration(workspace):
    window, store, _ = workspace
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    source = event(start=start, end=datetime(2026, 10, 25, 3, 30, tzinfo=ZONE))
    window.go_date(date(2027, 10, 31))
    with patch.object(window, "read_event_clipboard", return_value=clipboard(source)):
        window.paste_events(scope="single")
        assert store.list_events() == [] and window.pending_paste is not None
        window.confirm_paste()
    pasted = store.list_events()[0]
    target = datetime(2027, 10, 31, 2, 30, tzinfo=ZONE, fold=1)
    assert pasted.start.timestamp() == target.timestamp()
    assert pasted.end.timestamp() - pasted.start.timestamp() == 3600


def test_group_paste_preflights_other_members_in_daylight_gap_before_any_write(workspace):
    window, store, _ = workspace
    start = datetime(2027, 3, 20, 1, 30, tzinfo=ZONE)
    source = event(start=start, end=start + timedelta(minutes=20), group_id="source-group")
    later_start = datetime(2027, 3, 21, 2, 30, tzinfo=ZONE)
    later = replace(source, id="later", ical_uid="later@example.invalid", start=later_start,
                    end=later_start + timedelta(minutes=20))
    selection = EventClipboard.capture([source, later], source, "group")
    window.go_date(date(2027, 3, 27))
    with patch.object(window, "read_event_clipboard", return_value=selection):
        window.paste_events(scope="group")
    assert store.list_events() == [] and store.history() == []
    assert "daylight" in window.local_issues[-1].details.lower()
