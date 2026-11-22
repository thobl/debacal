"""Source popup grouping is separate from event visibility and selected sources."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QVersionNumber, qVersion
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

from debacal.calendar_list import CALENDAR_ORDER_MIME, CalendarList, CalendarRow
from debacal.models import Calendar
from debacal.preferences import Preferences


def calendar(identity, *, main=True, visible=True):
    return Calendar(identity, identity, '#587c67', in_main_list=main, visible=visible)


class DragMixin:
    def source(self):
        return self.row


class Enter(DragMixin, QDragEnterEvent):
    def __init__(self, row, point):
        self.row, self.mime = row, QMimeData()
        self.mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode())
        # Qt 6.12 added floating-point positions and deprecated the integer overload.
        point_type = QPointF if QVersionNumber.fromString(qVersion()) >= QVersionNumber(6, 12) else QPoint
        super().__init__(point_type(point), Qt.DropAction.MoveAction, self.mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


class Drop(DragMixin, QDropEvent):
    def __init__(self, row, point):
        self.row, self.mime = row, QMimeData()
        self.mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode())
        super().__init__(QPointF(point), Qt.DropAction.MoveAction, self.mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        def make(calendars):
            listing = CalendarList()
            owned.append(listing)
            rows = []
            for source in calendars:
                row = CalendarRow(source)
                row.set_preferences(Preferences())
                listing.attach_row(row)
                rows.append(row)
            listing.set_rows(rows)
            listing.resize(290, 400)
            listing.show()
            app.processEvents()
            return listing
        yield app, make
        for widget in reversed(owned):
            widget.set_expanded(False)
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_popup_is_independent_of_main_list_geometry_and_event_visibility(desktop):
    app, make = desktop
    listing = make([calendar('rare', main=False), calendar('main', visible=False), calendar('other', main=False)])
    main, rare, other = listing.rows()
    assert listing.ordered_ids() == ['main', 'rare', 'other']
    assert listing.visible_row_count() == 1
    assert not listing.more_expanded and listing.more_button.isVisible()
    assert main.isVisible() and not rare.isVisible() and not other.isVisible()
    assert rare.calendar.visible and not main.calendar.visible
    height = listing.content_height()
    QTest.mouseClick(listing.more_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert listing.more_expanded and listing.popup.isVisible()
    assert listing.visible_row_count() == 1
    assert rare.isVisible() and other.isVisible() and listing.content_height() == height
    assert listing.display_rows(True) == [main] and listing.display_rows(False) == [rare, other]
    visible = QSignalSpy(rare.visibility_requested)
    QTest.mouseClick(rare.eye, Qt.MouseButton.LeftButton)
    assert visible.at(0) == ['rare', False]
    assert not rare.calendar.in_main_list
    QTest.mouseClick(listing.more_button, Qt.MouseButton.LeftButton)
    assert not rare.isVisible() and not other.isVisible() and listing.content_height() == height
    listing.more_button.setFocus()
    QTest.keyClick(listing.more_button, Qt.Key.Key_Space)
    assert listing.more_expanded and rare.isVisible()


def test_status_change_emits_even_when_order_is_unchanged_and_preserves_source_metadata(desktop):
    _, make = desktop
    source = replace(calendar('selected', visible=False), kind='caldav',
                     url='https://calendar.example.test/dav/synthetic/', username='synthetic')
    listing = make([source, calendar('rare', main=False)])
    row = listing.rows()[0]
    row.set_calendar(source, current=True)
    changed = QSignalSpy(listing.grouped_layout_changed)
    order = QSignalSpy(listing.order_changed)
    assert listing.move_calendar('selected', 0, in_main_list=False)
    assert listing.ordered_ids() == ['selected', 'rare']
    assert changed.at(0) == [['selected', 'rare'], []] and order.count() == 0
    assert row.calendar == replace(source, in_main_list=False)
    assert row.choice.isChecked() and row.choice.font().underline()
    assert listing.visible_row_count() == 1
    assert row.isVisible() and listing.display_rows(True) == [row]
    assert listing.display_rows(False) == [listing.rows()[1]]
    assert not listing.move_calendar('selected', 0, in_main_list=False)
    assert changed.count() == 1


@pytest.mark.parametrize('initial_main', [True, False])
def test_authenticated_drag_can_drop_into_either_empty_group(desktop, initial_main):
    app, make = desktop
    listing = make([calendar('one', main=initial_main), calendar('two', main=initial_main)])
    if not initial_main:
        listing.set_expanded(True)
    row = listing.rows()[0]
    if initial_main:
        listing._accept_more_drag(Enter(row, listing.more_button.rect().center()))
        listing._expand_during_drag()
        app.processEvents()
        destination = listing.popup_list
        point = QPoint(10, 0)
    else:
        listing.dragEnterEvent(Enter(row, QPoint(10, 0)))
        app.processEvents()
        assert listing.main_drop_target.isVisible()
        destination = listing
        point = listing.main_drop_target.geometry().center()
    destination.dragEnterEvent(Enter(row, point))
    request = Drop(row, point)
    destination.dropEvent(request)
    assert request.isAccepted() and row.calendar.in_main_list != initial_main
    assert set(listing.ordered_ids()) == {'one', 'two'}
    assert [value.calendar.id for value in listing.group_rows(True)] == (['two'] if initial_main else ['one'])
    assert [value.calendar.id for value in listing.group_rows(False)] == (['one'] if initial_main else ['two'])
    assert listing.main_drop_target.isHidden()
    assert listing.more_button.property('iconName') == 'more'
    assert not listing.more_button.icon().isNull()
    assert not listing._dragging


def test_ellipsis_accepts_main_drag_and_hover_opens_popup(desktop):
    app, make = desktop
    listing = make([calendar('main'), calendar('first', main=False), calendar('last', main=False)])
    main = listing.rows()[0]
    point = listing.more_button.rect().center()
    listing._accept_more_drag(Enter(main, point))
    assert listing._drop_group is False and listing._expand_timer.isActive()
    listing._expand_during_drag()
    app.processEvents()
    assert listing.more_expanded and listing.visible_row_count() == 1
    listing.popup_list.dropEvent(Drop(main, QPoint(10, 0)))
    assert listing.ordered_ids() == ['main', 'first', 'last']
    assert listing.group_rows(True) == []
    assert not listing._scroll_timer.isActive() and not listing._expand_timer.isActive()
    assert listing.more_button.isVisible()


def test_external_drag_never_reveals_empty_targets_or_changes_groups(desktop):
    _, make = desktop
    listing, other = make([calendar('main')]), make([calendar('outside')])
    request = Enter(other.rows()[0], QPoint(10, 0))
    listing.dragEnterEvent(request)
    assert not request.isAccepted() and not listing._dragging
    assert listing.more_button.isHidden() and listing.main_drop_target.isHidden()
    assert listing.ordered_ids() == ['main']


def test_refresh_preserves_open_group_and_row_identity_without_repolishing_controls(desktop):
    app, make = desktop
    listing = make([calendar('main'), calendar('rare', main=False)])
    listing.set_expanded(True)
    app.processEvents()
    rows = listing.rows()
    geometry = listing.popup.geometry()
    with patch.object(listing.box, 'insertWidget', wraps=listing.box.insertWidget) as insert, \
            patch.object(listing.more_button, 'setStyleSheet', wraps=listing.more_button.setStyleSheet) as polish:
        for _ in range(20):
            listing.set_rows(rows)
            app.processEvents()
            assert listing.popup.geometry() == geometry
        assert insert.call_count == polish.call_count == 0
    assert listing.more_expanded and listing.rows() == rows and listing.visible_row_count() == 1
    listing.set_rows([rows[0]])
    assert listing.more_button.isHidden() and listing.rows() == [rows[0]] and not rows[1].isVisible()
    assert not listing.more_expanded and not listing.popup.isVisible()


@pytest.mark.parametrize('reopen_during_drag', [False, True])
def test_popup_drag_keeps_original_pointer_offset_when_empty_main_target_appears(desktop, monkeypatch, reopen_during_drag):
    app, make = desktop
    listing = make([calendar('one', main=False), calendar('two', main=False)])
    listing.set_expanded(True)
    app.processEvents()
    geometry = listing.popup.geometry()
    row = listing.rows()[0]
    hotspot = QPoint(12, 10)
    anchor = row.mapToGlobal(hotspot)
    original_image = row.grab().size()
    observed = []
    def execute(drag, _allowed, _default):
        assert listing.main_drop_target.isVisible()
        assert not listing.more_expanded and not listing.popup.isVisible()
        assert row in listing.rows() and row.calendar.id == 'one'
        assert drag.source() is row
        assert drag.hotSpot() == hotspot and drag.pixmap().size() == original_image
        if reopen_during_drag:
            request = Enter(row, listing.more_button.rect().center())
            assert listing._accept_more_drag(request) and request.isAccepted()
            listing._expand_during_drag()
            app.processEvents()
            assert listing.more_expanded and listing.popup.geometry() == geometry
            assert row in listing.rows() and row.isVisible()
        observed.append(True)
        return Qt.DropAction.IgnoreAction
    monkeypatch.setattr(QDrag, 'exec', execute)
    listing.start_drag(row, anchor)
    assert observed == [True] and listing.main_drop_target.isHidden()
    assert not listing.popup.isVisible() and not listing._dragging
    listing.set_expanded(True)
    assert listing.more_expanded and listing.popup.isVisible()
    assert row in listing.rows() and row.isVisible()


def test_open_popup_geometry_and_scroll_stay_fixed_through_membership_refreshes(desktop):
    app, make = desktop
    listing = make([calendar('main')] + [calendar(str(index), main=False) for index in range(30)])
    listing.set_expanded(True)
    app.processEvents()
    scrollbar = listing.popup_scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(min(100, scrollbar.maximum()))
    geometry, position = listing.popup.geometry(), scrollbar.value()
    for _ in range(12):
        listing.set_rows(listing.rows())
        app.processEvents()
        assert listing.popup.geometry() == geometry and scrollbar.value() == position
    assert listing.move_calendar('main', 0, in_main_list=False)
    app.processEvents()
    assert listing.popup.isVisible() and listing.popup.geometry() == geometry
    assert scrollbar.value() == position
    assert listing.move_calendar('29', 0, in_main_list=True)
    app.processEvents()
    assert listing.popup.isVisible() and listing.popup.geometry() == geometry
    assert scrollbar.value() == position


def test_current_occasional_calendar_stays_in_main_without_changing_saved_group(desktop):
    _, make = desktop
    listing = make([calendar('main'), calendar('first', main=False), calendar('second', main=False)])
    main, first, second = listing.rows()
    first.set_calendar(first.calendar, current=True)
    listing.set_rows(listing.rows())
    assert first.isVisible() and listing.display_rows(True) == [main, first]
    assert listing.display_rows(False) == [second]
    assert not first.calendar.in_main_list
    first.set_calendar(first.calendar, current=False)
    second.set_calendar(second.calendar, current=True)
    listing.set_rows(listing.rows())
    assert not first.isVisible() and second.isVisible()
    assert listing.display_rows(True) == [main, second]
    assert listing.group_rows(False) == [first, second]


def test_drag_exposes_empty_overflow_target_without_resizing_main_list(desktop):
    _, make = desktop
    listing = make([calendar('first'), calendar('second')])
    height = listing.content_height()
    assert listing.more_button.isHidden()
    listing._begin_drag()
    assert listing.more_button.isVisible()
    assert listing.content_height() == height
    listing._clear_drop()
    assert not listing._dragging and listing.more_button.isHidden()
    assert not listing._scroll_timer.isActive() and not listing._expand_timer.isActive()


@pytest.mark.parametrize('parent_in_popup', [False, True])
def test_escape_dismisses_foreground_menu_or_dialog_before_calendar_popup(desktop, parent_in_popup):
    app, make = desktop
    listing = make([calendar('main'), calendar('rare', main=False)])
    listing.set_expanded(True)
    app.processEvents()
    foreground_parent = listing.popup if parent_in_popup else listing
    menu = QMenu(foreground_parent)
    menu.addAction('Calendar action')
    menu.popup(listing.popup.mapToGlobal(QPoint(8, 8)))
    app.processEvents()
    assert menu.isVisible()
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    app.processEvents()
    assert not menu.isVisible() and listing.popup.isVisible()
    dialog = QDialog(foreground_parent)
    dialog.setModal(True)
    dialog.show()
    app.processEvents()
    assert dialog.isVisible()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert not dialog.isVisible() and listing.popup.isVisible()
    QTest.keyClick(listing.popup, Qt.Key.Key_Escape)
    assert not listing.popup.isVisible() and not listing.more_expanded
    assert not listing.more_button.isChecked()
    menu.deleteLater()
    dialog.deleteLater()


def test_outside_click_and_wheel_dismiss_popup_but_inside_scroll_does_not(desktop):
    app, make = desktop
    listing = make([calendar('main'), calendar('rare', main=False)])
    listing.set_expanded(True)
    QTest.mouseClick(listing, Qt.MouseButton.LeftButton, pos=QPoint(280, 380))
    assert not listing.popup.isVisible()
    listing.set_expanded(True)
    app.processEvents()

    def wheel(widget):
        point = QPoint(5, 5)
        event = QWheelEvent(QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)

    wheel(listing.popup_scroll.viewport())
    assert listing.popup.isVisible()
    wheel(listing)
    assert not listing.popup.isVisible()


def test_owner_resize_dismisses_popup_instead_of_leaving_it_detached(desktop):
    app, make = desktop
    listing = make([calendar('main'), calendar('rare', main=False)])
    listing.set_expanded(True)
    app.processEvents()
    listing.resize(listing.width() + 20, listing.height() + 20)
    app.processEvents()
    assert not listing.popup.isVisible()
